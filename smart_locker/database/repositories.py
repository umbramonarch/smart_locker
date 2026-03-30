"""
File: repositories.py
Description: Data access layer using the repository pattern. Provides CRUD
             operations for User, Device, and TransactionLog models with
             SQLAlchemy query construction and flush-based persistence.
Project: smart_locker/database
Notes: All write operations call session.flush() to assign IDs immediately
       but leave final commit/rollback to the caller or context manager.
"""

import logging
from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from smart_locker.database.models import (
    Device,
    DeviceStatus,
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
    def find_by_display_name(session: Session, name: str) -> User | None:
        """Case-insensitive lookup by display name.

        Used by the source import to match borrower names from the
        "Aktueller Einsatzort" column to registered users.

        Args:
            session: Active database session.
            name: Display name to search for (compared case-insensitively).

        Returns:
            User object or None if no match.
        """
        stmt = select(User).where(func.lower(User.display_name) == name.strip().lower())
        return session.execute(stmt).scalar_one_or_none()

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


class DeviceRepository:
    """Data access layer for Device entities.

    Provides lookup by ID and PM number, availability and borrower queries,
    borrow/return state transitions, metadata updates from source imports,
    and device creation with full field support.
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
    def borrow(session: Session, device: Device, user_id: int) -> None:
        """Mark a device as borrowed by the given user.

        Args:
            session: Active database session.
            device: Device object to borrow.
            user_id: ID of the borrowing user.
        """
        device.status = DeviceStatus.BORROWED
        device.current_borrower_id = user_id
        session.flush()

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
        barcode: str | None = None,
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
            barcode: Barcode value for future scanner integration (optional).
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
            barcode=barcode,
            calibration_due=calibration_due,
        )
        # Apply optional status and borrower (used by source import when
        # the "Aktueller Einsatzort" column indicates a device is checked out)
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
            pm_number: Company equipment/PM number string.

        Returns:
            Device object or None if not found.
        """
        stmt = select(Device).where(Device.pm_number == pm_number)
        return session.execute(stmt).scalar_one_or_none()

    @staticmethod
    def update_metadata(session: Session, device: Device, **kwargs) -> bool:
        """Update source-managed metadata fields on a device.

        Only updates fields that differ from the current value. Restricted
        to the ALLOWED set — slot, image, and description are never
        overwritten by source imports. Status and current_borrower_id ARE
        updated because the source Excel "Aktueller Einsatzort" column is
        the authoritative record of who has the device.

        Args:
            session: Active database session.
            device: Device object to update.
            **kwargs: Field name/value pairs to update.

        Returns:
            True if any field was changed, False if all values matched.
        """
        # Fields that the source import is allowed to modify
        ALLOWED = {
            "name", "device_type", "serial_number", "manufacturer",
            "model", "barcode", "calibration_due",
            "status", "current_borrower_id",
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
        """Find all devices matching a model/Typbezeichnung (case-insensitive).

        Used by the photo watcher to assign one image to every device that
        shares the same model string (e.g. all "87V" units).

        Args:
            session: Active database session.
            model: Model string to match (compared case-insensitively).

        Returns:
            List of matching Device objects (may be empty).
        """
        stmt = select(Device).where(func.lower(Device.model) == model.strip().lower())
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
    def get_user_history(
        session: Session, user_id: int
    ) -> list[TransactionLog]:
        """Return all transactions for a user, most recent first.

        Args:
            session: Active database session.
            user_id: ID of the user.

        Returns:
            List of TransactionLog entries ordered by timestamp descending.
        """
        stmt = (
            select(TransactionLog)
            .where(TransactionLog.user_id == user_id)
            .order_by(TransactionLog.timestamp.desc())
        )
        return list(session.execute(stmt).scalars().all())

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
