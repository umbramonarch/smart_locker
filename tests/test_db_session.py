"""
File: test_db_session.py
Description: Regression tests for database session handling. Locks in the fix for
             an intermittent ``IllegalStateChangeError`` 500: the request session
             factory must be a plain ``sessionmaker``, NOT a ``scoped_session``.
             FastAPI runs sync handlers on a reused thread pool, and
             ``scoped_session.remove()`` in a per-request teardown closes whatever
             session is bound to the recycled thread — sometimes another request's
             session mid-commit. Callers now close their own specific session.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_db_session.py -v
"""

from sqlalchemy import select
from sqlalchemy.orm import scoped_session, sessionmaker

from smart_locker.database.engine import (
    get_session,
    get_session_factory,
    init_db,
    reset_engine,
)

_URL = "sqlite:///:memory:"


def test_request_factory_is_plain_sessionmaker():
    """The shared request factory must be a sessionmaker, not a scoped_session."""
    reset_engine()
    try:
        factory = get_session_factory(_URL)
        assert isinstance(factory, sessionmaker)
        assert not isinstance(factory, scoped_session)
    finally:
        reset_engine()


def test_get_session_is_reentrant_and_closes():
    """get_session must yield a usable session and close cleanly each time, so
    repeated use never trips the 'commit() already in progress' state change."""
    reset_engine()
    init_db(_URL)
    try:
        for _ in range(3):
            with get_session(_URL) as s:
                assert s.execute(select(1)).scalar() == 1
    finally:
        reset_engine()
