"""
File: key_manager.py
Description: Key management — loads and caches encryption and HMAC keys from
             environment variables. Two separate 32-byte keys are used:
             SMART_LOCKER_ENC_KEY (AES-256-GCM) and SMART_LOCKER_HMAC_KEY
             (HMAC-SHA256), limiting damage if one key is compromised.
Project: smart_locker/security
Notes: Keys are lazily loaded on first access. Generate keys with
       'python -m scripts.generate_key' and store them in .env.
"""

import base64
import logging
import os

from config.settings import ENC_KEY_ENV_VAR, HMAC_KEY_ENV_VAR

logger = logging.getLogger(__name__)


class KeyManager:
    """Loads, validates, and caches cryptographic keys from environment variables.

    Two separate 32-byte keys are managed: one for AES-256-GCM encryption
    and one for HMAC-SHA256 hashing. Keys are lazily loaded on first access
    and cached for subsequent calls, so the environment is read at most once
    per key per process lifetime.
    """

    def __init__(self) -> None:
        self._enc_key: bytes | None = None
        self._hmac_key: bytes | None = None

    @staticmethod
    def _load_key(env_var: str) -> bytes:
        """Read a base64-encoded 32-byte key from an environment variable.

        Args:
            env_var: Name of the environment variable to read.

        Returns:
            Decoded 32-byte key.

        Raises:
            EnvironmentError: If the variable is missing.
            ValueError: If the decoded key is not exactly 32 bytes.
        """
        raw = os.getenv(env_var)
        if not raw:
            raise EnvironmentError(
                f"Missing environment variable '{env_var}'. "
                f"Generate keys with: python -m scripts.generate_key"
            )
        key = base64.b64decode(raw)
        if len(key) != 32:
            raise ValueError(
                f"Key from '{env_var}' must be exactly 32 bytes, got {len(key)}."
            )
        return key

    @property
    def enc_key(self) -> bytes:
        """The 32-byte AES-256-GCM encryption key, lazily loaded from environment."""
        if self._enc_key is None:
            self._enc_key = self._load_key(ENC_KEY_ENV_VAR)
            logger.info("Encryption key loaded.")
        return self._enc_key

    @property
    def hmac_key(self) -> bytes:
        """The 32-byte HMAC-SHA256 key, lazily loaded from environment."""
        if self._hmac_key is None:
            self._hmac_key = self._load_key(HMAC_KEY_ENV_VAR)
            logger.info("HMAC key loaded.")
        return self._hmac_key


# Module-level singleton
key_manager = KeyManager()
