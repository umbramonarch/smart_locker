"""
File: owner_edit.py
Description: Public dashboard holder change on Inventory. Writes the stored
             place on a catalog row that is not in the cabinet and marks the
             mirror dirty. Cabinet units are refused — holder follows the
             kiosk borrow/return.
Project: smart_locker/services
Notes: POST /api/dashboard/owner is public per the plan — changing the
       holder of a device that is not in the cabinet has no password.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from config.settings import in_locker_token
from smart_locker.database.repositories import (
    DeviceRepository,
    RegistrantRepository,
    UserRepository,
)
from smart_locker.services.device_catalog import LockerOwned, set_place

logger = logging.getLogger(__name__)


class OwnerEditError(Exception):
    """Base for owner-edit failures the API maps to HTTP errors."""


class UnknownPm(OwnerEditError):
    """The id is not a row in the catalog."""


class InvalidOwnerRequest(OwnerEditError):
    """id was empty."""


@dataclass(frozen=True)
class OwnerEditResult:
    """Outcome of one owner change."""

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


def set_owner(
    session: Session,
    pm_number: str,
    owner: str,
) -> OwnerEditResult:
    """Change the holder of one non-cabinet catalog row.

    Args:
        session: Active database session (caller commits).
        pm_number: Device id to change.
        owner: New holder/place text (registered user, registrant, or free text).

    Returns:
        OwnerEditResult (``locker`` is always False on success).

    Raises:
        InvalidOwnerRequest: Empty id.
        LockerOwned: The row is a cabinet unit.
        UnknownPm: id is not in the catalog.
    """
    pm = (pm_number or "").strip()
    if not pm:
        raise InvalidOwnerRequest("Device id is required.")
    name = (owner or "").strip()

    device = DeviceRepository.find_by_pm(session, pm)
    if device is None:
        raise UnknownPm(f"{pm} is not in the catalog.")

    stored = set_place(session, device, name)
    return OwnerEditResult(pm_number=device.pm_number, owner=stored, locker=False)
