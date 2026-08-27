"""
File: test_api.py
Description: Tests for the REST API layer — session management, device listing,
             borrow/return endpoints, user self-registration (with registrant
             validation), admin manual registration, Register Device (PM + slot
             + NFC), registrant list retrieval, admin source sync, software
             update, Exit kiosk / Shut down, and SSE event stream. Uses
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
    mock_ctx.pending_tag_bind = None
    mock_ctx.pending_registration = None
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
        mock_context.pending_tag_bind = PendingTagBind(device_id=1)

        resp = client.post("/api/admin/register", json={"name": "Not In List"})
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert mock_context.pending_tag_bind is None

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
        mock_context.pending_registration = PendingRegistration("Someone")
        mock_context.pending_tag_bind = None
        resp = client.post(f"/api/admin/devices/{test_devices[0].id}/bind-tag")
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert mock_context.pending_registration is None
        assert mock_context.pending_tag_bind.device_id == test_devices[0].id
        assert mock_context.pending_tag_bind.is_expired is False

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
        """Overlay flag update does not replace an already-logged-in user."""
        mock_context.session_mgr.start_session(test_user)
        mock_context.admin_overlay_open = True
        resp = client.post("/api/admin/session?overlay=false")
        assert resp.status_code == 200
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
            dumped = str(row)
            assert digest not in dumped


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
