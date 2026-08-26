"""
File: test_tap_router.py
Description: Tests for NFC UID classification and auto-intent borrow/return.
             Covers idle vs logged-in taps for work cards, device tags, and
             unknown UIDs. Idle borrowed tags return unattended. In-memory
             SQLite — no NFC hardware.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_tap_router.py -v
"""

import time

import pytest

from smart_locker.auth.session_manager import SessionManager
from smart_locker.auth.tap_router import (
    TapKind,
    bind_uid_to_device,
    classify_uid,
    handle_insert,
)
from smart_locker.database.models import DeviceStatus, TransactionType
from smart_locker.database.repositories import (
    DeviceRepository,
    TransactionRepository,
    UserRepository,
)
from smart_locker.security.encryption import encrypt
from smart_locker.security.hashing import compute_uid_hmac
from smart_locker.services.locker_service import LockerService


class TestClassifyUid:
    """HMAC lookup: users first, then devices, else unknown."""

    def test_work_card(self, db_session, enc_key, hmac_key):
        uid = "A1B2C3D4"
        UserRepository.create(
            db_session,
            display_name="Alice",
            uid_hmac=compute_uid_hmac(uid, hmac_key),
            encrypted_card_uid=encrypt(uid, enc_key),
        )
        kind, user, device = classify_uid(db_session, uid, hmac_key)
        assert kind is TapKind.WORK_CARD
        assert user is not None
        assert user.display_name == "Alice"
        assert device is None

    def test_device_tag(self, db_session, hmac_key):
        uid = "AABBCCDD"
        device = DeviceRepository.create(
            db_session, name="Fluke 87V", device_type="Multimeter",
            pm_number="PM-001",
        )
        DeviceRepository.bind_tag(
            db_session, device, compute_uid_hmac(uid, hmac_key)
        )
        kind, user, found = classify_uid(db_session, uid, hmac_key)
        assert kind is TapKind.DEVICE_TAG
        assert user is None
        assert found is not None
        assert found.id == device.id

    def test_unknown(self, db_session, hmac_key):
        kind, user, device = classify_uid(db_session, "FFFFFFFF", hmac_key)
        assert kind is TapKind.UNKNOWN
        assert user is None
        assert device is None


class TestIdleTaps:
    """No session: work card logs in; borrowed tag returns; available does not."""

    def _mgr(self):
        return SessionManager(timeout_seconds=60)

    def test_idle_work_card(self, db_session, enc_key, hmac_key):
        uid = "A1B2C3D4"
        UserRepository.create(
            db_session,
            display_name="Alice",
            uid_hmac=compute_uid_hmac(uid, hmac_key),
            encrypted_card_uid=encrypt(uid, enc_key),
        )
        mgr = self._mgr()
        result = handle_insert(db_session, uid, hmac_key, mgr)
        assert result.event == "auth_success"
        assert result.payload["user"]["name"] == "Alice"
        assert mgr.has_active_session

    def test_idle_available_tag_does_not_borrow(self, db_session, hmac_key):
        uid = "AABBCCDD"
        device = DeviceRepository.create(
            db_session, name="Fluke 87V", device_type="Multimeter",
            pm_number="PM-001", locker_slot=3,
        )
        DeviceRepository.bind_tag(
            db_session, device, compute_uid_hmac(uid, hmac_key)
        )
        mgr = self._mgr()
        result = handle_insert(db_session, uid, hmac_key, mgr)
        assert result.event == "device_tag_idle"
        assert result.payload["message"]
        assert "work card" in result.payload["message"].lower()
        assert uid not in result.payload.get("message", "")
        assert uid not in result.cli_message
        assert not mgr.has_active_session
        assert device.status == DeviceStatus.AVAILABLE
        assert device.current_borrower_id is None
        assert TransactionRepository.get_device_history(db_session, device.id) == []

    def test_idle_borrowed_tag_returns_unattended(
        self, db_session, enc_key, hmac_key
    ):
        """Idle tap of a borrowed sticker returns it; no work card, no session."""
        tag_uid = "AABBCCDD"
        user = UserRepository.create(
            db_session,
            display_name="Alice",
            uid_hmac=compute_uid_hmac("A1B2C3D4", hmac_key),
            encrypted_card_uid=encrypt("A1B2C3D4", enc_key),
        )
        device = DeviceRepository.create(
            db_session, name="Fluke 87V", device_type="Multimeter",
            pm_number="PM-001", locker_slot=7,
        )
        DeviceRepository.bind_tag(
            db_session, device, compute_uid_hmac(tag_uid, hmac_key)
        )
        mgr = self._mgr()
        session = mgr.start_session(user)
        assert LockerService.borrow_device(db_session, session, device.id) is True
        mgr.end_session()
        assert not mgr.has_active_session

        result = handle_insert(db_session, tag_uid, hmac_key, mgr)
        assert result.event == "device_action"
        assert result.payload["success"] is True
        assert result.payload["action"] == "return"
        assert result.payload["device_id"] == device.id
        assert result.payload["device_name"] == "Fluke 87V"
        assert result.payload["locker_slot"] == 7
        assert "AABBCCDD" not in result.payload["message"]
        assert "AABBCCDD" not in result.cli_message
        sse = result.to_sse()
        assert sse["event"] == "device_action"
        assert sse["locker_slot"] == 7
        assert device.status == DeviceStatus.AVAILABLE
        assert device.current_borrower_id is None
        assert not mgr.has_active_session
        history = TransactionRepository.get_device_history(db_session, device.id)
        ret = next(t for t in history if t.transaction_type == TransactionType.RETURN)
        assert ret.user_id == user.id
        assert ret.performed_by_id is None
        assert ret.notes == "returned at kiosk without card"

    def test_idle_unknown(self, db_session, hmac_key):
        mgr = self._mgr()
        result = handle_insert(db_session, "FFFFFFFF", hmac_key, mgr)
        assert result.event == "auth_failed"
        assert not mgr.has_active_session


class TestSessionDeviceTags:
    """Logged-in auto-intent: borrow, return, fail cases, session stays."""

    def _user(self, db_session, enc_key, hmac_key, name, uid, role="user"):
        return UserRepository.create(
            db_session,
            display_name=name,
            uid_hmac=compute_uid_hmac(uid, hmac_key),
            encrypted_card_uid=encrypt(uid, enc_key),
            role=role,
        )

    def _device_with_tag(self, db_session, hmac_key, name, pm, tag_uid, locker_slot=None):
        device = DeviceRepository.create(
            db_session, name=name, device_type="t", pm_number=pm,
            locker_slot=locker_slot,
        )
        DeviceRepository.bind_tag(
            db_session, device, compute_uid_hmac(tag_uid, hmac_key)
        )
        return device

    def test_available_tag_borrows_and_logs(self, db_session, enc_key, hmac_key):
        user = self._user(db_session, enc_key, hmac_key, "Alice", "A1B2C3D4")
        device = self._device_with_tag(
            db_session, hmac_key, "Fluke 87V", "PM-001", "AABBCCDD", locker_slot=4
        )
        mgr = SessionManager(timeout_seconds=60)
        mgr.start_session(user)
        result = handle_insert(db_session, "AABBCCDD", hmac_key, mgr)
        assert result.event == "device_action"
        assert result.payload["success"] is True
        assert result.payload["action"] == "borrow"
        assert result.payload["device_id"] == device.id
        assert result.payload["device_name"] == "Fluke 87V"
        assert result.payload["locker_slot"] == 4
        assert "borrowed" in result.payload["message"].lower()
        assert "AABBCCDD" not in result.payload["message"]
        assert "AABBCCDD" not in result.cli_message
        assert device.status == DeviceStatus.BORROWED
        assert device.current_borrower_id == user.id
        assert mgr.has_active_session
        history = TransactionRepository.get_device_history(db_session, device.id)
        assert len(history) == 1
        assert history[0].transaction_type == TransactionType.BORROW

    def test_own_borrowed_tag_returns(self, db_session, enc_key, hmac_key):
        user = self._user(db_session, enc_key, hmac_key, "Alice", "A1B2C3D4")
        device = self._device_with_tag(
            db_session, hmac_key, "Fluke 87V", "PM-001", "AABBCCDD", locker_slot=7
        )
        mgr = SessionManager(timeout_seconds=60)
        session = mgr.start_session(user)
        LockerService.borrow_device(db_session, session, device.id)
        result = handle_insert(db_session, "AABBCCDD", hmac_key, mgr)
        assert result.event == "device_action"
        assert result.payload["success"] is True
        assert result.payload["action"] == "return"
        assert result.payload["locker_slot"] == 7
        assert device.status == DeviceStatus.AVAILABLE
        assert mgr.has_active_session

    def test_someone_elses_tag_as_user_fails(self, db_session, enc_key, hmac_key):
        alice = self._user(db_session, enc_key, hmac_key, "Alice", "AAAA1111")
        bob = self._user(db_session, enc_key, hmac_key, "Bob", "BBBB2222")
        device = self._device_with_tag(
            db_session, hmac_key, "Fluke 87V", "PM-001", "AABBCCDD"
        )
        alice_session = SessionManager(timeout_seconds=60).start_session(alice)
        LockerService.borrow_device(db_session, alice_session, device.id)
        mgr = SessionManager(timeout_seconds=60)
        mgr.start_session(bob)
        result = handle_insert(db_session, "AABBCCDD", hmac_key, mgr)
        assert result.event == "device_action"
        assert result.payload["success"] is False
        assert device.status == DeviceStatus.BORROWED
        assert device.current_borrower_id == alice.id
        assert mgr.has_active_session
        assert mgr.current_session.user.id == bob.id

    def test_someone_elses_tag_as_admin_returns_on_behalf(
        self, db_session, enc_key, hmac_key
    ):
        alice = self._user(db_session, enc_key, hmac_key, "Alice", "AAAA1111")
        admin = self._user(
            db_session, enc_key, hmac_key, "Admin", "ADMINUID", role="admin"
        )
        device = self._device_with_tag(
            db_session, hmac_key, "Fluke 87V", "PM-001", "AABBCCDD"
        )
        alice_session = SessionManager(timeout_seconds=60).start_session(alice)
        LockerService.borrow_device(db_session, alice_session, device.id)
        mgr = SessionManager(timeout_seconds=60)
        mgr.start_session(admin)
        result = handle_insert(db_session, "AABBCCDD", hmac_key, mgr)
        assert result.event == "device_action"
        assert result.payload["success"] is True
        assert result.payload["action"] == "return"
        assert device.status == DeviceStatus.AVAILABLE
        assert mgr.has_active_session
        history = TransactionRepository.get_device_history(db_session, device.id)
        ret = next(t for t in history if t.transaction_type == TransactionType.RETURN)
        assert ret.user_id == alice.id
        assert ret.performed_by_id == admin.id

    def test_session_work_card_logs_out_no_new_session(
        self, db_session, enc_key, hmac_key
    ):
        alice = self._user(db_session, enc_key, hmac_key, "Alice", "AAAA1111")
        self._user(db_session, enc_key, hmac_key, "Bob", "BBBB2222")
        mgr = SessionManager(timeout_seconds=60)
        mgr.start_session(alice)
        result = handle_insert(db_session, "BBBB2222", hmac_key, mgr)
        assert result.event == "session_ended"
        assert not mgr.has_active_session

    def test_session_unknown_stays_logged_in(self, db_session, enc_key, hmac_key):
        alice = self._user(db_session, enc_key, hmac_key, "Alice", "AAAA1111")
        mgr = SessionManager(timeout_seconds=60)
        mgr.start_session(alice)
        result = handle_insert(db_session, "FFFFFFFF", hmac_key, mgr)
        assert result.event == "unknown_tag"
        assert result.payload["message"]
        assert "FFFFFFFF" not in result.payload["message"]
        assert "FFFFFFFF" not in result.cli_message
        assert mgr.has_active_session
        assert mgr.current_session.user.id == alice.id

    def test_maintenance_fails_session_remains(self, db_session, enc_key, hmac_key):
        alice = self._user(db_session, enc_key, hmac_key, "Alice", "AAAA1111")
        device = self._device_with_tag(
            db_session, hmac_key, "Fluke 87V", "PM-001", "AABBCCDD"
        )
        device.status = DeviceStatus.MAINTENANCE
        db_session.flush()
        mgr = SessionManager(timeout_seconds=60)
        mgr.start_session(alice)
        result = handle_insert(db_session, "AABBCCDD", hmac_key, mgr)
        assert result.event == "device_action"
        assert result.payload["success"] is False
        assert device.status == DeviceStatus.MAINTENANCE
        assert mgr.has_active_session

    def test_borrow_limit_fails_session_remains(
        self, db_session, enc_key, hmac_key, monkeypatch
    ):
        import smart_locker.services.locker_service as svc_module

        monkeypatch.setattr(svc_module, "MAX_BORROWS", 1)
        alice = self._user(db_session, enc_key, hmac_key, "Alice", "AAAA1111")
        first = DeviceRepository.create(
            db_session, name="Held", device_type="t", pm_number="PM-HELD",
        )
        tagged = self._device_with_tag(
            db_session, hmac_key, "Fluke 87V", "PM-001", "AABBCCDD"
        )
        mgr = SessionManager(timeout_seconds=60)
        session = mgr.start_session(alice)
        assert LockerService.borrow_device(db_session, session, first.id) is True
        result = handle_insert(db_session, "AABBCCDD", hmac_key, mgr)
        assert result.event == "device_action"
        assert result.payload["success"] is False
        assert tagged.status == DeviceStatus.AVAILABLE
        assert mgr.has_active_session

    def test_overlay_open_does_not_borrow(self, db_session, enc_key, hmac_key):
        """Admin overlay: bound tag does not borrow; session stays."""
        alice = self._user(db_session, enc_key, hmac_key, "Alice", "AAAA1111")
        device = self._device_with_tag(
            db_session, hmac_key, "Fluke 87V", "PM-001", "AABBCCDD"
        )
        mgr = SessionManager(timeout_seconds=60)
        session = mgr.start_session(alice)
        session.last_activity = time.monotonic() - 30
        before = session.last_activity
        result = handle_insert(
            db_session, "AABBCCDD", hmac_key, mgr, admin_overlay_open=True
        )
        assert result.event == "device_tag_idle"
        assert result.to_sse()["event"] == "device_tag_idle"
        message = result.payload["message"].lower()
        assert "admin" in message
        assert "work card" not in message
        assert device.status == DeviceStatus.AVAILABLE
        assert mgr.has_active_session
        assert session.last_activity > before

    def test_failed_and_unknown_taps_touch_session(
        self, db_session, enc_key, hmac_key
    ):
        """Failed tag taps and unknown UIDs reset last_activity."""
        alice = self._user(db_session, enc_key, hmac_key, "Alice", "AAAA1111")
        bob = self._user(db_session, enc_key, hmac_key, "Bob", "BBBB2222")
        held = self._device_with_tag(
            db_session, hmac_key, "Held", "PM-HELD", "CCDDEEFF"
        )
        maint = self._device_with_tag(
            db_session, hmac_key, "Scope", "PM-MAINT", "11223344"
        )
        maint.status = DeviceStatus.MAINTENANCE
        db_session.flush()
        mgr = SessionManager(timeout_seconds=60)
        alice_session = mgr.start_session(bob)
        LockerService.borrow_device(db_session, alice_session, held.id)
        mgr.end_session()
        session = mgr.start_session(alice)
        session.last_activity = time.monotonic() - 30
        before = session.last_activity
        result = handle_insert(db_session, "CCDDEEFF", hmac_key, mgr)
        assert result.payload["success"] is False
        assert session.last_activity > before
        before = session.last_activity
        handle_insert(db_session, "11223344", hmac_key, mgr)
        assert session.last_activity > before
        before = session.last_activity
        handle_insert(db_session, "FFFFFFFF", hmac_key, mgr)
        assert session.last_activity > before
        avail = self._device_with_tag(
            db_session, hmac_key, "Meter", "PM-OK", "99AABBCC"
        )
        before = session.last_activity
        result = handle_insert(db_session, "99AABBCC", hmac_key, mgr)
        assert result.payload["success"] is True
        assert session.last_activity > before
        assert avail.status == DeviceStatus.BORROWED


class TestHmacCollision:
    """Cross-table uniqueness: enroll vs bind reject each other's HMAC."""

    def test_bind_rejects_work_card(self, db_session, enc_key, hmac_key):
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
        with pytest.raises(ValueError, match="work card"):
            bind_uid_to_device(db_session, device, uid, hmac_key)
        assert device.tag_hmac is None

    def test_bind_rejects_other_device_tag(self, db_session, hmac_key):
        uid = "AABBCCDD"
        a = DeviceRepository.create(
            db_session, name="A", device_type="t", pm_number="PM-A",
        )
        b = DeviceRepository.create(
            db_session, name="B", device_type="t", pm_number="PM-B",
        )
        bind_uid_to_device(db_session, a, uid, hmac_key)
        with pytest.raises(ValueError, match="already bound"):
            bind_uid_to_device(db_session, b, uid, hmac_key)
        assert b.tag_hmac is None
        assert a.tag_hmac == compute_uid_hmac(uid, hmac_key)
