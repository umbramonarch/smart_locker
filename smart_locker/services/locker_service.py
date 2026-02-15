"""Borrow/return business logic for the Smart Locker system."""

import logging

from sqlalchemy.orm import Session

from smart_locker.auth.session_manager import UserSession
from smart_locker.database.models import DeviceStatus
from smart_locker.database.repositories import DeviceRepository, TransactionRepository

logger = logging.getLogger(__name__)


class LockerService:
    """Handles device borrow and return operations."""

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

        device = DeviceRepository.find_by_id(db_session, device_id)
        if device is None:
            logger.warning("Borrow failed: device %d not found.", device_id)
            return False

        if device.status != DeviceStatus.AVAILABLE:
            logger.warning(
                "Borrow failed: device %d (%s) is %s.",
                device_id,
                device.name,
                device.status.value,
            )
            return False

        user = user_session.user
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
            logger.warning(
                "Return failed: device %d is borrowed by user %d, not %d.",
                device_id,
                device.current_borrower_id,
                user.id,
            )
            return False

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
        return DeviceRepository.get_available_devices(db_session)

    @staticmethod
    def get_user_borrowed_devices(db_session: Session, user_id: int) -> list:
        return DeviceRepository.get_borrowed_by_user(db_session, user_id)
