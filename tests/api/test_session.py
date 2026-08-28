"""
File: test_session.py
Description: Tests for GET/POST /api/session and LAN hitchhike of the
             process-global kiosk session.
Project: smart_locker/tests/api
Notes: Run with: python -m pytest tests/api/test_session.py -v
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

class TestSessionEndpoints:
    """Tests for session management API — get status, end session, touch."""

    def test_get_session_no_active(self, client):
        """Verify GET /api/session returns active=False when no session exists."""
        resp = client.get("/api/session")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active"] is False
        assert data["user"] is None
        assert data["overlay"] is False

    def test_get_session_active(self, client, mock_context, test_user):
        """Verify GET /api/session returns user data when a session is active."""
        mock_context.session_mgr.start_session(test_user)
        resp = client.get("/api/session")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active"] is True
        assert data["user"]["id"] == test_user.id
        assert data["user"]["name"] == "Test User"
        assert data["user"]["role"] == "user"
        assert data["overlay"] is False

    def test_lan_cannot_get_session(self, lan_client, mock_context, test_user):
        """LAN must not hitchhike the live kiosk session identity."""
        mock_context.session_mgr.start_session(test_user)
        resp = lan_client.get("/api/session")
        assert resp.status_code == 403
        assert mock_context.session_mgr.has_active_session

    def test_end_session(self, client, mock_context, test_user):
        """Verify POST /api/session/end terminates the active session."""
        mock_context.session_mgr.start_session(test_user)
        mock_context.pending_tag_bind = PendingTagBind(device_id=1)
        mock_context.admin_overlay_open = True
        resp = client.post("/api/session/end")
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert not mock_context.session_mgr.has_active_session
        assert mock_context.pending_tag_bind is None
        assert mock_context.admin_overlay_open is False

    def test_end_session_no_active(self, client):
        """Verify POST /api/session/end returns 401 when no session exists."""
        resp = client.post("/api/session/end")
        assert resp.status_code == 401

    def test_lan_cannot_end_kiosk_session(self, lan_client, mock_context, test_user):
        """A LAN client must not ride the process-global kiosk session."""
        mock_context.session_mgr.start_session(test_user)
        resp = lan_client.post("/api/session/end")
        assert resp.status_code == 403
        assert mock_context.session_mgr.has_active_session

    def test_lan_cannot_bind_tag_with_kiosk_session(
        self, lan_client, mock_context, admin_user, test_devices
    ):
        """Kiosk bind-tag is session+admin+loopback, not LAN."""
        mock_context.session_mgr.start_session(admin_user)
        mock_context.pending_tag_bind = None
        resp = lan_client.post(
            f"/api/admin/devices/{test_devices[0].id}/bind-tag"
        )
        assert resp.status_code == 403
        assert mock_context.pending_tag_bind is None

    def test_lan_cannot_unbind_tag_with_kiosk_session(
        self, lan_client, mock_context, admin_user, test_devices, db_session, hmac_key
    ):
        """Kiosk unbind-tag is session+admin+loopback, not LAN."""
        DeviceRepository.bind_tag(
            db_session,
            test_devices[0],
            compute_uid_hmac("AABBCCDD", hmac_key),
        )
        db_session.commit()
        mock_context.session_mgr.start_session(admin_user)
        resp = lan_client.post(
            f"/api/admin/devices/{test_devices[0].id}/unbind-tag"
        )
        assert resp.status_code == 403
        db_session.expire_all()
        assert test_devices[0].tag_hmac is not None

    def test_lan_cannot_borrow_with_kiosk_session(
        self, lan_client, mock_context, test_user, test_devices
    ):
        """Borrow is a require_session mutation; LAN must not ride NFC login."""
        mock_context.session_mgr.start_session(test_user)
        resp = lan_client.post(f"/api/devices/{test_devices[0].id}/borrow")
        assert resp.status_code == 403
        assert test_devices[0].status == DeviceStatus.AVAILABLE

    def test_dashboard_catalog_gets_stay_public_from_lan(
        self, lan_client, mock_context, test_user, test_devices
    ):
        """Public Inventory/Locker GET stays open for LAN even with a kiosk session."""
        mock_context.session_mgr.start_session(test_user)
        locker = lan_client.get("/api/dashboard/devices")
        assert locker.status_code == 200
        assert any(d["pm_number"] == "PM-001" for d in locker.json())

    def test_touch_session(self, client, mock_context, test_user):
        """Verify POST /api/session/touch succeeds when a session is active."""
        mock_context.session_mgr.start_session(test_user)
        resp = client.post("/api/session/touch")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_touch_session_no_active(self, client):
        """Verify POST /api/session/touch returns 401 when no session exists."""
        resp = client.post("/api/session/touch")
        assert resp.status_code == 401

