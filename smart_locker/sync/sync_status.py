"""
File: sync_status.py
Description: Thread-safe record of the most recent source-Excel import — when it
             ran, what triggered it, and the per-category counts. Written by
             every import path (startup, daily cron, file watcher, network-share
             mtime poll, and the manual admin "Sync now" action) and read by the
             GET /api/admin/sync-status endpoint so the dashboard can show a
             "last synced …" line.
Project: smart_locker/sync
Notes: Deliberately in-memory (no DB table): the appliance runs a startup import
       on every boot, which re-seeds this immediately, so persistence across
       restarts adds nothing. Reset to "never synced" until the first import.
"""

import logging
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class _LastSync:
    """Snapshot of the latest source import (see module docstring)."""

    at: str | None = None          # ISO-8601 UTC, second precision; None = never run
    trigger: str | None = None     # startup | cron | watch | mtime-poll | manual
    imported: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: int = 0
    ok: bool = False               # True when the run completed with no errors
    message: str | None = None     # populated on failure (exception text)


_lock = threading.Lock()
_last = _LastSync()


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string at second precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_result(trigger: str, result) -> None:
    """Record a successful (or partially-errored) import run.

    Args:
        trigger: Which mechanism ran the import (startup/cron/watch/mtime-poll/manual).
        result: An ``ImportResult`` with imported/updated/unchanged/errors counts.
    """
    with _lock:
        _last.at = _now_iso()
        _last.trigger = trigger
        _last.imported = result.imported
        _last.updated = result.updated
        _last.unchanged = result.unchanged
        _last.errors = result.errors
        _last.ok = result.errors == 0
        _last.message = None


def record_error(trigger: str, message: str) -> None:
    """Record an import run that raised before producing a result.

    Args:
        trigger: Which mechanism attempted the import.
        message: Short error description (exception text).
    """
    with _lock:
        _last.at = _now_iso()
        _last.trigger = trigger
        _last.imported = _last.updated = _last.unchanged = 0
        _last.errors = 1
        _last.ok = False
        _last.message = message


def get() -> dict:
    """Return a snapshot of the last import as a plain dict (JSON-serialisable)."""
    with _lock:
        return asdict(_last)
