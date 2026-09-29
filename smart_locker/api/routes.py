"""
File: routes.py
Description: REST API endpoints and SSE event stream for the Smart Locker kiosk.
             Provides session management, device listing, borrow/return operations,
             user self-registration (with registrant name validation), admin-only
             manual registration, Register Device (PM + slot + NFC), device-tag
             bind/unbind, registrant list retrieval, catalog mirror sync,
             dashboard (public Inventory/Locker/Display from SQLite, public
             owner edit for non-cabinet devices, admin-secret device editor and
             mirror review), admin Stop system / Shut down, and first-boot
             Setup (enroll the first admin via a cabinet card tap).
Project: smart_locker/api
Notes: Kiosk session mutations require an active session AND a loopback
       client (require_session). LAN browsers must not ride the process-global
       kiosk session. SSE at /api/events is kiosk-loopback only (dashboard
       polls public GETs; it does not use EventSource). Self-registration
       validates against the approved registrants list; admin registration
       bypasses this check. Catalog GETs
       under /api/dashboard/ stay public, and changing the holder of a
       non-cabinet device is public per the plan. Other dashboard mutations
       require SMART_LOCKER_DASHBOARD_ADMIN_SECRET (header
       X-Smart-Locker-Admin), not loopback. Appliance
       session/shutdown/stop-system are kiosk-loopback
       only. Software update accepts the admin session, the dashboard secret,
       or — on a first boot with neither — the open Setup gate.
"""

import asyncio
import ipaddress
import json
import logging
import math
import secrets
import threading
import time

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
)
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import smart_locker.api.app_context as ctx_module
from smart_locker.api.app_context import (
    PendingRegistration,
    PendingTagBind,
    REGISTRATION_TIMEOUT_SECONDS,
    arm_pending_registration,
    arm_pending_tag_bind,
    assign_pending_registration,
    assign_pending_tag_bind,
    clear_pending_tag_bind_for_device,
    clear_pending_tag_bind_if,
    drop_expired_pending,
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
from sqlalchemy.exc import IntegrityError
from smart_locker.database.models import (
    Device,
    DeviceStatus,
    User,
    UserRole,
)
from smart_locker.database.repositories import (
    DeviceRepository, RegistrantRepository, TransactionRepository, UserRepository,
)
from smart_locker.nfc.factory import fake_reader_enabled
from smart_locker.services.appliance import (
    ApplianceError,
    ApplianceUnavailable,
    SYSTEMCTL,
    SYSTEMD_RUN,
    launch_update,
    shutdown as appliance_shutdown,
    stop_system,
)
from smart_locker.services.device_catalog import (
    catalog_record,
    device_record,
    is_registered,
    unbind_device_tag as clear_device_tag,
)
from smart_locker.services.locker_service import LockerService
from smart_locker.services.owner_edit import (
    InvalidOwnerRequest,
    LockerOwned,
    UnknownPm,
    owner_choices,
    set_owner,
)
from smart_locker.services.setup_service import setup_needed, write_dashboard_secret
from smart_locker.services.user_service import (
    LastAdminError,
    PersonHoldsDeviceError,
    person_record,
    update_person,
)
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
_SYSTEMD_RUN = SYSTEMD_RUN

# Same for Stop system: systemctl must exist to stop the service.
_SYSTEMCTL = SYSTEMCTL


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
        dict: ``asset_label`` from ``SMART_LOCKER_ASSET_LABEL``, the borrow
        limit, and the calibration due-soon window in days.
    """
    from config.settings import MAX_BORROWS, asset_label, calibration_warn_days

    return {
        "asset_label": asset_label(),
        "max_borrows": MAX_BORROWS,
        "calibration_warn_days": calibration_warn_days(),
    }


@router.get("/api/health")
def health() -> dict:
    """Liveness/health probe (no auth) for remote, hands-off monitoring.

    Returns a small JSON snapshot a remote operator can open in any browser —
    no SSH, no Linux — to confirm the appliance is alive and see at a glance
    whether the database answers, the NFC reader is running, when the last
    mirror tick ran, and whether the last mirror write succeeded. Every
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
        from smart_locker.sync.mirror import last_write as _last_wb

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


def require_session(
    request: Request,
    db: Session = Depends(get_db),
) -> UserSession:
    """Require an active kiosk session from a loopback client.

    FastAPI dependency that checks for an active session and returns it.
    Automatically calls ``touch()`` to reset the inactivity timer on
    every request that uses this dependency.

    The session's cached ``user`` is a snapshot frozen at login — the row is
    re-fetched on every request so a mid-session deactivation ends the kiosk
    session outright and a mid-session demote stops passing the admin gate
    on the very next call.

    A process-global session started from the Riverdi is not authorization
    for a LAN browser: bind, unbind, borrow, export, and session-end stay
    kiosk-local. Dashboard catalog GETs stay public; dashboard mutations
    use ``require_dashboard_admin``.

    Args:
        request: Incoming ASGI request (client address, not X-Forwarded-For).
        db: Request database session (injected by ``get_db``).

    Returns:
        UserSession: The currently active kiosk session, with ``user``
            refreshed to the live row.

    Raises:
        HTTPException: 403 if the client is not loopback; 401 if no session
            is active or the account was deactivated/deleted mid-session.
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
    fresh = UserRepository.find_by_id(db, session.user.id)
    if fresh is None or not fresh.is_active:
        # The account is gone or deactivated — end the session (this emits
        # session_ended so the kiosk kicks back to idle) and refuse. A dead
        # session must not extend its own timer, so no touch() here.
        ctx_module.context.end_kiosk_session(reason="account_inactive")
        raise HTTPException(
            status_code=401,
            detail="Session ended — this account is no longer active.",
        )
    session.user = fresh
    # Detach the live row into a plain snapshot: get_db commits/rolls back and
    # closes ``db`` at request end, which would otherwise leave session.user
    # expired-and-detached — an unreadable .id on the very next call. Every
    # consumer reads only loaded scalars (id, display_name, role).
    db.expunge(fresh)
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
    ctx.end_kiosk_session(reason=sse_reason)


def _secret_matches(provided: str, expected: str) -> bool:
    """Constant-time secret comparison safe for arbitrary header text.

    ``secrets.compare_digest`` raises ``TypeError`` on non-ASCII ``str`` —
    a latin-1 header like ``päss`` would 500 the gate. Comparing UTF-8 bytes
    accepts every value a header can carry.

    Args:
        provided: ``X-Smart-Locker-Admin`` header value (may be empty).
        expected: Configured secret (non-empty when this is called).

    Returns:
        True only when the values match byte-for-byte.
    """
    p, e = provided.encode("utf-8"), expected.encode("utf-8")
    return len(p) == len(e) and secrets.compare_digest(p, e)


def require_dashboard_admin(request: Request) -> None:
    """Require the dashboard admin secret header. Fail closed if unset.

    The Admin button reveal is client-only and is not authorization. An
    admin row in SQLite is also not authorization.

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
    if not _secret_matches(provided, expected):
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
        drop_expired_pending(ctx)


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


def _latch_card_result(ctx, outcome: str, reg: PendingRegistration) -> None:
    """Record a card window's terminal outcome for the dashboard status poll.

    ``AppContext.last_card_result`` is the latch the NFC bridge writes on
    tap resolution; the HTTP side writes the same shape when a window ends
    without a tap (dashboard cancel, or expiry observed by a status poll).
    ``AppContext`` owns the attribute — this only assigns it.

    Args:
        ctx: Application context (or test double with the same attribute).
        outcome: Terminal outcome — ``"cancelled"`` or ``"expired"``.
        reg: The registration window being dropped.
    """
    ctx.last_card_result = {
        "outcome": outcome,
        "reason": None,
        "user": None,
        "display_name": reg.display_name,
        "replace_user_id": reg.replace_user_id,
        "at": time.monotonic(),
    }


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
    """List locker devices with borrower info for the kiosk UI.

    The borrow and return grids show units that have a sticker bound, plus
    any unit currently on loan — a row borrowed while untagged stays
    returnable from the return grid. An untagged available row waits on the
    admin manage list (``GET /api/admin/devices``) until a sticker is bound.
    The current user's own borrowed devices show ``"You"`` as the borrower.

    Args:
        db: Database session (injected by ``get_db``).
        user_session: The active session (injected by ``require_session``).

    Returns:
        list[dict]: One dict per listed device with id, name, status,
                    borrower_name, etc.
    """
    devices = DeviceRepository.list_kiosk_devices(db)
    current_user_id = user_session.user.id
    return [
        {"id": d.id, **device_record(d, current_user_id=current_user_id),
         "image_path": d.image_path}
        for d in devices
    ]


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

    outcome = LockerService.borrow_device(db, user_session, device_id)

    if outcome:
        return {"success": True, "message": f"{device_name} borrowed."}
    refusal = f"Could not borrow {device_name}"
    if outcome.reason:
        refusal += f": {outcome.reason}"
    return {"success": False, "message": f"{refusal}."}


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
    the original borrower, a borrow for the new user, and marks the catalog
    mirror dirty. The device stays borrowed; only the current holder
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

    outcome = LockerService.transfer_device(db, user_session, device_id)

    if outcome:
        return {"success": True, "message": f"{device_name} transferred to you."}
    refusal = f"Could not transfer {device_name}"
    if outcome.reason:
        refusal += f": {outcome.reason}"
    return {"success": False, "message": f"{refusal}."}


# --- Registration Endpoints -------------------------------------------------

class RegisterRequest(BaseModel):
    """Request body for the self-registration endpoint.

    Validates that the display name is between 1 and 100 characters.
    """

    name: str = Field(..., min_length=1, max_length=100)


class RegisterDeviceRequest(BaseModel):
    """Admin Register Device: catalog PM plus a free locker slot."""

    pm_number: str = Field(..., min_length=1, max_length=50)
    locker_slot: int = Field(..., ge=1, le=MAX_LOCKER_SLOT)


class SetSlotRequest(BaseModel):
    """Admin change of the physical locker slot on an existing device."""

    locker_slot: int = Field(..., ge=1, le=MAX_LOCKER_SLOT)


class KioskDisplayBody(BaseModel):
    """Kiosk heartbeat of the screen currently shown on the Riverdi."""

    screen: str = Field(..., min_length=1, max_length=64)


class OwnerEditBody(BaseModel):
    """Public dashboard owner change for one non-cabinet catalog PM.

    Changing the holder of a device that is not in the cabinet has no admin
    password per the plan. Cabinet units are refused — their holder follows
    kiosk borrow/return.
    """

    pm_number: str = Field(..., min_length=1, max_length=50)
    owner: str = Field("", max_length=100)


class DeviceAddBody(BaseModel):
    """Dashboard editor: add one device to the catalog."""

    pm_number: str = Field(..., min_length=1, max_length=50)
    name: str = Field(..., min_length=1, max_length=100)
    device_type: str | None = Field(None, max_length=50)
    serial_number: str | None = Field(None, max_length=100)
    manufacturer: str | None = Field(None, max_length=100)
    model: str | None = Field(None, max_length=100)
    calibration_due: str | None = Field(None, max_length=20)
    location: str | None = Field(None, max_length=200)


class DeviceEditBody(BaseModel):
    """Dashboard editor: edit catalog fields on one device."""

    name: str | None = Field(None, max_length=100)
    device_type: str | None = Field(None, max_length=50)
    serial_number: str | None = Field(None, max_length=100)
    manufacturer: str | None = Field(None, max_length=100)
    model: str | None = Field(None, max_length=100)
    calibration_due: str | None = Field(None, max_length=20)
    location: str | None = Field(None, max_length=200)


class TagActionBody(BaseModel):
    """Dashboard admin actions: unbind or arm-bind one locker PM."""

    pm_number: str = Field(..., min_length=1, max_length=50)


class ServiceReturnBody(BaseModel):
    """Dashboard admin action: return a maintenance unit to service.

    ``calibration_due`` is required — a unit coming back from maintenance
    always carries its new calibration date.
    """

    calibration_due: str = Field(..., min_length=1, max_length=20)


class UserAddBody(BaseModel):
    """People: add a person — name + role, then their card tap on the cabinet."""

    name: str = Field(..., min_length=1, max_length=100)
    role: str = Field("user", max_length=10)


class UserUpdateBody(BaseModel):
    """People row edit: role and/or the active flag."""

    role: str | None = Field(None, max_length=10)
    is_active: bool | None = None


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

    The submitted name must exist in the ``registrants`` table (seeded from
    the "Location" column when the mirror adopts an existing sheet). If the
    name is not found, the request is rejected with 403 — the user must
    contact an admin for manual registration. Creates a
    ``PendingRegistration`` that the NFC bridge loop will detect on the next
    card tap.

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

    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Name is required.")

    conflict = _pending_nfc_conflict()
    if conflict:
        raise HTTPException(status_code=409, detail=conflict)

    # Validate name against the approved registrants list. While the startup
    # mirror tick is still adopting the sheet (background thread), a missing
    # name may just be "not yet imported" — say so instead of 403.
    registrant = RegistrantRepository.find_by_name(db, name)
    if registrant is None:
        from smart_locker.sync.scheduler import sync_in_progress

        if sync_in_progress():
            raise HTTPException(
                status_code=503,
                detail="Catalog sync is still running — try again shortly.",
            )
        raise HTTPException(
            status_code=403,
            detail="Name not found in approved list. Contact an admin for manual registration.",
        )

    conflict = arm_pending_registration(
        ctx_module.context,
        PendingRegistration(display_name=name),
    )
    if conflict:
        raise HTTPException(status_code=409, detail=conflict)
    logger.info("Registration started for '%s'. Awaiting card tap.", name)
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
        reg = ctx.pending_registration
        keep_dashboard_bind = bind is not None and bool(
            getattr(bind, "from_dashboard", False)
        )
        keep_dashboard_reg = reg is not None and bool(
            getattr(reg, "from_dashboard", False)
        )
        # An already-expired window is still cleared, but it does not count as
        # a cancellation — nothing the user could tap was waiting on it.
        was_pending = (
            reg is not None and not reg.is_expired and not keep_dashboard_reg
        ) or (
            bind is not None and not bind.is_expired and not keep_dashboard_bind
        )
        if not keep_dashboard_reg:
            assign_pending_registration(ctx, None)
        if not keep_dashboard_bind:
            assign_pending_tag_bind(ctx, None)
    return {"success": True, "cancelled": was_pending}


@router.get("/api/registrants")
def get_registrants(db: Session = Depends(get_db)):
    """Return the list of approved names available for self-registration.

    Reads the ``registrants`` table (seeded from the sheet's Location
    column when the mirror first adopts it) and filters out names that already
    have an active User record — those people are already registered and do not
    need to appear in the selection list. No session required; this is a public
    endpoint called from the idle/registration screen.

    Args:
        db: Active database session (injected by ``get_db``).

    Returns:
        dict: ``{"names": list[str]}`` — alphabetically sorted list of names
              that have not yet registered.
    """
    from smart_locker.sync.scheduler import sync_in_progress

    registrants = RegistrantRepository.get_all(db)

    # Build a set of names already registered (case-insensitive) so they can
    # be excluded from the list shown to new users.
    registered_lower = UserRepository.active_names(db)

    # Filter out already-registered names and return the rest sorted
    names = [
        r.display_name
        for r in registrants
        if r.display_name.lower() not in registered_lower
    ]
    return {"names": names, "syncing": sync_in_progress()}


# --- First-boot Setup -------------------------------------------------------

class SetupRequest(BaseModel):
    """Setup arm: the first admin's name, plus the dashboard admin password.

    ``password`` is written to the service-owned ``dashboard.secret`` file when
    no dashboard secret is configured; when one already exists it must arrive
    in the ``X-Smart-Locker-Admin`` header (the typed password then doubles as
    authorization) and the file is left alone.
    """

    name: str = Field(..., min_length=1, max_length=100)
    password: str = Field("", max_length=200)


@router.get("/api/setup")
def setup_state(db: Session = Depends(get_db)) -> dict:
    """Report whether first-boot Setup is open (no active admin enrolled).

    Public: the kiosk (5-tap) and the dashboard (Admin button) both poll
    this to decide between Setup and the normal admin path. Exposes only
    ``needed`` and whether an admin password is already stored — never user
    rows, UIDs, or the secret.

    Returns:
        dict: ``needed`` True while no active admin exists; ``secret_set``
              True when SMART_LOCKER_DASHBOARD_ADMIN_SECRET is configured.
    """
    return {
        "needed": setup_needed(db),
        "secret_set": bool(dashboard_admin_secret()),
    }


@router.post("/api/setup")
def start_setup(
    request: Request,
    body: SetupRequest,
    db: Session = Depends(get_db),
    _: None = Depends(require_loopback),
):
    """Arm a 60s card-tap window that enrolls the tapped card as admin.

    Loopback only, like every registration arm: a LAN caller must not plant a
    dashboard password of their choosing or squat the enrollment window the
    kiosk operator needs. Setup is open only while the database has no active
    admin — the moment one exists, this returns 404 and Setup is gone. Once
    ``SMART_LOCKER_DASHBOARD_ADMIN_SECRET`` is configured, arming requires it
    in the ``X-Smart-Locker-Admin`` header.

    Args:
        request: Incoming ASGI request (admin header check).
        body: Admin display name and dashboard password.
        db: Active database session (injected by ``get_db``).

    Returns:
        dict: ``{"success": True, "message": str}``.

    Raises:
        HTTPException: 403 if not loopback; 503 if system not ready or the NFC
                       reader is down; 404 once an admin exists; 401 when the
                       stored secret does not match; 422 for a blank name or a
                       missing password while no secret is configured; 409 when
                       a registration/bind window already owns the reader.
    """
    ctx = ctx_module.context
    if ctx is None:
        raise HTTPException(status_code=503, detail="System not ready.")

    if not setup_needed(db):
        raise HTTPException(status_code=404, detail="Setup is already complete.")

    secret = dashboard_admin_secret()
    if secret:
        provided = request.headers.get(DASHBOARD_ADMIN_HEADER) or ""
        if not _secret_matches(provided, secret):
            raise HTTPException(
                status_code=401,
                detail="Dashboard admin authorization required.",
            )

    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Admin name is required.")

    password = body.password.strip()
    if not password and not secret:
        # Setup is the only UI writer of the dashboard secret — a blank
        # password here would leave every admin-gated dashboard route dead
        # (401 fail-closed) with no later UI path to set it.
        raise HTTPException(
            status_code=422, detail="Dashboard password is required."
        )

    reader = getattr(ctx, "reader", None)
    if reader is None or not getattr(reader, "is_running", False):
        raise HTTPException(
            status_code=503,
            detail="NFC reader is not available — a card tap cannot complete Setup.",
        )

    # Conflict check, optional secret write, and the arm itself must be one
    # critical section: two racing POSTs must not both write the secret or
    # silently replace each other's window.
    with pending_state_lock:
        conflict = _pending_nfc_conflict()
        if conflict:
            raise HTTPException(status_code=409, detail=conflict)
        if password and not secret:
            try:
                write_dashboard_secret(password)
            except (OSError, ValueError) as e:
                logger.error(
                    "Setup could not store the admin password: %s",
                    type(e).__name__,
                )
                raise HTTPException(
                    status_code=500,
                    detail="Could not store the admin password.",
                ) from e
        assign_pending_tag_bind(ctx, None)
        assign_pending_registration(
            ctx,
            PendingRegistration(display_name=name, role="admin"),
        )
    logger.info("Setup armed: awaiting the first admin card tap.")
    return {"success": True, "message": "Tap the admin card on the locker reader."}


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
                       no active admin users exist in the database, 409 if a
                       registration/bind window owns the reader.
    """
    if ctx_module.context is None:
        raise HTTPException(status_code=503, detail="System not ready.")

    # Find the first active admin user in the database
    admin_user = UserRepository.first_active_admin(db)
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

    # A live card/bind window owns the reader — a new session would swallow
    # the awaited tap as a login and its end would kill the window. Refuse
    # while one is armed. (The existing-session overlay branch above stays
    # reachable: updating the flag does not touch the reader.)
    conflict = _pending_nfc_conflict()
    if conflict:
        raise HTTPException(status_code=409, detail=conflict)

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

    Use case: when someone's name is not in the registrants table and they
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

    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Name is required.")

    conflict = _pending_nfc_conflict()
    if conflict:
        raise HTTPException(status_code=409, detail=conflict)

    conflict = arm_pending_registration(
        ctx_module.context,
        PendingRegistration(display_name=name),
    )
    if conflict:
        raise HTTPException(status_code=409, detail=conflict)
    logger.info(
        "Admin-initiated registration for '%s' by admin %s. Awaiting card tap.",
        name, user_session.user.display_name,
    )
    return {"success": True, "message": "Tap the new user's NFC card to complete registration."}


# --- People (kiosk + dashboard share one policy, two gates) ------------------

def _require_admin_session(user_session: UserSession) -> None:
    """403 unless the live kiosk session belongs to an admin."""
    if user_session.user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin access required.")


# People edits (role / active flag) serialize across the kiosk and dashboard
# request threads: the last-active-admin count must be checked and written
# under one lock or two racing PATCHes could both see ">1 admin" and leave
# the locker with none.
_person_edit_lock = threading.Lock()


def _apply_person_edit(
    db: Session,
    user_id: int,
    body: UserUpdateBody,
    *,
    actor: str | None = None,
) -> dict:
    """Apply a People row edit; map service refusals to HTTP errors.

    The whole check+write+commit runs under ``_person_edit_lock``. The commit
    on lock entry ends the request session's pre-lock read snapshot, so
    ``update_person``'s last-admin and holds-device checks see every write
    committed before this request waited on the lock. (It commits nothing
    meaningful in production — the edit itself is what follows.)

    Args:
        db: Active database session (auto-committed by ``get_db``).
        user_id: Primary key of the user being edited.
        body: Role and/or active flag.
        actor: Who made the edit (kiosk admin name or "dashboard"), for the
            audit log line.

    Returns:
        dict: The updated ``person_record``.
    """
    with _person_edit_lock:
        db.commit()
        user = UserRepository.find_by_id(db, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="Person not found.")
        try:
            update_person(
                db, user, role=body.role, is_active=body.is_active, actor=actor
            )
        except (LastAdminError, PersonHoldsDeviceError) as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        db.commit()
        return person_record(user)


def _parse_person_role(role: str) -> str:
    """Validate a People role value; 422 on anything but user/admin."""
    try:
        return UserRole((role or "").strip()).value
    except ValueError as e:
        raise HTTPException(
            status_code=422, detail="Role must be 'user' or 'admin'."
        ) from e


def _arm_card_window(
    pending: PendingRegistration, *, require_idle: bool
) -> None:
    """Arm a 60s card-tap window (enroll or replace) or raise the HTTP error.

    ``require_idle`` is the dashboard rule: a remote arm is refused while a
    kiosk session is live, so a borrower's card tap cannot land inside a
    window it did not ask for.
    """
    ctx = ctx_module.context
    if ctx is None:
        raise HTTPException(status_code=503, detail="System not ready.")
    if require_idle and ctx.session_mgr.has_active_session:
        raise HTTPException(
            status_code=409,
            detail="A kiosk session is active. Arm from the kiosk or end the session.",
        )
    conflict = arm_pending_registration(ctx, pending)
    if conflict:
        raise HTTPException(status_code=409, detail=conflict)


@router.get("/api/admin/users")
def list_people(
    db: Session = Depends(get_db),
    user_session: UserSession = Depends(require_session),
):
    """People list for the kiosk admin menu (loopback session, admin only).

    Args:
        db: Active database session (injected by ``get_db``).
        user_session: The active admin session (injected by ``require_session``).

    Returns:
        list[dict]: ``person_record`` per user — id, name, role, active,
                    registered date. Card credentials are never included.
    """
    _require_admin_session(user_session)
    return [person_record(u) for u in UserRepository.list_all(db)]


@router.patch("/api/admin/users/{user_id}")
def edit_person_kiosk(
    user_id: int,
    body: UserUpdateBody,
    db: Session = Depends(get_db),
    user_session: UserSession = Depends(require_session),
):
    """Edit one person's role or active flag from the kiosk admin menu.

    Args:
        user_id: Primary key of the user.
        body: ``role`` and/or ``is_active``.
        db: Active database session (injected by ``get_db``).
        user_session: The active admin session (injected by ``require_session``).

    Returns:
        dict: The updated ``person_record``.

    Raises:
        HTTPException: 403 if not admin, 404 unknown person, 409 last-admin /
            still-holding-device refusal, 422 bad role or empty edit.
    """
    _require_admin_session(user_session)
    return _apply_person_edit(
        db, user_id, body, actor=user_session.user.display_name
    )


@router.post("/api/admin/users")
def add_person_kiosk(
    body: UserAddBody,
    user_session: UserSession = Depends(require_session),
):
    """People add-person: arm a 60s window; the tapped card becomes the person.

    Args:
        body: Display name plus role ("user" or "admin").
        user_session: The active admin session (injected by ``require_session``).

    Returns:
        dict: ``{"success": True, "message": str}``.

    Raises:
        HTTPException: 403 if not admin, 503 if not ready, 422 blank name or
            bad role, 409 if a window already owns the reader.
    """
    _require_admin_session(user_session)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Name is required.")
    role = _parse_person_role(body.role)
    _arm_card_window(
        PendingRegistration(display_name=name, role=role),
        require_idle=False,
    )
    logger.info(
        "People add-person armed for '%s' (role=%s) by admin %s.",
        name,
        role,
        user_session.user.display_name,
    )
    return {"success": True, "message": "Tap the new card on the reader."}


@router.post("/api/admin/users/{user_id}/replace-card")
def replace_card_kiosk(
    user_id: int,
    db: Session = Depends(get_db),
    user_session: UserSession = Depends(require_session),
):
    """Arm a 60s window; the tapped card rebinds this person's work card.

    Args:
        user_id: Primary key of the user receiving the new card.
        db: Active database session (injected by ``get_db``).
        user_session: The active admin session (injected by ``require_session``).

    Returns:
        dict: ``{"success": True, "message": str}``.

    Raises:
        HTTPException: 403 if not admin, 503 if not ready, 404 unknown person,
            409 if a window already owns the reader.
    """
    _require_admin_session(user_session)
    user = UserRepository.find_by_id(db, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Person not found.")
    _arm_card_window(
        PendingRegistration(
            display_name=user.display_name,
            role=user.role.value,
            replace_user_id=user.id,
        ),
        require_idle=False,
    )
    logger.info(
        "Card replace armed for %s (id=%d) by admin %s.",
        user.display_name,
        user.id,
        user_session.user.display_name,
    )
    return {
        "success": True,
        "message": f"Tap the new card for {user.display_name}.",
    }


@router.get("/api/admin/devices")
def list_admin_devices(
    db: Session = Depends(get_db),
    user_session: UserSession = Depends(require_session),
):
    """Every locker unit for the admin manage list (bind, unbind, slot).

    Unlike the kiosk ``GET /api/devices`` — which feeds the borrow/return
    grids — this feed includes rows with no sticker yet: binding one is
    what the list is for. Same record shape plus the internal ``id`` the
    bind-tag endpoints key on.

    Args:
        db: Database session (injected by ``get_db``).
        user_session: The active admin session (injected by
            ``require_session``).

    Returns:
        list[dict]: One dict per locker device, ordered by slot then name.

    Raises:
        HTTPException: 403 if not admin.
    """
    if user_session.user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin access required.")

    return [
        {"id": d.id, **device_record(d), "image_path": d.image_path}
        for d in DeviceRepository.list_by_slot(db)
    ]


@router.get("/api/admin/devices/registerable")
def list_registerable_devices(
    db: Session = Depends(get_db),
    user_session: UserSession = Depends(require_session),
):
    """Catalog rows the kiosk Register Device screen can offer (admin).

    Rows whose place is the in-locker word and which have no cabinet slot
    yet — the SQLite catalog is the lookup, so the screen no longer waits
    on a share file.

    Args:
        db: Database session (injected by ``get_db``).
        user_session: The active admin session (injected by ``require_session``).

    Returns:
        list[dict]: ``pm_number`` and ``name`` per eligible row.

    Raises:
        HTTPException: 403 if not admin.
    """
    if user_session.user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin access required.")

    from smart_locker.services.device_catalog import registerable_devices

    return [
        {"pm_number": d.pm_number, "name": d.name}
        for d in registerable_devices(db)
    ]


@router.post("/api/admin/devices/register")
def register_locker_device(
    body: RegisterDeviceRequest,
    db: Session = Depends(get_db),
    user_session: UserSession = Depends(require_session),
):
    """Promote a catalog row into a locker slot (PM + free slot) and arm NFC bind.

    Looks up the PM in the SQLite catalog, assigns the chosen slot, then
    waits for the sticker tap (same window as bind-tag).

    Args:
        body: PM number and locker slot.
        db: Database session (injected by ``get_db``).
        user_session: The active admin session (injected by ``require_session``).

    Returns:
        dict: ``success``, ``device_id``, ``name``, ``pm_number``, ``locker_slot``.

    Raises:
        HTTPException: 503 if not ready, 403 if not admin,
            404 if PM unknown, 409 if PM or slot taken or the row is not
            marked for the locker.
    """
    if ctx_module.context is None:
        raise HTTPException(status_code=503, detail="System not ready.")

    if user_session.user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin access required.")

    conflict = _pending_nfc_conflict()
    if conflict:
        raise HTTPException(status_code=409, detail=conflict)

    from smart_locker.services.device_registration import (
        AlreadyRegistered,
        InvalidSlot,
        NotRegisterable,
        SlotTaken,
        UnknownPm,
        register_locker_device as create_from_catalog,
    )

    try:
        device = create_from_catalog(db, body.pm_number, body.locker_slot)
    except UnknownPm as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except (SlotTaken, AlreadyRegistered, NotRegisterable) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except InvalidSlot as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    db.flush()
    # Arm before committing: a bind conflict rolls the slot assignment back
    # so a 409 never leaves the registration half-applied.
    bind = PendingTagBind(device_id=device.id)
    conflict = arm_pending_tag_bind(ctx_module.context, bind)
    if conflict:
        db.rollback()
        raise HTTPException(status_code=409, detail=conflict)
    try:
        db.commit()
    except Exception:
        db.rollback()
        # Clear only this request's armed window — a bind armed after this
        # window was consumed by a tap must survive.
        clear_pending_tag_bind_if(ctx_module.context, bind)
        raise
    from smart_locker.sync import mirror

    mirror.mark_dirty()
    mirror.schedule_flush()
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
        HTTPException: 403 if not admin, 404 if missing, 409 if the row is
            not a locker unit or the slot is taken.
    """
    if user_session.user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin access required.")

    device = DeviceRepository.find_by_id(db, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found.")
    if not is_registered(device):
        raise HTTPException(
            status_code=409,
            detail="Device is not in the locker — use Register Device.",
        )

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

    # The committed slot change is a catalog change: the after-commit
    # listener turns this flag into mark_dirty + schedule_flush.
    db.info["mirror_dirty_pending"] = True
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
            the device does not exist or is not a locker unit, 409 if a
            pending window owns the reader.
    """
    if ctx_module.context is None:
        raise HTTPException(status_code=503, detail="System not ready.")

    if user_session.user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin access required.")

    device = DeviceRepository.find_by_id(db, device_id)
    if device is None or not is_registered(device):
        raise HTTPException(status_code=404, detail="Device not found.")

    conflict = arm_pending_tag_bind(
        ctx_module.context, PendingTagBind(device_id=device.id)
    )
    if conflict:
        raise HTTPException(status_code=409, detail=conflict)
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
        HTTPException: 403 if not admin, 404 if the device does not exist,
            409 while the unit is borrowed.
    """
    if user_session.user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin access required.")

    device = DeviceRepository.find_by_id(db, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found.")

    from smart_locker.services.device_catalog import DeviceBorrowed

    try:
        clear_device_tag(db, device)
    except DeviceBorrowed as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    if ctx_module.context is not None:
        # Cancel only a bind window aimed at this device — a window armed
        # for another device must survive the unbind.
        clear_pending_tag_bind_for_device(ctx_module.context, device.id)
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
    """Run one mirror tick now (admin only).

    The SQLite catalog is the source of truth — the tick detects hand edits
    in the mirror file and flushes any pending write. Only users with ADMIN
    role may invoke this.

    Args:
        user_session: The active session (injected by ``require_session``).

    Returns:
        dict: Tick summary — ``flushed``, ``written``, ``external``,
              ``skipped``, ``error``.

    Raises:
        HTTPException: 403 if not admin, 409 if a tick is already running.
    """
    if user_session.user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin access required.")

    from smart_locker.database.engine import get_engine
    from smart_locker.sync.scheduler import run_mirror_tick

    result = run_mirror_tick(get_engine(), trigger="manual")
    if result.get("skipped") == "in_progress":
        raise HTTPException(
            status_code=409, detail="A mirror sync is already running."
        )
    return {"success": result.get("error") is None, **result}


@router.post("/api/admin/sync-preview")
def preview_source_sync(
    user_session: UserSession = Depends(require_session),
):
    """List hand edits found in the mirror file (admin only).

    The Pi never merges sheet edits silently — this previews the differences
    an admin would apply with ``POST /api/dashboard/mirror/apply``.

    Args:
        user_session: The active session (injected by ``require_session``).

    Returns:
        dict: ``preview`` plus ``diffs`` (per-field sheet-vs-database
              differences) or ``error`` when the file cannot be read.

    Raises:
        HTTPException: 403 if not admin.
    """
    if user_session.user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin access required.")

    from smart_locker.database.engine import get_engine
    from smart_locker.sync import mirror

    diffs, err = mirror.external_diffs()
    return {"preview": True, "diffs": diffs, "count": len(diffs), "error": err}


@router.get("/api/admin/sync-status")
def get_sync_status(user_session: UserSession = Depends(require_session)):
    """Return the most recent mirror-tick outcome plus mirror state (admin only).

    Powers the dashboard "last synced …" line. Reports when the last tick
    ran, what triggered it (startup/interval/change/manual), whether it
    succeeded, and the mirror's pending/external state. Includes ``at_local``
    and ``ago`` for the admin footer clock.

    Args:
        user_session: The active session (injected by ``require_session``).

    Returns:
        dict: The last-sync snapshot plus a ``mirror`` status block.

    Raises:
        HTTPException: 403 if not admin.
    """
    if user_session.user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin access required.")
    from smart_locker.sync import mirror

    return {**sync_status.get(), "mirror": mirror.mirror_status()}


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


def _require_update_authorized(request: Request, db: Session) -> None:
    """Gate POST /api/admin/update. Never returns on refusal.

    The update does not require an enrolled admin — a first-boot box with no
    admin and no dashboard password may still take a stick, but only from the
    kiosk itself (loopback). Once ``SMART_LOCKER_DASHBOARD_ADMIN_SECRET`` is
    set, the ``X-Smart-Locker-Admin`` header is what authorizes the call, from
    the kiosk or the LAN. A loopback admin kiosk session also still
    authorizes, so the cabinet behaves exactly as before. There is no
    unauthenticated LAN path — a remote caller cannot own the physical
    first-boot ceremony.

    Args:
        request: Incoming ASGI request (client address + admin header).
        db: Active database session (injected by ``get_db``).

    Raises:
        HTTPException: 401 without a matching secret or session, or when no
                       secret is configured and an admin exists (fail closed);
                       403 for a non-admin session.
    """
    secret = dashboard_admin_secret()
    provided = request.headers.get(DASHBOARD_ADMIN_HEADER) or ""
    if secret and _secret_matches(provided, secret):
        return

    if _is_loopback_request(request):
        ctx = ctx_module.context
        session = ctx.session_mgr.current_session if ctx is not None else None
        if session is not None:
            if session.user.role == UserRole.ADMIN:
                return
            raise HTTPException(status_code=403, detail="Admin access required.")
        if not secret and UserRepository.first_active_admin(db) is None:
            # First boot at the kiosk itself: no admin enrolled and no
            # password stored yet — the physical Setup screen runs the update.
            return
        raise HTTPException(status_code=401, detail="No active session.")

    # LAN: only the secret authorizes (checked above). First-boot openness is
    # kiosk-local — a LAN host must never launch the root updater unit.
    raise HTTPException(
        status_code=401,
        detail=(
            "Dashboard admin authorization required."
            if secret
            else "Dashboard admin is not configured."
        ),
    )


@router.post("/api/admin/update")
def trigger_update(
    request: Request,
    db: Session = Depends(get_db),
):
    """Launch the safe software-update script out-of-process.

    Backs the admin-panel "Software Update" button and the first-boot Setup
    screen. Authorized by the dashboard admin secret (header
    ``X-Smart-Locker-Admin``, kiosk or LAN), by a loopback admin kiosk
    session, or — on a box with no admin and no password yet — openly so a
    first boot can take a stick. The update itself is applied by
    ``deploy/install/update.sh``, which finds an unpacked ``locker-updates``
    tree (USB first, then ``$APP_DIR/locker-updates``), snapshots the DB +
    code, swaps in the new version, migrates, restarts the service,
    health-checks, and AUTO-ROLLS-BACK on failure — so a bad update
    self-reverts on a box no one is standing next to.

    The script restarts the very systemd service that hosts this request, so it
    must run in its OWN cgroup; we launch it as a transient ``systemd-run`` unit
    so the restart cannot kill the updater mid-apply. On a non-Pi/dev host (no
    ``systemd-run``, or the script is absent) this returns 503 with a clear
    message rather than pretending to update.

    Returns:
        dict: ``{"started": True, "message": ...}`` once the updater is launched.

    Raises:
        HTTPException: 401/403 when not authorized; 503 if updates aren't
                       runnable here; 500 if the updater unit could not launch.
    """
    _require_update_authorized(request, db)

    try:
        launch_update(BASE_DIR, _SYSTEMD_RUN)
    except ApplianceUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except ApplianceError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    logger.info("Software update launched.")
    return {"started": True, "message": "Update started. The kiosk will restart briefly."}


@router.post("/api/admin/exit-kiosk", include_in_schema=False)
@router.post("/api/admin/stop-system")
def admin_stop_system(
    background_tasks: BackgroundTasks,
    _: None = Depends(require_loopback),
    user_session: UserSession = Depends(require_session),
):
    """Stop the whole locker system (admin only): Chromium, then the service.

    The reply goes out first; a background task then SIGTERMs the kiosk
    browser and runs ``sudo -n /usr/bin/systemctl stop smart-locker``. An
    explicit ``systemctl stop`` stays stopped — ``Restart=always`` does not
    bring the service back, and nothing runs until the next boot. On a
    Windows/dev host (no systemctl) this returns 503.

    Args:
        background_tasks: FastAPI post-response task runner.
        user_session: The active session (injected by ``require_session``).

    Returns:
        dict: ``{"ok": True, "message": ...}`` — the stop then follows.

    Raises:
        HTTPException: 403 if not admin; 503 if systemd is absent.
    """
    if user_session.user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin access required.")
    if _SYSTEMCTL is None:
        raise HTTPException(
            status_code=503,
            detail="Stop system runs on the Raspberry Pi appliance only.",
        )
    background_tasks.add_task(stop_system)
    _end_kiosk_session()
    logger.info(
        "System stop requested by admin %s.", user_session.user.display_name
    )
    return {"ok": True, "message": "Stopping the locker system."}


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
    """Public company catalog from SQLite — the workbook is only a mirror.

    Every catalog row is returned, cabinet units included. ``in_locker``
    marks rows with a locker slot so the Inventory tab offers owner edit
    only for non-cabinet devices.

    Args:
        db: Active database session (injected by ``get_db``).

    Returns:
        list[dict]: One dict per catalog device.
    """
    return [catalog_record(d) for d in DeviceRepository.list_all(db)]


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
    return [device_record(d) for d in DeviceRepository.list_by_slot(db)]


@router.get("/api/dashboard/owners")
def dashboard_owners(db: Session = Depends(get_db)):
    """Names for the owner-edit dropdown (public — owner edit has no password).

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
):
    """Change owner for one non-cabinet PM — public, no admin password.

    Updates the stored place in SQLite; the mirror catches up when the file
    can be written. Cabinet units are refused (owner stays with kiosk
    borrow/return).

    Args:
        body: PM number and new owner text.
        db: Active database session (injected by ``get_db``).

    Returns:
        dict: ``ok``, ``pm_number``, ``owner``, ``locker``.

    Raises:
        HTTPException: 400 empty PM; 404 PM not in the catalog;
                       409 locker PM.
    """
    try:
        result = set_owner(db, body.pm_number, body.owner)
    except InvalidOwnerRequest as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except UnknownPm as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except LockerOwned as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {
        "ok": True,
        "pm_number": result.pm_number,
        "owner": result.owner,
        "locker": result.locker,
    }


def _parse_calibration(raw: str | None) -> "date | None":
    """Convert a dashboard date field to a ``date`` (422 on garbage)."""
    from smart_locker.sync.catalog_sheet import parse_date

    text = (raw or "").strip()
    if not text:
        return None
    parsed = parse_date(text)
    if parsed is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Calibration date '{text}' is not a date "
                "(YYYY-MM-DD, DD.MM.YYYY, or DD/MM/YYYY — day-first)."
            ),
        )
    return parsed


@router.post("/api/dashboard/devices")
def dashboard_add_device(
    body: DeviceAddBody,
    db: Session = Depends(get_db),
    _: None = Depends(require_dashboard_admin),
):
    """Add one device to the catalog (dashboard admin secret required).

    The row lands in SQLite immediately; the mirror catches up when the file
    can be written. The new row is not a cabinet unit — Register Device on
    the kiosk puts it into a slot.

    Args:
        body: Catalog fields; ``pm_number`` and ``name`` are required.
        db: Active database session (injected by ``get_db``).

    Returns:
        dict: ``ok`` plus the stored ``catalog_record``.

    Raises:
        HTTPException: 401 without secret; 422 for empty id/name or a bad
                       date; 409 when the id is already cataloged.
    """
    from smart_locker.services.device_catalog import (
        DuplicatePm,
        DuplicateSerial,
        InvalidCatalogField,
        add_device,
    )

    try:
        device = add_device(
            db,
            pm_number=body.pm_number,
            name=body.name,
            device_type=body.device_type,
            serial_number=body.serial_number,
            manufacturer=body.manufacturer,
            model=body.model,
            calibration_due=_parse_calibration(body.calibration_due),
            location=body.location,
        )
    except InvalidCatalogField as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except (DuplicatePm, DuplicateSerial) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"ok": True, "device": catalog_record(device)}


@router.patch("/api/dashboard/devices/{pm_number}")
def dashboard_edit_device(
    pm_number: str,
    body: DeviceEditBody,
    db: Session = Depends(get_db),
    _: None = Depends(require_dashboard_admin),
):
    """Edit catalog fields on one device (dashboard admin secret required).

    Editable: name, device_type, serial_number, manufacturer, model,
    calibration_due — plus ``location`` while the row is not a cabinet unit.

    Args:
        pm_number: Catalog id of the device.
        body: Fields to change; absent fields are left alone.
        db: Active database session (injected by ``get_db``).

    Returns:
        dict: ``ok``, ``changed``, plus the stored ``catalog_record``.

    Raises:
        HTTPException: 401 without secret; 404 unknown PM; 422 invalid
                       field; 409 when location is sent for a cabinet unit
                       or the serial is already held by another row.
    """
    from smart_locker.services.device_catalog import (
        DuplicateSerial,
        InvalidCatalogField,
        LockerOwned,
        update_device_fields,
    )

    device = DeviceRepository.find_by_pm(db, pm_number.strip())
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found.")

    fields = body.model_dump(exclude_unset=True)
    if "calibration_due" in fields:
        fields["calibration_due"] = _parse_calibration(fields["calibration_due"])
    try:
        changed = update_device_fields(db, device, fields)
    except (LockerOwned, DuplicateSerial) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except InvalidCatalogField as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return {"ok": True, "changed": changed, "device": catalog_record(device)}


@router.delete("/api/dashboard/devices/{pm_number}")
def dashboard_remove_device(
    pm_number: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_dashboard_admin),
):
    """Remove one device from the catalog (dashboard admin secret required).

    A borrowed unit is refused — return it at the kiosk first. A unit with
    audit history is refused too — the borrow trail is kept, so only
    never-used rows can be removed.

    Args:
        pm_number: Catalog id of the device.
        db: Active database session (injected by ``get_db``).

    Returns:
        dict: ``ok`` and ``pm_number``.

    Raises:
        HTTPException: 401 without secret; 404 unknown PM; 409 borrowed,
                       history-bearing, or a live tag-bind window on the row.
    """
    from smart_locker.services.device_catalog import (
        DeviceBorrowed,
        DeviceHasHistory,
        remove_device,
    )

    device = DeviceRepository.find_by_pm(db, pm_number.strip())
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found.")

    # Deleting the row out from under an armed bind window would leave the
    # next sticker tap binding a device that no longer exists.
    with pending_state_lock:
        ctx = ctx_module.context
        bind = ctx.pending_tag_bind if ctx is not None else None
        live_bind = (
            bind is not None
            and bind.device_id == device.id
            and not bind.is_expired
        )
    if live_bind:
        raise HTTPException(
            status_code=409,
            detail="A tag bind is in progress for this device.",
        )

    pm = device.pm_number
    try:
        remove_device(db, device)
        db.commit()
    except (DeviceBorrowed, DeviceHasHistory) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except IntegrityError as e:
        # A borrow landed between the borrowed-check and the delete — the
        # transaction FK makes this a conflict, not a 500.
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"{pm} cannot be removed while records reference it.",
        ) from e
    return {"ok": True, "pm_number": pm}


@router.post("/api/dashboard/devices/{pm_number}/maintenance")
def dashboard_to_maintenance(
    pm_number: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_dashboard_admin),
):
    """Take one cabinet unit out of service (dashboard admin secret required).

    The unit cannot be borrowed while in maintenance; its Location cell in
    the mirror becomes the maintenance token on the next write. Refused
    while borrowed — return it at the kiosk first.

    Args:
        pm_number: Catalog id of the device.
        db: Active database session (injected by ``get_db``).

    Returns:
        dict: ``ok``, ``changed``, plus the stored ``catalog_record``.

    Raises:
        HTTPException: 401 without secret; 404 unknown PM; 409 non-cabinet
                       row or borrowed unit.
    """
    from smart_locker.services.device_catalog import (
        DeviceBorrowed,
        NotCabinetUnit,
        UnknownPm,
        to_maintenance,
    )

    device = DeviceRepository.find_by_pm(db, pm_number.strip())
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found.")
    try:
        changed = to_maintenance(db, device)
    except UnknownPm as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except (NotCabinetUnit, DeviceBorrowed) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"ok": True, "changed": changed, "device": catalog_record(device)}


@router.post("/api/dashboard/devices/{pm_number}/back-in-service")
def dashboard_back_in_service(
    pm_number: str,
    body: ServiceReturnBody,
    db: Session = Depends(get_db),
    _: None = Depends(require_dashboard_admin),
):
    """Return a maintenance unit to service (dashboard admin secret required).

    Saves the new calibration date and clears the maintenance status. A
    date that is not in the future is stored but keeps the unit
    unborrowable through the normal calibration gate.

    Args:
        pm_number: Catalog id of the device.
        body: ``calibration_due`` — the new calibration date (required).
        db: Active database session (injected by ``get_db``).

    Returns:
        dict: ``ok`` plus the stored ``catalog_record``.

    Raises:
        HTTPException: 401 without secret; 404 unknown PM; 422 missing or
                       unparseable date; 409 non-cabinet row or a unit not
                       in maintenance.
    """
    from smart_locker.services.device_catalog import (
        InvalidCatalogField,
        NotCabinetUnit,
        NotInMaintenance,
        UnknownPm,
        back_in_service,
    )

    device = DeviceRepository.find_by_pm(db, pm_number.strip())
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found.")
    try:
        changed = back_in_service(
            db, device, _parse_calibration(body.calibration_due)
        )
    except UnknownPm as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except InvalidCatalogField as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except (NotCabinetUnit, NotInMaintenance) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"ok": True, "changed": changed, "device": catalog_record(device)}


@router.get("/api/dashboard/mirror")
def dashboard_mirror_status() -> dict:
    """Public mirror status for the dashboard warning line.

    The workbook is a hidden Pi-written mirror — this reports whether a
    write is pending, the file is unavailable, or hand edits wait for an
    admin decision. Never raises; an unconfigured mirror reports
    ``configured: false``.
    """
    from smart_locker.sync import mirror

    return mirror.mirror_status()


@router.get("/api/dashboard/mirror/diffs")
def dashboard_mirror_diffs(
    _: None = Depends(require_dashboard_admin),
):
    """Hand edits found in the mirror file (dashboard admin secret required).

    Returns:
        dict: ``diffs`` — one entry per sheet/database difference.
    """
    from smart_locker.database.engine import get_engine
    from smart_locker.sync import mirror

    diffs, err = mirror.external_diffs()
    return {"diffs": diffs, "error": err}


@router.post("/api/dashboard/mirror/apply")
def dashboard_mirror_apply(
    _: None = Depends(require_dashboard_admin),
):
    """Apply the sheet's hand edits to the database (admin secret required).

    Added rows insert, changed cells update catalog fields, removed rows
    delete when the unit is neither in the cabinet nor borrowed. The mirror
    then converges on the next write.

    Returns:
        dict: ``ok``, ``applied``, ``skipped`` counts.

    Raises:
        HTTPException: 401 without secret; 503 when the file cannot be read.
    """
    from smart_locker.database.engine import get_engine
    from smart_locker.sync import mirror

    try:
        result = mirror.apply_external(get_engine())
    except mirror.MirrorUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return {"ok": True, **result}


@router.post("/api/dashboard/mirror/dismiss")
def dashboard_mirror_dismiss(
    _: None = Depends(require_dashboard_admin),
):
    """Keep the database and overwrite the sheet edits (admin secret required).

    Returns:
        dict: ``ok``.
    """
    from smart_locker.sync import mirror

    mirror.dismiss_external()
    return {"ok": True}


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
    if device is None or not is_registered(device):
        raise HTTPException(status_code=404, detail="Device not found.")

    conflict = arm_pending_tag_bind(
        ctx_module.context,
        PendingTagBind(device_id=device.id, from_dashboard=True),
    )
    if conflict:
        raise HTTPException(status_code=409, detail=conflict)
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
        HTTPException: 401 without secret; 404 if the PM is not a locker
            device; 409 while the unit is borrowed.
    """
    pm = body.pm_number.strip()
    device = DeviceRepository.find_by_pm(db, pm)
    if device is None or not is_registered(device):
        raise HTTPException(status_code=404, detail="Device not found.")

    from smart_locker.services.device_catalog import DeviceBorrowed

    try:
        clear_device_tag(db, device)
    except DeviceBorrowed as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    if ctx_module.context is not None:
        # Cancel only a bind window aimed at this device — a window armed
        # for another device must survive the unbind.
        clear_pending_tag_bind_for_device(ctx_module.context, device.id)
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
    chronological order. Requires ``X-Smart-Locker-Admin``. The Admin
    button reveal is not authorization.

    Args:
        db: Active database session (injected by ``get_db``).

    Returns:
        list[dict]: One dict per transaction with timestamp, user, device,
                    type, performed-by (for admin returns), and notes.
    """
    transactions = TransactionRepository.get_dashboard_history(db)

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
    Requires ``X-Smart-Locker-Admin``. The Admin button is not authorization.

    Args:
        db: Active database session (injected by ``get_db``).

    Returns:
        list[dict]: ``person_record`` per user — id, display name, role,
                    active status, and registration timestamp.
    """
    return [person_record(u) for u in UserRepository.list_all(db)]


@router.patch("/api/dashboard/users/{user_id}")
def edit_person_dashboard(
    user_id: int,
    body: UserUpdateBody,
    db: Session = Depends(get_db),
    _: None = Depends(require_dashboard_admin),
):
    """Edit one person's role or active flag from the dashboard People list.

    Requires ``X-Smart-Locker-Admin`` (fail closed if unset). Same refusals as
    the kiosk: the last active admin cannot be demoted or deactivated, and a
    person holding a borrowed device cannot be deactivated.

    Args:
        user_id: Primary key of the user.
        body: ``role`` and/or ``is_active``.
        db: Active database session (injected by ``get_db``).

    Returns:
        dict: The updated ``person_record``.

    Raises:
        HTTPException: 401 without secret; 404 unknown person; 409 last-admin /
            still-holding-device refusal; 422 bad role or empty edit.
    """
    return _apply_person_edit(db, user_id, body, actor="dashboard")


@router.post("/api/dashboard/users")
def add_person_dashboard(
    body: UserAddBody,
    _: None = Depends(require_dashboard_admin),
):
    """People add-person from the dashboard: arm the cabinet reader 60s.

    A remote PC cannot write a card by itself — this only arms the window;
    the new card is tapped on the locker. Refused while a kiosk session is
    live, like ``/api/dashboard/bind-tag``.

    Args:
        body: Display name plus role ("user" or "admin").

    Returns:
        dict: ``ok`` plus the instruction message.

    Raises:
        HTTPException: 401 without secret; 503 if not ready; 422 blank name or
            bad role; 409 if a session or pending window owns the reader.
    """
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Name is required.")
    role = _parse_person_role(body.role)
    _arm_card_window(
        PendingRegistration(display_name=name, role=role, from_dashboard=True),
        require_idle=True,
    )
    logger.info(
        "Dashboard armed add-person for '%s' (role=%s). Awaiting card at kiosk.",
        name,
        role,
    )
    return {"ok": True, "message": "Tap the new card on the kiosk."}


@router.post("/api/dashboard/users/{user_id}/replace-card")
def replace_card_dashboard(
    user_id: int,
    db: Session = Depends(get_db),
    _: None = Depends(require_dashboard_admin),
):
    """Arm the cabinet reader 60s; the tapped card rebinds this person's card.

    Requires ``X-Smart-Locker-Admin``. The replacement happens on the locker
    reader — the LAN request only opens the window.

    Args:
        user_id: Primary key of the user receiving the new card.
        db: Active database session (injected by ``get_db``).

    Returns:
        dict: ``ok`` plus the instruction message.

    Raises:
        HTTPException: 401 without secret; 503 if not ready; 404 unknown
            person; 409 if a session or pending window owns the reader.
    """
    user = UserRepository.find_by_id(db, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Person not found.")
    _arm_card_window(
        PendingRegistration(
            display_name=user.display_name,
            role=user.role.value,
            replace_user_id=user.id,
            from_dashboard=True,
        ),
        require_idle=True,
    )
    logger.info(
        "Dashboard armed card replace for %s (id=%d). Awaiting card at kiosk.",
        user.display_name,
        user.id,
    )
    return {
        "ok": True,
        "message": f"Tap the new card for {user.display_name} on the kiosk.",
    }


@router.get("/api/dashboard/card-window")
def dashboard_card_window_status(
    _: None = Depends(require_dashboard_admin),
) -> dict:
    """Poll the card-window state (dashboard admin secret required).

    The dashboard People editor arms 60s windows remotely but the card is
    tapped on the locker — this is how the remote screen learns the window
    is waiting, saw its tap resolve, or went back to idle.

    A still-armed registration reports ``armed``; a resolved one reports the
    latch the NFC bridge wrote (or ``cancelled``/``expired`` when the window
    ended without a tap); otherwise ``idle``. An expired window is dropped
    here the same way the bridge drops it, and its latch becomes ``expired``.

    Returns:
        dict: ``{"state": "armed"|"resolved"|"idle", ...}`` — armed carries
              ``display_name``, ``replace``, ``seconds_left``; resolved
              carries ``outcome``, ``reason``, ``user``, ``display_name``,
              ``replace``.

    Raises:
        HTTPException: 401 without secret; 503 if system not ready.
    """
    ctx = ctx_module.context
    if ctx is None:
        raise HTTPException(status_code=503, detail="System not ready.")

    with pending_state_lock:
        reg = ctx.pending_registration
        if reg is not None and reg.is_expired:
            _latch_card_result(ctx, "expired", reg)
            assign_pending_registration(ctx, None)
            reg = None
        if reg is not None:
            elapsed = time.monotonic() - reg.created_at
            return {
                "state": "armed",
                "display_name": reg.display_name,
                "replace": reg.replace_user_id is not None,
                "seconds_left": max(
                    0, math.ceil(REGISTRATION_TIMEOUT_SECONDS - elapsed)
                ),
            }
        result = getattr(ctx, "last_card_result", None)

    if isinstance(result, dict):
        return {
            "state": "resolved",
            "outcome": result.get("outcome"),
            "reason": result.get("reason"),
            "user": result.get("user"),
            "display_name": result.get("display_name"),
            "replace": result.get("replace_user_id") is not None,
        }
    return {"state": "idle"}


@router.post("/api/dashboard/card-window/cancel")
def dashboard_cancel_card_window(
    _: None = Depends(require_dashboard_admin),
) -> dict:
    """Cancel an armed card/bind window the dashboard itself armed.

    Requires ``X-Smart-Locker-Admin``. Only ``from_dashboard`` windows are
    cleared — a window armed at the kiosk (a self-registration the user is
    about to tap into, or a kiosk tag bind) is not the dashboard's to kill.
    Cancelling a registration latches ``last_card_result`` as ``cancelled``
    so the status poll reports a settled outcome instead of going silent.

    Returns:
        dict: ``{"ok": True, "cancelled": bool}`` — ``cancelled`` is True if
              a dashboard-armed window was actually pending and dropped.

    Raises:
        HTTPException: 401 without secret; 503 if system not ready.
    """
    ctx = ctx_module.context
    if ctx is None:
        raise HTTPException(status_code=503, detail="System not ready.")

    cancelled = False
    with pending_state_lock:
        reg = ctx.pending_registration
        if reg is not None and bool(getattr(reg, "from_dashboard", False)):
            _latch_card_result(ctx, "cancelled", reg)
            assign_pending_registration(ctx, None)
            cancelled = True
        bind = ctx.pending_tag_bind
        if bind is not None and bool(getattr(bind, "from_dashboard", False)):
            assign_pending_tag_bind(ctx, None)
            cancelled = True
    if cancelled:
        logger.info("Dashboard cancelled its armed card/bind window.")
    return {"ok": True, "cancelled": cancelled}
