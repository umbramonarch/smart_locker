"""Event-driven card insert/remove detection.

Implements pyscard's CardObserver interface. When a card is inserted,
reads the UID via a fast APDU command and posts a CardEvent to a queue.
The main application thread consumes events from the queue.
"""

import enum
import logging
import queue
from dataclasses import dataclass, field
from datetime import datetime, timezone

from smartcard.CardMonitoring import CardObserver
from smartcard.Exceptions import CardConnectionException
from smartcard.util import toHexString

from smart_locker.nfc.apdu import GET_UID, APDUResponse

logger = logging.getLogger(__name__)


def _mask_uid(uid: str | None) -> str:
    """Mask a UID for safe logging — show first 2 and last 2 chars only."""
    if uid is None:
        return "(none)"
    if len(uid) <= 4:
        return "****"
    return uid[:2] + "*" * (len(uid) - 4) + uid[-2:]


class CardEventType(enum.Enum):
    INSERTED = "inserted"
    REMOVED = "removed"


@dataclass
class CardEvent:
    """Represents a card insert or remove event."""

    event_type: CardEventType
    uid: str | None = None  # Hex string, None if read failed
    reader_name: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class LockerCardObserver(CardObserver):
    """Observes card insert/remove events from pyscard's CardMonitor.

    On insert: connects to card, reads UID, posts CardEvent to queue.
    On remove: posts removal event (UID recalled from last insert).

    Filters by reader name substring to ignore non-target readers.
    """

    def __init__(
        self,
        event_queue: queue.Queue[CardEvent],
        reader_filter: str = "ACR1252",
    ) -> None:
        super().__init__()
        self._queue = event_queue
        self._reader_filter = reader_filter
        self._last_uid: str | None = None

    def update(self, observable, actions) -> None:
        added_cards, removed_cards = actions

        for card in added_cards:
            reader_name = str(card.reader)
            if self._reader_filter not in reader_name:
                logger.debug("Ignoring card on non-target reader: %s", reader_name)
                continue

            uid = self._read_uid(card)
            self._last_uid = uid

            event = CardEvent(
                event_type=CardEventType.INSERTED,
                uid=uid,
                reader_name=reader_name,
            )
            self._queue.put(event)

            if uid:
                logger.info("Card inserted: UID=%s on %s", _mask_uid(uid), reader_name)
            else:
                logger.warning("Card inserted but UID read failed on %s", reader_name)

            # Process only the first card if multiple detected
            if len(added_cards) > 1:
                logger.warning(
                    "Multiple cards detected (%d), processing first only.",
                    len(added_cards),
                )
                break

        for card in removed_cards:
            reader_name = str(card.reader)
            if self._reader_filter not in reader_name:
                continue

            event = CardEvent(
                event_type=CardEventType.REMOVED,
                uid=self._last_uid,
                reader_name=reader_name,
            )
            self._queue.put(event)
            logger.info("Card removed: UID=%s from %s", _mask_uid(self._last_uid), reader_name)
            self._last_uid = None

    @staticmethod
    def _read_uid(card) -> str | None:
        """Connect to card and read UID via APDU. Returns hex string or None."""
        connection = None
        try:
            connection = card.createConnection()
            connection.connect()
            response, sw1, sw2 = connection.transmit(GET_UID)
            apdu = APDUResponse.from_raw(response, sw1, sw2)

            if apdu.success and len(apdu.data) >= 4:
                return apdu.uid_hex
            else:
                logger.warning("GET_UID failed: %s", apdu)
                return None

        except CardConnectionException:
            logger.warning("Card removed too fast — could not read UID.")
            return None
        except Exception:
            logger.exception("Unexpected error reading card UID.")
            return None
        finally:
            if connection is not None:
                try:
                    connection.disconnect()
                except Exception:
                    pass
