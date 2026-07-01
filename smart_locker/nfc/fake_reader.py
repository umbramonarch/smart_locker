"""
File: fake_reader.py
Description: Drop-in, hardware-free replacement for NFCReader used by the
             no-hardware simulation harness. Exposes the same public surface
             (start / stop / wait_for_event / poll_event / is_running) but,
             instead of talking to a PC/SC reader, lets callers inject
             simulated card taps via simulate_tap(uid), which enqueues a
             synthetic CardEvent(INSERTED) onto the same kind of queue the
             real observers post to.
Project: smart_locker/nfc
Notes: Enabled ONLY when SMART_LOCKER_FAKE_READER is set (see factory.py);
       production never enables it. The raw card UID is NEVER logged here —
       same security invariant as the real reader ("Card inserted on <reader>"
       with no UID). No pyscard / PC/SC dependency beyond the shared CardEvent
       dataclass, so it runs on any host (native dev box or inside QEMU).
"""

import logging
import queue
from typing import Union

from smart_locker.nfc.card_observer import CardEvent, CardEventType
from smart_locker.nfc.reader_observer import ReaderEvent

logger = logging.getLogger(__name__)

# Unified event type, identical to NFCReader's so the bridge's isinstance checks
# (CardEvent / ReaderEvent) behave exactly as they do for the real reader.
Event = Union[CardEvent, ReaderEvent]

# Synthetic reader name surfaced to logs / status — clearly marked as simulated.
FAKE_READER_NAME = "FAKE-ACR1252U (simulated)"


class FakeNFCReader:
    """Simulated NFC reader that injects card taps instead of reading hardware.

    Implements the same duck-typed interface as ``NFCReader`` so it can be
    swapped in wherever a reader is constructed (``AppContext``,
    ``SmartLockerApp``) without any other code change. Events are delivered
    through a thread-safe ``queue.Queue`` exactly like the real reader, so the
    downstream NFC bridge, authenticator, and SSE pipeline are exercised
    unchanged. Taps are produced by calling :meth:`simulate_tap`.
    """

    def __init__(self, reader_filter: str | None = None) -> None:
        """Initialize the fake reader.

        Args:
            reader_filter: Accepted for signature parity with ``NFCReader``;
                ignored (there is no hardware to filter).
        """
        self._reader_filter = reader_filter
        self._event_queue: queue.Queue[Event] = queue.Queue()
        self._running = False

    def start(self) -> str:
        """Begin "monitoring" (no hardware) and report the synthetic reader name.

        Returns:
            The synthetic reader name, so ``AppContext.start`` marks NFC
            available and launches the bridge loop just as with real hardware.
        """
        self._running = True
        logger.info("Fake NFC reader started (simulation mode — no hardware).")
        return FAKE_READER_NAME

    def stop(self) -> None:
        """Stop the fake reader and mark it not running."""
        self._running = False
        logger.info("Fake NFC reader stopped.")

    def wait_for_event(self, timeout: float | None = None) -> Event | None:
        """Block until a simulated event arrives or the timeout expires.

        Args:
            timeout: Seconds to wait. None blocks forever.

        Returns:
            The next ``CardEvent``/``ReaderEvent``, or None on timeout.
        """
        try:
            return self._event_queue.get(block=True, timeout=timeout)
        except queue.Empty:
            return None

    def poll_event(self) -> Event | None:
        """Non-blocking check for a pending simulated event."""
        try:
            return self._event_queue.get_nowait()
        except queue.Empty:
            return None

    @property
    def is_running(self) -> bool:
        """Whether the fake reader is currently 'monitoring'."""
        return self._running

    def simulate_tap(self, uid: str) -> None:
        """Inject a simulated card-inserted event carrying ``uid``.

        Mirrors exactly what ``LockerCardObserver`` posts on a real tap, so the
        bridge → authenticator → SSE flow runs identically. The UID is NOT
        logged (security invariant: raw card UIDs are never logged).

        Args:
            uid: Hex UID string of the simulated card (looked up via HMAC by
                the authenticator, exactly as a real UID would be).
        """
        event = CardEvent(
            event_type=CardEventType.INSERTED,
            uid=uid,
            reader_name=FAKE_READER_NAME,
        )
        self._event_queue.put(event)
        # Intentionally no UID in the log line — matches the real reader.
        logger.info("Simulated card tap on %s.", FAKE_READER_NAME)
