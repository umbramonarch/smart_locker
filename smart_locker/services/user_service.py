"""
File: user_service.py
Description: User enrollment with encrypted card UID and indexed HMAC lookup.
Project: smart_locker/services
Notes: The encryption and HMAC keys are injected at construction time.
"""

import logging

from sqlalchemy.orm import Session

from smart_locker.database.models import User
from smart_locker.database.repositories import DeviceRepository, UserRepository
from smart_locker.security.encryption import encrypt
from smart_locker.security.hashing import compute_uid_hmac

logger = logging.getLogger(__name__)


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
            ValueError: If the UID HMAC is already bound to a device sticker.
        """
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
