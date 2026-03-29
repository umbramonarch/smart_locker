"""
File: locker_service.py
Description: Borrow/return business logic for the Smart Locker system. Enforces
             per-user borrow limits, device availability checks, ownership rules,
             and admin return-on-behalf capability with full transaction logging.
Project: smart_locker/services
Notes: The borrow limit is configured via MAX_BORROWS in config/settings.py
       (default 5). Admins can return any device on behalf of the original borrower.
"""

import logging

from sqlalchemy.orm import Session

from config.settings import MAX_BORROWS
from smart_locker.auth.session_manager import UserSession
from smart_locker.database.models import DeviceStatus, UserRole
from smart_locker.database.repositories import DeviceRepository, TransactionRepository

logger = logging.getLogger(__name__)


class LockerService:
    """Business logic for device borrow and return operations.

    Enforces per-user borrow limits (MAX_BORROWS from config), device
    availability checks, ownership rules for returns, and admin
    return-on-behalf capability. All operations are logged to the
    transaction audit trail.
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

        logger.info(
            "%s returned %s (device=%d)",
            user.display_name,
            device.name,
            device_id,
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
