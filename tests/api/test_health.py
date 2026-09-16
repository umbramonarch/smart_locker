"""
File: test_health.py
Description: Tests for public GET /api/config (asset label).
Project: smart_locker/tests/api
Notes: Run with: python -m pytest tests/api/test_health.py -v
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

class TestPublicConfig:
    """Kiosk/dashboard read the site asset label from a public config endpoint."""

    def test_config_default_asset_label(self, client, mock_context):
        """Unset env keeps the built-in PM number label."""
        resp = client.get("/api/config")
        assert resp.status_code == 200
        assert resp.json()["asset_label"] == "PM number"

    def test_config_asset_label_from_env(self, client, mock_context, monkeypatch):
        """SMART_LOCKER_ASSET_LABEL is returned without a kiosk session."""
        monkeypatch.setenv("SMART_LOCKER_ASSET_LABEL", "Asset ID")
        resp = client.get("/api/config")
        assert resp.status_code == 200
        assert resp.json()["asset_label"] == "Asset ID"


from datetime import date, timedelta


class TestCalibrationAlerts:
    """GET /api/calibration/alerts — loopback-only warn summary for the kiosk."""

    def test_alerts_counts_and_sorted_rows(
        self, client, db_session, monkeypatch
    ):
        """Counts and rows: overdue first (most overdue first), then due_soon."""
        monkeypatch.setattr("config.settings.CALIBRATION_WARN_DAYS", 14)
        today = date.today()
        DeviceRepository.create(
            db_session, name="Dev A", device_type="t",
            pm_number="PM-1", locker_slot=1,
            calibration_due=today - timedelta(days=10),
        )
        DeviceRepository.create(
            db_session, name="Dev B", device_type="t",
            pm_number="PM-2", locker_slot=2,
            calibration_due=today - timedelta(days=2),
        )
        DeviceRepository.create(
            db_session, name="Dev C", device_type="t",
            pm_number="PM-3", locker_slot=3,
            calibration_due=today + timedelta(days=3),
        )
        DeviceRepository.create(
            db_session, name="Dev D", device_type="t",
            pm_number="PM-4", locker_slot=4,
            calibration_due=today + timedelta(days=60),
        )
        db_session.commit()
        resp = client.get("/api/calibration/alerts")
        assert resp.status_code == 200
        data = resp.json()
        assert data["overdue"] == 2
        assert data["due_soon"] == 1
        devs = data["devices"]
        assert [d["pm_number"] for d in devs] == ["PM-1", "PM-2", "PM-3"]
        for d in devs:
            assert set(d) == {
                "name", "pm_number", "locker_slot",
                "calibration_due", "calibration_state",
                "calibration_days_left",
            }
        # Read-only payload: no person names / borrower fields.
        assert "borrower" not in resp.text
        assert "Admin" not in resp.text

    def test_alerts_empty_when_all_ok(self, client, db_session, monkeypatch):
        """No flagged devices → zero counts and an empty list."""
        monkeypatch.setattr("config.settings.CALIBRATION_WARN_DAYS", 14)
        DeviceRepository.create(
            db_session, name="Dev", device_type="t",
            pm_number="PM-9", locker_slot=9,
            calibration_due=date.today() + timedelta(days=90),
        )
        db_session.commit()
        data = client.get("/api/calibration/alerts").json()
        assert data == {"overdue": 0, "due_soon": 0, "devices": []}

    def test_alerts_lan_is_403(self, lan_client, db_session):
        """LAN clients cannot read the kiosk-local alerts endpoint."""
        assert lan_client.get("/api/calibration/alerts").status_code == 403

    def test_config_calibration_warn_days(self, client, monkeypatch):
        """GET /api/config exposes calibration_warn_days."""
        monkeypatch.setattr("config.settings.CALIBRATION_WARN_DAYS", 7)
        resp = client.get("/api/config")
        assert resp.status_code == 200
        assert resp.json()["calibration_warn_days"] == 7
