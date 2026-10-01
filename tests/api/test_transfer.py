"""
File: test_transfer.py
Description: Tests for HTTP device transfer, per-user borrow-limit
             enforcement, and missing-device handling on the device routes.
Project: smart_locker/tests/api
Notes: Run with: python -m pytest tests/api/test_transfer.py -v
       The HTTP layer only reports success/message; the borrow-limit detail
       lives in LockerService log warnings, so tests assert the generic
       "Could not ..." message the kiosk actually returns.
"""
import pytest
from sqlalchemy import select

from config.settings import MAX_BORROWS
from smart_locker.database.models import (
    DeviceStatus,
    TransactionLog,
    TransactionType,
)
from smart_locker.database.repositories import DeviceRepository, UserRepository
from smart_locker.services.locker_service import LockerService


@pytest.fixture()
def other_user(db_session):
    """Create a second non-admin user used as the transfer receiver."""
    return UserRepository.create(
        db_session,
        display_name="Second User",
        uid_hmac="def456",
        encrypted_card_uid="encrypted_second",
        role="user",
    )


def _create_devices(db_session, count, slot_start=100):
    """Persist ``count`` extra AVAILABLE devices with unique keys/slots."""
    devices = []
    for i in range(count):
        devices.append(
            DeviceRepository.create(
                db_session,
                name=f"Spare-{slot_start + i}",
                device_type="Tool",
                pm_number=f"PM-T{slot_start + i:03d}",
                serial_number=f"SN-T{slot_start + i:03d}",
                locker_slot=slot_start + i,
            )
        )
    return devices


def _borrow_all(db_session, user, devices):
    """Mark every device borrowed by ``user`` directly (no log rows)."""
    for device in devices:
        DeviceRepository.borrow(db_session, device, user.id)
    db_session.commit()


def _device_logs(db_session, device_id):
    """Return TransactionLog rows for one device, oldest first."""
    return list(
        db_session.execute(
            select(TransactionLog)
            .where(TransactionLog.device_id == device_id)
            .order_by(TransactionLog.id)
        ).scalars().all()
    )


class TestTransfer:
    """Tests for POST /api/devices/{id}/transfer — happy path and guard rails."""

    def test_transfer_success(
        self, client, mock_context, test_user, other_user, test_devices, db_session
    ):
        """Transfer re-assigns a borrowed device and logs both sides."""
        camera = test_devices[0]
        # user1 holds the camera.
        user1_session = mock_context.session_mgr.start_session(test_user)
        assert LockerService.borrow_device(db_session, user1_session, camera.id)
        db_session.commit()

        # user2 taps in and takes over responsibility.
        mock_context.session_mgr.start_session(other_user)
        resp = client.post(f"/api/devices/{camera.id}/transfer")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["message"] == f"{camera.name} transferred to you."

        db_session.expire_all()
        device = DeviceRepository.find_by_id(db_session, camera.id)
        assert device.status == DeviceStatus.BORROWED
        assert device.current_borrower_id == other_user.id

        # Seed borrow + transfer return (out) + transfer borrow (in).
        logs = _device_logs(db_session, camera.id)
        assert len(logs) == 3
        out_log, in_log = logs[-2], logs[-1]
        assert out_log.transaction_type == TransactionType.RETURN
        assert out_log.user_id == test_user.id
        assert "transferred to" in out_log.notes
        assert other_user.display_name in out_log.notes
        assert in_log.transaction_type == TransactionType.BORROW
        assert in_log.user_id == other_user.id
        assert "transferred from" in in_log.notes
        assert test_user.display_name in in_log.notes

    def test_transfer_not_borrowed(
        self, client, mock_context, test_user, test_devices
    ):
        """Cannot transfer a device that is not currently borrowed."""
        mock_context.session_mgr.start_session(test_user)
        resp = client.post(f"/api/devices/{test_devices[0].id}/transfer")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "Could not transfer" in data["message"]

    def test_transfer_self(
        self, client, mock_context, test_user, test_devices, db_session
    ):
        """A user cannot transfer a device they already hold."""
        user_session = mock_context.session_mgr.start_session(test_user)
        assert LockerService.borrow_device(db_session, user_session, test_devices[0].id)
        db_session.commit()

        resp = client.post(f"/api/devices/{test_devices[0].id}/transfer")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "Could not transfer" in data["message"]

        db_session.expire_all()
        device = DeviceRepository.find_by_id(db_session, test_devices[0].id)
        assert device.status == DeviceStatus.BORROWED
        assert device.current_borrower_id == test_user.id

    def test_transfer_receiver_at_borrow_limit(
        self, client, mock_context, test_user, admin_user, test_devices, db_session
    ):
        """Transfer fails when the receiver already holds MAX_BORROWS devices."""
        camera = test_devices[0]
        _borrow_all(db_session, test_user, _create_devices(db_session, MAX_BORROWS))
        assert (
            DeviceRepository.count_borrowed_by_user(db_session, test_user.id)
            == MAX_BORROWS
        )

        # Admin holds the camera; test_user is already at the limit.
        admin_session = mock_context.session_mgr.start_session(admin_user)
        assert LockerService.borrow_device(db_session, admin_session, camera.id)
        db_session.commit()

        mock_context.session_mgr.start_session(test_user)
        resp = client.post(f"/api/devices/{camera.id}/transfer")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "Could not transfer" in data["message"]

        db_session.expire_all()
        device = DeviceRepository.find_by_id(db_session, camera.id)
        assert device.status == DeviceStatus.BORROWED
        assert device.current_borrower_id == admin_user.id

    def test_transfer_no_session(self, client, test_devices):
        """Verify transfer returns 401 when no session exists."""
        resp = client.post(f"/api/devices/{test_devices[0].id}/transfer")
        assert resp.status_code == 401

    def test_transfer_lan_forbidden(
        self, lan_client, mock_context, test_user, test_devices
    ):
        """A LAN browser cannot transfer even while a kiosk session is active."""
        mock_context.session_mgr.start_session(test_user)
        resp = lan_client.post(f"/api/devices/{test_devices[0].id}/transfer")
        assert resp.status_code == 403


class TestMissingDevice:
    """Borrow/return/transfer against a device id that does not exist."""

    def test_borrow_missing_device(self, client, mock_context, test_user):
        """Borrow on an unknown id returns success False, not a 404."""
        mock_context.session_mgr.start_session(test_user)
        resp = client.post("/api/devices/9999/borrow")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["message"] == "Could not borrow Device 9999."

    def test_return_missing_device(self, client, mock_context, test_user):
        """Return on an unknown id returns success False, not a 404."""
        mock_context.session_mgr.start_session(test_user)
        resp = client.post("/api/devices/9999/return")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["message"] == "Could not return Device 9999."

    def test_transfer_missing_device(self, client, mock_context, test_user):
        """Transfer on an unknown id returns success False, not a 404."""
        mock_context.session_mgr.start_session(test_user)
        resp = client.post("/api/devices/9999/transfer")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["message"] == "Could not transfer Device 9999."


class TestBorrowLimit:
    """MAX_BORROWS enforcement through POST /api/devices/{id}/borrow."""

    def test_borrow_at_limit_fails(
        self, client, mock_context, test_user, test_devices, db_session
    ):
        """The next borrow after MAX_BORROWS active loans is refused."""
        _borrow_all(db_session, test_user, _create_devices(db_session, MAX_BORROWS))
        assert (
            DeviceRepository.count_borrowed_by_user(db_session, test_user.id)
            == MAX_BORROWS
        )

        mock_context.session_mgr.start_session(test_user)
        resp = client.post(f"/api/devices/{test_devices[0].id}/borrow")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["message"] == (
            f"Could not borrow {test_devices[0].name}: "
            f"borrow limit reached ({MAX_BORROWS}/{MAX_BORROWS})."
        )

        db_session.expire_all()
        device = DeviceRepository.find_by_id(db_session, test_devices[0].id)
        assert device.status == DeviceStatus.AVAILABLE
        assert device.current_borrower_id is None
