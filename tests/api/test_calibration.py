"""
File: test_calibration.py
Description: Calibration gate through the real routes: a badge before the
             due date, borrow refused on the due date and after, while
             return stays open. Payload fields ride GET /api/devices and the
             public dashboard feeds.
Project: smart_locker/tests/api
Notes: Run with: python -m pytest tests/api/test_calibration.py -v
"""
from datetime import date, timedelta

from config.settings import MAX_BORROWS
from smart_locker.database.models import DeviceStatus, TransactionType
from smart_locker.database.repositories import (
    DeviceRepository,
    TransactionRepository,
)

TODAY = date.today()


def _mk_device(db_session, *, pm, slot, cal=None, name=None):
    """One cabinet unit (slot assigned) with an optional calibration date."""
    device = DeviceRepository.create(
        db_session,
        name=name or f"Meter {pm}",
        device_type="Tool",
        pm_number=pm,
        locker_slot=slot,
        calibration_due=cal,
    )
    # Cabinet units on the kiosk grids carry a sticker.
    DeviceRepository.bind_tag(db_session, device, f"tag:{pm}")
    return device


class TestCalibrationBorrowBlock:
    """POST /api/devices/{id}/borrow — refused on the due date and after."""

    def test_borrow_day_before_due_succeeds(
        self, client, mock_context, test_user, db_session
    ):
        """The day before the due date borrows — badge only, no block."""
        device = _mk_device(
            db_session, pm="PM-101", slot=11, cal=TODAY + timedelta(days=1)
        )
        db_session.commit()

        mock_context.session_mgr.start_session(test_user)
        resp = client.post(f"/api/devices/{device.id}/borrow")
        assert resp.status_code == 200
        assert resp.json() == {
            "success": True,
            "message": f"{device.name} borrowed.",
        }

        db_session.expire_all()
        device = DeviceRepository.find_by_id(db_session, device.id)
        assert device.status == DeviceStatus.BORROWED
        assert device.current_borrower_id == test_user.id
        txns = TransactionRepository.get_device_history(db_session, device.id)
        assert [t.transaction_type for t in txns] == [TransactionType.BORROW]

    def test_borrow_on_due_date_refused(
        self, client, mock_context, test_user, db_session
    ):
        """Due today is refused and the message says why."""
        device = _mk_device(db_session, pm="PM-102", slot=12, cal=TODAY)
        db_session.commit()

        mock_context.session_mgr.start_session(test_user)
        resp = client.post(f"/api/devices/{device.id}/borrow")
        assert resp.status_code == 200
        assert resp.json() == {
            "success": False,
            "message": f"Could not borrow {device.name}: calibration due today.",
        }

        db_session.expire_all()
        device = DeviceRepository.find_by_id(db_session, device.id)
        assert device.status == DeviceStatus.AVAILABLE
        assert device.current_borrower_id is None
        assert TransactionRepository.get_device_history(db_session, device.id) == []

    def test_borrow_overdue_refused(
        self, client, mock_context, test_user, db_session
    ):
        """Overdue is refused; the reason names the missed date."""
        due = TODAY - timedelta(days=3)
        device = _mk_device(db_session, pm="PM-103", slot=13, cal=due)
        db_session.commit()

        mock_context.session_mgr.start_session(test_user)
        resp = client.post(f"/api/devices/{device.id}/borrow")
        assert resp.status_code == 200
        assert resp.json() == {
            "success": False,
            "message": (
                f"Could not borrow {device.name}: "
                f"calibration overdue (due {due.isoformat()})."
            ),
        }

        db_session.expire_all()
        assert (
            DeviceRepository.find_by_id(db_session, device.id).status
            == DeviceStatus.AVAILABLE
        )

    def test_borrow_without_calibration_succeeds(
        self, client, mock_context, test_user, db_session
    ):
        """No calibration date is never blocked."""
        device = _mk_device(db_session, pm="PM-104", slot=14, cal=None)
        db_session.commit()

        mock_context.session_mgr.start_session(test_user)
        resp = client.post(f"/api/devices/{device.id}/borrow")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_return_overdue_succeeds(
        self, client, mock_context, test_user, db_session
    ):
        """Return of an overdue unit still works."""
        device = _mk_device(
            db_session, pm="PM-105", slot=15, cal=TODAY - timedelta(days=10)
        )
        db_session.commit()

        mock_context.session_mgr.start_session(test_user)
        resp = client.post(f"/api/devices/{device.id}/borrow")
        assert resp.json()["success"] is False  # cannot borrow it first…

        # …so seed the loan directly, then return through the route.
        DeviceRepository.borrow(db_session, device, test_user.id)
        db_session.commit()

        resp = client.post(f"/api/devices/{device.id}/return")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

        db_session.expire_all()
        device = DeviceRepository.find_by_id(db_session, device.id)
        assert device.status == DeviceStatus.AVAILABLE
        assert device.current_borrower_id is None

    def test_transfer_overdue_refused(
        self, client, mock_context, test_user, admin_user, db_session
    ):
        """A handover is a new loan — an overdue unit cannot change hands."""
        device = _mk_device(
            db_session, pm="PM-106", slot=16, cal=TODAY - timedelta(days=1)
        )
        DeviceRepository.borrow(db_session, device, admin_user.id)
        db_session.commit()

        mock_context.session_mgr.start_session(test_user)
        resp = client.post(f"/api/devices/{device.id}/transfer")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["message"] == (
            f"Could not transfer {device.name}: "
            f"calibration overdue (due {(TODAY - timedelta(days=1)).isoformat()})."
        )

        db_session.expire_all()
        device = DeviceRepository.find_by_id(db_session, device.id)
        assert device.status == DeviceStatus.BORROWED
        assert device.current_borrower_id == admin_user.id


class TestCalibrationPayload:
    """calibration_state / calibration_days_left on the device feeds."""

    def test_devices_payload_states(
        self, client, mock_context, test_user, db_session
    ):
        """GET /api/devices carries the state the kiosk badge renders."""
        ok = _mk_device(
            db_session, pm="PM-201", slot=21,
            cal=TODAY + timedelta(days=60), name="FarAway",
        )
        soon = _mk_device(
            db_session, pm="PM-202", slot=22,
            cal=TODAY + timedelta(days=5), name="DueSoon",
        )
        due = _mk_device(db_session, pm="PM-203", slot=23, cal=TODAY, name="DueToday")
        over = _mk_device(
            db_session, pm="PM-204", slot=24,
            cal=TODAY - timedelta(days=2), name="Overdue",
        )
        none = _mk_device(db_session, pm="PM-205", slot=25, cal=None, name="NoDate")
        db_session.commit()

        mock_context.session_mgr.start_session(test_user)
        resp = client.get("/api/devices")
        assert resp.status_code == 200
        by_pm = {d["pm_number"]: d for d in resp.json()}

        assert by_pm[ok.pm_number]["calibration_state"] == "ok"
        assert by_pm[ok.pm_number]["calibration_days_left"] == 60
        assert by_pm[soon.pm_number]["calibration_state"] == "due_soon"
        assert by_pm[soon.pm_number]["calibration_days_left"] == 5
        assert by_pm[due.pm_number]["calibration_state"] == "due"
        assert by_pm[due.pm_number]["calibration_days_left"] == 0
        assert by_pm[over.pm_number]["calibration_state"] == "overdue"
        assert by_pm[over.pm_number]["calibration_days_left"] == -2
        assert by_pm[none.pm_number]["calibration_state"] is None
        assert by_pm[none.pm_number]["calibration_days_left"] is None

    def test_dashboard_devices_payload_is_public(
        self, lan_client, db_session
    ):
        """The LAN dashboard Locker feed carries the same state, no gate."""
        device = _mk_device(
            db_session, pm="PM-206", slot=26,
            cal=TODAY + timedelta(days=3), name="BenchPsu",
        )
        db_session.commit()

        resp = lan_client.get("/api/dashboard/devices")
        assert resp.status_code == 200
        row = next(d for d in resp.json() if d["pm_number"] == "PM-206")
        assert row["calibration_state"] == "due_soon"
        assert row["calibration_days_left"] == 3

    def test_dashboard_inventory_payload_is_public(self, lan_client, db_session):
        """Inventory rows (catalog incl. non-locker) carry the state too."""
        _mk_device(
            db_session, pm="PM-207", slot=27,
            cal=TODAY - timedelta(days=1), name="OldScope",
        )
        DeviceRepository.create(
            db_session, name="Shelf Tool", device_type="Tool",
            pm_number="PM-208", calibration_due=TODAY + timedelta(days=2),
        )
        db_session.commit()

        resp = lan_client.get("/api/dashboard/inventory")
        assert resp.status_code == 200
        by_pm = {d["pm_number"]: d for d in resp.json()}
        assert by_pm["PM-207"]["calibration_state"] == "overdue"
        assert by_pm["PM-208"]["calibration_state"] == "due_soon"

    def test_warn_window_env_controls_badge(
        self, client, mock_context, test_user, db_session, monkeypatch
    ):
        """SMART_LOCKER_CALIBRATION_WARN_DAYS moves the due-soon boundary."""
        monkeypatch.setenv("SMART_LOCKER_CALIBRATION_WARN_DAYS", "2")
        device = _mk_device(
            db_session, pm="PM-209", slot=29,
            cal=TODAY + timedelta(days=5), name="WarnEdge",
        )
        db_session.commit()

        mock_context.session_mgr.start_session(test_user)
        resp = client.get("/api/devices")
        row = next(d for d in resp.json() if d["pm_number"] == "PM-209")
        # 5 days out is beyond a 2-day window — no badge, borrow allowed.
        assert row["calibration_state"] == "ok"
        resp = client.post(f"/api/devices/{device.id}/borrow")
        assert resp.json()["success"] is True

    def test_config_exposes_warn_days(self, client):
        """/api/config hands the kiosk the warn window for its fallback."""
        resp = client.get("/api/config")
        assert resp.status_code == 200
        assert resp.json()["calibration_warn_days"] == 14

    def test_borrow_limit_reason_names_the_limit(
        self, client, mock_context, test_user, db_session
    ):
        """While reasons exist, the limit refusal says why too."""
        for i in range(MAX_BORROWS):
            d = _mk_device(
                db_session, pm=f"PM-3{i:02d}", slot=30 + i, name=f"Held {i}"
            )
            DeviceRepository.borrow(db_session, d, test_user.id)
        device = _mk_device(db_session, pm="PM-399", slot=47, name="OneTooMany")
        db_session.commit()

        mock_context.session_mgr.start_session(test_user)
        resp = client.post(f"/api/devices/{device.id}/borrow")
        assert resp.json() == {
            "success": False,
            "message": (
                f"Could not borrow OneTooMany: "
                f"borrow limit reached ({MAX_BORROWS}/{MAX_BORROWS})."
            ),
        }
