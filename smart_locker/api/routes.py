"""
File: routes.py
Description: REST API endpoints and SSE event stream for the Smart Locker kiosk.
             Provides session management, device listing, borrow/return operations,
             user self-registration (with registrant name validation), admin-only
             manual registration and device-tag bind/unbind, registrant list
             retrieval, source sync, public dashboard data endpoints, and an
             admin-only Excel export download.
Project: smart_locker/api
Notes: All device/session endpoints require an active kiosk session enforced by
       the require_session dependency. SSE stream at /api/events pushes NFC and
       session events to the browser. Self-registration validates against the
       approved registrants list; admin registration bypasses this check.
       Dashboard endpoints under /api/dashboard/ are public (no auth) — they
       replace the old auto-synced Excel file for network-wide device visibility.
"""

import asyncio
import json
import logging
import shutil
import subprocess
import time

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import smart_locker.api.app_context as ctx_module
from smart_locker.api.app_context import PendingRegistration, PendingTagBind
from smart_locker.auth.session_manager import UserSession
from config.settings import BASE_DIR
from smart_locker.database.engine import get_session, get_session_factory
from sqlalchemy import select
from smart_locker.database.models import (
    Device,
    DeviceStatus,
    TransactionLog,
    User,
    UserRole,
)
from smart_locker.database.repositories import DeviceRepository, RegistrantRepository
from smart_locker.nfc.factory import fake_reader_enabled
from smart_locker.services.locker_service import LockerService
from smart_locker.sync import sync_status

logger = logging.getLogger(__name__)

router = APIRouter()

# Static frontend directory (index.html, dashboard.html, …) — used to serve the
# dashboard at the documented bare "/dashboard" URL below.
_FRONTEND_DIR = BASE_DIR / "smart_locker" / "frontend"

# Process start time, used for the /api/health uptime field.
_START_TIME = time.time()


def _last_update_status() -> dict | None:
    """Read ``logs/update-status.json`` if present (best-effort).

    Used by the public health probe so the kiosk overlay can see
    success vs rollback after the in-memory admin session is gone.

    Returns:
        Parsed status dict, or ``None`` if the marker is missing/unreadable.
    """
    path = BASE_DIR / "logs" / "update-status.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None

# systemd-run presence marks a real Pi/systemd host. The admin "Update now"
# button is disabled (clean 503) anywhere this is absent (dev box / Windows).
# Resolved once at import — it cannot change while the process runs.
_SYSTEMD_RUN = shutil.which("systemd-run")


# --- Page routes ------------------------------------------------------------

@router.get("/dashboard")
def serve_dashboard() -> FileResponse:
    """Serve the read-only dashboard at the documented ``/dashboard`` URL.

    The frontend is mounted via ``StaticFiles(html=True)``, which maps a
    directory to its ``index.html`` but does NOT map a bare name to
    ``<name>.html`` — so ``/dashboard`` would otherwise 404 while only
    ``/dashboard.html`` worked. This route makes the user-facing URL printed in
    the README/GUIDE resolve correctly. Registered before the static mount, so
    it takes priority.

    Returns:
        FileResponse: the dashboard HTML page.
    """
    return FileResponse(_FRONTEND_DIR / "dashboard.html")


@router.get("/api/health")
def health() -> dict:
    """Liveness/health probe (no auth) for remote, hands-off monitoring.

    Returns a small JSON snapshot a remote operator can open in any browser —
    no SSH, no Linux — to confirm the appliance is alive and see at a glance
    whether the database answers, the NFC reader is running, and when the last
    source sync ran. Every probe is individually guarded so this endpoint can
    NEVER raise and take the server down; it always returns HTTP 200, and the
    ``status`` field is ``"ok"`` or ``"degraded"``.

    Returns:
        dict: status, uptime, database/reader liveness, and the last-sync snapshot.
    """
    ctx = ctx_module.context

    # Database probe — a trivial query, guarded so a DB hiccup can't 500 here.
    # Reuses get_session() (create → rollback-on-error → close) rather than
    # re-implementing the session lifecycle.
    db_ok = False
    try:
        with get_session() as db:
            db.execute(select(1))
        db_ok = True
    except Exception:
        pass

    reader_running = False
    try:
        reader_running = bool(ctx is not None and ctx.reader.is_running)
    except Exception:
        pass

    session_active = False
    try:
        session_active = bool(ctx is not None and ctx.session_mgr.has_active_session)
    except Exception:
        pass

    try:
        last_sync = sync_status.get()
    except Exception:
        last_sync = None

    return {
        "status": "ok" if db_ok else "degraded",
        "uptime_seconds": round(time.time() - _START_TIME, 1),
        "database": db_ok,
        "nfc_reader": reader_running,
        "fake_reader": fake_reader_enabled(),
        "session_active": session_active,
        "last_sync": last_sync,
        "update": _last_update_status(),
    }


# --- Dependencies -----------------------------------------------------------

def get_db() -> Session:
    """Yield a database session for the request, with auto-commit/rollback.

    FastAPI dependency that provides a SQLAlchemy session bound to this
    request. Commits on success, rolls back on exception, and closes this
    session on completion. The factory is a plain sessionmaker (NOT a
    scoped_session) — see engine.get_session_factory for why that matters
    under FastAPI's reused thread pool.

    Yields:
        Session: An active SQLAlchemy database session.
    """
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def require_session() -> UserSession:
    """Require an active kiosk session, refreshing the inactivity timer.

    FastAPI dependency that checks for an active session and returns it.
    Automatically calls ``touch()`` to reset the inactivity timer on
    every request that uses this dependency.

    Returns:
        UserSession: The currently active kiosk session.

    Raises:
        HTTPException: 401 if no session is active.
    """
    if ctx_module.context is None or not ctx_module.context.session_mgr.has_active_session:
        raise HTTPException(status_code=401, detail="No active session.")
    session = ctx_module.context.session_mgr.current_session
    if session is None:
        raise HTTPException(status_code=401, detail="No active session.")
    ctx_module.context.session_mgr.touch()
    return session


# --- SSE Event Stream -------------------------------------------------------

@router.get("/api/events")
async def sse_events():
    """Server-Sent Events stream for NFC and session events.

    Returns an SSE ``StreamingResponse`` that forwards events from the
    application's async queue to the browser. Sends a keepalive comment
    every 15 seconds to prevent proxy/browser timeout.

    Returns:
        StreamingResponse: An SSE text/event-stream response.
    """

    async def event_generator():
        """Yield SSE-formatted events from the application queue."""
        while True:
            try:
                data = await asyncio.wait_for(ctx_module.context.sse_queue.get(), timeout=15.0)
                event_name = data.get("event", "message")
                yield f"event: {event_name}\ndata: {json.dumps(data)}\n\n"
            except asyncio.TimeoutError:
                # Keepalive heartbeat
                yield ": keepalive\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# --- Dev / Simulation Endpoints (no-hardware harness) -----------------------
# These drive the simulated NFC reader (FakeNFCReader) so the full
# tap -> authenticate -> SSE flow can be exercised with no hardware. They are
# INERT unless SMART_LOCKER_FAKE_READER is enabled AND the running reader is the
# fake one: otherwise they return 404, so the surface is identical to the
# endpoints not existing. The fake reader must never be enabled in production.

class TapRequest(BaseModel):
    """Request body for POST /api/dev/tap."""

    uid: str | None = Field(
        default=None,
        description="Hex UID to simulate; falls back to SMART_LOCKER_FAKE_DEFAULT_UID.",
    )


def _running_fake_reader():
    """Return the active fake reader, or raise 404 if not in simulation mode.

    A reader counts as simulated only when the env flag is set AND the reader
    actually constructed at startup exposes ``simulate_tap`` — so flipping the
    flag after launch cannot retro-activate these endpoints.

    Returns:
        The running FakeNFCReader instance.

    Raises:
        HTTPException: 404 when the simulation harness is not active.
    """
    reader = ctx_module.context.reader if ctx_module.context is not None else None
    if not fake_reader_enabled() or reader is None or not hasattr(reader, "simulate_tap"):
        raise HTTPException(status_code=404, detail="Not found.")
    return reader


@router.get("/api/dev/status")
def dev_status():
    """Report whether the no-hardware simulation harness is active.

    Always present, but only reports True when the fake reader is the running
    reader. The kiosk UI calls this to decide whether to show the simulated-tap
    control; production always reports inactive.

    Returns:
        dict: ``fake_reader`` (bool) and ``default_uid_set`` (bool).
    """
    import os

    reader = ctx_module.context.reader if ctx_module.context is not None else None
    active = fake_reader_enabled() and reader is not None and hasattr(reader, "simulate_tap")
    return {
        "fake_reader": bool(active),
        "default_uid_set": bool(os.getenv("SMART_LOCKER_FAKE_DEFAULT_UID")),
    }


@router.post("/api/dev/tap")
def dev_tap(body: TapRequest):
    """Inject a simulated card tap (simulation mode only).

    Enqueues a ``CardEvent(INSERTED)`` so the normal NFC bridge runs exactly as
    for a real tap — work-card login/logout, device-tag auto-intent, or a
    pending registration / tag-bind intercept. The UID is never logged.

    Args:
        body: TapRequest with an optional ``uid`` (falls back to the
            ``SMART_LOCKER_FAKE_DEFAULT_UID`` env var).

    Returns:
        dict: ``{"ok": True}`` once the event is queued.

    Raises:
        HTTPException: 404 if simulation mode is off; 400 if no UID is available.
    """
    import os

    reader = _running_fake_reader()
    uid = (body.uid or os.getenv("SMART_LOCKER_FAKE_DEFAULT_UID") or "").strip()
    if not uid:
        raise HTTPException(
            status_code=400,
            detail="No UID supplied and SMART_LOCKER_FAKE_DEFAULT_UID is not set.",
        )
    reader.simulate_tap(uid)
    return {"ok": True}


# --- Session Endpoints ------------------------------------------------------

@router.get("/api/session")
def get_session_status():
    """Check current session state (used on page load to restore state).

    Returns:
        dict: ``{"active": bool, "user": dict|None, "overlay": bool}`` with
              user id, name, and role if a session is active. ``overlay`` is
              True while the hidden admin / Register Device UI owns auto-intent.
    """
    if ctx_module.context is None or not ctx_module.context.session_mgr.has_active_session:
        return {"active": False, "user": None, "overlay": False}
    session = ctx_module.context.session_mgr.current_session
    if session is None:
        return {"active": False, "user": None, "overlay": False}
    user = session.user
    return {
        "active": True,
        "overlay": bool(ctx_module.context.admin_overlay_open),
        "user": {
            "id": user.id,
            "name": user.display_name,
            "role": user.role.value,
        },
    }


@router.post("/api/session/end")
def end_session(user_session: UserSession = Depends(require_session)):
    """End the current session (End Session button).

    Terminates the active session and pushes a ``session_ended`` SSE event
    so that all connected clients are notified.

    Args:
        user_session: The active session (injected by ``require_session``).

    Returns:
        dict: ``{"success": True}``.
    """
    ctx_module.context.session_mgr.end_session()
    ctx_module.context.admin_overlay_open = False
    ctx_module.context.pending_tag_bind = None
    # Push SSE event so other tabs / SSE listeners know
    try:
        ctx_module.context.sse_queue.put_nowait(
            {"event": "session_ended", "reason": "explicit"},
        )
    except Exception:
        pass
    return {"success": True}


@router.post("/api/session/touch")
def touch_session(user_session: UserSession = Depends(require_session)):
    """Reset the inactivity timer (called on any UI interaction).

    The ``require_session`` dependency already calls ``touch()`` — this
    endpoint exists so the frontend can explicitly ping the session.

    Args:
        user_session: The active session (injected by ``require_session``).

    Returns:
        dict: ``{"success": True}``.
    """
    # touch() already called by require_session dependency
    return {"success": True}


# --- Device Endpoints -------------------------------------------------------

@router.get("/api/devices")
def list_devices(
    db: Session = Depends(get_db),
    user_session: UserSession = Depends(require_session),
):
    """List all devices with borrower info for the kiosk UI.

    Returns a flat list of device dicts with status and borrower name.
    The current user's own borrowed devices show ``"You"`` as the borrower.

    Args:
        db: Database session (injected by ``get_db``).
        user_session: The active session (injected by ``require_session``).

    Returns:
        list[dict]: One dict per device with id, name, status, borrower_name, etc.
    """
    devices = DeviceRepository.list_all(db)
    current_user_id = user_session.user.id
    result = []

    for d in devices:
        borrower_name = None
        if d.status == DeviceStatus.BORROWED and d.current_borrower_id is not None:
            if d.current_borrower_id == current_user_id:
                borrower_name = "You"
            elif d.current_borrower is not None:
                borrower_name = d.current_borrower.display_name

        result.append({
            "id": d.id,
            "pm_number": d.pm_number,
            "name": d.name,
            "device_type": d.device_type,
            "serial_number": d.serial_number,
            "manufacturer": d.manufacturer,
            "model": d.model,
            "barcode": d.barcode,
            "locker_slot": d.locker_slot,
            "description": d.description,
            "image_path": d.image_path,
            "calibration_due": d.calibration_due.isoformat() if d.calibration_due else None,
            "status": d.status.value,
            "borrower_name": borrower_name,
            "has_tag": d.tag_hmac is not None,
        })

    return result


@router.post("/api/devices/{device_id}/borrow")
def borrow_device(
    device_id: int,
    db: Session = Depends(get_db),
    user_session: UserSession = Depends(require_session),
):
    """Borrow a device by ID.

    Delegates to ``LockerService.borrow_device`` which enforces the per-user
    borrow limit and device availability constraints.

    Args:
        device_id: Primary key of the device to borrow.
        db: Database session (injected by ``get_db``).
        user_session: The active session (injected by ``require_session``).

    Returns:
        dict: ``{"success": bool, "message": str}``.
    """
    device = DeviceRepository.find_by_id(db, device_id)
    device_name = device.name if device else f"Device {device_id}"

    success = LockerService.borrow_device(db, user_session, device_id)

    if success:
        return {"success": True, "message": f"{device_name} borrowed."}
    return {"success": False, "message": f"Could not borrow {device_name}."}


@router.post("/api/devices/{device_id}/return")
def return_device(
    device_id: int,
    db: Session = Depends(get_db),
    user_session: UserSession = Depends(require_session),
):
    """Return a device by ID.

    Delegates to ``LockerService.return_device`` which checks borrower
    ownership (admins may return on behalf of other users).

    Args:
        device_id: Primary key of the device to return.
        db: Database session (injected by ``get_db``).
        user_session: The active session (injected by ``require_session``).

    Returns:
        dict: ``{"success": bool, "message": str}``.
    """
    device = DeviceRepository.find_by_id(db, device_id)
    device_name = device.name if device else f"Device {device_id}"

    success = LockerService.return_device(db, user_session, device_id)

    if success:
        return {"success": True, "message": f"{device_name} returned."}
    return {"success": False, "message": f"Could not return {device_name}."}


# --- Registration Endpoints -------------------------------------------------

class RegisterRequest(BaseModel):
    """Request body for the self-registration endpoint.

    Validates that the display name is between 1 and 100 characters.
    """

    name: str = Field(..., min_length=1, max_length=100)


@router.post("/api/register")
def start_registration(body: RegisterRequest, db: Session = Depends(get_db)):
    """Begin self-registration: validate name against approved list, await NFC tap.

    The submitted name must exist in the ``registrants`` table (populated from
    the "Aktueller Einsatzort" column during source Excel import). If the name
    is not found, the request is rejected with 403 — the user must contact an
    admin for manual registration. Creates a ``PendingRegistration`` that the
    NFC bridge loop will detect on the next card tap.

    Args:
        body: Request body with the user's display name.
        db: Active database session (injected by ``get_db``).

    Returns:
        dict: ``{"success": True, "message": str}``.

    Raises:
        HTTPException: 503 if system not ready, 409 if session active,
                       403 if name not in approved registrants list.
    """
    if ctx_module.context is None:
        raise HTTPException(status_code=503, detail="System not ready.")

    if ctx_module.context.session_mgr.has_active_session:
        raise HTTPException(status_code=409, detail="A session is active. End it first.")

    # Validate name against the approved registrants list
    registrant = RegistrantRepository.find_by_name(db, body.name.strip())
    if registrant is None:
        raise HTTPException(
            status_code=403,
            detail="Name not found in approved list. Contact an admin for manual registration.",
        )

    ctx_module.context.pending_tag_bind = None
    ctx_module.context.pending_registration = PendingRegistration(
        display_name=body.name.strip(),
    )
    logger.info("Registration started for '%s'. Awaiting card tap.", body.name.strip())
    return {"success": True, "message": "Tap your NFC card to complete registration."}


@router.post("/api/register/cancel")
def cancel_registration():
    """Cancel a pending self-registration.

    Clears the pending registration state so the next card tap will not
    trigger enrollment.

    Returns:
        dict: ``{"success": True, "cancelled": bool}`` — ``cancelled`` is
              True if a registration was actually pending.

    Raises:
        HTTPException: 503 if system not ready.
    """
    if ctx_module.context is None:
        raise HTTPException(status_code=503, detail="System not ready.")

    was_pending = (
        ctx_module.context.pending_registration is not None
        or ctx_module.context.pending_tag_bind is not None
    )
    ctx_module.context.pending_registration = None
    ctx_module.context.pending_tag_bind = None
    return {"success": True, "cancelled": was_pending}


@router.get("/api/registrants")
def get_registrants(db: Session = Depends(get_db)):
    """Return the list of approved names available for self-registration.

    Reads the ``registrants`` table (populated from the "Aktueller Einsatzort"
    column during source Excel import) and filters out names that already have
    an active User record — those people are already registered and do not need
    to appear in the selection list. No session required; this is a public
    endpoint called from the idle/registration screen.

    Args:
        db: Active database session (injected by ``get_db``).

    Returns:
        dict: ``{"names": list[str]}`` — alphabetically sorted list of names
              that have not yet registered.
    """
    registrants = RegistrantRepository.get_all(db)

    # Build a set of names already registered (case-insensitive) so they can
    # be excluded from the list shown to new users.
    registered_lower = {
        name.lower()
        for name in db.execute(
            select(User.display_name).where(User.is_active.is_(True))
        ).scalars()
    }

    # Filter out already-registered names and return the rest sorted
    names = [
        r.display_name
        for r in registrants
        if r.display_name.lower() not in registered_lower
    ]
    return {"names": names}


# --- Admin Endpoints --------------------------------------------------------

@router.post("/api/admin/session")
def start_admin_session(
    overlay: bool = True,
    db: Session = Depends(get_db),
):
    """Start a backend session for the admin panel (triggered by 5x clock tap).

    The hidden admin panel on the kiosk UI allows physical-access admin control
    without an NFC card. This endpoint finds the first active admin user in the
    database and creates a real backend session so that subsequent API calls
    (borrow, return, sync, etc.) pass the ``require_session`` check.

    Unlike NFC-based authentication, this bypasses card tap — security relies on
    physical kiosk access and the hidden 5-tap gesture. ``overlay=true`` (the
    default) blocks auto-intent on device tags while the admin panel is open.
    If a session is already active, only the overlay flag is updated (the
    logged-in user is not replaced).

    Args:
        overlay: When True, device-tag taps do not borrow/return. Pass False
            after jumping to the kiosk Borrow/Return screens.
        db: Active database session (injected by ``get_db``).

    Returns:
        dict: ``{"success": True, "user": {"id", "name", "role"}}`` with the
              admin user whose session was created.

    Raises:
        HTTPException: 503 if system not ready, 404 if no active admin users
                       exist in the database.
    """
    if ctx_module.context is None:
        raise HTTPException(status_code=503, detail="System not ready.")

    # Find the first active admin user in the database
    stmt = (
        select(User)
        .where(User.role == UserRole.ADMIN, User.is_active.is_(True))
        .order_by(User.id)
        .limit(1)
    )
    admin_user = db.execute(stmt).scalars().first()
    if ctx_module.context.session_mgr.has_active_session:
        ctx_module.context.admin_overlay_open = overlay
        session = ctx_module.context.session_mgr.current_session
        user = session.user if session is not None else None
        if user is None:
            raise HTTPException(status_code=401, detail="No active session.")
        return {
            "success": True,
            "user": {
                "id": user.id,
                "name": user.display_name,
                "role": user.role.value,
            },
        }

    if admin_user is None:
        raise HTTPException(
            status_code=404,
            detail="No active admin users found. Enroll an admin card first.",
        )

    ctx_module.context.session_mgr.start_session(admin_user)
    ctx_module.context.admin_overlay_open = overlay
    logger.info("Admin panel session started for %s (id=%d)", admin_user.display_name, admin_user.id)

    return {
        "success": True,
        "user": {
            "id": admin_user.id,
            "name": admin_user.display_name,
            "role": admin_user.role.value,
        },
    }


@router.post("/api/admin/register")
def start_admin_registration(
    body: RegisterRequest,
    user_session: UserSession = Depends(require_session),
):
    """Begin admin-initiated manual registration for a user.

    Unlike the self-service ``POST /api/register``, this endpoint does NOT
    validate the name against the registrants table — the admin can register
    anyone with any name. It also does NOT reject the request when a session
    is active (the admin is already logged in). The NFC bridge loop will
    enroll the next card tap as a new user with the provided name.

    Use case: when someone's name is not in the source Excel and they
    cannot self-register, an admin uses the "Register User" button in the
    admin panel to manually enroll them.

    Args:
        body: Request body with the user's display name.
        user_session: The active admin session (injected by ``require_session``).

    Returns:
        dict: ``{"success": True, "message": str}``.

    Raises:
        HTTPException: 503 if system not ready, 403 if caller is not admin.
    """
    if ctx_module.context is None:
        raise HTTPException(status_code=503, detail="System not ready.")

    if user_session.user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin access required.")

    ctx_module.context.pending_tag_bind = None
    ctx_module.context.pending_registration = PendingRegistration(
        display_name=body.name.strip(),
    )
    logger.info(
        "Admin-initiated registration for '%s' by admin %s. Awaiting card tap.",
        body.name.strip(), user_session.user.display_name,
    )
    return {"success": True, "message": "Tap the new user's NFC card to complete registration."}


@router.post("/api/admin/devices/{device_id}/bind-tag")
def start_device_tag_bind(
    device_id: int,
    db: Session = Depends(get_db),
    user_session: UserSession = Depends(require_session),
):
    """Start a 60s window to bind the next NFC insert to this device (admin).

    Does not create a device. The next sticker tap binds ``tag_hmac`` on this
    row (re-bind replaces). A work card or another device's tag fails the bind.

    Args:
        device_id: Primary key of the locker device to bind.
        db: Database session (injected by ``get_db``).
        user_session: The active admin session (injected by ``require_session``).

    Returns:
        dict: ``{"success": True, "message": str}``.

    Raises:
        HTTPException: 503 if system not ready, 403 if not admin, 404 if
            the device does not exist.
    """
    if ctx_module.context is None:
        raise HTTPException(status_code=503, detail="System not ready.")

    if user_session.user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin access required.")

    device = DeviceRepository.find_by_id(db, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found.")

    ctx_module.context.pending_registration = None
    ctx_module.context.pending_tag_bind = PendingTagBind(device_id=device_id)
    logger.info(
        "Device tag bind started for %s (pm=%s) by admin %s. Awaiting sticker.",
        device.name,
        device.pm_number,
        user_session.user.display_name,
    )
    return {"success": True, "message": "Tap the sticker to bind it."}


@router.post("/api/admin/devices/{device_id}/unbind-tag")
def unbind_device_tag(
    device_id: int,
    db: Session = Depends(get_db),
    user_session: UserSession = Depends(require_session),
):
    """Clear the NFC sticker HMAC on a device (admin).

    Args:
        device_id: Primary key of the locker device to unbind.
        db: Database session (injected by ``get_db``).
        user_session: The active admin session (injected by ``require_session``).

    Returns:
        dict: ``{"success": True}``.

    Raises:
        HTTPException: 403 if not admin, 404 if the device does not exist.
    """
    if user_session.user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin access required.")

    device = DeviceRepository.find_by_id(db, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found.")

    DeviceRepository.unbind_tag(db, device)
    logger.info(
        "Unbound device tag for %s (pm=%s) by admin %s.",
        device.name,
        device.pm_number,
        user_session.user.display_name,
    )
    return {"success": True}


@router.post("/api/admin/sync-source")
def trigger_source_sync(
    user_session: UserSession = Depends(require_session),
):
    """Manually trigger source Excel import (admin only).

    Reads the company device master list and inserts/updates devices in
    the database. Only users with ADMIN role may invoke this.

    Args:
        user_session: The active session (injected by ``require_session``).

    Returns:
        dict: Import summary with ``imported``, ``updated``, ``unchanged``,
              and ``errors`` counts.

    Raises:
        HTTPException: 403 if not admin, 400 if source path not configured.
    """
    if user_session.user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin access required.")

    from config.settings import SOURCE_EXCEL_PATH
    if not SOURCE_EXCEL_PATH:
        raise HTTPException(status_code=400, detail="Source Excel path not configured.")

    from smart_locker.database.engine import get_engine
    from smart_locker.sync.source_import import import_from_source_excel

    try:
        result = import_from_source_excel(get_engine(), SOURCE_EXCEL_PATH)
    except Exception as e:
        sync_status.record_error("manual", str(e))
        raise HTTPException(status_code=500, detail=f"Import failed: {e}") from e

    sync_status.record_result("manual", result)
    return {
        "success": True,
        "imported": result.imported,
        "updated": result.updated,
        "unchanged": result.unchanged,
        "errors": result.errors,
    }


@router.post("/api/admin/sync-preview")
def preview_source_sync(
    user_session: UserSession = Depends(require_session),
):
    """Preview the source import diff without writing anything (admin only).

    Runs the import in dry-run mode (the real create/update logic inside a
    rolled-back transaction) so the admin sees exactly how many devices would
    be added, updated, left unchanged, or skipped before committing.

    Args:
        user_session: The active session (injected by ``require_session``).

    Returns:
        dict: ``imported`` (would-add), ``updated`` (would-change),
              ``unchanged``, ``skipped`` (non-locker), and ``errors`` counts.

    Raises:
        HTTPException: 403 if not admin, 400 if source path not configured.
    """
    if user_session.user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin access required.")

    from config.settings import SOURCE_EXCEL_PATH
    if not SOURCE_EXCEL_PATH:
        raise HTTPException(status_code=400, detail="Source Excel path not configured.")

    from smart_locker.database.engine import get_engine
    from smart_locker.sync.source_import import import_from_source_excel

    result = import_from_source_excel(get_engine(), SOURCE_EXCEL_PATH, dry_run=True)
    return {
        "preview": True,
        "imported": result.imported,
        "updated": result.updated,
        "unchanged": result.unchanged,
        "skipped": result.non_locker_skipped,
        "errors": result.errors,
    }


@router.get("/api/admin/sync-status")
def get_sync_status(user_session: UserSession = Depends(require_session)):
    """Return the most recent source-import outcome (admin only).

    Powers the dashboard "last synced …" line. Reports when the last import
    ran, what triggered it (startup/cron/watch/mtime-poll/manual), the
    per-category counts, and whether it succeeded.

    Args:
        user_session: The active session (injected by ``require_session``).

    Returns:
        dict: The last-sync snapshot (``at`` is null if no import has run yet).

    Raises:
        HTTPException: 403 if not admin.
    """
    if user_session.user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin access required.")
    return sync_status.get()


@router.get("/api/admin/export-excel")
def export_excel(user_session: UserSession = Depends(require_session)):
    """Download the full database as an Excel workbook (admin only).

    Generates a three-sheet ``.xlsx`` file (Devices, Transactions, Users) in
    memory and returns it as an HTTP file download. No file is written to disk,
    so there are no Windows file-locking issues. This replaces the old automatic
    Excel sync that wrote to ``SMART_LOCKER_EXCEL_PATH`` on every DB change.

    Args:
        user_session: The active admin session (injected by ``require_session``).

    Returns:
        Response: Binary ``.xlsx`` content with ``Content-Disposition: attachment``
                  header so the browser triggers a file download.

    Raises:
        HTTPException: 403 if the caller is not an admin.
    """
    if user_session.user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin access required.")

    from smart_locker.database.engine import get_engine
    from smart_locker.sync.excel_sync import export_to_excel_bytes

    xlsx_bytes = export_to_excel_bytes(get_engine())
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=smart_locker_data.xlsx"},
    )


# --- Software update (admin only) -------------------------------------------

def _deployed_version() -> str:
    """Read the deployed version marker written by update.sh (best-effort).

    ``read_text`` raises ``FileNotFoundError`` (an ``OSError``) when the marker
    is absent — e.g. a dev checkout that was never deployed — so no separate
    existence check is needed.
    """
    try:
        return (BASE_DIR / "VERSION").read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


@router.get("/api/admin/update-status")
def get_update_status(user_session: UserSession = Depends(require_session)):
    """Return the last/in-progress software-update outcome (admin only).

    Reads the small JSON marker that ``deploy/install/update.sh`` writes to
    ``logs/update-status.json`` so the admin panel can show update progress and
    the result (success / rolled_back / failed) without any SSH access.

    Args:
        user_session: The active session (injected by ``require_session``).

    Returns:
        dict: ``{state, message, version, at, current_version}`` — ``state`` is
              ``idle`` when no update has ever run.

    Raises:
        HTTPException: 403 if not admin.
    """
    if user_session.user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin access required.")

    current = _deployed_version()
    data = _last_update_status()
    if not data:
        # Missing marker (no update has ever run) or an unreadable/corrupt one →
        # report idle either way; the panel just shows "no update yet".
        return {"state": "idle", "message": "No update has run yet.",
                "version": current, "at": None, "current_version": current}
    data["current_version"] = current
    return data


@router.post("/api/admin/update")
def trigger_update(user_session: UserSession = Depends(require_session)):
    """Launch the safe software-update script out-of-process (admin only).

    Backs the admin-panel "Update now" button. The update itself is applied by
    ``deploy/install/update.sh``, which picks up a release tarball delivered to
    the locker share, snapshots the DB + code, swaps in the new version, migrates,
    restarts the service, health-checks, and AUTO-ROLLS-BACK on failure — so a
    bad update self-reverts on a box no one is standing next to.

    The script restarts the very systemd service that hosts this request, so it
    must run in its OWN cgroup; we launch it as a transient ``systemd-run`` unit
    so the restart cannot kill the updater mid-apply. On a non-Pi/dev host (no
    ``systemd-run``, or the script is absent) this returns 503 with a clear
    message rather than pretending to update.

    Args:
        user_session: The active session (injected by ``require_session``).

    Returns:
        dict: ``{"started": True, "message": ...}`` once the updater is launched.

    Raises:
        HTTPException: 403 if not admin; 503 if updates aren't runnable here;
                       500 if the updater unit could not be launched.
    """
    if user_session.user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin access required.")

    script = BASE_DIR / "deploy" / "install" / "update.sh"
    if not script.exists():
        raise HTTPException(status_code=503, detail="Update script not found on this host.")
    if _SYSTEMD_RUN is None:
        raise HTTPException(
            status_code=503,
            detail="Software updates run on the Raspberry Pi appliance only.",
        )

    # Run in a transient unit so update.sh survives the service restart it
    # triggers. Every argument is fixed and space-free so the sudoers rule can
    # whitelist this exact command (no wildcard → no privilege-escalation gap).
    cmd = [
        "sudo", "-n", "systemd-run", "--collect",
        "--unit=smart-locker-update",
        "/bin/bash", str(script),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=15)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        detail = (getattr(e, "stderr", "") or str(e)).strip()
        logger.error("Failed to launch update unit: %s", detail)
        raise HTTPException(status_code=500, detail=f"Could not start update: {detail}") from e

    logger.info("Software update launched by admin %s.", user_session.user.display_name)
    return {"started": True, "message": "Update started. The kiosk will restart briefly."}


# --- Dashboard Endpoints (public, no auth) ----------------------------------

@router.get("/api/dashboard/devices")
def dashboard_devices(db: Session = Depends(get_db)):
    """Public device inventory for the network dashboard.

    Returns all devices with their current status and borrower name. Unlike
    the kiosk ``GET /api/devices`` endpoint, this requires no active session
    — it replaces the old shared Excel file that anyone on the network could
    open. Devices are sorted by locker slot then name.

    Args:
        db: Active database session (injected by ``get_db``).

    Returns:
        list[dict]: One dict per device with inventory and status fields.
                    Sensitive fields (internal IDs, image paths) are excluded.
    """
    devices = db.execute(
        select(Device).order_by(Device.locker_slot, Device.name)
    ).scalars().all()

    result = []
    for d in devices:
        # Resolve borrower display name from the relationship
        borrower_name = None
        if d.status == DeviceStatus.BORROWED and d.current_borrower is not None:
            borrower_name = d.current_borrower.display_name

        result.append({
            "pm_number": d.pm_number,
            "name": d.name,
            "device_type": d.device_type,
            "manufacturer": d.manufacturer,
            "model": d.model,
            "serial_number": d.serial_number,
            "barcode": d.barcode,
            "locker_slot": d.locker_slot,
            "status": d.status.value,
            "borrower_name": borrower_name,
            "calibration_due": d.calibration_due.isoformat() if d.calibration_due else None,
            "description": d.description,
        })

    return result


@router.get("/api/dashboard/transactions")
def dashboard_transactions(db: Session = Depends(get_db)):
    """Public transaction history for the network dashboard.

    Returns the most recent 500 borrow/return transactions in reverse
    chronological order. No session required — this replaces the
    Transactions sheet from the old shared Excel file.

    Args:
        db: Active database session (injected by ``get_db``).

    Returns:
        list[dict]: One dict per transaction with timestamp, user, device,
                    type, performed-by (for admin returns), and notes.
    """
    transactions = db.execute(
        select(TransactionLog)
        .order_by(TransactionLog.timestamp.desc())
        .limit(500)
    ).scalars().all()

    result = []
    for t in transactions:
        result.append({
            "timestamp": t.timestamp.strftime("%Y-%m-%d %H:%M:%S") if t.timestamp else None,
            "user_name": t.user.display_name if t.user else "",
            "device_name": t.device.name if t.device else "",
            "transaction_type": t.transaction_type.value,
            "performed_by": t.performed_by.display_name if t.performed_by else "",
            "notes": t.notes,
        })

    return result


@router.get("/api/dashboard/users")
def dashboard_users(db: Session = Depends(get_db)):
    """Public registered-users list for the network dashboard.

    Returns all registered users with their role and registration date.
    Sensitive fields (uid_hmac, encrypted_card_uid) are never included.
    No session required — this replaces the Users sheet from the old
    shared Excel file.

    Args:
        db: Active database session (injected by ``get_db``).

    Returns:
        list[dict]: One dict per user with display name, role, active
                    status, and registration timestamp.
    """
    users = db.execute(
        select(User).order_by(User.display_name)
    ).scalars().all()

    result = []
    for u in users:
        result.append({
            "display_name": u.display_name,
            "role": u.role.value,
            "is_active": u.is_active,
            "registered_at": u.created_at.strftime("%Y-%m-%d %H:%M:%S") if u.created_at else None,
        })

    return result
