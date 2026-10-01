"""
File: device_registration.py
Description: Admin Register Device — look up a PM in the catalog Excel,
             assign a unique locker slot, and insert one SQLite row. Sync never
             creates locker devices; this module is the only insert path.
Project: smart_locker/services
Notes: Unknown PM or a missing/locked workbook leaves the database unchanged.
       New rows start AVAILABLE. NFC bind is armed by the API after insert.
       After insert, the API commits SQLite then schedules Location write-back.
"""

from __future__ import annotations

import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config.settings import MAX_LOCKER_SLOT, asset_label
from smart_locker.database.models import Device, DeviceStatus
from smart_locker.database.repositories import DeviceRepository
from smart_locker.sync.source_import import CatalogReadError, lookup_catalog_by_pm

logger = logging.getLogger(__name__)


class CatalogUnavailable(Exception):
    """Source Excel is missing, locked, or unreadable (share down)."""


class UnknownPm(Exception):
    """The PM number is not in the catalog spreadsheet."""


class AlreadyRegistered(Exception):
    """This PM is already a locker device."""


class InvalidSlot(Exception):
    """Slot must be an integer >= 1."""


def _require_valid_slot(locker_slot: int) -> None:
    """Reject a non-positive or out-of-range slot.

    Slots are shared — several devices may sit in one cabinet slot — so this
    only validates the 1..MAX_LOCKER_SLOT range.

    Args:
        locker_slot: Requested cabinet number.

    Raises:
        InvalidSlot: locker_slot is not in 1..MAX_LOCKER_SLOT.
    """
    if not isinstance(locker_slot, int) or locker_slot < 1 or locker_slot > MAX_LOCKER_SLOT:
        raise InvalidSlot(f"Slot must be between 1 and {MAX_LOCKER_SLOT}.")


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
        locker_slot: Physical cabinet slot (>= 1; slots may be shared).

    Returns:
        The new Device row (AVAILABLE, no tag yet).

    Raises:
        CatalogUnavailable: Workbook missing, locked, or unreadable.
        UnknownPm: PM not in the sheet.
        AlreadyRegistered: PM already in SQLite.
        InvalidSlot: Slot is not in 1..MAX_LOCKER_SLOT.
    """
    pm = (pm_number or "").strip()
    if not pm:
        raise UnknownPm(f"{asset_label()} is empty.")

    existing = DeviceRepository.find_by_pm(session, pm)
    if existing is not None:
        raise AlreadyRegistered(f"{pm} is already in the locker.")

    _require_valid_slot(locker_slot)

    try:
        catalog = lookup_catalog_by_pm(source_path, pm)
    except CatalogReadError as e:
        raise CatalogUnavailable(str(e)) from e

    if catalog is None:
        raise UnknownPm(f"{asset_label()} '{pm}' was not found in the source Excel.")

    serial = catalog.serial_number
    if serial and DeviceRepository.find_by_serial(session, serial) is not None:
        logger.warning("Serial %s already in locker — omitting it for %s.", serial, pm)
        serial = None

    try:
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
    except IntegrityError as e:
        session.rollback()
        # The only remaining unique keys on devices are pm_number, serial,
        # and tag_hmac — a duplicate PM race reports as already registered,
        # anything else surfaces as-is rather than masquerading as a slot
        # conflict (slots are shared and carry no constraint).
        if DeviceRepository.find_by_pm(session, pm) is not None:
            raise AlreadyRegistered(f"{pm} is already in the locker.") from e
        raise

    if catalog.model:
        for sib in DeviceRepository.find_by_model(session, catalog.model):
            if sib.id != device.id and sib.image_path:
                device.image_path = sib.image_path
                break

    logger.info(
        "Registered locker device %s (pm=%s, slot=%s).",
        device.name, device.pm_number, locker_slot,
    )
    return device


def set_locker_slot(session: Session, device: Device, locker_slot: int) -> Device:
    """Move an existing locker device to a different (possibly shared) slot.

    Args:
        session: Active database session.
        device: Locker device to move.
        locker_slot: New cabinet number.

    Returns:
        The same Device row.

    Raises:
        InvalidSlot: Slot is not in 1..MAX_LOCKER_SLOT.
    """
    _require_valid_slot(locker_slot)
    DeviceRepository.set_locker_slot(session, device, locker_slot)
    session.flush()
    logger.info("Moved %s (pm=%s) to slot %s.", device.name, device.pm_number, locker_slot)
    return device
