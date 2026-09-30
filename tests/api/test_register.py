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

    def test_register_while_sync_running_returns_503(
        self, client, mock_context
    ):
        """A name lookup during an in-flight mirror tick is 503, not 403 —
        'not yet adopted' is not 'not approved'."""
        from smart_locker.sync import mirror

        assert mirror._tick_lock.acquire(blocking=False)
        try:
            resp = client.post("/api/register", json={"name": "Not Yet Imported"})
            assert resp.status_code == 503
            assert "sync" in resp.json()["detail"].lower()
        finally:
            mirror._tick_lock.release()

        resp = client.post("/api/register", json={"name": "Not Listed"})
        assert resp.status_code == 403

    def test_get_registrants_reports_syncing(self, client):
        """GET /api/registrants exposes the in-flight tick as syncing=true."""
        from smart_locker.sync import mirror

        assert mirror._tick_lock.acquire(blocking=False)
        try:
            resp = client.get("/api/registrants")
            assert resp.status_code == 200
            assert resp.json()["syncing"] is True
        finally:
            mirror._tick_lock.release()

        resp = client.get("/api/registrants")
        assert resp.json()["syncing"] is False


class TestRegisterDeviceApi:
    """Admin POST /api/admin/devices/register promotes a catalog row."""

    def test_registerable_requires_session(self, client, mock_context):
        resp = client.get("/api/admin/devices/registerable")
        assert resp.status_code == 401

    def test_registerable_rejects_non_admin(
        self, client, mock_context, test_user
    ):
        mock_context.session_mgr.start_session(test_user)
        resp = client.get("/api/admin/devices/registerable")
        assert resp.status_code == 403

    def test_registerable_lists_only_waiting_rows(
        self, client, mock_context, admin_user, test_devices, db_session
    ):
        """In-locker place + no slot is offered; everything else is excluded."""
        waiting = DeviceRepository.create(
            db_session,
            name="Bench Scope",
            device_type="Tool",
            pm_number="PM-WAIT",
        )
        waiting.location = "locker"  # any capitalization is the place word
        shelf = DeviceRepository.create(
            db_session,
            name="Shelf Meter",
            device_type="Tool",
            pm_number="PM-SHELF",
        )
        shelf.location = "Shelf 3"
        db_session.commit()
        mock_context.session_mgr.start_session(admin_user)

        resp = client.get("/api/admin/devices/registerable")
        assert resp.status_code == 200
        rows = {d["pm_number"]: d for d in resp.json()}
        assert "PM-WAIT" in rows
        assert "PM-SHELF" not in rows
        # test_devices rows already hold a slot — not offered again.
        assert "PM-001" not in rows
        assert rows["PM-WAIT"]["name"] == "Bench Scope"

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

    def test_register_unknown_pm(
        self, client, mock_context, admin_user, db_session
    ):
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(
            "/api/admin/devices/register",
            json={"pm_number": "PM-MISSING", "locker_slot": 1},
        )
        assert resp.status_code == 404
        assert DeviceRepository.find_by_pm(db_session, "PM-MISSING") is None

    def test_register_promotes_catalog_row(
        self, client, mock_context, admin_user, db_session
    ):
        """A catalog-only row gains a slot and arms the sticker bind."""
        device = DeviceRepository.create(
            db_session,
            name="Van kit",
            device_type="Tool",
            pm_number="PM-NEW",
        )
        device.location = "locker"  # the in-locker word makes it registerable
        db_session.commit()
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(
            "/api/admin/devices/register",
            json={"pm_number": "PM-NEW", "locker_slot": 7},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["locker_slot"] == 7
        db_session.expire_all()
        assert device.locker_slot == 7
        # The sticker-bind window is armed on the never-tagged row.
        assert mock_context.pending_tag_bind is not None
        assert mock_context.pending_tag_bind.device_id == device.id
        listed = client.get("/api/dashboard/devices").json()
        assert "PM-NEW" in {d["pm_number"] for d in listed}

    def test_register_non_locker_place_is_409(
        self, client, mock_context, admin_user, db_session
    ):
        """A catalog row whose place is not the in-locker word is refused."""
        device = DeviceRepository.create(
            db_session,
            name="Shelf Meter",
            device_type="Tool",
            pm_number="PM-SHELF",
        )
        device.location = "Shelf 3"
        db_session.commit()
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(
            "/api/admin/devices/register",
            json={"pm_number": "PM-SHELF", "locker_slot": 7},
        )
        assert resp.status_code == 409
        db_session.expire_all()
        assert device.locker_slot is None

    def test_register_already_registered_is_409(
        self, client, mock_context, admin_user, test_devices, db_session
    ):
        """A PM that is already a cabinet unit cannot re-register."""
        db_session.commit()
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(
            "/api/admin/devices/register",
            json={"pm_number": test_devices[0].pm_number, "locker_slot": 9},
        )
        assert resp.status_code == 409

    def test_register_duplicate_slot(
        self, client, mock_context, admin_user, test_devices, db_session
    ):
        """Slots are shared labels — an occupied slot is still a valid pick."""
        device = DeviceRepository.create(
            db_session,
            name="Scope",
            device_type="Tool",
            pm_number="PM-NEW",
        )
        device.location = "locker"  # registerable rows carry the place word
        db_session.commit()
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(
            "/api/admin/devices/register",
            json={"pm_number": "PM-NEW", "locker_slot": test_devices[0].locker_slot},
        )
        assert resp.status_code == 200
        db_session.expire_all()
        assert device.locker_slot == test_devices[0].locker_slot

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

    def test_set_slot_requires_session(self, client, mock_context, test_devices):
        resp = client.post(
            f"/api/admin/devices/{test_devices[0].id}/slot",
            json={"locker_slot": 9},
        )
        assert resp.status_code == 401

    def test_set_slot_shared_slot_allowed(
        self, client, mock_context, admin_user, test_devices, db_session
    ):
        """Change-slot accepts a slot another unit already holds."""
        db_session.commit()
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(
            f"/api/admin/devices/{test_devices[0].id}/slot",
            json={"locker_slot": test_devices[1].locker_slot},
        )
        assert resp.status_code == 200
        db_session.expire_all()
        assert test_devices[0].locker_slot == test_devices[1].locker_slot

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

