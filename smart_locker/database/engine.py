"""
File: engine.py
Description: SQLAlchemy engine and session factory. Uses SQLite with WAL journal
             mode for read concurrency and provides a plain sessionmaker; each
             caller creates and closes its own Session (a scoped_session is unsafe
             under FastAPI's reused thread pool — its thread-local remove() can
             close another request's session mid-commit).
Project: smart_locker/database
Notes: The engine and session factory are module-level singletons. Use
       reset_engine() in tests to tear down between test cases.
"""

import logging
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from config.settings import DATABASE_URL
from smart_locker.database.models import Base

logger = logging.getLogger(__name__)

# Module-level singletons — lazily created by get_engine() / get_session_factory()
_engine = None
_session_factory = None


def get_engine(url: str | None = None):
    """Create or return the singleton engine."""
    global _engine
    if _engine is None:
        db_url = url or DATABASE_URL
        engine_kwargs: dict = {"echo": False}
        if db_url.startswith("sqlite"):
            # NFC dispatch and Location write-back run on worker threads.
            engine_kwargs["connect_args"] = {"check_same_thread": False}
            if ":memory:" in db_url:
                engine_kwargs["poolclass"] = StaticPool
        _engine = create_engine(db_url, **engine_kwargs)

        # Enable WAL mode for SQLite
        if db_url.startswith("sqlite"):

            @event.listens_for(_engine, "connect")
            def _set_sqlite_pragma(dbapi_conn, _connection_record):
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                # Writers retry a locked db instead of failing — the background
                # source import can overlap tap and HTTP commits.
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.close()

        logger.info("Database engine created: %s", db_url)
    return _engine


def get_session_factory(url: str | None = None) -> sessionmaker[Session]:
    """Create or return the session factory.

    Returns a plain ``sessionmaker``, NOT a ``scoped_session``. FastAPI runs sync
    handlers on a reused thread pool, and ``scoped_session.remove()`` in a request
    teardown closes whatever session is bound to the recycled thread — which can
    be a *different* request's session mid-commit, raising
    ``IllegalStateChangeError`` (an intermittent 500). Each caller instead creates
    a session with ``factory()`` and closes that specific session in a ``finally``.
    """
    global _session_factory
    if _session_factory is None:
        engine = get_engine(url)
        _session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    return _session_factory


@contextmanager
def get_session(url: str | None = None) -> Generator[Session, None, None]:
    """Context manager that yields a session with auto-commit/rollback."""
    factory = get_session_factory(url)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db(url: str | None = None) -> None:
    """Create all tables."""
    engine = get_engine(url)
    Base.metadata.create_all(engine)
    logger.info("Database tables created.")


def reset_engine() -> None:
    """Reset the engine and session factory (useful for tests)."""
    global _engine, _session_factory
    if _session_factory is not None:
        # A plain sessionmaker has no registry to clear; some tests inject a
        # scoped_session, which does — clear it if present.
        remove = getattr(_session_factory, "remove", None)
        if callable(remove):
            remove()
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
