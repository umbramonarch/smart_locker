"""
File: test_security.py
Description: Tests for the security module — AES-256-GCM encryption/decryption,
             HMAC-SHA256 hashing, and key generation/validation. Verifies
             round-trip encrypt/decrypt, nonce uniqueness, and HMAC determinism.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_security.py -v
"""

import base64
import os

import pytest

from smart_locker.security.encryption import encrypt, decrypt
from smart_locker.security.hashing import compute_uid_hmac


class TestEncryption:
    """Tests for AES-256-GCM encryption — round-trip, nonce uniqueness, key binding, AAD."""
    def test_encrypt_decrypt_roundtrip(self, enc_key):
        plaintext = "A1B2C3D4E5F6"
        token = encrypt(plaintext, enc_key)
        result = decrypt(token, enc_key)
        assert result == plaintext

    def test_different_nonce_each_time(self, enc_key):
        plaintext = "A1B2C3D4"
        token1 = encrypt(plaintext, enc_key)
        token2 = encrypt(plaintext, enc_key)
        assert token1 != token2  # Random nonce ensures different ciphertext

    def test_decrypt_both_produce_same_plaintext(self, enc_key):
        plaintext = "DEADBEEF"
        token1 = encrypt(plaintext, enc_key)
        token2 = encrypt(plaintext, enc_key)
        assert decrypt(token1, enc_key) == decrypt(token2, enc_key) == plaintext

    def test_wrong_key_fails(self, enc_key):
        plaintext = "A1B2C3D4"
        token = encrypt(plaintext, enc_key)
        wrong_key = b"\xFF" * 32
        with pytest.raises(Exception):  # InvalidTag
            decrypt(token, wrong_key)

    def test_associated_data_binding(self, enc_key):
        plaintext = "A1B2C3D4"
        aad = b"user_id=42"
        token = encrypt(plaintext, enc_key, associated_data=aad)
        # Correct AAD works
        assert decrypt(token, enc_key, associated_data=aad) == plaintext
        # Wrong AAD fails
        with pytest.raises(Exception):
            decrypt(token, enc_key, associated_data=b"user_id=99")

    def test_output_is_base64(self, enc_key):
        token = encrypt("test", enc_key)
        # Should not raise
        raw = base64.b64decode(token)
        assert len(raw) > 12 + 16  # At least nonce + tag + some ciphertext


class TestHashing:
    """Tests for HMAC-SHA256 hashing — determinism, format, key sensitivity, case normalization."""
    def test_hmac_deterministic(self, hmac_key):
        uid = "A1B2C3D4"
        digest1 = compute_uid_hmac(uid, hmac_key)
        digest2 = compute_uid_hmac(uid, hmac_key)
        assert digest1 == digest2

    def test_hmac_hex_output(self, hmac_key):
        digest = compute_uid_hmac("A1B2C3D4", hmac_key)
        assert len(digest) == 64  # SHA-256 hex = 64 chars
        assert all(c in "0123456789abcdef" for c in digest)

    def test_different_uids_different_hmacs(self, hmac_key):
        d1 = compute_uid_hmac("A1B2C3D4", hmac_key)
        d2 = compute_uid_hmac("DEADBEEF", hmac_key)
        assert d1 != d2

    def test_different_keys_different_hmacs(self):
        uid = "A1B2C3D4"
        d1 = compute_uid_hmac(uid, b"\x01" * 32)
        d2 = compute_uid_hmac(uid, b"\x02" * 32)
        assert d1 != d2

    def test_case_normalization(self, hmac_key):
        d1 = compute_uid_hmac("a1b2c3d4", hmac_key)
        d2 = compute_uid_hmac("A1B2C3D4", hmac_key)
        assert d1 == d2  # Both normalized to uppercase
