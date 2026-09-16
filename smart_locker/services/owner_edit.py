"""
File: owner_edit.py
Description: Public dashboard owner change on Inventory. Writes
             the Location cell in the catalog Excel for PMs that are not
             locker devices. Never inserts a locker row. Locker PMs are
             refused — borrow and return stay on the kiosk.
Project: smart_locker/services
Notes: POST /api/dashboard/owner is public (no secret). Inventory/Locker
       GETs stay public. This module is the Excel write. Registered kiosk
       users are not listed — the dropdown only offers names already
       visible in Inventory (Excel Location), skipping names that match
       a deactivated user.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from config.settings import in_locker_token
from smart_locker.database.repositories import (
    DeviceRepository,
    RegistrantRepository,
    UserRepository,
)
from smart_locker.sync.location_writeback import write_location_value

logger = logging.getLogger(__name__)


class OwnerEditError(Exception):
    """Base for owner-edit failures the API maps to HTTP errors."""


class CatalogUnavailable(OwnerEditError):
    """Source Excel is missing, locked, or unreadable."""


class UnknownPm(OwnerEditError):
    """The PM is not a row in the catalog Excel."""


class InvalidOwnerRequest(OwnerEditError):
    """PM number was empty."""


class LockerOwned(OwnerEditError):
    """This PM is a locker device; owner is set at the kiosk."""


@dataclass(frozen=True)
class OwnerEditResult:
    """Outcome of one public dashboard owner change."""

    pm_number: str
    owner: str
    locker: bool


def owner_choices(session: Session) -> list[str]:
    """Names for the dashboard owner dropdown.

    In-locker token, then registrant names. Case-insensitive
    duplicates keep the first spelling. Registered kiosk users are
    not listed — the dropdown only offers names already visible in
    Inventory (Excel Location). Registrant names matching a
    deactivated user are skipped.

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
    inactive_lower = UserRepository.display_names_lower(session, False)
    for registrant in RegistrantRepository.get_all(session):
        if registrant.display_name.strip().lower() in inactive_lower:
            continue
        _add(registrant.display_name)
    return names


def set_owner(
    session: Session,
    source_path: str | Path,
    pm_number: str,
    owner: str,
) -> OwnerEditResult:
    """Change Location for one non-locker PM in Excel only.

    Args:
        session: Active database session (caller commits).
        source_path: Path to ``device-list.xlsx``.
        pm_number: Equipment number to match in Excel.
        owner: New owner / location text (registered user, registrant, or free text).

    Returns:
        OwnerEditResult (``locker`` is always False on success).

    Raises:
        InvalidOwnerRequest: Empty PM number.
        LockerOwned: PM is already a locker device.
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

    if DeviceRepository.find_by_pm(session, pm) is not None:
        raise LockerOwned(
            "This device is in the locker. Change owner at the kiosk."
        )

    written = write_location_value(path, pm, name)
    if written.error:
        raise CatalogUnavailable(
            "Catalog Excel is not available."
            if written.error in ("missing", "unconfigured", "unavailable")
            else f"Catalog Excel could not be written ({written.error})."
        )
    if written.skipped and written.written == 0 and written.unchanged == 0:
        raise UnknownPm(f"{pm} is not in the catalog Excel.")

    logger.info("Dashboard owner: %s → %s.", pm, name)
    return OwnerEditResult(pm_number=pm, owner=name, locker=False)
