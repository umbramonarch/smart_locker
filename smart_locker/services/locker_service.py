"""
File: locker_service.py
Description: Borrow/return business logic for the Smart Locker system. Enforces
             per-user borrow limits, device availability checks, ownership rules,
             admin return-on-behalf, and unattended idle return with full
             transaction logging. After a successful location change, writes
             Location back into the catalog Excel (best-effort).
Project: smart_locker/services
Notes: The borrow limit is configured via MAX_BORROWS in config/settings.py
       (default 5). Admins can return any device on behalf of the original
       borrower. Idle sticker taps use return_unattended (no work card).
       Excel write-back never raises; SQLite remains the locker source of truth.
"""

import logging

from sqlalchemy.orm import Session

from config.settings import MAX_BORROWS
from smart_locker.auth.session_manager import UserSession
from smart_locker.database.models import DeviceStatus, UserRole
from smart_locker.database.repositories import DeviceRepository, TransactionRepository, UserRepository

logger = logging.getLogger(__name__)


def _write_location(db_session: Session) -> None:
    """Commit SQLite, then enqueue Location write-back. Never blocks on Excel."""
    db_session.flush()
    try:
        db_session.commit()
    except Exception:
        logger.exception("Commit before Location write-back failed.")
        db_session.rollback()
        raise
    from smart_locker.sync.location_writeback import schedule_write_location

    schedule_write_location()


class LockerService:
    """Business logic for device borrow and return operations.

    Enforces per-user borrow limits (MAX_BORROWS from config), device
    availability checks, ownership rules for returns, admin
    return-on-behalf, and unattended return from the idle kiosk. All
    operations are logged to the transaction audit trail.
    """

    @staticmethod
    def borrow_device(
        db_session: Session,
        user_session: UserSession,
        device_id: int,
        notes: str | None = None,
    ) -> bool:
        """Borrow a device for the current user.

        Args:
            db_session: Active database session.
            user_session: Current authenticated user session.
            device_id: ID of device to borrow.
            notes: Optional notes for the transaction.

        Returns:
            True if borrow succeeded, False otherwise.
        """
        if user_session.is_expired:
            logger.warning("Borrow attempted with expired session.")
            return False

        user = user_session.user

        device = DeviceRepository.find_by_id(db_session, device_id)
        if device is None:
            logger.warning("Borrow failed: device %d not found.", device_id)
            return False

        borrowed_count = DeviceRepository.count_borrowed_by_user(db_session, user.id)
        if borrowed_count >= MAX_BORROWS:
            logger.warning(
                "Borrow failed: %s has reached the borrow limit (%d/%d).",
                user.display_name,
                borrowed_count,
                MAX_BORROWS,
            )
            return False

        if device.status != DeviceStatus.AVAILABLE:
            logger.warning(
                "Borrow failed: device %d (%s) is %s.",
                device_id,
                device.name,
                device.status.value,
            )
            return False
        DeviceRepository.borrow(db_session, device, user.id)
        TransactionRepository.log_borrow(db_session, user.id, device_id, notes)
        user_session.touch()
        _write_location(db_session)

        logger.info(
            "%s borrowed %s (device=%d)",
            user.display_name,
            device.name,
            device_id,
        )
        return True

    @staticmethod
    def return_device(
        db_session: Session,
        user_session: UserSession,
        device_id: int,
        notes: str | None = None,
    ) -> bool:
        """Return a borrowed device.

        Args:
            db_session: Active database session.
            user_session: Current authenticated user session.
            device_id: ID of device to return.
            notes: Optional notes for the transaction.

        Returns:
            True if return succeeded, False otherwise.
        """
        if user_session.is_expired:
            logger.warning("Return attempted with expired session.")
            return False

        device = DeviceRepository.find_by_id(db_session, device_id)
        if device is None:
            logger.warning("Return failed: device %d not found.", device_id)
            return False

        if device.status != DeviceStatus.BORROWED:
            logger.warning(
                "Return failed: device %d (%s) is not borrowed.",
                device_id,
                device.name,
            )
            return False

        user = user_session.user
        if device.current_borrower_id != user.id:
            if user.role != UserRole.ADMIN:
                logger.warning(
                    "Return failed: device %d is borrowed by user %d, not %d.",
                    device_id,
                    device.current_borrower_id,
                    user.id,
                )
                return False

            # Admin returning on behalf of the original borrower
            original_borrower_id = device.current_borrower_id
            DeviceRepository.return_device(db_session, device)
            TransactionRepository.log_return(
                db_session,
                user_id=original_borrower_id,
                device_id=device_id,
                notes=notes,
                performed_by_id=user.id,
            )
            user_session.touch()
            _write_location(db_session)
            logger.info(
                "Admin %s returned %s (device=%d) on behalf of user %d",
                user.display_name,
                device.name,
                device_id,
                original_borrower_id,
            )
            return True

        DeviceRepository.return_device(db_session, device)
        TransactionRepository.log_return(db_session, user.id, device_id, notes)
        user_session.touch()
        _write_location(db_session)

        logger.info(
            "%s returned %s (device=%d)",
            user.display_name,
            device.name,
            device_id,
        )
        return True

    @staticmethod
    def transfer_device(
        db_session: Session,
        user_session: UserSession,
        device_id: int,
        notes: str | None = None,
    ) -> bool:
        """Transfer responsibility for a borrowed device to the current user.

        Records a return for the original borrower and a borrow for the new
        user, so the audit trail is preserved. The device stays borrowed;
        only current_borrower_id changes. Enforces the new user's borrow limit.

        Args:
            db_session: Active database session.
            user_session: Current authenticated user session.
            device_id: ID of the device to transfer.
            notes: Optional notes for the transaction.

        Returns:
            True if transfer succeeded, False otherwise.
        """
        if user_session.is_expired:
            logger.warning("Transfer attempted with expired session.")
            return False

        device = DeviceRepository.find_by_id(db_session, device_id)
        if device is None:
            logger.warning("Transfer failed: device %d not found.", device_id)
            return False

        if device.status != DeviceStatus.BORROWED:
            logger.warning(
                "Transfer failed: device %d (%s) is not borrowed.",
                device_id,
                device.name,
            )
            return False

        user = user_session.user
        if device.current_borrower_id == user.id:
            logger.warning(
                "Transfer failed: device %d is already held by %s.",
                device_id,
                user.display_name,
            )
            return False

        original_borrower_id = device.current_borrower_id
        if original_borrower_id is None:
            logger.warning(
                "Transfer failed: device %d has no recorded borrower.",
                device_id,
            )
            return False

        new_borrowed_count = DeviceRepository.count_borrowed_by_user(
            db_session, user.id
        )
        if new_borrowed_count >= MAX_BORROWS:
            logger.warning(
                "Transfer failed: %s has reached the borrow limit (%d/%d).",
                user.display_name,
                new_borrowed_count,
                MAX_BORROWS,
            )
            return False

        original_borrower = UserRepository.find_by_id(db_session, original_borrower_id)
        original_name = original_borrower.display_name if original_borrower else "unknown"

        DeviceRepository.return_device(db_session, device)
        TransactionRepository.log_return(
            db_session,
            user_id=original_borrower_id,
            device_id=device_id,
            notes=f"transferred to {user.display_name}",
        )
        DeviceRepository.borrow(db_session, device, user.id)
        TransactionRepository.log_borrow(
            db_session,
            user.id,
            device_id,
            notes=f"transferred from {original_name}",
        )
        user_session.touch()
        _write_location(db_session)

        logger.info(
            "%s transferred %s (device=%d) from user %d",
            user.display_name,
            device.name,
            device_id,
            original_borrower_id,
        )
        return True

    @staticmethod
    def return_unattended(db_session: Session, device_id: int) -> bool:
        """Return a borrowed device without a work-card session.

        Used when a borrowed device sticker is tapped on the idle kiosk.
        The original borrower stays on the transaction log; notes record
        that no card was presented.

        Args:
            db_session: Active database session.
            device_id: ID of the device to return.

        Returns:
            True if return succeeded, False otherwise.
        """
        device = DeviceRepository.find_by_id(db_session, device_id)
        if device is None:
            logger.warning("Unattended return failed: device %d not found.", device_id)
            return False

        if device.status != DeviceStatus.BORROWED:
            logger.warning(
                "Unattended return failed: device %d (%s) is not borrowed.",
                device_id,
                device.name,
            )
            return False

        original_borrower_id = device.current_borrower_id
        if original_borrower_id is None:
            logger.warning(
                "Unattended return failed: device %d (%s) has no borrower.",
                device_id,
                device.name,
            )
            return False

        DeviceRepository.return_device(db_session, device)
        TransactionRepository.log_return(
            db_session,
            user_id=original_borrower_id,
            device_id=device_id,
            notes="returned at kiosk without card",
        )
        _write_location(db_session)
        logger.info(
            "Unattended return of %s (device=%d) for user %d",
            device.name,
            device_id,
            original_borrower_id,
        )
        return True

    @staticmethod
    def get_available_devices(db_session: Session) -> list:
        """Return all devices with AVAILABLE status.

        Args:
            db_session: Active database session.

        Returns:
            List of available Device objects.
        """
        return DeviceRepository.get_available_devices(db_session)

    @staticmethod
    def get_user_borrowed_devices(db_session: Session, user_id: int) -> list:
        """Return all devices currently borrowed by a specific user.

        Args:
            db_session: Active database session.
            user_id: ID of the borrower.

        Returns:
            List of Device objects borrowed by the user.
        """
        return DeviceRepository.get_borrowed_by_user(db_session, user_id)
