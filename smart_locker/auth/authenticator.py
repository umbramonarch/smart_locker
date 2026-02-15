"""Card UID to user lookup via HMAC.

Flow:
1. Receive raw UID hex string from NFC reader
2. Compute HMAC-SHA256(uid, hmac_key) → deterministic digest
3. Query UserRepository.find_by_uid_hmac(digest) — indexed O(1) lookup
4. Check user.is_active
5. Return User or None
"""

import logging

from sqlalchemy.orm import Session

from smart_locker.database.models import User
from smart_locker.database.repositories import UserRepository
from smart_locker.security.hashing import compute_uid_hmac

logger = logging.getLogger(__name__)


class Authenticator:
    """Authenticates users by their NFC card UID."""

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
