"""SQLAlchemy engine and session factory.

Uses SQLite with WAL mode for read concurrency. Provides a scoped_session
for thread safety (NFC monitor runs on a background thread).
"""

import logging
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, scoped_session, sessionmaker

from config.settings import DATABASE_URL
from smart_locker.database.models import Base

logger = logging.getLogger(__name__)

_engine = None
_session_factory = None


def get_engine(url: str | None = None):
    """Create or return the singleton engine."""
    global _engine
    if _engine is None:
        db_url = url or DATABASE_URL
        _engine = create_engine(db_url, echo=False)

        # Enable WAL mode for SQLite
        if db_url.startswith("sqlite"):

            @event.listens_for(_engine, "connect")
            def _set_sqlite_pragma(dbapi_conn, _connection_record):
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

        logger.info("Database engine created: %s", db_url)
    return _engine


def get_session_factory(url: str | None = None) -> scoped_session[Session]:
    """Create or return the scoped session factory."""
    global _session_factory
    if _session_factory is None:
        engine = get_engine(url)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        _session_factory = scoped_session(factory)
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
        factory.remove()


def init_db(url: str | None = None) -> None:
    """Create all tables."""
    engine = get_engine(url)
    Base.metadata.create_all(engine)
    logger.info("Database tables created.")


def reset_engine() -> None:
    """Reset the engine and session factory (useful for tests)."""
    global _engine, _session_factory
    if _session_factory is not None:
        _session_factory.remove()
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
