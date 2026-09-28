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

from smart_locker.database.models import Device, DeviceStatus
from smart_locker.database.repositories import DeviceRepository
from smart_locker.services.locker_service import LockerService

from tests.api.helpers import dashboard_admin_headers


@pytest.fixture()
def locker_unit(db_session):
    """One registered cabinet unit (slot assigned, sticker bound, AVAILABLE)."""
    device = DeviceRepository.create(
        db_session,
        name="Multimeter",
        device_type="Multimeter",
        pm_number="PM-001",
        locker_slot=1,
    )
    # Borrow requires a bound sticker — same convention as test_devices.
    DeviceRepository.bind_tag(db_session, device, "tag-hmac-PM-001")
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

    def test_lan_without_secret_is_401(self, lan_client, locker_unit):
        """A LAN mutation without the secret is 401 — same as loopback."""
        resp = lan_client.post("/api/dashboard/devices/PM-001/maintenance")
        assert resp.status_code == 401

    def test_stale_check_loses_to_committed_borrow(
        self, db_session, locker_unit, test_user
    ):
        """A borrow committed between the status check and the write wins:
        to_maintenance must raise DeviceBorrowed, not clobber the loan."""
        from sqlalchemy.orm import Session
        from smart_locker.database.engine import get_engine
        from smart_locker.services.device_catalog import (
            DeviceBorrowed,
            to_maintenance,
        )

        db_session.commit()  # publish test_user for the borrower FK
        # locker_unit in db_session still reads AVAILABLE — a stale snapshot.
        with Session(get_engine()) as other:
            rival = other.get(Device, locker_unit.id)
            rival.status = DeviceStatus.BORROWED
            rival.current_borrower_id = test_user.id
            other.commit()

        assert locker_unit.status == DeviceStatus.AVAILABLE
        with pytest.raises(DeviceBorrowed):
            to_maintenance(db_session, locker_unit)

        db_session.expire_all()
        assert locker_unit.status == DeviceStatus.BORROWED
        assert locker_unit.current_borrower_id == test_user.id

    def test_stale_borrow_loses_to_committed_maintenance(
        self, db_session, locker_unit, test_user, mock_context
    ):
        """A maintenance commit between the availability check and the
        borrow write wins: borrow_device refuses instead of overwriting."""
        from sqlalchemy.orm import Session
        from smart_locker.database.engine import get_engine

        db_session.commit()
        with Session(get_engine()) as other:
            rival = other.get(Device, locker_unit.id)
            rival.status = DeviceStatus.MAINTENANCE
            other.commit()

        assert locker_unit.status == DeviceStatus.AVAILABLE  # stale snapshot
        user_session = mock_context.session_mgr.start_session(test_user)
        outcome = LockerService.borrow_device(
            db_session, user_session, locker_unit.id
        )
        assert not outcome
        assert "maintenance" in outcome.reason

        db_session.expire_all()
        assert locker_unit.status == DeviceStatus.MAINTENANCE
        assert locker_unit.current_borrower_id is None

    def test_post_marks_mirror_dirty(
        self, client, locker_unit, dashboard_secret, monkeypatch
    ):
        """The dashboard POST drives the deferred mirror pipeline — the
        after_commit listener marks dirty and schedules the flush."""
        from smart_locker.sync import mirror

        calls = []
        monkeypatch.setattr(mirror, "mark_dirty", lambda: calls.append("dirty"))
        monkeypatch.setattr(
            mirror, "schedule_flush", lambda: calls.append("flush")
        )
        resp = client.post(
            "/api/dashboard/devices/PM-001/maintenance",
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        assert calls == ["dirty", "flush"]


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

    def test_raced_back_in_service_is_refused(
        self, db_session, maintenance_unit
    ):
        """The row already left maintenance under us — the conditional
        write misses and the call refuses instead of overwriting."""
        from sqlalchemy.orm import Session
        from smart_locker.database.engine import get_engine
        from smart_locker.services.device_catalog import (
            NotInMaintenance,
            back_in_service,
        )

        with Session(get_engine()) as other:
            rival = other.get(Device, maintenance_unit.id)
            rival.status = DeviceStatus.AVAILABLE
            other.commit()

        # maintenance_unit in db_session still reads MAINTENANCE — stale.
        assert maintenance_unit.status == DeviceStatus.MAINTENANCE
        with pytest.raises(NotInMaintenance):
            back_in_service(
                db_session,
                maintenance_unit,
                date.today() + timedelta(days=90),
            )

        db_session.expire_all()
        assert maintenance_unit.status == DeviceStatus.AVAILABLE

    def test_back_in_service_clears_dangling_borrower(
        self, db_session, locker_unit, test_user
    ):
        """A corrupted maintenance+borrower row comes back clean: one
        conditional write restores AVAILABLE and clears the borrower FK."""
        from smart_locker.services.device_catalog import back_in_service

        db_session.commit()  # publish test_user for the borrower FK
        locker_unit.status = DeviceStatus.MAINTENANCE
        locker_unit.current_borrower_id = test_user.id
        db_session.commit()

        new_due = date.today() + timedelta(days=90)
        assert back_in_service(db_session, locker_unit, new_due) is True
        db_session.commit()

        db_session.expire_all()
        assert locker_unit.status == DeviceStatus.AVAILABLE
        assert locker_unit.current_borrower_id is None
        assert locker_unit.calibration_due == new_due
