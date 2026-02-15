"""Shared test fixtures."""

import os
import base64

import pytest

from smart_locker.database.engine import get_engine, get_session_factory, init_db, reset_engine


@pytest.fixture(autouse=True)
def _set_test_keys(monkeypatch):
    """Provide deterministic test keys via environment variables."""
    enc_key = base64.b64encode(b"\x01" * 32).decode()
    hmac_key = base64.b64encode(b"\x02" * 32).decode()
    monkeypatch.setenv("SMART_LOCKER_ENC_KEY", enc_key)
    monkeypatch.setenv("SMART_LOCKER_HMAC_KEY", hmac_key)


@pytest.fixture()
def db_session():
    """Provide an in-memory SQLite session for testing."""
    reset_engine()
    url = "sqlite:///:memory:"
    init_db(url)
    factory = get_session_factory(url)
    session = factory()
    try:
        yield session
        session.rollback()
    finally:
        factory.remove()
        reset_engine()


@pytest.fixture()
def enc_key():
    return b"\x01" * 32


@pytest.fixture()
def hmac_key():
    return b"\x02" * 32
