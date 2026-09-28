"""
File: conftest.py
Description: Shared FastAPI TestClient fixtures for kiosk API tests. In-memory
             SQLite with StaticPool so route handlers share the test database.
Project: smart_locker/tests/api
Notes: These fixtures override the suite db_session only for tests/api/.
       Kiosk clients are loopback; lan_client is 192.0.2.10.
"""

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from smart_locker.api.routes import router
from smart_locker.auth.session_manager import SessionManager
from smart_locker.database.models import DeviceStatus
from smart_locker.database.repositories import DeviceRepository, UserRepository

import smart_locker.api.app_context as ctx_module
from smart_locker.api.app_context import AppContext


@pytest.fixture()
def _db_setup():
    """Set up in-memory database with shared connection for API tests.

    In-memory SQLite creates a new database per connection, so we use
    StaticPool to ensure the test setup and the route handlers share
    the same underlying database.
    """
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker, scoped_session
    from sqlalchemy.pool import StaticPool
    from smart_locker.database.models import Base
    import smart_locker.database.engine as eng_mod

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
    scoped = scoped_session(factory)

    eng_mod._engine = engine
    eng_mod._session_factory = scoped

    yield scoped

    # Drain scheduled mirror-flush workers before the engine goes away — a
    # late tick on a disposed in-memory engine crashes the test process.
    from smart_locker.sync import mirror
    mirror.flush_scheduled()

    scoped.remove()
    eng_mod.reset_engine()


@pytest.fixture()
def db_session(_db_setup):
    """Provide a database session for test setup."""
    session = _db_setup()
    try:
        yield session
        session.commit()
    finally:
        _db_setup.remove()


@pytest.fixture()
def test_user(db_session):
    """Create a standard test user."""
    return UserRepository.create(
        db_session,
        display_name="Test User",
        uid_hmac="abc123",
        encrypted_card_uid="encrypted_test",
        role="user",
    )


@pytest.fixture()
def admin_user(db_session):
    """Create an admin test user."""
    return UserRepository.create(
        db_session,
        display_name="Admin User",
        uid_hmac="admin456",
        encrypted_card_uid="encrypted_admin",
        role="admin",
    )


@pytest.fixture()
def test_devices(db_session):
    """Create a set of test devices — cabinet units with stickers bound."""
    devices = []
    for i, (name, dtype, status) in enumerate([
        ("Camera", "Camera", DeviceStatus.AVAILABLE),
        ("Drone", "Drone", DeviceStatus.AVAILABLE),
        ("Laptop", "Laptop", DeviceStatus.MAINTENANCE),
    ], start=1):
        d = DeviceRepository.create(
            db_session,
            name=name,
            device_type=dtype,
            pm_number=f"PM-{i:03d}",
            serial_number=f"SN-{i:03d}",
            locker_slot=i,
            description=f"Test {name}",
        )
        # Kiosk borrow/return grids list only units that carry a sticker.
        DeviceRepository.bind_tag(db_session, d, f"tag-hmac-{i:03d}")
        if status == DeviceStatus.MAINTENANCE:
            d.status = DeviceStatus.MAINTENANCE
            db_session.flush()
        devices.append(d)
    return devices


@pytest.fixture()
def session_mgr():
    """Create a fresh SessionManager."""
    return SessionManager(timeout_seconds=120)


@pytest.fixture()
def mock_context(session_mgr):
    """Set up a mock AppContext with real SessionManager and SSE queue."""
    mock_ctx = MagicMock()
    mock_ctx.session_mgr = session_mgr
    mock_ctx.sse_queue = asyncio.Queue()
    mock_ctx.admin_overlay_open = False
    mock_ctx.kiosk_screen = "idle"
    mock_ctx.pending_tag_bind = None
    mock_ctx.pending_registration = None
    real_context = AppContext.__new__(AppContext)
    real_context.session_mgr = session_mgr
    real_context.sse_queue = mock_ctx.sse_queue
    real_context.admin_overlay_open = False
    real_context.pending_tag_bind = None
    real_context.pending_registration = None
    real_context.broadcast_sse = mock_ctx.broadcast_sse

    def end_kiosk_session(**kwargs):
        real_context.end_kiosk_session(**kwargs)
        mock_ctx.admin_overlay_open = real_context.admin_overlay_open
        mock_ctx.pending_tag_bind = real_context.pending_tag_bind
        mock_ctx.pending_registration = real_context.pending_registration

    mock_ctx.end_kiosk_session = end_kiosk_session
    ctx_module.context = mock_ctx
    yield mock_ctx
    ctx_module.context = None


@pytest.fixture()
def client(_db_setup, mock_context, db_session):
    """TestClient as the kiosk (loopback). Appliance routes require this."""
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, client=("127.0.0.1", 50000))


@pytest.fixture()
def lan_client(_db_setup, mock_context, db_session):
    """TestClient as a LAN browser. Must not start sessions or power off."""
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, client=("192.0.2.10", 50000))


@pytest.fixture()
def dashboard_secret(monkeypatch):
    """Configured dashboard admin secret for authorized mutation tests."""
    monkeypatch.setenv("SMART_LOCKER_DASHBOARD_ADMIN_SECRET", "test-dashboard-secret")
    return "test-dashboard-secret"
