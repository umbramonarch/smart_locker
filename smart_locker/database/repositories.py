"""Data access layer — repository pattern for database CRUD operations."""

import logging
from datetime import datetime, timezone

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
    """CRUD operations for users."""

    @staticmethod
    def find_by_uid_hmac(session: Session, uid_hmac: str) -> User | None:
        """O(1) indexed lookup by HMAC of card UID."""
        stmt = select(User).where(User.uid_hmac == uid_hmac)
        return session.execute(stmt).scalar_one_or_none()

    @staticmethod
    def find_by_id(session: Session, user_id: int) -> User | None:
        return session.get(User, user_id)

    @staticmethod
    def create(
        session: Session,
        display_name: str,
        uid_hmac: str,
        encrypted_card_uid: str,
        role: str = "user",
    ) -> User:
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
        stmt = select(User).order_by(User.display_name)
        return list(session.execute(stmt).scalars().all())


class DeviceRepository:
    """CRUD operations for devices."""

    @staticmethod
    def find_by_id(session: Session, device_id: int) -> Device | None:
        return session.get(Device, device_id)

    @staticmethod
    def get_available_devices(session: Session) -> list[Device]:
        stmt = select(Device).where(Device.status == DeviceStatus.AVAILABLE)
        return list(session.execute(stmt).scalars().all())

    @staticmethod
    def get_borrowed_by_user(session: Session, user_id: int) -> list[Device]:
        stmt = select(Device).where(
            Device.status == DeviceStatus.BORROWED,
            Device.current_borrower_id == user_id,
        )
        return list(session.execute(stmt).scalars().all())

    @staticmethod
    def borrow(session: Session, device: Device, user_id: int) -> None:
        device.status = DeviceStatus.BORROWED
        device.current_borrower_id = user_id
        session.flush()

    @staticmethod
    def return_device(session: Session, device: Device) -> None:
        device.status = DeviceStatus.AVAILABLE
        device.current_borrower_id = None
        session.flush()

    @staticmethod
    def count_borrowed_by_user(session: Session, user_id: int) -> int:
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
        serial_number: str,
        locker_slot: int | None = None,
        description: str | None = None,
        image_path: str | None = None,
    ) -> Device:
        device = Device(
            name=name,
            device_type=device_type,
            serial_number=serial_number,
            locker_slot=locker_slot,
            description=description,
            image_path=image_path,
        )
        session.add(device)
        session.flush()
        logger.info("Created device: %s (id=%d)", name, device.id)
        return device

    @staticmethod
    def list_all(session: Session) -> list[Device]:
        stmt = select(Device).order_by(Device.name)
        return list(session.execute(stmt).scalars().all())


class TransactionRepository:
    """CRUD operations for transaction logs."""

    @staticmethod
    def log_borrow(
        session: Session,
        user_id: int,
        device_id: int,
        notes: str | None = None,
    ) -> TransactionLog:
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
        stmt = (
            select(TransactionLog)
            .where(TransactionLog.device_id == device_id)
            .order_by(TransactionLog.timestamp.desc())
        )
        return list(session.execute(stmt).scalars().all())
