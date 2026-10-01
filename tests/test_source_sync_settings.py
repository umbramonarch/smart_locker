"""
File: test_source_sync_settings.py
Description: Configuration behavior for source-workbook polling.
Project: smart_locker/tests
"""

from config.settings import _env_int


def test_source_sync_interval_minutes_defaults_to_five_and_clamps_to_one(monkeypatch):
    """The source poll is responsive by default and never becomes a busy loop."""
    name = "SMART_LOCKER_SOURCE_SYNC_INTERVAL_MINUTES"

    monkeypatch.delenv(name, raising=False)
    assert _env_int(name, 5, minimum=1) == 5

    monkeypatch.setenv(name, "0")
    assert _env_int(name, 5, minimum=1) == 1

    monkeypatch.setenv(name, "12")
    assert _env_int(name, 5, minimum=1) == 12
