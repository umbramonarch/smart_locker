"""
File: test_events.py
Description: Tests for SSE /api/events fan-out so a second client cannot
             starve the kiosk EventSource.
Project: smart_locker/tests/api
Notes: Run with: python -m pytest tests/api/test_events.py -v
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
from smart_locker.api.app_context import AppContext

class TestSseEvents:
    """GET /api/events must fan-out so a second client cannot starve the kiosk."""

    def test_push_sse_fans_out_to_two_queues(self, _db_setup, monkeypatch):
        """_push_sse copies the same payload onto every subscriber queue."""
        monkeypatch.setenv("SMART_LOCKER_FAKE_READER", "1")
        ctx = AppContext()
        ctx_module.context = ctx
        try:
            q1 = ctx.subscribe_sse()
            q2 = ctx.subscribe_sse()
            from smart_locker.api.routes import _push_sse

            _push_sse({"event": "auth_success"})
            assert q1.get_nowait()["event"] == "auth_success"
            assert q2.get_nowait()["event"] == "auth_success"
        finally:
            ctx_module.context = None

