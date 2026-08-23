"""
File: test_sync_resilience.py
Description: Resilience tests for the sync subsystem. The Pi appliance sits
             unattended in a locker, so a sync failure must NEVER crash the
             running kiosk. These tests lock in that contract: an Excel export
             whose target is unreachable (the locker share is down) and a
             source import whose file is missing both degrade gracefully —
             logging and returning — instead of raising.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_sync_resilience.py -v
       Uses the in-memory db_session fixture; no disk DB or hardware needed.
"""

from smart_locker.database.engine import get_engine
from smart_locker.sync.excel_sync import export_to_excel
from smart_locker.sync.source_import import import_from_source_excel


class TestExportResilience:
    """export_to_excel must never raise when the target is unreachable."""

    def test_export_to_unreachable_drive_does_not_raise(self, db_session):
        """A down network share (unmapped drive) is skipped, not fatal."""
        # Z: is an unmapped drive on the CI/dev box — stands in for the locker share
        # CIFS share being offline. The call must return without raising.
        export_to_excel(get_engine(), "Z:/__offline__/locker_data.xlsx")

    def test_export_to_missing_parent_dir_does_not_raise(self, db_session, tmp_path):
        """A target whose parent directory does not exist is skipped, not fatal."""
        target = tmp_path / "no_such_subdir" / "locker_data.xlsx"
        export_to_excel(get_engine(), target)
        # Nothing was written and, crucially, nothing was raised.
        assert not target.exists()

    def test_export_to_valid_path_still_writes(self, db_session, tmp_path):
        """The hardening must not break the happy path — a real target is written."""
        target = tmp_path / "locker_data.xlsx"
        export_to_excel(get_engine(), target)
        assert target.exists()


class TestImportResilience:
    """import_from_source_excel must report errors, not raise, on a bad source."""

    def test_import_missing_file_returns_error(self, db_session):
        """A missing source file (wrong path / share down) yields errors >= 1."""
        result = import_from_source_excel(get_engine(), "Z:/__offline__/device-list.xlsx")
        assert result.errors >= 1
        # No exception propagated — the kiosk keeps serving from the local DB.
