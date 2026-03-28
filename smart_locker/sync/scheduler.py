"""Scheduled tasks — daily source Excel import.

Uses APScheduler's BackgroundScheduler to run periodic imports
without blocking the main application thread.
"""

import logging
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def _run_source_import(engine, source_path: str | Path) -> None:
    """Execute the source Excel import (called by scheduler)."""
    from smart_locker.sync.source_import import import_from_source_excel

    path = Path(source_path)
    if not path.exists():
        logger.warning("Source Excel not found at %s — skipping scheduled import.", path)
        return

    logger.info("Starting scheduled source Excel import from %s", path)
    try:
        result = import_from_source_excel(engine, path)
        logger.info(
            "Scheduled import complete: %d imported, %d updated, %d unchanged, %d errors.",
            result.imported, result.updated, result.unchanged, result.errors,
        )
    except Exception as e:
        logger.error("Scheduled source Excel import failed: %s", e)


def start_scheduler(
    engine,
    source_path: str | Path,
    hour: int = 6,
    minute: int = 0,
) -> None:
    """Start the background scheduler for daily source Excel import.

    Args:
        engine: SQLAlchemy engine.
        source_path: Path to the company source Excel file.
        hour: Hour to run (0-23).
        minute: Minute to run (0-59).
    """
    global _scheduler

    if not source_path:
        logger.info("Source Excel path not configured — scheduler disabled.")
        return

    _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        _run_source_import,
        trigger=CronTrigger(hour=hour, minute=minute),
        args=[engine, source_path],
        id="source_excel_import",
        name="Daily source Excel import",
        misfire_grace_time=3600,
    )
    _scheduler.start()
    logger.info("Scheduler started: source Excel import at %02d:%02d daily.", hour, minute)


def stop_scheduler() -> None:
    """Shut down the scheduler gracefully."""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Scheduler stopped.")
