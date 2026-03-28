"""Shared application state for the API layer.

Bridges the NFC reader (pyscard background threads) with the async ASGI
server by polling the reader's sync queue via asyncio.to_thread and pushing
typed events into an asyncio.Queue consumed by the SSE endpoint.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field

from smart_locker.auth.session_manager import SessionManager
from config.settings import SESSION_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

REGISTRATION_TIMEOUT_SECONDS = 60


@dataclass
class PendingRegistration:
    """Holds state for a user self-registration awaiting NFC card tap."""

    display_name: str
    created_at: float = field(default_factory=time.monotonic)

    @property
    def is_expired(self) -> bool:
        return (time.monotonic() - self.created_at) > REGISTRATION_TIMEOUT_SECONDS


class AppContext:
    """Singleton holding NFC reader, authenticator, session manager, and SSE queue."""

    def __init__(self) -> None:
        # Defer NFC/crypto imports so the module can be imported without pyscard
        from smart_locker.nfc.reader import NFCReader
        from smart_locker.auth.authenticator import Authenticator
        from smart_locker.security.key_manager import key_manager

        self.reader = NFCReader()
        self.authenticator = Authenticator(hmac_key=key_manager.hmac_key)
        self.session_mgr = SessionManager(timeout_seconds=SESSION_TIMEOUT_SECONDS)
        self.sse_queue: asyncio.Queue = asyncio.Queue()
        self._bridge_task: asyncio.Task | None = None
        self._nfc_available = False
        self.pending_registration: PendingRegistration | None = None

    async def start(self) -> None:
        """Start NFC reader and launch the bridge task."""
        from smart_locker.nfc.exceptions import NFCError

        try:
            reader_name = self.reader.start()
            self._nfc_available = True
            logger.info("NFC reader ready: %s", reader_name)
        except NFCError as e:
            self._nfc_available = False
            logger.warning("NFC reader not available: %s. Running in API-only mode.", e)

        if self._nfc_available:
            self._bridge_task = asyncio.create_task(self._nfc_bridge_loop())

    async def stop(self) -> None:
        """Stop the bridge task and NFC reader."""
        if self._bridge_task is not None:
            self._bridge_task.cancel()
            try:
                await self._bridge_task
            except asyncio.CancelledError:
                pass
            self._bridge_task = None

        if self._nfc_available:
            self.reader.stop()

    async def _nfc_bridge_loop(self) -> None:
        """Poll NFC events and push SSE events to the browser."""
        from smart_locker.nfc.card_observer import CardEvent, CardEventType
        from smart_locker.nfc.reader_observer import ReaderEvent, ReaderEventType
        from smart_locker.database.engine import get_session

        had_session = False

        while True:
            # Check for session timeout transition
            currently_active = self.session_mgr.has_active_session
            if had_session and not currently_active:
                await self.sse_queue.put({"event": "session_timeout"})
                logger.info("Session timeout detected by NFC bridge.")
            had_session = currently_active

            # Poll NFC queue (non-blocking via thread)
            try:
                event = await asyncio.to_thread(self.reader.wait_for_event, 0.5)
            except Exception:
                logger.exception("NFC bridge error polling reader.")
                await asyncio.sleep(1.0)
                continue

            if event is None:
                continue

            if isinstance(event, CardEvent):
                if event.event_type != CardEventType.INSERTED:
                    continue

                if event.uid is None:
                    logger.warning("Card inserted but UID could not be read.")
                    continue

                # Pending registration: enroll the card instead of authenticating
                if self.pending_registration is not None:
                    await self._handle_registration_tap(event.uid, get_session)
                    continue

                # Second tap while session active -> logout
                if self.session_mgr.has_active_session:
                    session = self.session_mgr.current_session
                    if session is not None:
                        logger.info("Second tap logout for %s.", session.user.display_name)
                        self.session_mgr.end_session()
                        await self.sse_queue.put({
                            "event": "session_ended",
                            "reason": "card_tap",
                        })
                    continue

                # Authenticate
                with get_session() as db_session:
                    user = self.authenticator.authenticate(db_session, event.uid)

                if user is None:
                    await self.sse_queue.put({"event": "auth_failed"})
                    continue

                self.session_mgr.start_session(user)
                await self.sse_queue.put({
                    "event": "auth_success",
                    "user": {
                        "id": user.id,
                        "name": user.display_name,
                        "role": user.role.value,
                    },
                })

            elif isinstance(event, ReaderEvent):
                if event.event_type == ReaderEventType.DISCONNECTED:
                    logger.warning("NFC reader disconnected.")
                    if self.session_mgr.has_active_session:
                        self.session_mgr.end_session()
                    await self.sse_queue.put({"event": "reader_disconnected"})
                elif event.event_type == ReaderEventType.CONNECTED:
                    logger.info("NFC reader reconnected.")
                    await self.sse_queue.put({"event": "reader_connected"})

    async def _handle_registration_tap(self, uid: str, get_session) -> None:
        """Enroll a new user when a card is tapped during pending registration."""
        from smart_locker.security.key_manager import key_manager
        from smart_locker.services.user_service import UserService

        pending = self.pending_registration
        self.pending_registration = None

        if pending.is_expired:
            logger.info("Registration expired for '%s'.", pending.display_name)
            await self.sse_queue.put({
                "event": "registration_failed",
                "reason": "Registration timed out. Please try again.",
            })
            return

        user_svc = UserService(enc_key=key_manager.enc_key, hmac_key=key_manager.hmac_key)

        try:
            with get_session() as db_session:
                # Check if card is already enrolled
                existing = self.authenticator.authenticate(db_session, uid)
                if existing is not None:
                    logger.warning("Registration failed: card already enrolled to %s.", existing.display_name)
                    await self.sse_queue.put({
                        "event": "registration_failed",
                        "reason": "This card is already registered.",
                    })
                    return

                user = user_svc.enroll_user(
                    db_session,
                    display_name=pending.display_name,
                    card_uid_hex=uid,
                    role="user",
                )
                logger.info("Self-registered user: %s (id=%d)", user.display_name, user.id)
                await self.sse_queue.put({
                    "event": "registration_success",
                    "user": {
                        "id": user.id,
                        "name": user.display_name,
                        "role": user.role.value,
                    },
                })
        except Exception:
            logger.exception("Registration failed for '%s'.", pending.display_name)
            await self.sse_queue.put({
                "event": "registration_failed",
                "reason": "Registration failed. Please try again.",
            })


# Module-level singleton — initialized by server.py lifespan
context: AppContext | None = None
