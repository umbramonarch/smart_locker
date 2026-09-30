"""
File: test_session.py
Description: Tests for GET/POST /api/session and LAN hitchhike of the
             process-global kiosk session.
Project: smart_locker/tests/api
Notes: Run with: python -m pytest tests/api/test_session.py -v
"""
import asyncio
import sys
import threading
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from smart_locker.api.app_context import PendingRegistration, PendingTagBind
from smart_locker.api.routes import router
from smart_locker.auth.session_manager import SessionManager
from smart_locker.database.models import DeviceStatus, UserRole
from smart_locker.database.repositories import DeviceRepository, RegistrantRepository, UserRepository
from smart_locker.security.hashing import compute_uid_hmac

import smart_locker.api.app_context as ctx_module

from tests.api.helpers import dashboard_admin_headers

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


class TestCommitBeforeResponse:
    """Read-your-writes: a mutation must commit before its response leaves.

    Regression for the yield-dependency teardown ordering on fastapi>=0.106:
    ``get_db``'s post-yield ``session.commit()`` ran AFTER the response was
    sent, so a back-to-back request could read the pre-commit snapshot —
    verified as a return (200) followed by a deactivation refused 409
    "still holds 1 borrowed device". ``CommitBeforeResponseRoute`` commits
    the request session (stashed on ``request.state`` by ``get_db``) before
    the Response is handed back.
    """

    def test_mutation_session_commits_before_response_sent(
        self, _db_setup, mock_context, db_session, test_user, test_devices,
        monkeypatch,
    ):
        """No pending work may remain uncommitted once the response is sent.

        The ASGI wrapper flags when the response body goes out; the
        ``Session.commit`` spy flags any commit that still had an open
        transaction after that point. Under the old get_db-only commit this
        fails deterministically — the teardown commit is exactly such a
        late commit — no timing luck required.
        """
        response_sent = threading.Event()
        late_commits: list[str] = []

        class _SentBoundary:
            """ASGI wrapper that flags the moment the response body is sent."""

            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                async def wrapped_send(message):
                    if (
                        scope["type"] == "http"
                        and message["type"] == "http.response.body"
                        and not message.get("more_body")
                    ):
                        response_sent.set()
                    await send(message)

                await self.app(scope, receive, wrapped_send)

        original_commit = Session.commit

        def commit_spy(session):
            if session.in_transaction() and response_sent.is_set():
                late_commits.append(sys._getframe(1).f_code.co_name)
            return original_commit(session)

        monkeypatch.setattr(Session, "commit", commit_spy)

        app = FastAPI()
        app.include_router(router)
        sending_client = TestClient(_SentBoundary(app), client=("127.0.0.1", 50000))

        mock_context.session_mgr.start_session(test_user)
        resp = sending_client.post(f"/api/devices/{test_devices[0].id}/borrow")
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert late_commits == []

    def test_return_then_deactivate_succeeds(
        self, client, lan_client, mock_context, test_user, test_devices,
        dashboard_secret,
    ):
        """The verified race verbatim: return, then immediately deactivate.

        The return's commit must be visible to the very next request — the
        dashboard People edit must not still count the unit as borrowed.
        """
        mock_context.session_mgr.start_session(test_user)
        device_id = test_devices[0].id

        resp = client.post(f"/api/devices/{device_id}/borrow")
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        resp = client.post(f"/api/devices/{device_id}/return")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

        resp = lan_client.patch(
            f"/api/dashboard/users/{test_user.id}",
            json={"is_active": False},
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        assert resp.json()["is_active"] is False

