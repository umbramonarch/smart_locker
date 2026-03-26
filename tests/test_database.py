"""Tests for the database module: models, repositories."""

from datetime import date

import pytest

from smart_locker.database.models import DeviceStatus, TransactionType, UserRole
from smart_locker.database.repositories import (
    DeviceRepository,
    TransactionRepository,
    UserRepository,
)
from smart_locker.security.encryption import encrypt
from smart_locker.security.hashing import compute_uid_hmac


class TestUserRepository:
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
