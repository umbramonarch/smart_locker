"""
File: models.py
Description: SQLAlchemy 2.0 ORM models for the Smart Locker system. Defines
             User, Device, and TransactionLog tables with their relationships,
             enums (UserRole, DeviceStatus, TransactionType), and indexes.
Project: smart_locker/database
Notes: Users store an HMAC digest (indexed for O(1) lookup) and an AES-GCM
       encrypted card UID. Devices use pm_number as the unique business key.
"""

import enum
from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """SQLAlchemy declarative base for all ORM models in the Smart Locker system."""

    pass


class UserRole(enum.Enum):
    """User authorization level — determines access to admin-only features
    such as returning devices on behalf of other users and viewing decrypted
    card UIDs.
    """

    USER = "user"
    ADMIN = "admin"


class DeviceStatus(enum.Enum):
    """Current lifecycle state of a device in the locker system. Controls
    whether a device can be borrowed, returned, or is temporarily out of
    service for maintenance.
    """

    AVAILABLE = "available"
    BORROWED = "borrowed"
    MAINTENANCE = "maintenance"


class TransactionType(enum.Enum):
    """Type of transaction recorded in the audit log. Every borrow and return
    operation creates a corresponding TransactionLog entry.
    """

    BORROW = "borrow"
    RETURN = "return"


def _utcnow() -> datetime:
    """Return the current UTC time as a timezone-aware datetime.

    Used as the default factory for all timestamp columns so that every
    row records when it was created or last modified in a consistent timezone.
    """
    return datetime.now(timezone.utc)


class User(Base):
    """Registered kiosk user with encrypted NFC card credentials.

    Each user stores an HMAC-SHA256 digest of their card UID (for fast indexed
    lookups during authentication) and an AES-256-GCM encrypted copy of the
    raw UID (accessible only to admins). The ``is_active`` flag allows soft
    deactivation without deleting transaction history.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole), nullable=False, default=UserRole.USER
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # HMAC-SHA256 of card UID — indexed for O(1) lookup
    uid_hmac: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    # AES-256-GCM encrypted card UID — admin-only access
    encrypted_card_uid: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    # Relationships
    borrowed_devices: Mapped[list["Device"]] = relationship(
        back_populates="current_borrower"
    )
    transactions: Mapped[list["TransactionLog"]] = relationship(
        back_populates="user", foreign_keys="[TransactionLog.user_id]"
    )

    __table_args__ = (Index("ix_users_uid_hmac", "uid_hmac"),)


class Registrant(Base):
    """Approved name for self-service NFC card registration.

    Stores unique person names extracted from the company's source Excel file
    (specifically the "Aktueller Einsatzort" / current deployment location
    column). During source import, every non-schrank value in that column is
    treated as a person's name and added to this table.

    The kiosk registration screen presents these names as a selectable list.
    Users whose names appear here may self-register by selecting their name
    and tapping an NFC card. Users whose names do NOT appear must request
    manual registration from an admin (available only in the hidden admin
    panel). This acts as a lightweight authentication gate — only employees
    listed in the company device master sheet can self-register.

    Names are deduplicated (unique constraint) and only grow over successive
    imports — an import never removes existing registrant rows.
    """

    __tablename__ = "registrants"

    # Auto-incrementing primary key
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Person's name as it appears in the Excel "Aktueller Einsatzort" column.
    # Unique constraint prevents duplicate entries across successive imports.
    display_name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)

    # Timestamp when this registrant name was first imported
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class Device(Base):
    """Physical device stored in the locker system.

    Identified by ``pm_number`` (the company equipment/PM number — unique business
    key). Tracks inventory metadata (manufacturer, model, serial, barcode),
    locker placement, calibration schedule, and current borrow state. The
    ``status`` field controls availability; ``current_borrower_id`` links to
    the User who currently holds the device.
    """

    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pm_number: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    device_type: Mapped[str] = mapped_column(String(50), nullable=False)
    serial_number: Mapped[str | None] = mapped_column(
        String(100), nullable=True, unique=True
    )
    manufacturer: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    barcode: Mapped[str | None] = mapped_column(String(100), nullable=True)
    locker_slot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    calibration_due: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[DeviceStatus] = mapped_column(
        Enum(DeviceStatus), nullable=False, default=DeviceStatus.AVAILABLE
    )
    current_borrower_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    # Relationships
    current_borrower: Mapped[User | None] = relationship(
        back_populates="borrowed_devices"
    )
    transactions: Mapped[list["TransactionLog"]] = relationship(
        back_populates="device"
    )


class TransactionLog(Base):
    """Immutable audit record for every borrow and return operation.

    Links a user and device to a transaction type with a UTC timestamp. For
    admin-initiated returns (returning a device on behalf of another user),
    ``performed_by_id`` records the admin's user ID while ``user_id`` remains
    the original borrower.
    """

    __tablename__ = "transaction_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False
    )
    device_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("devices.id"), nullable=False
    )
    transaction_type: Mapped[TransactionType] = mapped_column(
        Enum(TransactionType), nullable=False
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # For admin returns: the admin who performed the return on behalf of the borrower
    performed_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True
    )

    # Relationships
    user: Mapped[User] = relationship(
        back_populates="transactions", foreign_keys=[user_id]
    )
    performed_by: Mapped[User | None] = relationship(foreign_keys=[performed_by_id])
    device: Mapped[Device] = relationship(back_populates="transactions")
