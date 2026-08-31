"""
File: routes.py
Description: REST API endpoints and SSE event stream for the Smart Locker kiosk.
             Provides session management, device listing, borrow/return operations,
             user self-registration (with registrant name validation), admin-only
             manual registration, Register Device (PM + slot + NFC), device-tag
             bind/unbind, registrant list retrieval, source sync, dashboard
             (public Inventory from Excel and Locker from SQLite, Display
             snapshot without person names, admin-secret owner edit and 5-tap
             unbind / arm-bind), an admin-only Excel export download, admin
             Exit kiosk / Shut down, and source sync that writes Location back.
Project: smart_locker/api
Notes: Kiosk session mutations require an active session AND a loopback
       client (require_session). LAN browsers must not ride the process-global
       kiosk session. SSE at /api/events is kiosk-loopback only (dashboard
       polls public GETs; it does not use EventSource). Self-registration
       validates against the approved registrants list; admin registration
       bypasses this check. Catalog GETs
       under /api/dashboard/ stay public. Dashboard mutations require
       SMART_LOCKER_DASHBOARD_ADMIN_SECRET (header X-Smart-Locker-Admin), not
       loopback. Appliance session/shutdown/exit/update are kiosk-loopback only.
"""

import asyncio
import ipaddress
import json
import logging
import secrets
import shutil
import subprocess
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, selectinload

import smart_locker.api.app_context as ctx_module
from smart_locker.api.app_context import (
    PendingRegistration,
    PendingTagBind,
    assign_pending_registration,
    assign_pending_tag_bind,
    pending_state_lock,
)
from smart_locker.auth.session_manager import UserSession
from config.settings import (
    BASE_DIR,
    DASHBOARD_ADMIN_HEADER,
    MAX_LOCKER_SLOT,
    dashboard_admin_secret,
)
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
from smart_locker.services.appliance import (
    ApplianceError,
    ApplianceUnavailable,
    exit_kiosk,
    shutdown as appliance_shutdown,
)
from smart_locker.services.locker_service import LockerService
from smart_locker.services.owner_edit import (
    CatalogUnavailable,
    InvalidOwnerRequest,
    LockerOwned,
    UnknownPm,
    owner_choices,
    set_owner,
)
from smart_locker.sync import sync_status
from smart_locker.sync.inventory_reader import InventoryReadError, read_inventory

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
    """Serve the public dashboard at the documented ``/dashboard`` URL.

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


@router.get("/api/config")
def public_config() -> dict:
    """Site overlay for the kiosk and dashboard (no auth).

    Returns the display noun for the locker join key. Storage and JSON still
    use ``pm_number``. Excel header extras are not needed in the browser.

    Returns:
        dict: ``asset_label`` from ``SMART_LOCKER_ASSET_LABEL``.
    """
    from config.settings import asset_label

    return {"asset_label": asset_label()}


@router.get("/api/health")
def health() -> dict:
    """Liveness/health probe (no auth) for remote, hands-off monitoring.

    Returns a small JSON snapshot a remote operator can open in any browser —
    no SSH, no Linux — to confirm the appliance is alive and see at a glance
    whether the database answers, the NFC reader is running, when the last
    source sync ran, and whether Location write-back last succeeded. Every
    probe is individually guarded so this endpoint can NEVER raise and take
    the server down; it always returns HTTP 200, and the ``status`` field is
    ``"ok"`` or ``"degraded"``.

    Returns:
        dict: status, uptime, database/reader liveness, last-sync, and
        last-writeback snapshots.
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

    try:
        from smart_locker.sync.location_writeback import last_writeback as _last_wb

        last_writeback = _last_wb()
    except Exception:
        last_writeback = None

    return {
        "status": "ok" if db_ok else "degraded",
        "uptime_seconds": round(time.time() - _START_TIME, 1),
        "database": db_ok,
        "nfc_reader": reader_running,
        "fake_reader": fake_reader_enabled(),
        "session_active": session_active,
        "last_sync": last_sync,
        "last_writeback": last_writeback,
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


def require_session(request: Request) -> UserSession:
    """Require an active kiosk session from a loopback client.

    FastAPI dependency that checks for an active session and returns it.
    Automatically calls ``touch()`` to reset the inactivity timer on
    every request that uses this dependency.

    A process-global session started from the Riverdi is not authorization
    for a LAN browser: bind, unbind, borrow, export, and session-end stay
    kiosk-local. Dashboard catalog GETs stay public; dashboard mutations
    use ``require_dashboard_admin``.

    Args:
        request: Incoming ASGI request (client address, not X-Forwarded-For).

    Returns:
        UserSession: The currently active kiosk session.

    Raises:
        HTTPException: 403 if the client is not loopback; 401 if no session
            is active.
    """
    if not _is_loopback_request(request):
        raise HTTPException(
            status_code=403,
            detail="Kiosk-local access required.",
        )
    if ctx_module.context is None or not ctx_module.context.session_mgr.has_active_session:
        raise HTTPException(status_code=401, detail="No active session.")
    session = ctx_module.context.session_mgr.current_session
    if session is None:
        raise HTTPException(status_code=401, detail="No active session.")
    ctx_module.context.session_mgr.touch()
    return session


def _is_loopback_request(request: Request) -> bool:
    """Whether the HTTP client is on loopback (kiosk Chromium / local tests).

    Does not trust ``X-Forwarded-For``. Binding ``API_HOST`` to ``0.0.0.0`` is
    not enough: LAN browsers must not pass this check.

    Args:
        request: Incoming ASGI request.

    Returns:
        True for 127.0.0.0/8, ::1, IPv4-mapped loopback, and localhost.
    """
    if request.client is None or not request.client.host:
        return False
    host = request.client.host.strip()
    if host.lower() in {"localhost", "ip6-localhost"}:
        return True
    raw = host.strip("[]")
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return False
    if addr.is_loopback:
        return True
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped.is_loopback
    return False


def require_loopback(request: Request) -> None:
    """Refuse appliance and kiosk-session-start calls from the LAN.

    FastAPI dependency. Session start, SSE ``/api/events``, shutdown,
    exit-kiosk, and software update are kiosk-local.

    Args:
        request: Incoming ASGI request.

    Raises:
        HTTPException: 403 if the client is not loopback.
    """
    if not _is_loopback_request(request):
        raise HTTPException(
            status_code=403,
            detail="Kiosk-local access required.",
        )


def _push_sse(payload: dict) -> None:
    """Enqueue an SSE payload; safe from the Starlette threadpool.

    ``asyncio.Queue.put_nowait`` is not thread-safe. When the event loop
    is known, schedule the put on that loop. Fan-out copies the payload
    to every EventSource subscriber.

    Args:
        payload: Event dict for the kiosk SSE stream.
    """
    ctx = ctx_module.context
    if ctx is None:
        return
    broadcast = getattr(ctx, "broadcast_sse", None)
    subscribers = getattr(ctx, "_sse_subscribers", None)
    if callable(broadcast) and isinstance(subscribers, list):
        try:
            broadcast(payload)
            return
        except Exception:
            pass
    queue = getattr(ctx, "sse_queue", None)
    if queue is None:
        return
    loop = getattr(ctx, "_loop", None)
    try:
        if isinstance(loop, asyncio.AbstractEventLoop) and loop.is_running():
            loop.call_soon_threadsafe(queue.put_nowait, payload)
        else:
            queue.put_nowait(payload)
    except Exception:
        pass


def _end_kiosk_session(*, sse_reason: str = "explicit") -> None:
    """Drop the process-global kiosk session and overlay bind state.

    Args:
        sse_reason: ``reason`` field on the ``session_ended`` SSE event.
    """
    ctx = ctx_module.context
    if ctx is None:
        return
    ctx.session_mgr.end_session()
    ctx.admin_overlay_open = False
    assign_pending_tag_bind(ctx, None)
    _push_sse({"event": "session_ended", "reason": sse_reason})


def require_dashboard_admin(request: Request) -> None:
    """Require the dashboard admin secret header. Fail closed if unset.

    The 5-tap overlay is client-only and is not authorization. An admin
    row in SQLite is also not authorization.

    Args:
        request: Incoming ASGI request.

    Raises:
        HTTPException: 401 if the secret is unset or the header does not match.
    """
    expected = dashboard_admin_secret()
    if not expected:
        raise HTTPException(
            status_code=401,
            detail="Dashboard admin is not configured.",
        )
    provided = request.headers.get(DASHBOARD_ADMIN_HEADER) or ""
    if len(provided) != len(expected) or not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=401,
            detail="Dashboard admin authorization required.",
        )


def _clear_expired_pending() -> None:
    """Drop expired registration/bind windows so a new arm can proceed."""
    ctx = ctx_module.context
    if ctx is None:
        return
    with pending_state_lock:
        pending_reg = ctx.pending_registration
        if pending_reg is not None and pending_reg.is_expired:
            assign_pending_registration(ctx, None)
        pending_bind = ctx.pending_tag_bind
        if pending_bind is not None and pending_bind.is_expired:
            assign_pending_tag_bind(ctx, None)


def _pending_nfc_conflict() -> str | None:
    """Message if a non-expired bind or registration already owns the reader.

    Returns:
        Conflict detail, or None if the reader is free.
    """
    _clear_expired_pending()
    ctx = ctx_module.context
    if ctx is None:
        return None
    if ctx.pending_registration is not None:
        return "A registration is already waiting for a card tap."
    if ctx.pending_tag_bind is not None:
        return "A device-tag bind is already waiting for a sticker tap."
    return None


# --- SSE Event Stream -------------------------------------------------------

@router.get("/api/events")
async def sse_events(_: None = Depends(require_loopback)):
    """Server-Sent Events stream for NFC and session events.

    Loopback only (kiosk Chromium). A LAN EventSource must not observe
    auth_success or other session identity. Each client gets its own
    queue so a second EventSource cannot steal events from the kiosk.
    Keepalive comments every 15 seconds.

    Returns:
        StreamingResponse: An SSE text/event-stream response.

    Raises:
        HTTPException: 403 if the client is not loopback; 503 if the
            app context is not ready.
    """
    ctx = ctx_module.context
    if ctx is None:
        raise HTTPException(status_code=503, detail="System not ready.")

    subscribe = getattr(ctx, "subscribe_sse", None)
    unsubscribe = getattr(ctx, "unsubscribe_sse", None)
    if callable(subscribe) and isinstance(getattr(ctx, "_sse_subscribers", None), list):
        queue = subscribe()
    else:
        queue = ctx.sse_queue

    async def event_generator():
        """Yield SSE-formatted events from this client's queue."""
        try:
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=15.0)
                    event_name = data.get("event", "message")
                    yield f"event: {event_name}\ndata: {json.dumps(data)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            if callable(unsubscribe) and queue is not getattr(ctx, "sse_queue", None):
                unsubscribe(queue)

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
def dev_tap(request: Request, body: TapRequest):
    """Inject a simulated card tap (simulation mode only).

    Enqueues a ``CardEvent(INSERTED)`` so the normal NFC bridge runs exactly as
    for a real tap — work-card login/logout, device-tag auto-intent, or a
    pending registration / tag-bind intercept. The UID is never logged.
    When the fake reader is on, the caller must be loopback so a LAN host
    cannot inject taps.

    Args:
        request: Incoming ASGI request (loopback check; not X-Forwarded-For).
        body: TapRequest with an optional ``uid`` (falls back to the
            ``SMART_LOCKER_FAKE_DEFAULT_UID`` env var).

    Returns:
        dict: ``{"ok": True}`` once the event is queued.

    Raises:
        HTTPException: 404 if simulation mode is off; 403 if not loopback;
            400 if no UID is available.
    """
    import os

    reader = _running_fake_reader()
    if not _is_loopback_request(request):
        raise HTTPException(
            status_code=403,
            detail="Kiosk-local access required.",
        )
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
def get_session_status(_: None = Depends(require_loopback)):
    """Check current session state (kiosk Chromium / loopback only).

    LAN browsers must not hitchhike the process-global kiosk identity.

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
    _end_kiosk_session()
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


@router.post("/api/devices/{device_id}/transfer")
def transfer_device(
    device_id: int,
    db: Session = Depends(get_db),
    user_session: UserSession = Depends(require_session),
):
    """Transfer responsibility for a borrowed device to the current user.

    Delegates to ``LockerService.transfer_device`` which records a return for
    the original borrower, a borrow for the new user, and triggers the Excel
    Location write-back. The device stays borrowed; only the current holder
    changes.

    Args:
        device_id: Primary key of the device to transfer.
        db: Database session (injected by ``get_db``).
        user_session: The active session (injected by ``require_session``).

    Returns:
        dict: ``{"success": bool, "message": str}``.
    """
    device = DeviceRepository.find_by_id(db, device_id)
    device_name = device.name if device else f"Device {device_id}"

    success = LockerService.transfer_device(db, user_session, device_id)

    if success:
        return {"success": True, "message": f"{device_name} transferred to you."}
    return {"success": False, "message": f"Could not transfer {device_name}."}


# --- Registration Endpoints -------------------------------------------------

class RegisterRequest(BaseModel):
    """Request body for the self-registration endpoint.

    Validates that the display name is between 1 and 100 characters.
    """

    name: str = Field(..., min_length=1, max_length=100)


class RegisterDeviceRequest(BaseModel):
    """Admin Register Device: PM from Excel plus a free locker slot."""

    pm_number: str = Field(..., min_length=1, max_length=50)
    locker_slot: int = Field(..., ge=1, le=MAX_LOCKER_SLOT)


class SetSlotRequest(BaseModel):
    """Admin change of the physical locker slot on an existing device."""

    locker_slot: int = Field(..., ge=1, le=MAX_LOCKER_SLOT)


class KioskDisplayBody(BaseModel):
    """Kiosk heartbeat of the screen currently shown on the Riverdi."""

    screen: str = Field(..., min_length=1, max_length=64)


class OwnerEditBody(BaseModel):
    """Admin-secret-gated dashboard owner change for one catalog PM.

    Inventory/Locker GETs stay public. This POST requires
    ``X-Smart-Locker-Admin``; clock 5-tap is not authorization.
    """

    pm_number: str = Field(..., min_length=1, max_length=50)
    owner: str = Field("", max_length=100)


class TagActionBody(BaseModel):
    """Dashboard 5-tap overlay: unbind or arm-bind one locker PM."""

    pm_number: str = Field(..., min_length=1, max_length=50)


# Labels for GET /api/dashboard/display. Unknown ids are title-cased.
_KIOSK_SCREEN_LABELS = {
    "idle": "Idle",
    "main-menu": "Main menu",
    "borrow": "Locker",
    "return": "Return",
    "admin": "Admin",
    "register": "Register",
    "device-detail": "Device detail",
    "auth-failed": "Sign-in failed",
}


def _normalize_kiosk_screen(raw: str) -> str:
    """Keep a short lowercase screen id (letters, digits, hyphen, underscore).

    Args:
        raw: Value from the kiosk heartbeat.

    Returns:
        Sanitized id, or ``idle`` if nothing usable remains.
    """
    cleaned = "".join(
        ch for ch in (raw or "").strip().lower() if ch.isalnum() or ch in "-_"
    )
    return cleaned[:64] or "idle"


def _kiosk_display_snapshot() -> dict:
    """Build the public Display-tab payload from AppContext.

    Returns:
        ``screen`` and human ``label``. Person names are omitted.
    """
    ctx = ctx_module.context
    screen = "idle"
    raw = getattr(ctx, "kiosk_screen", "idle")
    if isinstance(raw, str) and raw.strip():
        screen = _normalize_kiosk_screen(raw)
    if bool(getattr(ctx, "admin_overlay_open", False)):
        screen = "admin"
    occupied = False
    session_mgr = getattr(ctx, "session_mgr", None)
    if session_mgr is not None and getattr(session_mgr, "has_active_session", False):
        occupied = True
    label = _KIOSK_SCREEN_LABELS.get(screen, screen.replace("-", " ").title())
    return {"screen": screen, "label": label, "occupied": occupied}


@router.post("/api/register")
def start_registration(
    body: RegisterRequest,
    db: Session = Depends(get_db),
    _: None = Depends(require_loopback),
):
    """Begin self-registration: validate name against approved list, await NFC tap.

    The submitted name must exist in the ``registrants`` table (populated from
    the "Location" column during source Excel import). If the name
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

    conflict = _pending_nfc_conflict()
    if conflict:
        raise HTTPException(status_code=409, detail=conflict)

    # Validate name against the approved registrants list
    registrant = RegistrantRepository.find_by_name(db, body.name.strip())
    if registrant is None:
        raise HTTPException(
            status_code=403,
            detail="Name not found in approved list. Contact an admin for manual registration.",
        )

    assign_pending_tag_bind(ctx_module.context, None)
    assign_pending_registration(
        ctx_module.context,
        PendingRegistration(display_name=body.name.strip()),
    )
    logger.info("Registration started for '%s'. Awaiting card tap.", body.name.strip())
    return {"success": True, "message": "Tap your NFC card to complete registration."}


@router.post("/api/register/cancel")
def cancel_registration(_: None = Depends(require_loopback)):
    """Cancel a pending self-registration.

    Clears the pending registration state so the next card tap will not
    trigger enrollment. Does not clear a dashboard-secret-armed device-tag
    bind. Loopback-only so a LAN client cannot cancel a kiosk or dashboard bind.

    Returns:
        dict: ``{"success": True, "cancelled": bool}`` — ``cancelled`` is
              True if a registration (or kiosk bind) was actually pending.

    Raises:
        HTTPException: 503 if system not ready; 403 if not loopback.
    """
    if ctx_module.context is None:
        raise HTTPException(status_code=503, detail="System not ready.")

    ctx = ctx_module.context
    with pending_state_lock:
        bind = ctx.pending_tag_bind
        keep_dashboard = bind is not None and bool(
            getattr(bind, "from_dashboard", False)
        )
        was_pending = ctx.pending_registration is not None or (
            bind is not None and not keep_dashboard
        )
        assign_pending_registration(ctx, None)
        if not keep_dashboard:
            assign_pending_tag_bind(ctx, None)
    return {"success": True, "cancelled": was_pending}


@router.get("/api/registrants")
def get_registrants(db: Session = Depends(get_db)):
    """Return the list of approved names available for self-registration.

    Reads the ``registrants`` table (populated from the "Location"
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
    _: None = Depends(require_loopback),
):
    """Start a backend session for the admin panel (triggered by 5x clock tap).

    Kiosk-local only (loopback). The hidden admin panel on the kiosk UI allows
    physical-access admin control without an NFC card. This endpoint finds the
    first active admin user in the database and creates a real backend session
    so that subsequent API calls (borrow, return, sync, etc.) pass the
    ``require_session`` check.

    ``overlay=true`` (the default) blocks auto-intent on device tags while the
    admin panel is open. If a session is already active, only the overlay flag
    is updated (the logged-in user is not replaced). Overlay is not
    authentication; LAN callers are refused even when an admin row exists.

    Args:
        overlay: When True, device-tag taps do not borrow/return. Pass False
            after jumping to the kiosk Borrow/Return screens.
        db: Active database session (injected by ``get_db``).

    Returns:
        dict: ``{"success": True, "user": {"id", "name", "role"}}`` with the
              admin user whose session was created.

    Raises:
        HTTPException: 403 if not loopback, 503 if system not ready, 404 if
                       no active admin users exist in the database.
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
        session = ctx_module.context.session_mgr.current_session
        user = session.user if session is not None else None
        if user is None:
            raise HTTPException(status_code=401, detail="No active session.")
        if user.role != UserRole.ADMIN:
            raise HTTPException(
                status_code=403,
                detail="An active kiosk session is in progress.",
            )
        ctx_module.context.admin_overlay_open = overlay
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

    conflict = _pending_nfc_conflict()
    if conflict:
        raise HTTPException(status_code=409, detail=conflict)

    assign_pending_tag_bind(ctx_module.context, None)
    assign_pending_registration(
        ctx_module.context,
        PendingRegistration(display_name=body.name.strip()),
    )
    logger.info(
        "Admin-initiated registration for '%s' by admin %s. Awaiting card tap.",
        body.name.strip(), user_session.user.display_name,
    )
    return {"success": True, "message": "Tap the new user's NFC card to complete registration."}


@router.post("/api/admin/devices/register")
def register_locker_device(
    body: RegisterDeviceRequest,
    db: Session = Depends(get_db),
    user_session: UserSession = Depends(require_session),
):
    """Create a locker row from Excel catalog (PM + free slot) and arm NFC bind.

    Looks up the PM in ``device-list.xlsx``, copies catalog fields, assigns the
    chosen slot, then waits for the sticker tap (same window as bind-tag).

    Args:
        body: PM number and locker slot.
        db: Database session (injected by ``get_db``).
        user_session: The active admin session (injected by ``require_session``).

    Returns:
        dict: ``success``, ``device_id``, ``name``, ``pm_number``, ``locker_slot``.

    Raises:
        HTTPException: 503 if not ready / share down, 403 if not admin,
            400 if source path unset, 404 if PM unknown, 409 if PM or slot taken.
    """
    if ctx_module.context is None:
        raise HTTPException(status_code=503, detail="System not ready.")

    if user_session.user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin access required.")

    conflict = _pending_nfc_conflict()
    if conflict:
        raise HTTPException(status_code=409, detail=conflict)

    from config.settings import SOURCE_EXCEL_PATH
    if not SOURCE_EXCEL_PATH:
        raise HTTPException(status_code=400, detail="Source Excel path not configured.")

    from smart_locker.services.device_registration import (
        AlreadyRegistered,
        CatalogUnavailable,
        InvalidSlot,
        SlotTaken,
        UnknownPm,
        register_locker_device as create_from_catalog,
    )

    try:
        device = create_from_catalog(
            db, SOURCE_EXCEL_PATH, body.pm_number, body.locker_slot,
        )
    except CatalogUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except UnknownPm as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except (SlotTaken, AlreadyRegistered) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except InvalidSlot as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    db.flush()
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    from smart_locker.sync.location_writeback import schedule_write_location

    schedule_write_location()
    assign_pending_tag_bind(
        ctx_module.context, PendingTagBind(device_id=device.id)
    )
    logger.info(
        "Locker device registered %s (pm=%s, slot=%s) by admin %s. Awaiting sticker.",
        device.name,
        device.pm_number,
        device.locker_slot,
        user_session.user.display_name,
    )
    return {
        "success": True,
        "device_id": device.id,
        "name": device.name,
        "pm_number": device.pm_number,
        "locker_slot": device.locker_slot,
        "message": "Tap the sticker to bind it.",
    }


@router.post("/api/admin/devices/{device_id}/slot")
def set_device_slot(
    device_id: int,
    body: SetSlotRequest,
    db: Session = Depends(get_db),
    user_session: UserSession = Depends(require_session),
):
    """Move an existing locker device to a different free slot (admin).

    Args:
        device_id: Primary key of the locker device.
        body: New slot number.
        db: Database session (injected by ``get_db``).
        user_session: The active admin session (injected by ``require_session``).

    Returns:
        dict: ``{"success": True, "locker_slot": int}``.

    Raises:
        HTTPException: 403 if not admin, 404 if missing, 409 if slot taken.
    """
    if user_session.user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin access required.")

    device = DeviceRepository.find_by_id(db, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found.")

    from smart_locker.services.device_registration import (
        InvalidSlot,
        SlotTaken,
        set_locker_slot,
    )

    try:
        set_locker_slot(db, device, body.locker_slot)
    except SlotTaken as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except InvalidSlot as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    logger.info(
        "Slot for %s (pm=%s) set to %s by admin %s.",
        device.name,
        device.pm_number,
        device.locker_slot,
        user_session.user.display_name,
    )
    return {"success": True, "locker_slot": device.locker_slot}


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

    conflict = _pending_nfc_conflict()
    if conflict:
        raise HTTPException(status_code=409, detail=conflict)

    assign_pending_tag_bind(
        ctx_module.context, PendingTagBind(device_id=device_id)
    )
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
    if ctx_module.context is not None:
        assign_pending_tag_bind(ctx_module.context, None)
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
    """Manually trigger source Excel import then Location write-back (admin only).

    Reads the device catalog spreadsheet and updates catalog fields on locker
    devices already in SQLite. After the import, locker locations are written
    back into Location. Only users with ADMIN role may invoke this.

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
    from smart_locker.sync.scheduler import ImportInProgress, run_source_import_exclusive

    try:
        result = run_source_import_exclusive(
            get_engine(), SOURCE_EXCEL_PATH, trigger="manual"
        )
    except ImportInProgress as e:
        raise HTTPException(
            status_code=409, detail="A catalog import is already running."
        ) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Import failed: {e}") from e

    if result is None:
        raise HTTPException(status_code=400, detail="Source Excel file not found.")
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

    Runs the import in dry-run mode so the admin sees how many locker PMs
    would be updated, left unchanged, or skipped (not in the locker) before
    committing. Sync never inserts locker rows.

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
    ran, what triggered it (startup/interval/watch/manual), the
    per-category counts, and whether it succeeded. Includes ``at_local``
    and ``ago`` for the admin footer clock.

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
def trigger_update(
    _: None = Depends(require_loopback),
    user_session: UserSession = Depends(require_session),
):
    """Launch the safe software-update script out-of-process (admin only).

    Backs the admin-panel "Software Update" button. The update itself is applied by
    ``deploy/install/update.sh``, which finds an unpacked ``locker-updates`` tree
    (USB first, then ``$APP_DIR/locker-updates``), snapshots the DB + code, swaps
    in the new version, migrates, restarts the service, health-checks, and
    AUTO-ROLLS-BACK on failure — so a bad update self-reverts on a box no one is
    standing next to.

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


@router.post("/api/admin/exit-kiosk")
def admin_exit_kiosk(
    _: None = Depends(require_loopback),
    user_session: UserSession = Depends(require_session),
):
    """Stop the Chromium kiosk browser (admin only). The backend stays up.

    Chromium was started by graphical autostart; it does not come back until
    the next login or reboot. On a Windows/dev host this returns 503.

    Args:
        user_session: The active session (injected by ``require_session``).

    Returns:
        dict: ``{"ok": True, "message": ...}`` after SIGTERM was sent.

    Raises:
        HTTPException: 403 if not admin; 503 if this host has no kiosk
                       browser; 500 if the stop command failed.
    """
    if user_session.user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin access required.")
    try:
        exit_kiosk()
    except ApplianceUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except ApplianceError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    _end_kiosk_session()
    logger.info("Kiosk browser stopped by admin %s.", user_session.user.display_name)
    return {"ok": True, "message": "Kiosk browser closed. Service is still running."}


@router.post("/api/admin/shutdown")
def admin_shutdown(
    _: None = Depends(require_loopback),
    user_session: UserSession = Depends(require_session),
):
    """Power off the Raspberry Pi (admin only).

    Runs ``sudo -n /usr/bin/systemctl poweroff``. On a Windows/dev host this
    returns 503. A missing sudoers rule returns 500 with a hint to run
    ``apply-sudoers.sh``.

    Args:
        user_session: The active session (injected by ``require_session``).

    Returns:
        dict: ``{"ok": True, "message": ...}`` once poweroff has been requested.

    Raises:
        HTTPException: 403 if not admin; 503 if systemd is absent; 500 if
                       sudo/systemctl refused.
    """
    if user_session.user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin access required.")
    try:
        appliance_shutdown()
    except ApplianceUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except ApplianceError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    logger.info("Appliance shutdown started by admin %s.", user_session.user.display_name)
    return {"ok": True, "message": "Shutting down."}


# --- Dashboard Endpoints ----------------------------------------------------
# Public GETs: Inventory (Excel), Locker (SQLite), Display (no person names).
# Mutations and users/tx lists require SMART_LOCKER_DASHBOARD_ADMIN_SECRET.

@router.post("/api/kiosk/display")
def kiosk_display_heartbeat(
    body: KioskDisplayBody,
    _: None = Depends(require_loopback),
) -> dict:
    """Record the screen the kiosk is showing (loopback / kiosk Chromium).

    The dashboard Display tab polls this snapshot. This endpoint does not
    change kiosk navigation. A missing AppContext is a no-op, not a 500.

    Args:
        body: Screen id from the kiosk (``idle``, ``main-menu``, ``borrow``, …).

    Returns:
        dict: ``ok`` true after the id is stored (or skipped).
    """
    ctx = ctx_module.context
    if ctx is None:
        return {"ok": True}
    ctx.kiosk_screen = _normalize_kiosk_screen(body.screen)
    return {"ok": True}


@router.get("/api/dashboard/display")
def dashboard_display() -> dict:
    """Public snapshot of what the kiosk is showing.

    Returns:
        dict: ``screen`` id, human ``label``, and ``occupied`` (session
            active, without naming the user).
    """
    return _kiosk_display_snapshot()


@router.get("/api/dashboard/inventory")
def dashboard_inventory(db: Session = Depends(get_db)):
    """Public company catalog from the live Excel file (not SQLite).

    Share down or an unreadable workbook is HTTP 503 so the Inventory tab
    can error while the Locker tab still uses ``/api/dashboard/devices``.
    ``in_locker`` marks PMs that already have a SQLite locker row so the
    Inventory tab does not offer owner edit for them.

    Args:
        db: Active database session (injected by ``get_db``).

    Returns:
        list[dict]: One dict per Excel PM row.

    Raises:
        HTTPException: 503 when the catalog path is empty or unreadable.
    """
    from config.settings import SOURCE_EXCEL_PATH
    from smart_locker.sync.source_import import pm_match_key

    if not SOURCE_EXCEL_PATH:
        raise HTTPException(
            status_code=503, detail="Catalog Excel is not configured."
        )
    try:
        rows = read_inventory(SOURCE_EXCEL_PATH)
    except InventoryReadError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    locker_keys = {
        pm_match_key(d.pm_number)
        for d in DeviceRepository.list_all(db)
        if d.pm_number
    }
    return [
        {
            "pm_number": r.pm_number,
            "name": r.name,
            "manufacturer": r.manufacturer,
            "model": r.model,
            "serial_number": r.serial_number,
            "location": r.location,
            "calibration_due": r.calibration_due,
            "in_locker": pm_match_key(r.pm_number) in locker_keys,
        }
        for r in rows
    ]


@router.get("/api/dashboard/devices")
def dashboard_devices(db: Session = Depends(get_db)):
    """Public locker inventory for the dashboard Locker tab.

    SQLite devices only (admin-registered locker rows). Unlike
    the kiosk ``GET /api/devices`` endpoint, this requires no active session.
    Devices are sorted by locker slot then name.

    Args:
        db: Active database session (injected by ``get_db``).

    Returns:
        list[dict]: One dict per locker device with status fields and
                    ``has_tag`` (bool). Sensitive fields (internal IDs,
                    image paths, ``tag_hmac``) are excluded.
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
            "locker_slot": d.locker_slot,
            "status": d.status.value,
            "borrower_name": borrower_name,
            "calibration_due": d.calibration_due.isoformat() if d.calibration_due else None,
            "description": d.description,
            "has_tag": d.tag_hmac is not None,
        })

    return result


@router.get("/api/dashboard/owners")
def dashboard_owners(
    db: Session = Depends(get_db),
    _: None = Depends(require_dashboard_admin),
):
    """Names for the owner-edit dropdown (dashboard admin secret required).

    Combines the in-locker token, registered users, and registrant names
    so the Inventory owner dialog can offer the same list plus free text.

    Args:
        db: Active database session (injected by ``get_db``).

    Returns:
        dict: ``names`` (list of strings) and ``in_locker_token``.
    """
    from config.settings import in_locker_token

    return {
        "names": owner_choices(db),
        "in_locker_token": in_locker_token(),
    }


@router.post("/api/dashboard/owner")
def dashboard_set_owner(
    body: OwnerEditBody,
    db: Session = Depends(get_db),
    _: None = Depends(require_dashboard_admin),
):
    """Change owner for one non-locker PM (dashboard admin secret required).

    Writes the catalog Excel Location cell. Locker devices are refused
    (owner stays with kiosk borrow/return). Does not insert locker rows.

    Args:
        body: PM number and new owner text.
        db: Active database session (injected by ``get_db``).

    Returns:
        dict: ``ok``, ``pm_number``, ``owner``, ``locker``.

    Raises:
        HTTPException: 401 without secret; 400 empty PM; 404 PM not in Excel;
                       409 locker PM; 503 share down.
    """
    from config.settings import SOURCE_EXCEL_PATH

    try:
        result = set_owner(db, SOURCE_EXCEL_PATH, body.pm_number, body.owner)
    except InvalidOwnerRequest as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except UnknownPm as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except LockerOwned as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except CatalogUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return {
        "ok": True,
        "pm_number": result.pm_number,
        "owner": result.owner,
        "locker": result.locker,
    }


@router.post("/api/dashboard/bind-tag")
def dashboard_bind_tag(
    body: TagActionBody,
    db: Session = Depends(get_db),
    _: None = Depends(require_dashboard_admin),
):
    """Arm a 60s NFC bind window for one locker PM.

    Requires ``X-Smart-Locker-Admin`` matching
    ``SMART_LOCKER_DASHBOARD_ADMIN_SECRET`` (fail closed if unset). Does not
    create a kiosk admin session. Refuses while a kiosk user is logged in or
    a non-expired bind/registration already owns the reader. Does not drop
    an in-progress kiosk enroll.

    Args:
        body: PM number of an existing locker device.
        db: Active database session (injected by ``get_db``).

    Returns:
        dict: ``ok``, ``pm_number``, ``name``.

    Raises:
        HTTPException: 401 without secret; 503 if not ready; 409 if a session
            or pending window is active; 404 if the PM is not a locker device.
    """
    if ctx_module.context is None:
        raise HTTPException(status_code=503, detail="System not ready.")

    if ctx_module.context.session_mgr.has_active_session:
        raise HTTPException(
            status_code=409,
            detail="A kiosk session is active. Bind from the kiosk or end the session.",
        )

    conflict = _pending_nfc_conflict()
    if conflict:
        raise HTTPException(status_code=409, detail=conflict)

    pm = body.pm_number.strip()
    device = DeviceRepository.find_by_pm(db, pm)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found.")

    assign_pending_tag_bind(
        ctx_module.context,
        PendingTagBind(device_id=device.id, from_dashboard=True),
    )
    logger.info(
        "Dashboard tag bind armed for %s (pm=%s). Awaiting sticker at kiosk.",
        device.name,
        device.pm_number,
    )
    return {
        "ok": True,
        "pm_number": device.pm_number,
        "name": device.name,
        "message": "Tap the sticker on the kiosk.",
    }


@router.post("/api/dashboard/unbind-tag")
def dashboard_unbind_tag(
    body: TagActionBody,
    db: Session = Depends(get_db),
    _: None = Depends(require_dashboard_admin),
):
    """Clear the NFC sticker HMAC on one locker PM.

    Requires the dashboard admin secret. Also clears an armed bind window
    so the next tap is not re-bound.

    Args:
        body: PM number of an existing locker device.
        db: Active database session (injected by ``get_db``).

    Returns:
        dict: ``ok``, ``pm_number``.

    Raises:
        HTTPException: 401 without secret; 404 if the PM is not a locker device.
    """
    pm = body.pm_number.strip()
    device = DeviceRepository.find_by_pm(db, pm)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found.")

    DeviceRepository.unbind_tag(db, device)
    if ctx_module.context is not None:
        assign_pending_tag_bind(ctx_module.context, None)
    logger.info(
        "Dashboard unbound device tag for %s (pm=%s).",
        device.name,
        device.pm_number,
    )
    return {"ok": True, "pm_number": device.pm_number}


@router.get("/api/dashboard/transactions")
def dashboard_transactions(
    db: Session = Depends(get_db),
    _: None = Depends(require_dashboard_admin),
):
    """Transaction history for the network dashboard (admin secret).

    Returns the most recent 500 borrow/return transactions in reverse
    chronological order. Requires ``X-Smart-Locker-Admin``. The 5-tap
    overlay is not authorization.

    Args:
        db: Active database session (injected by ``get_db``).

    Returns:
        list[dict]: One dict per transaction with timestamp, user, device,
                    type, performed-by (for admin returns), and notes.
    """
    transactions = db.execute(
        select(TransactionLog)
        .options(
            selectinload(TransactionLog.user),
            selectinload(TransactionLog.device),
            selectinload(TransactionLog.performed_by),
        )
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
def dashboard_users(
    db: Session = Depends(get_db),
    _: None = Depends(require_dashboard_admin),
):
    """Registered-users list for the network dashboard (admin secret).

    Returns all registered users with their role and registration date.
    Sensitive fields (uid_hmac, encrypted_card_uid) are never included.
    Requires ``X-Smart-Locker-Admin``. The 5-tap overlay is not authorization.

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
