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

