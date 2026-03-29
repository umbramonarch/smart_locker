"""
File: test_api.py
Description: Tests for the REST API layer — session management, device listing,
             borrow/return endpoints, user registration, admin source sync, and
             SSE event stream. Uses FastAPI's TestClient with mocked NFC context.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_api.py -v
"""

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from smart_locker.api.routes import router, get_db, require_session
from smart_locker.auth.session_manager import SessionManager, UserSession
from smart_locker.database.engine import get_session_factory, init_db, reset_engine
from smart_locker.database.models import DeviceStatus, UserRole
from smart_locker.database.repositories import DeviceRepository, UserRepository
from smart_locker.services.locker_service import LockerService

import smart_locker.api.app_context as ctx_module


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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

    # Inject into the engine module so get_session_factory() returns ours
    eng_mod._engine = engine
    eng_mod._session_factory = scoped

    yield scoped

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
    """Create a set of test devices."""
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
    ctx_module.context = mock_ctx
    yield mock_ctx
    ctx_module.context = None


@pytest.fixture()
def client(_db_setup, mock_context, db_session):
    """TestClient with the API router, sharing the test database."""
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Session Endpoint Tests
# ---------------------------------------------------------------------------

class TestSessionEndpoints:
    """Tests for session management API — get status, end session, touch."""

    def test_get_session_no_active(self, client):
        """Verify GET /api/session returns active=False when no session exists."""
        resp = client.get("/api/session")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active"] is False
        assert data["user"] is None

    def test_get_session_active(self, client, mock_context, test_user):
        """Verify GET /api/session returns user data when a session is active."""
        mock_context.session_mgr.start_session(test_user)
        resp = client.get("/api/session")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active"] is True
        assert data["user"]["id"] == test_user.id
        assert data["user"]["name"] == "Test User"
        assert data["user"]["role"] == "user"

    def test_end_session(self, client, mock_context, test_user):
        """Verify POST /api/session/end terminates the active session."""
        mock_context.session_mgr.start_session(test_user)
        resp = client.post("/api/session/end")
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert not mock_context.session_mgr.has_active_session

    def test_end_session_no_active(self, client):
        """Verify POST /api/session/end returns 401 when no session exists."""
        resp = client.post("/api/session/end")
        assert resp.status_code == 401

    def test_touch_session(self, client, mock_context, test_user):
        """Verify POST /api/session/touch succeeds when a session is active."""
        mock_context.session_mgr.start_session(test_user)
        resp = client.post("/api/session/touch")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_touch_session_no_active(self, client):
        """Verify POST /api/session/touch returns 401 when no session exists."""
        resp = client.post("/api/session/touch")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Device Endpoint Tests
# ---------------------------------------------------------------------------

class TestDeviceEndpoints:
    """Tests for device listing API — auth required, borrower name resolution."""

    def test_list_devices_no_session(self, client):
        """Verify GET /api/devices returns 401 when no session exists."""
        resp = client.get("/api/devices")
        assert resp.status_code == 401

    def test_list_devices(self, client, mock_context, test_user, test_devices):
        """Verify GET /api/devices returns all devices with correct fields and shape."""
        mock_context.session_mgr.start_session(test_user)
        resp = client.get("/api/devices")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3
        # Check device shape
        cam = next(d for d in data if d["name"] == "Camera")
        assert cam["status"] == "available"
        assert cam["borrower_name"] is None
        assert cam["serial_number"] == "SN-001"
        assert cam["pm_number"] == "PM-001"
        assert cam["locker_slot"] == 1
        # New fields present (None for test fixtures without values)
        assert "manufacturer" in cam
        assert "model" in cam
        assert "barcode" in cam
        assert "calibration_due" in cam

    def test_list_devices_borrower_name_you(
        self, client, mock_context, test_user, test_devices, db_session
    ):
        """Devices borrowed by current user should have borrower_name='You'."""
        mock_context.session_mgr.start_session(test_user)
        user_session = mock_context.session_mgr.current_session
        # Borrow the camera
        LockerService.borrow_device(db_session, user_session, test_devices[0].id)
        db_session.commit()

        resp = client.get("/api/devices")
        data = resp.json()
        cam = next(d for d in data if d["name"] == "Camera")
        assert cam["status"] == "borrowed"
        assert cam["borrower_name"] == "You"

    def test_list_devices_borrower_name_other(
        self, client, mock_context, test_user, admin_user, test_devices, db_session
    ):
        """Devices borrowed by another user show their display name."""
        # Admin borrows the camera
        admin_session = mock_context.session_mgr.start_session(admin_user)
        LockerService.borrow_device(db_session, admin_session, test_devices[0].id)
        db_session.commit()

        # Switch to test_user's session
        mock_context.session_mgr.start_session(test_user)
        resp = client.get("/api/devices")
        data = resp.json()
        cam = next(d for d in data if d["name"] == "Camera")
        assert cam["status"] == "borrowed"
        assert cam["borrower_name"] == "Admin User"


# ---------------------------------------------------------------------------
# Borrow/Return Endpoint Tests
# ---------------------------------------------------------------------------

class TestBorrowReturn:
    """Tests for borrow/return API — success, auth, ownership, and admin override."""

    def test_borrow_success(self, client, mock_context, test_user, test_devices):
        """Verify POST /api/devices/{id}/borrow succeeds for an available device."""
        mock_context.session_mgr.start_session(test_user)
        resp = client.post(f"/api/devices/{test_devices[0].id}/borrow")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "Camera" in data["message"]

    def test_borrow_no_session(self, client, test_devices):
        """Verify borrow returns 401 when no session exists."""
        resp = client.post(f"/api/devices/{test_devices[0].id}/borrow")
        assert resp.status_code == 401

    def test_borrow_maintenance_device(self, client, mock_context, test_user, test_devices):
        """Cannot borrow a device under maintenance."""
        mock_context.session_mgr.start_session(test_user)
        laptop = test_devices[2]  # MAINTENANCE status
        resp = client.post(f"/api/devices/{laptop.id}/borrow")
        data = resp.json()
        assert data["success"] is False

    def test_borrow_already_borrowed(
        self, client, mock_context, test_user, test_devices, db_session
    ):
        """Cannot borrow a device that's already borrowed."""
        mock_context.session_mgr.start_session(test_user)
        user_session = mock_context.session_mgr.current_session
        LockerService.borrow_device(db_session, user_session, test_devices[0].id)
        db_session.commit()

        resp = client.post(f"/api/devices/{test_devices[0].id}/borrow")
        data = resp.json()
        assert data["success"] is False

    def test_return_success(
        self, client, mock_context, test_user, test_devices, db_session
    ):
        """Verify POST /api/devices/{id}/return succeeds after borrowing."""
        mock_context.session_mgr.start_session(test_user)
        user_session = mock_context.session_mgr.current_session
        LockerService.borrow_device(db_session, user_session, test_devices[0].id)
        db_session.commit()

        resp = client.post(f"/api/devices/{test_devices[0].id}/return")
        data = resp.json()
        assert data["success"] is True
        assert "Camera" in data["message"]

    def test_return_no_session(self, client, test_devices):
        """Verify return returns 401 when no session exists."""
        resp = client.post(f"/api/devices/{test_devices[0].id}/return")
        assert resp.status_code == 401

    def test_return_not_borrowed(self, client, mock_context, test_user, test_devices):
        """Cannot return a device that's not borrowed."""
        mock_context.session_mgr.start_session(test_user)
        resp = client.post(f"/api/devices/{test_devices[0].id}/return")
        data = resp.json()
        assert data["success"] is False

    def test_return_wrong_user(
        self, client, mock_context, test_user, admin_user, test_devices, db_session
    ):
        """Non-admin cannot return another user's device."""
        # Admin borrows
        admin_session = mock_context.session_mgr.start_session(admin_user)
        LockerService.borrow_device(db_session, admin_session, test_devices[0].id)
        db_session.commit()

        # Test user tries to return
        mock_context.session_mgr.start_session(test_user)
        resp = client.post(f"/api/devices/{test_devices[0].id}/return")
        data = resp.json()
        assert data["success"] is False

    def test_admin_return_other_users_device(
        self, client, mock_context, test_user, admin_user, test_devices, db_session
    ):
        """Admin can return another user's device."""
        # Test user borrows
        user_session = mock_context.session_mgr.start_session(test_user)
        LockerService.borrow_device(db_session, user_session, test_devices[0].id)
        db_session.commit()

        # Admin returns
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(f"/api/devices/{test_devices[0].id}/return")
        data = resp.json()
        assert data["success"] is True
