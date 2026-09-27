"""
File: conftest.py
Description: True end-to-end harness. Boots the real app via create_app() +
             TestClient lifespan, so a real AppContext and NFC bridge loop run
             against a FakeNFCReader (SMART_LOCKER_FAKE_READER). Taps are driven
             through POST /api/dev/tap and observed via a subscribe_sse() queue,
             exactly like the kiosk EventSource client.
Project: smart_locker/tests/e2e
Notes: All tests share one in-memory StaticPool SQLite engine wired into
       smart_locker.database.engine so HTTP handlers, services, and the bridge
       thread see the same rows. Call the `e2e` factory AFTER any monkeypatching
       that must be in place before AppContext construction (e.g.
       app_context.SESSION_TIMEOUT_SECONDS).
"""

import asyncio
import queue as queue_mod
import time
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import smart_locker.api.app_context as ctx_module
import smart_locker.database.engine as eng_mod
from smart_locker.api.server import create_app
from smart_locker.database.models import Base


class E2EHarness:
    """Handle to a booted app: HTTP client, live AppContext, SSE capture, DB."""

    def __init__(self, client: TestClient, ctx, session_factory):
        self.client = client
        self.ctx = ctx
        self.session_factory = session_factory
        self.events = ctx.subscribe_sse()

    def tap(self, uid: str) -> None:
        """Inject a card tap through the dev harness (full bridge pipeline)."""
        r = self.client.post("/api/dev/tap", json={"uid": uid})
        assert r.status_code == 200, f"dev/tap failed: {r.status_code} {r.text}"

    def next_event(self, timeout: float = 8.0) -> dict:
        """Return the next SSE payload broadcast to kiosk subscribers."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                return self.events.get_nowait()
            except asyncio.QueueEmpty:
                time.sleep(0.05)
        raise TimeoutError("No SSE event within %.1fs" % timeout)

    def wait_event(self, name: str, timeout: float = 8.0) -> dict:
        """Drain the SSE queue until an event with this name arrives."""
        deadline = time.monotonic() + timeout
        seen: list[dict] = []
        while time.monotonic() < deadline:
            try:
                payload = self.events.get_nowait()
            except asyncio.QueueEmpty:
                time.sleep(0.05)
                continue
            if payload.get("event") == name:
                return payload
            seen.append(payload)
        raise TimeoutError(f"No '{name}' event within {timeout}s. Saw: {seen}")

    def assert_no_event(self, name: str, within: float = 1.5) -> list[dict]:
        """Assert no event with this name is broadcast during the window.

        Returns everything that did arrive so callers can inspect further.
        """
        deadline = time.monotonic() + within
        seen: list[dict] = []
        while time.monotonic() < deadline:
            try:
                payload = self.events.get_nowait()
            except asyncio.QueueEmpty:
                time.sleep(0.05)
                continue
            seen.append(payload)
            assert payload.get("event") != name, (
                f"Unexpected '{name}' event: {payload}"
            )
        return seen

    def db(self):
        """New short-lived session on the shared test database."""
        return self.session_factory()


@pytest.fixture()
def e2e(monkeypatch):
    """Factory fixture: call ``e2e()`` to boot the real app for one test.

    Booting inside the call lets the test monkeypatch settings/env first
    (e.g. ``app_context.SESSION_TIMEOUT_SECONDS``) before the lifespan
    constructs AppContext. The fixture owns teardown.
    """
    eng_mod.reset_engine()

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    eng_mod._engine = engine
    eng_mod._session_factory = factory

    stack = ExitStack()

    def boot() -> E2EHarness:
        monkeypatch.setenv("SMART_LOCKER_FAKE_READER", "1")
        app = create_app()
        client = stack.enter_context(
            TestClient(app, client=("127.0.0.1", 50000))
        )
        assert ctx_module.context is not None, "lifespan did not create AppContext"
        return E2EHarness(client, ctx_module.context, factory)

    yield boot

    stack.close()
    # Drain scheduled mirror-flush workers and any in-flight scheduler tick
    # before the engine goes away — a late tick on a disposed in-memory
    # engine crashes the test process.
    from smart_locker.sync import mirror, scheduler

    mirror.flush_scheduled()
    deadline = time.monotonic() + 10
    while scheduler.sync_in_progress() and time.monotonic() < deadline:
        time.sleep(0.05)
    eng_mod.reset_engine()
    ctx_module.context = None


@pytest.fixture()
def lan(e2e):
    """Boot the app and add a second, LAN-originated client to the harness.

    Shares the booted app — the LAN client never runs the lifespan, it just
    issues requests from a non-loopback client address.
    """
    harness = e2e()
    harness.lan_client = TestClient(harness.client.app, client=("192.0.2.10", 50000))
    return harness
