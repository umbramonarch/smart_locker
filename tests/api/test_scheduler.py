"""
File: test_scheduler.py
Description: Source-import scheduler lifecycle: re-arming replaces the running
             scheduler/observer pair instead of stacking duplicates, and the
             import_in_progress flag reflects the shared mutex.
Project: smart_locker/tests/api
Notes: Uses a nonexistent source path so the queued startup import returns
       early without touching the database.
"""

from sqlalchemy import create_engine

from smart_locker.sync import scheduler


def test_import_in_progress_follows_the_mutex():
    """The flag is true exactly while the import lock is held."""
    assert scheduler._import_lock.acquire(blocking=False)
    try:
        assert scheduler.import_in_progress() is True
    finally:
        scheduler._import_lock.release()
    assert scheduler.import_in_progress() is False


def test_start_scheduler_restart_stops_previous_pair(tmp_path):
    """A second start_scheduler must not stack watchers on the globals."""
    engine = create_engine("sqlite:///:memory:")
    source = tmp_path / "device-list.xlsx"  # missing file: import no-ops
    try:
        scheduler.start_scheduler(engine, source, interval_hours=6)
        first_scheduler = scheduler._scheduler
        first_observer = scheduler._observer
        assert first_scheduler is not None and first_observer is not None

        scheduler.start_scheduler(engine, source, interval_hours=6)
        assert scheduler._scheduler is not first_scheduler
        assert scheduler._observer is not first_observer
        assert not first_observer.is_alive()
        assert not first_scheduler.running
    finally:
        scheduler.stop_scheduler()
        engine.dispose()
    assert scheduler._scheduler is None
    assert scheduler._observer is None
