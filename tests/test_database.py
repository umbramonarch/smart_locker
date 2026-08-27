"""
File: test_database.py
Description: Tests for the database module — ORM models and repository CRUD
             operations. Validates user creation, device creation with extended
             schema, borrowing/returning state transitions, and transaction logging.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_database.py -v
"""

from datetime import date

import pytest

from smart_locker.database.models import DeviceStatus, TransactionType, UserRole
from sqlalchemy.exc import IntegrityError

from smart_locker.database.repositories import (
    DeviceRepository,
    TransactionRepository,
    UserRepository,
)
from smart_locker.security.encryption import encrypt
from smart_locker.security.hashing import compute_uid_hmac


class TestUserRepository:
    """Tests for UserRepository CRUD — creation, HMAC lookup, and listing."""
    def test_create_and_find_by_id(self, db_session, enc_key, hmac_key):
        uid = "A1B2C3D4"
        user = UserRepository.create(
            db_session,
            display_name="Alice",
            uid_hmac=compute_uid_hmac(uid, hmac_key),
            encrypted_card_uid=encrypt(uid, enc_key),
        )
        db_session.flush()
        found = UserRepository.find_by_id(db_session, user.id)
        assert found is not None
        assert found.display_name == "Alice"

    def test_find_by_uid_hmac(self, db_session, enc_key, hmac_key):
        uid = "DEADBEEF"
        hmac_digest = compute_uid_hmac(uid, hmac_key)
        UserRepository.create(
            db_session,
            display_name="Bob",
            uid_hmac=hmac_digest,
            encrypted_card_uid=encrypt(uid, enc_key),
        )
        db_session.flush()

        found = UserRepository.find_by_uid_hmac(db_session, hmac_digest)
        assert found is not None
        assert found.display_name == "Bob"

    def test_find_by_uid_hmac_not_found(self, db_session):
        found = UserRepository.find_by_uid_hmac(db_session, "nonexistent")
        assert found is None

    def test_list_all(self, db_session, enc_key, hmac_key):
        for name, uid in [("Alice", "AAAA"), ("Bob", "BBBB")]:
            UserRepository.create(
                db_session,
                display_name=name,
                uid_hmac=compute_uid_hmac(uid, hmac_key),
                encrypted_card_uid=encrypt(uid, enc_key),
            )
        users = UserRepository.list_all(db_session)
        assert len(users) == 2


class TestDeviceRepository:
    """Tests for DeviceRepository CRUD — creation, borrow/return state, and extended fields."""
    def test_create_and_find(self, db_session):
        device = DeviceRepository.create(
            db_session,
            name="Multimeter",
            device_type="measurement",
            pm_number="PM-001",
            serial_number="SN001",
            locker_slot=1,
        )
        found = DeviceRepository.find_by_id(db_session, device.id)
        assert found is not None
        assert found.name == "Multimeter"
        assert found.pm_number == "PM-001"
        assert found.status == DeviceStatus.AVAILABLE

    def test_borrow_and_return(self, db_session, enc_key, hmac_key):
        user = UserRepository.create(
            db_session,
            display_name="Alice",
            uid_hmac=compute_uid_hmac("AAAA", hmac_key),
            encrypted_card_uid=encrypt("AAAA", enc_key),
        )
        device = DeviceRepository.create(
            db_session, name="Scope", device_type="measurement",
            pm_number="PM-002", serial_number="SN002",
        )
        db_session.flush()

        # Borrow
        DeviceRepository.borrow(db_session, device, user.id)
        assert device.status == DeviceStatus.BORROWED
        assert device.current_borrower_id == user.id

        # Check borrowed list
        borrowed = DeviceRepository.get_borrowed_by_user(db_session, user.id)
        assert len(borrowed) == 1

        # Return
        DeviceRepository.return_device(db_session, device)
        assert device.status == DeviceStatus.AVAILABLE
        assert device.current_borrower_id is None

    def test_get_available_devices(self, db_session):
        d1 = DeviceRepository.create(
            db_session, name="D1", device_type="t", pm_number="PM-D1", serial_number="S1",
        )
        d2 = DeviceRepository.create(
            db_session, name="D2", device_type="t", pm_number="PM-D2", serial_number="S2",
        )
        db_session.flush()

        available = DeviceRepository.get_available_devices(db_session)
        assert len(available) == 2

        d1.status = DeviceStatus.MAINTENANCE
        db_session.flush()
        available = DeviceRepository.get_available_devices(db_session)
        assert len(available) == 1

    def test_create_device_with_extended_fields(self, db_session):
        device = DeviceRepository.create(
            db_session,
            name="PM-042 Keysight DSOX3054T",
            device_type="Oscilloscope",
            pm_number="PM-042",
            serial_number="MY12345678",
            locker_slot=3,
            description="4-channel 500MHz oscilloscope",
            manufacturer="Keysight",
            model="DSOX3054T",
            barcode="4900123456789",
            calibration_due=date(2026, 9, 15),
        )
        found = DeviceRepository.find_by_id(db_session, device.id)
        assert found.pm_number == "PM-042"
        assert found.manufacturer == "Keysight"
        assert found.model == "DSOX3054T"
        assert found.barcode == "4900123456789"
        assert found.calibration_due == date(2026, 9, 15)
        assert found.serial_number == "MY12345678"

    def test_bind_lookup_unbind_tag(self, db_session, hmac_key):
        """Bind stores tag_hmac; lookup finds the row; unbind clears it."""
        device = DeviceRepository.create(
            db_session, name="Fluke 87V", device_type="Multimeter",
            pm_number="PM-TAG-1",
        )
        digest = compute_uid_hmac("AABBCCDD", hmac_key)
        DeviceRepository.bind_tag(db_session, device, digest)
        found = DeviceRepository.find_by_tag_hmac(db_session, digest)
        assert found is not None
        assert found.id == device.id
        assert found.tag_hmac == digest

        DeviceRepository.unbind_tag(db_session, device)
        assert device.tag_hmac is None
        assert DeviceRepository.find_by_tag_hmac(db_session, digest) is None

    def test_find_by_slot(self, db_session):
        """Slot lookup finds the device occupying that locker number."""
        DeviceRepository.create(
            db_session, name="Fluke 87V", device_type="t",
            pm_number="PM-SLOT", locker_slot=7,
        )
        found = DeviceRepository.find_by_slot(db_session, 7)
        assert found is not None
        assert found.pm_number == "PM-SLOT"
        assert DeviceRepository.find_by_slot(db_session, 8) is None

    def test_tag_hmac_null_uniqueness(self, db_session):
        """SQLite UNIQUE on tag_hmac allows many unbound (NULL) devices."""
        DeviceRepository.create(
            db_session, name="A", device_type="t", pm_number="PM-NULL-1",
        )
        DeviceRepository.create(
            db_session, name="B", device_type="t", pm_number="PM-NULL-2",
        )
        db_session.commit()
        assert DeviceRepository.find_by_pm(db_session, "PM-NULL-1").tag_hmac is None
        assert DeviceRepository.find_by_pm(db_session, "PM-NULL-2").tag_hmac is None

    def test_rebind_replaces_hmac(self, db_session, hmac_key):
        """Re-bind replaces the HMAC on that device; the old digest no longer matches."""
        device = DeviceRepository.create(
            db_session, name="Scope", device_type="t", pm_number="PM-REBIND",
        )
        first = compute_uid_hmac("11111111", hmac_key)
        second = compute_uid_hmac("22222222", hmac_key)
        DeviceRepository.bind_tag(db_session, device, first)
        DeviceRepository.bind_tag(db_session, device, second)
        assert DeviceRepository.find_by_tag_hmac(db_session, first) is None
        found = DeviceRepository.find_by_tag_hmac(db_session, second)
        assert found is not None
        assert found.id == device.id

    def test_tag_hmac_unique_constraint(self, db_session, hmac_key):
        """Two devices cannot share the same tag_hmac."""
        digest = compute_uid_hmac("DEADBEEF", hmac_key)
        d1 = DeviceRepository.create(
            db_session, name="A", device_type="t", pm_number="PM-U1",
        )
        d2 = DeviceRepository.create(
            db_session, name="B", device_type="t", pm_number="PM-U2",
        )
        DeviceRepository.bind_tag(db_session, d1, digest)
        db_session.flush()
        d2.tag_hmac = digest
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_update_metadata_ignores_tag_hmac(self, db_session, hmac_key):
        """Source-import ALLOWED set must not overwrite tag_hmac."""
        device = DeviceRepository.create(
            db_session, name="Old", device_type="t", pm_number="PM-META",
        )
        digest = compute_uid_hmac("CAFEBABE", hmac_key)
        DeviceRepository.bind_tag(db_session, device, digest)
        DeviceRepository.update_metadata(
            db_session, device, tag_hmac="should-not-apply", name="New",
        )
        assert device.tag_hmac == digest
        assert device.name == "New"

    def test_update_metadata_ignores_status_and_borrower(self, db_session):
        """Re-import must not overwrite locker status or current borrower."""
        user = UserRepository.create(
            db_session,
            display_name="Anna",
            uid_hmac="ab" * 16,
            encrypted_card_uid="enc_anna",
            role="user",
        )
        device = DeviceRepository.create(
            db_session, name="Old", device_type="t", pm_number="PM-LOC",
        )
        DeviceRepository.borrow(db_session, device, user.id)
        DeviceRepository.update_metadata(
            db_session,
            device,
            status=DeviceStatus.AVAILABLE,
            current_borrower_id=None,
            name="New",
        )
        assert device.status == DeviceStatus.BORROWED
        assert device.current_borrower_id == user.id
        assert device.name == "New"

    def test_create_device_without_serial(self, db_session):
        device = DeviceRepository.create(
            db_session,
            name="PM-099 Fluke",
            device_type="Multimeter",
            pm_number="PM-099",
        )
        found = DeviceRepository.find_by_id(db_session, device.id)
        assert found.pm_number == "PM-099"
        assert found.serial_number is None


class TestTransactionRepository:
    """Tests for TransactionRepository — borrow/return logging and history queries."""
    def test_log_borrow_and_return(self, db_session, enc_key, hmac_key):
        user = UserRepository.create(
            db_session,
            display_name="Alice",
            uid_hmac=compute_uid_hmac("AAAA", hmac_key),
            encrypted_card_uid=encrypt("AAAA", enc_key),
        )
        device = DeviceRepository.create(
            db_session, name="D1", device_type="t", pm_number="PM-T1", serial_number="S1",
        )
        db_session.flush()

        txn1 = TransactionRepository.log_borrow(db_session, user.id, device.id)
        assert txn1.transaction_type == TransactionType.BORROW

        txn2 = TransactionRepository.log_return(db_session, user.id, device.id)
        assert txn2.transaction_type == TransactionType.RETURN

        history = TransactionRepository.get_user_history(db_session, user.id)
        assert len(history) == 2

        device_history = TransactionRepository.get_device_history(db_session, device.id)
        assert len(device_history) == 2
