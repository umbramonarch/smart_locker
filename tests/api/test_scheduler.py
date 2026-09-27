"""
File: test_scheduler.py
Description: Mirror scheduler lifecycle: re-arming replaces the running
             scheduler instead of stacking duplicates, and the
             sync_in_progress flag reflects the tick mutex.
Project: smart_locker/tests/api
Notes: The mirror is unconfigured in tests, so the queued startup tick
       returns early without touching the database or a workbook.
"""

from sqlalchemy import create_engine

from smart_locker.sync import mirror, scheduler


def test_sync_in_progress_follows_the_tick_mutex():
    """The flag is true exactly while the mirror tick lock is held."""
    assert mirror._tick_lock.acquire(blocking=False)
    try:
        assert scheduler.sync_in_progress() is True
    finally:
        mirror._tick_lock.release()
    assert scheduler.sync_in_progress() is False


def test_start_scheduler_restart_stops_previous(tmp_path):
    """A second start_scheduler must not stack schedulers on the global."""
    engine = create_engine("sqlite:///:memory:")
    try:
        scheduler.start_scheduler(engine, interval_seconds=60)
        first_scheduler = scheduler._scheduler
        assert first_scheduler is not None

        scheduler.start_scheduler(engine, interval_seconds=60)
        assert scheduler._scheduler is not first_scheduler
        assert not first_scheduler.running
    finally:
        scheduler.stop_scheduler()
        engine.dispose()
    assert scheduler._scheduler is None
