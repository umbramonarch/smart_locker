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

    Created when a user submits their name on the registration screen. The
    registration is valid for ``REGISTRATION_TIMEOUT_SECONDS`` (60s) — if no
    card is tapped before then, the attempt expires and the user must retry.
    ``role`` is "admin" only for first-boot Setup; every other path keeps
    the default "user". ``replace_user_id`` turns the window into a card
    replacement: the tapped card rebinds that existing user instead of
    enrolling a new one (``display_name`` stays as the screen label).
    ``from_dashboard`` marks a secret-armed window so public
    ``POST /api/register/cancel`` cannot clear it.
    """

    display_name: str
    created_at: float = field(default_factory=time.monotonic)
    role: str = "user"
    replace_user_id: int | None = None
    from_dashboard: bool = False

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


def drop_expired_pending(ctx) -> None:
    """Clear expired registration/bind windows. Caller holds the lock.

    Shared by the HTTP-side conflict checks and the NFC bridge so "expired"
    means the same thing on both sides of ``pending_state_lock``.
    """
    reg = ctx.pending_registration
    if reg is not None and reg.is_expired:
        ctx.pending_registration = None
    bind = ctx.pending_tag_bind
    if bind is not None and bind.is_expired:
        ctx.pending_tag_bind = None


def arm_pending_registration(ctx, pending: PendingRegistration) -> str | None:
    """Atomically take the reader for a registration, or report the conflict.

    Expired windows are dropped, then any live registration or bind window
    blocks the arm — all under one lock hold so two racing arms cannot both
    succeed (the loser's window silently replacing the winner's).

    Args:
        ctx: Application context (or test double with the same attribute).
        pending: The pending registration to arm.

    Returns:
        A conflict detail string when a non-expired window already owns the
        reader, else None (armed).
    """
    with pending_state_lock:
        drop_expired_pending(ctx)
        if ctx.pending_registration is not None:
            return "A registration is already waiting for a card tap."
        if ctx.pending_tag_bind is not None:
            return "A device-tag bind is already waiting for a sticker tap."
        ctx.pending_registration = pending
        return None


def arm_pending_tag_bind(ctx, bind: PendingTagBind) -> str | None:
    """Atomically take the reader for a tag bind, or report the conflict.

    Same contract as :func:`arm_pending_registration` for ``PendingTagBind``.
    A pending registration is NOT cleared — it wins the conflict instead.

    Args:
        ctx: Application context (or test double with the same attribute).
        bind: The pending bind to arm.

    Returns:
        A conflict detail string when a non-expired window already owns the
        reader, else None (armed).
    """
    with pending_state_lock:
        drop_expired_pending(ctx)
        if ctx.pending_registration is not None:
            return "A registration is already waiting for a card tap."
        if ctx.pending_tag_bind is not None:
            return "A device-tag bind is already waiting for a sticker tap."
        ctx.pending_tag_bind = bind
        return None


def clear_pending_tag_bind_if(ctx, bind: PendingTagBind) -> None:
    """Clear ``pending_tag_bind`` only when it is still ``bind`` (same object).

    A bind window another request armed after ``bind`` was consumed survives —
    a commit-failure cleanup must not kill it.

    Args:
        ctx: Application context (or test double with the same attribute).
        bind: The pending bind this caller armed.
    """
    with pending_state_lock:
        if ctx.pending_tag_bind is bind:
            ctx.pending_tag_bind = None


def clear_pending_tag_bind_for_device(ctx, device_id: int) -> None:
    """Clear ``pending_tag_bind`` only when it targets ``device_id``.

    An unbind may cancel only a bind window aimed at that same device — a
    window armed for a different device survives.

    Args:
        ctx: Application context (or test double with the same attribute).
        device_id: Device the caller is unbinding.
    """
    with pending_state_lock:
        bind = ctx.pending_tag_bind
        if bind is not None and bind.device_id == device_id:
            ctx.pending_tag_bind = None


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

    def end_kiosk_session(
        self, *, event: str = "session_ended", reason: str | None = None,
        emit: bool = True,
        expected_pending: tuple[
            PendingRegistration | None, PendingTagBind | None
        ] | None = None,
    ) -> None:
        """Clear all kiosk session-scoped state and optionally publish its end event.

        When ``expected_pending`` is given (the dispatch snapshot), a pending
        window is cleared only if it is still that exact object — a window an
        HTTP route armed after the snapshot survives. Without it, both pending
        windows are cleared unconditionally (session end, timeout, reader
        disconnect).
        """
        self.session_mgr.end_session()
        self.admin_overlay_open = False
        if expected_pending is None:
            assign_pending_registration(self, None)
            assign_pending_tag_bind(self, None)
        else:
            exp_reg, exp_bind = expected_pending
            with pending_state_lock:
                if self.pending_registration is exp_reg:
                    assign_pending_registration(self, None)
                if self.pending_tag_bind is exp_bind:
                    assign_pending_tag_bind(self, None)
        if emit:
            payload = {"event": event}
            if reason is not None:
                payload["reason"] = reason
            self.broadcast_sse(payload)

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
                self.end_kiosk_session(event="session_timeout")
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

                try:
                    await self._dispatch_insert(
                        event.uid, get_session, event.reader_name
                    )
                except Exception:
                    # A dispatch failure must never kill the bridge — the next
                    # tap is still processed. The registration/bind paths report
                    # their own *_failed SSE inside _dispatch_insert.
                    logger.exception("NFC dispatch failed; bridge continues.")

            elif isinstance(event, ReaderEvent):
                if event.event_type == ReaderEventType.DISCONNECTED:
                    logger.warning("NFC reader disconnected.")
                    self.end_kiosk_session(event="reader_disconnected")
                elif event.event_type == ReaderEventType.CONNECTED:
                    logger.info("NFC reader reconnected.")
                    self.broadcast_sse({"event": "reader_connected"})

    async def _dispatch_insert(
        self, uid: str, get_session, reader_name: str = ""
    ) -> None:
        """Dispatch one insert through the application tap policy.

        Pending windows are snapshotted and expired windows invalidated under
        the shared lock. The router receives only scalar snapshots and one
        database session; this context applies its typed state/SSE outcome.
        """
        with pending_state_lock:
            pending_reg = self.pending_registration
            pending_bind = self.pending_tag_bind
            expired_reg = bool(pending_reg is not None and pending_reg.is_expired)
            expired_bind = bool(pending_bind is not None and pending_bind.is_expired)
            if expired_reg:
                assign_pending_registration(self, None)
                pending_reg = None
            if expired_bind:
                assign_pending_tag_bind(self, None)
                pending_bind = None

        if expired_reg:
            logger.info("Registration window expired.")
            # An expired window also drops a leftover admin overlay (the old
            # _end_leftover_session call site). dispatch_insert ends the
            # session itself so this tap can still log in.
            self.admin_overlay_open = False
        if expired_bind:
            logger.info("Device tag bind window expired.")

        from smart_locker.auth.tap_router import dispatch_insert
        from smart_locker.security.key_manager import key_manager

        overlay = self.admin_overlay_open
        session_mgr = self.session_mgr
        hmac_key = key_manager.hmac_key
        pending_snapshots = (pending_reg, pending_bind)

        def _run_insert():
            with get_session() as db_session:
                return dispatch_insert(
                    db_session,
                    uid,
                    hmac_key,
                    # The AES key is needed only to enroll; resolving it lazily
                    # keeps a missing SMART_LOCKER_ENC_KEY from failing taps.
                    key_manager.enc_key if pending_reg is not None else None,
                    session_mgr,
                    registration_display_name=(
                        pending_reg.display_name if pending_reg is not None else None
                    ),
                    registration_role=(
                        pending_reg.role if pending_reg is not None else "user"
                    ),
                    registration_replace_user_id=(
                        pending_reg.replace_user_id
                        if pending_reg is not None
                        else None
                    ),
                    registration_expired=expired_reg,
                    tag_bind_device_id=(
                        pending_bind.device_id if pending_bind is not None else None
                    ),
                    admin_overlay_open=overlay,
                    reader_name=reader_name,
                )

        try:
            outcome = await asyncio.to_thread(_run_insert)
        except Exception:
            # Includes the get_session() auto-commit: a handler exception is
            # rolled back inside dispatch_insert, but a commit-level failure
            # (locked DB, IO error) lands here. The bridge must survive and the
            # kiosk must not sit on "waiting for card" forever.
            logger.exception("NFC tap dispatch failed; reporting to kiosk.")
            self._report_dispatch_failure(pending_reg, pending_bind)
            return
        result = outcome.result

        if outcome.clear_pending_registration:
            with pending_state_lock:
                if self.pending_registration is pending_reg:
                    assign_pending_registration(self, None)
        if outcome.clear_pending_tag_bind:
            with pending_state_lock:
                if self.pending_tag_bind is pending_bind:
                    assign_pending_tag_bind(self, None)

        if outcome.end_leftover_session_silently:
            self._end_leftover_session(expected_pending=pending_snapshots)
        elif result.event == "session_ended":
            self.end_kiosk_session(
                reason=result.payload.get("reason", "card_tap"), emit=False,
                expected_pending=pending_snapshots,
            )
        elif result.event == "auth_success":
            self.admin_overlay_open = False

        sse = result.to_sse()
        if sse is not None:
            self.broadcast_sse(sse)

    def _report_dispatch_failure(
        self,
        pending_reg: PendingRegistration | None,
        pending_bind: PendingTagBind | None,
    ) -> None:
        """Emit the same ``*_failed`` SSE the armed window's flow would produce.

        Called when the dispatch transaction itself failed (commit error, key
        load, unexpected fault). Without this the kiosk would sit on "waiting
        for card" until the window expires, and the bridge would have died.
        """
        if pending_reg is not None:
            self._end_leftover_session(
                expected_pending=(pending_reg, pending_bind)
            )
            self.broadcast_sse({
                "event": "registration_failed",
                "reason": "Registration failed. Please try again.",
            })
        elif pending_bind is not None:
            with pending_state_lock:
                if self.pending_tag_bind is pending_bind:
                    assign_pending_tag_bind(self, None)
            self.broadcast_sse({
                "event": "tag_bind_failed",
                "reason": "Bind failed. Please try again.",
            })
        else:
            self.broadcast_sse({
                "event": "device_action",
                "success": False,
                "action": "error",
                "message": "Something went wrong. Please try again.",
            })

    def _end_leftover_session(
        self,
        *,
        expected_pending: tuple[
            PendingRegistration | None, PendingTagBind | None
        ] | None = None,
    ) -> None:
        """Drop a leftover overlay session with no session_ended SSE.

        After admin Register User the overlay session must not remain, or
        the next work-card tap is logout instead of login. The register
        success/fail screen stays until the frontend navigates to idle.
        """
        self.end_kiosk_session(emit=False, expected_pending=expected_pending)



# Module-level singleton — initialized by server.py lifespan
context: AppContext | None = None
