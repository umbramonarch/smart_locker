"""
File: conftest.py
Description: Shared pytest fixtures for the Smart Locker test suite. Provides
             deterministic test keys, in-memory SQLite sessions, and reusable
             encryption/HMAC key fixtures.
Project: smart_locker/tests
Notes: All tests use in-memory SQLite — no disk database or NFC hardware needed.
       The _set_test_keys fixture runs automatically for every test.
"""

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


@pytest.fixture(autouse=True)
def _disable_location_writeback(monkeypatch):
    """Do not write a developer SOURCE_EXCEL_PATH during tests.

    Borrow/return schedule Location write-back against settings. Tests that
    need write-back monkeypatch SOURCE_EXCEL_PATH onto a temp workbook.
    """
    monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", "")


@pytest.fixture(autouse=True)
def _default_site_overlay(monkeypatch):
    """Ignore a developer .env overlay so tests see built-in Excel/UI defaults."""
    monkeypatch.delenv("SMART_LOCKER_ASSET_LABEL", raising=False)
    monkeypatch.delenv("SMART_LOCKER_ID_HEADERS", raising=False)
    monkeypatch.delenv("SMART_LOCKER_LOCATION_HEADERS", raising=False)
    monkeypatch.delenv("SMART_LOCKER_IN_LOCKER_TOKEN", raising=False)
    monkeypatch.delenv("SMART_LOCKER_PUBLIC_URL", raising=False)
    monkeypatch.delenv("SMART_LOCKER_DASHBOARD_SHARE_PATH", raising=False)
    monkeypatch.delenv("SMART_LOCKER_DASHBOARD_ADMIN_SECRET", raising=False)


@pytest.fixture(autouse=True)
def _isolate_last_sync(tmp_path, monkeypatch):
    """Keep last-sync JSON off the repo disk and reset in-memory state per test."""
    monkeypatch.setenv("SMART_LOCKER_LAST_SYNC_PATH", str(tmp_path / "last_sync.json"))
    from smart_locker.sync import sync_status
    sync_status.reset()
    yield
    sync_status.reset()


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
        session.close()
        reset_engine()


@pytest.fixture()
def enc_key():
    """Provide a deterministic 32-byte AES encryption key for tests."""
    return b"\x01" * 32


@pytest.fixture()
def hmac_key():
    """Provide a deterministic 32-byte HMAC key for tests."""
    return b"\x02" * 32
