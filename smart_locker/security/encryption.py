"""
File: encryption.py
Description: AES-256-GCM encryption for card UIDs. Each encrypt call generates
             a random 12-byte nonce, so the same plaintext produces different
             ciphertext every time.
Project: smart_locker/security
Notes: Storage format is base64(nonce || ciphertext || GCM tag). Authentication
       uses HMAC lookup instead of reading encrypted UIDs.
"""

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def encrypt(plaintext: str, key: bytes, associated_data: bytes | None = None) -> str:
    """Encrypt plaintext with AES-256-GCM, returning a base64 token.

    Args:
        plaintext: UTF-8 string to encrypt.
        key: 32-byte encryption key.
        associated_data: Optional AAD bound to the ciphertext.

    Returns:
        Base64-encoded string: nonce || ciphertext || tag.
    """
    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), associated_data)
    # ct already contains ciphertext + 16-byte tag (appended by cryptography lib)
    return base64.b64encode(nonce + ct).decode("ascii")
