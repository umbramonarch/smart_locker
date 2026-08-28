"""
File: test_events.py
Description: Tests for SSE /api/events fan-out so a second client cannot
             starve the kiosk EventSource, and loopback gating so a LAN
             EventSource cannot observe kiosk auth/session events.
Project: smart_locker/tests/api
Notes: Run with: python -m pytest tests/api/test_events.py -v
"""
import asyncio
import contextlib
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

    def test_lan_cannot_open_sse(self, lan_client, mock_context):
        """LAN EventSource must not observe kiosk SSE (auth_success, etc.)."""
        mock_context.sse_queue.put_nowait({"event": "auth_success"})
        resp = lan_client.get("/api/events")
        assert resp.status_code == 403
        assert mock_context.sse_queue.qsize() == 1

    def test_loopback_sse_receives_events(self, client, mock_context):
        """Kiosk loopback EventSource still receives SSE payloads.

        Starlette TestClient buffers the whole body, so an infinite SSE
        stream hangs there. Drive the ASGI app and stop after the first
        chunk instead.
        """
        mock_context.subscribe_sse = None
        mock_context.sse_queue.put_nowait({"event": "auth_success"})
        messages = []

        async def _first_chunk():
            got_body = asyncio.Event()

            async def receive():
                await got_body.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                messages.append(message)
                if message["type"] == "http.response.body":
                    got_body.set()

            scope = {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/api/events",
                "raw_path": b"/api/events",
                "root_path": "",
                "query_string": b"",
                "headers": [(b"host", b"testserver")],
                "client": ("127.0.0.1", 50000),
                "server": ("testserver", 80),
            }
            task = asyncio.create_task(client.app(scope, receive, send))
            try:
                await asyncio.wait_for(got_body.wait(), timeout=2.0)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        asyncio.run(_first_chunk())
        start = next(m for m in messages if m["type"] == "http.response.start")
        assert start["status"] == 200
        body = b"".join(
            m.get("body", b"")
            for m in messages
            if m["type"] == "http.response.body"
        ).decode()
        assert "auth_success" in body

