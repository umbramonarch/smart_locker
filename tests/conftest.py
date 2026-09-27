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
def _disable_mirror(monkeypatch):
    """Keep the catalog mirror unconfigured during tests.

    Borrow/return and dashboard edits schedule a mirror flush against
    ``mirror_path()``. Tests that exercise the mirror point a path at a
    temp workbook on purpose. DB_PATH is faked to :memory: (the test
    engines are) so the "mirror next to the database" default resolves
    to None instead of a file inside the repo.
    """
    monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", "")
    monkeypatch.setattr("config.settings.DB_PATH", ":memory:")
    monkeypatch.delenv("SMART_LOCKER_MIRROR_PATH", raising=False)
    monkeypatch.delenv("SMART_LOCKER_SOURCE_EXCEL_PATH", raising=False)


@pytest.fixture(autouse=True)
def _isolate_mirror_state(tmp_path, monkeypatch):
    """Keep mirror_state.json off the repo disk for every test."""
    monkeypatch.setenv(
        "SMART_LOCKER_MIRROR_STATE_PATH", str(tmp_path / "mirror_state.json")
    )


@pytest.fixture(autouse=True)
def _join_mirror_flushers():
    """Join scheduled mirror-flush threads before/after each test.

    A worker spawned by ``schedule_flush`` that outlives its test would run
    workbook I/O against the next test's (or a disposed) database — and on
    Windows a query on a torn-down in-memory engine is a hard crash.
    """
    from smart_locker.sync import mirror

    mirror.flush_scheduled()
    yield
    mirror.flush_scheduled()


@pytest.fixture(autouse=True)
def _default_site_overlay(monkeypatch):
    """Ignore a developer .env overlay so tests see built-in catalog/UI defaults."""
    monkeypatch.delenv("SMART_LOCKER_ASSET_LABEL", raising=False)
    monkeypatch.delenv("SMART_LOCKER_ID_HEADERS", raising=False)
    monkeypatch.delenv("SMART_LOCKER_LOCATION_HEADERS", raising=False)
    monkeypatch.delenv("SMART_LOCKER_IN_LOCKER_TOKEN", raising=False)
    monkeypatch.delenv("SMART_LOCKER_MAINTENANCE_TOKEN", raising=False)
    monkeypatch.delenv("SMART_LOCKER_PHOTO_INPUT_PATH", raising=False)
    monkeypatch.delenv("SMART_LOCKER_PUBLIC_URL", raising=False)
    monkeypatch.delenv("SMART_LOCKER_DASHBOARD_SHARE_PATH", raising=False)
    monkeypatch.delenv("SMART_LOCKER_DASHBOARD_ADMIN_SECRET", raising=False)


@pytest.fixture(autouse=True)
def _isolate_dashboard_secret_path(tmp_path, monkeypatch):
    """Point the Setup-written dashboard.secret file at a per-test temp path.

    Without this, a test that arms Setup would write the admin password into
    the repository root — and a stale file would leak a configured secret
    into every later test.
    """
    monkeypatch.setenv(
        "SMART_LOCKER_DASHBOARD_SECRET_PATH", str(tmp_path / "dashboard.secret")
    )


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
