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
    PHOTO_INPUT_PATH,
    PHOTO_SERVE_DIR,
    SESSION_TIMEOUT_SECONDS,
    SOURCE_EXCEL_PATH,
    SOURCE_SYNC_INTERVAL_HOURS,
)
from smart_locker.auth.session_manager import SessionManager
from smart_locker.auth.tap_router import handle_insert
from smart_locker.database.engine import get_engine, get_session, init_db
from smart_locker.database.repositories import DeviceRepository
from smart_locker.nfc.card_observer import CardEvent, CardEventType
from smart_locker.nfc.exceptions import NFCError
from smart_locker.nfc.factory import create_reader
from smart_locker.nfc.reader_observer import ReaderEvent, ReaderEventType
from smart_locker.security.key_manager import key_manager

logger = logging.getLogger(__name__)


def _start_background_sync() -> None:
    """Start the source-import scheduler and the photo watcher, each guarded.

    The appliance must keep running no matter what the network share or the
    source files are doing. A failure to start either subsystem — the locker share
    is down at boot, a source/photo path is unreadable, or a watcher cannot be
    created — is logged and swallowed here so it can NEVER stop the web server
    and NFC flow from coming up. Each missed sync is retried by the interval
    import or the admin "Sync source" action once the share is back. The two
    subsystems are guarded independently so one failing does not disable the
    other.
    """
    if SOURCE_EXCEL_PATH:
        try:
            from smart_locker.sync.scheduler import start_scheduler
            start_scheduler(
                get_engine(), SOURCE_EXCEL_PATH, SOURCE_SYNC_INTERVAL_HOURS
            )
        except Exception:
            logger.exception(
                "Source-import scheduler failed to start — continuing without it. "
                "The kiosk stays up; run the admin 'Sync source' once the share is back."
            )

    if PHOTO_INPUT_PATH:
        try:
            from smart_locker.sync.photo_watcher import start_photo_watcher
            start_photo_watcher(get_engine(), PHOTO_INPUT_PATH, PHOTO_SERVE_DIR)
        except Exception:
            logger.exception(
                "Photo watcher failed to start — continuing without it. "
                "Photos can be applied later via 'python -m scripts.update_device --auto'."
            )


class SmartLockerApp:
    """Main application orchestrator for CLI mode.

    Initializes the NFC reader, authenticator, and session manager, then
    runs a blocking event loop that processes card and reader events from
    the console. Used when the system is started with ``--cli`` flag
    instead of the default FastAPI web server mode.
    """

    def __init__(self) -> None:
        self._reader = create_reader()
        self._session_mgr = SessionManager(timeout_seconds=SESSION_TIMEOUT_SECONDS)
        self._running = False

    def run(self) -> None:
        """Start the application main loop.

        Initializes logging and the database. If a source Excel path is
        configured, runs an immediate import on startup (so the database
        is current before the first user interaction), starts a file
        watcher for live source changes on a local filesystem, and
        schedules an interval import as a safety net. Then starts the NFC
        reader and enters a blocking
        event loop until Ctrl+C is pressed.

        Returns:
            None.
        """
        setup_logging()
        init_db()

        _start_background_sync()

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
        """Route an NFC insert through the shared tap router.

        A work card starts a session when idle and logs out when a session is
        already active (does not start the new user on the same tap). A device
        tag borrows or returns while logged in. A borrowed tag at idle returns
        the device without a work card. A device tag does not end the session.

        Args:
            event: The card-inserted event containing the card UID.

        Returns:
            None.
        """
        if event.uid is None:
            print("Could not read card. Please try tapping again.")
            return

        with get_session() as db_session:
            result = handle_insert(
                db_session,
                event.uid,
                key_manager.hmac_key,
                self._session_mgr,
                admin_overlay_open=False,
                reader_name=event.reader_name,
            )
            if result.cli_message:
                print(f"\n{result.cli_message}")

            if result.event != "auth_success":
                if result.event == "session_ended":
                    print()
                return

            active = self._session_mgr.current_session
            if active is None:
                return
            user = active.user

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

            print(
                "\nTap a device tag to borrow or return, "
                "or tap your work card to end the session."
            )

    def _on_card_removed(self, event: CardEvent) -> None:
        """Handle card removal (no-op — session persists on touch display).

        Args:
            event: The card-removed event (unused — removal is intentionally ignored).

        Returns:
            None.
        """
        # Card removal does not end the session — the user interacts with
        # the touch display after tapping. Session ends via timeout or a
        # work-card tap (handled in _on_card_inserted).
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

    Initializes logging and the database. If a source Excel path is
    configured, runs an immediate import on startup (so the database
    is current before the first request), starts a file watcher for
    live source changes on a local filesystem, and schedules an interval
    import as a safety net. Then creates and runs the FastAPI application
    with uvicorn.
    This is the default mode.

    Returns:
        None.
    """
    import uvicorn

    from config.settings import API_HOST, API_PORT
    from smart_locker.api.server import create_app

    setup_logging()
    init_db()

    _start_background_sync()

    app = create_app()
    logger.info("Starting Smart Locker web server on %s:%d", API_HOST, API_PORT)
    uvicorn.run(app, host=API_HOST, port=API_PORT)


if __name__ == "__main__":
    if "--cli" in sys.argv:
        main()
    else:
        run_server()
