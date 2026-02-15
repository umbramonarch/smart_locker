"""Smart Locker application entry point.

Wires together NFC reader, authentication, session management, and services.
Main loop: wait_for_card() → authenticate → start session → card removed → end session.

Usage:
    python -m smart_locker.app
"""

import logging
import signal
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.logging_config import setup_logging
from config.settings import SESSION_TIMEOUT_SECONDS
from smart_locker.auth.authenticator import Authenticator
from smart_locker.auth.session_manager import SessionManager
from smart_locker.database.engine import get_session, init_db
from smart_locker.database.repositories import DeviceRepository
from smart_locker.nfc.card_observer import CardEvent, CardEventType
from smart_locker.nfc.exceptions import NFCError
from smart_locker.nfc.reader import NFCReader
from smart_locker.nfc.reader_observer import ReaderEvent, ReaderEventType
from smart_locker.security.key_manager import key_manager
from smart_locker.services.locker_service import LockerService

logger = logging.getLogger(__name__)


class SmartLockerApp:
    """Main application orchestrator."""

    def __init__(self) -> None:
        self._reader = NFCReader()
        self._authenticator = Authenticator(hmac_key=key_manager.hmac_key)
        self._session_mgr = SessionManager(timeout_seconds=SESSION_TIMEOUT_SECONDS)
        self._running = False

    def run(self) -> None:
        """Start the application main loop."""
        setup_logging()
        init_db()

        logger.info("Smart Locker starting...")

        # Start NFC reader
        try:
            reader_name = self._reader.start()
            logger.info("NFC reader ready: %s", reader_name)
        except NFCError as e:
            logger.error("Failed to start NFC reader: %s", e)
            print(f"ERROR: {e}")
            return

        self._running = True

        # Handle Ctrl+C
        def _signal_handler(sig, frame):
            logger.info("Shutdown signal received.")
            self._running = False

        signal.signal(signal.SIGINT, _signal_handler)

        print("Smart Locker ready. Tap your card to begin.")
        print("Press Ctrl+C to exit.\n")

        try:
            self._main_loop()
        finally:
            self._reader.stop()
            logger.info("Smart Locker stopped.")

    def _main_loop(self) -> None:
        while self._running:
            event = self._reader.wait_for_event(timeout=1.0)
            if event is None:
                # Check session timeout
                if self._session_mgr.has_active_session:
                    session = self._session_mgr.current_session
                    if session is None:
                        # Session expired during check
                        print("\nSession timed out. Please tap your card again.")
                continue

            if isinstance(event, ReaderEvent):
                self._handle_reader_event(event)
            elif isinstance(event, CardEvent):
                self._handle_card_event(event)

    def _handle_reader_event(self, event: ReaderEvent) -> None:
        if event.event_type == ReaderEventType.DISCONNECTED:
            print("\nWARNING: NFC reader disconnected!")
            if self._session_mgr.has_active_session:
                self._session_mgr.end_session()
        elif event.event_type == ReaderEventType.CONNECTED:
            print("NFC reader reconnected.")

    def _handle_card_event(self, event: CardEvent) -> None:
        if event.event_type == CardEventType.INSERTED:
            self._on_card_inserted(event)
        elif event.event_type == CardEventType.REMOVED:
            self._on_card_removed(event)

    def _on_card_inserted(self, event: CardEvent) -> None:
        if event.uid is None:
            print("Could not read card. Please hold card steady on the reader.")
            return

        with get_session() as db_session:
            user = self._authenticator.authenticate(db_session, event.uid)

            if user is None:
                print("Unknown card. Please contact an administrator to enroll.")
                return

            session = self._session_mgr.start_session(user)
            print(f"\nWelcome, {user.display_name}!")

            # Show user's borrowed devices
            borrowed = DeviceRepository.get_borrowed_by_user(db_session, user.id)
            if borrowed:
                print(f"You have {len(borrowed)} borrowed device(s):")
                for d in borrowed:
                    print(f"  - {d.name} ({d.device_type})")

            # Show available devices
            available = DeviceRepository.get_available_devices(db_session)
            if available:
                print(f"\n{len(available)} device(s) available to borrow:")
                for d in available:
                    print(f"  [{d.id}] {d.name} ({d.device_type})")

            print("\nRemove card to end session.")

    def _on_card_removed(self, event: CardEvent) -> None:
        if self._session_mgr.has_active_session:
            user_name = self._session_mgr.current_session.user.display_name
            self._session_mgr.end_session()
            print(f"\nGoodbye, {user_name}!")
            print("Tap your card to begin.\n")


def main() -> None:
    app = SmartLockerApp()
    app.run()


if __name__ == "__main__":
    main()
