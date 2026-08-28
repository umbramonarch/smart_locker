"""
File: test_scheduler.py
Description: Tests for the sync scheduler module — startup import, file watcher
             debounce, 6-hour interval job, and no network-share mtime poll.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_scheduler.py -v
       Uses temporary directories and mock patches to avoid real file I/O.
"""

import tempfile
import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from openpyxl import Workbook

import smart_locker.sync.scheduler as sched
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
    ws.append(["Equipment", "Manufacturer", "Model", "Slot"])
    ws.append(["PM-SCHED-001", "TestMfr", "TestModel", "Bay 1"])
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
            from smart_locker.database.repositories import DeviceRepository

            DeviceRepository.create(
                db_session,
                name="Old",
                device_type="general",
                pm_number="PM-SCHED-001",
                manufacturer="OldMfr",
                model="OldModel",
                locker_slot=1,
            )
            db_session.commit()

            _run_source_import(get_engine(), source)

            device = DeviceRepository.find_by_pm(db_session, "PM-SCHED-001")
            assert device is not None
            assert device.manufacturer == "TestMfr"

    def test_writeback_error_is_logged(self, db_session, tmp_path):
        """Scheduler inspects Location write-back and logs a failure (I17)."""
        source = tmp_path / "source.xlsx"
        _create_test_excel(source)
        from smart_locker.sync.location_writeback import WritebackResult

        with patch(
            "smart_locker.sync.location_writeback.write_location_with_engine",
            return_value=WritebackResult(error="locked"),
        ), patch("smart_locker.sync.scheduler.logger") as mock_logger:
            from smart_locker.database.engine import get_engine

            _run_source_import(get_engine(), source)
            logged = " ".join(
                str(c) for c in mock_logger.warning.call_args_list
            )
            assert "locked" in logged


class TestStartupImport:
    """Tests for the immediate import on scheduler startup."""

    def test_startup_triggers_immediate_import(self, db_session):
        """start_scheduler runs an import immediately before starting interval/watcher."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "source.xlsx"
            _create_test_excel(source)

            from smart_locker.database.engine import get_engine
            from smart_locker.database.repositories import DeviceRepository

            DeviceRepository.create(
                db_session,
                name="Old",
                device_type="general",
                pm_number="PM-SCHED-001",
                manufacturer="OldMfr",
                model="OldModel",
                locker_slot=1,
            )
            db_session.commit()
            try:
                start_scheduler(get_engine(), source, interval_hours=6)

                device = DeviceRepository.find_by_pm(db_session, "PM-SCHED-001")
                assert device is not None
                assert device.manufacturer == "TestMfr"
            finally:
                stop_scheduler()

    def test_empty_source_path_disables_scheduler(self):
        """An empty source path skips scheduler setup entirely."""
        with patch("smart_locker.sync.scheduler.logger") as mock_logger:
            start_scheduler(MagicMock(), "", interval_hours=6)
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


class TestIntervalSchedule:
    """Tests for the periodic source-import interval (replaces daily cron + 30s poll)."""

    def test_interval_job_defaults_to_six_hours(self, db_session):
        """start_scheduler registers a 6-hour interval import, not a daily cron."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "source.xlsx"
            _create_test_excel(source)

            from smart_locker.database.engine import get_engine
            try:
                start_scheduler(get_engine(), source, interval_hours=6)
                job = sched._scheduler.get_job("source_excel_import")
                assert job is not None
                assert job.trigger.interval == timedelta(hours=6)
                assert sched._scheduler.get_job("source_excel_mtime_poll") is None
            finally:
                stop_scheduler()

    def test_interval_hours_is_configurable(self, db_session):
        """The interval follows the value passed into start_scheduler."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "source.xlsx"
            _create_test_excel(source)

            from smart_locker.database.engine import get_engine
            try:
                start_scheduler(get_engine(), source, interval_hours=12)
                job = sched._scheduler.get_job("source_excel_import")
                assert job.trigger.interval == timedelta(hours=12)
            finally:
                stop_scheduler()

    def test_network_share_does_not_start_mtime_poll(self, db_session):
        """CIFS/NFS sources do not get a 30-second mtime poll job."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "source.xlsx"
            _create_test_excel(source)

            from smart_locker.database.engine import get_engine
            try:
                with patch("smart_locker.sync.scheduler.is_network_path", return_value=True):
                    start_scheduler(get_engine(), source, interval_hours=6)
                assert sched._scheduler.get_job("source_excel_mtime_poll") is None
                job = sched._scheduler.get_job("source_excel_import")
                assert job is not None
                assert job.trigger.interval == timedelta(hours=6)
            finally:
                stop_scheduler()


class TestStopScheduler:
    """Tests for graceful scheduler shutdown."""

    def test_stop_without_start(self):
        """Stopping when nothing is running should not raise."""
        stop_scheduler()


class TestImportMutex:
    """I24: startup / watcher / interval / manual Sync share one mutex."""

    def test_overlapping_exclusive_raises(self):
        """A second exclusive import is ImportInProgress while the first holds."""
        import threading

        from smart_locker.sync.scheduler import ImportInProgress, run_source_import_exclusive

        entered = threading.Event()
        release = threading.Event()

        def slow_run(*_args, **_kwargs):
            entered.set()
            release.wait(5)

        with patch(
            "smart_locker.sync.scheduler._run_source_import", side_effect=slow_run
        ):
            t = threading.Thread(
                target=run_source_import_exclusive,
                args=(MagicMock(), "/tmp/x.xlsx"),
            )
            t.start()
            assert entered.wait(2)
            with pytest.raises(ImportInProgress):
                run_source_import_exclusive(MagicMock(), "/tmp/x.xlsx")
            release.set()
            t.join(2)


class TestSchedulerExceptionLogging:
    """Interval/watch/startup wrappers log failures instead of swallowing them."""

    def test_interval_logs_import_exception(self, monkeypatch):
        """A raised import still logs at exception and retries the dashboard launcher."""
        calls = []

        monkeypatch.setattr(
            "smart_locker.sync.dashboard_launcher.write_dashboard_launcher",
            lambda *_a, **_k: calls.append("launcher") or True,
        )
        monkeypatch.setattr("config.settings.DASHBOARD_SHARE_PATH", "/mnt/locker")
        monkeypatch.setattr("config.settings.PUBLIC_URL", "http://192.168.1.10:8000")

        with patch(
            "smart_locker.sync.scheduler.run_source_import_exclusive",
            side_effect=RuntimeError("import failed"),
        ):
            with patch("smart_locker.sync.scheduler.logger") as mock_logger:
                sched._interval_import(MagicMock(), "/x.xlsx")
                mock_logger.exception.assert_called()
                logged = " ".join(str(c) for c in mock_logger.exception.call_args_list)
                assert "Periodic source import failed" in logged
        assert calls == ["launcher"]

    def test_startup_retries_dashboard_launcher(self, monkeypatch):
        """Startup still tries the share launcher after the immediate import."""
        calls = []
        monkeypatch.setattr(
            "smart_locker.sync.dashboard_launcher.write_dashboard_launcher",
            lambda *_a, **_k: calls.append("launcher") or True,
        )
        monkeypatch.setattr("config.settings.DASHBOARD_SHARE_PATH", "/mnt/locker")
        monkeypatch.setattr("config.settings.PUBLIC_URL", "http://192.168.1.10:8000")
        with patch(
            "smart_locker.sync.scheduler.run_source_import_exclusive",
            return_value=None,
        ):
            sched._try_dashboard_launcher()
        assert calls == ["launcher"]

