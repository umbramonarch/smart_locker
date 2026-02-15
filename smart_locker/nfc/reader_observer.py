"""Reader hardware connect/disconnect monitoring.

Implements pyscard's ReaderObserver interface to detect when the ACR1252U
is plugged in or unplugged, so the UI can warn the user.
"""

import enum
import logging
import queue
from dataclasses import dataclass, field
from datetime import datetime, timezone

from smartcard.ReaderMonitoring import ReaderObserver

logger = logging.getLogger(__name__)


class ReaderEventType(enum.Enum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"


@dataclass
class ReaderEvent:
    event_type: ReaderEventType
    reader_name: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class LockerReaderObserver(ReaderObserver):
    """Detects ACR1252U USB connect/disconnect events."""

    def __init__(
        self,
        event_queue: queue.Queue,
        reader_filter: str = "ACR1252",
    ) -> None:
        super().__init__()
        self._queue = event_queue
        self._reader_filter = reader_filter

    def update(self, observable, actions) -> None:
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
