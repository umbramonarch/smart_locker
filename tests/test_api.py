"""
File: test_api.py
Description: Tests for the REST API layer — session management, device listing,
             borrow/return endpoints, user self-registration (with registrant
             validation), admin manual registration, Register Device (PM + slot
             + NFC), registrant list retrieval, admin source sync, software
             update, Exit kiosk / Shut down, dashboard Inventory/Display,
             public owner edit, dashboard NFC bind/unbind (admin secret),
             loopback appliance session/shutdown/exit, and SSE event stream. Uses
             FastAPI's TestClient with mocked NFC context.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_api.py -v
"""

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from smart_locker.api.app_context import PendingRegistration, PendingTagBind
from smart_locker.api.routes import router, get_db, require_session
from smart_locker.auth.session_manager import SessionManager, UserSession
from smart_locker.database.engine import get_session_factory, init_db, reset_engine
from smart_locker.database.models import DeviceStatus, UserRole
from smart_locker.database.repositories import DeviceRepository, RegistrantRepository, UserRepository
from smart_locker.security.hashing import compute_uid_hmac
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
    mock_ctx.admin_overlay_open = False
    mock_ctx.kiosk_screen = "idle"
    mock_ctx.pending_tag_bind = None
    mock_ctx.pending_registration = None
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


def _dashboard_admin_headers(secret: str) -> dict:
    """JSON + X-Smart-Locker-Admin for dashboard bind/unbind."""
    return {
        "Content-Type": "application/json",
        "X-Smart-Locker-Admin": secret,
    }


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
        assert data["overlay"] is False

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
        assert data["overlay"] is False

    def test_end_session(self, client, mock_context, test_user):
        """Verify POST /api/session/end terminates the active session."""
        mock_context.session_mgr.start_session(test_user)
        mock_context.pending_tag_bind = PendingTagBind(device_id=1)
        mock_context.admin_overlay_open = True
        resp = client.post("/api/session/end")
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert not mock_context.session_mgr.has_active_session
        assert mock_context.pending_tag_bind is None
        assert mock_context.admin_overlay_open is False

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
        assert "barcode" not in cam
        assert "calibration_due" in cam
        assert cam["has_tag"] is False
        assert "tag_hmac" not in cam

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

    def test_http_borrow_returns_before_excel_io(
        self, client, mock_context, test_user, test_devices, tmp_path, monkeypatch
    ):
        """POST /api/devices/{id}/borrow returns before Excel write-back I/O."""
        import time

        from openpyxl import Workbook

        from smart_locker.sync import location_writeback as wb
        from smart_locker.sync.location_writeback import (
            WritebackResult,
            flush_scheduled_writeback,
        )

        path = tmp_path / "device-list.xlsx"
        book = Workbook()
        book.active.append(["Equipment", "Location"])
        book.active.append(["PM-001", "Locker"])
        book.save(path)
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))

        def slow_write(engine, source_path):
            time.sleep(0.4)
            return WritebackResult()

        monkeypatch.setattr(wb, "write_location_with_engine", slow_write)
        mock_context.session_mgr.start_session(test_user)
        t0 = time.monotonic()
        resp = client.post(f"/api/devices/{test_devices[0].id}/borrow")
        elapsed = time.monotonic() - t0
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert elapsed < 0.25
        flush_scheduled_writeback()

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


# ---------------------------------------------------------------------------
# Registration & Registrant Endpoint Tests
# ---------------------------------------------------------------------------

class TestRegistrantEndpoints:
    """Tests for the registrant list and registration validation API endpoints."""

    def test_get_registrants_empty(self, client):
        """GET /api/registrants returns empty list when no registrants exist."""
        resp = client.get("/api/registrants")
        assert resp.status_code == 200
        assert resp.json()["names"] == []

    def test_get_registrants_returns_names(self, client, db_session):
        """GET /api/registrants returns names from the registrants table."""
        RegistrantRepository.add_names(db_session, {"Alice", "Bob"})
        db_session.commit()

        resp = client.get("/api/registrants")
        assert resp.status_code == 200
        names = resp.json()["names"]
        assert "Alice" in names
        assert "Bob" in names

    def test_get_registrants_excludes_registered_users(self, client, db_session):
        """GET /api/registrants excludes names that already have a User record."""
        RegistrantRepository.add_names(db_session, {"Alice", "Bob"})
        # "Alice" is already registered as a user
        UserRepository.create(
            db_session,
            display_name="Alice",
            uid_hmac="mmhash" * 10 + "mmmm",
            encrypted_card_uid="encrypted_mm",
        )
        db_session.commit()

        resp = client.get("/api/registrants")
        names = resp.json()["names"]
        # Alice should be excluded, Bob should remain
        assert "Alice" not in names
        assert "Bob" in names

    def test_register_validates_against_registrants(self, client, db_session, mock_context):
        """POST /api/register rejects names not in the registrants list."""
        # No registrants exist — any name should be rejected
        mock_context.pending_registration = None
        resp = client.post("/api/register", json={"name": "Unknown Person"})
        assert resp.status_code == 403
        assert "approved list" in resp.json()["detail"].lower()

    def test_register_accepts_approved_name(self, client, db_session, mock_context):
        """POST /api/register accepts a name that exists in the registrants list."""
        RegistrantRepository.add_names(db_session, {"Alice"})
        db_session.commit()
        mock_context.pending_registration = None

        resp = client.post("/api/register", json={"name": "Alice"})
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_admin_register_accepts_any_name(
        self, client, db_session, mock_context, admin_user
    ):
        """POST /api/admin/register accepts any name without registrant validation."""
        mock_context.session_mgr.start_session(admin_user)
        mock_context.pending_registration = None
        mock_context.pending_tag_bind = None

        resp = client.post("/api/admin/register", json={"name": "Not In List"})
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert mock_context.pending_registration is not None

    def test_admin_register_rejects_armed_bind(
        self, client, db_session, mock_context, admin_user
    ):
        """Admin register does not drop a non-expired device-tag bind window."""
        mock_context.session_mgr.start_session(admin_user)
        mock_context.pending_registration = None
        mock_context.pending_tag_bind = PendingTagBind(device_id=1)

        resp = client.post("/api/admin/register", json={"name": "Not In List"})
        assert resp.status_code == 409
        assert mock_context.pending_tag_bind is not None
        assert mock_context.pending_tag_bind.device_id == 1
        assert mock_context.pending_registration is None

    def test_admin_register_rejects_non_admin(
        self, client, db_session, mock_context, test_user
    ):
        """POST /api/admin/register rejects non-admin users."""
        mock_context.session_mgr.start_session(test_user)

        resp = client.post("/api/admin/register", json={"name": "Someone"})
        assert resp.status_code == 403

    def test_admin_register_requires_session(self, client, mock_context):
        """POST /api/admin/register requires an active session."""
        resp = client.post("/api/admin/register", json={"name": "Someone"})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Admin device-tag bind / unbind
# ---------------------------------------------------------------------------

class TestDeviceTagBindApi:
    """Auth gates for bind/unbind, has_tag on the kiosk list, duplicate names."""

    def test_bind_tag_requires_session(self, client, mock_context, test_devices):
        """POST /api/admin/devices/{id}/bind-tag returns 401 without a session."""
        resp = client.post(f"/api/admin/devices/{test_devices[0].id}/bind-tag")
        assert resp.status_code == 401

    def test_bind_tag_rejects_non_admin(
        self, client, mock_context, test_user, test_devices
    ):
        """POST /api/admin/devices/{id}/bind-tag returns 403 for a normal user."""
        mock_context.session_mgr.start_session(test_user)
        resp = client.post(f"/api/admin/devices/{test_devices[0].id}/bind-tag")
        assert resp.status_code == 403

    def test_bind_tag_accepts_admin(
        self, client, mock_context, admin_user, test_devices
    ):
        """Admin bind-tag sets pending_tag_bind for the chosen device."""
        mock_context.session_mgr.start_session(admin_user)
        mock_context.pending_registration = None
        mock_context.pending_tag_bind = None
        resp = client.post(f"/api/admin/devices/{test_devices[0].id}/bind-tag")
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert mock_context.pending_tag_bind.device_id == test_devices[0].id
        assert mock_context.pending_tag_bind.is_expired is False

    def test_bind_tag_rejects_pending_registration(
        self, client, mock_context, admin_user, test_devices
    ):
        """A new bind arm does not drop an in-progress kiosk enroll."""
        mock_context.session_mgr.start_session(admin_user)
        mock_context.pending_registration = PendingRegistration("Someone")
        mock_context.pending_tag_bind = None
        resp = client.post(f"/api/admin/devices/{test_devices[0].id}/bind-tag")
        assert resp.status_code == 409
        assert mock_context.pending_registration is not None
        assert mock_context.pending_registration.display_name == "Someone"
        assert mock_context.pending_tag_bind is None

    def test_bind_tag_unknown_device(self, client, mock_context, admin_user):
        """POST bind-tag returns 404 when the device id does not exist."""
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post("/api/admin/devices/99999/bind-tag")
        assert resp.status_code == 404

    def test_unbind_tag_requires_session(self, client, mock_context, test_devices):
        """POST /api/admin/devices/{id}/unbind-tag returns 401 without a session."""
        resp = client.post(f"/api/admin/devices/{test_devices[0].id}/unbind-tag")
        assert resp.status_code == 401

    def test_unbind_tag_rejects_non_admin(
        self, client, mock_context, test_user, test_devices
    ):
        """POST /api/admin/devices/{id}/unbind-tag returns 403 for a normal user."""
        mock_context.session_mgr.start_session(test_user)
        resp = client.post(f"/api/admin/devices/{test_devices[0].id}/unbind-tag")
        assert resp.status_code == 403

    def test_unbind_tag_accepts_admin(
        self, client, mock_context, admin_user, test_devices, db_session, hmac_key
    ):
        """Admin unbind-tag clears tag_hmac; GET list has has_tag False."""
        DeviceRepository.bind_tag(
            db_session,
            test_devices[0],
            compute_uid_hmac("AABBCCDD", hmac_key),
        )
        db_session.commit()
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(f"/api/admin/devices/{test_devices[0].id}/unbind-tag")
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        db_session.expire_all()
        assert test_devices[0].tag_hmac is None
        listed = client.get("/api/devices").json()
        cam = next(d for d in listed if d["name"] == "Camera")
        assert cam["has_tag"] is False
        assert "tag_hmac" not in cam
        assert mock_context.pending_tag_bind is None

    def test_unbind_tag_clears_pending_bind(
        self, client, mock_context, admin_user, test_devices, db_session, hmac_key
    ):
        """Kiosk unbind cancels an armed bind window so the next tap is not rebound."""
        DeviceRepository.bind_tag(
            db_session,
            test_devices[0],
            compute_uid_hmac("AABBCCDD", hmac_key),
        )
        db_session.commit()
        mock_context.session_mgr.start_session(admin_user)
        mock_context.pending_tag_bind = PendingTagBind(device_id=test_devices[0].id)
        resp = client.post(f"/api/admin/devices/{test_devices[0].id}/unbind-tag")
        assert resp.status_code == 200
        assert mock_context.pending_tag_bind is None

    def test_unbind_tag_unknown_device(self, client, mock_context, admin_user):
        """POST unbind-tag returns 404 when the device id does not exist."""
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post("/api/admin/devices/99999/unbind-tag")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Admin Register Device (PM + slot + NFC)
# ---------------------------------------------------------------------------

def _catalog_workbook(tmp_path, rows):
    """Write a temporary device-list.xlsx and return its path."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    path = tmp_path / "device-list.xlsx"
    wb.save(path)
    return path


class TestRegisterDeviceApi:
    """Admin POST /api/admin/devices/register creates a locker row from Excel."""

    def test_register_requires_session(self, client, mock_context):
        resp = client.post(
            "/api/admin/devices/register",
            json={"pm_number": "PM-001", "locker_slot": 1},
        )
        assert resp.status_code == 401

    def test_register_rejects_non_admin(self, client, mock_context, test_user):
        mock_context.session_mgr.start_session(test_user)
        resp = client.post(
            "/api/admin/devices/register",
            json={"pm_number": "PM-001", "locker_slot": 1},
        )
        assert resp.status_code == 403

    def test_register_accepts_admin(
        self, client, mock_context, admin_user, db_session, tmp_path, monkeypatch
    ):
        """Known PM + free slot inserts the locker row and arms the sticker bind."""
        path = _catalog_workbook(tmp_path, [
            ["Equipment", "Manufacturer", "Model"],
            ["PM-XL", "Fluke", "87V"],
        ])
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))
        mock_context.session_mgr.start_session(admin_user)
        mock_context.pending_tag_bind = None
        resp = client.post(
            "/api/admin/devices/register",
            json={"pm_number": "PM-XL", "locker_slot": 7},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["pm_number"] == "PM-XL"
        assert body["locker_slot"] == 7
        device = DeviceRepository.find_by_pm(db_session, "PM-XL")
        assert device is not None
        assert device.locker_slot == 7
        assert mock_context.pending_tag_bind.device_id == device.id

    def test_register_unknown_pm(
        self, client, mock_context, admin_user, db_session, tmp_path, monkeypatch
    ):
        path = _catalog_workbook(tmp_path, [
            ["Equipment", "Manufacturer"],
            ["PM-001", "Fluke"],
        ])
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(
            "/api/admin/devices/register",
            json={"pm_number": "PM-MISSING", "locker_slot": 1},
        )
        assert resp.status_code == 404
        assert DeviceRepository.find_by_pm(db_session, "PM-MISSING") is None

    def test_register_duplicate_slot(
        self, client, mock_context, admin_user, test_devices, tmp_path, monkeypatch
    ):
        path = _catalog_workbook(tmp_path, [
            ["Equipment", "Manufacturer"],
            ["PM-NEW", "Keysight"],
        ])
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(
            "/api/admin/devices/register",
            json={"pm_number": "PM-NEW", "locker_slot": test_devices[0].locker_slot},
        )
        assert resp.status_code == 409

    def test_set_slot_accepts_admin(
        self, client, mock_context, admin_user, test_devices, db_session
    ):
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(
            f"/api/admin/devices/{test_devices[0].id}/slot",
            json={"locker_slot": 9},
        )
        assert resp.status_code == 200
        db_session.expire_all()
        assert test_devices[0].locker_slot == 9

    def test_set_slot_requires_session(self, client, mock_context, test_devices):
        resp = client.post(
            f"/api/admin/devices/{test_devices[0].id}/slot",
            json={"locker_slot": 9},
        )
        assert resp.status_code == 401

    def test_register_slot_over_cap_is_422(
        self, client, mock_context, admin_user
    ):
        """locker_slot=999999 is rejected before insert (I22)."""
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(
            "/api/admin/devices/register",
            json={"pm_number": "PM-001", "locker_slot": 999999},
        )
        assert resp.status_code == 422

    def test_set_slot_over_cap_is_422(
        self, client, mock_context, admin_user, test_devices
    ):
        """Change-slot also enforces MAX_LOCKER_SLOT."""
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(
            f"/api/admin/devices/{test_devices[0].id}/slot",
            json={"locker_slot": 999999},
        )
        assert resp.status_code == 422


class TestAdminOverlaySession:
    """Admin overlay session flag and device-list payload used by Register Device."""

    def test_admin_session_defaults_overlay_true(
        self, client, mock_context, admin_user
    ):
        """POST /api/admin/session defaults admin_overlay_open to True."""
        resp = client.post("/api/admin/session")
        assert resp.status_code == 200
        assert mock_context.admin_overlay_open is True
        assert mock_context.session_mgr.has_active_session
        assert mock_context.session_mgr.current_session.user.id == admin_user.id

    def test_admin_session_overlay_false(
        self, client, mock_context, admin_user
    ):
        """POST /api/admin/session?overlay=false clears the overlay flag."""
        resp = client.post("/api/admin/session?overlay=false")
        assert resp.status_code == 200
        assert mock_context.admin_overlay_open is False

    def test_admin_session_existing_keeps_user(
        self, client, mock_context, test_user, admin_user
    ):
        """Non-admin session: overlay POST is refused; sticker auto-intent stays on."""
        mock_context.session_mgr.start_session(test_user)
        mock_context.admin_overlay_open = False
        resp = client.post("/api/admin/session?overlay=true")
        assert resp.status_code == 403
        assert mock_context.admin_overlay_open is False
        assert mock_context.session_mgr.current_session.user.id == test_user.id

    def test_admin_session_admin_may_update_overlay(
        self, client, mock_context, admin_user
    ):
        """Loopback overlay flag update is allowed for an already-logged-in admin."""
        mock_context.session_mgr.start_session(admin_user)
        mock_context.admin_overlay_open = True
        resp = client.post("/api/admin/session?overlay=false")
        assert resp.status_code == 200
        assert mock_context.admin_overlay_open is False
        assert mock_context.session_mgr.current_session.user.id == admin_user.id

    def test_admin_session_refuses_lan(
        self, lan_client, mock_context, admin_user
    ):
        """LAN callers cannot create the process-global admin session."""
        resp = lan_client.post("/api/admin/session")
        assert resp.status_code == 403
        assert not mock_context.session_mgr.has_active_session

    def test_admin_session_lan_cannot_flip_overlay(
        self, lan_client, mock_context, test_user, admin_user
    ):
        """LAN overlay=true is not auth and must not change an existing session."""
        mock_context.session_mgr.start_session(test_user)
        mock_context.admin_overlay_open = False
        resp = lan_client.post("/api/admin/session?overlay=true")
        assert resp.status_code == 403
        assert mock_context.admin_overlay_open is False
        assert mock_context.session_mgr.current_session.user.id == test_user.id

    def test_register_cancel_clears_pending_tag_bind(self, client, mock_context):
        """POST /api/register/cancel clears a pending device-tag bind."""
        mock_context.pending_tag_bind = PendingTagBind(device_id=1)
        resp = client.post("/api/register/cancel")
        assert resp.status_code == 200
        assert mock_context.pending_tag_bind is None

    def test_list_devices_duplicate_name_distinct_pm(
        self, client, mock_context, test_user, db_session
    ):
        """Same name, different PM — both rows in the bind-list payload."""
        DeviceRepository.create(
            db_session,
            name="Fluke 87V",
            device_type="Multimeter",
            pm_number="PM-101",
            locker_slot=1,
        )
        DeviceRepository.create(
            db_session,
            name="Fluke 87V",
            device_type="Multimeter",
            pm_number="PM-102",
            locker_slot=2,
        )
        db_session.commit()
        mock_context.session_mgr.start_session(test_user)
        resp = client.get("/api/devices")
        assert resp.status_code == 200
        rows = [d for d in resp.json() if d["name"] == "Fluke 87V"]
        assert len(rows) == 2
        pms = {d["pm_number"] for d in rows}
        assert pms == {"PM-101", "PM-102"}
        for d in rows:
            assert d["name"] == "Fluke 87V"
            assert "pm_number" in d
            assert "has_tag" in d
            assert "tag_hmac" not in d

    def test_list_devices_has_tag_true(
        self, client, mock_context, test_user, test_devices, db_session, hmac_key
    ):
        """has_tag is True when tag_hmac is set; digest is not in the payload."""
        DeviceRepository.bind_tag(
            db_session,
            test_devices[0],
            compute_uid_hmac("AABBCCDD", hmac_key),
        )
        db_session.commit()
        mock_context.session_mgr.start_session(test_user)
        resp = client.get("/api/devices")
        cam = next(d for d in resp.json() if d["name"] == "Camera")
        assert cam["has_tag"] is True
        assert "tag_hmac" not in cam

    def test_dashboard_devices_omits_tag_hmac(
        self, client, test_devices, db_session, hmac_key
    ):
        """Public dashboard JSON must not include tag_hmac after a bind."""
        digest = compute_uid_hmac("AABBCCDD", hmac_key)
        DeviceRepository.bind_tag(db_session, test_devices[0], digest)
        db_session.commit()
        resp = client.get("/api/dashboard/devices")
        assert resp.status_code == 200
        rows = resp.json()
        assert rows
        for row in rows:
            assert "tag_hmac" not in row
            assert "barcode" not in row
            assert "has_tag" in row
            dumped = str(row)
            assert digest not in dumped
        cam = next(r for r in rows if r["name"] == "Camera")
        assert cam["has_tag"] is True


# ---------------------------------------------------------------------------
# Dev / Simulation Endpoint Tests
# ---------------------------------------------------------------------------

class TestDevEndpoints:
    """Tests for the no-hardware simulation endpoints -- inert unless the fake
    reader is enabled AND the running reader is actually the fake one, so
    production is unaffected regardless of who can reach the kiosk's HTTP port.
    """

    def test_dev_status_inactive_by_default(self, client, mock_context, monkeypatch):
        """GET /api/dev/status reports inactive when SMART_LOCKER_FAKE_READER is unset.

        Forces the flag unset via monkeypatch rather than relying on the ambient
        host .env -- a dev box left with SMART_LOCKER_FAKE_READER=1 from a prior
        simulation session must not silently make this test pass for the wrong
        reason (production kiosks must never have this flag on either).
        """
        monkeypatch.delenv("SMART_LOCKER_FAKE_READER", raising=False)
        mock_context.reader = None
        resp = client.get("/api/dev/status")
        assert resp.status_code == 200
        assert resp.json()["fake_reader"] is False

    def test_dev_tap_404_by_default(self, client, mock_context, monkeypatch):
        """POST /api/dev/tap 404s when the simulation harness is not active (production posture)."""
        monkeypatch.delenv("SMART_LOCKER_FAKE_READER", raising=False)
        mock_context.reader = None
        resp = client.post("/api/dev/tap", json={"uid": "AABBCCDD"})
        assert resp.status_code == 404

    def test_dev_tap_404_when_flag_set_but_reader_not_fake(self, client, mock_context, monkeypatch):
        """The env flag alone is not enough -- the running reader must actually be the fake one."""
        monkeypatch.setenv("SMART_LOCKER_FAKE_READER", "1")
        mock_context.reader = object()  # no simulate_tap -- not a fake reader
        resp = client.post("/api/dev/tap", json={"uid": "AABBCCDD"})
        assert resp.status_code == 404

    def test_dev_tap_enqueues_card_event(self, client, mock_context, monkeypatch):
        """POST /api/dev/tap simulates a real tap: the fake reader enqueues a CardEvent(INSERTED)."""
        from smart_locker.nfc.card_observer import CardEvent, CardEventType
        from smart_locker.nfc.fake_reader import FakeNFCReader

        monkeypatch.setenv("SMART_LOCKER_FAKE_READER", "1")
        reader = FakeNFCReader()
        mock_context.reader = reader

        resp = client.post("/api/dev/tap", json={"uid": "AABBCCDD"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        event = reader.poll_event()
        assert isinstance(event, CardEvent)
        assert event.event_type == CardEventType.INSERTED
        assert event.uid == "AABBCCDD"

    def test_dev_tap_no_uid_available(self, client, mock_context, monkeypatch):
        """POST /api/dev/tap 400s when no UID is supplied and no default is configured."""
        from smart_locker.nfc.fake_reader import FakeNFCReader

        monkeypatch.setenv("SMART_LOCKER_FAKE_READER", "1")
        monkeypatch.delenv("SMART_LOCKER_FAKE_DEFAULT_UID", raising=False)
        mock_context.reader = FakeNFCReader()

        resp = client.post("/api/dev/tap", json={})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Admin Sync / Update Endpoint Tests
# ---------------------------------------------------------------------------

class TestAdminSyncAndUpdateEndpoints:
    """Auth-gate tests for the sync-preview, sync-status, and software-update
    admin endpoints -- these read/mutate sync state or launch a root-privileged
    updater, so require_session + the admin-role check is the only thing
    standing between a LAN client and those actions.
    """

    def test_sync_preview_requires_session(self, client, mock_context):
        resp = client.post("/api/admin/sync-preview")
        assert resp.status_code == 401

    def test_sync_preview_rejects_non_admin(self, client, mock_context, test_user):
        mock_context.session_mgr.start_session(test_user)
        resp = client.post("/api/admin/sync-preview")
        assert resp.status_code == 403

    def test_sync_status_requires_session(self, client, mock_context):
        resp = client.get("/api/admin/sync-status")
        assert resp.status_code == 401

    def test_sync_status_rejects_non_admin(self, client, mock_context, test_user):
        mock_context.session_mgr.start_session(test_user)
        resp = client.get("/api/admin/sync-status")
        assert resp.status_code == 403

    def test_sync_status_accepts_admin(self, client, mock_context, admin_user):
        mock_context.session_mgr.start_session(admin_user)
        resp = client.get("/api/admin/sync-status")
        assert resp.status_code == 200

    def test_sync_status_includes_local_and_relative(self, client, mock_context, admin_user):
        """Admin last-sync JSON includes at_local and ago for the footer clock."""
        from types import SimpleNamespace

        from smart_locker.sync import sync_status

        sync_status.record_result(
            "manual",
            SimpleNamespace(imported=0, updated=1, unchanged=0, errors=0),
        )
        mock_context.session_mgr.start_session(admin_user)
        row = client.get("/api/admin/sync-status").json()
        assert row["at"]
        assert row["at_local"]
        assert row["ago"]
        assert row["trigger"] == "manual"

    def test_update_status_requires_session(self, client, mock_context):
        resp = client.get("/api/admin/update-status")
        assert resp.status_code == 401

    def test_update_status_rejects_non_admin(self, client, mock_context, test_user):
        mock_context.session_mgr.start_session(test_user)
        resp = client.get("/api/admin/update-status")
        assert resp.status_code == 403

    def test_update_status_accepts_admin(self, client, mock_context, admin_user):
        mock_context.session_mgr.start_session(admin_user)
        resp = client.get("/api/admin/update-status")
        assert resp.status_code == 200
        assert resp.json()["state"] == "idle"

    def test_health_exposes_update_without_session(self, client, mock_context):
        """Kiosk overlay reads update verdict from public /api/health after restart."""
        resp = client.get("/api/health")
        assert resp.status_code == 200
        body = resp.json()
        assert "status" in body
        assert "update" in body

    def test_trigger_update_requires_session(self, client, mock_context):
        resp = client.post("/api/admin/update")
        assert resp.status_code == 401

    def test_trigger_update_rejects_non_admin(self, client, mock_context, test_user):
        mock_context.session_mgr.start_session(test_user)
        resp = client.post("/api/admin/update")
        assert resp.status_code == 403

    def test_trigger_update_unavailable_off_pi(self, client, mock_context, admin_user, monkeypatch):
        """When systemd-run isn't present (dev/CI host), the endpoint refuses cleanly
        (503) rather than pretending to update -- it must never fall through to
        attempting the privileged update path off the Pi."""
        import smart_locker.api.routes as routes_module

        monkeypatch.setattr(routes_module, "_SYSTEMD_RUN", None)
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post("/api/admin/update")
        assert resp.status_code == 503

    def test_exit_kiosk_requires_session(self, client, mock_context):
        resp = client.post("/api/admin/exit-kiosk")
        assert resp.status_code == 401

    def test_exit_kiosk_rejects_non_admin(self, client, mock_context, test_user):
        mock_context.session_mgr.start_session(test_user)
        resp = client.post("/api/admin/exit-kiosk")
        assert resp.status_code == 403

    def test_exit_kiosk_unavailable_off_pi(self, client, mock_context, admin_user, monkeypatch):
        """Dev/Windows hosts must not try to kill a browser — 503, not a hang."""
        import smart_locker.api.routes as routes_module
        from smart_locker.services.appliance import ApplianceUnavailable

        def _boom():
            raise ApplianceUnavailable(
                "Kiosk exit runs on the Raspberry Pi appliance only."
            )

        monkeypatch.setattr(routes_module, "exit_kiosk", _boom)
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post("/api/admin/exit-kiosk")
        assert resp.status_code == 503

    def test_exit_kiosk_accepts_admin(self, client, mock_context, admin_user, monkeypatch):
        import smart_locker.api.routes as routes_module

        monkeypatch.setattr(routes_module, "exit_kiosk", lambda: None)
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post("/api/admin/exit-kiosk")
        assert resp.status_code == 200
        assert resp.json().get("ok") is True

    def test_exit_kiosk_refuses_lan(
        self, lan_client, mock_context, admin_user, monkeypatch
    ):
        """LAN cannot stop Chromium even with a live admin session."""
        import smart_locker.api.routes as routes_module

        monkeypatch.setattr(routes_module, "exit_kiosk", lambda: None)
        mock_context.session_mgr.start_session(admin_user)
        resp = lan_client.post("/api/admin/exit-kiosk")
        assert resp.status_code == 403

    def test_shutdown_requires_session(self, client, mock_context):
        resp = client.post("/api/admin/shutdown")
        assert resp.status_code == 401

    def test_shutdown_rejects_non_admin(self, client, mock_context, test_user):
        mock_context.session_mgr.start_session(test_user)
        resp = client.post("/api/admin/shutdown")
        assert resp.status_code == 403

    def test_shutdown_unavailable_off_pi(self, client, mock_context, admin_user, monkeypatch):
        """Dev/Windows hosts must not invoke poweroff — 503, not a hang."""
        import smart_locker.api.routes as routes_module
        from smart_locker.services.appliance import ApplianceUnavailable

        def _boom():
            raise ApplianceUnavailable(
                "Shut down runs on the Raspberry Pi appliance only."
            )

        monkeypatch.setattr(routes_module, "appliance_shutdown", _boom)
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post("/api/admin/shutdown")
        assert resp.status_code == 503

    def test_shutdown_accepts_admin(self, client, mock_context, admin_user, monkeypatch):
        import smart_locker.api.routes as routes_module

        monkeypatch.setattr(routes_module, "appliance_shutdown", lambda: None)
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post("/api/admin/shutdown")
        assert resp.status_code == 200
        assert resp.json().get("ok") is True

    def test_shutdown_refuses_lan(
        self, lan_client, mock_context, admin_user, monkeypatch
    ):
        """LAN cannot power off even with a live admin session."""
        import smart_locker.api.routes as routes_module

        monkeypatch.setattr(routes_module, "appliance_shutdown", lambda: None)
        mock_context.session_mgr.start_session(admin_user)
        resp = lan_client.post("/api/admin/shutdown")
        assert resp.status_code == 403


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


class TestDashboardInventoryAndDisplay:
    """Inventory is Excel; Locker is SQLite; Display is a kiosk snapshot."""

    def test_inventory_is_excel_not_sqlite(
        self, client, test_devices, tmp_path, monkeypatch
    ):
        """Inventory includes Excel-only PMs that are not locker rows."""
        path = _catalog_workbook(tmp_path, [
            ["PM", "Name", "Manufacturer", "Model", "Serial", "Location", "Calibration due"],
            ["PM-001", "Scope", "Keysight", "DSOX", "SN-1", "Locker", "2026-01-01"],
            ["PM-999", "Van kit", "Fluke", "87V", "SN-9", "Workshop", None],
        ])
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))
        resp = client.get("/api/dashboard/inventory")
        assert resp.status_code == 200
        rows = resp.json()
        pms = {r["pm_number"] for r in rows}
        assert "PM-001" in pms
        assert "PM-999" in pms
        van = next(r for r in rows if r["pm_number"] == "PM-999")
        assert van["name"] == "Van kit"
        assert van["location"] == "Workshop"
        assert van["in_locker"] is False
        locker = next(r for r in rows if r["pm_number"] == "PM-001")
        assert locker["in_locker"] is True
        assert "status" not in van
        assert "locker_slot" not in van
        assert "tag_hmac" not in van

    def test_locker_stays_sqlite_only(
        self, client, test_devices, tmp_path, monkeypatch
    ):
        """Locker tab JSON is SQLite devices; Excel-only PMs are absent."""
        path = _catalog_workbook(tmp_path, [
            ["PM", "Name", "Location"],
            ["PM-001", "Scope", "Locker"],
            ["PM-999", "Van kit", "Workshop"],
        ])
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))
        resp = client.get("/api/dashboard/devices")
        assert resp.status_code == 200
        pms = {r["pm_number"] for r in resp.json()}
        assert "PM-001" in pms
        assert "PM-999" not in pms
        row = next(r for r in resp.json() if r["pm_number"] == "PM-001")
        assert "locker_slot" in row
        assert "status" in row

    def test_inventory_share_down_does_not_break_locker(
        self, client, test_devices, tmp_path, monkeypatch
    ):
        """Missing catalog → Inventory errors; Locker still answers."""
        monkeypatch.setattr(
            "config.settings.SOURCE_EXCEL_PATH", str(tmp_path / "missing.xlsx")
        )
        inv = client.get("/api/dashboard/inventory")
        assert inv.status_code == 503
        locker = client.get("/api/dashboard/devices")
        assert locker.status_code == 200
        assert locker.json()

    def test_inventory_unconfigured_is_error(self, client, monkeypatch):
        """Empty SOURCE_EXCEL_PATH is an Inventory error, not an empty catalog."""
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", "")
        resp = client.get("/api/dashboard/inventory")
        assert resp.status_code == 503

    def test_display_idle_has_no_user(self, client, mock_context):
        """Idle kiosk reports Idle and no current user."""
        mock_context.kiosk_screen = "idle"
        mock_context.admin_overlay_open = False
        resp = client.get("/api/dashboard/display")
        assert resp.status_code == 200
        data = resp.json()
        assert data["screen"] == "idle"
        assert data["label"] == "Idle"
        assert data["user_name"] is None

    def test_display_shows_session_user(
        self, client, mock_context, test_user
    ):
        """Logged-in kiosk reports the current user on Display."""
        mock_context.session_mgr.start_session(test_user)
        mock_context.kiosk_screen = "main-menu"
        resp = client.get("/api/dashboard/display")
        assert resp.status_code == 200
        data = resp.json()
        assert data["screen"] == "main-menu"
        assert data["label"] == "Main menu"
        assert data["user_name"] == "Test User"

    def test_heartbeat_updates_display(self, client, mock_context):
        """Kiosk POST of a screen id is what Display polls."""
        resp = client.post("/api/kiosk/display", json={"screen": "borrow"})
        assert resp.status_code == 200
        assert mock_context.kiosk_screen == "borrow"
        shown = client.get("/api/dashboard/display").json()
        assert shown["screen"] == "borrow"
        assert shown["label"] == "Locker"

    def test_display_admin_overlay_overrides_heartbeat(self, client, mock_context):
        """Open admin overlay is Admin even if the last heartbeat was idle."""
        mock_context.kiosk_screen = "idle"
        mock_context.admin_overlay_open = True
        data = client.get("/api/dashboard/display").json()
        assert data["screen"] == "admin"
        assert data["label"] == "Admin"

    def test_heartbeat_from_lan_is_403(self, lan_client, mock_context):
        """Display heartbeat is kiosk-loopback only."""
        mock_context.kiosk_screen = "idle"
        resp = lan_client.post("/api/kiosk/display", json={"screen": "borrow"})
        assert resp.status_code == 403
        assert mock_context.kiosk_screen == "idle"

    def test_heartbeat_null_context_is_ok(self, client, monkeypatch):
        """Missing AppContext on POST is 200, not 500."""
        monkeypatch.setattr(ctx_module, "context", None)
        resp = client.post("/api/kiosk/display", json={"screen": "idle"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


class TestDashboardOwnerEditApi:
    """POST /api/dashboard/owner requires the dashboard admin secret."""

    def test_owner_change_without_secret_is_401(
        self, client, tmp_path, monkeypatch
    ):
        """Unauthenticated owner write is 401, not 200."""
        path = _catalog_workbook(tmp_path, [
            ["Equipment", "Name", "Location"],
            ["PM-VAN", "Van kit", "Workshop"],
        ])
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))
        resp = client.post(
            "/api/dashboard/owner",
            json={"pm_number": "PM-VAN", "owner": "Alex"},
        )
        assert resp.status_code == 401

    def test_owner_change_with_secret(
        self, client, tmp_path, monkeypatch, dashboard_secret
    ):
        """Authorized owner write still does not need a kiosk work-card session."""
        path = _catalog_workbook(tmp_path, [
            ["Equipment", "Name", "Location"],
            ["PM-VAN", "Van kit", "Workshop"],
        ])
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))
        resp = client.post(
            "/api/dashboard/owner",
            json={"pm_number": "PM-VAN", "owner": "Alex"},
            headers=_dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["locker"] is False

    def test_non_locker_does_not_create_sqlite_row(
        self, client, db_session, tmp_path, monkeypatch, dashboard_secret
    ):
        """Excel-only PMs stay off the Pi after an Inventory owner edit."""
        path = _catalog_workbook(tmp_path, [
            ["Equipment", "Name", "Location"],
            ["PM-VAN", "Van kit", "Workshop"],
        ])
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))
        client.post(
            "/api/dashboard/owner",
            json={"pm_number": "PM-VAN", "owner": "Alex"},
            headers=_dashboard_admin_headers(dashboard_secret),
        )
        assert DeviceRepository.find_by_pm(db_session, "PM-VAN") is None

    def test_locker_pm_is_refused(
        self, client, test_user, test_devices, tmp_path, monkeypatch, db_session,
        dashboard_secret,
    ):
        """Locker PMs cannot have owner changed from the dashboard."""
        from smart_locker.database.models import TransactionLog
        from sqlalchemy import select

        path = _catalog_workbook(tmp_path, [
            ["Equipment", "Name", "Location"],
            ["PM-001", "Camera", "Locker"],
        ])
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))
        db_session.commit()
        resp = client.post(
            "/api/dashboard/owner",
            json={"pm_number": "PM-001", "owner": "Test User"},
            headers=_dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 409
        db_session.expire_all()
        device = DeviceRepository.find_by_pm(db_session, "PM-001")
        assert device.status == DeviceStatus.AVAILABLE
        assert device.current_borrower_id is None
        logs = db_session.execute(select(TransactionLog)).scalars().all()
        assert logs == []

    def test_owners_list_requires_secret(
        self, client, test_user, db_session
    ):
        """GET /api/dashboard/owners is not public."""
        RegistrantRepository.add_names(db_session, {"Bob Field"})
        db_session.commit()
        resp = client.get("/api/dashboard/owners")
        assert resp.status_code == 401

    def test_owners_list_with_secret(
        self, client, test_user, db_session, dashboard_secret
    ):
        """Dropdown names: registered users + registrants."""
        RegistrantRepository.add_names(db_session, {"Bob Field"})
        db_session.commit()
        resp = client.get(
            "/api/dashboard/owners",
            headers=_dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        names = resp.json()["names"]
        assert "Test User" in names
        assert "Bob Field" in names
        assert "in_locker_token" in resp.json()

    def test_users_and_transactions_require_secret(self, client):
        """Users and last-500 transactions are not public GETs."""
        assert client.get("/api/dashboard/users").status_code == 401
        assert client.get("/api/dashboard/transactions").status_code == 401

    def test_users_and_transactions_with_secret(
        self, client, test_user, dashboard_secret
    ):
        """Authorized GET returns users without hmac/uid fields."""
        users = client.get(
            "/api/dashboard/users",
            headers=_dashboard_admin_headers(dashboard_secret),
        )
        assert users.status_code == 200
        row = next(u for u in users.json() if u["display_name"] == "Test User")
        assert "uid_hmac" not in row
        tx = client.get(
            "/api/dashboard/transactions",
            headers=_dashboard_admin_headers(dashboard_secret),
        )
        assert tx.status_code == 200
        assert isinstance(tx.json(), list)

    def test_share_down_is_error(self, client, monkeypatch, tmp_path, dashboard_secret):
        """Missing catalog Excel is 503; does not invent a locker row."""
        monkeypatch.setattr(
            "config.settings.SOURCE_EXCEL_PATH", str(tmp_path / "missing.xlsx")
        )
        resp = client.post(
            "/api/dashboard/owner",
            json={"pm_number": "PM-001", "owner": "Alex"},
            headers=_dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 503


class TestDashboardTagApi:
    """Dashboard bind/unbind require the admin secret; overlay is not auth."""

    def test_bind_tag_without_secret_is_401(self, client, mock_context, test_devices):
        """POST /api/dashboard/bind-tag is 401 when the secret is unset."""
        mock_context.pending_tag_bind = None
        resp = client.post(
            "/api/dashboard/bind-tag",
            json={"pm_number": "PM-001"},
        )
        assert resp.status_code == 401
        assert mock_context.pending_tag_bind is None

    def test_unbind_tag_without_secret_is_401(
        self, client, test_devices, db_session, hmac_key
    ):
        """POST /api/dashboard/unbind-tag is 401 when the secret is unset."""
        DeviceRepository.bind_tag(
            db_session,
            test_devices[0],
            compute_uid_hmac("AABBCCDD", hmac_key),
        )
        db_session.commit()
        resp = client.post(
            "/api/dashboard/unbind-tag",
            json={"pm_number": "PM-001"},
        )
        assert resp.status_code == 401
        db_session.expire_all()
        assert test_devices[0].tag_hmac is not None

    def test_bind_tag_wrong_secret_is_401(
        self, client, mock_context, test_devices, dashboard_secret
    ):
        """Wrong header value is 401, not a successful arm."""
        mock_context.pending_tag_bind = None
        resp = client.post(
            "/api/dashboard/bind-tag",
            json={"pm_number": "PM-001"},
            headers=_dashboard_admin_headers("wrong-secret-value"),
        )
        assert resp.status_code == 401
        assert mock_context.pending_tag_bind is None

    def test_bind_tag_with_secret_arms(
        self, client, mock_context, test_devices, dashboard_secret
    ):
        """Configured secret arms pending_tag_bind without a kiosk session."""
        mock_context.pending_tag_bind = None
        resp = client.post(
            "/api/dashboard/bind-tag",
            json={"pm_number": "PM-001"},
            headers=_dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        assert resp.json().get("ok") is True
        assert mock_context.pending_tag_bind is not None
        assert mock_context.pending_tag_bind.device_id == test_devices[0].id
        assert mock_context.pending_tag_bind.is_expired is False

    def test_unbind_tag_with_secret_clears_hmac(
        self, client, test_devices, db_session, hmac_key, dashboard_secret
    ):
        """Authorized unbind clears tag_hmac; has_tag becomes False."""
        DeviceRepository.bind_tag(
            db_session,
            test_devices[0],
            compute_uid_hmac("AABBCCDD", hmac_key),
        )
        db_session.commit()
        resp = client.post(
            "/api/dashboard/unbind-tag",
            json={"pm_number": "PM-001"},
            headers=_dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        assert resp.json().get("ok") is True
        db_session.expire_all()
        assert test_devices[0].tag_hmac is None
        listed = client.get("/api/dashboard/devices").json()
        cam = next(d for d in listed if d["name"] == "Camera")
        assert cam["has_tag"] is False
        assert "tag_hmac" not in cam

    def test_unbind_clears_pending_tag_bind(
        self, client, mock_context, test_devices, db_session, hmac_key, dashboard_secret
    ):
        """Dashboard unbind cancels an armed bind window."""
        DeviceRepository.bind_tag(
            db_session,
            test_devices[0],
            compute_uid_hmac("AABBCCDD", hmac_key),
        )
        db_session.commit()
        mock_context.pending_tag_bind = PendingTagBind(device_id=test_devices[0].id)
        resp = client.post(
            "/api/dashboard/unbind-tag",
            json={"pm_number": "PM-001"},
            headers=_dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        assert mock_context.pending_tag_bind is None

    def test_bind_refused_while_kiosk_session_active(
        self, client, mock_context, test_user, test_devices, dashboard_secret
    ):
        """Dashboard bind must not arm while someone is logged in at the kiosk."""
        mock_context.session_mgr.start_session(test_user)
        mock_context.pending_tag_bind = None
        resp = client.post(
            "/api/dashboard/bind-tag",
            json={"pm_number": "PM-001"},
            headers=_dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 409
        assert mock_context.pending_tag_bind is None

    def test_bind_does_not_drop_pending_registration(
        self, client, mock_context, test_devices, dashboard_secret
    ):
        """Dashboard bind must not clear an in-progress kiosk enroll."""
        mock_context.pending_registration = PendingRegistration("Someone")
        mock_context.pending_tag_bind = None
        resp = client.post(
            "/api/dashboard/bind-tag",
            json={"pm_number": "PM-001"},
            headers=_dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 409
        assert mock_context.pending_registration is not None
        assert mock_context.pending_registration.display_name == "Someone"
        assert mock_context.pending_tag_bind is None

    def test_second_bind_rejected_while_armed(
        self, client, mock_context, test_devices, dashboard_secret
    ):
        """Last-writer-wins is refused: a non-expired bind blocks a new arm."""
        mock_context.pending_tag_bind = PendingTagBind(device_id=test_devices[1].id)
        resp = client.post(
            "/api/dashboard/bind-tag",
            json={"pm_number": "PM-001"},
            headers=_dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 409
        assert mock_context.pending_tag_bind.device_id == test_devices[1].id

    def test_unknown_pm_is_404(
        self, client, mock_context, dashboard_secret
    ):
        """Bind/unbind of a PM that is not a locker device is 404."""
        resp = client.post(
            "/api/dashboard/bind-tag",
            json={"pm_number": "PM-MISSING"},
            headers=_dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 404
        resp = client.post(
            "/api/dashboard/unbind-tag",
            json={"pm_number": "PM-MISSING"},
            headers=_dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 404
        assert mock_context.pending_tag_bind is None

