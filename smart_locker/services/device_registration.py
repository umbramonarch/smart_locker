"""
File: device_registration.py
Description: Admin Register Device — promote a catalog row into a cabinet
             unit by assigning a free locker slot. The SQLite catalog is the
             lookup; the NFC bind is armed by the API afterward.
Project: smart_locker/services
Notes: Unknown id or a taken slot leaves the database unchanged. The row
       keeps its catalog fields; registering only assigns the slot.
       After the API commits SQLite it marks the mirror dirty.
"""

from __future__ import annotations

import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config.settings import MAX_LOCKER_SLOT, asset_label
from smart_locker.database.models import Device
from smart_locker.database.repositories import DeviceRepository
from smart_locker.services.device_catalog import place_kind

logger = logging.getLogger(__name__)


class UnknownPm(Exception):
    """The id is not in the SQLite catalog."""


class SlotTaken(Exception):
    """Another locker device already occupies this slot."""


class AlreadyRegistered(Exception):
    """This id is already a locker device."""


class NotRegisterable(Exception):
    """The catalog row is not marked for the locker."""


class InvalidSlot(Exception):
    """Slot must be an integer >= 1."""


def _require_free_slot(session: Session, locker_slot: int, ignore_id: int | None = None) -> None:
    """Reject a non-positive or occupied slot.

    Args:
        session: Active database session.
        locker_slot: Requested cabinet number.
        ignore_id: Device id allowed to keep this slot (reassign to same slot).

    Raises:
        InvalidSlot: locker_slot is not in 1..MAX_LOCKER_SLOT.
        SlotTaken: another device occupies the slot.
    """
    if not isinstance(locker_slot, int) or locker_slot < 1 or locker_slot > MAX_LOCKER_SLOT:
        raise InvalidSlot(f"Slot must be between 1 and {MAX_LOCKER_SLOT}.")
    occupant = DeviceRepository.find_by_slot(session, locker_slot)
    if occupant is not None and occupant.id != ignore_id:
        raise SlotTaken(f"Slot {locker_slot} is already used by {occupant.pm_number}.")


def register_locker_device(
    session: Session,
    pm_number: str,
    locker_slot: int,
) -> Device:
    """Put one catalog device into a free locker slot.

    Args:
        session: Active database session.
        pm_number: Catalog id to promote.
        locker_slot: Physical cabinet slot (unique, >= 1).

    Returns:
        The same Device row, now a cabinet unit (AVAILABLE, no tag yet).

    Raises:
        UnknownPm: id not in the catalog.
        AlreadyRegistered: id already has a locker slot.
        NotRegisterable: id is not marked with the in-locker place word —
            only rows on the registerable list may be promoted.
        InvalidSlot: Slot is not >= 1.
        SlotTaken: Slot occupied.
    """
    pm = (pm_number or "").strip()
    if not pm:
        raise UnknownPm(f"{asset_label()} is empty.")

    device = DeviceRepository.find_by_pm(session, pm)
    if device is None:
        raise UnknownPm(f"{asset_label()} '{pm}' is not in the catalog.")
    if device.locker_slot is not None:
        raise AlreadyRegistered(f"{pm} is already in the locker.")
    if place_kind(device.location) != "locker":
        raise NotRegisterable(f"{pm} is not marked for the locker.")

    _require_free_slot(session, locker_slot)

    try:
        DeviceRepository.set_locker_slot(session, device, locker_slot)
    except IntegrityError as e:
        session.rollback()
        raise SlotTaken(f"Slot {locker_slot} is already used.") from e

    if device.model and not device.image_path:
        for sib in DeviceRepository.find_by_model(session, device.model):
            if sib.id != device.id and sib.image_path:
                device.image_path = sib.image_path
                session.flush()
                break

    logger.info(
        "Registered locker device %s (pm=%s, slot=%s).",
        device.name, device.pm_number, locker_slot,
    )
    return device


def set_locker_slot(session: Session, device: Device, locker_slot: int) -> Device:
    """Move an existing locker device to a different free slot.

    Args:
        session: Active database session.
        device: Locker device to move.
        locker_slot: New cabinet number.

    Returns:
        The same Device row.

    Raises:
        InvalidSlot: Slot is not >= 1.
        SlotTaken: Slot occupied by another device.
    """
    _require_free_slot(session, locker_slot, ignore_id=device.id)
    try:
        DeviceRepository.set_locker_slot(session, device, locker_slot)
        session.flush()
    except IntegrityError as e:
        session.rollback()
        raise SlotTaken(f"Slot {locker_slot} is already used.") from e
    logger.info("Moved %s (pm=%s) to slot %s.", device.name, device.pm_number, locker_slot)
    return device
