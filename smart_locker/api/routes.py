"""REST API endpoints and SSE event stream for the Smart Locker kiosk."""

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import smart_locker.api.app_context as ctx_module
from smart_locker.api.app_context import PendingRegistration
from smart_locker.auth.session_manager import UserSession
from smart_locker.database.engine import get_session_factory
from smart_locker.database.models import DeviceStatus, UserRole
from smart_locker.database.repositories import DeviceRepository
from smart_locker.services.locker_service import LockerService

logger = logging.getLogger(__name__)

router = APIRouter()


# --- Dependencies -----------------------------------------------------------

def get_db() -> Session:
    """Yield a database session for the request, with auto-commit/rollback."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        factory.remove()


def require_session() -> UserSession:
    """Require an active kiosk session. Returns the UserSession or 401."""
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
    """Server-Sent Events stream for NFC and session events."""

    async def event_generator():
        while True:
            try:
                data = await asyncio.wait_for(ctx_module.context.sse_queue.get(), timeout=15.0)
                event_name = data.get("event", "message")
                yield f"event: {event_name}\ndata: {json.dumps(data)}\n\n"
            except asyncio.TimeoutError:
                # Keepalive heartbeat
                yield ": keepalive\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# --- Session Endpoints ------------------------------------------------------

@router.get("/api/session")
def get_session_status():
    """Check current session state (used on page load to restore state)."""
    if ctx_module.context is None or not ctx_module.context.session_mgr.has_active_session:
        return {"active": False, "user": None}
    session = ctx_module.context.session_mgr.current_session
    if session is None:
        return {"active": False, "user": None}
    user = session.user
    return {
        "active": True,
        "user": {
            "id": user.id,
            "name": user.display_name,
            "role": user.role.value,
        },
    }


@router.post("/api/session/end")
def end_session(user_session: UserSession = Depends(require_session)):
    """End the current session (End Session button)."""
    ctx_module.context.session_mgr.end_session()
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
    """Reset the inactivity timer (called on any UI interaction)."""
    # touch() already called by require_session dependency
    return {"success": True}


# --- Device Endpoints -------------------------------------------------------

@router.get("/api/devices")
def list_devices(
    db: Session = Depends(get_db),
    user_session: UserSession = Depends(require_session),
):
    """List all devices with borrower info for the kiosk UI."""
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
        })

    return result


@router.post("/api/devices/{device_id}/borrow")
def borrow_device(
    device_id: int,
    db: Session = Depends(get_db),
    user_session: UserSession = Depends(require_session),
):
    """Borrow a device."""
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
    """Return a device."""
    device = DeviceRepository.find_by_id(db, device_id)
    device_name = device.name if device else f"Device {device_id}"

    success = LockerService.return_device(db, user_session, device_id)

    if success:
        return {"success": True, "message": f"{device_name} returned."}
    return {"success": False, "message": f"Could not return {device_name}."}


# --- Registration Endpoints -------------------------------------------------

class RegisterRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


@router.post("/api/register")
def start_registration(body: RegisterRequest):
    """Begin self-registration: store name, await NFC card tap."""
    if ctx_module.context is None:
        raise HTTPException(status_code=503, detail="System not ready.")

    if ctx_module.context.session_mgr.has_active_session:
        raise HTTPException(status_code=409, detail="A session is active. End it first.")

    ctx_module.context.pending_registration = PendingRegistration(
        display_name=body.name.strip(),
    )
    logger.info("Registration started for '%s'. Awaiting card tap.", body.name.strip())
    return {"success": True, "message": "Tap your NFC card to complete registration."}


@router.post("/api/register/cancel")
def cancel_registration():
    """Cancel a pending self-registration."""
    if ctx_module.context is None:
        raise HTTPException(status_code=503, detail="System not ready.")

    was_pending = ctx_module.context.pending_registration is not None
    ctx_module.context.pending_registration = None
    return {"success": True, "cancelled": was_pending}


# --- Admin Endpoints --------------------------------------------------------

@router.post("/api/admin/sync-source")
def trigger_source_sync(
    user_session: UserSession = Depends(require_session),
):
    """Manually trigger source Excel import (admin only)."""
    if user_session.user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin access required.")

    from config.settings import SOURCE_EXCEL_PATH
    if not SOURCE_EXCEL_PATH:
        raise HTTPException(status_code=400, detail="Source Excel path not configured.")

    from smart_locker.database.engine import get_engine
    from smart_locker.sync.source_import import import_from_source_excel

    result = import_from_source_excel(get_engine(), SOURCE_EXCEL_PATH)
    return {
        "success": True,
        "imported": result.imported,
        "updated": result.updated,
        "unchanged": result.unchanged,
        "errors": result.errors,
    }
