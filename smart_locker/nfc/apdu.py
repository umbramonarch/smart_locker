"""APDU command constants and response parsing for ACR1252U NFC reader.

All commands use the PC/SC pseudo-APDU interface (CLA=0xFF).
GET_UID works with all card types: MIFARE Classic, Ultralight, NTAG, DESFire, etc.
"""

from dataclasses import dataclass

# ── Universal Commands ────────────────────────────────────────────────────

# Get card UID — works with any card type
GET_UID = [0xFF, 0xCA, 0x00, 0x00, 0x00]

# Get ATS (Answer To Select) — card type identification
GET_ATS = [0xFF, 0xCA, 0x01, 0x00, 0x00]

# ── ACR1252U Reader Control ──────────────────────────────────────────────

# Buzzer control (ACR1252U-specific, via pseudo-APDU escape)
DISABLE_BUZZER = [0xFF, 0x00, 0x52, 0x00, 0x00]
ENABLE_BUZZER = [0xFF, 0x00, 0x52, 0xFF, 0x00]

# Get firmware version
GET_FIRMWARE_VERSION = [0xFF, 0x00, 0x48, 0x00, 0x00]

# ── MIFARE Classic Commands (for future card-data access) ────────────────


def build_load_key(key_bytes: list[int], key_slot: int = 0) -> list[int]:
    """Build LOAD KEY command to store a MIFARE key in reader volatile memory.

    Args:
        key_bytes: 6-byte MIFARE key (e.g. [0xFF]*6 for factory default).
        key_slot: Key slot 0x00 or 0x01 in reader memory.
    """
    return [0xFF, 0x82, 0x00, key_slot, 0x06] + key_bytes


def build_authenticate(block: int, key_type: int = 0x60, key_slot: int = 0) -> list[int]:
    """Build AUTHENTICATE command for a MIFARE Classic block.

    Args:
        block: Block number (0-255).
        key_type: 0x60 for Key A, 0x61 for Key B.
        key_slot: Key slot used in LOAD KEY.
    """
    return [0xFF, 0x86, 0x00, 0x00, 0x05, 0x01, 0x00, block, key_type, key_slot]


def build_read_binary(block: int, length: int = 16) -> list[int]:
    """Build READ BINARY command for a MIFARE Classic block.

    Args:
        block: Block number to read.
        length: Bytes to read (16 for MIFARE Classic).
    """
    return [0xFF, 0xB0, 0x00, block, length]


# ── Status Word Constants ────────────────────────────────────────────────

SW_SUCCESS = (0x90, 0x00)
SW_WRONG_LENGTH = (0x6C, None)  # SW2 contains correct length
SW_FUNCTION_NOT_SUPPORTED = (0x6A, 0x81)


@dataclass
class APDUResponse:
    """Parsed APDU response with data and status words."""

    data: bytes
    sw1: int
    sw2: int

    @classmethod
    def from_raw(cls, response: list[int], sw1: int, sw2: int) -> "APDUResponse":
        return cls(data=bytes(response), sw1=sw1, sw2=sw2)

    @property
    def success(self) -> bool:
        return self.sw1 == 0x90 and self.sw2 == 0x00

    @property
    def uid_hex(self) -> str:
        """Return data as uppercase hex string (for UID responses)."""
        return self.data.hex().upper()

    @property
    def status_hex(self) -> str:
        return f"{self.sw1:02X}{self.sw2:02X}"

    def __repr__(self) -> str:
        return (
            f"APDUResponse(data={self.data.hex().upper()}, "
            f"sw={self.status_hex}, success={self.success})"
        )
