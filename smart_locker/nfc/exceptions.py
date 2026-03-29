"""
File: exceptions.py
Description: NFC-specific exception hierarchy. Provides typed errors for reader
             discovery, card reading, and PC/SC service issues so callers can
             handle each failure mode distinctly.
Project: smart_locker/nfc
Notes: All exceptions inherit from NFCError for broad except clauses.
"""


class NFCError(Exception):
    """Base exception for all NFC-related errors.

    All NFC exceptions inherit from this class, allowing callers to catch
    ``NFCError`` for broad error handling while still distinguishing specific
    failure modes via subclasses.
    """


class ReaderNotFoundError(NFCError):
    """No NFC reader matching the configured filter was found.

    Raised during reader initialization when no connected reader's name
    contains the expected substring (default "ACR1252").
    """


class ReaderDisconnectedError(NFCError):
    """The NFC reader was physically disconnected during an operation.

    Raised when a USB disconnect is detected while a card read or other
    APDU command is in progress.
    """


class CardReadError(NFCError):
    """Failed to read data from a card via APDU command.

    Raised when authentication, UID read, or MIFARE block read returns
    a non-success status word from the card.
    """


class PCSCServiceError(NFCError):
    """The Windows Smart Card (PC/SC) service is not running.

    Raised when pyscard cannot enumerate readers because the underlying
    Windows service is stopped. Fix: start the 'Smart Card' service.
    """
