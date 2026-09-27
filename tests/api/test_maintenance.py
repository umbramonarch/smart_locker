"""
File: test_maintenance.py
Description: Dashboard maintenance lifecycle — "To maintenance" blocks borrow,
             and "Back in service" requires the new calibration date. Both are
             admin-secret mutations on the dashboard; the mirror Location cell
             derives the maintenance token from the status.
Project: smart_locker/tests/api
Notes: Run with: python -m pytest tests/api/test_maintenance.py -v
"""

from datetime import date, timedelta

import pytest

from smart_locker.database.models import DeviceStatus
from smart_locker.database.repositories import DeviceRepository
from smart_locker.services.locker_service import LockerService

from tests.api.helpers import dashboard_admin_headers


@pytest.fixture()
def locker_unit(db_session):
    """One registered cabinet unit (slot assigned, AVAILABLE)."""
    device = DeviceRepository.create(
        db_session,
        name="Multimeter",
        device_type="Multimeter",
        pm_number="PM-001",
        locker_slot=1,
    )
    db_session.commit()
    return device


@pytest.fixture()
def shelf_unit(db_session):
    """One non-cabinet catalog row (no slot, free-text place)."""
    device = DeviceRepository.create(
        db_session,
        name="Spare probe",
        device_type="Accessory",
        pm_number="PM-777",
    )
    device.location = "Workshop"
    db_session.commit()
    return device


@pytest.fixture()
def maintenance_unit(db_session, locker_unit):
    """A cabinet unit already in maintenance (via the service)."""
    from smart_locker.services.device_catalog import to_maintenance

    to_maintenance(db_session, locker_unit)
    db_session.commit()
    return locker_unit


class TestToMaintenance:
    """POST /api/dashboard/devices/{pm}/maintenance."""

    def test_requires_admin_secret(self, client, locker_unit, db_session):
        """No secret → 401, and the unit stays borrowable."""
        resp = client.post("/api/dashboard/devices/PM-001/maintenance")
        assert resp.status_code == 401
        db_session.expire_all()
        assert locker_unit.status == DeviceStatus.AVAILABLE

    def test_unknown_pm_is_404(self, client, dashboard_secret):
        resp = client.post(
            "/api/dashboard/devices/PM-999/maintenance",
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 404

    def test_non_cabinet_row_is_409(
        self, client, shelf_unit, dashboard_secret
    ):
        """The maintenance lifecycle needs a cabinet slot — a free-text
        place on a shelf row is not out-of-service."""
        resp = client.post(
            "/api/dashboard/devices/PM-777/maintenance",
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 409

    def test_borrowed_unit_is_409(
        self, client, mock_context, test_user, locker_unit, db_session,
        dashboard_secret,
    ):
        """Refused while borrowed — the loan owns the row."""
        session = mock_context.session_mgr.start_session(test_user)
        assert LockerService.borrow_device(db_session, session, locker_unit.id)
        db_session.commit()

        resp = client.post(
            "/api/dashboard/devices/PM-001/maintenance",
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 409
        db_session.expire_all()
        assert locker_unit.status == DeviceStatus.BORROWED

    def test_marks_maintenance_and_blocks_borrow(
        self, client, mock_context, test_user, locker_unit, db_session,
        dashboard_secret,
    ):
        """A marked unit reports the maintenance place and refuses borrow."""
        resp = client.post(
            "/api/dashboard/devices/PM-001/maintenance",
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["changed"] is True
        assert body["device"]["status"] == "maintenance"
        # The derived Location is the maintenance token the mirror writes.
        assert body["device"]["location"] == "Maintenance"

        db_session.expire_all()
        assert locker_unit.status == DeviceStatus.MAINTENANCE

        mock_context.session_mgr.start_session(test_user)
        borrow = client.post(f"/api/devices/{locker_unit.id}/borrow")
        assert borrow.status_code == 200
        assert borrow.json()["success"] is False
        assert "maintenance" in borrow.json()["message"].lower()

    def test_second_call_is_noop(
        self, client, locker_unit, db_session, dashboard_secret
    ):
        """Re-marking a maintenance unit succeeds without a change."""
        headers = dashboard_admin_headers(dashboard_secret)
        first = client.post(
            "/api/dashboard/devices/PM-001/maintenance", headers=headers
        )
        assert first.status_code == 200
        second = client.post(
            "/api/dashboard/devices/PM-001/maintenance", headers=headers
        )
        assert second.status_code == 200
        assert second.json()["changed"] is False

    def test_lan_client_with_secret(
        self, lan_client, locker_unit, dashboard_secret
    ):
        """Dashboard mutations ride the admin secret, not loopback."""
        resp = lan_client.post(
            "/api/dashboard/devices/PM-001/maintenance",
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200


class TestBackInService:
    """POST /api/dashboard/devices/{pm}/back-in-service."""

    def _mark(self, client, secret):
        resp = client.post(
            "/api/dashboard/devices/PM-001/maintenance",
            headers=dashboard_admin_headers(secret),
        )
        assert resp.status_code == 200

    def test_requires_admin_secret(self, client, maintenance_unit):
        resp = client.post(
            "/api/dashboard/devices/PM-001/back-in-service",
            json={"calibration_due": "2027-01-01"},
        )
        assert resp.status_code == 401

    def test_missing_date_is_422(
        self, client, maintenance_unit, dashboard_secret
    ):
        """Back in service without a new date is refused."""
        resp = client.post(
            "/api/dashboard/devices/PM-001/back-in-service",
            json={},
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 422

    def test_garbage_date_is_422(
        self, client, maintenance_unit, dashboard_secret
    ):
        resp = client.post(
            "/api/dashboard/devices/PM-001/back-in-service",
            json={"calibration_due": "someday"},
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 422

    def test_not_in_maintenance_is_409(
        self, client, locker_unit, dashboard_secret
    ):
        """An available unit cannot 'come back' — it never left."""
        resp = client.post(
            "/api/dashboard/devices/PM-001/back-in-service",
            json={"calibration_due": "2027-01-01"},
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 409

    def test_non_cabinet_row_is_409(
        self, client, shelf_unit, dashboard_secret
    ):
        resp = client.post(
            "/api/dashboard/devices/PM-777/back-in-service",
            json={"calibration_due": "2027-01-01"},
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 409

    def test_returns_unit_and_saves_date(
        self, client, mock_context, test_user, locker_unit, db_session,
        dashboard_secret,
    ):
        """Back in service clears the block and stores the new date."""
        self._mark(client, dashboard_secret)
        new_due = date.today() + timedelta(days=90)
        resp = client.post(
            "/api/dashboard/devices/PM-001/back-in-service",
            json={"calibration_due": new_due.isoformat()},
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["device"]["status"] == "available"
        assert body["device"]["calibration_due"] == new_due.isoformat()
        # The derived Location is the in-locker token again.
        assert body["device"]["location"] == "Locker"

        db_session.expire_all()
        assert locker_unit.status == DeviceStatus.AVAILABLE
        assert locker_unit.calibration_due == new_due

        # Borrowable again.
        mock_context.session_mgr.start_session(test_user)
        borrow = client.post(f"/api/devices/{locker_unit.id}/borrow")
        assert borrow.status_code == 200
        assert borrow.json()["success"] is True

    def test_past_date_stays_unborrowable(
        self, client, mock_context, test_user, locker_unit, db_session,
        dashboard_secret,
    ):
        """Back in service stores the date as given — a stale date keeps
        the borrow gate closed on its own."""
        self._mark(client, dashboard_secret)
        stale = date.today() - timedelta(days=1)
        resp = client.post(
            "/api/dashboard/devices/PM-001/back-in-service",
            json={"calibration_due": stale.isoformat()},
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        db_session.expire_all()
        assert locker_unit.status == DeviceStatus.AVAILABLE
        assert locker_unit.calibration_due == stale

        mock_context.session_mgr.start_session(test_user)
        borrow = client.post(f"/api/devices/{locker_unit.id}/borrow")
        assert borrow.status_code == 200
        assert borrow.json()["success"] is False
        assert "calibration" in borrow.json()["message"].lower()
