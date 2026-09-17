"""
File: app_context.py
Description: Shared application state for the API layer. Bridges the NFC reader
             (pyscard background threads) with the async ASGI server by polling
             the reader's sync queue via asyncio.to_thread and pushing typed
             events into an asyncio.Queue consumed by the SSE endpoint.
Project: smart_locker/api
Notes: The module-level singleton 'context' is initialized by server.py's
       lifespan handler. Also manages pending self-registration (incl. admin
       manual register and lost-card replace) and pending device-tag bind
       state.
"""

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field

from smart_locker.auth.session_manager import SessionManager
from config.settings import SESSION_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

# How long a self-registration attempt remains valid before the user must restart
REGISTRATION_TIMEOUT_SECONDS = 60

# HTTP threadpool and the NFC bridge both write pending bind/registration.
pending_state_lock = threading.RLock()


@dataclass
class PendingRegistration:
    """Holds state for a user self-registration awaiting an NFC card tap.

    Created when a user submits their name on the registration screen, or
    when an admin starts manual registration / card replacement. The
    registration is valid for ``REGISTRATION_TIMEOUT_SECONDS`` (60s) — if no
    card is tapped before then, the attempt expires and the user must retry.
    ``replace_user_id`` set means the next tap moves that user's card
    instead of enrolling a new user.
    """

    display_name: str
    role: str = "user"
    replace_user_id: int | None = None
    created_at: float = field(default_factory=time.monotonic)

    @property
    def is_expired(self) -> bool:
        """Whether the registration window has elapsed without a card tap."""
        return (time.monotonic() - self.created_at) > REGISTRATION_TIMEOUT_SECONDS


@dataclass
class PendingTagBind:
    """Holds state for an admin device-tag bind awaiting an NFC sticker tap.

    Created when an admin picks a locker device in Register Device, or when
    dashboard staff arm a bind with the admin secret. Valid for
    ``REGISTRATION_TIMEOUT_SECONDS`` (60s). The next insert binds that row
    instead of borrowing. ``from_dashboard`` marks a secret-armed window so
    public ``POST /api/register/cancel`` cannot clear it.
    """

    device_id: int
    created_at: float = field(default_factory=time.monotonic)
    from_dashboard: bool = False

    @property
    def is_expired(self) -> bool:
        """Whether the bind window has elapsed without a sticker tap."""
        return (time.monotonic() - self.created_at) > REGISTRATION_TIMEOUT_SECONDS


def assign_pending_tag_bind(ctx, bind: PendingTagBind | None) -> None:
    """Set ``pending_tag_bind`` under the HTTP/NFC shared lock.

    Args:
        ctx: Application context (or test double with the same attribute).
        bind: New pending bind, or None to clear.
    """
    with pending_state_lock:
        ctx.pending_tag_bind = bind


def assign_pending_registration(ctx, pending: PendingRegistration | None) -> None:
    """Set ``pending_registration`` under the HTTP/NFC shared lock.

    Args:
        ctx: Application context (or test double with the same attribute).
        pending: New pending registration, or None to clear.
    """
    with pending_state_lock:
        ctx.pending_registration = pending


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
        self._sse_subscribers: list[asyncio.Queue] = []
        self._sse_lock = threading.Lock()
        self._bridge_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
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

        self._loop = asyncio.get_running_loop()
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

    def subscribe_sse(self) -> asyncio.Queue:
        """Register a new SSE subscriber queue (one per EventSource client).

        Returns:
            Queue that will receive a copy of every kiosk SSE payload.
        """
        queue: asyncio.Queue = asyncio.Queue()
        with self._sse_lock:
            self._sse_subscribers.append(queue)
        return queue

    def unsubscribe_sse(self, queue: asyncio.Queue) -> None:
        """Drop a subscriber queue when the EventSource client disconnects.

        Args:
            queue: Queue previously returned by ``subscribe_sse``.
        """
        with self._sse_lock:
            try:
                self._sse_subscribers.remove(queue)
            except ValueError:
                pass

    def _sse_targets(self) -> list[asyncio.Queue]:
        """Queues that should receive the next event.

        HTTP EventSource clients each have a subscriber queue. Tests and the
        NFC bridge still use ``sse_queue`` when nobody is subscribed.
        """
        with self._sse_lock:
            extras = list(self._sse_subscribers)
        if extras:
            return extras
        return [self.sse_queue]

    def broadcast_sse(self, payload: dict) -> None:
        """Copy ``payload`` onto every SSE queue. Safe from the threadpool.

        Args:
            payload: Event dict for kiosk / dashboard SSE clients.
        """
        loop = self._loop
        for queue in self._sse_targets():
            try:
                if isinstance(loop, asyncio.AbstractEventLoop) and loop.is_running():
                    loop.call_soon_threadsafe(queue.put_nowait, dict(payload))
                else:
                    queue.put_nowait(dict(payload))
            except Exception:
                logger.debug("SSE fan-out drop", exc_info=True)

    def _drop_nfc_windows_on_session_end(self) -> None:
        """Clear armed NFC windows when the session ends without a tap.

        Reader disconnect and session timeout share this: a pending card
        registration/replacement or tag bind must not outlive the session
        that armed it — otherwise the next fresh tap would complete an
        operation nobody is supervising.
        """
        self.admin_overlay_open = False
        assign_pending_tag_bind(self, None)
        assign_pending_registration(self, None)

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
                self._drop_nfc_windows_on_session_end()
                self.broadcast_sse({"event": "session_timeout"})
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
                    self._drop_nfc_windows_on_session_end()
                    self.broadcast_sse({"event": "reader_disconnected"})
                elif event.event_type == ReaderEventType.CONNECTED:
                    logger.info("NFC reader reconnected.")
                    self.broadcast_sse({"event": "reader_connected"})

    async def _dispatch_insert(
        self, uid: str, get_session, reader_name: str = ""
    ) -> None:
        """Route one insert through pending intercepts, then the tap router.

        Expired registration and bind windows are dropped so the tap is
        classified instead of consumed as a failed enroll/bind. A leftover
        overlay session is ended when an enrol window expires so the
        same tap can log in rather than log out; an expired card-replace
        window keeps the admin session (the admin is still at the panel)
        and consumes the tap after reporting its timeout.
        A live registration is claimed atomically under
        ``pending_state_lock`` so a concurrent HTTP arm/cancel cannot
        retarget the tap.
        """
        with pending_state_lock:
            pending_reg = self.pending_registration
            pending_bind = self.pending_tag_bind
            expired_reg = bool(pending_reg is not None and pending_reg.is_expired)
            expired_bind = bool(pending_bind is not None and pending_bind.is_expired)
            expired_replace_user_id = (
                pending_reg.replace_user_id
                if expired_reg and pending_reg is not None
                else None
            )
            expired_was_replace = expired_replace_user_id is not None
            if expired_reg:
                assign_pending_registration(self, None)
                pending_reg = None
            elif pending_reg is not None:
                # Claim the window: the tap below acts on this object only.
                assign_pending_registration(self, None)
            if expired_bind:
                assign_pending_tag_bind(self, None)
                pending_bind = None

        if expired_reg and not expired_was_replace:
            logger.info("Registration window expired.")
            self._end_leftover_session()
        elif expired_reg:
            logger.info("Card replace window expired.")
            self.broadcast_sse({
                "event": "registration_failed",
                "reason": "Card replace timed out. Please try again.",
                "replaced": True,
                "replace_user_id": expired_replace_user_id,
            })
            return

        if expired_bind:
            logger.info("Device tag bind window expired.")

        if pending_reg is not None:
            await self._handle_registration_tap(pending_reg, uid, get_session)
            return

        if pending_bind is not None:
            if self._uid_is_work_card(uid, get_session):
                # Bind is for stickers. A work card must still log in (or stay
                # logged in) rather than consuming the window as tag_bind_failed.
                if self.session_mgr.has_active_session:
                    logger.info(
                        "Work card tapped during device-tag bind; bind window kept."
                    )
                    return
                logger.info(
                    "Work card login during device-tag bind; bind window will clear."
                )
            elif self._uid_is_borrowed_device_tag(uid, get_session):
                logger.info(
                    "Borrowed device tag during bind; unattended return wins."
                )
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
            assign_pending_tag_bind(self, None)

        sse = result.to_sse()
        if sse is not None:
            self.broadcast_sse(sse)

    async def _handle_registration_tap(
        self, pending: PendingRegistration | None, uid: str, get_session
    ) -> None:
        """Enroll or re-card a user when a card is tapped during pending registration.

        Validates that the registration has not expired and the card is not
        already enrolled, then creates the user record (or moves an existing
        user to the tapped card when ``replace_user_id`` is set) and pushes a
        success or failure SSE event to the frontend. The enrol path always
        ends a leftover overlay session so the next work-card tap is login,
        not logout; the replace path keeps the admin session on the Users
        overlay. Replace-path results (success and failure) carry ``replaced``
        and ``replace_user_id`` so the frontend can route them to the Users
        overlay without depending on request timing. The enrol path holds
        ``user_admin_lock`` across the deactivated-name recheck and the
        enroll commit so an admin deactivation cannot slip between them.

        Args:
            pending: The claimed registration window (``_dispatch_insert``
                already cleared it from ``self.pending_registration``).
            uid: Hex-encoded card UID from the NFC reader.
            get_session: Callable returning a SQLAlchemy session context manager.

        Returns:
            None. Result is pushed to the SSE queue.
        """
        from smart_locker.database.repositories import DeviceRepository, UserRepository
        from smart_locker.security.hashing import compute_uid_hmac
        from smart_locker.security.key_manager import key_manager
        from smart_locker.services.user_admin_lock import user_admin_lock
        from smart_locker.services.user_service import UserService, name_is_deactivated

        is_replace = pending is not None and pending.replace_user_id is not None
        try:
            if pending is None:
                return

            if pending.is_expired:
                logger.info("Registration expired for '%s'.", pending.display_name)
                payload = {
                    "event": "registration_failed",
                    "reason": "Registration timed out. Please try again.",
                }
                if is_replace:
                    payload["replaced"] = True
                    payload["replace_user_id"] = pending.replace_user_id
                self.broadcast_sse(payload)
                return

            user_svc = UserService(
                enc_key=key_manager.enc_key, hmac_key=key_manager.hmac_key
            )

            if is_replace:
                try:
                    with get_session() as db_session:
                        target = UserRepository.find_by_id(
                            db_session, pending.replace_user_id
                        )
                        if target is None or not target.is_active:
                            self.broadcast_sse({
                                "event": "registration_failed",
                                "reason": "User not found.",
                                "replaced": True,
                                "replace_user_id": pending.replace_user_id,
                            })
                            return
                        user_svc.replace_card(db_session, target, uid)
                        logger.info(
                            "Replaced card for user id=%d.", target.id
                        )
                        self.broadcast_sse({
                            "event": "registration_success",
                            "user": {
                                "id": target.id,
                                "name": target.display_name,
                                "role": target.role.value,
                            },
                            "replaced": True,
                            "replace_user_id": pending.replace_user_id,
                        })
                except ValueError as e:
                    self.broadcast_sse({
                        "event": "registration_failed",
                        "reason": str(e),
                        "replaced": True,
                        "replace_user_id": pending.replace_user_id,
                    })
                except Exception:
                    logger.exception(
                        "Card replace failed for '%s'.", pending.display_name
                    )
                    self.broadcast_sse({
                        "event": "registration_failed",
                        "reason": "Registration failed. Please try again.",
                        "replaced": True,
                        "replace_user_id": pending.replace_user_id,
                    })
                return

            try:
                with get_session() as db_session:
                    uid_hmac = compute_uid_hmac(uid, key_manager.hmac_key)
                    if DeviceRepository.find_by_tag_hmac(db_session, uid_hmac) is not None:
                        logger.warning("Registration failed: UID is already a device tag.")
                        self.broadcast_sse({
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
                        self.broadcast_sse({
                            "event": "registration_failed",
                            "reason": "This card is already registered.",
                        })
                        return

                    # Re-check at completion under user_admin_lock (shared
                    # with the admin deactivate endpoint): a deactivation
                    # committing between this SELECT and the INSERT below
                    # would otherwise let a deactivated name re-enrol. The
                    # commit lands inside the lock — get_session would only
                    # commit after the block exits. The outcome is computed
                    # under the lock and broadcast after release
                    # (broadcast_sse takes _sse_lock).
                    with user_admin_lock:
                        # Drop pre-lock reads so the recheck sees a
                        # deactivation committed just before the lock.
                        db_session.expire_all()
                        if name_is_deactivated(db_session, pending.display_name):
                            enrolled: dict | None = None
                        else:
                            user = user_svc.enroll_user(
                                db_session,
                                display_name=pending.display_name,
                                card_uid_hex=uid,
                                role=pending.role,
                            )
                            try:
                                db_session.commit()
                            except Exception:
                                db_session.rollback()
                                raise
                            enrolled = {
                                "id": user.id,
                                "name": user.display_name,
                                "role": user.role.value,
                            }

                    if enrolled is None:
                        logger.warning(
                            "Registration failed: name '%s' is deactivated.",
                            pending.display_name,
                        )
                        self.broadcast_sse({
                            "event": "registration_failed",
                            "reason": "This name is deactivated. Ask an admin to re-enrol under a new name.",
                        })
                        return

                    logger.info(
                        "Self-registered user: %s (id=%d, role=%s)",
                        enrolled["name"],
                        enrolled["id"],
                        enrolled["role"],
                    )
                    self.broadcast_sse({
                        "event": "registration_success",
                        "user": enrolled,
                        "replaced": False,
                    })
            except Exception:
                logger.exception("Registration failed for '%s'.", pending.display_name)
                self.broadcast_sse({
                    "event": "registration_failed",
                    "reason": "Registration failed. Please try again.",
                })
        finally:
            if not is_replace:
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

    def _uid_is_borrowed_device_tag(self, uid: str, get_session) -> bool:
        """Whether this UID is a sticker on a currently borrowed locker device.

        Unattended return of a borrowed sticker must win over an armed bind
        window so the slot overlay still runs. Lookup failures are treated
        as not-borrowed so a new/available tag can still bind.

        Args:
            uid: Hex-encoded UID from the NFC reader.
            get_session: Callable returning a SQLAlchemy session context manager.

        Returns:
            True if the UID matches a device whose status is BORROWED.
        """
        try:
            from smart_locker.database.models import DeviceStatus
            from smart_locker.database.repositories import DeviceRepository
            from smart_locker.security.hashing import compute_uid_hmac
            from smart_locker.security.key_manager import key_manager

            digest = compute_uid_hmac(uid, key_manager.hmac_key)
            with get_session() as db_session:
                device = DeviceRepository.find_by_tag_hmac(db_session, digest)
            return device is not None and device.status == DeviceStatus.BORROWED
        except Exception:
            logger.exception("Borrowed-tag lookup failed during tag-bind intercept.")
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

        with pending_state_lock:
            pending = self.pending_tag_bind
            assign_pending_tag_bind(self, None)
        if pending is None:
            return

        self.session_mgr.touch()

        if pending.is_expired:
            logger.info("Device tag bind timed out for device_id=%d.", pending.device_id)
            self.broadcast_sse({
                "event": "tag_bind_failed",
                "reason": "Bind timed out. Please try again.",
            })
            return

        try:
            with get_session() as db_session:
                device = DeviceRepository.find_by_id(db_session, pending.device_id)
                if device is None:
                    self.broadcast_sse({
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
                self.broadcast_sse({
                    "event": "tag_bind_success",
                    "device_id": device.id,
                    "device_name": device.name,
                    "pm_number": device.pm_number,
                })
        except ValueError as e:
            self.broadcast_sse({
                "event": "tag_bind_failed",
                "reason": str(e),
            })
        except Exception:
            logger.exception(
                "Device tag bind failed for device_id=%d.", pending.device_id
            )
            self.broadcast_sse({
                "event": "tag_bind_failed",
                "reason": "Bind failed. Please try again.",
            })


# Module-level singleton — initialized by server.py lifespan
context: AppContext | None = None
