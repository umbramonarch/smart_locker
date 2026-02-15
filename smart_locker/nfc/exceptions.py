"""NFC-specific exceptions."""


class NFCError(Exception):
    """Base exception for NFC operations."""


class ReaderNotFoundError(NFCError):
    """No matching NFC reader found in the system."""


class ReaderDisconnectedError(NFCError):
    """Reader was disconnected during an operation."""


class CardReadError(NFCError):
    """Failed to read data from a card."""


class PCSCServiceError(NFCError):
    """Windows Smart Card (PC/SC) service is not running."""
