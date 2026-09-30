"""
File: repositories.py
Description: Data access layer using the repository pattern. Provides CRUD
             operations for User, Device, TransactionLog, and Registrant
             models with SQLAlchemy query construction and flush-based
             persistence. The RegistrantRepository manages the list of
             approved names for self-service NFC card registration.
Project: smart_locker/database
Notes: All write operations call session.flush() to assign IDs immediately
       but leave final commit/rollback to the caller or context manager.
"""

import logging
from datetime import date, datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload

from smart_locker.database.models import (
    Device,
    DeviceStatus,
    Registrant,
    TransactionLog,
    TransactionType,
    User,
)

logger = logging.getLogger(__name__)


class UserRepository:
    """Data access layer for User entities.

    Provides indexed HMAC-based card lookup, primary key lookup, user
    creation with flush-based ID assignment, and listing.
    """

    @staticmethod
    def find_by_uid_hmac(session: Session, uid_hmac: str) -> User | None:
        """O(1) indexed lookup by HMAC of card UID."""
        stmt = select(User).where(User.uid_hmac == uid_hmac)
        return session.execute(stmt).scalar_one_or_none()

    @staticmethod
    def find_by_id(session: Session, user_id: int) -> User | None:
        """Look up a user by primary key.

        Args:
            session: Active database session.
            user_id: User's primary key ID.

        Returns:
            User object or None if not found.
        """
        return session.get(User, user_id)

    @staticmethod
    def create(
        session: Session,
        display_name: str,
        uid_hmac: str,
        encrypted_card_uid: str,
        role: str = "user",
    ) -> User:
        """Create and persist a new user.

        Args:
            session: Active database session.
            display_name: User's display name.
            uid_hmac: HMAC-SHA256 digest of the card UID.
            encrypted_card_uid: AES-256-GCM encrypted card UID.
            role: User role — "user" or "admin".

        Returns:
            Created User object with assigned ID.
        """
        from smart_locker.database.models import UserRole

        user = User(
            display_name=display_name,
            uid_hmac=uid_hmac,
            encrypted_card_uid=encrypted_card_uid,
            role=UserRole(role),
        )
        session.add(user)
        session.flush()
        logger.info("Created user: %s (id=%d)", display_name, user.id)
        return user

    @staticmethod
    def list_all(session: Session) -> list[User]:
        """Return all users ordered by display name.

        Args:
            session: Active database session.

        Returns:
            List of all User objects.
        """
        stmt = select(User).order_by(User.display_name)
        return list(session.execute(stmt).scalars().all())

    @staticmethod
    def active_names(session: Session) -> set[str]:
        """Names already held by active card users for self-registration."""
        stmt = select(User.display_name).where(User.is_active.is_(True))
        return {name.lower() for name in session.execute(stmt).scalars()}

    @staticmethod
    def first_active_admin(session: Session) -> User | None:
        """First active admin available to the existing kiosk overlay."""
        from smart_locker.database.models import UserRole

        stmt = (
            select(User)
            .where(User.role == UserRole.ADMIN, User.is_active.is_(True))
            .order_by(User.id)
            .limit(1)
        )
        return session.execute(stmt).scalars().first()

    @staticmethod
    def count_active_admins(session: Session) -> int:
        """How many active admin users exist (last-admin guard input)."""
        from smart_locker.database.models import UserRole

        stmt = select(func.count()).select_from(User).where(
            User.role == UserRole.ADMIN, User.is_active.is_(True)
        )
        return session.execute(stmt).scalar_one()


class DeviceRepository:
    """Data access layer for Device entities.

    Provides lookup by ID and PM number, availability and borrower queries,
    borrow/return state transitions, catalog metadata updates (dashboard
    editor and mirror apply/adopt), and device creation with full field
    support.
    """

    @staticmethod
    def find_by_id(session: Session, device_id: int) -> Device | None:
        """Look up a device by primary key.

        Args:
            session: Active database session.
            device_id: Device's primary key ID.

        Returns:
            Device object or None if not found.
        """
        return session.get(Device, device_id)

    @staticmethod
    def get_available_devices(session: Session) -> list[Device]:
        """Return all devices with AVAILABLE status.

        Args:
            session: Active database session.

        Returns:
            List of available Device objects.
        """
        stmt = select(Device).where(Device.status == DeviceStatus.AVAILABLE)
        return list(session.execute(stmt).scalars().all())

    @staticmethod
    def get_borrowed_by_user(session: Session, user_id: int) -> list[Device]:
        """Return all devices currently borrowed by a specific user.

        Args:
            session: Active database session.
            user_id: ID of the borrower.

        Returns:
            List of Device objects borrowed by the user.
        """
        stmt = select(Device).where(
            Device.status == DeviceStatus.BORROWED,
            Device.current_borrower_id == user_id,
        )
        return list(session.execute(stmt).scalars().all())

    @staticmethod
    def transition_status(
        session: Session,
        device: Device,
        expect: DeviceStatus,
        to: DeviceStatus,
        **fields,
    ) -> bool:
        """Move a device to ``to`` only while its row still holds ``expect``.

        The ``expect`` predicate is part of the UPDATE's WHERE clause, so it
        is evaluated atomically at write time — a status change another
        session committed after this session's earlier read wins the row,
        and this statement matches zero rows instead of overwriting the
        newer state. Extra column writes (borrower id, calibration date)
        ride along in the same UPDATE.

        Args:
            session: Active database session.
            device: Device row to transition (bound to ``session``).
            expect: Status the row must hold for the write to land.
            to: Status to set when the predicate matches.
            **fields: Extra column values written with the transition.

        Returns:
            True only when the row moved. On False the row holds a state
            this session has not seen — the caller refreshes to read the
            winning state.
        """
        stmt = (
            update(Device)
            .where(Device.id == device.id, Device.status == expect)
            .values(status=to, **fields)
        )
        result = session.execute(stmt)
        if result.rowcount != 1:
            return False
        session.refresh(device)
        return True

    @staticmethod
    def borrow(session: Session, device: Device, user_id: int) -> bool:
        """Mark a device as borrowed by the given user.

        The AVAILABLE → BORROWED write is conditional: the UPDATE only
        lands while the row is still available, so a maintenance write
        that committed after the caller's availability check wins instead
        of being overwritten.

        Args:
            session: Active database session.
            device: Device object to borrow.
            user_id: ID of the borrowing user.

        Returns:
            True when the borrow landed; False when the row was no longer
            available (the caller refreshes to read the winning state).
        """
        return DeviceRepository.transition_status(
            session,
            device,
            DeviceStatus.AVAILABLE,
            DeviceStatus.BORROWED,
            current_borrower_id=user_id,
        )

    @staticmethod
    def return_device(session: Session, device: Device) -> None:
        """Mark a device as available and clear the borrower.

        Args:
            session: Active database session.
            device: Device object to return.
        """
        device.status = DeviceStatus.AVAILABLE
        device.current_borrower_id = None
        session.flush()

    @staticmethod
    def count_borrowed_by_user(session: Session, user_id: int) -> int:
        """Count how many devices a user currently has borrowed.

        Args:
            session: Active database session.
            user_id: ID of the user.

        Returns:
            Number of devices currently borrowed by the user.
        """
        stmt = select(func.count()).select_from(Device).where(
            Device.status == DeviceStatus.BORROWED,
            Device.current_borrower_id == user_id,
        )
        return session.execute(stmt).scalar_one()

    @staticmethod
    def create(
        session: Session,
        name: str,
        device_type: str,
        pm_number: str,
        serial_number: str | None = None,
        locker_slot: int | None = None,
        description: str | None = None,
        image_path: str | None = None,
        manufacturer: str | None = None,
        model: str | None = None,
        calibration_due: date | None = None,
        status: str | None = None,
        current_borrower_id: int | None = None,
    ) -> Device:
        """Create and persist a new device.

        Args:
            session: Active database session.
            name: Display name for the device.
            device_type: Category (e.g., "Oscilloscope", "Multimeter").
            pm_number: Unique PM/equipment number (business key).
            serial_number: Manufacturer serial number (optional, unique).
            locker_slot: Physical locker slot number (optional).
            description: Short description of the device (optional).
            image_path: Path to device photo relative to frontend/images/ (optional).
            manufacturer: Device manufacturer name (optional).
            model: Model/type designation (optional).
            calibration_due: Next calibration date (optional).
            status: Device status string (AVAILABLE, BORROWED, MAINTENANCE).
                Defaults to AVAILABLE if not provided.
            current_borrower_id: User ID of the current borrower when importing
                a device that is already checked out (optional).

        Returns:
            Created Device object with assigned ID.
        """
        device = Device(
            name=name,
            device_type=device_type,
            pm_number=pm_number,
            serial_number=serial_number,
            locker_slot=locker_slot,
            description=description,
            image_path=image_path,
            manufacturer=manufacturer,
            model=model,
            calibration_due=calibration_due,
        )
        # Apply optional status and borrower (used by tests/fixtures that
        # create a device in a non-default state)
        if status is not None:
            device.status = DeviceStatus(status)
        if current_borrower_id is not None:
            device.current_borrower_id = current_borrower_id
        session.add(device)
        session.flush()
        logger.info("Created device: %s (id=%d, pm=%s)", name, device.id, pm_number)
        return device

    @staticmethod
    def find_by_pm(session: Session, pm_number: str) -> Device | None:
        """Look up a device by its PM number (unique business key).

        Args:
            session: Active database session.
            pm_number: Join-key string (unique business key).

        Returns:
            Device object or None if not found.
        """
        want = (pm_number or "").strip()
        if not want:
            return None
        found = session.execute(
            select(Device).where(Device.pm_number == want)
        ).scalar_one_or_none()
        if found is not None:
            return found
        from smart_locker.sync.catalog_sheet import pm_match_key

        key = pm_match_key(want)
        return session.execute(
            select(Device).where(func.lower(Device.pm_number) == key)
        ).scalar_one_or_none()

    @staticmethod
    def find_by_serial(session: Session, serial_number: str) -> Device | None:
        """Look up a device by manufacturer serial number.

        Exact match first, then a case-folded match — sheet-typed serials
        may differ only in letter case from the stored value.

        Args:
            session: Active database session.
            serial_number: Serial string as stored on the device.

        Returns:
            Device object or None if not found.
        """
        want = (serial_number or "").strip()
        if not want:
            return None
        found = session.execute(
            select(Device).where(Device.serial_number == want)
        ).scalar_one_or_none()
        if found is not None:
            return found
        return session.execute(
            select(Device).where(func.lower(Device.serial_number) == want.lower())
        ).scalar_one_or_none()

    @staticmethod
    def set_locker_slot(session: Session, device: Device, locker_slot: int) -> None:
        """Set the physical cabinet slot on a locker device.

        Args:
            session: Active database session.
            device: Device row to update.
            locker_slot: Cabinet slot number (>= 1).
        """
        device.locker_slot = locker_slot
        session.flush()

    @staticmethod
    def find_by_tag_hmac(session: Session, tag_hmac: str) -> Device | None:
        """Look up a device by the HMAC of its NFC sticker UID.

        Args:
            session: Active database session.
            tag_hmac: HMAC-SHA256 digest of the sticker UID.

        Returns:
            Device object or None if no sticker is bound to that digest.
        """
        stmt = select(Device).where(Device.tag_hmac == tag_hmac)
        return session.execute(stmt).scalar_one_or_none()

    @staticmethod
    def bind_tag(session: Session, device: Device, tag_hmac: str) -> None:
        """Set or replace the NFC sticker HMAC on a device.

        Args:
            session: Active database session.
            device: Device row to bind.
            tag_hmac: HMAC-SHA256 digest of the sticker UID.
        """
        device.tag_hmac = tag_hmac
        session.flush()

    @staticmethod
    def unbind_tag(session: Session, device: Device) -> None:
        """Clear the NFC sticker HMAC on a device.

        Args:
            session: Active database session.
            device: Device row to unbind.
        """
        device.tag_hmac = None
        session.flush()

    @staticmethod
    def update_metadata(session: Session, device: Device, **kwargs) -> bool:
        """Update catalog-managed metadata fields on a device.

        Only updates fields that differ from the current value. Restricted
        to the ALLOWED set — slot, image, description, tag_hmac, status, and
        current_borrower_id are never overwritten here (locker borrow state
        and tag bindings are locker-local).

        Args:
            session: Active database session.
            device: Device object to update.
            **kwargs: Field name/value pairs to update.

        Returns:
            True if any field was changed, False if all values matched.
        """
        ALLOWED = {
            "name", "device_type", "serial_number", "manufacturer",
            "model", "calibration_due",
        }
        changed = False
        for key, value in kwargs.items():
            if key not in ALLOWED:
                continue
            if getattr(device, key) != value:
                setattr(device, key, value)
                changed = True
        if changed:
            session.flush()
            logger.info("Updated device metadata: %s (pm=%s)", device.name, device.pm_number)
        return changed

    @staticmethod
    def find_by_model(session: Session, model: str) -> list[Device]:
        """Find all devices matching a model (case-insensitive).

        Used by the photo watcher to assign one image to every device that
        shares the same model string (e.g. all "87V" units).

        Args:
            session: Active database session.
            model: Model string to match (compared case-insensitively).

        Returns:
            List of matching Device objects (may be empty).
        """
        stmt = (
            select(Device)
            .where(func.lower(Device.model) == model.strip().lower())
            .order_by(Device.id)
        )
        return list(session.execute(stmt).scalars().all())

    @staticmethod
    def list_all(session: Session) -> list[Device]:
        """Return all devices ordered by name.

        Args:
            session: Active database session.

        Returns:
            List of all Device objects.
        """
        stmt = select(Device).order_by(Device.name)
        return list(session.execute(stmt).scalars().all())

    @staticmethod
    def list_by_slot(session: Session) -> list[Device]:
        """Return locker rows (slot assigned) in dashboard slot/name order."""
        stmt = (
            select(Device)
            .where(Device.locker_slot.is_not(None))
            .order_by(Device.locker_slot, Device.name)
        )
        return list(session.execute(stmt).scalars().all())

    @staticmethod
    def list_kiosk_devices(session: Session) -> list[Device]:
        """Return locker rows for the kiosk grids, in slot/name order.

        The kiosk borrow and return grids show units that have a sticker
        bound, plus any unit currently on loan — a row borrowed while
        untagged (possible before stickers were required) stays on the
        return grid so the stranded loan can still be closed.
        """
        stmt = (
            select(Device)
            .where(
                Device.locker_slot.is_not(None),
                Device.tag_hmac.is_not(None)
                | (Device.status == DeviceStatus.BORROWED),
            )
            .order_by(Device.locker_slot, Device.name)
        )
        return list(session.execute(stmt).scalars().all())


class TransactionRepository:
    """Data access layer for TransactionLog audit records.

    Provides borrow/return logging with optional admin-on-behalf tracking,
    and history queries by user or device with descending timestamp order.
    """

    @staticmethod
    def log_borrow(
        session: Session,
        user_id: int,
        device_id: int,
        notes: str | None = None,
    ) -> TransactionLog:
        """Record a borrow transaction.

        Args:
            session: Active database session.
            user_id: ID of the borrowing user.
            device_id: ID of the borrowed device.
            notes: Optional notes for the transaction.

        Returns:
            Created TransactionLog entry.
        """
        txn = TransactionLog(
            user_id=user_id,
            device_id=device_id,
            transaction_type=TransactionType.BORROW,
            notes=notes,
        )
        session.add(txn)
        session.flush()
        logger.info("Logged BORROW: user=%d device=%d", user_id, device_id)
        return txn

    @staticmethod
    def log_return(
        session: Session,
        user_id: int,
        device_id: int,
        notes: str | None = None,
        performed_by_id: int | None = None,
    ) -> TransactionLog:
        """Record a return transaction, optionally with admin performer.

        Args:
            session: Active database session.
            user_id: ID of the original borrower.
            device_id: ID of the returned device.
            notes: Optional notes for the transaction.
            performed_by_id: ID of the admin who performed the return on
                behalf of the borrower (None if self-return).

        Returns:
            Created TransactionLog entry.
        """
        txn = TransactionLog(
            user_id=user_id,
            device_id=device_id,
            transaction_type=TransactionType.RETURN,
            notes=notes,
            performed_by_id=performed_by_id,
        )
        session.add(txn)
        session.flush()
        if performed_by_id is not None:
            logger.info(
                "Logged RETURN: user=%d device=%d (admin=%d)", user_id, device_id, performed_by_id
            )
        else:
            logger.info("Logged RETURN: user=%d device=%d", user_id, device_id)
        return txn

    @staticmethod
    def count_for_device(session: Session, device_id: int) -> int:
        """Return how many audit rows reference one device.

        Args:
            session: Active database session.
            device_id: ID of the device.

        Returns:
            Transaction count; >0 means the row carries borrow history.
        """
        stmt = select(func.count()).select_from(TransactionLog).where(
            TransactionLog.device_id == device_id
        )
        return session.execute(stmt).scalar_one()

    @staticmethod
    def get_device_history(
        session: Session, device_id: int
    ) -> list[TransactionLog]:
        """Return all transactions for a device, most recent first.

        Args:
            session: Active database session.
            device_id: ID of the device.

        Returns:
            List of TransactionLog entries ordered by timestamp descending.
        """
        stmt = (
            select(TransactionLog)
            .where(TransactionLog.device_id == device_id)
            .order_by(TransactionLog.timestamp.desc())
        )
        return list(session.execute(stmt).scalars().all())

    @staticmethod
    def get_dashboard_history(session: Session) -> list[TransactionLog]:
        """Return the 500 newest transactions with dashboard relationships.

        Eager loading keeps the audit-feed route from issuing one query per
        user, device, or admin performer while mapping the response JSON.

        Args:
            session: Active database session.

        Returns:
            Up to 500 transactions ordered by timestamp descending.
        """
        stmt = (
            select(TransactionLog)
            .options(
                selectinload(TransactionLog.user),
                selectinload(TransactionLog.device),
                selectinload(TransactionLog.performed_by),
            )
            .order_by(TransactionLog.timestamp.desc())
            .limit(500)
        )
        return list(session.execute(stmt).scalars().all())


class RegistrantRepository:
    """Data access layer for Registrant entities.

    Manages the approved-names list used by the self-service registration
    screen. Person names seeded from the sheet's "Location" column land in
    the ``registrants`` table once — when the mirror adopts an existing
    catalog sheet (add new names, delete names that left). After that the
    list lives in the database. The repository provides methods for
    retrieving the sorted name list, replacing the list, bulk-adding names
    (skipping duplicates), and case-insensitive name lookup.
    """

    @staticmethod
    def get_all(session: Session) -> list[Registrant]:
        """Return all registrant names sorted alphabetically.

        Used by the ``GET /api/registrants`` endpoint to populate the
        self-service registration name list on the kiosk UI.

        Args:
            session: Active database session.

        Returns:
            List of Registrant objects ordered by display_name ascending.
        """
        stmt = select(Registrant).order_by(Registrant.display_name)
        return list(session.execute(stmt).scalars().all())

    @staticmethod
    def sync_names(session: Session, names: set[str]) -> int:
        """Replace the registrant list with the current Excel Location names.

        Adds names that are not already present (case-insensitive) and
        deletes rows whose display name is no longer in ``names``. Callers
        that could not read a Location column should skip this method so
        an import without that column does not wipe the list.

        Args:
            session: Active database session.
            names: Person names currently in Excel Location (not in-locker
                tokens). An empty set removes every registrant row.

        Returns:
            Number of newly inserted registrant names.
        """
        wanted = {n.strip() for n in names if n and str(n).strip()}
        wanted_lower = {n.lower() for n in wanted}
        existing = list(session.execute(select(Registrant)).scalars().all())
        removed = 0
        for row in existing:
            if row.display_name.lower() not in wanted_lower:
                session.delete(row)
                removed += 1
        if removed:
            session.flush()
            logger.info(
                "Removed %d registrant name(s) no longer in Location.", removed
            )
        return RegistrantRepository.add_names(session, wanted)

    @staticmethod
    def add_names(session: Session, names: set[str]) -> int:
        """Bulk-add new registrant names, skipping any that already exist.

        Performs a case-insensitive check for each name against the existing
        registrants table. Only names not already present are inserted. This
        makes the operation safe to call repeatedly (idempotent) — repeated
        calls will not create duplicate rows.

        Args:
            session: Active database session.
            names: Set of person name strings to add.

        Returns:
            Number of newly inserted registrant names.
        """
        # Fetch existing names once for efficient deduplication
        existing = {
            r.display_name.lower()
            for r in session.execute(select(Registrant)).scalars().all()
        }

        added = 0
        for name in sorted(names):
            # Skip names that are empty or already exist (case-insensitive)
            stripped = name.strip()
            if not stripped or stripped.lower() in existing:
                continue
            session.add(Registrant(display_name=stripped))
            existing.add(stripped.lower())
            added += 1

        if added > 0:
            session.flush()
            logger.info("Added %d new registrant name(s).", added)

        return added

    @staticmethod
    def find_by_name(session: Session, name: str) -> Registrant | None:
        """Case-insensitive lookup of a registrant by display name.

        Used by the ``POST /api/register`` endpoint to validate that the
        selected name exists in the approved registrants list before
        allowing self-service registration.

        Args:
            session: Active database session.
            name: Display name to search for (compared case-insensitively).

        Returns:
            Registrant object or None if no match.
        """
        stmt = select(Registrant).where(
            func.lower(Registrant.display_name) == name.strip().lower()
        )
        return session.execute(stmt).scalar_one_or_none()
