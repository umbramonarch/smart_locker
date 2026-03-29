"""
File: app.py
Description: Application entry point for the Smart Locker system. Supports two
             modes: a FastAPI + uvicorn web server (default) serving the kiosk UI
             with NFC bridge, and a CLI mode for console-only NFC operation.
Project: smart_locker
Notes: Run via 'python -m smart_locker.app' (web server on port 8000) or
       'python -m smart_locker.app --cli' (console-only NFC loop).
"""

import logging
import signal
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.logging_config import setup_logging
from config.settings import (
    EXCEL_SYNC_PATH,
    SESSION_TIMEOUT_SECONDS,
    SOURCE_EXCEL_PATH,
    SOURCE_SYNC_HOUR,
    SOURCE_SYNC_MINUTE,
)
from smart_locker.auth.authenticator import Authenticator
from smart_locker.auth.session_manager import SessionManager
from smart_locker.database.engine import get_engine, get_session, init_db
from smart_locker.database.repositories import DeviceRepository
from smart_locker.nfc.card_observer import CardEvent, CardEventType
from smart_locker.nfc.exceptions import NFCError
from smart_locker.nfc.reader import NFCReader
from smart_locker.nfc.reader_observer import ReaderEvent, ReaderEventType
from smart_locker.security.key_manager import key_manager
from smart_locker.services.locker_service import LockerService

logger = logging.getLogger(__name__)


class SmartLockerApp:
    """Main application orchestrator for CLI mode.

    Initializes the NFC reader, authenticator, and session manager, then
    runs a blocking event loop that processes card and reader events from
    the console. Used when the system is started with ``--cli`` flag
    instead of the default FastAPI web server mode.
    """

    def __init__(self) -> None:
        self._reader = NFCReader()
        self._authenticator = Authenticator(hmac_key=key_manager.hmac_key)
        self._session_mgr = SessionManager(timeout_seconds=SESSION_TIMEOUT_SECONDS)
        self._running = False

    def run(self) -> None:
        """Start the application main loop.

        Initializes logging, the database, Excel sync, and the optional
        daily source import scheduler, then starts the NFC reader and
        enters a blocking event loop until Ctrl+C is pressed.

        Returns:
            None.
        """
        setup_logging()
        init_db()

        from smart_locker.sync.excel_sync import register_auto_sync
        register_auto_sync(get_engine(), EXCEL_SYNC_PATH)

        if SOURCE_EXCEL_PATH:
            from smart_locker.sync.scheduler import start_scheduler
            start_scheduler(get_engine(), SOURCE_EXCEL_PATH, SOURCE_SYNC_HOUR, SOURCE_SYNC_MINUTE)

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

        # Handle Ctrl+C — set _running to False so _main_loop exits gracefully
        def _signal_handler(sig, frame):
            """Signal handler for SIGINT that triggers a graceful shutdown."""
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
        """Poll for NFC events and dispatch to appropriate handlers.

        Blocks until ``self._running`` is set to False (via SIGINT).
        On each iteration, waits up to 1 second for an event from the NFC
        reader queue. If no event arrives, the session manager is ticked
        so expired sessions are cleaned up.

        Returns:
            None.
        """
        while self._running:
            event = self._reader.wait_for_event(timeout=1.0)
            if event is None:
                # Tick the session manager so expired sessions are cleaned up
                self._session_mgr.has_active_session
                continue

            if isinstance(event, ReaderEvent):
                self._handle_reader_event(event)
            elif isinstance(event, CardEvent):
                self._handle_card_event(event)

    def _handle_reader_event(self, event: ReaderEvent) -> None:
        """Handle NFC reader connect/disconnect events.

        On disconnect, warns the user and ends any active session for safety.
        On reconnect, prints a confirmation message.

        Args:
            event: The reader connect/disconnect event.

        Returns:
            None.
        """
        if event.event_type == ReaderEventType.DISCONNECTED:
            print("\nWARNING: NFC reader disconnected!")
            if self._session_mgr.has_active_session:
                self._session_mgr.end_session()
        elif event.event_type == ReaderEventType.CONNECTED:
            print("NFC reader reconnected.")

    def _handle_card_event(self, event: CardEvent) -> None:
        """Route card insert/remove events to the appropriate handler.

        Args:
            event: The card inserted/removed event from the NFC reader.

        Returns:
            None.
        """
        if event.event_type == CardEventType.INSERTED:
            self._on_card_inserted(event)
        elif event.event_type == CardEventType.REMOVED:
            self._on_card_removed(event)

    def _on_card_inserted(self, event: CardEvent) -> None:
        """Authenticate user on card insert, or end session on second tap.

        If no session is active, the card UID is authenticated via HMAC lookup
        and a new session is started. If a session is already active, a second
        tap ends the session (logout). Displays borrowed and available devices
        after successful authentication.

        Args:
            event: The card-inserted event containing the card UID.

        Returns:
            None.
        """
        if event.uid is None:
            print("Could not read card. Please try tapping again.")
            return

        # Second tap while a session is active → log out
        if self._session_mgr.has_active_session:
            active_session = self._session_mgr.current_session
            if active_session is not None:
                user_name = active_session.user.display_name
                self._session_mgr.end_session()
                print(f"\nGoodbye, {user_name}!")
                print("Tap your card to begin.\n")
            return

        with get_session() as db_session:
            user = self._authenticator.authenticate(db_session, event.uid)

            if user is None:
                print("Unknown card. Please contact an administrator to enroll.")
                return

            self._session_mgr.start_session(user)
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

            print("\nTap your card again or wait to time out to end session.")

    def _on_card_removed(self, event: CardEvent) -> None:
        """Handle card removal (no-op — session persists on touch display).

        Args:
            event: The card-removed event (unused — removal is intentionally ignored).

        Returns:
            None.
        """
        # Card removal does not end the session — the user interacts with
        # the touch display after tapping. Session ends via timeout or a
        # second tap (handled in _on_card_inserted).
        pass


def main() -> None:
    """Legacy CLI mode — blocking NFC loop with console output.

    Creates a ``SmartLockerApp`` instance and starts the blocking event loop.
    Used when the application is started with the ``--cli`` flag.

    Returns:
        None.
    """
    app = SmartLockerApp()
    app.run()


def run_server() -> None:
    """Web server mode — FastAPI + uvicorn with NFC bridge.

    Initializes logging, the database, Excel sync, and the optional daily
    source import scheduler, then creates and runs the FastAPI application
    with uvicorn. This is the default mode when running the application.

    Returns:
        None.
    """
    import uvicorn

    from config.settings import API_HOST, API_PORT
    from smart_locker.api.server import create_app

    setup_logging()
    init_db()

    from smart_locker.sync.excel_sync import register_auto_sync
    register_auto_sync(get_engine(), EXCEL_SYNC_PATH)

    if SOURCE_EXCEL_PATH:
        from smart_locker.sync.scheduler import start_scheduler
        start_scheduler(get_engine(), SOURCE_EXCEL_PATH, SOURCE_SYNC_HOUR, SOURCE_SYNC_MINUTE)

    app = create_app()
    logger.info("Starting Smart Locker web server on %s:%d", API_HOST, API_PORT)
    uvicorn.run(app, host=API_HOST, port=API_PORT)


if __name__ == "__main__":
    if "--cli" in sys.argv:
        main()
    else:
        run_server()
