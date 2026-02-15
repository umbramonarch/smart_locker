"""Tests for the services layer."""

import pytest

from smart_locker.auth.session_manager import SessionManager
from smart_locker.database.models import DeviceStatus, User, UserRole
from smart_locker.database.repositories import DeviceRepository, UserRepository
from smart_locker.security.encryption import encrypt, decrypt
from smart_locker.security.hashing import compute_uid_hmac
from smart_locker.services.locker_service import LockerService
from smart_locker.services.user_service import UserService


class TestLockerService:
    def _setup(self, db_session, enc_key, hmac_key):
        uid = "A1B2C3D4"
        user = UserRepository.create(
            db_session,
            display_name="Alice",
            uid_hmac=compute_uid_hmac(uid, hmac_key),
            encrypted_card_uid=encrypt(uid, enc_key),
        )
        device = DeviceRepository.create(
            db_session, name="Multimeter", device_type="measurement", serial_number="SN001"
        )
        db_session.flush()

        mgr = SessionManager(timeout_seconds=60)
        session = mgr.start_session(user)
        return user, device, session

    def test_borrow_device(self, db_session, enc_key, hmac_key):
        user, device, session = self._setup(db_session, enc_key, hmac_key)
        result = LockerService.borrow_device(db_session, session, device.id)
        assert result is True
        assert device.status == DeviceStatus.BORROWED
        assert device.current_borrower_id == user.id

    def test_borrow_unavailable_device(self, db_session, enc_key, hmac_key):
        user, device, session = self._setup(db_session, enc_key, hmac_key)
        device.status = DeviceStatus.MAINTENANCE
        db_session.flush()
        result = LockerService.borrow_device(db_session, session, device.id)
        assert result is False

    def test_return_device(self, db_session, enc_key, hmac_key):
        user, device, session = self._setup(db_session, enc_key, hmac_key)
        LockerService.borrow_device(db_session, session, device.id)
        result = LockerService.return_device(db_session, session, device.id)
        assert result is True
        assert device.status == DeviceStatus.AVAILABLE

    def test_return_not_borrowed_fails(self, db_session, enc_key, hmac_key):
        _, device, session = self._setup(db_session, enc_key, hmac_key)
        result = LockerService.return_device(db_session, session, device.id)
        assert result is False

    def test_return_wrong_user_fails(self, db_session, enc_key, hmac_key):
        user1, device, session1 = self._setup(db_session, enc_key, hmac_key)
        LockerService.borrow_device(db_session, session1, device.id)

        # Create second user
        user2 = UserRepository.create(
            db_session,
            display_name="Bob",
            uid_hmac=compute_uid_hmac("BBBBBBBB", hmac_key),
            encrypted_card_uid=encrypt("BBBBBBBB", enc_key),
        )
        db_session.flush()
        mgr = SessionManager(timeout_seconds=60)
        session2 = mgr.start_session(user2)

        result = LockerService.return_device(db_session, session2, device.id)
        assert result is False


class TestUserService:
    def test_enroll_user(self, db_session, enc_key, hmac_key):
        svc = UserService(enc_key=enc_key, hmac_key=hmac_key)
        user = svc.enroll_user(db_session, "Alice", "A1B2C3D4")
        db_session.flush()

        assert user.display_name == "Alice"
        assert user.uid_hmac == compute_uid_hmac("A1B2C3D4", hmac_key)
        # Encrypted UID should be decryptable
        decrypted = decrypt(user.encrypted_card_uid, enc_key)
        assert decrypted == "A1B2C3D4"

    def test_get_public_user_info(self, db_session, enc_key, hmac_key):
        svc = UserService(enc_key=enc_key, hmac_key=hmac_key)
        user = svc.enroll_user(db_session, "Bob", "DEADBEEF")
        db_session.flush()

        info = svc.get_public_user_info(db_session, user.id)
        assert info is not None
        assert info.display_name == "Bob"
        assert not hasattr(info, "card_uid") or "card_uid" not in info.__dict__

    def test_get_admin_user_info_as_admin(self, db_session, enc_key, hmac_key):
        svc = UserService(enc_key=enc_key, hmac_key=hmac_key)
        admin = svc.enroll_user(db_session, "Admin", "AAAA1111", role="admin")
        target = svc.enroll_user(db_session, "User", "BBBB2222")
        db_session.flush()

        info = svc.get_admin_user_info(db_session, target.id, requesting_user=admin)
        assert info is not None
        assert info.card_uid == "BBBB2222"

    def test_get_admin_user_info_denied_for_non_admin(self, db_session, enc_key, hmac_key):
        svc = UserService(enc_key=enc_key, hmac_key=hmac_key)
        regular = svc.enroll_user(db_session, "Regular", "CCCC3333")
        target = svc.enroll_user(db_session, "Target", "DDDD4444")
        db_session.flush()

        info = svc.get_admin_user_info(db_session, target.id, requesting_user=regular)
        assert info is None
