"""
File: sync_status.py
Description: Thread-safe record of the most recent catalog-mirror tick — when
             it ran, what triggered it, and whether it wrote. Written by every
             tick path (startup, interval, and the manual admin "Sync Sheet"
             action) and read by GET /api/admin/sync-status so the kiosk admin
             footer can show a "last synced …" line.
Project: smart_locker/sync
Notes: Persisted to a small JSON file next to the SQLite database (local disk,
       never the CIFS share) so a restart or a skipped startup tick (share
       down) does not show "never". Override path with SMART_LOCKER_LAST_SYNC_PATH.
"""

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class _LastSync:
    """Snapshot of the latest mirror tick (see module docstring)."""

    at: str | None = None          # ISO-8601 UTC, second precision; None = never run
    trigger: str | None = None     # startup | interval | watch | manual
    updated: int = 0
    unchanged: int = 0
    errors: int = 0
    ok: bool = False               # True when the run completed with no errors
    message: str | None = None     # populated on failure (exception text)


_lock = threading.Lock()
_last = _LastSync()


def _utcnow() -> datetime:
    """Return the current UTC time (patched in tests)."""
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string at second precision."""
    return _utcnow().isoformat(timespec="seconds")


def _persist_path() -> Path:
    """JSON path for the last-sync snapshot (local disk, not the locker share)."""
    override = os.getenv("SMART_LOCKER_LAST_SYNC_PATH", "").strip()
    if override:
        return Path(override)
    from config.settings import DB_PATH
    return Path(DB_PATH).with_name("last_sync.json")


def _parse_iso(at: str) -> datetime:
    """Parse an ISO-8601 timestamp, treating naive values as UTC."""
    dt = datetime.fromisoformat(at)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _format_local(at: str) -> str:
    """Human local time: 'today 14:32', 'yesterday 14:32', or '2026-08-25 14:32'."""
    dt = _parse_iso(at).astimezone()
    now = _utcnow().astimezone()
    hhmm = dt.strftime("%H:%M")
    if dt.date() == now.date():
        return f"today {hhmm}"
    if dt.date() == now.date() - timedelta(days=1):
        return f"yesterday {hhmm}"
    return f"{dt.strftime('%Y-%m-%d')} {hhmm}"


def _format_ago(at: str) -> str:
    """Relative age: 'just now', '5m ago', '2h ago', '3d ago'."""
    dt = _parse_iso(at)
    seconds = int((_utcnow() - dt).total_seconds())
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def _load_unlocked() -> None:
    """Fill ``_last`` from disk. Caller must hold ``_lock``."""
    global _last
    path = _persist_path()
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except FileNotFoundError:
        return
    except (OSError, json.JSONDecodeError, TypeError) as e:
        logger.warning("Last-sync file %s unreadable (%s) — treating as never synced.", path, e)
        return
    if not isinstance(data, dict):
        logger.warning("Last-sync file %s is not an object — treating as never synced.", path)
        return
    _last = _LastSync(
        at=data.get("at"),
        trigger=data.get("trigger"),
        updated=int(data.get("updated") or 0),
        unchanged=int(data.get("unchanged") or 0),
        errors=int(data.get("errors") or 0),
        ok=bool(data.get("ok")),
        message=data.get("message"),
    )


def _save_unlocked() -> None:
    """Write ``_last`` to disk atomically. Caller must hold ``_lock``."""
    path = _persist_path()
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(asdict(_last), indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        logger.warning("Last-sync file %s not written (%s).", path, e)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def record_result(trigger: str, result) -> None:
    """Record a successful (or partially-errored) tick run.

    Args:
        trigger: Which mechanism ran the tick (startup/interval/manual).
        result: Object with updated/unchanged/errors counts.
    """
    with _lock:
        _last.at = _now_iso()
        _last.trigger = trigger
        _last.updated = result.updated
        _last.unchanged = result.unchanged
        _last.errors = result.errors
        _last.ok = result.errors == 0
        _last.message = None
        _save_unlocked()


def record_error(trigger: str, message: str) -> None:
    """Record a tick run that raised before producing a result.

    Args:
        trigger: Which mechanism attempted the tick.
        message: Short error description (exception text).
    """
    with _lock:
        _last.at = _now_iso()
        _last.trigger = trigger
        _last.updated = _last.unchanged = 0
        _last.errors = 1
        _last.ok = False
        _last.message = message
        _save_unlocked()


def get() -> dict:
    """Return a snapshot of the last tick as a plain dict (JSON-serialisable).

    Includes ``at_local`` and ``ago`` for the admin footer (local clock + relative).
    """
    with _lock:
        if _last.at is None:
            _load_unlocked()
        snap = asdict(_last)
    if snap["at"]:
        snap["at_local"] = _format_local(snap["at"])
        snap["ago"] = _format_ago(snap["at"])
    else:
        snap["at_local"] = None
        snap["ago"] = None
    return snap


def reset() -> None:
    """Clear the in-memory snapshot (tests). The JSON file is left as-is."""
    global _last
    with _lock:
        _last = _LastSync()
