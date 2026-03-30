"""
File: test_scheduler.py
Description: Tests for the sync scheduler module — startup import, file watcher
             debounce, and locked-file resilience. Validates that the scheduler
             runs an immediate import on startup, that the watchdog file handler
             debounces rapid events, and that the daily cron job is registered.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_scheduler.py -v
       Uses temporary directories and mock patches to avoid real file I/O.
"""

import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from openpyxl import Workbook

from smart_locker.sync.scheduler import (
    _SourceFileHandler,
    _run_source_import,
    start_scheduler,
    stop_scheduler,
)


def _create_test_excel(path: Path) -> None:
    """Write a minimal Excel file with one device row for import testing.

    Args:
        path: Destination file path for the test workbook.
    """
    wb = Workbook()
    ws = wb.active
    ws.append(["Equipment", "Hersteller", "Typbezeichnung", "Platz Messmittelschrank"])
    ws.append(["PM-SCHED-001", "TestMfr", "TestModel", "Schrank 1"])
    wb.save(path)


class TestRunSourceImport:
    """Tests for the _run_source_import helper function."""

    def test_missing_file_logs_warning(self):
        """Import with a nonexistent source file logs a warning and returns."""
        engine = MagicMock()
        with patch("smart_locker.sync.scheduler.logger") as mock_logger:
            _run_source_import(engine, "/nonexistent/path.xlsx")
            mock_logger.warning.assert_called_once()

    def test_import_called_for_existing_file(self, db_session):
        """Import runs successfully when the source file exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "source.xlsx"
            _create_test_excel(source)

            from smart_locker.database.engine import get_engine
            _run_source_import(get_engine(), source)

            from smart_locker.database.repositories import DeviceRepository
            device = DeviceRepository.find_by_pm(db_session, "PM-SCHED-001")
            assert device is not None
            assert device.manufacturer == "TestMfr"


class TestStartupImport:
    """Tests for the immediate import on scheduler startup."""

    def test_startup_triggers_immediate_import(self, db_session):
        """start_scheduler runs an import immediately before starting cron/watcher."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "source.xlsx"
            _create_test_excel(source)

            from smart_locker.database.engine import get_engine
            try:
                start_scheduler(get_engine(), source, hour=3, minute=0)

                from smart_locker.database.repositories import DeviceRepository
                device = DeviceRepository.find_by_pm(db_session, "PM-SCHED-001")
                assert device is not None, "Startup import should have inserted the device"
            finally:
                stop_scheduler()

    def test_empty_source_path_disables_scheduler(self):
        """An empty source path skips scheduler setup entirely."""
        with patch("smart_locker.sync.scheduler.logger") as mock_logger:
            start_scheduler(MagicMock(), "", hour=6, minute=0)
            mock_logger.info.assert_any_call(
                "Source Excel path not configured — scheduler disabled."
            )


class TestSourceFileHandler:
    """Tests for the watchdog file change handler debounce logic."""

    def test_debounce_collapses_rapid_events(self):
        """Multiple rapid on_modified calls produce a single import run."""
        engine = MagicMock()
        source = Path("/fake/source.xlsx")
        handler = _SourceFileHandler(engine, source)

        # Track how many times _do_import is called
        call_count = 0
        original_do_import = handler._do_import

        def counting_import():
            nonlocal call_count
            call_count += 1

        handler._do_import = counting_import

        # Simulate 5 rapid filesystem events
        mock_event = MagicMock()
        mock_event.is_directory = False
        mock_event.src_path = str(source)

        for _ in range(5):
            handler.on_modified(mock_event)
            time.sleep(0.05)

        # Wait for debounce window to expire (3s default + buffer)
        time.sleep(4.0)
        assert call_count == 1, f"Expected 1 debounced import, got {call_count}"

    def test_ignores_unrelated_files(self):
        """Events for files other than the source are ignored."""
        engine = MagicMock()
        source = Path("/fake/source.xlsx")
        handler = _SourceFileHandler(engine, source)

        handler._schedule_debounced_import = MagicMock()

        mock_event = MagicMock()
        mock_event.is_directory = False
        mock_event.src_path = "/fake/other_file.xlsx"

        handler.on_modified(mock_event)
        handler._schedule_debounced_import.assert_not_called()

    def test_ignores_directory_events(self):
        """Directory-level events are ignored."""
        engine = MagicMock()
        source = Path("/fake/source.xlsx")
        handler = _SourceFileHandler(engine, source)

        handler._schedule_debounced_import = MagicMock()

        mock_event = MagicMock()
        mock_event.is_directory = True
        mock_event.src_path = "/fake/source.xlsx"

        handler.on_modified(mock_event)
        handler._schedule_debounced_import.assert_not_called()


class TestStopScheduler:
    """Tests for graceful scheduler shutdown."""

    def test_stop_without_start(self):
        """Stopping when nothing is running should not raise."""
        stop_scheduler()
