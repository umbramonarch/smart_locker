"""
File: test_devices.py
Description: Tests for kiosk device listing and HTTP borrow/return.
Project: smart_locker/tests/api
Notes: Run with: python -m pytest tests/api/test_devices.py -v
"""
import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from smart_locker.api.app_context import PendingRegistration, PendingTagBind
from smart_locker.api.routes import router
from smart_locker.auth.session_manager import SessionManager
from smart_locker.database.models import DeviceStatus, UserRole
from smart_locker.database.repositories import DeviceRepository, RegistrantRepository, UserRepository
from smart_locker.security.hashing import compute_uid_hmac

import smart_locker.api.app_context as ctx_module
from smart_locker.services.locker_service import LockerService

class TestDeviceEndpoints:
    """Tests for device listing API — auth required, borrower name resolution."""

    def test_list_devices_no_session(self, client):
        """Verify GET /api/devices returns 401 when no session exists."""
        resp = client.get("/api/devices")
        assert resp.status_code == 401

    def test_list_devices(
        self, client, mock_context, test_user, test_devices, db_session
    ):
        """Verify GET /api/devices returns tagged devices with correct fields and shape."""
        DeviceRepository.create(
            db_session,
            name="Ghost",
            device_type="general",
            pm_number="PM-999",
            locker_slot=9,
        )
        db_session.commit()
        mock_context.session_mgr.start_session(test_user)
        resp = client.get("/api/devices")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3
        assert "Ghost" not in {d["name"] for d in data}
        # Check device shape
        cam = next(d for d in data if d["name"] == "Camera")
        assert cam["status"] == "available"
        assert cam["borrower_name"] is None
        assert cam["serial_number"] == "SN-001"
        assert cam["pm_number"] == "PM-001"
        assert cam["locker_slot"] == 1
        # New fields present (None for test fixtures without values)
        assert "manufacturer" in cam
        assert "model" in cam
        assert "barcode" not in cam
        assert "calibration_due" in cam
        assert cam["has_tag"] is True
        assert "tag_hmac" not in cam

    def test_list_devices_hides_untagged(
        self, client, mock_context, test_user, test_devices, db_session
    ):
        """A device without a bound sticker is not listed on the kiosk."""
        DeviceRepository.create(
            db_session,
            name="Ghost",
            device_type="general",
            pm_number="PM-999",
            locker_slot=9,
        )
        db_session.commit()
        mock_context.session_mgr.start_session(test_user)
        resp = client.get("/api/devices")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3
        assert "Ghost" not in {d["name"] for d in data}

    def test_list_devices_shows_own_borrowed_untagged(
        self, client, mock_context, test_user, test_devices, db_session
    ):
        """A BORROWED untagged loan held by the caller stays on the kiosk list."""
        ghost = DeviceRepository.create(
            db_session,
            name="Ghost",
            device_type="general",
            pm_number="PM-999",
            locker_slot=9,
        )
        db_session.commit()
        user_session = mock_context.session_mgr.start_session(test_user)
        assert LockerService.borrow_device(db_session, user_session, ghost.id) is True
        db_session.commit()

        resp = client.get("/api/devices")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 4
        row = next(d for d in data if d["name"] == "Ghost")
        assert row["status"] == "borrowed"
        assert row["borrower_name"] == "You"
        assert row["has_tag"] is False

    def test_list_devices_hides_other_borrowed_untagged(
        self, client, mock_context, test_user, admin_user, test_devices, db_session
    ):
        """A BORROWED untagged loan held by someone else stays hidden."""
        ghost = DeviceRepository.create(
            db_session,
            name="Ghost",
            device_type="general",
            pm_number="PM-999",
            locker_slot=9,
        )
        db_session.commit()
        admin_session = mock_context.session_mgr.start_session(admin_user)
        assert LockerService.borrow_device(db_session, admin_session, ghost.id) is True
        db_session.commit()

        mock_context.session_mgr.start_session(test_user)
        resp = client.get("/api/devices")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3
        assert "Ghost" not in {d["name"] for d in data}

    def test_list_devices_hides_available_untagged(
        self, client, mock_context, test_user, test_devices, db_session
    ):
        """An AVAILABLE untagged row is admin-only even for a logged-in user."""
        DeviceRepository.create(
            db_session,
            name="Ghost",
            device_type="general",
            pm_number="PM-999",
            locker_slot=9,
        )
        db_session.commit()
        mock_context.session_mgr.start_session(test_user)
        resp = client.get("/api/devices")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3
        assert "Ghost" not in {d["name"] for d in data}

    def test_list_devices_borrower_name_you(
        self, client, mock_context, test_user, test_devices, db_session
    ):
        """Devices borrowed by current user should have borrower_name='You'."""
        mock_context.session_mgr.start_session(test_user)
        user_session = mock_context.session_mgr.current_session
        # Borrow the camera
        LockerService.borrow_device(db_session, user_session, test_devices[0].id)
        db_session.commit()

        resp = client.get("/api/devices")
        data = resp.json()
        cam = next(d for d in data if d["name"] == "Camera")
        assert cam["status"] == "borrowed"
        assert cam["borrower_name"] == "You"

    def test_list_devices_borrower_name_other(
        self, client, mock_context, test_user, admin_user, test_devices, db_session
    ):
        """Devices borrowed by another user show their display name."""
        # Admin borrows the camera
        admin_session = mock_context.session_mgr.start_session(admin_user)
        LockerService.borrow_device(db_session, admin_session, test_devices[0].id)
        db_session.commit()

        # Switch to test_user's session
        mock_context.session_mgr.start_session(test_user)
        resp = client.get("/api/devices")
        data = resp.json()
        cam = next(d for d in data if d["name"] == "Camera")
        assert cam["status"] == "borrowed"
        assert cam["borrower_name"] == "Admin User"


class TestBorrowReturn:
    """Tests for borrow/return API — success, auth, ownership, and admin override."""

    def test_borrow_success(self, client, mock_context, test_user, test_devices):
        """Verify POST /api/devices/{id}/borrow succeeds for an available device."""
        mock_context.session_mgr.start_session(test_user)
        resp = client.post(f"/api/devices/{test_devices[0].id}/borrow")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "Camera" in data["message"]

    def test_http_borrow_returns_before_excel_io(
        self, client, mock_context, test_user, test_devices, tmp_path, monkeypatch
    ):
        """POST /api/devices/{id}/borrow returns before Excel write-back I/O."""
        import time

        from openpyxl import Workbook

        from smart_locker.sync import location_writeback as wb
        from smart_locker.sync.location_writeback import (
            WritebackResult,
            flush_scheduled_writeback,
        )

        path = tmp_path / "device-list.xlsx"
        book = Workbook()
        book.active.append(["Equipment", "Location"])
        book.active.append(["PM-001", "Locker"])
        book.save(path)
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))

        def slow_write(engine, source_path):
            time.sleep(0.4)
            return WritebackResult()

        monkeypatch.setattr(wb, "write_location_with_engine", slow_write)
        mock_context.session_mgr.start_session(test_user)
        t0 = time.monotonic()
        resp = client.post(f"/api/devices/{test_devices[0].id}/borrow")
        elapsed = time.monotonic() - t0
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert elapsed < 0.25
        flush_scheduled_writeback()

    def test_borrow_untagged_device_409(
        self, client, mock_context, test_user, db_session
    ):
        """HTTP borrow of an untagged device is refused (defensive)."""
        ghost = DeviceRepository.create(
            db_session,
            name="Ghost",
            device_type="general",
            pm_number="PM-999",
            locker_slot=9,
        )
        db_session.commit()
        mock_context.session_mgr.start_session(test_user)
        resp = client.post(f"/api/devices/{ghost.id}/borrow")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "Tap the sticker to bind it first."

    def test_borrow_409_then_bind_then_borrow_ok(
        self, client, mock_context, test_user, db_session, hmac_key
    ):
        """Untagged borrow is 409; after the sticker bind the same borrow works."""
        ghost = DeviceRepository.create(
            db_session,
            name="Ghost",
            device_type="general",
            pm_number="PM-999",
            locker_slot=9,
        )
        db_session.commit()
        mock_context.session_mgr.start_session(test_user)

        refused = client.post(f"/api/devices/{ghost.id}/borrow")
        assert refused.status_code == 409

        DeviceRepository.bind_tag(
            db_session, ghost, compute_uid_hmac("GHOST-TAP", hmac_key)
        )
        db_session.commit()

        resp = client.post(f"/api/devices/{ghost.id}/borrow")
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        db_session.expire_all()
        assert ghost.status == DeviceStatus.BORROWED

    def test_borrow_no_session(self, client, test_devices):
        """Verify borrow returns 401 when no session exists."""
        resp = client.post(f"/api/devices/{test_devices[0].id}/borrow")
        assert resp.status_code == 401

    def test_borrow_maintenance_device(self, client, mock_context, test_user, test_devices):
        """Cannot borrow a device under maintenance."""
        mock_context.session_mgr.start_session(test_user)
        laptop = test_devices[2]  # MAINTENANCE status
        resp = client.post(f"/api/devices/{laptop.id}/borrow")
        data = resp.json()
        assert data["success"] is False

    def test_borrow_already_borrowed(
        self, client, mock_context, test_user, test_devices, db_session
    ):
        """Cannot borrow a device that's already borrowed."""
        mock_context.session_mgr.start_session(test_user)
        user_session = mock_context.session_mgr.current_session
        LockerService.borrow_device(db_session, user_session, test_devices[0].id)
        db_session.commit()

        resp = client.post(f"/api/devices/{test_devices[0].id}/borrow")
        data = resp.json()
        assert data["success"] is False

    def test_return_success(
        self, client, mock_context, test_user, test_devices, db_session
    ):
        """Verify POST /api/devices/{id}/return succeeds after borrowing."""
        mock_context.session_mgr.start_session(test_user)
        user_session = mock_context.session_mgr.current_session
        LockerService.borrow_device(db_session, user_session, test_devices[0].id)
        db_session.commit()

        resp = client.post(f"/api/devices/{test_devices[0].id}/return")
        data = resp.json()
        assert data["success"] is True
        assert "Camera" in data["message"]

    def test_return_no_session(self, client, test_devices):
        """Verify return returns 401 when no session exists."""
        resp = client.post(f"/api/devices/{test_devices[0].id}/return")
        assert resp.status_code == 401

    def test_return_not_borrowed(self, client, mock_context, test_user, test_devices):
        """Cannot return a device that's not borrowed."""
        mock_context.session_mgr.start_session(test_user)
        resp = client.post(f"/api/devices/{test_devices[0].id}/return")
        data = resp.json()
        assert data["success"] is False

    def test_return_wrong_user(
        self, client, mock_context, test_user, admin_user, test_devices, db_session
    ):
        """Non-admin cannot return another user's device."""
        # Admin borrows
        admin_session = mock_context.session_mgr.start_session(admin_user)
        LockerService.borrow_device(db_session, admin_session, test_devices[0].id)
        db_session.commit()

        # Test user tries to return
        mock_context.session_mgr.start_session(test_user)
        resp = client.post(f"/api/devices/{test_devices[0].id}/return")
        data = resp.json()
        assert data["success"] is False

    def test_admin_return_other_users_device(
        self, client, mock_context, test_user, admin_user, test_devices, db_session
    ):
        """Admin can return another user's device."""
        # Test user borrows
        user_session = mock_context.session_mgr.start_session(test_user)
        LockerService.borrow_device(db_session, user_session, test_devices[0].id)
        db_session.commit()

        # Admin returns
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(f"/api/devices/{test_devices[0].id}/return")
        data = resp.json()
        assert data["success"] is True

