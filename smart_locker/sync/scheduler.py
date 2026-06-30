"""
File: scheduler.py
Description: Scheduled and reactive source Excel import. Combines three trigger
             mechanisms: (1) an immediate import on application startup so the
             database is always current before the first user interaction,
             (2) a watchdog-based file system watcher that triggers an import
             whenever the source Excel is modified on disk, and (3) a daily
             APScheduler cron job as a safety net.
Project: smart_locker/sync
Notes: Schedule defaults to 6:00 AM daily, configurable via
       SMART_LOCKER_SOURCE_SYNC_HOUR and SMART_LOCKER_SOURCE_SYNC_MINUTE.
       Disabled when SMART_LOCKER_SOURCE_EXCEL_PATH is empty.
       The file watcher uses a debounce window (default 3 s) so that rapid
       successive writes by Excel (save → temp → rename) collapse into a
       single import.
"""

import logging
import os
import threading
import time
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from smart_locker.sync import sync_status
from smart_locker.sync.fs_utils import is_network_path

logger = logging.getLogger(__name__)

# Module-level singletons — set by start_scheduler(), cleared by stop_scheduler()
_scheduler: BackgroundScheduler | None = None
_observer: Observer | None = None

# Debounce window in seconds — Excel save operations can produce multiple
# filesystem events in rapid succession (write temp, rename, update metadata).
# Collapsing them avoids redundant import runs.
_DEBOUNCE_SECONDS = 3.0

# Network-share mtime polling. watchdog's inotify Observer never fires for writes
# made by another host on a CIFS/NFS mount (see fs_utils.is_network_path), so on a
# network source we poll the file's modification time on a fixed interval instead
# and re-import when it changes. Interval is configurable (default 30 s).
_MTIME_POLL_SECONDS = max(5, int(os.getenv("SMART_LOCKER_SOURCE_POLL_SECONDS", "30")))
# Last-seen mtime per polled source path; seeded in start_scheduler so the first
# poll tick after startup does not re-import the file the startup import just read.
_poll_state: dict[str, float] = {}


def _run_source_import(engine, source_path: str | Path, trigger: str = "scheduled") -> None:
    """Execute the source Excel import (called by scheduler, watcher, or startup).

    Validates that the source file exists, then delegates to
    ``import_from_source_excel``. Logs the result summary or any errors and
    records the outcome in ``sync_status`` for the admin "last synced" display.

    Args:
        engine: SQLAlchemy Engine for database operations.
        source_path: Path to the company source Excel file on disk.
        trigger: Which mechanism initiated this run (startup/cron/watch/
            mtime-poll), recorded in the sync-status snapshot.

    Returns:
        None. Results are logged.
    """
    from smart_locker.sync.source_import import import_from_source_excel

    path = Path(source_path)
    if not path.exists():
        logger.warning("Source Excel not found at %s — skipping import.", path)
        return

    logger.info("Starting source Excel import from %s", path)
    try:
        result = import_from_source_excel(engine, path)
        logger.info(
            "Source import complete: %d imported, %d updated, %d unchanged, %d errors.",
            result.imported, result.updated, result.unchanged, result.errors,
        )
        sync_status.record_result(trigger, result)
    except Exception as e:
        logger.error("Source Excel import failed: %s", e)
        sync_status.record_error(trigger, str(e))


def _poll_source_mtime(engine, source_path: str | Path) -> None:
    """Re-import the source Excel when its modification time changes (network shares).

    Used in place of the inotify file watcher on CIFS/NFS mounts, where remote
    writes never raise inotify events. The first observation only records a
    baseline; subsequent changes trigger an import.

    Args:
        engine: SQLAlchemy Engine for database operations.
        source_path: Path to the company source Excel file on the network share.

    Returns:
        None.
    """
    path = Path(source_path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        # Share briefly unavailable (network hiccup) — try again next tick.
        return

    key = str(path)
    prev = _poll_state.get(key)
    if prev is None:
        _poll_state[key] = mtime
        return
    if mtime != prev:
        _poll_state[key] = mtime
        logger.info("Source Excel mtime changed (network-share poll) — triggering import.")
        _run_source_import(engine, source_path, trigger="mtime-poll")


class _SourceFileHandler(FileSystemEventHandler):
    """Watchdog handler that triggers an import when the source Excel changes.

    Monitors only the specific source file (not the entire directory).
    Uses a debounce timer so that bursts of filesystem events from a
    single Excel save operation produce only one import run.

    Attributes:
        _engine: SQLAlchemy Engine for database operations.
        _source_path: Resolved absolute path to the source Excel file.
        _source_name: Lowercased filename of the source Excel for matching.
        _timer: Threading timer used for debouncing rapid events.
        _lock: Thread lock protecting the debounce timer.
    """

    def __init__(self, engine, source_path: Path) -> None:
        """Initialize the file change handler.

        Args:
            engine: SQLAlchemy Engine for database operations.
            source_path: Absolute path to the source Excel file.
        """
        super().__init__()
        self._engine = engine
        self._source_path = source_path.resolve()
        # Store lowercased filename for case-insensitive matching on Windows
        self._source_name = source_path.name.lower()
        # Debounce state — a single timer that resets on each event
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def on_modified(self, event) -> None:
        """Handle file modification events for the source Excel.

        Called by watchdog whenever a file in the watched directory is
        modified. Filters to only the source file and debounces rapid
        successive events into a single import run.

        Args:
            event: Watchdog file system event with ``src_path`` attribute.

        Returns:
            None.
        """
        if event.is_directory:
            return

        # Match only the source file by name (case-insensitive for Windows)
        changed_name = Path(event.src_path).name.lower()
        if changed_name != self._source_name:
            return

        self._schedule_debounced_import()

    def on_created(self, event) -> None:
        """Handle file creation events for the source Excel.

        Covers the case where Excel saves by writing a temp file then
        renaming it, which appears as a create event on the target path.

        Args:
            event: Watchdog file system event with ``src_path`` attribute.

        Returns:
            None.
        """
        if event.is_directory:
            return

        changed_name = Path(event.src_path).name.lower()
        if changed_name != self._source_name:
            return

        self._schedule_debounced_import()

    def _schedule_debounced_import(self) -> None:
        """Reset the debounce timer and schedule an import after the window.

        If a timer is already running, it is cancelled and restarted so
        that only the last event in a burst triggers the actual import.

        Returns:
            None.
        """
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(
                _DEBOUNCE_SECONDS, self._do_import
            )
            # Daemon thread so it doesn't prevent application shutdown
            self._timer.daemon = True
            self._timer.start()

    def _do_import(self) -> None:
        """Execute the source import (called by the debounce timer).

        Runs in a background daemon thread spawned by the debounce timer.

        Returns:
            None.
        """
        logger.info("Source Excel changed — triggering import.")
        _run_source_import(self._engine, self._source_path, trigger="watch")


def start_scheduler(
    engine,
    source_path: str | Path,
    hour: int = 6,
    minute: int = 0,
) -> None:
    """Start the background scheduler, run an immediate import, and watch for changes.

    Performs three setup actions:
    1. Runs an immediate source import so the database is current on startup.
    2. Starts a watchdog file observer on the source directory so any
       modification to the source Excel triggers a new import.
    3. Starts an APScheduler cron job as a daily safety-net import.

    Args:
        engine: SQLAlchemy engine.
        source_path: Path to the company source Excel file.
        hour: Hour to run the daily cron job (0-23).
        minute: Minute to run the daily cron job (0-59).

    Returns:
        None.
    """
    global _scheduler, _observer

    if not source_path:
        logger.info("Source Excel path not configured — scheduler disabled.")
        return

    source = Path(source_path).resolve()

    # --- 1. Immediate import on startup ---
    _run_source_import(engine, source, trigger="startup")

    # --- 2. File watcher for live changes (local filesystems only) ---
    # inotify does not deliver events for writes made by other hosts on a network
    # share, so on the Pi (source Excel on the mounted M:/CIFS share) we skip the live
    # watch and rely on the startup import, a periodic mtime poll, and the daily cron.
    on_network = is_network_path(source)
    if on_network:
        logger.info(
            "Source Excel %s is on a network share (CIFS/NFS) — inotify is unreliable "
            "there, so live changes are picked up by an mtime poll every %d s (plus the "
            "startup import and the daily %02d:%02d cron).",
            source, _MTIME_POLL_SECONDS, hour, minute,
        )
        # Seed the baseline so the first poll tick does not re-import the file the
        # startup import just read; only a later change triggers an import.
        try:
            _poll_state[str(source)] = source.stat().st_mtime
        except OSError:
            _poll_state.pop(str(source), None)
    elif source.parent.exists():
        handler = _SourceFileHandler(engine, source)
        _observer = Observer()
        # Watch the parent directory (watchdog monitors directories, not files)
        _observer.schedule(handler, str(source.parent), recursive=False)
        _observer.daemon = True
        _observer.start()
        logger.info("File watcher started: monitoring %s for changes.", source)
    else:
        logger.warning(
            "Source directory %s does not exist — file watcher not started.",
            source.parent,
        )

    # --- 3. Daily cron job (safety net) + network-share mtime poll ---
    _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        _run_source_import,
        trigger=CronTrigger(hour=hour, minute=minute),
        args=[engine, source, "cron"],
        id="source_excel_import",
        name="Daily source Excel import",
        # 1 hour grace time — tolerate delayed execution (e.g. system wake from sleep)
        misfire_grace_time=3600,
    )
    if on_network:
        _scheduler.add_job(
            _poll_source_mtime,
            trigger=IntervalTrigger(seconds=_MTIME_POLL_SECONDS),
            args=[engine, source],
            id="source_excel_mtime_poll",
            name="Network-share source Excel mtime poll",
            max_instances=1,            # never overlap if an import runs long
            coalesce=True,              # collapse missed ticks into one
        )
    _scheduler.start()
    logger.info(
        "Scheduler started: daily import at %02d:%02d; %s.",
        hour, minute,
        f"mtime poll every {_MTIME_POLL_SECONDS}s (network share)" if on_network
        else "file watcher active",
    )


def stop_scheduler() -> None:
    """Shut down the scheduler and file watcher gracefully.

    Stops both the APScheduler ``BackgroundScheduler`` and the watchdog
    ``Observer``, then clears the module-level singletons.

    Returns:
        None.
    """
    global _scheduler, _observer

    if _observer is not None:
        _observer.stop()
        _observer.join(timeout=5)
        _observer = None
        logger.info("File watcher stopped.")

    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Scheduler stopped.")
