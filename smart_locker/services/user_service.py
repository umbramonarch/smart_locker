"""
File: user_service.py
Description: User enrollment with encrypted card UID and indexed HMAC lookup.
Project: smart_locker/services
Notes: The encryption and HMAC keys are injected at construction time.
"""

import logging

from sqlalchemy.orm import Session

from smart_locker.database.models import User, UserRole
from smart_locker.database.repositories import DeviceRepository, UserRepository
from smart_locker.security.encryption import encrypt
from smart_locker.security.hashing import compute_uid_hmac

logger = logging.getLogger(__name__)


class LastAdminError(Exception):
    """The change would leave the locker with no active admin."""


class PersonHoldsDeviceError(Exception):
    """The person still holds a borrowed device; deactivation is refused."""


class UserService:
    """Enroll users with encrypted UIDs and an HMAC for card lookup."""

    def __init__(self, enc_key: bytes, hmac_key: bytes) -> None:
        self._enc_key = enc_key
        self._hmac_key = hmac_key

    def enroll_user(
        self,
        db_session: Session,
        display_name: str,
        card_uid_hex: str,
        role: str = "user",
    ) -> User:
        """Enroll a new user with encrypted card UID and HMAC.

        The UID is canonicalised (upper-cased, surrounding whitespace stripped)
        at this boundary so the *stored ciphertext* matches the HMAC fingerprint
        regardless of how the caller formatted it. ``compute_uid_hmac`` already
        normalises before hashing, so without this an oddly-cased UID would still
        authenticate by HMAC yet decrypt to a different-cased string. The reader
        reports uppercase hex already; this makes every enrollment path (reader,
        ``--uid`` CLI, self-registration) consistent.

        Args:
            db_session: Active database session.
            display_name: User's display name.
            card_uid_hex: Raw card UID hex string.
            role: "user" or "admin".

        Returns:
            Created User object.

        Raises:
            ValueError: If the display name is blank or the UID HMAC is
                        already bound to a device sticker.
        """
        display_name = display_name.strip()
        if not display_name:
            raise ValueError("Display name is required.")

        card_uid_hex = card_uid_hex.upper().strip()
        uid_hmac = compute_uid_hmac(card_uid_hex, self._hmac_key)
        if DeviceRepository.find_by_tag_hmac(db_session, uid_hmac) is not None:
            logger.warning("Enrollment rejected: UID is already a device tag.")
            raise ValueError("This tag is already bound to a device.")
        encrypted_uid = encrypt(card_uid_hex, self._enc_key)

        user = UserRepository.create(
            db_session,
            display_name=display_name,
            uid_hmac=uid_hmac,
            encrypted_card_uid=encrypted_uid,
            role=role,
        )
        logger.info("Enrolled user: %s (role=%s)", display_name, role)
        return user

    def replace_card(
        self,
        db_session: Session,
        user: User,
        card_uid_hex: str,
    ) -> User:
        """Rebind ``user``'s work card to a new UID.

        The new UID must not be a device sticker or another person's card;
        the caller (the tap policy) already refused a UID enrolled to a
        different user. Replaces ``uid_hmac`` and the encrypted copy.

        Args:
            db_session: Active database session.
            user: The person receiving the new card.
            card_uid_hex: Raw card UID hex string.

        Returns:
            The same User row with the new card credentials.

        Raises:
            ValueError: If the UID is bound to a device or another user.
        """
        card_uid_hex = card_uid_hex.upper().strip()
        uid_hmac = compute_uid_hmac(card_uid_hex, self._hmac_key)
        if DeviceRepository.find_by_tag_hmac(db_session, uid_hmac) is not None:
            raise ValueError("This tag is already bound to a device.")
        holder = UserRepository.find_by_uid_hmac(db_session, uid_hmac)
        if holder is not None and holder.id != user.id:
            raise ValueError("This card is already registered to someone else.")

        user.uid_hmac = uid_hmac
        user.encrypted_card_uid = encrypt(card_uid_hex, self._enc_key)
        db_session.flush()
        logger.info("Replaced card for user %s (id=%d)", user.display_name, user.id)
        return user


def update_person(
    db_session: Session,
    user: User,
    *,
    role: str | None = None,
    is_active: bool | None = None,
    actor: str | None = None,
) -> User:
    """Apply People-screen edits to one user row.

    The role accepts "admin"/"user" (surrounding whitespace is stripped, like
    the add-person path). Demoting or deactivating the last active admin is
    refused — the locker must never strand itself without an admin — and
    deactivation is refused while the person holds a borrowed device. Both
    checks read the database at write time; the caller serializes the
    check+write pair so two racing edits cannot both pass the count.

    Args:
        db_session: Active database session.
        user: User row to edit.
        role: New role, or None to leave unchanged.
        is_active: New active flag, or None to leave unchanged.
        actor: Who made the edit (kiosk admin name or "dashboard") — appended
            to the audit log line when given.

    Returns:
        The updated User row (flushed, not committed).

    Raises:
        ValueError: Unknown role value or a no-op call.
        LastAdminError: The edit removes the last active admin.
        PersonHoldsDeviceError: Deactivation while a device is borrowed.
    """
    if role is None and is_active is None:
        raise ValueError("Nothing to update.")

    if role is not None:
        try:
            new_role = UserRole(str(role).strip())
        except ValueError as exc:
            raise ValueError("Role must be 'user' or 'admin'.") from exc
    else:
        new_role = user.role

    stays_active = user.is_active if is_active is None else is_active
    loses_admin = (
        user.role == UserRole.ADMIN
        and (new_role != UserRole.ADMIN or not stays_active)
    )
    if (
        loses_admin
        and user.is_active
        and UserRepository.count_active_admins(db_session) <= 1
    ):
        raise LastAdminError(
            "Cannot remove the last active admin — enroll another admin first."
        )

    if is_active is False and user.is_active:
        held = DeviceRepository.count_borrowed_by_user(db_session, user.id)
        if held:
            raise PersonHoldsDeviceError(
                f"{user.display_name} still holds {held} borrowed device(s)."
            )

    if role is not None:
        user.role = new_role
    if is_active is not None:
        user.is_active = is_active
    db_session.flush()
    log_msg = "Updated person %s (id=%d): role=%s active=%s"
    log_args: tuple = (
        user.display_name,
        user.id,
        user.role.value,
        user.is_active,
    )
    if actor:
        log_msg += " by %s"
        log_args += (actor,)
    logger.info(log_msg, *log_args)
    return user


def person_record(user: User) -> dict:
    """Shared People-list record for the kiosk and dashboard gates.

    Args:
        user: User row.

    Returns:
        dict: id, display_name, role, is_active, registered_at. Card
            credentials are never included.
    """
    return {
        "id": user.id,
        "display_name": user.display_name,
        "role": user.role.value,
        "is_active": user.is_active,
        "registered_at": (
            user.created_at.strftime("%Y-%m-%d %H:%M:%S")
            if user.created_at
            else None
        ),
    }
