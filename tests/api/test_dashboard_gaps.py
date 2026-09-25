"""
File: test_dashboard_gaps.py
Description: Coverage-gap tests for dashboard-adjacent routes -- populated
             transactions list (shape + newest-first), owner-edit 400/404
             mapping, the /dashboard page route and the static index mount,
             the dev-status default-UID flag, and public registrants over LAN.
Project: smart_locker/tests/api
Notes: Run with: python -m pytest tests/api/test_dashboard_gaps.py -v
"""
from datetime import datetime

from fastapi.testclient import TestClient

from smart_locker.api.server import create_app
from smart_locker.database.repositories import (
    RegistrantRepository,
    TransactionRepository,
)
from tests.api.helpers import catalog_workbook, dashboard_admin_headers


class TestDashboardTransactionsPopulated:
    """GET /api/dashboard/transactions with real rows -- shape + ordering."""

    def test_transactions_list_populated_newest_first(
        self, client, db_session, test_user, admin_user, test_devices,
        dashboard_secret,
    ):
        """Seeded log rows serialize names and sort newest first."""
        borrow = TransactionRepository.log_borrow(
            db_session, test_user.id, test_devices[0].id,
            notes="morning borrow",
        )
        borrow.timestamp = datetime(2026, 1, 5, 9, 0, 0)
        returned = TransactionRepository.log_return(
            db_session, test_user.id, test_devices[0].id,
            notes="back same day",
        )
        returned.timestamp = datetime(2026, 1, 5, 17, 30, 0)
        admin_return = TransactionRepository.log_return(
            db_session, test_user.id, test_devices[1].id,
            notes="admin returned on behalf",
            performed_by_id=admin_user.id,
        )
        admin_return.timestamp = datetime(2026, 1, 6, 8, 15, 0)
        db_session.commit()

        resp = client.get(
            "/api/dashboard/transactions",
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 3
        # Reverse chronological order -- newest first.
        assert [r["timestamp"] for r in rows] == [
            "2026-01-06 08:15:00",
            "2026-01-05 17:30:00",
            "2026-01-05 09:00:00",
        ]
        newest = rows[0]
        assert newest["transaction_type"] == "return"
        assert newest["user_name"] == "Test User"
        assert newest["device_name"] == "Drone"
        assert newest["performed_by"] == "Admin User"
        assert newest["notes"] == "admin returned on behalf"
        oldest = rows[-1]
        assert oldest["transaction_type"] == "borrow"
        assert oldest["performed_by"] == ""
        for row in rows:
            assert set(row) == {
                "timestamp", "user_name", "device_name",
                "transaction_type", "performed_by", "notes",
            }
            assert isinstance(row["timestamp"], str)


class TestDashboardOwnerGaps:
    """POST /api/dashboard/owner -- validation and catalog-miss mapping."""

    def test_owner_whitespace_pm_is_400(
        self, client, tmp_path, monkeypatch, dashboard_secret
    ):
        """A whitespace-only PM survives pydantic then fails set_owner -> 400."""
        path = catalog_workbook(tmp_path, [
            ["Equipment", "Name", "Location"],
            ["PM-VAN", "Van kit", "Workshop"],
        ])
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))
        resp = client.post(
            "/api/dashboard/owner",
            json={"pm_number": "   ", "owner": "Alex"},
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 400

    def test_owner_missing_pm_field_is_422(
        self, client, dashboard_secret
    ):
        """A body without pm_number fails pydantic validation -> 422."""
        resp = client.post(
            "/api/dashboard/owner",
            json={"owner": "Alex"},
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 422

    def test_owner_pm_not_in_catalog_is_404(
        self, client, tmp_path, monkeypatch, dashboard_secret
    ):
        """A PM absent from the Excel catalog maps UnknownPm -> 404."""
        path = catalog_workbook(tmp_path, [
            ["Equipment", "Name", "Location"],
            ["PM-VAN", "Van kit", "Workshop"],
        ])
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))
        resp = client.post(
            "/api/dashboard/owner",
            json={"pm_number": "PM-ABSENT", "owner": "Alex"},
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 404


class TestDashboardPages:
    """/dashboard route + static index -- HTML serving without lifespan."""

    def test_dashboard_page_serves_html(self, client):
        """The bare /dashboard URL serves frontend/dashboard.html."""
        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Smart Locker Dashboard" in resp.text

    def test_root_index_served_without_lifespan(self):
        """StaticFiles(html=True) serves index.html at / on the real app.

        ``TestClient(create_app())`` without a context manager never runs
        the lifespan hook, so no NFC reader is constructed -- the static
        mount answers anyway.
        """
        static_client = TestClient(create_app())
        resp = static_client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "<title>Smart Locker</title>" in resp.text


class TestDevStatusFlag:
    """GET /api/dev/status -- the default-UID flag tracks the env var."""

    def test_dev_status_reports_default_uid_flag(
        self, client, mock_context, monkeypatch
    ):
        """default_uid_set flips with SMART_LOCKER_FAKE_DEFAULT_UID."""
        monkeypatch.delenv("SMART_LOCKER_FAKE_READER", raising=False)
        monkeypatch.delenv("SMART_LOCKER_FAKE_DEFAULT_UID", raising=False)
        mock_context.reader = None

        body = client.get("/api/dev/status").json()
        assert body["fake_reader"] is False
        assert body["default_uid_set"] is False

        monkeypatch.setenv("SMART_LOCKER_FAKE_DEFAULT_UID", "AABBCCDD")
        body = client.get("/api/dev/status").json()
        assert body["default_uid_set"] is True


class TestRegistrantsLanGaps:
    """GET /api/registrants -- public read reachable from the LAN."""

    def test_registrants_public_from_lan(self, lan_client, db_session):
        """LAN browsers can read the self-registration name list."""
        RegistrantRepository.add_names(db_session, {"Alice", "Bob"})
        db_session.commit()
        resp = lan_client.get("/api/registrants")
        assert resp.status_code == 200
        names = resp.json()["names"]
        assert "Alice" in names
        assert "Bob" in names
