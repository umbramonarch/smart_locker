"""
File: test_sync_status.py
Description: Tests for last-sync persistence and the local-time + relative
             display fields used by the admin footer.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_sync_status.py -v
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from smart_locker.sync import sync_status
from smart_locker.sync.scheduler import _run_source_import


def _ok_result(**overrides):
    """Build an ImportResult-shaped object for record_result."""
    data = dict(imported=1, updated=2, unchanged=3, errors=0)
    data.update(overrides)
    return SimpleNamespace(**data)


class TestPersistLastSync:
    """Last-sync must survive process restart (in-memory clear + reload from JSON)."""

    def test_get_reloads_from_disk_after_memory_cleared(self):
        """After reset(), get() still returns the last recorded import from disk."""
        sync_status.record_result("manual", _ok_result())
        first = sync_status.get()
        assert first["at"] is not None
        assert first["trigger"] == "manual"
        assert first["imported"] == 1

        sync_status.reset()
        second = sync_status.get()
        assert second["at"] == first["at"]
        assert second["trigger"] == "manual"
        assert second["imported"] == 1
        assert second["updated"] == 2
        assert second["ok"] is True

    def test_get_is_never_when_no_file_and_no_memory(self):
        """With no prior import, at stays null (admin line: never)."""
        snap = sync_status.get()
        assert snap["at"] is None
        assert snap["at_local"] is None
        assert snap["ago"] is None

    def test_missing_source_does_not_clear_persisted_sync(self):
        """A skipped import (file missing) must not wipe the last successful snapshot."""
        sync_status.record_result("startup", _ok_result(imported=0, updated=4))
        at = sync_status.get()["at"]

        _run_source_import(MagicMock(), "/nonexistent/path.xlsx")
        sync_status.reset()

        snap = sync_status.get()
        assert snap["at"] == at
        assert snap["trigger"] == "startup"
        assert snap["updated"] == 4

    def test_corrupt_file_does_not_raise(self, tmp_path, monkeypatch):
        """Unreadable JSON is treated as never-synced, not an exception."""
        path = tmp_path / "last_sync.json"
        path.write_text("{not json", encoding="utf-8")
        monkeypatch.setenv("SMART_LOCKER_LAST_SYNC_PATH", str(path))
        sync_status.reset()
        snap = sync_status.get()
        assert snap["at"] is None


class TestLocalAndRelativeClock:
    """Admin footer needs local time + relative age, not raw UTC toLocaleString."""

    def test_get_includes_at_local_and_ago(self):
        """A recorded import exposes at_local and ago strings."""
        sync_status.record_result("interval", _ok_result())
        snap = sync_status.get()
        assert snap["at"]
        assert snap["at_local"].startswith("today ")
        assert snap["ago"] == "just now"
        assert ":" in snap["at_local"]

    def test_ago_is_two_hours_when_sync_was_two_hours_ago(self):
        """Relative field is '2h ago' when the snapshot is two hours old."""
        recorded = datetime(2026, 8, 26, 12, 32, tzinfo=timezone.utc)
        clock = {"t": recorded}

        def fake_utcnow():
            return clock["t"]

        with patch("smart_locker.sync.sync_status._utcnow", fake_utcnow):
            sync_status.record_result("startup", _ok_result())
            clock["t"] = recorded + timedelta(hours=2)
            snap = sync_status.get()

        assert snap["ago"] == "2h ago"


class TestUpdateShPreservesLastSync:
    """last_sync.json must survive update.sh rsync --delete, or persist is useless."""

    def test_update_sh_preserves_last_sync_json(self):
        """deploy/install/update.sh lists last_sync.json in PRESERVE."""
        text = Path(__file__).resolve().parents[1].joinpath(
            "deploy", "install", "update.sh"
        ).read_text(encoding="utf-8")
        assert '"last_sync.json"' in text
