"""
File: test_admin.py
Description: Tests for kiosk admin overlay, device-tag bind/unbind, Register
             Device catalog pick list, sync, update, Exit kiosk, and Shut down.
Project: smart_locker/tests/api
Notes: Run with: python -m pytest tests/api/test_admin.py -v
"""
import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from smart_locker.api.app_context import PendingRegistration, PendingTagBind
from smart_locker.api.routes import router
from smart_locker.auth.session_manager import SessionManager
from smart_locker.database.models import DeviceStatus, UserRole
from smart_locker.database.repositories import DeviceRepository, RegistrantRepository, UserRepository
from smart_locker.security.hashing import compute_uid_hmac

import smart_locker.api.app_context as ctx_module
import smart_locker.api.routes as routes_module

class TestDeviceTagBindApi:
    """Auth gates for bind/unbind, has_tag on the kiosk list, duplicate names."""

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
        self, client, mock_context, admin_user, test_devices, db_session, hmac_key
    ):
        """Admin unbind-tag clears tag_hmac; GET list has has_tag False."""
        DeviceRepository.bind_tag(
            db_session,
            test_devices[0],
            compute_uid_hmac("AABBCCDD", hmac_key),
        )
        db_session.commit()
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(f"/api/admin/devices/{test_devices[0].id}/unbind-tag")
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        db_session.expire_all()
        assert test_devices[0].tag_hmac is None
        listed = client.get("/api/devices").json()
        # Untagged devices are admin-only: Camera drops off the kiosk list.
        assert "Camera" not in {d["name"] for d in listed}
        assert mock_context.pending_tag_bind is None

    def test_unbind_tag_clears_pending_bind(
        self, client, mock_context, admin_user, test_devices, db_session, hmac_key
    ):
        """Kiosk unbind cancels an armed bind window so the next tap is not rebound."""
        DeviceRepository.bind_tag(
            db_session,
            test_devices[0],
            compute_uid_hmac("AABBCCDD", hmac_key),
        )
        db_session.commit()
        mock_context.session_mgr.start_session(admin_user)
        mock_context.pending_tag_bind = PendingTagBind(device_id=test_devices[0].id)
        resp = client.post(f"/api/admin/devices/{test_devices[0].id}/unbind-tag")
        assert resp.status_code == 200
        assert mock_context.pending_tag_bind is None

    def test_unbind_tag_unknown_device(self, client, mock_context, admin_user):
        """POST unbind-tag returns 404 when the device id does not exist."""
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post("/api/admin/devices/99999/unbind-tag")
        assert resp.status_code == 404

    def test_unbind_tag_rejects_borrowed_device(
        self, client, mock_context, admin_user, test_user, test_devices, db_session
    ):
        """Unbind on a borrowed device is 409; the tag and kiosk row survive."""
        mock_context.session_mgr.start_session(test_user)
        borrowed = client.post(f"/api/devices/{test_devices[0].id}/borrow")
        assert borrowed.json()["success"] is True
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(f"/api/admin/devices/{test_devices[0].id}/unbind-tag")
        assert resp.status_code == 409
        db_session.expire_all()
        assert test_devices[0].tag_hmac is not None
        listed = client.get("/api/devices").json()
        assert "Camera" in {d["name"] for d in listed}

    def test_borrow_unbind409_return_unbind_ok(
        self, client, mock_context, admin_user, test_user, test_devices, db_session
    ):
        """Borrowed row refuses unbind (409); after the return unbind succeeds."""
        mock_context.session_mgr.start_session(test_user)
        borrowed = client.post(f"/api/devices/{test_devices[0].id}/borrow")
        assert borrowed.json()["success"] is True

        mock_context.session_mgr.start_session(admin_user)
        refused = client.post(f"/api/admin/devices/{test_devices[0].id}/unbind-tag")
        assert refused.status_code == 409

        mock_context.session_mgr.start_session(test_user)
        returned = client.post(f"/api/devices/{test_devices[0].id}/return")
        assert returned.json()["success"] is True

        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(f"/api/admin/devices/{test_devices[0].id}/unbind-tag")
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        db_session.expire_all()
        assert test_devices[0].tag_hmac is None


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

    def test_list_devices_duplicate_name_distinct_pm(
        self, client, mock_context, test_user, db_session, hmac_key
    ):
        """Same name, different PM — both rows in the bind-list payload."""
        d1 = DeviceRepository.create(
            db_session,
            name="Fluke 87V",
            device_type="Multimeter",
            pm_number="PM-101",
            locker_slot=1,
        )
        d2 = DeviceRepository.create(
            db_session,
            name="Fluke 87V",
            device_type="Multimeter",
            pm_number="PM-102",
            locker_slot=2,
        )
        d1.tag_hmac = compute_uid_hmac("TAG-101", hmac_key)
        d2.tag_hmac = compute_uid_hmac("TAG-102", hmac_key)
        db_session.commit()
        mock_context.session_mgr.start_session(test_user)
        resp = client.get("/api/devices")
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
        self, client, mock_context, test_user, test_devices, db_session, hmac_key
    ):
        """has_tag flips False->True across a bind; digest is not in the payload."""
        DeviceRepository.unbind_tag(db_session, test_devices[0])
        db_session.commit()
        assert test_devices[0].tag_hmac is None
        DeviceRepository.bind_tag(
            db_session,
            test_devices[0],
            compute_uid_hmac("AABBCCDD", hmac_key),
        )
        db_session.commit()
        mock_context.session_mgr.start_session(test_user)
        resp = client.get("/api/devices")
        cam = next(d for d in resp.json() if d["name"] == "Camera")
        assert cam["has_tag"] is True
        assert "tag_hmac" not in cam

    def test_dashboard_devices_omits_tag_hmac(
        self, client, test_devices, db_session, hmac_key
    ):
        """Public dashboard JSON must not include tag_hmac after a bind."""
        DeviceRepository.unbind_tag(db_session, test_devices[0])
        db_session.commit()
        assert test_devices[0].tag_hmac is None
        assert (
            next(
                r
                for r in client.get("/api/dashboard/devices").json()
                if r["name"] == "Camera"
            )["has_tag"]
            is False
        )
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

    def test_trigger_update_requires_session(self, client, mock_context):
        resp = client.post("/api/admin/update")
        assert resp.status_code == 401

    def test_trigger_update_rejects_non_admin(self, client, mock_context, test_user):
        mock_context.session_mgr.start_session(test_user)
        resp = client.post("/api/admin/update")
        assert resp.status_code == 403

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
        """LAN cannot launch Software Update even with a live admin session."""
        import smart_locker.api.routes as routes_module

        monkeypatch.setattr(routes_module, "_SYSTEMD_RUN", "/usr/bin/systemd-run")
        mock_context.session_mgr.start_session(admin_user)
        resp = lan_client.post("/api/admin/update")
        assert resp.status_code == 403

    def test_exit_kiosk_requires_session(self, client, mock_context):
        resp = client.post("/api/admin/exit-kiosk")
        assert resp.status_code == 401

    def test_exit_kiosk_rejects_non_admin(self, client, mock_context, test_user):
        mock_context.session_mgr.start_session(test_user)
        resp = client.post("/api/admin/exit-kiosk")
        assert resp.status_code == 403

    def test_exit_kiosk_unavailable_off_pi(self, client, mock_context, admin_user, monkeypatch):
        """Dev/Windows hosts must not try to kill a browser — 503, not a hang."""
        import smart_locker.api.routes as routes_module
        from smart_locker.services.appliance import ApplianceUnavailable

        def _boom():
            raise ApplianceUnavailable(
                "Kiosk exit runs on the Raspberry Pi appliance only."
            )

        monkeypatch.setattr(routes_module, "exit_kiosk", _boom)
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post("/api/admin/exit-kiosk")
        assert resp.status_code == 503

    def test_exit_kiosk_accepts_admin(self, client, mock_context, admin_user, monkeypatch):
        import smart_locker.api.routes as routes_module

        monkeypatch.setattr(routes_module, "exit_kiosk", lambda: None)
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post("/api/admin/exit-kiosk")
        assert resp.status_code == 200
        assert resp.json().get("ok") is True
        assert not mock_context.session_mgr.has_active_session
        assert mock_context.admin_overlay_open is False

    def test_exit_kiosk_refuses_lan(
        self, lan_client, mock_context, admin_user, monkeypatch
    ):
        """LAN cannot stop Chromium even with a live admin session."""
        import smart_locker.api.routes as routes_module

        monkeypatch.setattr(routes_module, "exit_kiosk", lambda: None)
        mock_context.session_mgr.start_session(admin_user)
        resp = lan_client.post("/api/admin/exit-kiosk")
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


class TestCatalogLockerList:
    """GET /api/admin/devices/catalog-locker — Register Device pick list."""

    @staticmethod
    def _workbook(path, rows):
        """Write a catalog workbook (first row = headers)."""
        wb = Workbook()
        ws = wb.active
        for row in rows:
            ws.append(row)
        wb.save(path)
        return path

    def test_requires_session(self, client, mock_context):
        """No session → 401."""
        assert client.get("/api/admin/devices/catalog-locker").status_code == 401

    def test_requires_admin(self, client, mock_context, test_user):
        """A normal user session is 403."""
        mock_context.session_mgr.start_session(test_user)
        assert client.get("/api/admin/devices/catalog-locker").status_code == 403

    def test_no_source_path_is_400(
        self, client, mock_context, admin_user, monkeypatch
    ):
        """SOURCE_EXCEL_PATH unset → 400."""
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", "")
        mock_context.session_mgr.start_session(admin_user)
        resp = client.get("/api/admin/devices/catalog-locker")
        assert resp.status_code == 400

    def test_missing_workbook_is_503(
        self, client, mock_context, admin_user, monkeypatch, tmp_path
    ):
        """Share down → 503."""
        monkeypatch.setattr(
            "config.settings.SOURCE_EXCEL_PATH",
            str(tmp_path / "missing.xlsx"),
        )
        mock_context.session_mgr.start_session(admin_user)
        resp = client.get("/api/admin/devices/catalog-locker")
        assert resp.status_code == 503

    def test_returns_unregistered_locker_rows(
        self, client, mock_context, admin_user, test_devices, monkeypatch, tmp_path
    ):
        """In-locker Excel rows minus registered PMs, sorted by name."""
        path = self._workbook(tmp_path / "device-list.xlsx", [
            ["Equipment", "Name", "Manufacturer", "Model", "Location"],
            ["PM-001", "Camera", "Fluke", "87V", "Locker"],   # registered
            ["PM-100", "Zeta Scope", "Keysight", "DSOX", "Locker"],
            ["PM-101", "Alpha Meter", "BK", "880", "locker"],
            ["PM-102", "Bench PSU", "R&S", "HMC", "Jack B."],  # not in locker
        ])
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))
        mock_context.session_mgr.start_session(admin_user)
        resp = client.get("/api/admin/devices/catalog-locker")
        assert resp.status_code == 200
        rows = resp.json()["rows"]
        assert [r["pm_number"] for r in rows] == ["PM-101", "PM-100"]
        assert set(rows[0]) == {"pm_number", "name", "manufacturer", "model"}

    def test_other_cabinets_excluded(
        self, client, mock_context, admin_user, test_devices, monkeypatch, tmp_path
    ):
        """Cabinet rows are not offered — only this locker's token matches."""
        path = self._workbook(tmp_path / "device-list.xlsx", [
            ["Equipment", "Name", "Manufacturer", "Model", "Location"],
            ["PM-100", "Zeta Scope", "Keysight", "DSOX", "Locker"],
            ["PM-101", "Alpha Meter", "BK", "880", "Cabinet A"],
        ])
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))
        mock_context.session_mgr.start_session(admin_user)
        resp = client.get("/api/admin/devices/catalog-locker")
        assert resp.status_code == 200
        assert [r["pm_number"] for r in resp.json()["rows"]] == ["PM-100"]

    def test_no_location_column_returns_empty(
        self, client, mock_context, admin_user, monkeypatch, tmp_path
    ):
        """Sheet without a Location column → empty rows list."""
        path = self._workbook(tmp_path / "device-list.xlsx", [
            ["Equipment", "Name"],
            ["PM-100", "Scope"],
        ])
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))
        mock_context.session_mgr.start_session(admin_user)
        resp = client.get("/api/admin/devices/catalog-locker")
        assert resp.status_code == 200
        assert resp.json() == {"rows": []}

    def test_locked_workbook_is_503(
        self, client, mock_context, admin_user, monkeypatch, tmp_path
    ):
        """Share file locked during the copy step → 503."""
        import shutil

        path = self._workbook(tmp_path / "device-list.xlsx", [
            ["Equipment", "Name", "Location"],
            ["PM-100", "Scope", "Locker"],
        ])

        def _locked(_src, _dst):
            raise PermissionError("locked")

        monkeypatch.setattr(shutil, "copy2", _locked)
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))
        mock_context.session_mgr.start_session(admin_user)
        resp = client.get("/api/admin/devices/catalog-locker")
        assert resp.status_code == 503

    def test_corrupt_workbook_is_503(
        self, client, mock_context, admin_user, monkeypatch, tmp_path
    ):
        """Text saved as .xlsx is unreadable → 503."""
        path = tmp_path / "device-list.xlsx"
        path.write_text("this is not a workbook", encoding="utf-8")
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))
        mock_context.session_mgr.start_session(admin_user)
        resp = client.get("/api/admin/devices/catalog-locker")
        assert resp.status_code == 503

    def test_header_only_workbook_is_503(
        self, client, mock_context, admin_user, monkeypatch, tmp_path
    ):
        """Headers with no data rows → 503, not an empty pick list."""
        path = self._workbook(tmp_path / "device-list.xlsx", [
            ["Equipment", "Name", "Location"],
        ])
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))
        mock_context.session_mgr.start_session(admin_user)
        resp = client.get("/api/admin/devices/catalog-locker")
        assert resp.status_code == 503

    def test_no_pm_column_is_503(
        self, client, mock_context, admin_user, monkeypatch, tmp_path
    ):
        """Sheet without a PM/equipment column → 503."""
        path = self._workbook(tmp_path / "device-list.xlsx", [
            ["Name", "Location"],
            ["Scope", "Locker"],
        ])
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))
        mock_context.session_mgr.start_session(admin_user)
        resp = client.get("/api/admin/devices/catalog-locker")
        assert resp.status_code == 503

    def test_sorted_by_name_then_pm(
        self, client, mock_context, admin_user, monkeypatch, tmp_path
    ):
        """Same-name rows tie-break on PM number."""
        path = self._workbook(tmp_path / "device-list.xlsx", [
            ["Equipment", "Name", "Location"],
            ["PM-102", "Meter", "Locker"],
            ["PM-101", "Meter", "Locker"],
            ["PM-100", "Alpha Probe", "Locker"],
        ])
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))
        mock_context.session_mgr.start_session(admin_user)
        resp = client.get("/api/admin/devices/catalog-locker")
        assert resp.status_code == 200
        assert [r["pm_number"] for r in resp.json()["rows"]] == [
            "PM-100",
            "PM-101",
            "PM-102",
        ]

    def test_register_excludes_pm_from_pick_list(
        self, client, mock_context, admin_user, db_session, monkeypatch, tmp_path
    ):
        """Registering a PM drops it from the pick list on the next GET."""
        path = self._workbook(tmp_path / "device-list.xlsx", [
            ["Equipment", "Name", "Manufacturer", "Model", "Location"],
            ["PM-100", "Zeta Scope", "Keysight", "DSOX", "Locker"],
            ["PM-101", "Alpha Meter", "BK", "880", "Locker"],
        ])
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))
        monkeypatch.setattr(
            "smart_locker.sync.location_writeback.schedule_write_location",
            lambda: None,
        )
        mock_context.session_mgr.start_session(admin_user)
        mock_context.pending_tag_bind = None
        before = client.get("/api/admin/devices/catalog-locker").json()["rows"]
        assert [r["pm_number"] for r in before] == ["PM-101", "PM-100"]

        created = client.post(
            "/api/admin/devices/register",
            json={"pm_number": "PM-100", "locker_slot": 5},
        )
        assert created.status_code == 200
        assert DeviceRepository.find_by_pm(db_session, "PM-100") is not None

        after = client.get("/api/admin/devices/catalog-locker").json()["rows"]
        assert [r["pm_number"] for r in after] == ["PM-101"]


class TestAdminDevicesList:
    """GET /api/admin/devices — every locker row for the Register Device panel."""

    def test_requires_session(self, client, mock_context):
        """No session → 401."""
        assert client.get("/api/admin/devices").status_code == 401

    def test_requires_admin(self, client, mock_context, test_user, test_devices):
        """A normal user session is 403."""
        mock_context.session_mgr.start_session(test_user)
        assert client.get("/api/admin/devices").status_code == 403

    def test_lists_tagged_and_untagged(
        self, client, mock_context, admin_user, test_devices, db_session
    ):
        """Admin list includes untagged rows; the kiosk list omits them."""
        ghost = DeviceRepository.create(
            db_session,
            name="Ghost",
            device_type="general",
            pm_number="PM-999",
            locker_slot=9,
        )
        db_session.commit()
        mock_context.session_mgr.start_session(admin_user)

        resp = client.get("/api/admin/devices")
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 4
        ghost_row = next(r for r in rows if r["name"] == "Ghost")
        assert ghost_row["has_tag"] is False
        assert "tag_hmac" not in ghost_row

        kiosk = client.get("/api/devices").json()
        assert "Ghost" not in {d["name"] for d in kiosk}
        kiosk_row = kiosk[0]
        assert set(ghost_row) == set(kiosk_row)

    def test_duplicate_name_untagged_twin_on_admin_list(
        self, client, mock_context, admin_user, db_session, hmac_key
    ):
        """Same name, different PM — the admin bind list shows the untagged twin."""
        tagged = DeviceRepository.create(
            db_session,
            name="Fluke 87V",
            device_type="Multimeter",
            pm_number="PM-101",
            locker_slot=1,
        )
        twin = DeviceRepository.create(
            db_session,
            name="Fluke 87V",
            device_type="Multimeter",
            pm_number="PM-102",
            locker_slot=2,
        )
        tagged.tag_hmac = compute_uid_hmac("TAG-101", hmac_key)
        db_session.commit()
        assert twin.tag_hmac is None
        mock_context.session_mgr.start_session(admin_user)

        resp = client.get("/api/admin/devices")
        assert resp.status_code == 200
        rows = [d for d in resp.json() if d["name"] == "Fluke 87V"]
        assert len(rows) == 2
        assert {d["pm_number"] for d in rows} == {"PM-101", "PM-102"}
        twin_row = next(d for d in rows if d["pm_number"] == "PM-102")
        assert twin_row["has_tag"] is False

        kiosk = client.get("/api/devices").json()
        assert {d["pm_number"] for d in kiosk} == {"PM-101"}

    def test_slot_accuracy_counts_untagged_rows(
        self, client, mock_context, admin_user, test_devices, db_session
    ):
        """Slot occupancy from the admin list includes untagged rows."""
        DeviceRepository.create(
            db_session,
            name="Ghost",
            device_type="general",
            pm_number="PM-999",
            locker_slot=9,
        )
        db_session.commit()
        mock_context.session_mgr.start_session(admin_user)

        admin_slots = {d["locker_slot"] for d in client.get("/api/admin/devices").json()}
        assert admin_slots == {1, 2, 3, 9}
        kiosk_slots = {d["locker_slot"] for d in client.get("/api/devices").json()}
        assert kiosk_slots == {1, 2, 3}


class TestRegisterDuplicatePmApi:
    """POST /api/admin/devices/register refuses an already-registered PM."""

    def test_duplicate_pm_is_409(
        self, client, mock_context, admin_user, test_devices, monkeypatch, tmp_path
    ):
        """Registering PM-001 twice is 409 ('already in the locker')."""
        wb = Workbook()
        ws = wb.active
        ws.append(["Equipment", "Name", "Manufacturer", "Model", "Location"])
        ws.append(["PM-001", "Camera", "Fluke", "87V", "Locker"])
        path = tmp_path / "device-list.xlsx"
        wb.save(path)
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(
            "/api/admin/devices/register",
            json={"pm_number": "PM-001", "locker_slot": 9},
        )
        assert resp.status_code == 409
        assert "already in the locker" in resp.json()["detail"]


class TestRegisterNfcRaceRollback:
    """Register is all-or-nothing when a rival NFC window arms mid-request."""

    def test_arm_conflict_after_insert_rolls_back_row(
        self, client, mock_context, admin_user, db_session, monkeypatch, tmp_path
    ):
        """409 from the late arm leaves no row and no write-back; retry works."""
        wb = Workbook()
        ws = wb.active
        ws.append(["Equipment", "Name", "Manufacturer", "Model", "Location"])
        ws.append(["PM-100", "Zeta Scope", "Keysight", "DSOX", "Locker"])
        path = tmp_path / "device-list.xlsx"
        wb.save(path)
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))
        writebacks = []
        monkeypatch.setattr(
            "smart_locker.sync.location_writeback.schedule_write_location",
            lambda: writebacks.append(1),
        )
        mock_context.session_mgr.start_session(admin_user)
        mock_context.pending_tag_bind = None
        mock_context.pending_registration = None

        real_arm = routes_module._arm_tag_bind
        raced = []

        def racing_arm(bind):
            # Simulate a rival window arming after this request's early
            # conflict check but before its late arm (the Excel+insert gap).
            if not raced:
                raced.append(1)
                mock_context.pending_registration = PendingRegistration("Rival")
            return real_arm(bind)

        monkeypatch.setattr("smart_locker.api.routes._arm_tag_bind", racing_arm)

        resp = client.post(
            "/api/admin/devices/register",
            json={"pm_number": "PM-100", "locker_slot": 5},
        )
        assert resp.status_code == 409
        assert DeviceRepository.find_by_pm(db_session, "PM-100") is None
        assert writebacks == []

        # Slot is free and the PM retries clean once the rival clears.
        mock_context.pending_registration = None
        retry = client.post(
            "/api/admin/devices/register",
            json={"pm_number": "PM-100", "locker_slot": 5},
        )
        assert retry.status_code == 200
        assert DeviceRepository.find_by_pm(db_session, "PM-100") is not None
        assert writebacks == [1]


class TestMaintenanceApi:
    """Admin maintenance toggle on locker devices (Register Device list)."""

    def test_maintenance_requires_session(self, client, mock_context, test_devices):
        """POST /api/admin/devices/{id}/maintenance returns 401 without a session."""
        resp = client.post(
            f"/api/admin/devices/{test_devices[0].id}/maintenance",
            json={"maintenance": True},
        )
        assert resp.status_code == 401

    def test_maintenance_rejects_non_admin(
        self, client, mock_context, test_user, test_devices
    ):
        """A normal user session cannot toggle maintenance."""
        mock_context.session_mgr.start_session(test_user)
        resp = client.post(
            f"/api/admin/devices/{test_devices[0].id}/maintenance",
            json={"maintenance": True},
        )
        assert resp.status_code == 403

    def test_maintenance_on_available(
        self, client, mock_context, admin_user, test_devices, db_session, monkeypatch
    ):
        """Available device → maintenance, 200, write-back scheduled."""
        calls = []
        monkeypatch.setattr(
            "smart_locker.sync.location_writeback.schedule_write_location",
            lambda: calls.append(1),
        )
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(
            f"/api/admin/devices/{test_devices[0].id}/maintenance",
            json={"maintenance": True},
        )
        assert resp.status_code == 200
        assert resp.json() == {"success": True, "status": "maintenance"}
        db_session.refresh(test_devices[0])
        assert test_devices[0].status == DeviceStatus.MAINTENANCE
        assert calls == [1]

    def test_maintenance_on_borrowed_refused(
        self, client, mock_context, admin_user, test_user, db_session
    ):
        """A borrowed device must be returned first."""
        dev = DeviceRepository.create(
            db_session,
            name="Meter",
            device_type="general",
            pm_number="PM-900",
            locker_slot=9,
        )
        dev.status = DeviceStatus.BORROWED
        dev.current_borrower_id = test_user.id
        db_session.commit()
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(
            f"/api/admin/devices/{dev.id}/maintenance",
            json={"maintenance": True},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"] == "Return the device first."

    def test_maintenance_twice_refused(
        self, client, mock_context, admin_user, test_devices
    ):
        """A device already in maintenance returns 409."""
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(
            f"/api/admin/devices/{test_devices[2].id}/maintenance",
            json={"maintenance": True},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"] == "Already in maintenance."

    def test_back_in_service(
        self, client, mock_context, admin_user, test_devices, db_session, monkeypatch
    ):
        """maintenance=false on a maintenance device → available, 200."""
        calls = []
        monkeypatch.setattr(
            "smart_locker.sync.location_writeback.schedule_write_location",
            lambda: calls.append(1),
        )
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(
            f"/api/admin/devices/{test_devices[2].id}/maintenance",
            json={"maintenance": False},
        )
        assert resp.status_code == 200
        assert resp.json() == {"success": True, "status": "available"}
        db_session.refresh(test_devices[2])
        assert test_devices[2].status == DeviceStatus.AVAILABLE
        assert calls == [1]

    def test_back_in_service_on_available_refused(
        self, client, mock_context, admin_user, test_devices
    ):
        """maintenance=false on a non-maintenance device returns 409."""
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(
            f"/api/admin/devices/{test_devices[0].id}/maintenance",
            json={"maintenance": False},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"] == "Device is not in maintenance."

    def test_maintenance_unknown_device(
        self, client, mock_context, admin_user
    ):
        """Unknown device id returns 404."""
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(
            "/api/admin/devices/99999/maintenance",
            json={"maintenance": True},
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Device not found."


class TestAdminUsers:
    """Admin Users overlay API: list, deactivate, replace card."""

    def test_admin_register_role_admin_arms_pending_with_role(
        self, client, mock_context, admin_user
    ):
        """POST /api/admin/register carries role; default is user; bad role 422."""
        mock_context.session_mgr.start_session(admin_user)
        mock_context.pending_registration = None
        mock_context.pending_tag_bind = None
        resp = client.post(
            "/api/admin/register", json={"name": "New Admin", "role": "admin"}
        )
        assert resp.status_code == 200
        assert mock_context.pending_registration.role == "admin"

        mock_context.pending_registration = None
        resp = client.post("/api/admin/register", json={"name": "Plain User"})
        assert resp.status_code == 200
        assert mock_context.pending_registration.role == "user"

        resp = client.post(
            "/api/admin/register", json={"name": "Root", "role": "root"}
        )
        assert resp.status_code == 422

    def test_list_users_active_only_with_borrowed_count(
        self, client, mock_context, admin_user, test_user, test_devices, db_session
    ):
        """GET /api/admin/users lists active users with held-device counts."""
        gone = UserRepository.create(
            db_session,
            display_name="Gone User",
            uid_hmac="gonehmac",
            encrypted_card_uid="gone",
        )
        gone.is_active = False
        DeviceRepository.borrow(db_session, test_devices[0], test_user.id)
        db_session.commit()
        mock_context.session_mgr.start_session(admin_user)
        resp = client.get("/api/admin/users")
        assert resp.status_code == 200
        users = resp.json()["users"]
        names = {u["name"] for u in users}
        assert "Gone User" not in names
        assert "Test User" in names
        assert "Admin User" in names
        row = next(u for u in users if u["name"] == "Test User")
        assert set(row) == {"id", "name", "role", "borrowed_count"}
        assert row["borrowed_count"] == 1
        assert row["role"] == "user"

    def test_list_users_requires_admin(
        self, client, lan_client, mock_context, admin_user, test_user
    ):
        """Non-admin session is 403; a LAN client is 403 even with a session."""
        mock_context.session_mgr.start_session(test_user)
        assert client.get("/api/admin/users").status_code == 403
        mock_context.session_mgr.start_session(admin_user)
        assert lan_client.get("/api/admin/users").status_code == 403

    def test_deactivate_user(
        self, client, mock_context, admin_user, test_user, db_session
    ):
        """Deactivate hides the user from the list and keeps the session up."""
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(f"/api/admin/users/{test_user.id}/deactivate")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["session_ended"] is False
        db_session.expire_all()
        assert UserRepository.find_by_id(db_session, test_user.id).is_active is False
        users = client.get("/api/admin/users").json()["users"]
        assert "Test User" not in {u["name"] for u in users}
        assert mock_context.session_mgr.has_active_session

    def test_deactivate_holds_user_admin_lock(
        self, client, mock_context, admin_user, test_user, monkeypatch
    ):
        """Guards and commit run under the process-level user-admin lock."""
        import smart_locker.api.routes as routes

        entered = []

        class FakeLock:
            def __enter__(self):
                entered.append(True)
                return self

            def __exit__(self, *args):
                return False

        monkeypatch.setattr(routes, "user_admin_lock", FakeLock())
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(f"/api/admin/users/{test_user.id}/deactivate")
        assert resp.status_code == 200
        assert entered == [True]

    def test_concurrent_deactivate_last_admin_pair(
        self, _db_setup, mock_context, db_session
    ):
        """Racing deactivations of the last two admins: exactly one wins."""
        import threading

        import smart_locker.api.routes as routes

        admins = [
            UserRepository.create(
                db_session,
                display_name=f"Admin{i}",
                uid_hmac=f"admin-hmac-{i}",
                encrypted_card_uid=f"encrypted-{i}",
                role="admin",
            )
            for i in range(2)
        ]
        db_session.commit()
        user_session = mock_context.session_mgr.start_session(admins[0])
        barrier = threading.Barrier(2)
        outcomes = []
        outcomes_lock = threading.Lock()

        def deactivate(user_id):
            session = _db_setup()
            try:
                barrier.wait()
                try:
                    routes.admin_deactivate_user(user_id, session, user_session)
                except Exception as exc:  # noqa: BLE001 - record the HTTP status
                    with outcomes_lock:
                        outcomes.append(getattr(exc, "status_code", None))
                else:
                    with outcomes_lock:
                        outcomes.append(200)
            finally:
                _db_setup.remove()

        threads = [
            threading.Thread(target=deactivate, args=(admins[i].id,))
            for i in range(2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sorted(outcomes) == [200, 409]
        db_session.expire_all()
        assert UserRepository.count_active_admins(db_session) == 1

    def test_concurrent_double_deactivate_one_user(
        self, _db_setup, mock_context, admin_user, test_user
    ):
        """Racing deactivations of one user: one wins, the other sees 404."""
        import threading

        import smart_locker.api.routes as routes

        user_session = mock_context.session_mgr.start_session(admin_user)
        barrier = threading.Barrier(2)
        outcomes = []
        outcomes_lock = threading.Lock()

        def deactivate():
            session = _db_setup()
            try:
                barrier.wait()
                try:
                    routes.admin_deactivate_user(test_user.id, session, user_session)
                except Exception as exc:  # noqa: BLE001 - record the HTTP status
                    with outcomes_lock:
                        outcomes.append(getattr(exc, "status_code", None))
                else:
                    with outcomes_lock:
                        outcomes.append(200)
            finally:
                _db_setup.remove()

        threads = [threading.Thread(target=deactivate) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sorted(outcomes) == [200, 404]

    def test_deactivate_last_admin_refused(
        self, client, mock_context, admin_user
    ):
        """The only active admin cannot deactivate themselves."""
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(f"/api/admin/users/{admin_user.id}/deactivate")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "Register another admin first."

    def test_deactivate_user_holding_device_refused(
        self, client, mock_context, admin_user, test_user, test_devices, db_session
    ):
        """A user still holding a device cannot be deactivated."""
        DeviceRepository.borrow(db_session, test_devices[0], test_user.id)
        db_session.commit()
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(f"/api/admin/users/{test_user.id}/deactivate")
        assert resp.status_code == 409
        assert resp.json()["detail"] == (
            "Return or hand over this user's devices first."
        )
        db_session.expire_all()
        assert UserRepository.find_by_id(db_session, test_user.id).is_active is True

    def test_deactivate_self_when_other_admin_exists_ends_session(
        self, client, mock_context, admin_user, db_session, session_mgr
    ):
        """Deactivating the panel's own admin ends the kiosk session."""
        UserRepository.create(
            db_session,
            display_name="Second Admin",
            uid_hmac="admin2hmac",
            encrypted_card_uid="admin2",
            role="admin",
        )
        db_session.commit()
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(f"/api/admin/users/{admin_user.id}/deactivate")
        assert resp.status_code == 200
        assert resp.json()["session_ended"] is True
        assert session_mgr.has_active_session is False

    def test_deactivate_unknown_or_inactive_is_404(
        self, client, mock_context, admin_user, db_session
    ):
        """Missing users and already-inactive users are both 404."""
        inactive = UserRepository.create(
            db_session,
            display_name="Inactive User",
            uid_hmac="inactivehmac",
            encrypted_card_uid="inactive",
        )
        inactive.is_active = False
        db_session.commit()
        mock_context.session_mgr.start_session(admin_user)
        missing = client.post("/api/admin/users/99999/deactivate")
        assert missing.status_code == 404
        assert missing.json()["detail"] == "User not found."
        gone = client.post(f"/api/admin/users/{inactive.id}/deactivate")
        assert gone.status_code == 404
        assert gone.json()["detail"] == "User not found."

    def test_replace_card_arms_pending(
        self, client, mock_context, admin_user, test_user
    ):
        """Replace card arms pending_registration with replace_user_id."""
        mock_context.session_mgr.start_session(admin_user)
        mock_context.pending_registration = None
        mock_context.pending_tag_bind = None
        resp = client.post(f"/api/admin/users/{test_user.id}/replace-card")
        assert resp.status_code == 200
        pending = mock_context.pending_registration
        assert pending is not None
        assert pending.replace_user_id == test_user.id
        assert pending.display_name == test_user.display_name
        assert pending.role == test_user.role.value
        # A second arm while the window is open is refused.
        resp2 = client.post(f"/api/admin/users/{test_user.id}/replace-card")
        assert resp2.status_code == 409
        assert "waiting" in resp2.json()["detail"]

    def test_replace_card_unknown_user_404(self, client, mock_context, admin_user):
        """Replace card on a missing user is 404."""
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post("/api/admin/users/99999/replace-card")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "User not found."

    def test_replace_card_requires_admin_403(
        self, client, mock_context, test_user
    ):
        """Replace card under a non-admin session is 403."""
        mock_context.session_mgr.start_session(test_user)
        resp = client.post(f"/api/admin/users/{test_user.id}/replace-card")
        assert resp.status_code == 403

    def test_deactivate_requires_session(self, client, mock_context, test_user):
        """POST .../deactivate returns 401 without a session."""
        resp = client.post(f"/api/admin/users/{test_user.id}/deactivate")
        assert resp.status_code == 401

    def test_deactivate_rejects_non_admin(self, client, mock_context, test_user):
        """POST .../deactivate returns 403 for a normal user session."""
        mock_context.session_mgr.start_session(test_user)
        resp = client.post(f"/api/admin/users/{test_user.id}/deactivate")
        assert resp.status_code == 403

    def test_deactivate_refuses_lan_with_admin_session(
        self, lan_client, mock_context, admin_user, test_user
    ):
        """LAN cannot deactivate even with a live admin session."""
        mock_context.session_mgr.start_session(admin_user)
        resp = lan_client.post(f"/api/admin/users/{test_user.id}/deactivate")
        assert resp.status_code == 403

    def test_replace_card_requires_session(self, client, mock_context, test_user):
        """POST .../replace-card returns 401 without a session."""
        resp = client.post(f"/api/admin/users/{test_user.id}/replace-card")
        assert resp.status_code == 401

    def test_replace_card_refuses_lan_with_admin_session(
        self, lan_client, mock_context, admin_user, test_user
    ):
        """LAN cannot arm a card replace even with a live admin session."""
        mock_context.session_mgr.start_session(admin_user)
        resp = lan_client.post(f"/api/admin/users/{test_user.id}/replace-card")
        assert resp.status_code == 403

    def test_list_users_requires_session(self, client, mock_context):
        """GET /api/admin/users returns 401 without a session."""
        resp = client.get("/api/admin/users")
        assert resp.status_code == 401

    @pytest.mark.parametrize("role", [None, "Admin", ""])
    def test_admin_register_role_variants_are_422(
        self, client, mock_context, admin_user, role
    ):
        """role null / 'Admin' / '' are 422 and arm no pending registration."""
        mock_context.session_mgr.start_session(admin_user)
        mock_context.pending_registration = None
        mock_context.pending_tag_bind = None
        resp = client.post(
            "/api/admin/register", json={"name": "Someone", "role": role}
        )
        assert resp.status_code == 422
        assert mock_context.pending_registration is None

    def test_admin_register_extra_fields_ignored(
        self, client, mock_context, admin_user
    ):
        """Unknown body fields are ignored (pydantic default, codebase-wide):
        200 with role applied and the extra key dropped."""
        mock_context.session_mgr.start_session(admin_user)
        mock_context.pending_registration = None
        mock_context.pending_tag_bind = None
        resp = client.post(
            "/api/admin/register",
            json={"name": "Someone", "role": "admin", "nickname": "X"},
        )
        assert resp.status_code == 200
        pending = mock_context.pending_registration
        assert pending is not None
        assert pending.display_name == "Someone"
        assert pending.role == "admin"

    def test_admin_call_after_self_deactivate_is_401(
        self, client, mock_context, admin_user, db_session
    ):
        """After self-deactivate the old session is gone: an admin GET is 401,
        SSE carries session_ended/deactivated, and the overlay is cleared."""
        UserRepository.create(
            db_session,
            display_name="Second Admin",
            uid_hmac="admin2hmac",
            encrypted_card_uid="admin2",
            role="admin",
        )
        db_session.commit()
        mock_context.session_mgr.start_session(admin_user)
        mock_context.admin_overlay_open = True
        resp = client.post(f"/api/admin/users/{admin_user.id}/deactivate")
        assert resp.status_code == 200
        assert resp.json()["session_ended"] is True
        assert mock_context.admin_overlay_open is False
        sse = mock_context.sse_queue.get_nowait()
        assert sse == {"event": "session_ended", "reason": "deactivated"}
        assert client.get("/api/admin/users").status_code == 401

    def test_deactivated_card_fails_through_tap_router(
        self, client, mock_context, admin_user, db_session, enc_key, hmac_key
    ):
        """Endpoint-deactivate, then an idle tap of that card goes
        classify_uid -> WORK_CARD, idle handler -> auth_failed, no session."""
        from smart_locker.auth.tap_router import TapKind, classify_uid, handle_insert
        from smart_locker.security.encryption import encrypt

        uid = "D3ADB33F"
        user = UserRepository.create(
            db_session,
            display_name="Departed",
            uid_hmac=compute_uid_hmac(uid, hmac_key),
            encrypted_card_uid=encrypt(uid, enc_key),
        )
        db_session.commit()
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(f"/api/admin/users/{user.id}/deactivate")
        assert resp.status_code == 200
        mock_context.session_mgr.end_session()
        db_session.expire_all()  # route commits in its own session

        mgr = SessionManager(timeout_seconds=60)
        kind, found, device = classify_uid(db_session, uid, hmac_key)
        assert kind is TapKind.WORK_CARD
        assert found is not None
        assert found.id == user.id
        assert found.is_active is False
        assert device is None
        result = handle_insert(db_session, uid, hmac_key, mgr)
        assert result.event == "auth_failed"
        assert not mgr.has_active_session
        assert uid not in result.cli_message

    def test_history_readable_after_deactivate(
        self, client, mock_context, admin_user, test_user, test_devices, db_session
    ):
        """Deactivation keeps history: borrow/return rows stay readable and
        attributable to the deactivated user."""
        from smart_locker.database.repositories import TransactionRepository

        DeviceRepository.borrow(db_session, test_devices[0], test_user.id)
        TransactionRepository.log_borrow(
            db_session, test_user.id, test_devices[0].id
        )
        DeviceRepository.return_device(db_session, test_devices[0])
        TransactionRepository.log_return(
            db_session, test_user.id, test_devices[0].id
        )
        db_session.commit()
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(f"/api/admin/users/{test_user.id}/deactivate")
        assert resp.status_code == 200
        db_session.expire_all()  # route commits in its own session

        history = TransactionRepository.get_user_history(db_session, test_user.id)
        assert len(history) == 2
        assert {t.user_id for t in history} == {test_user.id}
        kept = UserRepository.find_by_id(db_session, test_user.id)
        assert kept is not None
        assert kept.display_name == "Test User"
        assert kept.is_active is False
