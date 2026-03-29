"""
File: card_observer.py
Description: Event-driven card insert/remove detection. Implements pyscard's
             CardObserver interface to read the card UID via a fast APDU command
             on insert and post CardEvent objects to a queue consumed by the
             main application thread.
Project: smart_locker/nfc
Notes: UID read includes retry logic (3 attempts with 50ms delay) to handle
       quick tap-and-go interactions. The reader buzzer is suppressed during
       card reads and restored afterward.
"""

import enum
import logging
import queue
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from smartcard.CardMonitoring import CardObserver
from smartcard.Exceptions import CardConnectionException

from smart_locker.nfc.apdu import GET_UID, DISABLE_BUZZER, ENABLE_BUZZER, APDUResponse

logger = logging.getLogger(__name__)



class CardEventType(enum.Enum):
    """Type of physical card interaction detected by the NFC reader."""

    INSERTED = "inserted"
    REMOVED = "removed"


@dataclass
class CardEvent:
    """Represents a card insert or remove event detected by the NFC reader.

    On insert, the observer attempts to read the card UID via APDU. If the
    read succeeds, ``uid`` contains the hex string; if the card was removed
    too fast, ``uid`` is None.
    """

    event_type: CardEventType
    uid: str | None = None  # Hex UID string, or None if read failed
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
        """Handle card insert/remove notifications from pyscard's CardMonitor.

        Args:
            observable: The CardMonitor instance.
            actions: Tuple of (added_cards, removed_cards) lists.
        """
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
                logger.info("Card inserted on %s", reader_name)
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
            logger.info("Card removed from %s", reader_name)
            self._last_uid = None

    @staticmethod
    def _read_uid(card) -> str | None:
        """Connect to card and read UID via APDU with retry logic.

        Attempts to read the UID multiple times with small delays to allow
        the card to stabilize on the reader, enabling quick tap interactions.

        Returns hex string or None.
        """
        max_attempts = 3
        delay_between_attempts = 0.05  # 50ms between retries

        for attempt in range(max_attempts):
            connection = None
            try:
                # Small delay before first read to let card stabilize
                if attempt == 0:
                    time.sleep(0.02)  # 20ms initial delay

                connection = card.createConnection()
                connection.connect()
                # Suppress reader buzzer for this card session
                connection.transmit(DISABLE_BUZZER)
                response, sw1, sw2 = connection.transmit(GET_UID)
                apdu = APDUResponse.from_raw(response, sw1, sw2)

                # Minimum 4 bytes for a valid UID (MIFARE Ultralight = 7, Classic = 4)
                if apdu.success and len(apdu.data) >= 4:
                    if attempt > 0:
                        logger.debug("UID read successful on attempt %d", attempt + 1)
                    return apdu.uid_hex
                else:
                    logger.warning("GET_UID failed on attempt %d: %s", attempt + 1, apdu)

            except CardConnectionException:
                if attempt < max_attempts - 1:
                    logger.debug("Card connection failed on attempt %d, retrying...", attempt + 1)
                else:
                    logger.warning("Card removed too fast — could not read UID after %d attempts.", max_attempts)
            except Exception:
                logger.exception("Unexpected error reading card UID on attempt %d.", attempt + 1)
                return None
            finally:
                if connection is not None:
                    try:
                        # Restore buzzer so the next card tap will beep
                        connection.transmit(ENABLE_BUZZER)
                    except Exception:
                        pass
                    try:
                        connection.disconnect()
                    except Exception:
                        pass

            # Wait before next attempt
            if attempt < max_attempts - 1:
                time.sleep(delay_between_attempts)

        return None
