"""Single-user session lifecycle management (kiosk pattern).

Only one user session is active at a time. Session ends on card removal,
explicit logout, or inactivity timeout.
"""

import logging
import time
from dataclasses import dataclass, field

from config.settings import SESSION_TIMEOUT_SECONDS
from smart_locker.database.models import User

logger = logging.getLogger(__name__)


@dataclass
class UserSession:
    """Represents an active user session."""

    user: User
    started_at: float = field(default_factory=time.monotonic)
    last_activity: float = field(default_factory=time.monotonic)
    timeout_seconds: int = SESSION_TIMEOUT_SECONDS

    @property
    def is_expired(self) -> bool:
        return (time.monotonic() - self.last_activity) > self.timeout_seconds

    def touch(self) -> None:
        """Reset inactivity timer (called on each UI interaction)."""
        self.last_activity = time.monotonic()

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_activity


class SessionManager:
    """Manages the single active user session."""

    def __init__(self, timeout_seconds: int | None = None) -> None:
        self._timeout = timeout_seconds if timeout_seconds is not None else SESSION_TIMEOUT_SECONDS
        self._current: UserSession | None = None

    @property
    def current_session(self) -> UserSession | None:
        if self._current is not None and self._current.is_expired:
            logger.info(
                "Session expired for %s (idle %.0fs)",
                self._current.user.display_name,
                self._current.idle_seconds,
            )
            self._current = None
        return self._current

    @property
    def has_active_session(self) -> bool:
        return self.current_session is not None

    def start_session(self, user: User) -> UserSession:
        """Start a new session, ending any existing one."""
        if self._current is not None:
            self.end_session()

        self._current = UserSession(user=user, timeout_seconds=self._timeout)
        logger.info("Session started for %s (id=%d)", user.display_name, user.id)
        return self._current

    def end_session(self) -> None:
        """End the current session explicitly (card tap / frontend logout)."""
        if self._current is not None:
            logger.info(
                "Session ended for %s (duration=%.0fs)",
                self._current.user.display_name,
                self._current.elapsed_seconds,
            )
            self._current = None

    def touch(self) -> None:
        """Reset inactivity timer on the current session."""
        if self._current is not None:
            self._current.touch()
