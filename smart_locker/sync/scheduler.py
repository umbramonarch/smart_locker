"""
File: scheduler.py
Description: Scheduled and reactive source Excel import, then Location
             write-back. Combines three trigger mechanisms: (1) an immediate
             import on application startup, (2) a watchdog file watcher on
             local filesystems, and (3) a periodic APScheduler interval job
             (default 5 minutes). After each successful import the Pi writes
             Location for locker PMs back into the sheet.
Project: smart_locker/sync
Notes: Interval defaults to 5 minutes, configurable via
       SMART_LOCKER_SOURCE_SYNC_INTERVAL_MINUTES. Disabled when
       SMART_LOCKER_SOURCE_EXCEL_PATH is empty.
       The file watcher uses a debounce window (default 3 s) so that rapid
       successive writes by Excel (save → temp → rename) collapse into a
       single import. Network shares skip the watcher (inotify never sees
       remote writes); catalog changes wait for the interval or admin Sync.
       Write-back skips the save when nothing changed, so the watcher cannot
       loop on our own Location updates.
"""

import logging
import threading
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from smart_locker.sync import sync_status
from smart_locker.sync.fs_utils import is_network_path

logger = logging.getLogger(__name__)

# Module-level singletons — set by start_scheduler(), cleared by stop_scheduler()
_scheduler: BackgroundScheduler | None = None
_observer: Observer | None = None
_import_lock = threading.Lock()

# Debounce window in seconds — Excel save operations can produce multiple
# filesystem events in rapid succession (write temp, rename, update metadata).
# Collapsing them avoids redundant import runs.
_DEBOUNCE_SECONDS = 3.0


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


class ImportInProgress(Exception):
    """A catalog import is already running in this process."""


def run_source_import_exclusive(
    engine,
    source_path: str | Path,
    trigger: str = "interval",
):
    """Run import+write-back, or raise if another import holds the mutex.

    Args:
        engine: SQLAlchemy Engine.
        source_path: Path to the source Excel file.
        trigger: startup / interval / watch / manual.

    Returns:
        ImportResult, or None when the file is missing.

    Raises:
        ImportInProgress: Watcher, interval, startup, or admin Sync is already
            inside ``_run_source_import``.
    """
    if not _import_lock.acquire(blocking=False):
        raise ImportInProgress()
    try:
        return _run_source_import(engine, source_path, trigger=trigger)
    finally:
        _import_lock.release()


def _interval_import(engine, source_path: str | Path, trigger: str = "interval") -> None:
    """Interval job entry: skip if import/sync already holds the mutex."""
    try:
        run_source_import_exclusive(engine, source_path, trigger=trigger)
    except ImportInProgress:
        logger.info("Source import already running — interval skipped.")
    except Exception:
        logger.exception("Periodic source import failed.")
    _try_dashboard_launcher()


def _run_source_import(engine, source_path: str | Path, trigger: str = "interval"):
    """Execute the source Excel import, then Location write-back.

    Validates that the source file exists, then delegates to
    ``import_from_source_excel``. Logs the result summary or any errors and
    records the outcome in ``sync_status`` for the admin "last synced" display.
    A missing file is skipped without recording, so a persisted last-sync from
    an earlier run is left in place (share down at boot). After a successful
    import, locker locations are written back into Location.

    Args:
        engine: SQLAlchemy Engine for database operations.
        source_path: Path to the source Excel file on disk.
        trigger: Which mechanism initiated this run (startup/interval/watch),
            recorded in the sync-status snapshot.

    Returns:
        ImportResult on success, or None when the file is missing.
    """
    from smart_locker.sync.source_import import import_from_source_excel

    path = Path(source_path)
    if not path.exists():
        logger.warning("Source Excel not found at %s — skipping import.", path)
        return None

    logger.info("Starting source Excel import from %s", path)
    try:
        result = import_from_source_excel(engine, path)
        logger.info(
            "Source import complete: %d imported, %d updated, %d unchanged, %d errors.",
            result.imported, result.updated, result.unchanged, result.errors,
        )
        sync_status.record_result(trigger, result)
        from smart_locker.sync.location_writeback import write_location_with_engine

        wb = write_location_with_engine(engine, path)
        if wb.error:
            logger.warning(
                "Location write-back after source import failed (%s).", wb.error
            )
        return result
    except Exception as e:
        logger.error("Source Excel import failed: %s", e)
        sync_status.record_error(trigger, str(e))
        raise


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
        try:
            run_source_import_exclusive(
                self._engine, self._source_path, trigger="watch"
            )
        except ImportInProgress:
            logger.info("Source import already running — watch event skipped.")
        except Exception:
            logger.exception("Watch-triggered source import failed.")


def start_scheduler(
    engine,
    source_path: str | Path,
    interval_minutes: int = 5,
) -> None:
    """Start the background scheduler, run an immediate import, and watch for changes.

    Performs three setup actions:
    1. Runs an immediate source import so the database is current on startup.
    2. Starts a watchdog file observer on the source directory when the path
       is a local filesystem (skipped on CIFS/NFS).
    3. Starts an APScheduler interval job (default every 5 minutes).

    Args:
        engine: SQLAlchemy engine.
        source_path: Path to the source Excel file.
        interval_minutes: Minutes between safety-net imports. Values below 1 are
            raised to 1 so a zero/empty env cannot spin the importer.

    Returns:
        None.
    """
    global _scheduler, _observer

    if not source_path:
        logger.info("Source Excel path not configured — scheduler disabled.")
        return

    try:
        minutes = max(1, int(interval_minutes))
    except (TypeError, ValueError):
        logger.warning(
            "Invalid source-sync interval %r — using 5 minutes.", interval_minutes
        )
        minutes = 5
    source = Path(source_path).resolve()

    # --- 1. Immediate import on startup ---
    try:
        run_source_import_exclusive(engine, source, trigger="startup")
    except ImportInProgress:
        logger.info("Source import already running — startup import skipped.")
    except Exception:
        logger.exception("Startup source import failed.")

    _try_dashboard_launcher()

    # --- 2. File watcher for live changes (local filesystems only) ---
    # inotify does not deliver events for writes made by other hosts on a network
    # share, so on the Pi (source Excel on the mounted CIFS share) we skip the live
    # watch and rely on the startup import, the interval job, and admin Sync.
    on_network = is_network_path(source)
    if on_network:
        logger.info(
            "Source Excel %s is on a network share (CIFS/NFS) — inotify is unreliable "
            "there, so catalog changes are picked up every %d minutes (plus the startup "
            "import and admin Sync Source).",
            source, minutes,
        )
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

    # --- 3. Interval job (safety net) ---
    _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        _interval_import,
        trigger=IntervalTrigger(minutes=minutes),
        args=[engine, source, "interval"],
        id="source_excel_import",
        name="Periodic source Excel import",
        misfire_grace_time=3600,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    logger.info(
        "Scheduler started: import every %d minutes; %s.",
        minutes,
        "network share (no live watch)" if on_network else "file watcher active",
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
