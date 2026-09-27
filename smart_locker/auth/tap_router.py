"""
File: tap_router.py
Description: Classify an NFC UID as a work card, device tag, or unknown, and
             apply idle login / logout / auto-intent borrow-return. Idle
             borrowed tags return without a work card. Shared by the web NFC
             bridge and the CLI event loop. Never includes the raw UID in
             results or log lines.
Project: smart_locker/auth
Notes: Never includes the raw UID in results or log lines.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from smart_locker.auth.session_manager import SessionManager
from smart_locker.database.models import Device, DeviceStatus, User, UserRole
from smart_locker.database.repositories import DeviceRepository, UserRepository
from smart_locker.security.hashing import compute_uid_hmac
from smart_locker.services.locker_service import LockerService

logger = logging.getLogger(__name__)


class TapKind(enum.Enum):
    """Classification of a UID after HMAC lookup in users, then devices."""

    WORK_CARD = "work_card"
    DEVICE_TAG = "device_tag"
    UNKNOWN = "unknown"


@dataclass
class TapResult:
    """NFC insert outcome for SSE and CLI. Never includes the raw UID.

    ``event`` is the SSE event name, or None when the insert should be
    ignored (no SSE, no CLI print).
    """

    event: str | None
    payload: dict = field(default_factory=dict)
    cli_message: str = ""

    def to_sse(self) -> dict | None:
        """Return the dict pushed to the SSE queue, or None to skip."""
        if self.event is None:
            return None
        data = {"event": self.event}
        data.update(self.payload)
        return data


@dataclass
class TapDispatchOutcome:
    """Complete application outcome for one NFC insert.

    The caller supplies the pending-window snapshot captured under its state
    lock.  This keeps registration, armed bind, and ordinary tap policy in one
    place while leaving UI state and SSE delivery to the API layer.
    """

    result: TapResult
    clear_pending_registration: bool = False
    clear_pending_tag_bind: bool = False
    end_leftover_session_silently: bool = False


def classify_uid(
    db_session: Session,
    card_uid_hex: str,
    hmac_key: bytes,
) -> tuple[TapKind, User | None, Device | None]:
    """Classify a UID by HMAC lookup: users first, then devices.

    Args:
        db_session: Active database session.
        card_uid_hex: Raw UID hex from the reader.
        hmac_key: HMAC-SHA256 key (same as work-card lookup).

    Returns:
        Tuple of (kind, user-or-None, device-or-None). Inactive users still
        classify as ``WORK_CARD``.
    """
    uid_hmac = compute_uid_hmac(card_uid_hex, hmac_key)
    user = UserRepository.find_by_uid_hmac(db_session, uid_hmac)
    if user is not None:
        return TapKind.WORK_CARD, user, None
    device = DeviceRepository.find_by_tag_hmac(db_session, uid_hmac)
    if device is not None:
        return TapKind.DEVICE_TAG, None, device
    return TapKind.UNKNOWN, None, None


def handle_registration_tap(
    db_session: Session,
    card_uid_hex: str,
    *,
    display_name: str,
    hmac_key: bytes,
    enc_key: bytes | None,
    role: str = "user",
) -> TapResult:
    """Enroll ``display_name`` from a fresh card tap.

    Classification, duplicate rejection, and enrollment deliberately share the
    caller's session. The AppContext owns the pending-window timeout and
    session-clearing policy; this function owns the database tap policy.
    ``role`` is "admin" only when a first-boot Setup window armed the tap.
    """
    if not display_name.strip():
        logger.warning("Registration refused: blank display name.")
        return TapResult(
            event="registration_failed",
            payload={"reason": "A name is required. Start registration again."},
        )

    kind, user, _ = classify_uid(db_session, card_uid_hex, hmac_key)
    if kind == TapKind.DEVICE_TAG:
        logger.warning("Registration failed: UID is already a device tag.")
        return TapResult(
            event="registration_failed",
            payload={"reason": "This tag is already bound to a device."},
        )

    if kind == TapKind.WORK_CARD and user is not None and user.is_active:
        logger.warning(
            "Registration failed: card already enrolled to %s.",
            user.display_name,
        )
        return TapResult(
            event="registration_failed",
            payload={"reason": "This card is already registered."},
        )

    from smart_locker.services.user_service import UserService

    user = UserService(enc_key=enc_key, hmac_key=hmac_key).enroll_user(
        db_session,
        display_name=display_name,
        card_uid_hex=card_uid_hex,
        role=role,
    )
    logger.info(
        "Enrolled %s via card tap: %s (id=%d)",
        user.role.value,
        user.display_name,
        user.id,
    )
    return TapResult(
        event="registration_success",
        payload={
            "user": {
                "id": user.id,
                "name": user.display_name,
                "role": user.role.value,
            },
        },
    )


def bind_uid_to_device(
    db_session: Session,
    device: Device,
    card_uid_hex: str,
    hmac_key: bytes,
) -> None:
    """Bind a sticker UID to ``device`` via HMAC.

    Re-bind on the same row replaces the HMAC. Rejects work-card UIDs and
    stickers already bound to a different device.

    Args:
        db_session: Active database session.
        device: Device row to bind.
        card_uid_hex: Raw sticker UID hex.
        hmac_key: HMAC-SHA256 key.

    Raises:
        ValueError: If the UID is a work card or already another device's tag.
    """
    tag_hmac = compute_uid_hmac(card_uid_hex, hmac_key)
    if UserRepository.find_by_uid_hmac(db_session, tag_hmac) is not None:
        raise ValueError("This card is a work card.")
    existing = DeviceRepository.find_by_tag_hmac(db_session, tag_hmac)
    if existing is not None and existing.id != device.id:
        raise ValueError("This tag is already bound to another device.")
    DeviceRepository.bind_tag(db_session, device, tag_hmac)


def handle_tag_bind_tap(
    db_session: Session,
    card_uid_hex: str,
    *,
    device_id: int,
    hmac_key: bytes,
    session_mgr: SessionManager,
    admin_overlay_open: bool = False,
    reader_name: str = "",
) -> tuple[TapResult, bool]:
    """Apply an armed tag bind, preserving normal work-card and return taps.

    Returns the tap result and whether the pending bind should be cleared.
    Classification, bind lookup, and any fall-through tap handling share the
    caller's session; no ORM row escapes this function.
    """
    kind, user, device = classify_uid(db_session, card_uid_hex, hmac_key)
    if kind == TapKind.WORK_CARD and user is not None:
        if session_mgr.has_active_session:
            return TapResult(event=None), False
        return (
            handle_insert(
                db_session, card_uid_hex, hmac_key, session_mgr,
                admin_overlay_open=admin_overlay_open, reader_name=reader_name,
                classified_uid=(kind, user, device),
            ),
            True,
        )

    if (
        kind == TapKind.DEVICE_TAG and device is not None
        and device.status == DeviceStatus.BORROWED
    ):
        return (
            handle_insert(
                db_session, card_uid_hex, hmac_key, session_mgr,
                admin_overlay_open=admin_overlay_open, reader_name=reader_name,
                classified_uid=(kind, user, device),
            ),
            False,
        )

    target = DeviceRepository.find_by_id(db_session, device_id)
    if target is None:
        return TapResult(
            event="tag_bind_failed", payload={"reason": "Device not found."}
        ), True
    try:
        bind_uid_to_device(db_session, target, card_uid_hex, hmac_key)
    except ValueError as exc:
        return TapResult(event="tag_bind_failed", payload={"reason": str(exc)}), True
    return TapResult(
        event="tag_bind_success",
        payload={
            "device_id": target.id,
            "device_name": target.name,
            "pm_number": target.pm_number,
        },
    ), True


def handle_insert(
    db_session: Session,
    card_uid_hex: str,
    hmac_key: bytes,
    session_mgr: SessionManager,
    *,
    admin_overlay_open: bool = False,
    reader_name: str = "",
    classified_uid: tuple[TapKind, User | None, Device | None] | None = None,
) -> TapResult:
    """Apply auto-intent / logout / idle-login for one NFC insert.

    Pending intercepts (registration, tag bind) must run before this.

    Args:
        db_session: Active database session.
        card_uid_hex: Raw UID hex from the reader.
        hmac_key: HMAC-SHA256 key.
        session_mgr: Kiosk session manager.
        admin_overlay_open: When True, bound device tags do not borrow/return.
        reader_name: Reader identifier for logs (never the UID).

    Returns:
        TapResult with SSE event name/payload and a CLI message. Never the UID.
        Idle borrowed tags return without a session. Available tags at idle
        do not borrow.
    """
    kind, user, device = (
        classified_uid
        if classified_uid is not None
        else classify_uid(db_session, card_uid_hex, hmac_key)
    )
    reader = reader_name or "reader"

    if session_mgr.has_active_session:
        return _handle_logged_in(
            db_session,
            session_mgr,
            kind,
            user,
            device,
            admin_overlay_open=admin_overlay_open,
            reader_name=reader,
        )
    return _handle_idle(
        db_session, session_mgr, kind, user, device, reader_name=reader
    )


def dispatch_insert(
    db_session: Session,
    card_uid_hex: str,
    hmac_key: bytes,
    enc_key: bytes | None,
    session_mgr: SessionManager,
    *,
    registration_display_name: str | None = None,
    registration_role: str = "user",
    registration_expired: bool = False,
    tag_bind_device_id: int | None = None,
    admin_overlay_open: bool = False,
    reader_name: str = "",
) -> TapDispatchOutcome:
    """Apply the pending-window intercept or ordinary policy for one insert.

    All UID classification and database work runs through the supplied
    ``db_session``.  Pending values are scalar snapshots, never ORM rows.
    Expired registration ends an overlay session before normal classification
    so its tap can log in rather than log out, matching registration completion.
    ``enc_key`` is required only when ``registration_display_name`` is set —
    callers resolve it lazily so a missing ENC key cannot break plain taps.
    Handler failures roll the session back before returning a failure result,
    so the caller's auto-commit is a no-op instead of re-raising.
    """
    if registration_expired:
        session_mgr.end_session()
        result = handle_insert(
            db_session,
            card_uid_hex,
            hmac_key,
            session_mgr,
            admin_overlay_open=admin_overlay_open,
            reader_name=reader_name,
        )
        return TapDispatchOutcome(
            result,
            clear_pending_registration=True,
            # A work-card login during an expired window still consumes an
            # armed bind, matching the plain-path behavior below.
            clear_pending_tag_bind=result.event == "auth_success",
        )

    if registration_display_name is not None:
        try:
            result = handle_registration_tap(
                db_session,
                card_uid_hex,
                display_name=registration_display_name,
                hmac_key=hmac_key,
                enc_key=enc_key,
                role=registration_role,
            )
        except Exception:
            logger.exception(
                "Registration failed for '%s'.", registration_display_name
            )
            # A failed flush leaves the session dirty; without rollback the
            # caller's auto-commit re-raises and the failure SSE never ships.
            db_session.rollback()
            result = TapResult(
                event="registration_failed",
                payload={"reason": "Registration failed. Please try again."},
            )
        return TapDispatchOutcome(
            result,
            clear_pending_registration=True,
            end_leftover_session_silently=True,
        )

    if tag_bind_device_id is not None:
        # An armed bind has always kept an active kiosk session alive while it
        # waits for its sticker, including a work-card tap that is ignored.
        session_mgr.touch()
        try:
            result, clear_bind = handle_tag_bind_tap(
                db_session,
                card_uid_hex,
                device_id=tag_bind_device_id,
                hmac_key=hmac_key,
                session_mgr=session_mgr,
                admin_overlay_open=admin_overlay_open,
                reader_name=reader_name,
            )
        except Exception:
            logger.exception(
                "Device tag bind failed for device_id=%d.", tag_bind_device_id
            )
            db_session.rollback()
            result = TapResult(
                event="tag_bind_failed",
                payload={"reason": "Bind failed. Please try again."},
            )
            clear_bind = True
        return TapDispatchOutcome(result, clear_pending_tag_bind=clear_bind)

    result = handle_insert(
        db_session,
        card_uid_hex,
        hmac_key,
        session_mgr,
        admin_overlay_open=admin_overlay_open,
        reader_name=reader_name,
    )
    return TapDispatchOutcome(
        result,
        clear_pending_tag_bind=result.event == "auth_success",
    )


def _device_action(device: Device, *, success: bool, action: str, message: str) -> TapResult:
    """SSE ``device_action`` payload. Includes ``locker_slot`` for the slot overlay."""
    return TapResult(
        event="device_action",
        payload={
            "success": success,
            "action": action,
            "message": message,
            "device_id": device.id,
            "device_name": device.name,
            "locker_slot": device.locker_slot,
        },
        cli_message=message,
    )


def _handle_idle(
    db_session: Session,
    session_mgr: SessionManager,
    kind: TapKind,
    user: User | None,
    device: Device | None,
    *,
    reader_name: str,
) -> TapResult:
    """Idle: work card logs in; borrowed tag returns; available tag does not."""
    if kind == TapKind.WORK_CARD and user is not None:
        if not user.is_active:
            logger.warning(
                "Inactive user attempted auth: %s (id=%d)",
                user.display_name,
                user.id,
            )
            return TapResult(
                event="auth_failed",
                cli_message="Unknown card. Please contact an administrator to enroll.",
            )
        session_mgr.start_session(user)
        return TapResult(
            event="auth_success",
            payload={
                "user": {
                    "id": user.id,
                    "name": user.display_name,
                    "role": user.role.value,
                },
            },
            cli_message=f"Welcome, {user.display_name}!",
        )

    if kind == TapKind.DEVICE_TAG and device is not None:
        logger.info("Device tag on %s", reader_name)
        if device.status == DeviceStatus.BORROWED:
            success = LockerService.return_unattended(db_session, device.id)
            name = device.name
            message = f"{name} returned." if success else f"Could not return {name}."
            return _device_action(
                device, success=success, action="return", message=message
            )
        return TapResult(
            event="device_tag_idle",
            payload={"message": "Tap your work card first."},
            cli_message="Tap your work card first.",
        )

    return TapResult(
        event="auth_failed",
        cli_message="Unknown card. Please contact an administrator to enroll.",
    )


def _handle_logged_in(
    db_session: Session,
    session_mgr: SessionManager,
    kind: TapKind,
    user: User | None,
    device: Device | None,
    *,
    admin_overlay_open: bool,
    reader_name: str,
) -> TapResult:
    """Active session: work card logs out; device tag auto-intents."""
    if kind == TapKind.WORK_CARD and user is not None:
        active = session_mgr.current_session
        name = active.user.display_name if active is not None else "user"
        return TapResult(
            event="session_ended",
            payload={"reason": "card_tap"},
            cli_message=f"Goodbye, {name}!\nTap your work card to begin.",
        )

    if kind == TapKind.UNKNOWN:
        active = session_mgr.current_session
        if active is not None:
            active.touch()
        return TapResult(
            event="unknown_tag",
            payload={"message": "Unknown tag."},
            cli_message="Unknown tag.",
        )

    if kind == TapKind.DEVICE_TAG and device is not None:
        logger.info("Device tag on %s", reader_name)
        if admin_overlay_open:
            active = session_mgr.current_session
            if active is not None:
                active.touch()
            # Keep device_tag_idle (plan lists no overlay event). Idle copy
            # ("tap your work card") would log this session out.
            message = "Close ADMIN MODE or use Borrow to check out a device."
            return TapResult(
                event="device_tag_idle",
                payload={"message": message},
                cli_message=message,
            )
        return _auto_intent(db_session, session_mgr, device)

    return TapResult(event=None)


def _auto_intent(
    db_session: Session,
    session_mgr: SessionManager,
    device: Device,
) -> TapResult:
    """Borrow, return, or request handover from device status. Session stays open. Always touch()."""
    user_session = session_mgr.current_session
    if user_session is None:
        return TapResult(event=None)

    user_session.touch()
    name = device.name

    if device.status == DeviceStatus.AVAILABLE:
        outcome = LockerService.borrow_device(db_session, user_session, device.id)
        if outcome:
            message = f"{name} borrowed."
        else:
            message = f"Could not borrow {name}"
            if outcome.reason:
                message += f": {outcome.reason}"
            message += "."
        return _device_action(
            device, success=outcome.success, action="borrow" if outcome else "refused", message=message
        )

    if device.status == DeviceStatus.BORROWED:
        user = user_session.user
        if device.current_borrower_id == user.id or user.role == UserRole.ADMIN:
            success = LockerService.return_device(db_session, user_session, device.id)
            message = f"{name} returned." if success else f"Could not return {name}."
            return _device_action(
                device, success=success, action="return", message=message
            )

        # Another user holds the device; offer a handover instead of failing —
        # but only when the transfer could actually succeed (the service owns
        # the refusal ladder: calibration block, borrow limit, …).
        current_holder = device.current_borrower
        current_holder_name = current_holder.display_name if current_holder else "someone"
        check = LockerService.check_transfer(db_session, user_session, device)
        if not check:
            message = f"Could not transfer {name}"
            if check.reason:
                message += f": {check.reason}"
            message += "."
            return _device_action(
                device, success=False, action="refused", message=message
            )
        return TapResult(
            event="handover_requested",
            payload={
                "device_id": device.id,
                "device_name": device.name,
                "current_holder_id": device.current_borrower_id,
                "current_holder_name": current_holder_name,
                "user_id": user.id,
                "user_name": user.display_name,
            },
            cli_message=f"{name} is held by {current_holder_name}. Transfer to {user.display_name}?",
        )

    # Maintenance and any later block state: the service is the authority on
    # why a unit cannot be borrowed — ask it and report its reason.
    outcome = LockerService.borrow_device(db_session, user_session, device.id)
    message = f"Could not borrow {name}"
    if outcome.reason:
        message += f": {outcome.reason}"
    message += "."
    return _device_action(device, success=False, action="refused", message=message)
