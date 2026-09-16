"""
File: test_replace_window.py
Description: Tests for the card-replace NFC window — HTTP arming, cancel,
             expiry, real-dispatch taps (sticker, inactive, same-card),
             double-arm conflicts, inactive targets, session-end cleanup,
             and blank-name rejection.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_replace_window.py -v
"""

import asyncio
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import smart_locker.api.app_context as ctx_module
from smart_locker.api.app_context import (
    AppContext,
    PendingRegistration,
    PendingTagBind,
)
from smart_locker.api.routes import router
from smart_locker.database.engine import get_session
from smart_locker.database.repositories import (
    DeviceRepository,
    RegistrantRepository,
    UserRepository,
)
from smart_locker.security.encryption import encrypt
from smart_locker.security.hashing import compute_uid_hmac


@pytest.fixture()
def ctx(monkeypatch):
    """Real AppContext installed as the process-global context."""
    monkeypatch.setenv("SMART_LOCKER_FAKE_READER", "1")
    real = AppContext()
    ctx_module.context = real
    yield real
    ctx_module.context = None


@pytest.fixture()
def client(db_session, ctx):
    """TestClient as the kiosk (loopback) sharing the test database."""
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, client=("127.0.0.1", 50000))


def _make_user(db_session, uid, enc_key, hmac_key, name="Alice", role="user"):
    """Persist a user on the given card UID."""
    user = UserRepository.create(
        db_session,
        display_name=name,
        uid_hmac=compute_uid_hmac(uid, hmac_key),
        encrypted_card_uid=encrypt(uid, enc_key),
        role=role,
    )
    db_session.commit()
    return user


def _run(ctx: AppContext, uid: str) -> None:
    """Dispatch one NFC insert through the real bridge path."""
    asyncio.run(ctx._dispatch_insert(uid, get_session, "test-reader"))


def _events(ctx: AppContext) -> list[dict]:
    """Drain the SSE queue."""
    out = []
    while True:
        try:
            out.append(ctx.sse_queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return out


class TestReplaceWindowHttp:
    """HTTP arming, cancel, conflict, and cleanup of the replace window."""

    def test_replace_arm_then_cancel_clears_pending(
        self, client, db_session, ctx, enc_key, hmac_key
    ):
        """Replace-arm then POST /api/register/cancel leaves no pending."""
        admin = _make_user(
            db_session, "ADADADAD", enc_key, hmac_key, "Admin", "admin"
        )
        target = _make_user(db_session, "A1B2C3D4", enc_key, hmac_key)
        ctx.session_mgr.start_session(admin)
        resp = client.post(f"/api/admin/users/{target.id}/replace-card")
        assert resp.status_code == 200
        assert ctx.pending_registration is not None
        assert ctx.pending_registration.replace_user_id == target.id
        resp = client.post("/api/register/cancel")
        assert resp.status_code == 200
        assert resp.json()["cancelled"] is True
        assert ctx.pending_registration is None

    def test_register_arm_then_cancel_clears_pending(
        self, client, db_session, ctx
    ):
        """Self-service register-arm then cancel leaves no pending."""
        RegistrantRepository.add_names(db_session, {"Alice"})
        db_session.commit()
        resp = client.post("/api/register", json={"name": "Alice"})
        assert resp.status_code == 200
        assert ctx.pending_registration is not None
        assert ctx.pending_registration.replace_user_id is None
        resp = client.post("/api/register/cancel")
        assert resp.status_code == 200
        assert resp.json()["cancelled"] is True
        assert ctx.pending_registration is None

    def test_arm_succeeds_after_expired_registration(
        self, client, db_session, ctx, enc_key, hmac_key
    ):
        """A 61s-expired registration window does not block a fresh arm."""
        admin = _make_user(
            db_session, "ADADADAD", enc_key, hmac_key, "Admin", "admin"
        )
        target = _make_user(db_session, "A1B2C3D4", enc_key, hmac_key)
        ctx.session_mgr.start_session(admin)
        ctx.pending_registration = PendingRegistration(
            "Stale", created_at=time.monotonic() - 61
        )
        resp = client.post(f"/api/admin/users/{target.id}/replace-card")
        assert resp.status_code == 200
        pending = ctx.pending_registration
        assert pending.replace_user_id == target.id
        assert pending.is_expired is False

    def test_arm_succeeds_after_expired_bind(
        self, client, db_session, ctx, enc_key, hmac_key
    ):
        """A 61s-expired tag-bind window does not block a fresh arm."""
        admin = _make_user(
            db_session, "ADADADAD", enc_key, hmac_key, "Admin", "admin"
        )
        device = DeviceRepository.create(
            db_session, name="Fluke 87V", device_type="t", pm_number="PM-001",
        )
        db_session.commit()
        ctx.session_mgr.start_session(admin)
        ctx.pending_tag_bind = PendingTagBind(
            device_id=device.id, created_at=time.monotonic() - 61
        )
        resp = client.post("/api/admin/register", json={"name": "New Hire"})
        assert resp.status_code == 200
        assert ctx.pending_tag_bind is None
        assert ctx.pending_registration.display_name == "New Hire"

    def test_replace_arm_twice_second_conflicts(
        self, client, db_session, ctx, enc_key, hmac_key
    ):
        """A second replace-arm for another user is 409; first arm kept."""
        admin = _make_user(
            db_session, "ADADADAD", enc_key, hmac_key, "Admin", "admin"
        )
        alice = _make_user(db_session, "A1B2C3D4", enc_key, hmac_key, "Alice")
        bob = _make_user(db_session, "BBBB2222", enc_key, hmac_key, "Bob")
        ctx.session_mgr.start_session(admin)
        resp = client.post(f"/api/admin/users/{alice.id}/replace-card")
        assert resp.status_code == 200
        resp = client.post(f"/api/admin/users/{bob.id}/replace-card")
        assert resp.status_code == 409
        assert ctx.pending_registration.replace_user_id == alice.id

    def test_replace_card_inactive_or_unknown_target_404(
        self, client, db_session, ctx, enc_key, hmac_key
    ):
        """Replace-card on an inactive or missing user is 404, nothing armed."""
        admin = _make_user(
            db_session, "ADADADAD", enc_key, hmac_key, "Admin", "admin"
        )
        target = _make_user(db_session, "A1B2C3D4", enc_key, hmac_key)
        UserRepository.deactivate(db_session, target)
        db_session.commit()
        ctx.session_mgr.start_session(admin)
        resp = client.post(f"/api/admin/users/{target.id}/replace-card")
        assert resp.status_code == 404
        assert ctx.pending_registration is None
        resp = client.post("/api/admin/users/9999/replace-card")
        assert resp.status_code == 404
        assert ctx.pending_registration is None

    def test_replace_arm_then_session_end_clears_pending(
        self, client, db_session, ctx, enc_key, hmac_key
    ):
        """Ending the kiosk session drops an armed replace window."""
        admin = _make_user(
            db_session, "ADADADAD", enc_key, hmac_key, "Admin", "admin"
        )
        target = _make_user(db_session, "A1B2C3D4", enc_key, hmac_key)
        ctx.session_mgr.start_session(admin)
        resp = client.post(f"/api/admin/users/{target.id}/replace-card")
        assert resp.status_code == 200
        assert ctx.pending_registration is not None
        resp = client.post("/api/session/end")
        assert resp.status_code == 200
        assert ctx.session_mgr.has_active_session is False
        assert ctx.pending_registration is None

    def test_whitespace_admin_register_name_422(
        self, client, db_session, ctx, enc_key, hmac_key
    ):
        """A whitespace-only admin-register name is 422 and arms nothing."""
        admin = _make_user(
            db_session, "ADADADAD", enc_key, hmac_key, "Admin", "admin"
        )
        ctx.session_mgr.start_session(admin)
        for blank in (" ", "   "):
            resp = client.post("/api/admin/register", json={"name": blank})
            assert resp.status_code == 422
            assert "blank" in resp.json()["detail"].lower()
            assert ctx.pending_registration is None
        resp = client.post("/api/admin/register", json={"name": "New Hire"})
        assert resp.status_code == 200
        assert ctx.pending_registration.display_name == "New Hire"


class TestReplaceWindowDispatch:
    """Real-dispatch taps during an armed replace window."""

    def test_sticker_tap_fails_and_old_card_still_works(
        self, client, db_session, ctx, enc_key, hmac_key
    ):
        """A sticker UID tap fails the replace; the old card still logs in."""
        admin = _make_user(
            db_session, "ADADADAD", enc_key, hmac_key, "Admin", "admin"
        )
        target = _make_user(db_session, "A1B2C3D4", enc_key, hmac_key)
        device = DeviceRepository.create(
            db_session, name="Fluke 87V", device_type="t", pm_number="PM-001",
        )
        DeviceRepository.bind_tag(
            db_session, device, compute_uid_hmac("AABBCCDD", hmac_key)
        )
        db_session.commit()
        ctx.session_mgr.start_session(admin)
        ctx.admin_overlay_open = True
        resp = client.post(f"/api/admin/users/{target.id}/replace-card")
        assert resp.status_code == 200
        _run(ctx, "AABBCCDD")
        events = _events(ctx)
        assert events[0]["event"] == "registration_failed"
        assert events[0]["replaced"] is True
        assert events[0]["replace_user_id"] == target.id
        assert "bound to a device" in events[0]["reason"].lower()
        assert ctx.session_mgr.has_active_session
        db_session.expire_all()
        assert ctx.authenticator.authenticate(db_session, "A1B2C3D4").id == target.id

    def test_inactive_user_card_tap_fails(
        self, client, db_session, ctx, enc_key, hmac_key
    ):
        """Tapping a deactivated user's card fails the replace."""
        admin = _make_user(
            db_session, "ADADADAD", enc_key, hmac_key, "Admin", "admin"
        )
        target = _make_user(db_session, "A1B2C3D4", enc_key, hmac_key)
        other = _make_user(db_session, "C3C3C3C3", enc_key, hmac_key, "Cara")
        UserRepository.deactivate(db_session, other)
        db_session.commit()
        ctx.session_mgr.start_session(admin)
        ctx.admin_overlay_open = True
        resp = client.post(f"/api/admin/users/{target.id}/replace-card")
        assert resp.status_code == 200
        _run(ctx, "C3C3C3C3")
        events = _events(ctx)
        assert events[0]["event"] == "registration_failed"
        assert events[0]["replaced"] is True
        assert events[0]["replace_user_id"] == target.id
        assert events[0]["reason"] == "This card is already registered."
        assert ctx.session_mgr.has_active_session
        db_session.expire_all()
        assert ctx.authenticator.authenticate(db_session, "A1B2C3D4").id == target.id

    def test_same_card_tap_success_shape(
        self, client, db_session, ctx, enc_key, hmac_key
    ):
        """Tapping the user's current card is a success carrying replace_user_id."""
        admin = _make_user(
            db_session, "ADADADAD", enc_key, hmac_key, "Admin", "admin"
        )
        target = _make_user(db_session, "A1B2C3D4", enc_key, hmac_key)
        db_session.commit()
        ctx.session_mgr.start_session(admin)
        ctx.admin_overlay_open = True
        resp = client.post(f"/api/admin/users/{target.id}/replace-card")
        assert resp.status_code == 200
        _run(ctx, "A1B2C3D4")
        events = _events(ctx)
        assert len(events) == 1
        assert set(events[0]) == {"event", "user", "replaced", "replace_user_id"}
        assert events[0]["event"] == "registration_success"
        assert events[0]["replaced"] is True
        assert events[0]["replace_user_id"] == target.id
        assert events[0]["user"] == {
            "id": target.id,
            "name": "Alice",
            "role": "user",
        }
        assert ctx.session_mgr.has_active_session
        db_session.expire_all()
        assert ctx.authenticator.authenticate(db_session, "A1B2C3D4").id == target.id
