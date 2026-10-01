"""
File: test_register.py
Description: Tests for self-registration, admin register, Register Device,
             and loopback gating of POST /api/register and /cancel.
Project: smart_locker/tests/api
Notes: Run with: python -m pytest tests/api/test_register.py -v
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
from tests.api.helpers import catalog_workbook

class TestRegistrantEndpoints:
    """Tests for the registrant list and registration validation API endpoints."""

    def test_get_registrants_empty(self, client):
        """GET /api/registrants returns empty list when no registrants exist."""
        resp = client.get("/api/registrants")
        assert resp.status_code == 200
        assert resp.json()["names"] == []

    def test_get_registrants_returns_names(self, client, db_session):
        """GET /api/registrants returns names from the registrants table."""
        RegistrantRepository.add_names(db_session, {"Alice", "Bob"})
        db_session.commit()

        resp = client.get("/api/registrants")
        assert resp.status_code == 200
        names = resp.json()["names"]
        assert "Alice" in names
        assert "Bob" in names

    def test_get_registrants_excludes_registered_users(self, client, db_session):
        """GET /api/registrants excludes names that already have a User record."""
        RegistrantRepository.add_names(db_session, {"Alice", "Bob"})
        # "Alice" is already registered as a user
        UserRepository.create(
            db_session,
            display_name="Alice",
            uid_hmac="mmhash" * 10 + "mmmm",
            encrypted_card_uid="encrypted_mm",
        )
        db_session.commit()

        resp = client.get("/api/registrants")
        names = resp.json()["names"]
        # Alice should be excluded, Bob should remain
        assert "Alice" not in names
        assert "Bob" in names

    def test_register_validates_against_registrants(self, client, db_session, mock_context):
        """POST /api/register rejects names not in the registrants list."""
        # No registrants exist — any name should be rejected
        mock_context.pending_registration = None
        resp = client.post("/api/register", json={"name": "Unknown Person"})
        assert resp.status_code == 403
        assert "approved list" in resp.json()["detail"].lower()

    def test_register_accepts_approved_name(self, client, db_session, mock_context):
        """POST /api/register accepts a name that exists in the registrants list."""
        RegistrantRepository.add_names(db_session, {"Alice"})
        db_session.commit()
        mock_context.pending_registration = None

        resp = client.post("/api/register", json={"name": "Alice"})
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_admin_register_accepts_any_name(
        self, client, db_session, mock_context, admin_user
    ):
        """POST /api/admin/register accepts any name without registrant validation."""
        mock_context.session_mgr.start_session(admin_user)
        mock_context.pending_registration = None
        mock_context.pending_tag_bind = None

        resp = client.post("/api/admin/register", json={"name": "Not In List"})
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert mock_context.pending_registration is not None

    def test_admin_register_rejects_armed_bind(
        self, client, db_session, mock_context, admin_user
    ):
        """Admin register does not drop a non-expired device-tag bind window."""
        mock_context.session_mgr.start_session(admin_user)
        mock_context.pending_registration = None
        mock_context.pending_tag_bind = PendingTagBind(device_id=1)

        resp = client.post("/api/admin/register", json={"name": "Not In List"})
        assert resp.status_code == 409
        assert mock_context.pending_tag_bind is not None
        assert mock_context.pending_tag_bind.device_id == 1
        assert mock_context.pending_registration is None

    def test_admin_register_rejects_non_admin(
        self, client, db_session, mock_context, test_user
    ):
        """POST /api/admin/register rejects non-admin users."""
        mock_context.session_mgr.start_session(test_user)

        resp = client.post("/api/admin/register", json={"name": "Someone"})
        assert resp.status_code == 403

    def test_admin_register_requires_session(self, client, mock_context):
        """POST /api/admin/register requires an active session."""
        resp = client.post("/api/admin/register", json={"name": "Someone"})
        assert resp.status_code == 401

    def test_lan_cannot_start_registration(self, lan_client, db_session, mock_context):
        """LAN POST /api/register must not arm enrollment or clear a kiosk bind."""
        RegistrantRepository.add_names(db_session, {"Alice"})
        db_session.commit()
        mock_context.pending_registration = None
        mock_context.pending_tag_bind = PendingTagBind(device_id=7)
        resp = lan_client.post("/api/register", json={"name": "Alice"})
        assert resp.status_code == 403
        assert mock_context.pending_registration is None
        assert mock_context.pending_tag_bind is not None
        assert mock_context.pending_tag_bind.device_id == 7

    def test_lan_cannot_cancel_registration(self, lan_client, mock_context):
        """LAN cancel must not drop kiosk registration or a dashboard-armed bind."""
        mock_context.pending_registration = PendingRegistration(display_name="Alice")
        mock_context.pending_tag_bind = PendingTagBind(
            device_id=1, from_dashboard=True
        )
        resp = lan_client.post("/api/register/cancel")
        assert resp.status_code == 403
        assert mock_context.pending_registration is not None
        assert mock_context.pending_tag_bind is not None
        assert mock_context.pending_tag_bind.from_dashboard is True


class TestRegisterDeviceApi:
    """Admin POST /api/admin/devices/register creates a locker row from Excel."""

    def test_registerable_requires_session(self, client, mock_context):
        resp = client.get("/api/admin/devices/registerable")
        assert resp.status_code == 401

    def test_registerable_rejects_non_admin(
        self, client, mock_context, test_user
    ):
        mock_context.session_mgr.start_session(test_user)
        resp = client.get("/api/admin/devices/registerable")
        assert resp.status_code == 403

    def test_registerable_lists_only_unregistered_rows(
        self, client, mock_context, admin_user, test_devices, tmp_path,
        monkeypatch, db_session
    ):
        """Every Excel row with an id that is not in the locker is offered."""
        path = catalog_workbook(tmp_path, [
            ["Equipment", "Name", "Manufacturer", "Model"],
            ["PM-001", "Camera", "Fluke", "87V"],   # already a locker row
            ["PM-WAIT", "Bench Scope", "Keysight", "DSOX"],
            ["WG-77", "Solder Station", "Weller", "WX1010"],
        ])
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))
        db_session.commit()
        mock_context.session_mgr.start_session(admin_user)

        resp = client.get("/api/admin/devices/registerable")
        assert resp.status_code == 200
        rows = {d["pm_number"]: d for d in resp.json()}
        assert "PM-WAIT" in rows
        assert "WG-77" in rows          # any catalog id is offered, not PM-only
        assert "PM-001" not in rows     # registered rows are never offered
        assert rows["PM-WAIT"]["name"] == "Bench Scope"

    def test_registerable_excludes_case_variant_of_registered_pm(
        self, client, mock_context, admin_user, test_devices, tmp_path,
        monkeypatch, db_session
    ):
        """The picker uses the same case-insensitive PM match as registration."""
        path = catalog_workbook(tmp_path, [
            ["Equipment", "Name"],
            ["pm-001", "Camera case variant"],
            ["PM-WAIT", "Bench Scope"],
        ])
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))
        db_session.commit()
        mock_context.session_mgr.start_session(admin_user)

        rows = client.get("/api/admin/devices/registerable").json()
        assert {row["pm_number"] for row in rows} == {"PM-WAIT"}

    def test_registerable_share_down_is_503(
        self, client, mock_context, admin_user, tmp_path, monkeypatch
    ):
        """A missing workbook is 503, not an empty list."""
        monkeypatch.setattr(
            "config.settings.SOURCE_EXCEL_PATH", str(tmp_path / "missing.xlsx")
        )
        mock_context.session_mgr.start_session(admin_user)
        resp = client.get("/api/admin/devices/registerable")
        assert resp.status_code == 503

    def test_register_requires_session(self, client, mock_context):
        resp = client.post(
            "/api/admin/devices/register",
            json={"pm_number": "PM-001", "locker_slot": 1},
        )
        assert resp.status_code == 401

    def test_register_rejects_non_admin(self, client, mock_context, test_user):
        mock_context.session_mgr.start_session(test_user)
        resp = client.post(
            "/api/admin/devices/register",
            json={"pm_number": "PM-001", "locker_slot": 1},
        )
        assert resp.status_code == 403

    def test_register_accepts_admin(
        self, client, mock_context, admin_user, db_session, tmp_path, monkeypatch
    ):
        """Known PM + free slot inserts the locker row and arms the sticker bind."""
        path = catalog_workbook(tmp_path, [
            ["Equipment", "Manufacturer", "Model"],
            ["PM-XL", "Fluke", "87V"],
        ])
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))
        mock_context.session_mgr.start_session(admin_user)
        mock_context.pending_tag_bind = None
        scheduled = []
        monkeypatch.setattr(
            "smart_locker.sync.location_writeback.schedule_write_location",
            lambda: scheduled.append(True),
        )
        resp = client.post(
            "/api/admin/devices/register",
            json={"pm_number": "PM-XL", "locker_slot": 7},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["pm_number"] == "PM-XL"
        assert body["locker_slot"] == 7
        device = DeviceRepository.find_by_pm(db_session, "PM-XL")
        assert device is not None
        assert device.locker_slot == 7
        assert mock_context.pending_tag_bind.device_id == device.id
        assert scheduled == [True]

    def test_register_unknown_pm(
        self, client, mock_context, admin_user, db_session, tmp_path, monkeypatch
    ):
        path = catalog_workbook(tmp_path, [
            ["Equipment", "Manufacturer"],
            ["PM-001", "Fluke"],
        ])
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(
            "/api/admin/devices/register",
            json={"pm_number": "PM-MISSING", "locker_slot": 1},
        )
        assert resp.status_code == 404
        assert DeviceRepository.find_by_pm(db_session, "PM-MISSING") is None

    def test_register_shared_slot(
        self, client, mock_context, admin_user, test_devices, tmp_path, monkeypatch,
        db_session
    ):
        """A second PM registers into an occupied slot — slots are shared."""
        path = catalog_workbook(tmp_path, [
            ["Equipment", "Manufacturer"],
            ["PM-NEW", "Keysight"],
        ])
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(
            "/api/admin/devices/register",
            json={"pm_number": "PM-NEW", "locker_slot": test_devices[0].locker_slot},
        )
        assert resp.status_code == 200
        db_session.expire_all()
        device = DeviceRepository.find_by_pm(db_session, "PM-NEW")
        assert device is not None
        assert device.locker_slot == test_devices[0].locker_slot
        assert device.status == DeviceStatus.AVAILABLE
        # The bind window points at the new row, not the existing occupant.
        assert mock_context.pending_tag_bind.device_id == device.id
        occupants = DeviceRepository.find_all_by_slot(
            db_session, test_devices[0].locker_slot
        )
        assert {d.pm_number for d in occupants} == {"PM-001", "PM-NEW"}

    def test_register_duplicate_pm_stays_409(
        self, client, mock_context, admin_user, test_devices, tmp_path, monkeypatch,
        db_session
    ):
        """Duplicate PM is still refused; sharing a slot does not loosen PMs."""
        path = catalog_workbook(tmp_path, [
            ["Equipment", "Manufacturer"],
            ["PM-001", "Keysight"],
        ])
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(
            "/api/admin/devices/register",
            json={"pm_number": "PM-001", "locker_slot": 9},
        )
        assert resp.status_code == 409
        assert len(DeviceRepository.find_all_by_slot(db_session, 9)) == 0

    def test_set_slot_accepts_admin(
        self, client, mock_context, admin_user, test_devices, db_session
    ):
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(
            f"/api/admin/devices/{test_devices[0].id}/slot",
            json={"locker_slot": 9},
        )
        assert resp.status_code == 200
        db_session.expire_all()
        assert test_devices[0].locker_slot == 9

    def test_set_slot_to_occupied_slot(
        self, client, mock_context, admin_user, test_devices, db_session
    ):
        """Moving onto an occupied slot is allowed — both rows keep the slot."""
        mock_context.session_mgr.start_session(admin_user)
        target_slot = test_devices[1].locker_slot
        resp = client.post(
            f"/api/admin/devices/{test_devices[0].id}/slot",
            json={"locker_slot": target_slot},
        )
        assert resp.status_code == 200
        db_session.expire_all()
        occupants = DeviceRepository.find_all_by_slot(db_session, target_slot)
        assert {d.id for d in occupants} == {test_devices[0].id, test_devices[1].id}

    def test_set_slot_requires_session(self, client, mock_context, test_devices):
        resp = client.post(
            f"/api/admin/devices/{test_devices[0].id}/slot",
            json={"locker_slot": 9},
        )
        assert resp.status_code == 401

    def test_register_slot_over_cap_is_422(
        self, client, mock_context, admin_user
    ):
        """locker_slot=999999 is rejected before insert (I22)."""
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(
            "/api/admin/devices/register",
            json={"pm_number": "PM-001", "locker_slot": 999999},
        )
        assert resp.status_code == 422

    def test_set_slot_over_cap_is_422(
        self, client, mock_context, admin_user, test_devices
    ):
        """Change-slot also enforces MAX_LOCKER_SLOT."""
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(
            f"/api/admin/devices/{test_devices[0].id}/slot",
            json={"locker_slot": 999999},
        )
        assert resp.status_code == 422
