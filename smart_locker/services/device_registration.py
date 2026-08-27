"""
File: device_registration.py
Description: Admin Register Device — look up a PM in the company Excel catalog,
             assign a unique locker slot, and insert one SQLite row. Sync never
             creates locker devices; this module is the only insert path.
Project: smart_locker/services
Notes: Unknown PM or a missing/locked workbook leaves the database unchanged.
       New rows start AVAILABLE. NFC bind is armed by the API after insert.
       After insert, Aktueller Einsatzort is written as the in-locker token.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from smart_locker.database.models import Device, DeviceStatus
from smart_locker.database.repositories import DeviceRepository
from smart_locker.sync.source_import import CatalogReadError, lookup_catalog_by_pm

logger = logging.getLogger(__name__)


class CatalogUnavailable(Exception):
    """Source Excel is missing, locked, or unreadable (share down)."""


class UnknownPm(Exception):
    """The PM number is not in the company Excel catalog."""


class SlotTaken(Exception):
    """Another locker device already occupies this slot."""


class AlreadyRegistered(Exception):
    """This PM is already a locker device."""


class InvalidSlot(Exception):
    """Slot must be an integer >= 1."""


def _require_free_slot(session: Session, locker_slot: int, ignore_id: int | None = None) -> None:
    """Reject a non-positive or occupied slot.

    Args:
        session: Active database session.
        locker_slot: Requested cabinet number.
        ignore_id: Device id allowed to keep this slot (reassign to same slot).

    Raises:
        InvalidSlot: locker_slot is not >= 1.
        SlotTaken: another device occupies the slot.
    """
    if not isinstance(locker_slot, int) or locker_slot < 1:
        raise InvalidSlot("Slot must be 1 or higher.")
    occupant = DeviceRepository.find_by_slot(session, locker_slot)
    if occupant is not None and occupant.id != ignore_id:
        raise SlotTaken(f"Slot {locker_slot} is already used by {occupant.pm_number}.")


def register_locker_device(
    session: Session,
    source_path: str,
    pm_number: str,
    locker_slot: int,
) -> Device:
    """Insert one locker device from the Excel catalog into a free slot.

    Args:
        session: Active database session.
        source_path: Path to ``device-list.xlsx``.
        pm_number: Equipment number to look up.
        locker_slot: Physical cabinet slot (unique, >= 1).

    Returns:
        The new Device row (AVAILABLE, no tag yet).

    Raises:
        CatalogUnavailable: Workbook missing, locked, or unreadable.
        UnknownPm: PM not in the sheet.
        AlreadyRegistered: PM already in SQLite.
        InvalidSlot: Slot is not >= 1.
        SlotTaken: Slot occupied.
    """
    pm = (pm_number or "").strip()
    if not pm:
        raise UnknownPm("PM number is empty.")

    existing = DeviceRepository.find_by_pm(session, pm)
    if existing is not None:
        raise AlreadyRegistered(f"{pm} is already in the locker.")

    _require_free_slot(session, locker_slot)

    try:
        catalog = lookup_catalog_by_pm(source_path, pm)
    except CatalogReadError as e:
        raise CatalogUnavailable(str(e)) from e

    if catalog is None:
        raise UnknownPm(f"PM '{pm}' was not found in the source Excel.")

    serial = catalog.serial_number
    if serial and DeviceRepository.find_by_serial(session, serial) is not None:
        logger.warning("Serial %s already in locker — omitting it for %s.", serial, pm)
        serial = None

    device = DeviceRepository.create(
        session,
        name=catalog.name,
        device_type=catalog.device_type,
        pm_number=catalog.pm_number,
        serial_number=serial,
        locker_slot=locker_slot,
        manufacturer=catalog.manufacturer,
        model=catalog.model,
        calibration_due=catalog.calibration_due,
        status=DeviceStatus.AVAILABLE.value,
    )

    if catalog.model:
        for sib in DeviceRepository.find_by_model(session, catalog.model):
            if sib.id != device.id and sib.image_path:
                device.image_path = sib.image_path
                break

    from smart_locker.sync.einsatzort_writeback import write_einsatzort

    write_einsatzort(session, source_path)
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
    DeviceRepository.set_locker_slot(session, device, locker_slot)
    logger.info("Moved %s (pm=%s) to slot %s.", device.name, device.pm_number, locker_slot)
    return device
