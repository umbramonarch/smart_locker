"""
File: app_context.py
Description: Shared application state for the API layer. Bridges the NFC reader
             (pyscard background threads) with the async ASGI server by polling
             the reader's sync queue via asyncio.to_thread and pushing typed
             events into an asyncio.Queue consumed by the SSE endpoint.
Project: smart_locker/api
Notes: The module-level singleton 'context' is initialized by server.py's
       lifespan handler. Also manages pending self-registration and
       pending device-tag bind state.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field

from smart_locker.auth.session_manager import SessionManager
from config.settings import SESSION_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

# How long a self-registration attempt remains valid before the user must restart
REGISTRATION_TIMEOUT_SECONDS = 60


@dataclass
class PendingRegistration:
    """Holds state for a user self-registration awaiting an NFC card tap.

    Created when a user submits their name on the registration screen. The
    registration is valid for ``REGISTRATION_TIMEOUT_SECONDS`` (60s) — if no
    card is tapped before then, the attempt expires and the user must retry.
    """

    display_name: str
    created_at: float = field(default_factory=time.monotonic)

    @property
    def is_expired(self) -> bool:
        """Whether the registration window has elapsed without a card tap."""
        return (time.monotonic() - self.created_at) > REGISTRATION_TIMEOUT_SECONDS


@dataclass
class PendingTagBind:
    """Holds state for an admin device-tag bind awaiting an NFC sticker tap.

    Created when an admin picks a locker device in Register Device. Valid for
    ``REGISTRATION_TIMEOUT_SECONDS`` (60s). The next insert binds that row
    instead of borrowing.
    """

    device_id: int
    created_at: float = field(default_factory=time.monotonic)

    @property
    def is_expired(self) -> bool:
        """Whether the bind window has elapsed without a sticker tap."""
        return (time.monotonic() - self.created_at) > REGISTRATION_TIMEOUT_SECONDS


class AppContext:
    """Shared application state bridging NFC hardware with the async API layer.

    Holds the NFC reader, authenticator, session manager, SSE event queue,
    and pending registration state. A background asyncio task polls the
    reader's synchronous event queue via ``asyncio.to_thread`` and pushes
    typed events into the async SSE queue consumed by the browser.
    """

    def __init__(self) -> None:
        # Defer NFC/crypto imports so the module can be imported without pyscard.
        # create_reader() picks the real NFCReader, or the simulated reader when
        # SMART_LOCKER_FAKE_READER is set (no-hardware simulation harness).
        from smart_locker.nfc.factory import create_reader
        from smart_locker.auth.authenticator import Authenticator
        from smart_locker.security.key_manager import key_manager

        self.reader = create_reader()
        self.authenticator = Authenticator(hmac_key=key_manager.hmac_key)
        self.session_mgr = SessionManager(timeout_seconds=SESSION_TIMEOUT_SECONDS)
        self.sse_queue: asyncio.Queue = asyncio.Queue()
        self._bridge_task: asyncio.Task | None = None
        self._nfc_available = False
        self.pending_registration: PendingRegistration | None = None
        self.pending_tag_bind: PendingTagBind | None = None
        # True only while the admin / Register Device overlay is on screen.
        self.admin_overlay_open: bool = False
        # Last kiosk screen id heartbeated from the Riverdi UI (Display tab).
        self.kiosk_screen: str = "idle"

    async def start(self) -> None:
        """Start NFC reader and launch the bridge task.

        Attempts to connect to the NFC reader. If the reader is unavailable,
        the system falls back to API-only mode (no card events). On success,
        launches the ``_nfc_bridge_loop`` as a background asyncio task.

        Returns:
            None.
        """
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
        """Stop the bridge task and NFC reader.

        Cancels the background bridge task (if running), waits for it to
        finish, and then stops the NFC reader hardware.

        Returns:
            None.
        """
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
        """Poll NFC events and push SSE events to the browser.

        Runs indefinitely as a background asyncio task. On each iteration,
        checks for session timeout transitions, then polls the NFC reader's
        synchronous queue via ``asyncio.to_thread`` and dispatches card/reader
        events as SSE messages to the frontend.

        Returns:
            None. Runs until cancelled.
        """
        from smart_locker.nfc.card_observer import CardEvent, CardEventType
        from smart_locker.nfc.reader_observer import ReaderEvent, ReaderEventType
        from smart_locker.database.engine import get_session

        had_session = False

        while True:
            # Check for session timeout transition
            currently_active = self.session_mgr.has_active_session
            if had_session and not currently_active:
                self.admin_overlay_open = False
                self.pending_tag_bind = None
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

                await self._dispatch_insert(
                    event.uid, get_session, event.reader_name
                )

            elif isinstance(event, ReaderEvent):
                if event.event_type == ReaderEventType.DISCONNECTED:
                    logger.warning("NFC reader disconnected.")
                    if self.session_mgr.has_active_session:
                        self.session_mgr.end_session()
                    self.admin_overlay_open = False
                    self.pending_tag_bind = None
                    await self.sse_queue.put({"event": "reader_disconnected"})
                elif event.event_type == ReaderEventType.CONNECTED:
                    logger.info("NFC reader reconnected.")
                    await self.sse_queue.put({"event": "reader_connected"})

    async def _dispatch_insert(
        self, uid: str, get_session, reader_name: str = ""
    ) -> None:
        """Route one insert through pending intercepts, then the tap router.

        Expired registration and bind windows are dropped so the tap is
        classified instead of consumed as a failed enroll/bind. A leftover
        overlay session is ended when a registration window expires so the
        same tap can log in rather than log out.
        """
        if self.pending_registration is not None:
            if self.pending_registration.is_expired:
                logger.info(
                    "Registration window expired for '%s'.",
                    self.pending_registration.display_name,
                )
                self.pending_registration = None
                self._end_leftover_session()
            else:
                await self._handle_registration_tap(uid, get_session)
                return

        if self.pending_tag_bind is not None:
            if self.pending_tag_bind.is_expired:
                logger.info(
                    "Device tag bind window expired for device_id=%d.",
                    self.pending_tag_bind.device_id,
                )
                self.pending_tag_bind = None
            elif self._uid_is_work_card(uid, get_session):
                # Bind is for stickers. A work card must still log in (or stay
                # logged in) rather than consuming the window as tag_bind_failed.
                logger.info(
                    "Work card tapped during device-tag bind; bind window kept."
                )
                if self.session_mgr.has_active_session:
                    return
            else:
                await self._handle_tag_bind_tap(uid, get_session)
                return

        from smart_locker.auth.tap_router import handle_insert
        from smart_locker.security.key_manager import key_manager

        overlay = self.admin_overlay_open
        session_mgr = self.session_mgr
        hmac_key = key_manager.hmac_key

        def _run_insert():
            with get_session() as db_session:
                return handle_insert(
                    db_session,
                    uid,
                    hmac_key,
                    session_mgr,
                    admin_overlay_open=overlay,
                    reader_name=reader_name,
                )

        result = await asyncio.to_thread(_run_insert)

        if result.event in ("session_ended", "auth_success"):
            self.admin_overlay_open = False
            if result.event == "session_ended":
                self.pending_tag_bind = None

        sse = result.to_sse()
        if sse is not None:
            await self.sse_queue.put(sse)

    async def _handle_registration_tap(self, uid: str, get_session) -> None:
        """Enroll a new user when a card is tapped during pending registration.

        Validates that the registration has not expired and the card is not
        already enrolled, then creates the user record and pushes a success
        or failure SSE event to the frontend. Always ends a leftover overlay
        session so the next work-card tap is login, not logout.

        Args:
            uid: Hex-encoded card UID from the NFC reader.
            get_session: Callable returning a SQLAlchemy session context manager.

        Returns:
            None. Result is pushed to the SSE queue.
        """
        from smart_locker.database.repositories import DeviceRepository
        from smart_locker.security.hashing import compute_uid_hmac
        from smart_locker.security.key_manager import key_manager
        from smart_locker.services.user_service import UserService

        pending = self.pending_registration
        self.pending_registration = None
        try:
            if pending is None:
                return

            if pending.is_expired:
                logger.info("Registration expired for '%s'.", pending.display_name)
                await self.sse_queue.put({
                    "event": "registration_failed",
                    "reason": "Registration timed out. Please try again.",
                })
                return

            user_svc = UserService(
                enc_key=key_manager.enc_key, hmac_key=key_manager.hmac_key
            )

            try:
                with get_session() as db_session:
                    uid_hmac = compute_uid_hmac(uid, key_manager.hmac_key)
                    if DeviceRepository.find_by_tag_hmac(db_session, uid_hmac) is not None:
                        logger.warning("Registration failed: UID is already a device tag.")
                        await self.sse_queue.put({
                            "event": "registration_failed",
                            "reason": "This tag is already bound to a device.",
                        })
                        return

                    # Check if card is already enrolled
                    existing = self.authenticator.authenticate(db_session, uid)
                    if existing is not None:
                        logger.warning(
                            "Registration failed: card already enrolled to %s.",
                            existing.display_name,
                        )
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
                    logger.info(
                        "Self-registered user: %s (id=%d)",
                        user.display_name,
                        user.id,
                    )
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
        finally:
            self._end_leftover_session()

    def _uid_is_work_card(self, uid: str, get_session) -> bool:
        """Whether this UID is an enrolled work card (not a device sticker).

        Used so an armed bind window does not steal login. Lookup failures
        are treated as not-a-work-card so a sticker tap still binds.

        Args:
            uid: Hex-encoded UID from the NFC reader.
            get_session: Callable returning a SQLAlchemy session context manager.

        Returns:
            True if the UID matches an active user row.
        """
        try:
            from smart_locker.database.repositories import UserRepository
            from smart_locker.security.hashing import compute_uid_hmac
            from smart_locker.security.key_manager import key_manager

            digest = compute_uid_hmac(uid, key_manager.hmac_key)
            with get_session() as db_session:
                user = UserRepository.find_by_uid_hmac(db_session, digest)
            return user is not None and bool(user.is_active)
        except Exception:
            logger.exception("Work-card lookup failed during tag-bind intercept.")
            return False

    def _end_leftover_session(self) -> None:
        """Drop a leftover overlay session with no session_ended SSE.

        After admin Register User the overlay session must not remain, or
        the next work-card tap is logout instead of login. The register
        success/fail screen stays until the frontend navigates to idle.
        """
        self.session_mgr.end_session()
        self.admin_overlay_open = False

    async def _handle_tag_bind_tap(self, uid: str, get_session) -> None:
        """Bind the next insert to the device chosen in Register Device.

        Fails if the UID is a work card or already another device's tag.
        Does not borrow. Never logs the raw UID.

        Args:
            uid: Hex-encoded sticker UID from the NFC reader.
            get_session: Callable returning a SQLAlchemy session context manager.
        """
        from smart_locker.auth.tap_router import bind_uid_to_device
        from smart_locker.database.repositories import DeviceRepository
        from smart_locker.security.key_manager import key_manager

        pending = self.pending_tag_bind
        self.pending_tag_bind = None
        if pending is None:
            return

        self.session_mgr.touch()

        if pending.is_expired:
            logger.info("Device tag bind timed out for device_id=%d.", pending.device_id)
            await self.sse_queue.put({
                "event": "tag_bind_failed",
                "reason": "Bind timed out. Please try again.",
            })
            return

        try:
            with get_session() as db_session:
                device = DeviceRepository.find_by_id(db_session, pending.device_id)
                if device is None:
                    await self.sse_queue.put({
                        "event": "tag_bind_failed",
                        "reason": "Device not found.",
                    })
                    return
                bind_uid_to_device(
                    db_session, device, uid, key_manager.hmac_key
                )
                logger.info(
                    "Bound device tag for %s (pm=%s).",
                    device.name,
                    device.pm_number,
                )
                await self.sse_queue.put({
                    "event": "tag_bind_success",
                    "device_id": device.id,
                    "device_name": device.name,
                    "pm_number": device.pm_number,
                })
        except ValueError as e:
            await self.sse_queue.put({
                "event": "tag_bind_failed",
                "reason": str(e),
            })
        except Exception:
            logger.exception(
                "Device tag bind failed for device_id=%d.", pending.device_id
            )
            await self.sse_queue.put({
                "event": "tag_bind_failed",
                "reason": "Bind failed. Please try again.",
            })


# Module-level singleton — initialized by server.py lifespan
context: AppContext | None = None
