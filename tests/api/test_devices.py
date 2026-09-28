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

    def test_list_devices(self, client, mock_context, test_user, test_devices):
        """Verify GET /api/devices returns all devices with correct fields and shape."""
        mock_context.session_mgr.start_session(test_user)
        resp = client.get("/api/devices")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3
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

    def test_list_devices_excludes_untagged(
        self, client, mock_context, test_user, test_devices, db_session
    ):
        """A cabinet unit with no sticker is absent from the kiosk list."""
        DeviceRepository.create(
            db_session,
            name="Fresh Unit",
            device_type="Tool",
            pm_number="PM-NEW",
            locker_slot=9,
        )
        db_session.commit()
        mock_context.session_mgr.start_session(test_user)
        resp = client.get("/api/devices")
        assert resp.status_code == 200
        names = {d["name"] for d in resp.json()}
        assert "Fresh Unit" not in names
        assert "Camera" in names

    def test_list_devices_borrower_name_you(
        self, client, mock_context, test_user, test_devices, db_session
    ):
        """Devices borrowed by current user should have borrower_name='You'."""
        mock_context.session_mgr.start_session(test_user)
        user_session = mock_context.session_mgr.current_session
        # Borrow the camera
        assert LockerService.borrow_device(db_session, user_session, test_devices[0].id)
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
        assert LockerService.borrow_device(db_session, admin_session, test_devices[0].id)
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

    def test_http_borrow_returns_before_mirror_io(
        self, client, mock_context, test_user, test_devices, monkeypatch
    ):
        """POST /api/devices/{id}/borrow returns before mirror workbook I/O."""
        import time

        from smart_locker.sync import mirror

        def slow_tick(engine, trigger="interval"):
            time.sleep(0.4)
            return {}

        monkeypatch.setattr(mirror, "tick", slow_tick)
        mock_context.session_mgr.start_session(test_user)
        t0 = time.monotonic()
        resp = client.post(f"/api/devices/{test_devices[0].id}/borrow")
        elapsed = time.monotonic() - t0
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert elapsed < 0.25
        mirror.flush_scheduled()

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
        assert LockerService.borrow_device(db_session, user_session, test_devices[0].id)
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
        assert LockerService.borrow_device(db_session, user_session, test_devices[0].id)
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
        assert LockerService.borrow_device(db_session, admin_session, test_devices[0].id)
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
        assert LockerService.borrow_device(db_session, user_session, test_devices[0].id)
        db_session.commit()

        # Admin returns
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(f"/api/devices/{test_devices[0].id}/return")
        data = resp.json()
        assert data["success"] is True

