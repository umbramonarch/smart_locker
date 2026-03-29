"""
File: authenticator.py
Description: Card UID to user lookup via HMAC-SHA256. Receives a raw UID hex
             string from the NFC reader, computes a deterministic HMAC digest,
             performs an indexed O(1) database lookup, and checks user status.
Project: smart_locker/auth
Notes: The HMAC key is injected at construction time from key_manager. The raw
       card UID is never logged or stored in plaintext.
"""

import logging

from sqlalchemy.orm import Session

from smart_locker.database.models import User
from smart_locker.database.repositories import UserRepository
from smart_locker.security.hashing import compute_uid_hmac

logger = logging.getLogger(__name__)


class Authenticator:
    """Authenticates kiosk users by their NFC card UID.

    Computes a deterministic HMAC-SHA256 digest of the raw UID hex string
    and performs an indexed O(1) database lookup on the ``users.uid_hmac``
    column. No decryption is needed during authentication — only the HMAC
    key is required.
    """

    def __init__(self, hmac_key: bytes) -> None:
        self._hmac_key = hmac_key

    def authenticate(self, session: Session, card_uid_hex: str) -> User | None:
        """Look up and validate a user by card UID.

        Args:
            session: Active database session.
            card_uid_hex: Card UID as hex string (e.g. "A1B2C3D4").

        Returns:
            User object if authenticated, None if not found or inactive.
        """
        uid_hmac = compute_uid_hmac(card_uid_hex, self._hmac_key)
        user = UserRepository.find_by_uid_hmac(session, uid_hmac)

        if user is None:
            logger.warning("Unknown card: HMAC=%s...", uid_hmac[:16])
            return None

        if not user.is_active:
            logger.warning(
                "Inactive user attempted auth: %s (id=%d)",
                user.display_name,
                user.id,
            )
            return None

        logger.info("Authenticated: %s (id=%d)", user.display_name, user.id)
        return user
