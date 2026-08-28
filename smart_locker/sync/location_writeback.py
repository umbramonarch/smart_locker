"""
File: location_writeback.py
Description: Pi → Excel write-back of the Location column only, matched by PM.
             Locker available (or any non-borrowed state) writes a stable
             in-locker token; borrowed writes the borrower's display name.
             Other columns and sheets are left untouched. Never inserts rows.
Project: smart_locker/sync
Notes: Called after borrow/return, Register Device, and source import.
       A locked or missing workbook is logged and skipped — the kiosk and
       SQLite stay correct; the next sync retries. Dashboard owner edit
       uses write_location_value for one PM. Unchanged cells skip
       the save so a local file-watcher cannot loop. If the sheet changes
       between copy and replace, the write is retried from the latest file
       so catalog edits are not reverted.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import load_workbook
from sqlalchemy.orm import Session

from config.settings import in_locker_token
from smart_locker.database.models import Device, DeviceStatus
from smart_locker.database.repositories import DeviceRepository
from smart_locker.sync.source_import import (
    find_column,
    location_candidates,
    pm_candidates,
    pm_match_key,
)

logger = logging.getLogger(__name__)

# Default in-locker token when SMART_LOCKER_IN_LOCKER_TOKEN is unset.
# Live writes use config.settings.in_locker_token().
IN_LOCKER_TOKEN = "Locker"

_MAX_RETRIES = 3
_RETRY_DELAY_SECONDS = 1.0
_IO_TIMEOUT_SECONDS = 8.0
_excel_writer_lock = threading.Lock()
_scheduled_lock = threading.Lock()
_scheduled_threads: list[threading.Thread] = []


class _StaleWorkbook(Exception):
    """The source xlsx changed on disk after we copied it; retry from the latest file."""


@dataclass
class WritebackResult:
    """Outcome of one Location write-back. Never used to fail the kiosk."""

    written: int = 0
    unchanged: int = 0
    skipped: int = 0
    saved: bool = False
    error: str | None = None


_last_writeback_lock = threading.Lock()
_last_writeback: dict | None = None


def _remember_writeback(result: WritebackResult) -> None:
    """Store the latest write-back outcome for ``/api/health``.

    Args:
        result: Outcome of this attempt (success, skip, or error).
    """
    global _last_writeback
    snapshot = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "error": result.error,
        "saved": result.saved,
        "written": result.written,
    }
    with _last_writeback_lock:
        _last_writeback = snapshot


def last_writeback() -> dict | None:
    """Return the last Location write-back snapshot, or None if none yet.

    Returns:
        Dict with ``at``, ``error``, ``saved``, ``written``, or None.
    """
    with _last_writeback_lock:
        return dict(_last_writeback) if _last_writeback else None


def _location_value(device: Device) -> str | None:
    """Map one locker device to the Excel location cell, or skip.

    Args:
        device: Locker row (status + optional borrower already loaded).

    Returns:
        Borrower display name when borrowed, the in-locker token when
        available, or None for MAINTENANCE (do not write Location).
    """
    if device.status == DeviceStatus.MAINTENANCE:
        return None
    if device.status == DeviceStatus.BORROWED:
        if device.current_borrower is None:
            logger.warning(
                "Location write-back skipped for %s — BORROWED with no borrower.",
                device.pm_number,
            )
            return None
        name = (device.current_borrower.display_name or "").strip()
        if not name:
            logger.warning(
                "Location write-back skipped for %s — BORROWED with empty borrower name.",
                device.pm_number,
            )
            return None
        return name
    return in_locker_token()


_FORMULA_MARKERS = frozenset("=+-@")


def _stored_location_text(value) -> str:
    """Normalize a Location cell, stripping a leading text-indicator apostrophe.

    Args:
        value: Raw openpyxl cell value.

    Returns:
        Stripped text comparable to a wanted Location string.
    """
    text = _cell_text(value)
    if len(text) >= 2 and text[0] == "'" and text[1] in _FORMULA_MARKERS:
        return text[1:]
    return text


def _assign_location_text(cell, new_value: str) -> None:
    """Write Location as Excel text so ``=+@-`` cannot become a formula.

    Args:
        cell: openpyxl cell to assign.
        new_value: Location text (borrower name, in-locker token, or owner).
    """
    text = "" if new_value is None else str(new_value)
    if text[:1] in _FORMULA_MARKERS:
        text = "'" + text
    cell.value = text
    cell.data_type = "s"


def _cell_text(value) -> str:
    """Normalize an Excel cell to a stripped string.

    Args:
        value: Raw openpyxl cell value.

    Returns:
        Stripped text, or empty string when the cell is empty.
        Integer-valued floats lose the ``.0`` tail (Excel PM cells).
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).strip()
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value).strip()


def _wanted_by_pm(session: Session) -> dict[str, str]:
    """Build PM → Location text for every locker device.

    Args:
        session: Active database session (autoflush sees in-request borrows).

    Returns:
        Mapping of stripped PM number to the location string to write.
    """
    wanted: dict[str, str] = {}
    for device in DeviceRepository.list_all(session):
        pm = (device.pm_number or "").strip()
        if not pm:
            continue
        loc = _location_value(device)
        if loc is None:
            continue
        wanted[pm_match_key(pm)] = loc
    return wanted


def _replace_into(tmp_path: Path, dest: Path) -> None:
    """Atomically replace ``dest`` with ``tmp_path``, retrying a lock.

    Args:
        tmp_path: Staged workbook in the same directory as ``dest``.
        dest: Target ``device-list.xlsx`` on the share or disk.

    Raises:
        PermissionError: Still locked after ``_MAX_RETRIES`` attempts.
        OSError: Replace failed for a reason other than a lock.
    """
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            tmp_path.replace(dest)
            return
        except PermissionError:
            if attempt < _MAX_RETRIES:
                logger.debug(
                    "Location write-back: %s is locked, retrying in %ss "
                    "(attempt %d/%d)",
                    dest, _RETRY_DELAY_SECONDS, attempt, _MAX_RETRIES,
                )
                time.sleep(_RETRY_DELAY_SECONDS)
            else:
                raise


def _write_once(path: Path, wanted: dict[str, str]) -> WritebackResult:
    """Copy, edit Location, and replace if anything changed.

    Args:
        path: Source ``device-list.xlsx``.
        wanted: PM number → Location text to write.

    Returns:
        Counts of written / unchanged / skipped PMs and whether a save ran.

    Raises:
        OSError: Copy, load, save, or replace failed (caller retries/logs).
    """
    result = WritebackResult()
    if not wanted:
        return result

    mtime = path.stat().st_mtime
    work_fd, work_str = tempfile.mkstemp(suffix=".xlsx")
    os.close(work_fd)
    work_path = Path(work_str)
    dest_path: Path | None = None
    wb = None
    try:
        shutil.copy2(path, work_path)
        wb = load_workbook(work_path)
        ws = wb.active
        headers = [
            str(cell.value).strip() if cell.value else ""
            for cell in ws[1]
        ]
        pm_idx = find_column(headers, pm_candidates())
        loc_idx = find_column(headers, location_candidates())
        if pm_idx is None:
            logger.warning(
                "Location write-back skipped — no PM column in %s (headers: %s).",
                path, headers,
            )
            result.error = "no_pm_column"
            return result
        if loc_idx is None:
            logger.warning(
                "Location write-back skipped — no Location column "
                "in %s (headers: %s).",
                path, headers,
            )
            result.error = "no_location_column"
            return result

        pm_col = pm_idx + 1
        loc_col = loc_idx + 1
        seen: set[str] = set()
        dirty = False
        for row_i in range(2, ws.max_row + 1):
            pm = _cell_text(ws.cell(row=row_i, column=pm_col).value)
            key = pm_match_key(pm)
            if not key or key not in wanted:
                continue
            seen.add(key)
            new_value = wanted[key]
            cell = ws.cell(row=row_i, column=loc_col)
            if _stored_location_text(cell.value) == new_value:
                result.unchanged += 1
                continue
            _assign_location_text(cell, new_value)
            result.written += 1
            dirty = True

        result.skipped = len(wanted) - len(seen)
        if not dirty:
            return result

        dest_fd, dest_str = tempfile.mkstemp(suffix=".xlsx", dir=path.parent)
        os.close(dest_fd)
        dest_path = Path(dest_str)
        wb.save(dest_path)
        wb.close()
        wb = None
        try:
            if path.stat().st_mtime != mtime:
                raise _StaleWorkbook()
        except FileNotFoundError:
            raise _StaleWorkbook() from None
        _replace_into(dest_path, path)
        dest_path = None
        result.saved = True
        logger.info(
            "Location write-back: %d written, %d unchanged, %d not in Excel (%s).",
            result.written, result.unchanged, result.skipped, path,
        )
        return result
    finally:
        if wb is not None:
            try:
                wb.close()
            except Exception:
                pass
        work_path.unlink(missing_ok=True)
        if dest_path is not None:
            dest_path.unlink(missing_ok=True)


def write_location_values(
    source_path: str | Path, wanted: dict[str, str], *, engine=None
) -> WritebackResult:
    """Write PM → Location cells. Never raises.

    Serializes application writers. On a stale-file retry, recomputes
    ``wanted`` from committed SQLite when ``engine`` is set. Equal CIFS
    mtime is not treated as proof the sheet is unchanged: the writer lock
    is the concurrency control; the mtime check only detects an external
    edit after our copy.

    Args:
        source_path: Path to ``device-list.xlsx``.
        wanted: Mapping of PM number to Location text (any spelling).
        engine: Optional SQLAlchemy engine; when set, ``wanted`` is rebuilt
            from SQLite on each attempt.

    Returns:
        WritebackResult. ``error`` is set when the workbook could not be
        written (missing, locked, no columns). ``saved`` is False when
        nothing changed or the file could not be written.
    """
    result = WritebackResult()
    try:
        path = Path(source_path) if source_path else None
        if path is None or not str(source_path).strip():
            result.error = "unconfigured"
            return result
        if not path.exists():
            logger.warning("Location write-back skipped — file not found: %s", path)
            result.error = "missing"
            return result

        keyed = {pm_match_key(k): v for k, v in (wanted or {}).items() if pm_match_key(k)}

        def _attempt(snapshot: dict[str, str]) -> WritebackResult:
            # Hold the lock for the whole workbook I/O, including after the
            # waiter's timeout: the daemon thread cannot be killed, and a
            # second writer must not _replace_into the same file on hung CIFS.
            with _excel_writer_lock:
                live = snapshot
                if engine is not None:
                    with Session(engine) as session:
                        live = _wanted_by_pm(session)
                return _write_once(path, live)

        try:
            for attempt in range(1, _MAX_RETRIES + 1):
                try:
                    result = _call_with_timeout(
                        _attempt, _IO_TIMEOUT_SECONDS, keyed
                    )
                    return result
                except TimeoutError:
                    logger.warning(
                        "Location write-back timed out after %ss (%s).",
                        _IO_TIMEOUT_SECONDS, path,
                    )
                    result.error = "timeout"
                    return result
                except _StaleWorkbook:
                    if attempt < _MAX_RETRIES:
                        logger.info(
                            "Location write-back: %s changed during edit, "
                            "retrying (%d/%d).",
                            path, attempt, _MAX_RETRIES,
                        )
                        continue
                    logger.warning(
                        "Location write-back skipped — %s kept changing during edit.",
                        path,
                    )
                    result.error = "stale"
                    return result
        except PermissionError:
            logger.warning(
                "Location write-back skipped — %s is locked (open in Excel).",
                path,
            )
            result.error = "locked"
            return result
        except OSError as e:
            logger.warning(
                "Location write-back skipped — %s unavailable or unwritable (%s).",
                path, e,
            )
            result.error = "unavailable"
            return result
        except Exception:
            logger.exception(
                "Location write-back failed for %s — locker database is unchanged.",
                path,
            )
            result.error = "failed"
            return result
        return result
    finally:
        if result is not None:
            _remember_writeback(result)


def _call_with_timeout(fn, timeout: float, *args):
    """Run ``fn`` in a worker thread and abort waiting after ``timeout``.

    The worker cannot be killed; the caller returns so the kiosk is not
    frozen on hung CIFS I/O.

    Args:
        fn: Callable to run.
        timeout: Seconds to wait.
        *args: Positional arguments for ``fn``.

    Returns:
        ``fn``'s return value.

    Raises:
        TimeoutError: Worker still running after ``timeout``.
        Exception: Whatever ``fn`` raised.
    """
    box: list = []
    err: list = []

    def _run() -> None:
        try:
            box.append(fn(*args))
        except Exception as exc:
            err.append(exc)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError("excel I/O timeout")
    if err:
        raise err[0]
    return box[0] if box else None


def write_location_value(
    source_path: str | Path, pm_number: str, value: str
) -> WritebackResult:
    """Write one PM's Location cell. Never raises.

    Args:
        source_path: Path to ``device-list.xlsx``.
        pm_number: Equipment number to match.
        value: Text to put in the Location cell.

    Returns:
        WritebackResult for that single PM.
    """
    pm = (pm_number or "").strip()
    if not pm:
        return WritebackResult(error="no_pm")
    return write_location_values(source_path, {pm: (value or "").strip()})


def write_location(session: Session, source_path: str | Path) -> WritebackResult:
    """Write locker location into the Location column. Never raises.

    Args:
        session: Active database session (reads devices; does not commit).
        source_path: Path to the source ``device-list.xlsx``.

    Returns:
        WritebackResult. ``saved`` is False when nothing changed or the
        file could not be written.
    """
    return write_location_values(source_path, _wanted_by_pm(session))


def write_location_with_engine(engine, source_path: str | Path) -> WritebackResult:
    """Write Location using a short-lived session on ``engine``. Never raises.

    Args:
        engine: SQLAlchemy engine (committed locker state).
        source_path: Path to ``device-list.xlsx``.

    Returns:
        WritebackResult from ``write_location``.
    """
    try:
        with Session(engine) as session:
            return write_location_values(source_path, _wanted_by_pm(session), engine=engine)
    except Exception:
        logger.exception(
            "Location write-back failed for %s — locker database is unchanged.",
            source_path,
        )
        failed = WritebackResult(error="failed")
        _remember_writeback(failed)
        return failed


def maybe_write_location(session: Session) -> None:
    """Write-back when ``SOURCE_EXCEL_PATH`` is set. Never raises.

    Args:
        session: The request/tap session that just changed borrow state.

    Returns:
        None.
    """
    try:
        import config.settings as settings

        path = settings.SOURCE_EXCEL_PATH
        if not path:
            return
        write_location(session, path)
    except Exception:
        logger.exception(
            "Location write-back failed — locker database is unchanged."
        )


def schedule_write_location() -> None:
    """Enqueue Location write-back on a worker thread. Never raises.

    Commits must already be visible on the engine. Empty SOURCE_EXCEL_PATH
    is a no-op. Tests that need the file written call
    ``flush_scheduled_writeback``.
    """
    try:
        import config.settings as settings

        path = settings.SOURCE_EXCEL_PATH
        if not path:
            return
        from smart_locker.database.engine import get_engine

        engine = get_engine()

        def _run() -> None:
            write_location_with_engine(engine, path)

        worker = threading.Thread(
            target=_run, daemon=True, name="location-writeback"
        )
        with _scheduled_lock:
            _scheduled_threads[:] = [t for t in _scheduled_threads if t.is_alive()]
            _scheduled_threads.append(worker)
        worker.start()
    except Exception:
        logger.exception(
            "Could not schedule Location write-back — locker database is unchanged."
        )


def flush_scheduled_writeback(timeout: float = 15.0) -> None:
    """Wait for scheduled write-back threads (tests).

    Args:
        timeout: Seconds to wait for each thread.
    """
    with _scheduled_lock:
        threads = list(_scheduled_threads)
        _scheduled_threads.clear()
    for worker in threads:
        worker.join(timeout)
