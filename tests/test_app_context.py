"""
File: test_app_context.py
Description: Tests for NFC insert intercepts on AppContext — pending
             registration vs pending tag-bind, bind success/fail, expired
             bind/registration fall-through, and leftover admin session
             after enroll. In-memory SQLite, no hardware.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_app_context.py -v
"""

import asyncio
import time

from smart_locker.api.app_context import (
    AppContext,
    PendingRegistration,
    PendingTagBind,
)
from smart_locker.database.engine import get_session
from smart_locker.database.models import DeviceStatus
from smart_locker.database.repositories import DeviceRepository, UserRepository
from smart_locker.security.encryption import encrypt
from smart_locker.security.hashing import compute_uid_hmac


def _make_ctx(monkeypatch) -> AppContext:
    """Real AppContext with the fake reader so __init__ needs no PC/SC."""
    monkeypatch.setenv("SMART_LOCKER_FAKE_READER", "1")
    return AppContext()


def _run(ctx: AppContext, uid: str) -> None:
    asyncio.run(ctx._dispatch_insert(uid, get_session, "test-reader"))


def _events(ctx: AppContext) -> list[dict]:
    out = []
    while True:
        try:
            out.append(ctx.sse_queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return out


class TestPendingTagBindExpiry:
    """PendingTagBind 60s window."""

    def test_is_expired_after_timeout(self):
        pending = PendingTagBind(device_id=1, created_at=time.monotonic() - 61)
        assert pending.is_expired is True

    def test_is_not_expired_when_fresh(self):
        pending = PendingTagBind(device_id=1)
        assert pending.is_expired is False


class TestDispatchBind:
    """Bridge intercept: next insert binds, does not borrow."""

    def test_unbound_sticker_binds_and_does_not_borrow(
        self, db_session, hmac_key, monkeypatch
    ):
        device = DeviceRepository.create(
            db_session, name="Fluke 87V", device_type="t", pm_number="PM-001",
        )
        db_session.commit()
        ctx = _make_ctx(monkeypatch)
        ctx.pending_tag_bind = PendingTagBind(device_id=device.id)
        _run(ctx, "AABBCCDD")
        events = _events(ctx)
        assert ctx.pending_tag_bind is None
        assert len(events) == 1
        assert events[0]["event"] == "tag_bind_success"
        assert events[0]["device_id"] == device.id
        assert events[0]["device_name"] == "Fluke 87V"
        assert events[0]["pm_number"] == "PM-001"
        db_session.expire_all()
        found = DeviceRepository.find_by_id(db_session, device.id)
        assert found.tag_hmac == compute_uid_hmac("AABBCCDD", hmac_key)
        assert found.status == DeviceStatus.AVAILABLE

    def test_work_card_fails_bind_no_logout(
        self, db_session, enc_key, hmac_key, monkeypatch
    ):
        uid = "A1B2C3D4"
        alice = UserRepository.create(
            db_session,
            display_name="Alice",
            uid_hmac=compute_uid_hmac(uid, hmac_key),
            encrypted_card_uid=encrypt(uid, enc_key),
        )
        device = DeviceRepository.create(
            db_session, name="Fluke 87V", device_type="t", pm_number="PM-001",
        )
        db_session.commit()
        ctx = _make_ctx(monkeypatch)
        ctx.session_mgr.start_session(alice)
        ctx.pending_tag_bind = PendingTagBind(device_id=device.id)
        _run(ctx, uid)
        events = _events(ctx)
        assert events[0]["event"] == "tag_bind_failed"
        assert ctx.session_mgr.has_active_session
        db_session.expire_all()
        assert DeviceRepository.find_by_id(db_session, device.id).tag_hmac is None

    def test_other_device_tag_fails_first_keeps_hmac(
        self, db_session, hmac_key, monkeypatch
    ):
        uid = "AABBCCDD"
        first = DeviceRepository.create(
            db_session, name="A", device_type="t", pm_number="PM-A",
        )
        second = DeviceRepository.create(
            db_session, name="B", device_type="t", pm_number="PM-B",
        )
        digest = compute_uid_hmac(uid, hmac_key)
        DeviceRepository.bind_tag(db_session, first, digest)
        db_session.commit()
        ctx = _make_ctx(monkeypatch)
        ctx.pending_tag_bind = PendingTagBind(device_id=second.id)
        _run(ctx, uid)
        events = _events(ctx)
        assert events[0]["event"] == "tag_bind_failed"
        db_session.expire_all()
        assert DeviceRepository.find_by_id(db_session, first.id).tag_hmac == digest
        assert DeviceRepository.find_by_id(db_session, second.id).tag_hmac is None

    def test_expired_bind_falls_through_to_classify(
        self, db_session, enc_key, hmac_key, monkeypatch
    ):
        uid = "A1B2C3D4"
        UserRepository.create(
            db_session,
            display_name="Alice",
            uid_hmac=compute_uid_hmac(uid, hmac_key),
            encrypted_card_uid=encrypt(uid, enc_key),
        )
        device = DeviceRepository.create(
            db_session, name="Fluke 87V", device_type="t", pm_number="PM-001",
        )
        db_session.commit()
        ctx = _make_ctx(monkeypatch)
        ctx.pending_tag_bind = PendingTagBind(
            device_id=device.id, created_at=time.monotonic() - 61
        )
        _run(ctx, uid)
        events = _events(ctx)
        assert ctx.pending_tag_bind is None
        assert events[0]["event"] == "auth_success"
        db_session.expire_all()
        assert DeviceRepository.find_by_id(db_session, device.id).tag_hmac is None


class TestDispatchRegistration:
    """Registration intercept rejects a device-tag UID; runs before bind."""

    def test_registration_rejects_device_tag(
        self, db_session, hmac_key, monkeypatch
    ):
        uid = "AABBCCDD"
        device = DeviceRepository.create(
            db_session, name="Fluke 87V", device_type="t", pm_number="PM-001",
        )
        DeviceRepository.bind_tag(
            db_session, device, compute_uid_hmac(uid, hmac_key)
        )
        db_session.commit()
        ctx = _make_ctx(monkeypatch)
        ctx.pending_registration = PendingRegistration("Bob")
        _run(ctx, uid)
        events = _events(ctx)
        assert events[0]["event"] == "registration_failed"
        assert "bound to a device" in events[0]["reason"].lower()
        db_session.expire_all()
        assert UserRepository.find_by_display_name(db_session, "Bob") is None

    def test_registration_runs_before_pending_bind(
        self, db_session, hmac_key, monkeypatch
    ):
        device = DeviceRepository.create(
            db_session, name="Fluke 87V", device_type="t", pm_number="PM-001",
        )
        db_session.commit()
        ctx = _make_ctx(monkeypatch)
        ctx.pending_registration = PendingRegistration("Bob")
        ctx.pending_tag_bind = PendingTagBind(device_id=device.id)
        _run(ctx, "FEEDBEEF")
        events = _events(ctx)
        assert events[0]["event"] == "registration_success"
        assert events[0]["user"]["name"] == "Bob"
        db_session.expire_all()
        assert DeviceRepository.find_by_id(db_session, device.id).tag_hmac is None
        assert UserRepository.find_by_display_name(db_session, "Bob") is not None

    def test_admin_enroll_then_next_tap_logs_in(
        self, db_session, enc_key, hmac_key, monkeypatch
    ):
        """After admin enroll, leftover session must not turn the next tap into logout."""
        admin = UserRepository.create(
            db_session,
            display_name="Admin",
            uid_hmac=compute_uid_hmac("AAAA1111", hmac_key),
            encrypted_card_uid=encrypt("AAAA1111", enc_key),
            role="admin",
        )
        db_session.commit()
        ctx = _make_ctx(monkeypatch)
        ctx.session_mgr.start_session(admin)
        ctx.admin_overlay_open = True
        ctx.pending_registration = PendingRegistration("Bob")
        new_uid = "B0B0B0B0"
        _run(ctx, new_uid)
        events = _events(ctx)
        assert events[0]["event"] == "registration_success"
        assert events[0]["user"]["name"] == "Bob"
        assert all(e["event"] != "session_ended" for e in events)
        assert not ctx.session_mgr.has_active_session
        assert ctx.admin_overlay_open is False

        _run(ctx, new_uid)
        events = _events(ctx)
        assert events[0]["event"] == "auth_success"
        assert events[0]["user"]["name"] == "Bob"
        assert ctx.session_mgr.has_active_session

    def test_expired_registration_falls_through_to_login(
        self, db_session, enc_key, hmac_key, monkeypatch
    ):
        """Expired pending_registration must not consume the next work-card tap."""
        uid = "A1B2C3D4"
        UserRepository.create(
            db_session,
            display_name="Alice",
            uid_hmac=compute_uid_hmac(uid, hmac_key),
            encrypted_card_uid=encrypt(uid, enc_key),
        )
        db_session.commit()
        ctx = _make_ctx(monkeypatch)
        ctx.pending_registration = PendingRegistration(
            "Bob", created_at=time.monotonic() - 61
        )
        _run(ctx, uid)
        events = _events(ctx)
        assert ctx.pending_registration is None
        assert events[0]["event"] == "auth_success"
        assert events[0]["user"]["name"] == "Alice"

    def test_expired_registration_with_leftover_session_logs_in(
        self, db_session, enc_key, hmac_key, monkeypatch
    ):
        """Expired admin register window plus leftover overlay session → login, not logout."""
        uid = "A1B2C3D4"
        UserRepository.create(
            db_session,
            display_name="Alice",
            uid_hmac=compute_uid_hmac(uid, hmac_key),
            encrypted_card_uid=encrypt(uid, enc_key),
        )
        admin = UserRepository.create(
            db_session,
            display_name="Admin",
            uid_hmac=compute_uid_hmac("AAAA1111", hmac_key),
            encrypted_card_uid=encrypt("AAAA1111", enc_key),
            role="admin",
        )
        db_session.commit()
        ctx = _make_ctx(monkeypatch)
        ctx.session_mgr.start_session(admin)
        ctx.admin_overlay_open = True
        ctx.pending_registration = PendingRegistration(
            "Bob", created_at=time.monotonic() - 61
        )
        _run(ctx, uid)
        events = _events(ctx)
        assert ctx.pending_registration is None
        assert events[0]["event"] == "auth_success"
        assert events[0]["user"]["name"] == "Alice"
        assert ctx.session_mgr.current_session.user.display_name == "Alice"
