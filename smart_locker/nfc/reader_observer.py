"""
File: reader_observer.py
Description: Reader hardware connect/disconnect monitoring. Implements pyscard's
             ReaderObserver interface to detect when the ACR1252U is plugged in
             or unplugged, posting events to the shared queue for the UI layer.
Project: smart_locker/nfc
Notes: Filters by reader name substring (default "ACR1252") to ignore
       non-target readers connected to the system.
"""

import enum
import logging
import queue
from dataclasses import dataclass, field
from datetime import datetime, timezone

from smartcard.ReaderMonitoring import ReaderObserver

logger = logging.getLogger(__name__)


class ReaderEventType(enum.Enum):
    """Type of hardware event for the NFC reader USB connection."""

    CONNECTED = "connected"
    DISCONNECTED = "disconnected"


@dataclass
class ReaderEvent:
    """Represents an NFC reader hardware connect or disconnect event.

    Posted to the shared event queue when the ACR1252U is plugged in or
    unplugged so the UI can show a reader-status indicator.
    """

    event_type: ReaderEventType
    reader_name: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class LockerReaderObserver(ReaderObserver):
    """Monitors NFC reader USB connect/disconnect events via pyscard.

    Implements the ReaderObserver interface from pyscard's ReaderMonitor.
    Filters events by reader name substring (default "ACR1252") to ignore
    non-target smart card readers that may be connected to the system.
    """

    def __init__(
        self,
        event_queue: queue.Queue,
        reader_filter: str = "ACR1252",
    ) -> None:
        super().__init__()
        self._queue = event_queue
        self._reader_filter = reader_filter

    def update(self, observable, actions) -> None:
        """Handle reader connect/disconnect notifications from pyscard's ReaderMonitor.

        Args:
            observable: The ReaderMonitor instance.
            actions: Tuple of (added_readers, removed_readers) lists.
        """
        added_readers, removed_readers = actions

        for reader in added_readers:
            name = str(reader)
            if self._reader_filter in name:
                event = ReaderEvent(
                    event_type=ReaderEventType.CONNECTED,
                    reader_name=name,
                )
                self._queue.put(event)
                logger.info("Reader connected: %s", name)

        for reader in removed_readers:
            name = str(reader)
            if self._reader_filter in name:
                event = ReaderEvent(
                    event_type=ReaderEventType.DISCONNECTED,
                    reader_name=name,
                )
                self._queue.put(event)
                logger.warning("Reader disconnected: %s", name)
