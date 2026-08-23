"""
File: tap_router.py
Description: Classify an NFC UID as a work card, device tag, or unknown, and
             apply idle login / logout / auto-intent borrow-return. Shared by
             the web NFC bridge and the CLI event loop. Never includes the
             raw UID in results or log lines.
Project: smart_locker/auth
Notes: Never includes the raw UID in results or log lines.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from smart_locker.auth.session_manager import SessionManager
from smart_locker.database.models import Device, DeviceStatus, User
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


def handle_insert(
    db_session: Session,
    card_uid_hex: str,
    hmac_key: bytes,
    session_mgr: SessionManager,
    *,
    admin_overlay_open: bool = False,
    reader_name: str = "",
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
    """
    kind, user, device = classify_uid(db_session, card_uid_hex, hmac_key)
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
    return _handle_idle(session_mgr, kind, user, device, reader_name=reader)


def _handle_idle(
    session_mgr: SessionManager,
    kind: TapKind,
    user: User | None,
    device: Device | None,
    *,
    reader_name: str,
) -> TapResult:
    """Idle (no session): work card logs in; device tag does not."""
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
        session_mgr.end_session()
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
    """Borrow or return from device status. Session stays open. Always touch()."""
    user_session = session_mgr.current_session
    if user_session is None:
        return TapResult(event=None)

    user_session.touch()
    name = device.name

    if device.status == DeviceStatus.AVAILABLE:
        success = LockerService.borrow_device(db_session, user_session, device.id)
        message = f"{name} borrowed." if success else f"Could not borrow {name}."
        return TapResult(
            event="device_action",
            payload={
                "success": success,
                "action": "borrow",
                "message": message,
                "device_id": device.id,
                "device_name": name,
            },
            cli_message=message,
        )

    if device.status == DeviceStatus.BORROWED:
        success = LockerService.return_device(db_session, user_session, device.id)
        message = f"{name} returned." if success else f"Could not return {name}."
        return TapResult(
            event="device_action",
            payload={
                "success": success,
                "action": "return",
                "message": message,
                "device_id": device.id,
                "device_name": name,
            },
            cli_message=message,
        )

    message = f"Could not borrow {name}."
    return TapResult(
        event="device_action",
        payload={
            "success": False,
            "action": "borrow",
            "message": message,
            "device_id": device.id,
            "device_name": name,
        },
        cli_message=message,
    )
