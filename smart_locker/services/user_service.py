"""User management service — public vs admin views, enrollment."""

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from smart_locker.database.models import User, UserRole
from smart_locker.database.repositories import UserRepository
from smart_locker.security.encryption import encrypt, decrypt
from smart_locker.security.hashing import compute_uid_hmac

logger = logging.getLogger(__name__)


@dataclass
class PublicUserInfo:
    """Public-facing user info (no card data)."""

    id: int
    display_name: str
    role: str


@dataclass
class AdminUserInfo:
    """Admin-only user info with decrypted card UID."""

    id: int
    display_name: str
    role: str
    card_uid: str  # Decrypted raw UID hex


class UserService:
    """User management with separate public and admin views."""

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

        Args:
            db_session: Active database session.
            display_name: User's display name.
            card_uid_hex: Raw card UID hex string.
            role: "user" or "admin".

        Returns:
            Created User object.
        """
        uid_hmac = compute_uid_hmac(card_uid_hex, self._hmac_key)
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

    @staticmethod
    def get_public_user_info(db_session: Session, user_id: int) -> PublicUserInfo | None:
        """Get public user info (no card data)."""
        user = UserRepository.find_by_id(db_session, user_id)
        if user is None:
            return None
        return PublicUserInfo(
            id=user.id,
            display_name=user.display_name,
            role=user.role.value,
        )

    def get_admin_user_info(
        self,
        db_session: Session,
        user_id: int,
        requesting_user: User,
    ) -> AdminUserInfo | None:
        """Get admin user info with decrypted card UID.

        Args:
            db_session: Active database session.
            user_id: ID of the user to look up.
            requesting_user: The user making the request (must be admin).

        Returns:
            AdminUserInfo with decrypted card UID, or None.
        """
        if requesting_user.role != UserRole.ADMIN:
            logger.warning(
                "Non-admin user %s (id=%d) attempted admin info access.",
                requesting_user.display_name,
                requesting_user.id,
            )
            return None

        user = UserRepository.find_by_id(db_session, user_id)
        if user is None:
            return None

        decrypted_uid = decrypt(user.encrypted_card_uid, self._enc_key)

        return AdminUserInfo(
            id=user.id,
            display_name=user.display_name,
            role=user.role.value,
            card_uid=decrypted_uid,
        )
