"""
File: test_auth.py
Description: Tests for authentication and session management. Validates HMAC-based
             card UID lookup, inactive user rejection, session lifecycle (start,
             expire, end), and inactivity timeout behaviour.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_auth.py -v
"""

import time

import pytest

from smart_locker.auth.authenticator import Authenticator
from smart_locker.auth.session_manager import SessionManager, UserSession
from smart_locker.database.models import User, UserRole
from smart_locker.database.repositories import UserRepository
from smart_locker.security.encryption import encrypt
from smart_locker.security.hashing import compute_uid_hmac


class TestAuthenticator:
    """Tests for HMAC-based card UID authentication and inactive user rejection."""
    def test_authenticate_known_user(self, db_session, enc_key, hmac_key):
        """Verify that a known, active user is returned when their card is tapped."""
        uid = "A1B2C3D4"
        UserRepository.create(
            db_session,
            display_name="Alice",
            uid_hmac=compute_uid_hmac(uid, hmac_key),
            encrypted_card_uid=encrypt(uid, enc_key),
        )
        db_session.flush()

        auth = Authenticator(hmac_key=hmac_key)
        user = auth.authenticate(db_session, uid)
        assert user is not None
        assert user.display_name == "Alice"

    def test_authenticate_unknown_card(self, db_session, hmac_key):
        """Verify that an unregistered card UID returns None (no match)."""
        auth = Authenticator(hmac_key=hmac_key)
        user = auth.authenticate(db_session, "UNKNOWN")
        assert user is None

    def test_authenticate_inactive_user(self, db_session, enc_key, hmac_key):
        """Verify that a deactivated user's card tap returns None (rejected)."""
        uid = "DEADBEEF"
        user = UserRepository.create(
            db_session,
            display_name="Deactivated",
            uid_hmac=compute_uid_hmac(uid, hmac_key),
            encrypted_card_uid=encrypt(uid, enc_key),
        )
        user.is_active = False
        db_session.flush()

        auth = Authenticator(hmac_key=hmac_key)
        result = auth.authenticate(db_session, uid)
        assert result is None


class TestSessionManager:
    """Tests for session lifecycle — start, end, timeout, touch, and replacement."""
    def _make_user(self, db_session, name="Test User", uid="AABB"):
        """Create a User via the database so SQLAlchemy state is initialized."""
        user = UserRepository.create(
            db_session,
            display_name=name,
            uid_hmac=compute_uid_hmac(uid, b"\x02" * 32),
            encrypted_card_uid=encrypt(uid, b"\x01" * 32),
        )
        db_session.flush()
        return user

    def test_start_and_end_session(self, db_session):
        """Verify session start creates an active session and end clears it."""
        mgr = SessionManager(timeout_seconds=60)
        user = self._make_user(db_session)

        session = mgr.start_session(user)
        assert mgr.has_active_session
        assert session.user.display_name == "Test User"

        mgr.end_session()
        assert not mgr.has_active_session

    def test_session_timeout(self, db_session):
        """Verify a session with zero timeout expires immediately after inactivity."""
        mgr = SessionManager(timeout_seconds=0)  # Immediate timeout
        user = self._make_user(db_session)

        session = mgr.start_session(user)
        # Force the last_activity far enough into the past
        session.last_activity = time.monotonic() - 1
        assert not mgr.has_active_session  # Expired

    def test_touch_resets_timeout(self, db_session):
        """Verify that touch() extends the session by resetting the inactivity timer."""
        mgr = SessionManager(timeout_seconds=1)
        user = self._make_user(db_session)

        mgr.start_session(user)
        time.sleep(0.5)
        mgr.touch()
        time.sleep(0.5)
        # Should still be active because we touched it
        assert mgr.has_active_session

    def test_start_new_session_ends_old(self, db_session):
        """Verify that starting a new session automatically ends the previous one."""
        mgr = SessionManager(timeout_seconds=60)
        user1 = self._make_user(db_session, "Alice", "AAAA")
        user2 = self._make_user(db_session, "Bob", "BBBB")

        mgr.start_session(user1)
        mgr.start_session(user2)
        assert mgr.current_session.user.display_name == "Bob"
