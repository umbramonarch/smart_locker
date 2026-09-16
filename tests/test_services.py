"""
File: test_services.py
Description: Tests for the services layer — LockerService borrow/return logic
             and UserService enrollment. Validates borrow limits, availability
             checks, admin return-on-behalf, unattended idle return, and user
             enrollment with encryption.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_services.py -v
"""

import pytest

from smart_locker.auth.session_manager import SessionManager
from smart_locker.database.models import DeviceStatus, TransactionType, User, UserRole
from smart_locker.database.repositories import DeviceRepository, TransactionRepository, UserRepository
from smart_locker.security.encryption import encrypt, decrypt
from smart_locker.security.hashing import compute_uid_hmac
from smart_locker.services.locker_service import LockerService
from smart_locker.services.user_service import UserService


class TestLockerService:
    """Tests for LockerService borrow/return logic — limits, availability, ownership, admin override."""
    def _setup(self, db_session, enc_key, hmac_key):
        """Create a user, device, and active session for locker service tests."""
        uid = "A1B2C3D4"
        user = UserRepository.create(
            db_session,
            display_name="Alice",
            uid_hmac=compute_uid_hmac(uid, hmac_key),
            encrypted_card_uid=encrypt(uid, enc_key),
        )
        device = DeviceRepository.create(
            db_session, name="Multimeter", device_type="measurement",
            pm_number="PM-001", serial_number="SN001",
        )
        db_session.flush()

        mgr = SessionManager(timeout_seconds=60)
        session = mgr.start_session(user)
        return user, device, session

    def test_borrow_device(self, db_session, enc_key, hmac_key):
        """Verify borrowing sets device status to BORROWED and assigns the borrower."""
        user, device, session = self._setup(db_session, enc_key, hmac_key)
        result = LockerService.borrow_device(db_session, session, device.id)
        assert result is True
        assert device.status == DeviceStatus.BORROWED
        assert device.current_borrower_id == user.id

    def test_borrow_unavailable_device(self, db_session, enc_key, hmac_key):
        """Verify borrowing a MAINTENANCE device is rejected."""
        user, device, session = self._setup(db_session, enc_key, hmac_key)
        device.status = DeviceStatus.MAINTENANCE
        db_session.flush()
        result = LockerService.borrow_device(db_session, session, device.id)
        assert result is False

    def test_return_device(self, db_session, enc_key, hmac_key):
        """Verify returning a borrowed device resets it to AVAILABLE."""
        user, device, session = self._setup(db_session, enc_key, hmac_key)
        LockerService.borrow_device(db_session, session, device.id)
        result = LockerService.return_device(db_session, session, device.id)
        assert result is True
        assert device.status == DeviceStatus.AVAILABLE

    def test_return_not_borrowed_fails(self, db_session, enc_key, hmac_key):
        """Verify returning a device that is not currently borrowed is rejected."""
        _, device, session = self._setup(db_session, enc_key, hmac_key)
        result = LockerService.return_device(db_session, session, device.id)
        assert result is False

    def test_borrow_limit_enforced(self, db_session, enc_key, hmac_key, monkeypatch):
        """Verify a user cannot borrow more devices than the configured limit."""
        monkeypatch.setenv("SMART_LOCKER_MAX_BORROWS", "2")
        import config.settings as settings
        monkeypatch.setattr(settings, "MAX_BORROWS", 2)
        import smart_locker.services.locker_service as svc_module
        monkeypatch.setattr(svc_module, "MAX_BORROWS", 2)

        user, device1, session = self._setup(db_session, enc_key, hmac_key)
        device2 = DeviceRepository.create(db_session, name="Scope", device_type="measurement", pm_number="PM-002", serial_number="SN002")
        device3 = DeviceRepository.create(db_session, name="PSU", device_type="power", pm_number="PM-003", serial_number="SN003")
        db_session.flush()

        assert LockerService.borrow_device(db_session, session, device1.id) is True
        assert LockerService.borrow_device(db_session, session, device2.id) is True
        # Third borrow should be rejected — limit is 2
        assert LockerService.borrow_device(db_session, session, device3.id) is False
        assert device3.status == DeviceStatus.AVAILABLE

    def test_borrow_limit_does_not_affect_other_users(self, db_session, enc_key, hmac_key, monkeypatch):
        """Verify one user's borrow count does not block another user from borrowing."""
        monkeypatch.setenv("SMART_LOCKER_MAX_BORROWS", "1")
        import smart_locker.services.locker_service as svc_module
        monkeypatch.setattr(svc_module, "MAX_BORROWS", 1)

        user1, device1, session1 = self._setup(db_session, enc_key, hmac_key)
        device2 = DeviceRepository.create(db_session, name="Scope", device_type="measurement", pm_number="PM-002", serial_number="SN002")
        db_session.flush()

        assert LockerService.borrow_device(db_session, session1, device1.id) is True

        user2 = UserRepository.create(
            db_session,
            display_name="Bob",
            uid_hmac=compute_uid_hmac("BBBBBBBB", hmac_key),
            encrypted_card_uid=encrypt("BBBBBBBB", enc_key),
        )
        db_session.flush()
        from smart_locker.auth.session_manager import SessionManager
        session2 = SessionManager(timeout_seconds=60).start_session(user2)

        # Bob has 0 borrows — should succeed even though Alice is at her limit
        assert LockerService.borrow_device(db_session, session2, device2.id) is True

    def test_return_wrong_user_fails(self, db_session, enc_key, hmac_key):
        """Verify a non-admin user cannot return a device borrowed by someone else."""
        user1, device, session1 = self._setup(db_session, enc_key, hmac_key)
        LockerService.borrow_device(db_session, session1, device.id)

        user2 = UserRepository.create(
            db_session,
            display_name="Bob",
            uid_hmac=compute_uid_hmac("BBBBBBBB", hmac_key),
            encrypted_card_uid=encrypt("BBBBBBBB", enc_key),
        )
        db_session.flush()
        session2 = SessionManager(timeout_seconds=60).start_session(user2)

        result = LockerService.return_device(db_session, session2, device.id)
        assert result is False

    def test_borrow_refuses_deactivated_user(self, db_session, enc_key, hmac_key):
        """Borrow fails when the session user was deactivated, even if the
        session still holds a stale active copy from login time."""
        user, device, session = self._setup(db_session, enc_key, hmac_key)
        # Simulate login-time staleness: the session keeps a detached copy
        # while the database row is deactivated underneath it.
        db_session.expunge(user)
        assert session.user.is_active is True
        UserRepository.deactivate(
            db_session, UserRepository.find_by_id(db_session, user.id)
        )
        db_session.commit()

        assert LockerService.borrow_device(db_session, session, device.id) is False
        assert device.status == DeviceStatus.AVAILABLE
        assert UserRepository.borrowed_count(db_session, user.id) == 0

    def test_transfer_to_deactivated_user_refused(self, db_session, enc_key, hmac_key):
        """Transfer fails when the new holder is deactivated; the device stays put."""
        holder, device, holder_session = self._setup(db_session, enc_key, hmac_key)
        assert LockerService.borrow_device(db_session, holder_session, device.id) is True
        bob = UserRepository.create(
            db_session,
            display_name="Bob",
            uid_hmac=compute_uid_hmac("BBBBBBBB", hmac_key),
            encrypted_card_uid=encrypt("BBBBBBBB", enc_key),
        )
        db_session.flush()
        bob_session = SessionManager(timeout_seconds=60).start_session(bob)
        UserRepository.deactivate(db_session, bob)
        db_session.commit()

        assert LockerService.transfer_device(db_session, bob_session, device.id) is False
        assert device.status == DeviceStatus.BORROWED
        assert device.current_borrower_id == holder.id

    def test_transfer_from_deactivated_holder_succeeds(self, db_session, enc_key, hmac_key):
        """A deactivated holder's device can still be handed to an active user."""
        holder, device, holder_session = self._setup(db_session, enc_key, hmac_key)
        assert LockerService.borrow_device(db_session, holder_session, device.id) is True
        UserRepository.deactivate(db_session, holder)
        bob = UserRepository.create(
            db_session,
            display_name="Bob",
            uid_hmac=compute_uid_hmac("BBBBBBBB", hmac_key),
            encrypted_card_uid=encrypt("BBBBBBBB", enc_key),
        )
        db_session.flush()
        bob_session = SessionManager(timeout_seconds=60).start_session(bob)

        assert LockerService.transfer_device(db_session, bob_session, device.id) is True
        assert device.current_borrower_id == bob.id

    def test_return_by_deactivated_holder_succeeds(self, db_session, enc_key, hmac_key):
        """Returns stay open for deactivated holders — devices must come back."""
        user, device, session = self._setup(db_session, enc_key, hmac_key)
        assert LockerService.borrow_device(db_session, session, device.id) is True
        UserRepository.deactivate(db_session, user)

        assert LockerService.return_device(db_session, session, device.id) is True
        assert device.status == DeviceStatus.AVAILABLE

    def test_borrow_and_transfer_hold_user_admin_lock(
        self, db_session, enc_key, hmac_key, monkeypatch
    ):
        """Borrow and transfer guard-plus-commit run under the shared lock."""
        import smart_locker.services.locker_service as svc_module

        entered = []

        class FakeLock:
            def __enter__(self):
                entered.append(True)
                return self

            def __exit__(self, *args):
                return False

        monkeypatch.setattr(svc_module, "user_admin_lock", FakeLock())
        holder, device, holder_session = self._setup(db_session, enc_key, hmac_key)
        assert LockerService.borrow_device(db_session, holder_session, device.id) is True
        bob = UserRepository.create(
            db_session,
            display_name="Bob",
            uid_hmac=compute_uid_hmac("BBBBBBBB", hmac_key),
            encrypted_card_uid=encrypt("BBBBBBBB", enc_key),
        )
        db_session.flush()
        bob_session = SessionManager(timeout_seconds=60).start_session(bob)

        assert LockerService.transfer_device(db_session, bob_session, device.id) is True
        assert entered == [True, True]

    def test_admin_can_return_on_behalf_of_user(self, db_session, enc_key, hmac_key):
        """Verify an admin can return a device borrowed by another user."""
        user, device, user_session = self._setup(db_session, enc_key, hmac_key)
        LockerService.borrow_device(db_session, user_session, device.id)

        admin = UserRepository.create(
            db_session,
            display_name="Admin",
            uid_hmac=compute_uid_hmac("ADMINUID", hmac_key),
            encrypted_card_uid=encrypt("ADMINUID", enc_key),
            role="admin",
        )
        db_session.flush()
        admin_session = SessionManager(timeout_seconds=60).start_session(admin)

        result = LockerService.return_device(db_session, admin_session, device.id)
        assert result is True
        assert device.status == DeviceStatus.AVAILABLE
        assert device.current_borrower_id is None

    def test_return_unattended(self, db_session, enc_key, hmac_key):
        """Idle return logs the original borrower and the unattended note."""
        user, device, session = self._setup(db_session, enc_key, hmac_key)
        LockerService.borrow_device(db_session, session, device.id)

        result = LockerService.return_unattended(db_session, device.id)
        assert result is True
        assert device.status == DeviceStatus.AVAILABLE
        assert device.current_borrower_id is None

        history = TransactionRepository.get_device_history(db_session, device.id)
        return_txn = next(
            t for t in history if t.transaction_type == TransactionType.RETURN
        )
        assert return_txn.user_id == user.id
        assert return_txn.performed_by_id is None
        assert return_txn.notes == "returned at kiosk without card"

    def test_return_unattended_not_borrowed_fails(self, db_session, enc_key, hmac_key):
        """Unattended return is rejected when the device is not borrowed."""
        _, device, _ = self._setup(db_session, enc_key, hmac_key)
        result = LockerService.return_unattended(db_session, device.id)
        assert result is False
        assert device.status == DeviceStatus.AVAILABLE

    def test_admin_return_logs_both_borrower_and_admin(self, db_session, enc_key, hmac_key):
        """Verify admin return logs the original borrower and the acting admin."""
        user, device, user_session = self._setup(db_session, enc_key, hmac_key)
        LockerService.borrow_device(db_session, user_session, device.id)

        admin = UserRepository.create(
            db_session,
            display_name="Admin",
            uid_hmac=compute_uid_hmac("ADMINUID", hmac_key),
            encrypted_card_uid=encrypt("ADMINUID", enc_key),
            role="admin",
        )
        db_session.flush()
        admin_session = SessionManager(timeout_seconds=60).start_session(admin)

        LockerService.return_device(db_session, admin_session, device.id)

        history = TransactionRepository.get_device_history(db_session, device.id)
        return_txn = next(t for t in history if t.transaction_type == TransactionType.RETURN)
        assert return_txn.user_id == user.id           # original borrower
        assert return_txn.performed_by_id == admin.id  # admin who acted

    def test_transfer_device(self, db_session, enc_key, hmac_key):
        """Verify transfer moves a borrowed device to another user and logs both sides."""
        user1, device, session1 = self._setup(db_session, enc_key, hmac_key)
        LockerService.borrow_device(db_session, session1, device.id)

        user2 = UserRepository.create(
            db_session,
            display_name="Bob",
            uid_hmac=compute_uid_hmac("BBBBBBBB", hmac_key),
            encrypted_card_uid=encrypt("BBBBBBBB", enc_key),
        )
        db_session.flush()
        session2 = SessionManager(timeout_seconds=60).start_session(user2)

        result = LockerService.transfer_device(db_session, session2, device.id)
        assert result is True
        assert device.status == DeviceStatus.BORROWED
        assert device.current_borrower_id == user2.id

        history = TransactionRepository.get_device_history(db_session, device.id)
        return_txn = next(t for t in history if t.transaction_type == TransactionType.RETURN)
        borrow_txn = next(t for t in history if t.transaction_type == TransactionType.BORROW)
        assert return_txn.user_id == user1.id
        assert borrow_txn.user_id == user2.id
        assert "transferred" in (return_txn.notes or "").lower()
        assert "transferred" in (borrow_txn.notes or "").lower()

    def test_transfer_device_fails_at_borrow_limit(self, db_session, enc_key, hmac_key, monkeypatch):
        """Verify transfer is rejected when the receiving user is already at the limit."""
        import smart_locker.services.locker_service as svc_module
        monkeypatch.setattr(svc_module, "MAX_BORROWS", 1)

        user1, device, session1 = self._setup(db_session, enc_key, hmac_key)
        user2 = UserRepository.create(
            db_session,
            display_name="Bob",
            uid_hmac=compute_uid_hmac("BBBBBBBB", hmac_key),
            encrypted_card_uid=encrypt("BBBBBBBB", enc_key),
        )
        other = DeviceRepository.create(db_session, name="Scope", device_type="measurement", pm_number="PM-002", serial_number="SN002")
        db_session.flush()

        session2 = SessionManager(timeout_seconds=60).start_session(user2)
        # Bob already has one borrowed device
        DeviceRepository.borrow(db_session, other, user2.id)
        TransactionRepository.log_borrow(db_session, user2.id, other.id)

        # Alice borrows the device to be transferred
        LockerService.borrow_device(db_session, session1, device.id)

        result = LockerService.transfer_device(db_session, session2, device.id)
        assert result is False
        assert device.current_borrower_id == user1.id


class TestUserService:
    """Tests for UserService enrollment, public info, and admin-only UID decryption."""
    def test_enroll_user(self, db_session, enc_key, hmac_key):
        """Verify enrollment stores HMAC and encrypted UID that round-trips correctly."""
        svc = UserService(enc_key=enc_key, hmac_key=hmac_key)
        user = svc.enroll_user(db_session, "Alice", "A1B2C3D4")
        db_session.flush()

        assert user.display_name == "Alice"
        assert user.uid_hmac == compute_uid_hmac("A1B2C3D4", hmac_key)
        # Encrypted UID should be decryptable
        decrypted = decrypt(user.encrypted_card_uid, enc_key)
        assert decrypted == "A1B2C3D4"

    def test_get_public_user_info(self, db_session, enc_key, hmac_key):
        """Verify public user info returns name and role but no card data."""
        svc = UserService(enc_key=enc_key, hmac_key=hmac_key)
        user = svc.enroll_user(db_session, "Bob", "DEADBEEF")
        db_session.flush()

        info = svc.get_public_user_info(db_session, user.id)
        assert info is not None
        assert info.display_name == "Bob"
        assert not hasattr(info, "card_uid") or "card_uid" not in info.__dict__

    def test_get_admin_user_info_as_admin(self, db_session, enc_key, hmac_key):
        """Verify admin can decrypt and view another user's card UID."""
        svc = UserService(enc_key=enc_key, hmac_key=hmac_key)
        admin = svc.enroll_user(db_session, "Admin", "AAAA1111", role="admin")
        target = svc.enroll_user(db_session, "User", "BBBB2222")
        db_session.flush()

        info = svc.get_admin_user_info(db_session, target.id, requesting_user=admin)
        assert info is not None
        assert info.card_uid == "BBBB2222"

    def test_enroll_rejects_device_tag_uid(self, db_session, enc_key, hmac_key):
        """Enrollment fails when the UID HMAC is already bound to a device."""
        device = DeviceRepository.create(
            db_session, name="Fluke 87V", device_type="Multimeter",
            pm_number="PM-COLLIDE",
        )
        uid = "AABBCCDD"
        DeviceRepository.bind_tag(
            db_session, device, compute_uid_hmac(uid, hmac_key)
        )
        svc = UserService(enc_key=enc_key, hmac_key=hmac_key)
        with pytest.raises(ValueError, match="already bound"):
            svc.enroll_user(db_session, "Alice", uid)

    def test_get_admin_user_info_denied_for_non_admin(self, db_session, enc_key, hmac_key):
        """Verify non-admin users are denied access to admin user info."""
        svc = UserService(enc_key=enc_key, hmac_key=hmac_key)
        regular = svc.enroll_user(db_session, "Regular", "CCCC3333")
        target = svc.enroll_user(db_session, "Target", "DDDD4444")
        db_session.flush()

        info = svc.get_admin_user_info(db_session, target.id, requesting_user=regular)
        assert info is None


class TestReplaceCard:
    """Tests for UserService.replace_card — lost-card replacement."""

    def _svc_and_user(self, db_session, enc_key, hmac_key):
        """Build a UserService and enroll Alice on card A1B2C3D4."""
        svc = UserService(enc_key=enc_key, hmac_key=hmac_key)
        user = svc.enroll_user(db_session, "Alice", "A1B2C3D4")
        db_session.flush()
        return svc, user

    def test_replace_card_success(self, db_session, enc_key, hmac_key):
        """Replacing moves the user to the new UID; name/role unchanged."""
        from smart_locker.auth.authenticator import Authenticator

        svc, user = self._svc_and_user(db_session, enc_key, hmac_key)
        old_hmac = user.uid_hmac
        old_role = user.role
        svc.replace_card(db_session, user, "e5e5e5e5")  # lowercase canonicalises
        db_session.flush()
        assert user.uid_hmac != old_hmac
        assert decrypt(user.encrypted_card_uid, enc_key) == "E5E5E5E5"
        assert user.display_name == "Alice"
        assert user.role == old_role
        assert user.role == UserRole.USER
        # New card logs in; old card does not.
        auth = Authenticator(hmac_key)
        assert auth.authenticate(db_session, "A1B2C3D4") is None
        assert auth.authenticate(db_session, "E5E5E5E5").id == user.id

    def test_replace_card_same_card_is_noop(self, db_session, enc_key, hmac_key):
        """Tapping the user's current card is a harmless success."""
        svc, user = self._svc_and_user(db_session, enc_key, hmac_key)
        old_hmac = user.uid_hmac
        result = svc.replace_card(db_session, user, "A1B2C3D4")
        assert result is user
        assert user.uid_hmac == old_hmac

    def test_replace_card_other_users_card_refused(
        self, db_session, enc_key, hmac_key
    ):
        """A card already belonging to another user is refused."""
        svc, user = self._svc_and_user(db_session, enc_key, hmac_key)
        svc.enroll_user(db_session, "Bob", "BBBB2222")
        db_session.flush()
        with pytest.raises(ValueError, match="already registered"):
            svc.replace_card(db_session, user, "BBBB2222")

    def test_replace_card_device_tag_refused(self, db_session, enc_key, hmac_key):
        """A UID bound to a device sticker is refused."""
        device = DeviceRepository.create(
            db_session, name="Fluke 87V", device_type="Multimeter",
            pm_number="PM-COLLIDE",
        )
        uid = "AABBCCDD"
        DeviceRepository.bind_tag(db_session, device, compute_uid_hmac(uid, hmac_key))
        svc, user = self._svc_and_user(db_session, enc_key, hmac_key)
        with pytest.raises(ValueError, match="already bound"):
            svc.replace_card(db_session, user, uid)
