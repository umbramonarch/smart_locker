"""
File: test_apdu.py
Description: Tests for APDU command construction and response parsing. Validates
             GET_UID, LOAD KEY, AUTHENTICATE, READ BINARY command byte sequences
             and APDUResponse status word interpretation.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_apdu.py -v
"""

from smart_locker.nfc.apdu import (
    APDUResponse,
    GET_UID,
    GET_ATS,
    build_authenticate,
    build_load_key,
    build_read_binary,
)


class TestAPDUCommands:
    """Tests for APDU command byte sequence construction (GET_UID, LOAD KEY, etc.)."""
    def test_get_uid_format(self):
        assert GET_UID == [0xFF, 0xCA, 0x00, 0x00, 0x00]
        assert len(GET_UID) == 5

    def test_get_ats_format(self):
        assert GET_ATS == [0xFF, 0xCA, 0x01, 0x00, 0x00]

    def test_build_load_key_default_slot(self):
        key = [0xFF] * 6
        cmd = build_load_key(key)
        assert cmd == [0xFF, 0x82, 0x00, 0x00, 0x06] + key
        assert len(cmd) == 11

    def test_build_load_key_slot1(self):
        key = [0x00] * 6
        cmd = build_load_key(key, key_slot=1)
        assert cmd[3] == 1

    def test_build_authenticate(self):
        cmd = build_authenticate(block=4)
        assert cmd == [0xFF, 0x86, 0x00, 0x00, 0x05, 0x01, 0x00, 4, 0x60, 0x00]

    def test_build_authenticate_key_b(self):
        cmd = build_authenticate(block=8, key_type=0x61, key_slot=1)
        assert cmd[8] == 0x61  # Key B
        assert cmd[9] == 1    # Slot 1

    def test_build_read_binary(self):
        cmd = build_read_binary(block=4)
        assert cmd == [0xFF, 0xB0, 0x00, 4, 16]

    def test_build_read_binary_custom_length(self):
        cmd = build_read_binary(block=0, length=4)
        assert cmd[4] == 4


class TestAPDUResponse:
    """Tests for APDUResponse parsing, status word checks, and UID formatting."""
    def test_success_response(self):
        resp = APDUResponse.from_raw([0xA1, 0xB2, 0xC3, 0xD4], 0x90, 0x00)
        assert resp.success is True
        assert resp.uid_hex == "A1B2C3D4"
        assert resp.data == bytes([0xA1, 0xB2, 0xC3, 0xD4])

    def test_error_response(self):
        resp = APDUResponse.from_raw([], 0x6A, 0x81)
        assert resp.success is False
        assert resp.status_hex == "6A81"

    def test_uid_hex_uppercase(self):
        resp = APDUResponse.from_raw([0xde, 0xad, 0xbe, 0xef], 0x90, 0x00)
        assert resp.uid_hex == "DEADBEEF"

    def test_empty_data(self):
        resp = APDUResponse.from_raw([], 0x90, 0x00)
        assert resp.success is True
        assert resp.data == b""
        assert resp.uid_hex == ""

    def test_7_byte_uid(self):
        data = [0x04, 0xA1, 0xB2, 0xC3, 0xD4, 0xE5, 0xF6]
        resp = APDUResponse.from_raw(data, 0x90, 0x00)
        assert len(resp.data) == 7
        assert resp.uid_hex == "04A1B2C3D4E5F6"
