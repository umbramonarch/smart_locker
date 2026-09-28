"""
File: device_catalog.py
Description: The SQLite catalog — place-word matching, display serialization,
             and the dashboard editor operations (add, edit, remove, place).
             The devices table holds every catalog device; a non-NULL
             locker_slot is what makes a row a cabinet unit.
Project: smart_locker/services
Notes: Client-specific fields are selected at the API boundary; tag digests
       never leave SQLite. Location for a registered unit is derived from
       status/borrower — the stored column belongs to non-cabinet rows.
"""

import logging
from datetime import date

from sqlalchemy.exc import IntegrityError, InvalidRequestError
from sqlalchemy.orm import Session

from config.settings import in_locker_token, maintenance_token
from smart_locker.database.models import Device, DeviceStatus
from smart_locker.database.repositories import (
    DeviceRepository,
    TransactionRepository,
)
from smart_locker.services.calibration import calibration_fields
from smart_locker.sync.catalog_sheet import normalize_pm

logger = logging.getLogger(__name__)


class CatalogError(Exception):
    """Base for catalog editor failures the API maps to HTTP errors."""


class DuplicatePm(CatalogError):
    """A device with this id already exists in the catalog."""


class DuplicateSerial(CatalogError):
    """Another catalog row already holds this serial number."""


class UnknownPm(CatalogError):
    """The id is not in the catalog."""


class DeviceBorrowed(CatalogError):
    """The unit is borrowed; the change is refused."""


class DeviceHasHistory(CatalogError):
    """The row has audit transactions; removing it would lose the trail."""


class LockerOwned(CatalogError):
    """This unit is in the cabinet; place is owned by borrow/return."""


class NotCabinetUnit(CatalogError):
    """The row has no cabinet slot; the maintenance lifecycle does not apply."""


class NotInMaintenance(CatalogError):
    """Only a maintenance unit can return to service."""


class InvalidCatalogField(CatalogError):
    """A required field was empty or a field may not be edited on this row."""


# --- Place words -------------------------------------------------------------

def place_kind(text: str | None) -> str:
    """Classify a place cell: ``locker``, ``maintenance``, or ``other``.

    One case-fold function for the two place words. The whole cell must be
    the word — ``"Blocker"`` is not the locker, ``"locker room"`` is not
    the locker.

    Args:
        text: Stored place/Location text.

    Returns:
        ``"locker"``, ``"maintenance"``, or ``"other"``.
    """
    folded = (text or "").strip().casefold()
    if not folded:
        return "other"
    if folded == in_locker_token().casefold():
        return "locker"
    if folded == maintenance_token().casefold():
        return "maintenance"
    return "other"


def canonical_place(text: str | None) -> str:
    """The stored spelling of a place: canonical token, or the typed text.

    Args:
        text: Place text as typed or read from a sheet.

    Returns:
        ``in_locker_token()``/``maintenance_token()`` for place words,
        otherwise the trimmed input.
    """
    trimmed = (text or "").strip()
    kind = place_kind(trimmed)
    if kind == "locker":
        return in_locker_token()
    if kind == "maintenance":
        return maintenance_token()
    return trimmed


def is_registered(device: Device) -> bool:
    """Whether a catalog row is a cabinet unit (a locker slot was assigned)."""
    return device.locker_slot is not None


def display_location(device: Device) -> str:
    """The Location text a screen or the mirror shows for one row.

    Cabinet units derive it: borrowed → borrower name, maintenance → the
    maintenance token, otherwise the in-locker token. Catalog-only rows
    return their stored place/owner text.

    Args:
        device: Device row (borrower relationship loaded when borrowed).

    Returns:
        Location text, ``""`` when nothing applies.
    """
    if not is_registered(device):
        return (device.location or "").strip()
    if device.status == DeviceStatus.BORROWED:
        borrower = device.current_borrower
        return (borrower.display_name or "").strip() if borrower else ""
    if device.status == DeviceStatus.MAINTENANCE:
        return maintenance_token()
    return in_locker_token()


# --- Serialization -----------------------------------------------------------

def device_record(device: Device, *, current_user_id: int | None = None) -> dict:
    """Build the common, display-safe fields for a locker device."""
    borrower_name = None
    if device.status == DeviceStatus.BORROWED and device.current_borrower_id is not None:
        if device.current_borrower_id == current_user_id:
            borrower_name = "You"
        elif device.current_borrower is not None:
            borrower_name = device.current_borrower.display_name
    return {
        "pm_number": device.pm_number,
        "name": device.name,
        "device_type": device.device_type,
        "serial_number": device.serial_number,
        "manufacturer": device.manufacturer,
        "model": device.model,
        "locker_slot": device.locker_slot,
        "description": device.description,
        "calibration_due": device.calibration_due.isoformat() if device.calibration_due else None,
        **calibration_fields(device.calibration_due),
        "status": device.status.value,
        "borrower_name": borrower_name,
        "has_tag": device.tag_hmac is not None,
    }


def catalog_record(device: Device) -> dict:
    """Build the Inventory row for the dashboard catalog view.

    One dict per catalog device — cabinet units included (``in_locker``)
    so the Inventory tab shows the whole catalog like the sheet did.
    """
    return {
        "pm_number": device.pm_number,
        "name": device.name,
        "device_type": device.device_type,
        "manufacturer": device.manufacturer,
        "model": device.model,
        "serial_number": device.serial_number,
        "location": display_location(device),
        "calibration_due": device.calibration_due.isoformat() if device.calibration_due else None,
        **calibration_fields(device.calibration_due),
        "in_locker": is_registered(device),
        "status": device.status.value,
        "has_tag": device.tag_hmac is not None,
    }


def unbind_device_tag(session, device: Device) -> None:
    """Clear a tag through the same service for either authorized client.

    Args:
        session: Active database session (caller commits).
        device: Device row to unbind.

    Raises:
        DeviceBorrowed: The unit is borrowed right now — return it first.
    """
    if device.status == DeviceStatus.BORROWED:
        raise DeviceBorrowed(f"{device.pm_number} is borrowed — return it first.")
    DeviceRepository.unbind_tag(session, device)


def _mark_mirror_dirty(session: Session) -> None:
    """Flag a catalog change so the after-commit hook rewrites the mirror.

    The key matches the one the Session after_commit listener in
    ``locker_service`` pops — every catalog mutation funnels through the
    same deferred-write mechanism as borrow/return.
    """
    session.info["mirror_dirty_pending"] = True


# --- Catalog editor ----------------------------------------------------------

def _resolve_serial(session: Session, device: Device | None, serial: str | None) -> str | None:
    """Return the serial to store, or refuse when another row holds it.

    Dashboard edits are user-typed input: a held serial is a conflict the
    admin must resolve, not silently dropped (which would erase the row's
    existing serial).
    """
    serial = (serial or "").strip() or None
    if serial is None:
        return None
    holder = DeviceRepository.find_by_serial(session, serial)
    if holder is not None and (device is None or holder.id != device.id):
        raise DuplicateSerial(
            f"Serial {serial} is already held by {holder.pm_number}."
        )
    return serial


def add_device(
    session: Session,
    *,
    pm_number: str,
    name: str,
    device_type: str | None = None,
    serial_number: str | None = None,
    manufacturer: str | None = None,
    model: str | None = None,
    calibration_due: date | None = None,
    location: str | None = None,
) -> Device:
    """Add one catalog device (not yet a cabinet unit).

    Args:
        session: Active database session (caller commits).
        pm_number: Device id — any non-empty id, not only ``PM…``.
        name: Display name.
        device_type: Category, defaults to ``"general"``.
        serial_number: Optional serial (dropped when another row holds it).
        manufacturer/model/calibration_due: Optional catalog fields.
        location: Place or holder text; the in-locker word puts the row on
            the kiosk Register Device list.

    Returns:
        The new Device row (``locker_slot`` NULL, AVAILABLE).

    Raises:
        InvalidCatalogField: id or name empty.
        DuplicatePm: id already in the catalog.
        DuplicateSerial: another row already holds the serial.
    """
    pm = normalize_pm(pm_number)
    name = (name or "").strip()
    if not pm or not name:
        raise InvalidCatalogField("Device id and name are required.")
    if DeviceRepository.find_by_pm(session, pm) is not None:
        raise DuplicatePm(f"{pm} is already in the catalog.")

    serial = _resolve_serial(session, None, serial_number)
    try:
        device = DeviceRepository.create(
            session,
            name=name,
            device_type=(device_type or "").strip() or "general",
            pm_number=pm,
            serial_number=serial,
            manufacturer=(manufacturer or "").strip() or None,
            model=(model or "").strip() or None,
            calibration_due=calibration_due,
        )
    except IntegrityError as e:
        session.rollback()
        if serial is not None and (
            DeviceRepository.find_by_serial(session, serial) is not None
        ):
            raise DuplicateSerial(
                f"Serial {serial} is already held by another row."
            ) from e
        raise DuplicatePm(f"{pm} is already in the catalog.") from e
    device.location = canonical_place(location) or None
    session.flush()
    _mark_mirror_dirty(session)
    logger.info("Catalog add: %s (pm=%s).", device.name, device.pm_number)
    return device


_EDITABLE_FIELDS = {
    "name", "device_type", "serial_number", "manufacturer", "model",
    "calibration_due",
}


def update_device_fields(session: Session, device: Device, fields: dict) -> bool:
    """Apply dashboard edits to one catalog row.

    Editable: name, device_type, serial_number, manufacturer, model,
    calibration_due. ``location`` is editable only while the row is not a
    cabinet unit — a registered unit's place is owned by borrow/return.

    Args:
        session: Active database session (caller commits).
        device: Row being edited.
        fields: Field/value pairs; unknown keys are ignored.

    Returns:
        True when at least one field changed.

    Raises:
        LockerOwned: ``location`` on a cabinet unit differs from the place
            borrow/return derives (an unchanged resend is a no-op).
        InvalidCatalogField: ``name`` was set to empty.
        DuplicateSerial: another row already holds the serial.
    """
    if "location" in fields and is_registered(device):
        # The dashboard PATCH resends the derived place it displayed — a
        # value equal to it is a no-op, not a borrow/return takeover.
        if canonical_place(fields["location"]) != display_location(device):
            raise LockerOwned(
                "This device is in the locker. Place follows borrow/return."
            )
        fields = {k: v for k, v in fields.items() if k != "location"}

    updates: dict = {}
    for key in _EDITABLE_FIELDS:
        if key in fields:
            updates[key] = fields[key]
    # Text fields are user-typed: strip, and keep the stored forms the
    # mirror round-trips — an empty cell reads back as NULL/"general".
    for key in ("name", "device_type", "manufacturer", "model"):
        if key in updates:
            updates[key] = (updates[key] or "").strip()
    if updates.get("device_type") == "":
        updates["device_type"] = "general"
    for key in ("manufacturer", "model"):
        if updates.get(key) == "":
            updates[key] = None
    if "serial_number" in updates:
        updates["serial_number"] = _resolve_serial(session, device, updates["serial_number"])
    if "name" in updates and not updates["name"]:
        raise InvalidCatalogField("Name cannot be empty.")

    changed = DeviceRepository.update_metadata(session, device, **updates)

    if "location" in fields:
        new_place = canonical_place(fields["location"]) or None
        if device.location != new_place:
            device.location = new_place
            session.flush()
            changed = True
    if changed:
        _mark_mirror_dirty(session)
        logger.info("Catalog edit: %s (pm=%s).", device.name, device.pm_number)
    return changed


def set_place(session: Session, device: Device, place: str | None) -> str:
    """Set the place/holder text on a non-cabinet row.

    Args:
        session: Active database session (caller commits).
        device: Row being edited.
        place: Free text, a person name, or a place word.

    Returns:
        The stored (canonical) place text.

    Raises:
        LockerOwned: The row is a cabinet unit.
    """
    if is_registered(device):
        raise LockerOwned(
            "This device is in the locker. Change holder at the kiosk."
        )
    stored = canonical_place(place)
    if device.location == (stored or None):
        return stored
    device.location = stored or None
    session.flush()
    _mark_mirror_dirty(session)
    logger.info("Catalog place: %s → %s.", device.pm_number, stored)
    return stored


def remove_device(session: Session, device: Device) -> None:
    """Delete one catalog row.

    Args:
        session: Active database session (caller commits).
        device: Row to delete; a bound sticker dies with the row.

    Raises:
        DeviceBorrowed: The unit is borrowed right now.
        DeviceHasHistory: The row has audit transactions — the borrow trail
            is kept, so only never-used rows can be removed.
    """
    if device.status == DeviceStatus.BORROWED:
        raise DeviceBorrowed(f"{device.pm_number} is borrowed — return it first.")
    if TransactionRepository.count_for_device(session, device.id):
        raise DeviceHasHistory(
            f"{device.pm_number} has borrow history — the audit trail is kept."
        )
    logger.info("Catalog remove: %s (pm=%s).", device.name, device.pm_number)
    session.delete(device)
    session.flush()
    _mark_mirror_dirty(session)


# --- Maintenance -------------------------------------------------------------
#
# "To maintenance" and the maintenance word in the mirror do the same thing:
# a cabinet unit out of service cannot be borrowed (LockerService refuses),
# and its derived Location cell becomes the maintenance token. Refused while
# borrowed — the loan owns the row. "Back in service" always carries the new
# calibration date; when that date is not in the future the calibration gate
# keeps borrow closed anyway.

def to_maintenance(session: Session, device: Device) -> bool:
    """Take a cabinet unit out of service.

    The AVAILABLE → MAINTENANCE write is a conditional UPDATE: a kiosk
    borrow that committed after this session's status check wins the row,
    and this call reports the conflict instead of overwriting the loan.

    Args:
        session: Active database session (caller commits).
        device: A cabinet unit (``locker_slot`` set).

    Returns:
        True when the status changed, False when it already was maintenance.

    Raises:
        NotCabinetUnit: The row is not a cabinet unit.
        DeviceBorrowed: The unit is borrowed right now — including a borrow
            that raced this call and committed first.
        UnknownPm: The row was deleted between the read and the write.
    """
    if not is_registered(device):
        raise NotCabinetUnit(f"{device.pm_number} is not a cabinet unit.")
    if device.status == DeviceStatus.BORROWED:
        raise DeviceBorrowed(f"{device.pm_number} is borrowed — return it first.")
    if device.status == DeviceStatus.MAINTENANCE:
        return False
    moved = DeviceRepository.transition_status(
        session, device, DeviceStatus.AVAILABLE, DeviceStatus.MAINTENANCE
    )
    if not moved:
        # The snapshot was stale — read the state that won the row.
        try:
            session.refresh(device)
        except InvalidRequestError as e:
            raise UnknownPm(
                f"{device.pm_number} was removed from the catalog."
            ) from e
        if device.status == DeviceStatus.BORROWED:
            raise DeviceBorrowed(
                f"{device.pm_number} is borrowed — return it first."
            )
        return False
    _mark_mirror_dirty(session)
    logger.info("To maintenance: %s (pm=%s).", device.name, device.pm_number)
    return True


def back_in_service(session: Session, device: Device, calibration_due: date | None) -> bool:
    """Return a maintenance unit to service and save its new calibration date.

    The date is required — a unit whose calibration lap ran out must not
    come back dateless. A date that is not in the future is stored anyway;
    the borrow gate keeps the unit unborrowable until it is.

    Args:
        session: Active database session (caller commits).
        device: A cabinet unit currently in maintenance.
        calibration_due: The new calibration-due date (required).

    Returns:
        True when anything changed.

    Raises:
        NotCabinetUnit: The row is not a cabinet unit.
        NotInMaintenance: The unit is not in maintenance.
        InvalidCatalogField: No calibration date was given.
        UnknownPm: The row was deleted between the read and the write.
    """
    if not is_registered(device):
        raise NotCabinetUnit(f"{device.pm_number} is not a cabinet unit.")
    if device.status != DeviceStatus.MAINTENANCE:
        raise NotInMaintenance(f"{device.pm_number} is not in maintenance.")
    if calibration_due is None:
        raise InvalidCatalogField(
            "Back in service needs the new calibration date."
        )
    # One conditional write carries the new date and clears any borrower
    # reference: the predicate is checked at write time (a raced change
    # wins the row), and a maintenance row that wrongly kept a borrower
    # does not come back still attributed.
    moved = DeviceRepository.transition_status(
        session,
        device,
        DeviceStatus.MAINTENANCE,
        DeviceStatus.AVAILABLE,
        calibration_due=calibration_due,
        current_borrower_id=None,
    )
    if not moved:
        try:
            session.refresh(device)
        except InvalidRequestError as e:
            raise UnknownPm(
                f"{device.pm_number} was removed from the catalog."
            ) from e
        raise NotInMaintenance(f"{device.pm_number} is not in maintenance.")
    _mark_mirror_dirty(session)
    logger.info(
        "Back in service: %s (pm=%s), calibration due %s.",
        device.name, device.pm_number, calibration_due,
    )
    return True


def apply_place_word(session: Session, device: Device, text: str | None) -> bool:
    """Apply a sheet/hand-typed place word to a cabinet unit.

    Only the maintenance word acts — every other cell value is derived
    state the Pi owns (borrower name, in-locker token), so the word is a
    no-op there. The reverse direction is not offered: a sheet cell cannot
    supply the new calibration date back-in-service requires.

    Args:
        session: Active database session (caller commits).
        device: Any catalog row.
        text: The Location cell as read from the sheet.

    Returns:
        True when the unit is in maintenance after the call, False for a
        non-cabinet row, a borrowed unit, or a non-maintenance word.
    """
    if not is_registered(device):
        return False
    if place_kind(text) != "maintenance":
        return False
    if device.status == DeviceStatus.BORROWED:
        return False
    if device.status == DeviceStatus.MAINTENANCE:
        return True
    try:
        return to_maintenance(session, device)
    except DeviceBorrowed:
        # A borrow landed between the status check and the write — that is
        # "skipped", not an error worth surfacing into the mirror apply.
        return False


def registerable_devices(session: Session) -> list[Device]:
    """Catalog rows eligible for Register Device on the kiosk.

    Rows whose place is the in-locker word and which have no cabinet slot
    yet. The admin assigns a slot and taps the sticker to put one in.
    """
    return [
        d for d in DeviceRepository.list_all(session)
        if d.locker_slot is None and place_kind(d.location) == "locker"
    ]
