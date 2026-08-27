"""
File: owner_edit.py
Description: Public dashboard owner change. Writes the Location cell in the
             catalog Excel. If the PM is already a locker row, also updates
             SQLite borrow state and logs a transaction. Never inserts a
             locker device.
Project: smart_locker/services
Notes: Non-locker PMs are Excel-only. Locker PMs map a registered user to
       borrowed, and the in-locker token to available. Confirm belongs in
       the dashboard UI; this module is the write.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from config.settings import in_locker_token
from smart_locker.database.models import Device, DeviceStatus
from smart_locker.database.repositories import (
    DeviceRepository,
    RegistrantRepository,
    TransactionRepository,
    UserRepository,
)
from smart_locker.sync.location_writeback import write_location_value

logger = logging.getLogger(__name__)

_DASHBOARD_NOTE = "owner set from dashboard"


class OwnerEditError(Exception):
    """Base for owner-edit failures the API maps to HTTP errors."""


class CatalogUnavailable(OwnerEditError):
    """Source Excel is missing, locked, or unreadable."""


class UnknownPm(OwnerEditError):
    """The PM is not a row in the catalog Excel."""


class InvalidOwnerRequest(OwnerEditError):
    """PM number was empty."""


@dataclass(frozen=True)
class OwnerEditResult:
    """Outcome of one public owner change."""

    pm_number: str
    owner: str
    locker: bool


def owner_choices(session: Session) -> list[str]:
    """Names for the dashboard owner dropdown.

    In-locker token, then registered users, then registrant names.
    Case-insensitive duplicates keep the first spelling.

    Args:
        session: Active database session.

    Returns:
        Display names suitable for a ``<datalist>``.
    """
    names: list[str] = []
    seen: set[str] = set()

    def _add(raw: str | None) -> None:
        text = (raw or "").strip()
        if not text:
            return
        key = text.lower()
        if key in seen:
            return
        seen.add(key)
        names.append(text)

    _add(in_locker_token())
    for user in UserRepository.list_all(session):
        _add(user.display_name)
    for registrant in RegistrantRepository.get_all(session):
        _add(registrant.display_name)
    return names


def _excel_owner_value(session: Session, device: Device | None, owner: str) -> str:
    """Canonical Location text to write for this PM.

    Args:
        session: Active database session.
        device: Locker row if this PM is already registered, else None.
        owner: Stripped name from the dashboard.

    Returns:
        Location cell value (in-locker token, user display name, or free text).
    """
    token = in_locker_token()
    if device is not None and (not owner or owner.lower() == token.lower()):
        return token
    if device is not None:
        user = UserRepository.find_by_display_name(session, owner)
        if user is not None:
            return user.display_name
    return owner


def _apply_locker_owner(session: Session, device: Device, owner: str) -> None:
    """Update SQLite borrow state and log when the name maps to a user or locker.

    Args:
        session: Active database session (caller commits).
        device: Existing locker row.
        owner: Canonical Location text already written to Excel.

    Returns:
        None.
    """
    token = in_locker_token()
    if not owner or owner.lower() == token.lower():
        if (
            device.status == DeviceStatus.BORROWED
            and device.current_borrower_id is not None
        ):
            original = device.current_borrower_id
            DeviceRepository.return_device(session, device)
            TransactionRepository.log_return(
                session,
                user_id=original,
                device_id=device.id,
                notes=_DASHBOARD_NOTE,
            )
            logger.info(
                "Dashboard owner: returned %s (device=%d) to locker.",
                device.pm_number,
                device.id,
            )
        return

    user = UserRepository.find_by_display_name(session, owner)
    if user is None:
        return

    if (
        device.status == DeviceStatus.BORROWED
        and device.current_borrower_id == user.id
    ):
        return

    if (
        device.status == DeviceStatus.BORROWED
        and device.current_borrower_id is not None
    ):
        original = device.current_borrower_id
        DeviceRepository.return_device(session, device)
        TransactionRepository.log_return(
            session,
            user_id=original,
            device_id=device.id,
            notes=_DASHBOARD_NOTE,
        )

    DeviceRepository.borrow(session, device, user.id)
    TransactionRepository.log_borrow(
        session, user.id, device.id, notes=_DASHBOARD_NOTE
    )
    logger.info(
        "Dashboard owner: %s now holds %s (device=%d).",
        user.display_name,
        device.pm_number,
        device.id,
    )


def set_owner(
    session: Session,
    source_path: str | Path,
    pm_number: str,
    owner: str,
) -> OwnerEditResult:
    """Change Location for one PM. SQLite only if that PM is already a locker row.

    Args:
        session: Active database session (caller commits).
        source_path: Path to ``device-list.xlsx``.
        pm_number: Equipment number to match in Excel.
        owner: New owner / location text (registered user, registrant, or free text).

    Returns:
        OwnerEditResult with whether a locker row was updated.

    Raises:
        InvalidOwnerRequest: Empty PM number.
        CatalogUnavailable: Workbook missing, locked, or unreadable.
        UnknownPm: PM is not in the catalog sheet.
    """
    pm = (pm_number or "").strip()
    if not pm:
        raise InvalidOwnerRequest("PM number is required.")
    name = (owner or "").strip()

    path = Path(source_path) if source_path else None
    if path is None or not str(source_path).strip():
        raise CatalogUnavailable("Catalog Excel is not configured.")

    device = DeviceRepository.find_by_pm(session, pm)
    excel_value = _excel_owner_value(session, device, name)

    written = write_location_value(path, pm, excel_value)
    if written.error:
        raise CatalogUnavailable(
            "Catalog Excel is not available."
            if written.error in ("missing", "unconfigured", "unavailable")
            else f"Catalog Excel could not be written ({written.error})."
        )
    if written.skipped and written.written == 0 and written.unchanged == 0:
        raise UnknownPm(f"{pm} is not in the catalog Excel.")

    if device is not None:
        _apply_locker_owner(session, device, excel_value)

    logger.info(
        "Dashboard owner: %s → %s (locker=%s).",
        pm,
        excel_value,
        device is not None,
    )
    return OwnerEditResult(
        pm_number=pm, owner=excel_value, locker=device is not None
    )
