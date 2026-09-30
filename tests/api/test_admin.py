"""
File: test_admin.py
Description: Tests for kiosk admin overlay, device-tag bind/unbind, sync,
             update, Stop system, and Shut down.
Project: smart_locker/tests/api
Notes: Run with: python -m pytest tests/api/test_admin.py -v
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
from smart_locker.services.locker_service import LockerService

import smart_locker.api.app_context as ctx_module

class TestDeviceTagBindApi:
    """Auth gates for bind/unbind, has_tag on the manage list, duplicate names."""

    def test_bind_tag_requires_session(self, client, mock_context, test_devices):
        """POST /api/admin/devices/{id}/bind-tag returns 401 without a session."""
        resp = client.post(f"/api/admin/devices/{test_devices[0].id}/bind-tag")
        assert resp.status_code == 401

    def test_bind_tag_rejects_non_admin(
        self, client, mock_context, test_user, test_devices
    ):
        """POST /api/admin/devices/{id}/bind-tag returns 403 for a normal user."""
        mock_context.session_mgr.start_session(test_user)
        resp = client.post(f"/api/admin/devices/{test_devices[0].id}/bind-tag")
        assert resp.status_code == 403

    def test_bind_tag_accepts_admin(
        self, client, mock_context, admin_user, test_devices
    ):
        """Admin bind-tag sets pending_tag_bind for the chosen device."""
        mock_context.session_mgr.start_session(admin_user)
        mock_context.pending_registration = None
        mock_context.pending_tag_bind = None
        resp = client.post(f"/api/admin/devices/{test_devices[0].id}/bind-tag")
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert mock_context.pending_tag_bind.device_id == test_devices[0].id
        assert mock_context.pending_tag_bind.is_expired is False

    def test_bind_tag_rejects_pending_registration(
        self, client, mock_context, admin_user, test_devices
    ):
        """A new bind arm does not drop an in-progress kiosk enroll."""
        mock_context.session_mgr.start_session(admin_user)
        mock_context.pending_registration = PendingRegistration("Someone")
        mock_context.pending_tag_bind = None
        resp = client.post(f"/api/admin/devices/{test_devices[0].id}/bind-tag")
        assert resp.status_code == 409
        assert mock_context.pending_registration is not None
        assert mock_context.pending_registration.display_name == "Someone"
        assert mock_context.pending_tag_bind is None

    def test_bind_tag_unknown_device(self, client, mock_context, admin_user):
        """POST bind-tag returns 404 when the device id does not exist."""
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post("/api/admin/devices/99999/bind-tag")
        assert resp.status_code == 404

    def test_unbind_tag_requires_session(self, client, mock_context, test_devices):
        """POST /api/admin/devices/{id}/unbind-tag returns 401 without a session."""
        resp = client.post(f"/api/admin/devices/{test_devices[0].id}/unbind-tag")
        assert resp.status_code == 401

    def test_unbind_tag_rejects_non_admin(
        self, client, mock_context, test_user, test_devices
    ):
        """POST /api/admin/devices/{id}/unbind-tag returns 403 for a normal user."""
        mock_context.session_mgr.start_session(test_user)
        resp = client.post(f"/api/admin/devices/{test_devices[0].id}/unbind-tag")
        assert resp.status_code == 403

    def test_unbind_tag_accepts_admin(
        self, client, mock_context, admin_user, test_devices, db_session
    ):
        """Admin unbind-tag clears tag_hmac; GET list has has_tag False."""
        # test_devices rows are already sticker-bound.
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(f"/api/admin/devices/{test_devices[0].id}/unbind-tag")
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        db_session.expire_all()
        assert test_devices[0].tag_hmac is None
        # The untagged unit leaves the kiosk grids but stays on the
        # admin manage list — binding a new sticker is done from there.
        kiosk = client.get("/api/devices").json()
        assert "Camera" not in {d["name"] for d in kiosk}
        listed = client.get("/api/admin/devices").json()
        cam = next(d for d in listed if d["name"] == "Camera")
        assert cam["has_tag"] is False
        assert "tag_hmac" not in cam
        assert mock_context.pending_tag_bind is None

    def test_unbind_tag_clears_pending_bind(
        self, client, mock_context, admin_user, test_devices
    ):
        """Kiosk unbind cancels an armed bind window so the next tap is not rebound."""
        mock_context.session_mgr.start_session(admin_user)
        mock_context.pending_tag_bind = PendingTagBind(device_id=test_devices[0].id)
        resp = client.post(f"/api/admin/devices/{test_devices[0].id}/unbind-tag")
        assert resp.status_code == 200
        assert mock_context.pending_tag_bind is None

    def test_unbind_tag_borrowed_is_409(
        self, client, mock_context, admin_user, test_devices, db_session
    ):
        """Unbind is refused while the unit is borrowed — the loan owns the row."""
        session = mock_context.session_mgr.start_session(admin_user)
        assert LockerService.borrow_device(db_session, session, test_devices[0].id)
        db_session.commit()

        resp = client.post(f"/api/admin/devices/{test_devices[0].id}/unbind-tag")
        assert resp.status_code == 409
        db_session.expire_all()
        assert test_devices[0].tag_hmac is not None

    def test_unbind_tag_unknown_device(self, client, mock_context, admin_user):
        """POST unbind-tag returns 404 when the device id does not exist."""
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post("/api/admin/devices/99999/unbind-tag")
        assert resp.status_code == 404


class TestAdminOverlaySession:
    """Admin overlay session flag and device-list payload used by Register Device."""

    def test_admin_session_defaults_overlay_true(
        self, client, mock_context, admin_user
    ):
        """POST /api/admin/session defaults admin_overlay_open to True."""
        resp = client.post("/api/admin/session")
        assert resp.status_code == 200
        assert mock_context.admin_overlay_open is True
        assert mock_context.session_mgr.has_active_session
        assert mock_context.session_mgr.current_session.user.id == admin_user.id

    def test_admin_session_overlay_false(
        self, client, mock_context, admin_user
    ):
        """POST /api/admin/session?overlay=false clears the overlay flag."""
        resp = client.post("/api/admin/session?overlay=false")
        assert resp.status_code == 200
        assert mock_context.admin_overlay_open is False

    def test_admin_session_existing_keeps_user(
        self, client, mock_context, test_user, admin_user
    ):
        """Non-admin session: overlay POST is refused; sticker auto-intent stays on."""
        mock_context.session_mgr.start_session(test_user)
        mock_context.admin_overlay_open = False
        resp = client.post("/api/admin/session?overlay=true")
        assert resp.status_code == 403
        assert mock_context.admin_overlay_open is False
        assert mock_context.session_mgr.current_session.user.id == test_user.id

    def test_admin_session_admin_may_update_overlay(
        self, client, mock_context, admin_user
    ):
        """Loopback overlay flag update is allowed for an already-logged-in admin."""
        mock_context.session_mgr.start_session(admin_user)
        mock_context.admin_overlay_open = True
        resp = client.post("/api/admin/session?overlay=false")
        assert resp.status_code == 200
        assert mock_context.admin_overlay_open is False
        assert mock_context.session_mgr.current_session.user.id == admin_user.id

    def test_admin_session_deactivated_user_ends_session(
        self, client, mock_context, admin_user, db_session
    ):
        """A session whose account was deactivated dies on the next overlay
        POST — the cached role must not bless a dead session."""
        mock_context.session_mgr.start_session(admin_user)
        admin_user.is_active = False
        db_session.commit()

        resp = client.post("/api/admin/session")
        assert resp.status_code == 401
        assert not mock_context.session_mgr.has_active_session
        events = [c.args[0] for c in mock_context.broadcast_sse.call_args_list]
        assert {
            "event": "session_ended", "reason": "account_inactive"
        } in events

    def test_admin_session_demoted_admin_is_403(
        self, client, mock_context, admin_user, db_session
    ):
        """A mid-session demote stops passing the admin overlay gate — the
        role is re-read, not trusted from the login snapshot."""
        mock_context.session_mgr.start_session(admin_user)
        admin_user.role = UserRole.USER
        db_session.commit()

        resp = client.post("/api/admin/session")
        assert resp.status_code == 403
        # The session survives — it is a live user, just no longer admin.
        assert mock_context.session_mgr.has_active_session

    def test_admin_session_refuses_lan(
        self, lan_client, mock_context, admin_user
    ):
        """LAN callers cannot create the process-global admin session."""
        resp = lan_client.post("/api/admin/session")
        assert resp.status_code == 403
        assert not mock_context.session_mgr.has_active_session

    def test_admin_session_lan_cannot_flip_overlay(
        self, lan_client, mock_context, test_user, admin_user
    ):
        """LAN overlay=true is not auth and must not change an existing session."""
        mock_context.session_mgr.start_session(test_user)
        mock_context.admin_overlay_open = False
        resp = lan_client.post("/api/admin/session?overlay=true")
        assert resp.status_code == 403
        assert mock_context.admin_overlay_open is False
        assert mock_context.session_mgr.current_session.user.id == test_user.id

    def test_register_cancel_clears_pending_tag_bind(self, client, mock_context):
        """POST /api/register/cancel clears a pending kiosk device-tag bind."""
        mock_context.pending_tag_bind = PendingTagBind(device_id=1)
        resp = client.post("/api/register/cancel")
        assert resp.status_code == 200
        assert mock_context.pending_tag_bind is None

    def test_register_cancel_keeps_dashboard_armed_bind(self, client, mock_context):
        """Public cancel must not clear a dashboard-secret-armed bind."""
        mock_context.pending_tag_bind = PendingTagBind(
            device_id=1, from_dashboard=True
        )
        resp = client.post("/api/register/cancel")
        assert resp.status_code == 200
        assert mock_context.pending_tag_bind is not None
        assert mock_context.pending_tag_bind.device_id == 1
        assert mock_context.pending_tag_bind.from_dashboard is True

    def test_admin_devices_requires_session(self, client, mock_context):
        """GET /api/admin/devices returns 401 without a session."""
        resp = client.get("/api/admin/devices")
        assert resp.status_code == 401

    def test_admin_devices_rejects_non_admin(
        self, client, mock_context, test_user
    ):
        """GET /api/admin/devices returns 403 for a normal user."""
        mock_context.session_mgr.start_session(test_user)
        resp = client.get("/api/admin/devices")
        assert resp.status_code == 403

    def test_admin_devices_lists_untagged_units(
        self, client, mock_context, admin_user, test_devices, db_session
    ):
        """A cabinet unit with no sticker stays on the manage list."""
        DeviceRepository.create(
            db_session,
            name="Fresh Unit",
            device_type="Tool",
            pm_number="PM-NEW",
            locker_slot=9,
        )
        db_session.commit()
        mock_context.session_mgr.start_session(admin_user)
        resp = client.get("/api/admin/devices")
        assert resp.status_code == 200
        by_name = {d["name"]: d for d in resp.json()}
        assert by_name["Fresh Unit"]["has_tag"] is False
        assert "tag_hmac" not in by_name["Fresh Unit"]
        assert by_name["Camera"]["has_tag"] is True

    def test_list_devices_duplicate_name_distinct_pm(
        self, client, mock_context, admin_user, db_session
    ):
        """Same name, different PM — both rows in the bind-list payload."""
        DeviceRepository.create(
            db_session,
            name="Fluke 87V",
            device_type="Multimeter",
            pm_number="PM-101",
            locker_slot=1,
        )
        DeviceRepository.create(
            db_session,
            name="Fluke 87V",
            device_type="Multimeter",
            pm_number="PM-102",
            locker_slot=2,
        )
        db_session.commit()
        mock_context.session_mgr.start_session(admin_user)
        resp = client.get("/api/admin/devices")
        assert resp.status_code == 200
        rows = [d for d in resp.json() if d["name"] == "Fluke 87V"]
        assert len(rows) == 2
        pms = {d["pm_number"] for d in rows}
        assert pms == {"PM-101", "PM-102"}
        for d in rows:
            assert d["name"] == "Fluke 87V"
            assert "pm_number" in d
            assert "has_tag" in d
            assert "tag_hmac" not in d

    def test_list_devices_has_tag_true(
        self, client, mock_context, test_user, test_devices
    ):
        """has_tag is True when tag_hmac is set; digest is not in the payload."""
        # test_devices rows are already sticker-bound.
        mock_context.session_mgr.start_session(test_user)
        resp = client.get("/api/devices")
        cam = next(d for d in resp.json() if d["name"] == "Camera")
        assert cam["has_tag"] is True
        assert "tag_hmac" not in cam

    def test_dashboard_devices_omits_tag_hmac(
        self, client, test_devices, db_session, hmac_key
    ):
        """Public dashboard JSON must not include tag_hmac after a bind."""
        digest = compute_uid_hmac("AABBCCDD", hmac_key)
        DeviceRepository.bind_tag(db_session, test_devices[0], digest)
        db_session.commit()
        resp = client.get("/api/dashboard/devices")
        assert resp.status_code == 200
        rows = resp.json()
        assert rows
        for row in rows:
            assert "tag_hmac" not in row
            assert "barcode" not in row
            assert "has_tag" in row
            dumped = str(row)
            assert digest not in dumped
        cam = next(r for r in rows if r["name"] == "Camera")
        assert cam["has_tag"] is True


class TestAdminSyncAndUpdateEndpoints:
    """Auth-gate tests for the sync-preview, sync-status, and software-update
    admin endpoints. Kiosk mutations are loopback + session + admin role.
    Dashboard catalog GETs stay public; dashboard mutations use the admin secret.
    """

    def test_sync_preview_requires_session(self, client, mock_context):
        resp = client.post("/api/admin/sync-preview")
        assert resp.status_code == 401

    def test_sync_preview_rejects_non_admin(self, client, mock_context, test_user):
        mock_context.session_mgr.start_session(test_user)
        resp = client.post("/api/admin/sync-preview")
        assert resp.status_code == 403

    def test_sync_status_requires_session(self, client, mock_context):
        resp = client.get("/api/admin/sync-status")
        assert resp.status_code == 401

    def test_sync_status_rejects_non_admin(self, client, mock_context, test_user):
        mock_context.session_mgr.start_session(test_user)
        resp = client.get("/api/admin/sync-status")
        assert resp.status_code == 403

    def test_sync_status_accepts_admin(self, client, mock_context, admin_user):
        mock_context.session_mgr.start_session(admin_user)
        resp = client.get("/api/admin/sync-status")
        assert resp.status_code == 200

    def test_sync_status_includes_local_and_relative(self, client, mock_context, admin_user):
        """Admin last-sync JSON includes at_local and ago for the footer clock."""
        from types import SimpleNamespace

        from smart_locker.sync import sync_status

        sync_status.record_result(
            "manual",
            SimpleNamespace(imported=0, updated=1, unchanged=0, errors=0),
        )
        mock_context.session_mgr.start_session(admin_user)
        row = client.get("/api/admin/sync-status").json()
        assert row["at"]
        assert row["at_local"]
        assert row["ago"]
        assert row["trigger"] == "manual"

    def test_update_status_requires_session(self, client, mock_context):
        resp = client.get("/api/admin/update-status")
        assert resp.status_code == 401

    def test_update_status_rejects_non_admin(self, client, mock_context, test_user):
        mock_context.session_mgr.start_session(test_user)
        resp = client.get("/api/admin/update-status")
        assert resp.status_code == 403

    def test_update_status_accepts_admin(self, client, mock_context, admin_user):
        mock_context.session_mgr.start_session(admin_user)
        resp = client.get("/api/admin/update-status")
        assert resp.status_code == 200
        assert resp.json()["state"] == "idle"

    def test_health_exposes_update_without_session(self, client, mock_context):
        """Kiosk overlay reads update verdict from public /api/health after restart."""
        resp = client.get("/api/health")
        assert resp.status_code == 200
        body = resp.json()
        assert "status" in body
        assert "update" in body
        assert "last_writeback" in body

    def test_trigger_update_requires_auth_with_admin(self, client, mock_context, admin_user):
        """Once an admin exists, an unauthenticated update POST is refused."""
        resp = client.post("/api/admin/update")
        assert resp.status_code == 401

    def test_trigger_update_first_boot_allowed(
        self, client, mock_context, monkeypatch
    ):
        """First boot (no admin, no secret) may launch Software Update."""
        import smart_locker.api.routes as routes_module

        monkeypatch.setattr(routes_module, "_SYSTEMD_RUN", "/usr/bin/systemd-run")
        monkeypatch.setattr(routes_module, "launch_update", lambda *a: None)
        resp = client.post("/api/admin/update")
        assert resp.status_code == 200

    def test_trigger_update_first_boot_refuses_lan(
        self, lan_client, mock_context, monkeypatch
    ):
        """First-boot update openness is kiosk-local — a LAN caller must not
        launch the root updater unit before any admin or secret exists."""
        import smart_locker.api.routes as routes_module

        monkeypatch.setattr(routes_module, "_SYSTEMD_RUN", "/usr/bin/systemd-run")
        monkeypatch.setattr(routes_module, "launch_update", lambda *a: None)
        resp = lan_client.post("/api/admin/update")
        assert resp.status_code == 401

    def test_trigger_update_non_ascii_header_is_401_not_500(
        self, lan_client, mock_context, admin_user, dashboard_secret, monkeypatch
    ):
        """A latin-1 admin header must not crash secrets.compare_digest."""
        import smart_locker.api.routes as routes_module

        monkeypatch.setattr(routes_module, "_SYSTEMD_RUN", "/usr/bin/systemd-run")
        monkeypatch.setattr(routes_module, "launch_update", lambda *a: None)
        resp = lan_client.post(
            "/api/admin/update",
            headers={"X-Smart-Locker-Admin": "päss".encode("latin-1")},
        )
        assert resp.status_code == 401

    def test_trigger_update_accepts_dashboard_secret(
        self, client, lan_client, mock_context, admin_user,
        dashboard_secret, monkeypatch,
    ):
        """The dashboard secret authorizes the update from the LAN too."""
        import smart_locker.api.routes as routes_module

        monkeypatch.setattr(routes_module, "_SYSTEMD_RUN", "/usr/bin/systemd-run")
        monkeypatch.setattr(routes_module, "launch_update", lambda *a: None)
        headers = {"X-Smart-Locker-Admin": dashboard_secret}
        assert client.post("/api/admin/update", headers=headers).status_code == 200
        assert lan_client.post("/api/admin/update", headers=headers).status_code == 200

    def test_trigger_update_rejects_bad_secret(
        self, lan_client, mock_context, admin_user, dashboard_secret, monkeypatch
    ):
        import smart_locker.api.routes as routes_module

        monkeypatch.setattr(routes_module, "_SYSTEMD_RUN", "/usr/bin/systemd-run")
        monkeypatch.setattr(routes_module, "launch_update", lambda *a: None)
        resp = lan_client.post(
            "/api/admin/update", headers={"X-Smart-Locker-Admin": "wrong"}
        )
        assert resp.status_code == 401

    def test_trigger_update_rejects_non_admin(self, client, mock_context, test_user):
        mock_context.session_mgr.start_session(test_user)
        resp = client.post("/api/admin/update")
        assert resp.status_code == 403

    def test_trigger_update_deactivated_session_is_401(
        self, client, mock_context, admin_user, db_session
    ):
        """A deactivated account's session does not authorize an update —
        the loopback-session gate re-reads the user row."""
        mock_context.session_mgr.start_session(admin_user)
        admin_user.is_active = False
        db_session.commit()
        resp = client.post("/api/admin/update")
        assert resp.status_code == 401
        assert not mock_context.session_mgr.has_active_session

    def test_trigger_update_demoted_admin_is_403(
        self, client, mock_context, admin_user, db_session
    ):
        """A mid-session demote loses update authorization on the live role."""
        mock_context.session_mgr.start_session(admin_user)
        admin_user.role = UserRole.USER
        db_session.commit()
        resp = client.post("/api/admin/update")
        assert resp.status_code == 403
        assert mock_context.session_mgr.has_active_session

    def test_trigger_update_unavailable_off_pi(self, client, mock_context, admin_user, monkeypatch):
        """When systemd-run isn't present (dev/CI host), the endpoint refuses cleanly
        (503) rather than pretending to update -- it must never fall through to
        attempting the privileged update path off the Pi."""
        import smart_locker.api.routes as routes_module

        monkeypatch.setattr(routes_module, "_SYSTEMD_RUN", None)
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post("/api/admin/update")
        assert resp.status_code == 503

    def test_trigger_update_refuses_lan(
        self, lan_client, mock_context, admin_user, monkeypatch
    ):
        """LAN cannot launch Software Update without the dashboard secret."""
        import smart_locker.api.routes as routes_module

        monkeypatch.setattr(routes_module, "_SYSTEMD_RUN", "/usr/bin/systemd-run")
        mock_context.session_mgr.start_session(admin_user)
        resp = lan_client.post("/api/admin/update")
        assert resp.status_code == 401

    def test_stop_system_requires_session(self, client, mock_context):
        resp = client.post("/api/admin/stop-system")
        assert resp.status_code == 401

    def test_stop_system_rejects_non_admin(self, client, mock_context, test_user):
        mock_context.session_mgr.start_session(test_user)
        resp = client.post("/api/admin/stop-system")
        assert resp.status_code == 403

    def test_stop_system_unavailable_off_pi(self, client, mock_context, admin_user, monkeypatch):
        """Dev/Windows hosts (no systemctl) must refuse cleanly — 503, not a hang."""
        import smart_locker.api.routes as routes_module

        monkeypatch.setattr(routes_module, "_SYSTEMCTL", None)
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post("/api/admin/stop-system")
        assert resp.status_code == 503

    def test_stop_system_accepts_admin(self, client, mock_context, admin_user, monkeypatch):
        import smart_locker.api.routes as routes_module

        monkeypatch.setattr(routes_module, "_SYSTEMCTL", "/usr/bin/systemctl")
        monkeypatch.setattr(routes_module, "stop_system", lambda: None)
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post("/api/admin/stop-system")
        assert resp.status_code == 200
        assert resp.json().get("ok") is True
        assert not mock_context.session_mgr.has_active_session
        assert mock_context.admin_overlay_open is False

    def test_stop_system_stops_browser_then_service(
        self, client, mock_context, admin_user, monkeypatch
    ):
        """The post-response task closes Chromium, then kills any in-flight
        update unit (so its final `systemctl start` can't resurrect the box),
        then stops the service."""
        import smart_locker.api.routes as routes_module
        import smart_locker.services.appliance as appliance

        calls = []
        monkeypatch.setattr(routes_module, "_SYSTEMCTL", "/usr/bin/systemctl")
        monkeypatch.setattr(appliance, "exit_kiosk", lambda: calls.append("browser"))
        monkeypatch.setattr(appliance, "_stop_update_unit", lambda: calls.append("update"))
        monkeypatch.setattr(appliance, "stop_service", lambda: calls.append("service"))
        monkeypatch.setattr(appliance, "_STOP_RESPONSE_DELAY_SECONDS", 0)
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post("/api/admin/stop-system")
        assert resp.status_code == 200
        assert calls == ["browser", "update", "service"]

    def test_stop_system_still_stops_service_when_kiosk_close_raises(
        self, client, mock_context, admin_user, monkeypatch
    ):
        """An unexpected exit_kiosk failure (OSError, not just the declared
        appliance errors) must not keep the service running."""
        import smart_locker.api.routes as routes_module
        import smart_locker.services.appliance as appliance

        calls = []

        def _boom():
            raise OSError("pgrep vanished mid-stop")

        monkeypatch.setattr(routes_module, "_SYSTEMCTL", "/usr/bin/systemctl")
        monkeypatch.setattr(appliance, "exit_kiosk", _boom)
        monkeypatch.setattr(appliance, "_stop_update_unit", lambda: calls.append("update"))
        monkeypatch.setattr(appliance, "stop_service", lambda: calls.append("service"))
        monkeypatch.setattr(appliance, "_STOP_RESPONSE_DELAY_SECONDS", 0)
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post("/api/admin/stop-system")
        assert resp.status_code == 200
        assert calls == ["update", "service"]

    def test_exit_kiosk_alias_behaves_like_stop_system(
        self, client, mock_context, admin_user, monkeypatch
    ):
        """Cached pre-rename kiosk builds still POST /api/admin/exit-kiosk —
        the alias must hit the same handler instead of 404ing."""
        import smart_locker.api.routes as routes_module

        monkeypatch.setattr(routes_module, "_SYSTEMCTL", "/usr/bin/systemctl")
        monkeypatch.setattr(routes_module, "stop_system", lambda: None)
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post("/api/admin/exit-kiosk")
        assert resp.status_code == 200
        assert resp.json().get("ok") is True

    def test_exit_kiosk_alias_requires_session(self, client, mock_context):
        resp = client.post("/api/admin/exit-kiosk")
        assert resp.status_code == 401

    def test_stop_system_refuses_lan(
        self, lan_client, mock_context, admin_user, monkeypatch
    ):
        """LAN cannot stop the system even with a live admin session."""
        import smart_locker.api.routes as routes_module

        monkeypatch.setattr(routes_module, "_SYSTEMCTL", "/usr/bin/systemctl")
        monkeypatch.setattr(routes_module, "stop_system", lambda: None)
        mock_context.session_mgr.start_session(admin_user)
        resp = lan_client.post("/api/admin/stop-system")
        assert resp.status_code == 403

    def test_shutdown_requires_session(self, client, mock_context):
        resp = client.post("/api/admin/shutdown")
        assert resp.status_code == 401

    def test_shutdown_rejects_non_admin(self, client, mock_context, test_user):
        mock_context.session_mgr.start_session(test_user)
        resp = client.post("/api/admin/shutdown")
        assert resp.status_code == 403

    def test_shutdown_unavailable_off_pi(self, client, mock_context, admin_user, monkeypatch):
        """Dev/Windows hosts must not invoke poweroff — 503, not a hang."""
        import smart_locker.api.routes as routes_module
        from smart_locker.services.appliance import ApplianceUnavailable

        def _boom():
            raise ApplianceUnavailable(
                "Shut down runs on the Raspberry Pi appliance only."
            )

        monkeypatch.setattr(routes_module, "appliance_shutdown", _boom)
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post("/api/admin/shutdown")
        assert resp.status_code == 503

    def test_shutdown_accepts_admin(self, client, mock_context, admin_user, monkeypatch):
        import smart_locker.api.routes as routes_module

        monkeypatch.setattr(routes_module, "appliance_shutdown", lambda: None)
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post("/api/admin/shutdown")
        assert resp.status_code == 200
        assert resp.json().get("ok") is True

    def test_shutdown_refuses_lan(
        self, lan_client, mock_context, admin_user, monkeypatch
    ):
        """LAN cannot power off even with a live admin session."""
        import smart_locker.api.routes as routes_module

        monkeypatch.setattr(routes_module, "appliance_shutdown", lambda: None)
        mock_context.session_mgr.start_session(admin_user)
        resp = lan_client.post("/api/admin/shutdown")
        assert resp.status_code == 403

