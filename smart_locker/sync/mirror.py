"""
File: mirror.py
Description: The hidden catalog mirror. SQLite is the catalog; the .xlsx is a
             Pi-written copy of it. A database change marks the mirror dirty
             and the next tick regenerates the active sheet. A locked or
             missing file defers the write; a file changed by hand is listed
             on the dashboard for an admin to apply or keep — never merged
             silently. The first sight of a populated sheet adopts it as the
             catalog seed (the running-Pi migration).
Project: smart_locker/sync
Notes: Workbook I/O never raises into the kiosk path: borrows commit in
       SQLite no matter what the file does. State persists in
       ``mirror_state.json`` next to the database so a restart keeps pending
       writes and the external-edit gate.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from smart_locker.database.models import Device, DeviceStatus
from smart_locker.database.repositories import (
    DeviceRepository,
    RegistrantRepository,
    TransactionRepository,
)
from smart_locker.services.device_catalog import (
    canonical_place,
    display_location,
    is_registered,
)
from smart_locker.sync.catalog_sheet import (
    MIRROR_HEADERS,
    CatalogRow,
    is_in_locker_location,
    pm_match_key,
    read_catalog_rows,
)
from smart_locker.sync.workbook_adapter import (
    WorkbookAdapter,
    WorkbookStaleError,
)

logger = logging.getLogger(__name__)

_IO_TIMEOUT_SECONDS = 8.0
_writer_lock = threading.Lock()
_tick_lock = threading.Lock()
_state_lock = threading.Lock()
_scheduled_lock = threading.Lock()
_scheduled_threads: list[threading.Thread] = []

# Column order inside one canonical mirror row (must match MIRROR_HEADERS).
_FIELDS = [
    "pm", "name", "device_type", "manufacturer", "model",
    "serial_number", "calibration_due", "location",
]
_FIELD_LABELS = {
    "pm": "id",
    "name": "name",
    "device_type": "type",
    "manufacturer": "manufacturer",
    "model": "model",
    "serial_number": "serial",
    "calibration_due": "calibration",
    "location": "location",
}


class MirrorError(Exception):
    """Base for mirror failures the API maps to HTTP errors."""


class MirrorUnavailable(MirrorError):
    """The mirror file cannot be read right now."""


# ---------------------------------------------------------------------------
# Persisted state
# ---------------------------------------------------------------------------

def _state_path() -> Path:
    from config.settings import mirror_state_path

    return mirror_state_path()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _load_state() -> dict:
    """Read the persisted mirror state (defaults when missing/corrupt)."""
    state = {
        "seeded": False,
        "pending_writes": False,
        "external_pending": False,
        "external_decided_at": None,
        "last_write_at": None,
        "last_write_rows": [],
        "last_seen_mtime": None,
        "last_error": None,
    }
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return state
    except (OSError, json.JSONDecodeError, TypeError):
        return state
    if isinstance(data, dict):
        for key in state:
            if key in data:
                state[key] = data[key]
    return state


def _save_state(state: dict) -> None:
    """Write the mirror state atomically. Never raises.

    Monotonic fields are merged with what is on disk so a writer whose
    snapshot predates a concurrent save cannot regress them:
    ``pending_writes`` and ``seeded`` OR-merge (True is sticky),
    ``external_pending`` follows the newer ``external_decided_at`` (an
    admin decision saved mid-tick outranks the tick's stale flag), and the
    write snapshot (``last_write_*``, ``last_seen_mtime``, ``last_error``)
    follows the newer ``last_write_at``.
    """
    path = _state_path()
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with _state_lock:
            try:
                disk = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError):
                disk = {}
            if isinstance(disk, dict):
                if disk.get("pending_writes"):
                    state["pending_writes"] = True
                if disk.get("seeded"):
                    state["seeded"] = True
                if (disk.get("external_decided_at") or "") > (
                    state.get("external_decided_at") or ""
                ):
                    state["external_pending"] = disk["external_pending"]
                    state["external_decided_at"] = disk["external_decided_at"]
                if (disk.get("last_write_at") or "") > (
                    state.get("last_write_at") or ""
                ):
                    for key in (
                        "last_write_at", "last_write_rows",
                        "last_seen_mtime", "last_error",
                    ):
                        state[key] = disk.get(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
            tmp.replace(path)
    except OSError as e:
        logger.warning("Mirror state %s not written (%s).", path, e)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def configured_path() -> Path | None:
    """The mirror workbook path, or None when no mirror is configured."""
    from config.settings import mirror_path

    return mirror_path()


# ---------------------------------------------------------------------------
# Canonical rows
# ---------------------------------------------------------------------------

def _expected_rows(session: Session) -> list[list[str]]:
    """Canonical mirror rows for every catalog device, sorted by id."""
    rows: list[list[str]] = []
    for device in DeviceRepository.list_all(session):
        rows.append([
            (device.pm_number or "").strip(),
            (device.name or "").strip(),
            (device.device_type or "").strip(),
            (device.manufacturer or "").strip(),
            (device.model or "").strip(),
            (device.serial_number or "").strip(),
            device.calibration_due.isoformat() if device.calibration_due else "",
            canonical_place(display_location(device)),
        ])
    rows.sort(key=lambda r: pm_match_key(r[0]))
    return rows


def _canonical_rows(parsed: list[CatalogRow]) -> list[list[str]]:
    """Canonical mirror rows from parsed sheet rows (same shape as expected)."""
    rows: list[list[str]] = []
    for row in parsed:
        rows.append([
            (row.pm_number or "").strip(),
            (row.name or "").strip(),
            (row.device_type or "").strip(),
            (row.manufacturer or "").strip(),
            (row.model or "").strip(),
            (row.serial_number or "").strip(),
            row.calibration_due.isoformat() if row.calibration_due else "",
            canonical_place(row.location),
        ])
    rows.sort(key=lambda r: pm_match_key(r[0]))
    return rows


def _fingerprint(rows: list[list[str]]) -> str:
    """Stable content hash of canonical rows (independent of file bytes)."""
    return hashlib.sha256(json.dumps(rows).encode("utf-8")).hexdigest()


def _row_map(rows: list[list[str]]) -> dict[str, list[str]]:
    """pm_match_key → canonical row. First duplicate wins."""
    out: dict[str, list[str]] = {}
    for row in rows:
        key = pm_match_key(row[0])
        if key and key not in out:
            out[key] = row
    return out


def _write_values(rows: list[list[str]]) -> list[list]:
    """Convert canonical string rows to cell values (calibration as a date)."""
    from smart_locker.sync.catalog_sheet import parse_date

    out: list[list] = []
    for row in rows:
        values: list = list(row)
        values[6] = parse_date(row[6])
        out.append(values)
    return out


# ---------------------------------------------------------------------------
# Status reporting
# ---------------------------------------------------------------------------

_last_write_lock = threading.Lock()
_last_write: dict | None = None


def _remember_write(result: dict) -> None:
    global _last_write
    snapshot = {
        "at": _now_iso(),
        "error": result.get("error"),
        "saved": bool(result.get("flushed")),
        "written": int(result.get("written") or 0),
    }
    with _last_write_lock:
        _last_write = snapshot


def last_write() -> dict | None:
    """Last mirror write snapshot for ``/api/health`` (same shape as before)."""
    with _last_write_lock:
        return dict(_last_write) if _last_write else None


def mirror_status() -> dict:
    """Public mirror state for the dashboard status line and sync-status."""
    path = configured_path()
    state = _load_state()
    if path is None:
        summary = "unconfigured"
    elif state["external_pending"]:
        summary = "external_changes"
    elif state["pending_writes"]:
        summary = "pending"
    elif state["last_error"]:
        summary = "error"
    else:
        summary = "ok"
    return {
        "configured": path is not None,
        "state": summary,
        "seeded": state["seeded"],
        "pending_writes": state["pending_writes"],
        "external_changes": state["external_pending"],
        "last_write_at": state["last_write_at"],
        "last_error": state["last_error"],
    }


def mark_dirty() -> None:
    """Flag that the database catalog changed and the mirror owes a write."""
    state = _load_state()
    state["pending_writes"] = True
    _save_state(state)


def tick_in_progress() -> bool:
    """Whether a mirror tick holds the mutex right now."""
    if _tick_lock.acquire(blocking=False):
        _tick_lock.release()
        return False
    return True


# ---------------------------------------------------------------------------
# Adoption (the running-Pi migration)
# ---------------------------------------------------------------------------

def _adopt(engine, parsed: list[CatalogRow]) -> int:
    """Insert catalog rows the database does not know yet; refresh the rest.

    Existing rows keep locker fields (slot, tag, status, borrower); only
    catalog metadata updates. Person names in Location seed the registrant
    list one final time — people live in the database after this.

    Args:
        engine: SQLAlchemy engine.
        parsed: Catalog rows read from the sheet.

    Returns:
        Number of inserted rows.
    """
    inserted = 0
    with Session(engine) as session:
        registrant_names: set[str] = set()
        has_location = any(r.location is not None for r in parsed)
        for row in parsed:
            if row.location and not is_in_locker_location(row.location):
                registrant_names.add(row.location.strip())
            existing = DeviceRepository.find_by_pm(session, row.pm_number)
            if existing is not None:
                updates: dict = {}
                if "name" in row.present:
                    updates["name"] = row.name
                if "device_type" in row.present and row.device_type:
                    updates["device_type"] = row.device_type
                if "serial_number" in row.present and row.serial_number:
                    holder = DeviceRepository.find_by_serial(session, row.serial_number)
                    if holder is None or holder.id == existing.id:
                        updates["serial_number"] = row.serial_number
                if "manufacturer" in row.present and row.manufacturer:
                    updates["manufacturer"] = row.manufacturer
                if "model" in row.present and row.model:
                    updates["model"] = row.model
                if "calibration_due" in row.present and row.calibration_due is not None:
                    updates["calibration_due"] = row.calibration_due
                DeviceRepository.update_metadata(session, existing, **updates)
                session.commit()
                continue

            serial = row.serial_number
            if serial and DeviceRepository.find_by_serial(session, serial) is not None:
                serial = None
            try:
                device = DeviceRepository.create(
                    session,
                    name=row.name or row.pm_number,
                    device_type=row.device_type or "general",
                    pm_number=row.pm_number,
                    serial_number=serial,
                    manufacturer=row.manufacturer,
                    model=row.model,
                    calibration_due=row.calibration_due,
                )
            except IntegrityError:
                session.rollback()
                continue
            device.location = canonical_place(row.location) or None
            session.commit()
            inserted += 1
        if has_location:
            RegistrantRepository.sync_names(session, registrant_names)
            session.commit()
    return inserted


# ---------------------------------------------------------------------------
# External-edit detection and application
# ---------------------------------------------------------------------------

def _read_catalog(path: Path) -> tuple[list[CatalogRow] | None, str | None]:
    """Read the sheet under the same I/O timebox as writes.

    A hung share read must not wedge the tick (it holds the tick mutex) or
    park a dashboard request thread forever — the worker is abandoned and
    the next tick retries.
    """
    try:
        return _call_with_timeout(
            lambda: read_catalog_rows(path), _IO_TIMEOUT_SECONDS
        )
    except TimeoutError:
        return None, "timeout"


def _read_sheet(workbook: WorkbookAdapter) -> tuple[list[list[str]], str | None]:
    """Canonical rows currently in the file, or an error string."""
    parsed, err = _read_catalog(workbook.path)
    if err:
        return [], err
    return _canonical_rows(parsed), None


def _error_category(err: str) -> str:
    """Bucket a workbook error into a path-free category for public JSON.

    Adapter messages carry the filesystem path; the mirror status endpoint
    is public, so ``last_error`` keeps only the category.
    """
    text = err.lower()
    if "not found" in text:
        return "missing"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "locked" in text:
        return "locked"
    if "unavailable" in text:
        return "unavailable"
    if "empty" in text:
        return "empty"
    if "pm" in text or "catalog" in text:
        return "not_catalog"
    return "unreadable"


def _compute_diffs(
    sheet_rows: list[list[str]], written_rows: list[list[str]]
) -> list[dict]:
    """Field-level differences between the file and what the Pi last wrote.

    Args:
        sheet_rows: Canonical rows read from the file now.
        written_rows: Canonical rows from the last successful write.

    Returns:
        One dict per difference: ``added`` (new sheet row), ``removed``
        (sheet row deleted), or ``changed`` (one field differs).
    """
    sheet = _row_map(sheet_rows)
    written = _row_map(written_rows)
    diffs: list[dict] = []
    for key, row in sheet.items():
        old = written.get(key)
        if old is None:
            diffs.append({"pm_number": row[0], "kind": "added",
                          "sheet": row, "database": None})
            continue
        for i, field in enumerate(_FIELDS):
            if row[i] != old[i]:
                diffs.append({
                    "pm_number": row[0],
                    "kind": "changed",
                    "field": _FIELD_LABELS[field],
                    "sheet": row[i],
                    "database": old[i],
                })
    for key, row in written.items():
        if key not in sheet:
            diffs.append({"pm_number": row[0], "kind": "removed",
                          "sheet": None, "database": row})
    diffs.sort(key=lambda d: (pm_match_key(d["pm_number"]), d.get("field") or ""))
    return diffs


def external_diffs() -> tuple[list[dict], str | None]:
    """List the sheet's hand edits for the dashboard (recomputed live).

    Returns:
        ``(diffs, None)`` or ``([], error)`` when the file cannot be read.
    """
    path = configured_path()
    if path is None or not path.exists():
        return [], "Mirror file is not available."
    sheet_rows, err = _read_sheet(WorkbookAdapter(path))
    if err:
        return [], err
    state = _load_state()
    return _compute_diffs(sheet_rows, state["last_write_rows"]), None


def apply_external(engine) -> dict:
    """Apply the sheet's hand edits to the database.

    Added rows insert; changed cells update the matching catalog field
    (Location applies only on non-cabinet rows — a cabinet unit's place is
    derived); removed rows delete only when the unit is neither in the
    cabinet nor borrowed. Then the mirror converges on the next write.

    Returns:
        Counts: applied, skipped.
    """
    path = configured_path()
    if path is None or not path.exists():
        raise MirrorUnavailable("Mirror file is not available.")
    workbook = WorkbookAdapter(path)
    parsed, err = _read_catalog(workbook.path)
    if err:
        raise MirrorUnavailable(err)
    sheet_rows = _canonical_rows(parsed)
    sheet_map = {pm_match_key(r.pm_number): r for r in parsed}
    state = _load_state()
    diffs = _compute_diffs(sheet_rows, state["last_write_rows"])

    applied = 0
    skipped = 0
    with Session(engine) as session:
        for diff in diffs:
            row = sheet_map.get(pm_match_key(diff["pm_number"]))
            device = DeviceRepository.find_by_pm(session, diff["pm_number"])
            if diff["kind"] == "added":
                if row is None:
                    skipped += 1
                    continue
                if device is not None and is_registered(device):
                    skipped += 1
                    continue
                if device is None:
                    serial = row.serial_number
                    if serial and DeviceRepository.find_by_serial(session, serial) is not None:
                        serial = None
                    try:
                        device = DeviceRepository.create(
                            session,
                            name=row.name or row.pm_number,
                            device_type=row.device_type or "general",
                            pm_number=row.pm_number,
                            serial_number=serial,
                            manufacturer=row.manufacturer,
                            model=row.model,
                            calibration_due=row.calibration_due,
                        )
                        device.location = canonical_place(row.location) or None
                        session.commit()
                    except IntegrityError:
                        session.rollback()
                        skipped += 1
                        continue
                else:
                    try:
                        _apply_catalog_row(
                            session, device, row, include_location=True
                        )
                        session.commit()
                    except IntegrityError:
                        session.rollback()
                        skipped += 1
                        continue
                applied += 1
            elif diff["kind"] == "removed":
                if (
                    device is not None
                    and not is_registered(device)
                    and device.status != DeviceStatus.BORROWED
                    and TransactionRepository.count_for_device(session, device.id) == 0
                ):
                    session.delete(device)
                    session.commit()
                    applied += 1
                else:
                    skipped += 1
            elif diff["kind"] == "changed":
                if device is None or row is None:
                    skipped += 1
                    continue
                if _apply_field(session, device, diff["field"], row):
                    session.commit()
                    applied += 1
                else:
                    session.rollback()
                    skipped += 1

    state["external_pending"] = False
    state["external_decided_at"] = _now_iso()
    state["pending_writes"] = True
    _save_state(state)
    schedule_flush()
    return {"applied": applied, "skipped": skipped}


def _apply_catalog_row(
    session: Session, device: Device, row: CatalogRow, *, include_location: bool
) -> None:
    """Copy every sheet field onto an existing non-registered row."""
    updates = {}
    for key in ("name", "device_type", "manufacturer", "model"):
        value = getattr(row, key)
        if value:
            updates[key] = value
    if row.serial_number:
        holder = DeviceRepository.find_by_serial(session, row.serial_number)
        if holder is None or holder.id == device.id:
            updates["serial_number"] = row.serial_number
    if row.calibration_due is not None:
        updates["calibration_due"] = row.calibration_due
    DeviceRepository.update_metadata(session, device, **updates)
    if include_location and not is_registered(device):
        device.location = canonical_place(row.location) or None
        session.flush()


def _apply_field(session: Session, device: Device, field: str, row: CatalogRow) -> bool:
    """Apply one hand-edited sheet cell to the database field it maps to."""
    attr = {
        "name": "name",
        "type": "device_type",
        "manufacturer": "manufacturer",
        "model": "model",
        "serial": "serial_number",
        "calibration": "calibration_due",
        "location": "location",
    }.get(field)
    if attr is None:
        return False
    if attr == "location":
        if is_registered(device):
            return False
        device.location = canonical_place(row.location) or None
        session.flush()
        return True
    if attr == "serial_number":
        # An empty cell is a no-op like the other fields — sheet edits do
        # not clear stored values.
        if not row.serial_number:
            return False
        holder = DeviceRepository.find_by_serial(session, row.serial_number)
        if holder is not None and holder.id != device.id:
            return False
        DeviceRepository.update_metadata(
            session, device, serial_number=row.serial_number
        )
        return True
    if attr == "calibration_due":
        if row.calibration_due is None:
            return False
        DeviceRepository.update_metadata(
            session, device, calibration_due=row.calibration_due
        )
        return True
    value = getattr(row, attr)
    if not value:
        return False
    DeviceRepository.update_metadata(session, device, **{attr: value})
    return True


def dismiss_external() -> None:
    """Keep the database: drop the external-edit gate and flush over them.

    ``pending_writes`` is set so the scheduled flush actually rewrites the
    file — without it the rejected edits would sit in the sheet until the
    next unrelated catalog change.
    """
    state = _load_state()
    state["external_pending"] = False
    state["external_decided_at"] = _now_iso()
    state["pending_writes"] = True
    _save_state(state)
    schedule_flush()


# ---------------------------------------------------------------------------
# The tick
# ---------------------------------------------------------------------------

def tick(engine, trigger: str = "interval") -> dict:
    """One mirror cycle: adopt if first sight, detect hand edits, flush.

    Never raises into the kiosk path; every failure lands in ``error`` and
    the persisted state so the next tick retries.

    Args:
        engine: SQLAlchemy engine.
        trigger: startup | interval | change | manual — recorded for status.

    Returns:
        A summary dict (also recorded via ``sync_status`` and ``last_write``).
    """
    if not _tick_lock.acquire(blocking=False):
        return {"trigger": trigger, "skipped": "in_progress", "error": None}
    result = {
        "trigger": trigger, "seeded": False, "adopted": 0,
        "external": False, "flushed": False, "written": 0,
        "write_attempted": False,
        "skipped": None, "error": None,
    }
    try:
        path = configured_path()
        if path is None:
            result["skipped"] = "unconfigured"
            return result
        workbook = WorkbookAdapter(path)
        state = _load_state()

        if not state["seeded"]:
            _adopt_or_mark(engine, path, state, result)

        if state["seeded"]:
            _detect(path, workbook, state, result)

            if state.get("pending_writes"):
                if state.get("external_pending"):
                    result["skipped"] = "external_changes"
                else:
                    _flush(engine, path, workbook, state, result)

        _save_state(state)
        return result
    except Exception as e:
        logger.exception("Mirror tick failed.")
        result["error"] = str(e)
        return result
    finally:
        _tick_lock.release()
        if result.get("write_attempted"):
            _remember_write(result)
        if result.get("skipped") in (None, "external_changes"):
            _record_tick(trigger, result)


def _record_tick(trigger: str, result: dict) -> None:
    """Report one finished tick through the shared last-sync snapshot."""
    from smart_locker.sync import sync_status

    if result["error"]:
        sync_status.record_error(trigger, result["error"])
        return
    sync_status.record_result(
        trigger,
        SimpleNamespace(
            updated=int(result["written"] or 0) + int(result["adopted"] or 0),
            unchanged=0,
            errors=0,
        ),
    )


def _adopt_or_mark(engine, path: Path, state: dict, result: dict) -> None:
    """First sight of the sheet: adopt its rows, or mark seeded when absent.

    A sheet that exists but is not a catalog workbook (no PM column, locked,
    unreadable) is never adopted — it is left alone and reported instead of
    being overwritten.
    """
    if not path.exists():
        state["seeded"] = True
        # No sheet yet — the pending write below creates the mirror file.
        state["pending_writes"] = True
        return
    parsed, err = _read_catalog(path)
    if err:
        state["last_error"] = _error_category(err)
        result["error"] = err
        return
    adopted = _adopt(engine, parsed)
    state["seeded"] = True
    state["pending_writes"] = True
    # Baseline for hand-edit detection is the sheet as adopted — not the
    # regenerated rows — so the pending first write is not mistaken for a
    # hand edit.
    state["last_write_rows"] = _canonical_rows(parsed)
    try:
        state["last_seen_mtime"] = path.stat().st_mtime
    except OSError:
        state["last_seen_mtime"] = None
    result["seeded"] = True
    result["adopted"] = adopted
    logger.info("Mirror adopted %d catalog row(s) from %s.", adopted, path)


def _detect(path: Path, workbook: WorkbookAdapter, state: dict, result: dict) -> None:
    """Compare the file to what the Pi last wrote; flag hand edits."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        # File missing: nothing left to review — the database wins trivially
        # and a pending write recreates the mirror.
        state["last_seen_mtime"] = None
        state["external_pending"] = False
        state["external_decided_at"] = _now_iso()
        return
    if mtime == state.get("last_seen_mtime"):
        result["external"] = bool(state.get("external_pending"))
        return
    sheet_rows, err = _read_sheet(workbook)
    if err:
        state["last_error"] = _error_category(err)
        result["error"] = err
        return
    state["last_seen_mtime"] = mtime
    state["last_error"] = None
    written = state.get("last_write_rows") or []
    state["external_decided_at"] = _now_iso()
    if _fingerprint(sheet_rows) != _fingerprint(written):
        state["external_pending"] = True
        result["external"] = True
        logger.info("Mirror %s changed outside the Pi — edits held for review.", path)
    else:
        state["external_pending"] = False


class _WriterBusy(Exception):
    """A write worker already holds (or waits on) the writer lock."""


def _flush(
    engine, path: Path, workbook: WorkbookAdapter, state: dict, result: dict
) -> None:
    """Regenerate the active sheet from the database. Serialized + timeboxed.

    ``expected_mtime`` binds the write to the stat ``_detect`` just took:
    a file that changed since refuses the write as a hand edit.
    """
    with Session(engine) as session:
        expected = _expected_rows(session)

    result["write_attempted"] = True
    expected_mtime = state.get("last_seen_mtime")

    def _write() -> bool:
        # A worker parked on the lock would write its own stale snapshot;
        # pending_writes stays set and the next tick retries instead.
        if not _writer_lock.acquire(blocking=False):
            raise _WriterBusy()
        try:
            return workbook.write_sheet(
                MIRROR_HEADERS,
                _write_values(expected),
                expected_mtime=expected_mtime,
            )
        finally:
            _writer_lock.release()

    try:
        _call_with_timeout(_write, _IO_TIMEOUT_SECONDS)
    except _WriterBusy:
        result["write_attempted"] = False
        result["skipped"] = "writer_busy"
        state["last_error"] = "busy"
        return
    except TimeoutError:
        logger.warning("Mirror write timed out after %ss (%s).", _IO_TIMEOUT_SECONDS, path)
        state["last_error"] = "timeout"
        result["error"] = "timeout"
        return
    except WorkbookStaleError:
        # The file changed after _detect stat it — a hand edit, not a write.
        state["external_pending"] = True
        state["external_decided_at"] = _now_iso()
        result["external"] = True
        return
    except PermissionError:
        logger.warning("Mirror %s is locked (open in Excel) — write deferred.", path)
        state["last_error"] = "locked"
        result["error"] = "locked"
        return
    except OSError as e:
        logger.warning("Mirror %s unavailable (%s) — write deferred.", path, e)
        state["last_error"] = "unavailable"
        result["error"] = "unavailable"
        return
    except Exception:
        logger.exception("Mirror write failed for %s.", path)
        state["last_error"] = "failed"
        result["error"] = "failed"
        return

    try:
        state["last_seen_mtime"] = path.stat().st_mtime
    except OSError:
        state["last_seen_mtime"] = None
    state["pending_writes"] = False
    state["external_pending"] = False
    state["external_decided_at"] = _now_iso()
    state["last_write_at"] = _now_iso()
    state["last_write_rows"] = expected
    state["last_error"] = None
    result["flushed"] = True
    result["written"] = len(expected)
    logger.info("Mirror wrote %d catalog row(s) to %s.", len(expected), path)


def _call_with_timeout(fn, timeout: float):
    """Run ``fn`` in a worker thread and abort waiting after ``timeout``.

    The worker cannot be killed; the caller returns so the kiosk is not
    frozen on hung CIFS I/O. The writer lock stays held by the worker.
    """
    box: list = []
    err: list = []

    def _run() -> None:
        try:
            box.append(fn())
        except Exception as exc:
            err.append(exc)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError("workbook I/O timeout")
    if err:
        raise err[0]
    return box[0] if box else None


def schedule_flush() -> None:
    """Run a mirror tick on a worker thread. Never raises.

    Called after any committed catalog change (loans via the after-commit
    listener; dashboard edits by their routes). Tests that need the file
    written call ``flush_scheduled``.
    """
    try:
        from smart_locker.database.engine import get_engine

        engine = get_engine()

        def _run() -> None:
            tick(engine, trigger="change")

        worker = threading.Thread(
            target=_run, daemon=True, name="mirror-flush"
        )
        with _scheduled_lock:
            _scheduled_threads[:] = [t for t in _scheduled_threads if t.is_alive()]
            _scheduled_threads.append(worker)
        worker.start()
    except Exception:
        logger.exception("Could not schedule a mirror flush.")


def flush_scheduled(timeout: float = 15.0) -> None:
    """Wait for scheduled mirror-flush threads (tests).

    Args:
        timeout: Seconds to wait for each thread.
    """
    with _scheduled_lock:
        threads = list(_scheduled_threads)
        _scheduled_threads.clear()
    for worker in threads:
        worker.join(timeout)
