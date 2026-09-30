"""
File: scheduler.py
Description: Periodic mirror tick — the catalog sheet is Pi-written now, so
             the scheduler's job is (1) a startup tick that adopts the sheet
             on first sight or flushes pending writes, and (2) an interval
             tick that detects hand edits and retries deferred writes. The
             dashboard share launcher is retried alongside each tick.
Project: smart_locker/sync
Notes: Interval defaults to 60 s (SMART_LOCKER_MIRROR_SYNC_SECONDS, min 5).
       External edits are detected by statting the mirror file, which works
       over CIFS too — no watchdog needed. Workbook I/O stays off the HTTP
       path; a locked or missing file never touches the kiosk.
"""

import logging
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

# Module-level singleton — set by start_scheduler(), cleared by stop_scheduler()
_scheduler: BackgroundScheduler | None = None


def _try_dashboard_launcher() -> None:
    """Write dashboard.url onto the share if it is mounted. Never raises.

    Startup may see the CIFS share down; the interval job retries until the
    launcher files appear. Does not busy-loop.
    """
    try:
        from config.settings import DASHBOARD_SHARE_PATH, PUBLIC_URL
        from smart_locker.sync.dashboard_launcher import write_dashboard_launcher

        write_dashboard_launcher(DASHBOARD_SHARE_PATH, PUBLIC_URL)
    except Exception:
        logger.exception("Dashboard launcher retry failed.")


_startup_pending: threading.Event | None = None


def sync_in_progress() -> bool:
    """Whether a mirror tick holds the mutex right now.

    Lets HTTP routes distinguish "name not in the approved list" from "the
    catalog is still adopting" — the startup tick runs on a worker
    thread, so the registrants table can lag boot by seconds on a slow share.
    """
    pending = _startup_pending
    if pending is not None and pending.is_set():
        return True
    from smart_locker.sync import mirror

    return mirror.tick_in_progress()


def run_mirror_tick(engine, trigger: str = "manual") -> dict:
    """Run one mirror tick (adopt → detect → flush). Non-blocking mutex.

    Args:
        engine: SQLAlchemy engine.
        trigger: startup / interval / manual — recorded in sync_status.

    Returns:
        The tick summary dict; ``skipped`` is ``"in_progress"`` when another
        tick is already running.
    """
    from smart_locker.sync import mirror

    return mirror.tick(engine, trigger=trigger)


def _scheduled_tick(engine, trigger: str = "interval") -> None:
    """Scheduled tick entry — exceptions are logged, never raised."""
    try:
        run_mirror_tick(engine, trigger=trigger)
    except Exception:
        logger.exception("%s mirror tick failed.", trigger.capitalize())
    _try_dashboard_launcher()


def start_scheduler(engine, interval_seconds: int = 60) -> None:
    """Start the periodic mirror tick.

    Queues a startup tick immediately so application serving and health
    checks do not wait for workbook I/O, then ticks every
    ``interval_seconds`` (SMART_LOCKER_MIRROR_SYNC_SECONDS).

    Args:
        engine: SQLAlchemy engine.
        interval_seconds: Seconds between ticks. Values below 5 are raised.

    Returns:
        None.
    """
    global _scheduler

    stop_scheduler()

    try:
        seconds = max(5, int(interval_seconds))
    except (TypeError, ValueError):
        logger.warning(
            "Invalid mirror interval %r — using 60 seconds.", interval_seconds
        )
        seconds = 60

    _try_dashboard_launcher()

    # Startup tick on a plain thread: a one-shot date job races the
    # scheduler's own remove_job on shutdown and dies with JobLookupError.
    global _startup_pending
    startup_done = threading.Event()
    startup_done.set()
    _startup_pending = startup_done

    def _startup_tick() -> None:
        try:
            _scheduled_tick(engine, "startup")
        finally:
            startup_done.clear()

    try:
        threading.Thread(
            target=_startup_tick,
            name="mirror-startup-tick",
            daemon=True,
        ).start()
    except Exception:
        startup_done.clear()
        _startup_pending = None
        raise

    _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        _scheduled_tick,
        trigger=IntervalTrigger(seconds=seconds),
        args=[engine, "interval"],
        id="mirror_tick",
        name="Periodic mirror tick",
        misfire_grace_time=3600,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    logger.info("Scheduler started: mirror tick every %d s.", seconds)


def stop_scheduler() -> None:
    """Shut down the scheduler gracefully.

    Returns:
        None.
    """
    global _scheduler, _startup_pending

    _startup_pending = None
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Scheduler stopped.")
