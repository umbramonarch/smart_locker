"""
File: test_people_windows.py
Description: Card-window lifecycle tests — POST /api/admin/session refuses to
             start while a registration/bind window owns the reader, the
             dashboard-secret GET /api/dashboard/card-window status poll and
             POST /api/dashboard/card-window/cancel, and session end leaving
             a dashboard-armed window in place.
Project: smart_locker/tests/api
Notes: Run with: python -m pytest tests/api/test_people_windows.py -v
       The card tap that resolves an armed window lives in
       tests/e2e/test_people.py — the FakeNFCReader drives it through the
       real bridge.
"""

import time

from smart_locker.api.app_context import PendingRegistration, PendingTagBind

from tests.api.helpers import dashboard_admin_headers


class TestAdminSessionWindowConflict:
    """POST /api/admin/session must not open a session on top of a live
    card/bind window — its end would kill the window and the awaited tap
    would be swallowed as a login."""

    def test_admin_session_409_with_dashboard_card_window(
        self, client, mock_context, admin_user
    ):
        mock_context.pending_registration = PendingRegistration(
            display_name="Remote Hire", from_dashboard=True
        )
        resp = client.post("/api/admin/session")
        assert resp.status_code == 409
        assert "card tap" in resp.json()["detail"].lower()
        assert not mock_context.session_mgr.has_active_session
        assert mock_context.pending_registration is not None

    def test_admin_session_409_with_pending_bind(
        self, client, mock_context, admin_user
    ):
        mock_context.pending_tag_bind = PendingTagBind(device_id=1)
        resp = client.post("/api/admin/session")
        assert resp.status_code == 409
        assert "sticker tap" in resp.json()["detail"].lower()
        assert not mock_context.session_mgr.has_active_session
        assert mock_context.pending_tag_bind is not None

    def test_admin_session_ok_with_no_window(
        self, client, mock_context, admin_user
    ):
        """No armed window -> the 5-tap admin session starts as before."""
        resp = client.post("/api/admin/session")
        assert resp.status_code == 200
        assert mock_context.session_mgr.has_active_session

    def test_existing_session_overlay_branch_ignores_window(
        self, client, mock_context, admin_user
    ):
        """The overlay-flag update path still works while a kiosk-armed
        window is pending — it does not touch the reader."""
        mock_context.session_mgr.start_session(admin_user)
        mock_context.admin_overlay_open = True
        mock_context.pending_registration = PendingRegistration(
            display_name="Kiosk Hire"
        )
        resp = client.post("/api/admin/session?overlay=false")
        assert resp.status_code == 200
        assert mock_context.admin_overlay_open is False
        assert mock_context.pending_registration is not None


class TestDashboardCardWindowCancel:
    """POST /api/dashboard/card-window/cancel drops only from_dashboard windows."""

    def test_cancel_requires_secret(self, client, lan_client, mock_context):
        """No secret configured -> 401 on loopback and LAN alike."""
        assert client.post("/api/dashboard/card-window/cancel").status_code == 401
        assert lan_client.post(
            "/api/dashboard/card-window/cancel"
        ).status_code == 401

    def test_cancel_wrong_secret_401(
        self, client, lan_client, mock_context, dashboard_secret
    ):
        url = "/api/dashboard/card-window/cancel"
        bad = dashboard_admin_headers("not-the-secret")
        assert client.post(url, headers=bad).status_code == 401
        assert lan_client.post(url, headers=bad).status_code == 401

    def test_cancel_dashboard_card_window(
        self, client, mock_context, dashboard_secret
    ):
        """A dashboard-armed card window is dropped and latched cancelled."""
        mock_context.pending_registration = PendingRegistration(
            display_name="Remote Hire", from_dashboard=True
        )
        resp = client.post(
            "/api/dashboard/card-window/cancel",
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "cancelled": True}
        assert mock_context.pending_registration is None
        result = mock_context.last_card_result
        assert result["outcome"] == "cancelled"
        assert result["reason"] is None
        assert result["user"] is None
        assert result["display_name"] == "Remote Hire"
        assert result["replace_user_id"] is None

    def test_cancel_dashboard_card_window_from_lan(
        self, lan_client, mock_context, dashboard_secret
    ):
        """The dashboard runs on the LAN — the same cancel works there."""
        mock_context.pending_registration = PendingRegistration(
            display_name="Remote Hire", from_dashboard=True
        )
        resp = lan_client.post(
            "/api/dashboard/card-window/cancel",
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        assert resp.json()["cancelled"] is True
        assert mock_context.pending_registration is None

    def test_cancel_keeps_kiosk_armed_window(
        self, client, mock_context, dashboard_secret
    ):
        """A kiosk-armed (not from_dashboard) window is not the dashboard's."""
        mock_context.pending_registration = PendingRegistration(
            display_name="Kiosk Hire"
        )
        resp = client.post(
            "/api/dashboard/card-window/cancel",
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "cancelled": False}
        assert mock_context.pending_registration is not None
        assert mock_context.pending_registration.display_name == "Kiosk Hire"
        assert mock_context.last_card_result is None

    def test_cancel_dashboard_bind(
        self, client, mock_context, dashboard_secret
    ):
        """A dashboard-armed sticker bind is cancelled too — with no card
        latch, since a bind is not a card window."""
        mock_context.pending_tag_bind = PendingTagBind(
            device_id=7, from_dashboard=True
        )
        resp = client.post(
            "/api/dashboard/card-window/cancel",
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        assert resp.json()["cancelled"] is True
        assert mock_context.pending_tag_bind is None
        assert mock_context.last_card_result is None

    def test_cancel_nothing_pending(
        self, client, mock_context, dashboard_secret
    ):
        resp = client.post(
            "/api/dashboard/card-window/cancel",
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "cancelled": False}


class TestDashboardCardWindowStatus:
    """GET /api/dashboard/card-window reports armed / resolved / idle."""

    def test_status_requires_secret(self, client, lan_client, mock_context):
        """No secret configured -> 401 on loopback and LAN alike."""
        assert client.get("/api/dashboard/card-window").status_code == 401
        assert lan_client.get("/api/dashboard/card-window").status_code == 401

    def test_status_wrong_secret_401(
        self, client, lan_client, mock_context, dashboard_secret
    ):
        url = "/api/dashboard/card-window"
        bad = dashboard_admin_headers("not-the-secret")
        assert client.get(url, headers=bad).status_code == 401
        assert lan_client.get(url, headers=bad).status_code == 401

    def test_status_idle(self, client, mock_context, dashboard_secret):
        resp = client.get(
            "/api/dashboard/card-window",
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        assert resp.json() == {"state": "idle"}

    def test_status_armed(self, client, mock_context, dashboard_secret):
        mock_context.pending_registration = PendingRegistration(
            display_name="Remote Hire", from_dashboard=True
        )
        resp = client.get(
            "/api/dashboard/card-window",
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "armed"
        assert body["display_name"] == "Remote Hire"
        assert body["replace"] is False
        assert 0 < body["seconds_left"] <= 60

    def test_status_armed_replace(self, client, mock_context, dashboard_secret):
        """A replace-card window reports replace=True."""
        mock_context.pending_registration = PendingRegistration(
            display_name="Test User",
            replace_user_id=3,
            from_dashboard=True,
        )
        resp = client.get(
            "/api/dashboard/card-window",
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "armed"
        assert body["replace"] is True

    def test_status_resolved(self, client, mock_context, dashboard_secret):
        """The latch written at tap resolution surfaces as 'resolved'."""
        mock_context.last_card_result = {
            "outcome": "success",
            "reason": None,
            "user": {"id": 1, "name": "Remote Hire", "role": "user"},
            "display_name": "Remote Hire",
            "replace_user_id": None,
            "at": time.monotonic(),
        }
        resp = client.get(
            "/api/dashboard/card-window",
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "resolved"
        assert body["outcome"] == "success"
        assert body["reason"] is None
        assert body["user"]["name"] == "Remote Hire"
        assert body["display_name"] == "Remote Hire"
        assert body["replace"] is False

    def test_status_expired_window_drops_and_latches(
        self, client, mock_context, dashboard_secret
    ):
        """A window past its 60s is dropped by the poll and reported expired."""
        mock_context.pending_registration = PendingRegistration(
            display_name="Remote Hire",
            created_at=time.monotonic() - 120,
            from_dashboard=True,
        )
        resp = client.get(
            "/api/dashboard/card-window",
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "resolved"
        assert body["outcome"] == "expired"
        assert body["display_name"] == "Remote Hire"
        assert body["replace"] is False
        assert mock_context.pending_registration is None
        assert mock_context.last_card_result["outcome"] == "expired"


class TestSessionEndKeepsDashboardWindow:
    """Ending a kiosk session must not kill a dashboard-armed card window."""

    def test_session_end_preserves_dashboard_card_window(
        self, client, mock_context, admin_user
    ):
        mock_context.pending_registration = PendingRegistration(
            display_name="Remote Hire", from_dashboard=True
        )
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post("/api/session/end")
        assert resp.status_code == 200
        assert mock_context.pending_registration is not None
        assert mock_context.pending_registration.from_dashboard is True
        assert mock_context.pending_registration.display_name == "Remote Hire"

    def test_session_end_still_clears_kiosk_card_window(
        self, client, mock_context, admin_user
    ):
        """A kiosk-armed window remains session-scoped and is cleared."""
        mock_context.pending_registration = PendingRegistration(
            display_name="Kiosk Hire"
        )
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post("/api/session/end")
        assert resp.status_code == 200
        assert mock_context.pending_registration is None
