"""
File: hashing.py
Description: HMAC-SHA256 for deterministic card UID fingerprinting. Produces a
             stable digest used as an indexed database lookup column, unlike
             AES-GCM which is non-deterministic due to random nonces.
Project: smart_locker/security
Notes: The UID is uppercased and stripped before hashing to ensure consistent
       digests regardless of input formatting.
"""

import hmac
import hashlib


def compute_uid_hmac(card_uid_hex: str, hmac_key: bytes) -> str:
    """Compute HMAC-SHA256 of a card UID.

    Args:
        card_uid_hex: Card UID as uppercase hex string (e.g. "A1B2C3D4").
        hmac_key: 32-byte HMAC key.

    Returns:
        Hex-encoded HMAC digest (64 characters).
    """
    uid_normalized = card_uid_hex.upper().strip()
    return hmac.new(
        hmac_key, uid_normalized.encode("utf-8"), hashlib.sha256
    ).hexdigest()
