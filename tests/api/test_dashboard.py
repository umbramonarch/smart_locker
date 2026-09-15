"""
File: test_dashboard.py
Description: Tests for public Inventory/Locker/Display GETs, public
             owner edit, and dashboard NFC bind/unbind.
Project: smart_locker/tests/api
Notes: Run with: python -m pytest tests/api/test_dashboard.py -v
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
from tests.api.helpers import catalog_workbook, dashboard_admin_headers

class TestDashboardInventoryAndDisplay:
    """Inventory is Excel; Locker is SQLite; Display is a kiosk snapshot."""

    def test_inventory_is_excel_not_sqlite(
        self, client, test_devices, tmp_path, monkeypatch
    ):
        """Inventory includes Excel-only PMs that are not locker rows."""
        path = catalog_workbook(tmp_path, [
            ["PM", "Name", "Manufacturer", "Model", "Serial", "Location", "Calibration due"],
            ["PM-001", "Scope", "Keysight", "DSOX", "SN-1", "Locker", "2026-01-01"],
            ["PM-999", "Van kit", "Fluke", "87V", "SN-9", "Workshop", None],
        ])
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))
        resp = client.get("/api/dashboard/inventory")
        assert resp.status_code == 200
        rows = resp.json()
        pms = {r["pm_number"] for r in rows}
        assert "PM-001" in pms
        assert "PM-999" in pms
        van = next(r for r in rows if r["pm_number"] == "PM-999")
        assert van["name"] == "Van kit"
        assert van["location"] == "Workshop"
        assert van["in_locker"] is False
        locker = next(r for r in rows if r["pm_number"] == "PM-001")
        assert locker["in_locker"] is True
        assert "status" not in van
        assert "locker_slot" not in van
        assert "tag_hmac" not in van

    def test_locker_stays_sqlite_only(
        self, client, test_devices, tmp_path, monkeypatch
    ):
        """Locker tab JSON is SQLite devices; Excel-only PMs are absent."""
        path = catalog_workbook(tmp_path, [
            ["PM", "Name", "Location"],
            ["PM-001", "Scope", "Locker"],
            ["PM-999", "Van kit", "Workshop"],
        ])
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))
        resp = client.get("/api/dashboard/devices")
        assert resp.status_code == 200
        pms = {r["pm_number"] for r in resp.json()}
        assert "PM-001" in pms
        assert "PM-999" not in pms
        row = next(r for r in resp.json() if r["pm_number"] == "PM-001")
        assert "locker_slot" in row
        assert "status" in row

    def test_inventory_share_down_does_not_break_locker(
        self, client, test_devices, tmp_path, monkeypatch
    ):
        """Missing catalog → Inventory errors; Locker still answers."""
        monkeypatch.setattr(
            "config.settings.SOURCE_EXCEL_PATH", str(tmp_path / "missing.xlsx")
        )
        inv = client.get("/api/dashboard/inventory")
        assert inv.status_code == 503
        locker = client.get("/api/dashboard/devices")
        assert locker.status_code == 200
        assert locker.json()

    def test_inventory_unconfigured_is_error(self, client, monkeypatch):
        """Empty SOURCE_EXCEL_PATH is an Inventory error, not an empty catalog."""
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", "")
        resp = client.get("/api/dashboard/inventory")
        assert resp.status_code == 503

    def test_display_idle_has_no_user(self, client, mock_context):
        """Idle kiosk reports Idle occupancy without a person name."""
        mock_context.kiosk_screen = "idle"
        mock_context.admin_overlay_open = False
        resp = client.get("/api/dashboard/display")
        assert resp.status_code == 200
        data = resp.json()
        assert data["screen"] == "idle"
        assert data["label"] == "Idle"
        assert data["occupied"] is False
        assert "user_name" not in data

    def test_display_shows_occupied_not_user_name(
        self, client, mock_context, test_user
    ):
        """Logged-in kiosk reports occupancy, not the borrower/user name."""
        mock_context.session_mgr.start_session(test_user)
        mock_context.kiosk_screen = "main-menu"
        resp = client.get("/api/dashboard/display")
        assert resp.status_code == 200
        data = resp.json()
        assert data["screen"] == "main-menu"
        assert data["label"] == "Main menu"
        assert data["occupied"] is True
        assert "user_name" not in data
        assert "Test User" not in resp.text

    def test_display_from_lan_has_no_person_name(
        self, lan_client, mock_context, test_user
    ):
        """Public Display from LAN stays occupancy-only even with a live session."""
        mock_context.session_mgr.start_session(test_user)
        resp = lan_client.get("/api/dashboard/display")
        assert resp.status_code == 200
        data = resp.json()
        assert data["occupied"] is True
        assert "user_name" not in data

    def test_heartbeat_updates_display(self, client, mock_context):
        """Kiosk POST of a screen id is what Display polls."""
        resp = client.post("/api/kiosk/display", json={"screen": "borrow"})
        assert resp.status_code == 200
        assert mock_context.kiosk_screen == "borrow"
        shown = client.get("/api/dashboard/display").json()
        assert shown["screen"] == "borrow"
        assert shown["label"] == "Locker"

    def test_display_admin_overlay_overrides_heartbeat(self, client, mock_context):
        """Open admin overlay is Admin even if the last heartbeat was idle."""
        mock_context.kiosk_screen = "idle"
        mock_context.admin_overlay_open = True
        data = client.get("/api/dashboard/display").json()
        assert data["screen"] == "admin"
        assert data["label"] == "Admin"

    def test_heartbeat_from_lan_is_403(self, lan_client, mock_context):
        """Display heartbeat is kiosk-loopback only."""
        mock_context.kiosk_screen = "idle"
        resp = lan_client.post("/api/kiosk/display", json={"screen": "borrow"})
        assert resp.status_code == 403
        assert mock_context.kiosk_screen == "idle"

    def test_heartbeat_null_context_is_ok(self, client, monkeypatch):
        """Missing AppContext on POST is 200, not 500."""
        monkeypatch.setattr(ctx_module, "context", None)
        resp = client.post("/api/kiosk/display", json={"screen": "idle"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


class TestDashboardOwnerEditApi:
    """POST /api/dashboard/owner is public; no secret needed."""

    def test_owner_change_without_secret_is_200(
        self, client, tmp_path, monkeypatch
    ):
        """Unauthenticated owner write is 200 and updates the Excel cell."""
        from openpyxl import load_workbook

        path = catalog_workbook(tmp_path, [
            ["Equipment", "Name", "Location"],
            ["PM-VAN", "Van kit", "Workshop"],
        ])
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))
        resp = client.post(
            "/api/dashboard/owner",
            json={"pm_number": "PM-VAN", "owner": "Alex"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["locker"] is False
        ws = load_workbook(path).active
        location_col = next(
            c.column for c in ws[1] if c.value == "Location"
        )
        assert ws.cell(row=2, column=location_col).value == "Alex"

    def test_owner_change_works_when_secret_unset(
        self, client, tmp_path, monkeypatch
    ):
        """Owner write succeeds with the admin secret unset (public route)."""
        monkeypatch.delenv("SMART_LOCKER_DASHBOARD_ADMIN_SECRET", raising=False)
        path = catalog_workbook(tmp_path, [
            ["Equipment", "Name", "Location"],
            ["PM-VAN", "Van kit", "Workshop"],
        ])
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))
        resp = client.post(
            "/api/dashboard/owner",
            json={"pm_number": "PM-VAN", "owner": "Alex"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_owner_change_with_secret(
        self, client, tmp_path, monkeypatch, dashboard_secret
    ):
        """Authorized owner write still does not need a kiosk work-card session."""
        path = catalog_workbook(tmp_path, [
            ["Equipment", "Name", "Location"],
            ["PM-VAN", "Van kit", "Workshop"],
        ])
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))
        resp = client.post(
            "/api/dashboard/owner",
            json={"pm_number": "PM-VAN", "owner": "Alex"},
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["locker"] is False

    def test_non_locker_does_not_create_sqlite_row(
        self, client, db_session, tmp_path, monkeypatch, dashboard_secret
    ):
        """Excel-only PMs stay off the Pi after an Inventory owner edit."""
        path = catalog_workbook(tmp_path, [
            ["Equipment", "Name", "Location"],
            ["PM-VAN", "Van kit", "Workshop"],
        ])
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))
        client.post(
            "/api/dashboard/owner",
            json={"pm_number": "PM-VAN", "owner": "Alex"},
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert DeviceRepository.find_by_pm(db_session, "PM-VAN") is None

    def test_locker_pm_is_refused(
        self, client, test_user, test_devices, tmp_path, monkeypatch, db_session,
        dashboard_secret,
    ):
        """Locker PMs cannot have owner changed from the dashboard."""
        from smart_locker.database.models import TransactionLog
        from sqlalchemy import select

        path = catalog_workbook(tmp_path, [
            ["Equipment", "Name", "Location"],
            ["PM-001", "Camera", "Locker"],
        ])
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))
        db_session.commit()
        resp = client.post(
            "/api/dashboard/owner",
            json={"pm_number": "PM-001", "owner": "Test User"},
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 409
        db_session.expire_all()
        device = DeviceRepository.find_by_pm(db_session, "PM-001")
        assert device.status == DeviceStatus.AVAILABLE
        assert device.current_borrower_id is None
        logs = db_session.execute(select(TransactionLog)).scalars().all()
        assert logs == []

    def test_owners_list_is_public(
        self, client, test_user, db_session
    ):
        """GET /api/dashboard/owners needs no secret; users are not listed."""
        RegistrantRepository.add_names(db_session, {"Bob Field"})
        db_session.commit()
        resp = client.get("/api/dashboard/owners")
        assert resp.status_code == 200
        names = resp.json()["names"]
        # "Test User" is a registered kiosk user, not an Excel registrant.
        assert "Test User" not in names
        assert "Bob Field" in names
        assert "in_locker_token" in resp.json()

    def test_users_and_transactions_require_secret(self, client, test_devices):
        """Users, transactions, and bind/unbind are not public."""
        assert client.get("/api/dashboard/users").status_code == 401
        assert client.get("/api/dashboard/transactions").status_code == 401
        assert client.post(
            "/api/dashboard/bind-tag", json={"pm_number": "PM-001"}
        ).status_code == 401
        assert client.post(
            "/api/dashboard/unbind-tag", json={"pm_number": "PM-001"}
        ).status_code == 401

    def test_users_and_transactions_with_secret(
        self, client, test_user, dashboard_secret
    ):
        """Authorized GET returns users without hmac/uid fields."""
        users = client.get(
            "/api/dashboard/users",
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert users.status_code == 200
        row = next(u for u in users.json() if u["display_name"] == "Test User")
        assert "uid_hmac" not in row
        tx = client.get(
            "/api/dashboard/transactions",
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert tx.status_code == 200
        assert isinstance(tx.json(), list)

    def test_transactions_eager_load_relationships(self):
        """Last-500 transactions must not lazy-load user/device/performed_by (I16)."""
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[2]
            / "smart_locker"
            / "api"
            / "routes.py"
        ).read_text(encoding="utf-8")
        assert "selectinload(TransactionLog.user)" in src
        assert "selectinload(TransactionLog.device)" in src
        assert "selectinload(TransactionLog.performed_by)" in src

    def test_share_down_is_error(self, client, monkeypatch, tmp_path, dashboard_secret):
        """Missing catalog Excel is 503; does not invent a locker row."""
        monkeypatch.setattr(
            "config.settings.SOURCE_EXCEL_PATH", str(tmp_path / "missing.xlsx")
        )
        resp = client.post(
            "/api/dashboard/owner",
            json={"pm_number": "PM-001", "owner": "Alex"},
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 503


class TestDashboardTagApi:
    """Dashboard bind/unbind require the admin secret; overlay is not auth."""

    def test_bind_tag_without_secret_is_401(self, client, mock_context, test_devices):
        """POST /api/dashboard/bind-tag is 401 when the secret is unset."""
        mock_context.pending_tag_bind = None
        resp = client.post(
            "/api/dashboard/bind-tag",
            json={"pm_number": "PM-001"},
        )
        assert resp.status_code == 401
        assert mock_context.pending_tag_bind is None

    def test_unbind_tag_without_secret_is_401(
        self, client, test_devices, db_session, hmac_key
    ):
        """POST /api/dashboard/unbind-tag is 401 when the secret is unset."""
        DeviceRepository.bind_tag(
            db_session,
            test_devices[0],
            compute_uid_hmac("AABBCCDD", hmac_key),
        )
        db_session.commit()
        resp = client.post(
            "/api/dashboard/unbind-tag",
            json={"pm_number": "PM-001"},
        )
        assert resp.status_code == 401
        db_session.expire_all()
        assert test_devices[0].tag_hmac is not None

    def test_bind_tag_wrong_secret_is_401(
        self, client, mock_context, test_devices, dashboard_secret
    ):
        """Wrong header value is 401, not a successful arm."""
        mock_context.pending_tag_bind = None
        resp = client.post(
            "/api/dashboard/bind-tag",
            json={"pm_number": "PM-001"},
            headers=dashboard_admin_headers("wrong-secret-value"),
        )
        assert resp.status_code == 401
        assert mock_context.pending_tag_bind is None

    def test_bind_tag_with_secret_arms(
        self, client, mock_context, test_devices, dashboard_secret
    ):
        """Configured secret arms pending_tag_bind without a kiosk session."""
        mock_context.pending_tag_bind = None
        resp = client.post(
            "/api/dashboard/bind-tag",
            json={"pm_number": "PM-001"},
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        assert resp.json().get("ok") is True
        assert mock_context.pending_tag_bind is not None
        assert mock_context.pending_tag_bind.device_id == test_devices[0].id
        assert mock_context.pending_tag_bind.is_expired is False
        assert mock_context.pending_tag_bind.from_dashboard is True

    def test_dashboard_bind_from_lan_is_secret_not_loopback(
        self, lan_client, mock_context, test_devices, dashboard_secret
    ):
        """Dashboard bind stays secret-gated for LAN staff, not loopback-only."""
        mock_context.pending_tag_bind = None
        resp = lan_client.post(
            "/api/dashboard/bind-tag",
            json={"pm_number": "PM-001"},
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        assert mock_context.pending_tag_bind is not None
        assert mock_context.pending_tag_bind.from_dashboard is True

    def test_unbind_tag_with_secret_clears_hmac(
        self, client, test_devices, db_session, hmac_key, dashboard_secret
    ):
        """Authorized unbind clears tag_hmac; has_tag becomes False."""
        DeviceRepository.bind_tag(
            db_session,
            test_devices[0],
            compute_uid_hmac("AABBCCDD", hmac_key),
        )
        db_session.commit()
        resp = client.post(
            "/api/dashboard/unbind-tag",
            json={"pm_number": "PM-001"},
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        assert resp.json().get("ok") is True
        db_session.expire_all()
        assert test_devices[0].tag_hmac is None
        listed = client.get("/api/dashboard/devices").json()
        cam = next(d for d in listed if d["name"] == "Camera")
        assert cam["has_tag"] is False
        assert "tag_hmac" not in cam

    def test_unbind_clears_pending_tag_bind(
        self, client, mock_context, test_devices, db_session, hmac_key, dashboard_secret
    ):
        """Dashboard unbind cancels an armed bind window."""
        DeviceRepository.bind_tag(
            db_session,
            test_devices[0],
            compute_uid_hmac("AABBCCDD", hmac_key),
        )
        db_session.commit()
        mock_context.pending_tag_bind = PendingTagBind(device_id=test_devices[0].id)
        resp = client.post(
            "/api/dashboard/unbind-tag",
            json={"pm_number": "PM-001"},
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        assert mock_context.pending_tag_bind is None

    def test_bind_refused_while_kiosk_session_active(
        self, client, mock_context, test_user, test_devices, dashboard_secret
    ):
        """Dashboard bind must not arm while someone is logged in at the kiosk."""
        mock_context.session_mgr.start_session(test_user)
        mock_context.pending_tag_bind = None
        resp = client.post(
            "/api/dashboard/bind-tag",
            json={"pm_number": "PM-001"},
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 409
        assert mock_context.pending_tag_bind is None

    def test_bind_does_not_drop_pending_registration(
        self, client, mock_context, test_devices, dashboard_secret
    ):
        """Dashboard bind must not clear an in-progress kiosk enroll."""
        mock_context.pending_registration = PendingRegistration("Someone")
        mock_context.pending_tag_bind = None
        resp = client.post(
            "/api/dashboard/bind-tag",
            json={"pm_number": "PM-001"},
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 409
        assert mock_context.pending_registration is not None
        assert mock_context.pending_registration.display_name == "Someone"
        assert mock_context.pending_tag_bind is None

    def test_second_bind_rejected_while_armed(
        self, client, mock_context, test_devices, dashboard_secret
    ):
        """Last-writer-wins is refused: a non-expired bind blocks a new arm."""
        mock_context.pending_tag_bind = PendingTagBind(device_id=test_devices[1].id)
        resp = client.post(
            "/api/dashboard/bind-tag",
            json={"pm_number": "PM-001"},
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 409
        assert mock_context.pending_tag_bind.device_id == test_devices[1].id

    def test_unknown_pm_is_404(
        self, client, mock_context, dashboard_secret
    ):
        """Bind/unbind of a PM that is not a locker device is 404."""
        resp = client.post(
            "/api/dashboard/bind-tag",
            json={"pm_number": "PM-MISSING"},
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 404
        resp = client.post(
            "/api/dashboard/unbind-tag",
            json={"pm_number": "PM-MISSING"},
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 404
        assert mock_context.pending_tag_bind is None

