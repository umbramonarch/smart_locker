"""High-level NFCReader class.

Wires together CardMonitor, ReaderMonitor, and observers.
Provides blocking and non-blocking interfaces for the main application.
"""

import logging
import queue
from typing import Union

from smartcard.CardMonitoring import CardMonitor
from smartcard.Exceptions import ListReadersException
from smartcard.ReaderMonitoring import ReaderMonitor
from smartcard.System import readers as list_readers

from config.settings import READER_NAME_FILTER
from smart_locker.nfc.apdu import (
    APDUResponse,
    GET_FIRMWARE_VERSION,
    DISABLE_BUZZER,
    build_authenticate,
    build_load_key,
    build_read_binary,
)
from smart_locker.nfc.card_observer import CardEvent, LockerCardObserver
from smart_locker.nfc.exceptions import (
    CardReadError,
    PCSCServiceError,
    ReaderNotFoundError,
)
from smart_locker.nfc.reader_observer import (
    LockerReaderObserver,
    ReaderEvent,
)

logger = logging.getLogger(__name__)

Event = Union[CardEvent, ReaderEvent]


class NFCReader:
    """High-level NFC reader interface with event-driven card detection."""

    def __init__(self, reader_filter: str | None = None) -> None:
        self._reader_filter = reader_filter or READER_NAME_FILTER
        self._event_queue: queue.Queue[Event] = queue.Queue()
        self._card_monitor: CardMonitor | None = None
        self._reader_monitor: ReaderMonitor | None = None
        self._card_observer: LockerCardObserver | None = None
        self._reader_observer: LockerReaderObserver | None = None
        self._running = False

    def start(self) -> str:
        """Start monitoring for cards and reader events.

        Returns:
            Name of the detected reader.

        Raises:
            PCSCServiceError: If the PC/SC service is not running.
            ReaderNotFoundError: If no matching reader is found.
        """
        # Check for reader
        try:
            available = list_readers()
        except ListReadersException as e:
            raise PCSCServiceError(
                "PC/SC service not running. Start the 'Smart Card' Windows service."
            ) from e

        target = None
        for r in available:
            if self._reader_filter in str(r):
                target = r
                break

        if target is None:
            reader_names = [str(r) for r in available] if available else ["(none)"]
            raise ReaderNotFoundError(
                f"No reader matching '{self._reader_filter}' found. "
                f"Available: {', '.join(reader_names)}"
            )

        reader_name = str(target)
        logger.info("Found NFC reader: %s", reader_name)

        # Start card monitoring
        self._card_observer = LockerCardObserver(
            self._event_queue, self._reader_filter
        )
        self._card_monitor = CardMonitor()
        self._card_monitor.addObserver(self._card_observer)

        # Start reader monitoring (detect unplug/replug)
        self._reader_observer = LockerReaderObserver(
            self._event_queue, self._reader_filter
        )
        self._reader_monitor = ReaderMonitor()
        self._reader_monitor.addObserver(self._reader_observer)

        self._running = True
        logger.info("NFC monitoring started.")
        return reader_name

    def stop(self) -> None:
        """Stop all monitoring and clean up."""
        if self._card_monitor and self._card_observer:
            self._card_monitor.deleteObserver(self._card_observer)
        if self._reader_monitor and self._reader_observer:
            self._reader_monitor.deleteObserver(self._reader_observer)

        self._card_monitor = None
        self._reader_monitor = None
        self._card_observer = None
        self._reader_observer = None
        self._running = False
        logger.info("NFC monitoring stopped.")

    def wait_for_event(self, timeout: float | None = None) -> Event | None:
        """Block until an event arrives or timeout expires.

        Args:
            timeout: Seconds to wait. None = block forever.

        Returns:
            CardEvent or ReaderEvent, or None on timeout.
        """
        try:
            return self._event_queue.get(block=True, timeout=timeout)
        except queue.Empty:
            return None

    def poll_event(self) -> Event | None:
        """Non-blocking check for a pending event."""
        try:
            return self._event_queue.get_nowait()
        except queue.Empty:
            return None

    @property
    def is_running(self) -> bool:
        return self._running

    @staticmethod
    def read_mifare_block(connection, block: int, key: list[int] | None = None) -> bytes:
        """Read a MIFARE Classic block (for future card data reading).

        Args:
            connection: An active pyscard connection.
            block: Block number to read.
            key: 6-byte MIFARE key. Defaults to factory key [0xFF]*6.

        Returns:
            16 bytes of block data.

        Raises:
            CardReadError: If authentication or read fails.
        """
        if key is None:
            key = [0xFF] * 6

        # Load key into reader slot 0
        resp, sw1, sw2 = connection.transmit(build_load_key(key))
        apdu = APDUResponse.from_raw(resp, sw1, sw2)
        if not apdu.success:
            raise CardReadError(f"LOAD KEY failed: {apdu}")

        # Authenticate
        resp, sw1, sw2 = connection.transmit(build_authenticate(block))
        apdu = APDUResponse.from_raw(resp, sw1, sw2)
        if not apdu.success:
            raise CardReadError(f"AUTHENTICATE failed for block {block}: {apdu}")

        # Read
        resp, sw1, sw2 = connection.transmit(build_read_binary(block))
        apdu = APDUResponse.from_raw(resp, sw1, sw2)
        if not apdu.success:
            raise CardReadError(f"READ BINARY failed for block {block}: {apdu}")

        return apdu.data
