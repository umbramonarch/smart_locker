"""
File: encryption.py
Description: AES-256-GCM encryption and decryption for card UIDs. Each encrypt
             call generates a random 12-byte nonce, so the same plaintext
             produces different ciphertext every time.
Project: smart_locker/security
Notes: Storage format is base64(nonce || ciphertext || GCM tag). Only admins
       should call decrypt — regular auth uses HMAC lookup instead.
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


def decrypt(token: str, key: bytes, associated_data: bytes | None = None) -> str:
    """Decrypt a base64 AES-256-GCM token back to plaintext.

    Args:
        token: Base64-encoded string produced by encrypt().
        key: 32-byte encryption key (must match).
        associated_data: Must match what was used during encrypt().

    Returns:
        Decrypted UTF-8 string.

    Raises:
        cryptography.exceptions.InvalidTag: If key, AAD, or data is wrong.
    """
    raw = base64.b64decode(token)
    nonce = raw[:12]
    ct = raw[12:]
    aesgcm = AESGCM(key)
    plaintext_bytes = aesgcm.decrypt(nonce, ct, associated_data)
    return plaintext_bytes.decode("utf-8")
