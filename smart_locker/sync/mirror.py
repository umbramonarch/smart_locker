"""
File: mirror.py
Description: The hidden catalog mirror. SQLite is the catalog; the .xlsx is a
             Pi-written copy of it. A database change marks the mirror dirty
             and the next tick regenerates the catalog sheet. A locked or
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
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from config.settings import in_locker_token
from smart_locker.database.models import Device, DeviceStatus
from smart_locker.database.repositories import (
    DeviceRepository,
    RegistrantRepository,
    TransactionRepository,
)
from smart_locker.services.device_catalog import (
    CatalogError,
    apply_place_word,
    canonical_place,
    display_location,
    is_registered,
    place_kind,
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
_reader_lock = threading.Lock()
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


_seq_lock = threading.Lock()
_seq_high_water = 0


def _note_seq(value) -> None:
    """Raise the local dirty-generation high-water mark past a persisted seq."""
    global _seq_high_water
    if isinstance(value, bool) or not isinstance(value, int):
        return
    with _seq_lock:
        _seq_high_water = max(_seq_high_water, value)


def _alloc_dirty_seq() -> int:
    """Allocate a dirty generation above every one seen this run.

    Dirty events get process-unique generations, so a ``mark_dirty`` that
    races a flush lands strictly above that flush's ``flushed_seq`` in the
    ``_save_state`` merge — it can never collide with a generation the
    tick allocated but had not persisted yet.
    """
    global _seq_high_water
    with _seq_lock:
        _seq_high_water += 1
        return _seq_high_water


def _valid_state_value(key: str, value) -> bool:
    """Whether a persisted state value has the shape its key expects.

    A hand-edited or poisoned ``mirror_state.json`` must not feed malformed
    values into merge comparisons or row fingerprints — bad values drop to
    the key's default instead.
    """
    if key in ("seeded", "pending_writes", "external_pending"):
        return isinstance(value, bool)
    if key in ("dirty_seq", "flushed_seq"):
        return isinstance(value, int) and not isinstance(value, bool)
    if key == "last_write_rows":
        return isinstance(value, list)
    if key in ("last_seen_mtime", "last_seen_size"):
        return value is None or (
            isinstance(value, (int, float)) and not isinstance(value, bool)
        )
    # external_decided_at / last_write_at / last_error
    return value is None or isinstance(value, str)


def _load_state() -> dict:
    """Read the persisted mirror state (defaults when missing/corrupt).

    Never raises: a poisoned state file falls back to per-key defaults so
    ``mark_dirty`` cannot re-raise through a borrow's ``after_commit``
    listener.
    """
    state = {
        "seeded": False,
        "pending_writes": False,
        "dirty_seq": 0,
        "flushed_seq": 0,
        "external_pending": False,
        "external_decided_at": None,
        "last_write_at": None,
        "last_write_rows": [],
        "last_seen_mtime": None,
        "last_seen_size": None,
        "last_error": None,
    }
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except Exception:
        return state
    if isinstance(data, dict):
        for key in state:
            if key in data and _valid_state_value(key, data[key]):
                state[key] = data[key]
        # Generations read from disk raise the allocator's floor so a seq
        # persisted before a restart is never handed out again.
        _note_seq(state["dirty_seq"])
        _note_seq(state["flushed_seq"])
    return state


def _save_state(state: dict) -> None:
    """Write the mirror state atomically. Never raises.

    Monotonic fields are merged with what is on disk so a writer whose
    snapshot predates a concurrent save cannot regress them: ``seeded``
    OR-merges (True is sticky), the ``dirty_seq``/``flushed_seq``
    generations take the max, and a disk ``pending_writes`` survives only
    while its dirty generation is newer than the merged ``flushed_seq`` —
    a dirty event that raced the flush is kept, while the flag the flush
    itself cleared stays cleared instead of resurrecting on every tick.
    ``external_pending`` follows the newer ``external_decided_at`` (an
    admin decision saved mid-tick outranks the tick's stale flag), and the
    write snapshot (``last_write_*``, ``last_seen_mtime``, ``last_error``)
    follows the newer ``last_write_at``.
    """
    path: Path | None = None
    tmp: Path | None = None
    try:
        path = _state_path()
        tmp = path.with_suffix(path.suffix + ".tmp")
        with _state_lock:
            try:
                disk = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                disk = {}
            if isinstance(disk, dict):
                for key in ("dirty_seq", "flushed_seq"):
                    disk_seq = disk.get(key)
                    if not _valid_state_value(key, disk_seq):
                        disk_seq = 0
                    state_seq = state.get(key)
                    if not _valid_state_value(key, state_seq):
                        state_seq = 0
                    state[key] = max(disk_seq, state_seq)
                # A racing save's generations raise the allocator floor so
                # the next dirty event still lands above them.
                _note_seq(state["dirty_seq"])
                _note_seq(state["flushed_seq"])
                # Strict ``is True``: a hand-edited truthy value must not
                # fake the sticky flags (a forged seeded=True would skip
                # adoption entirely). The pending flag is sticky only while
                # it names a dirty generation the flush did not cover — a
                # seq-less (pre-upgrade) flag is honored once, after which
                # the generations this save writes let the merge clear it.
                if disk.get("pending_writes") is True:
                    disk_dirty = disk.get("dirty_seq")
                    if not _valid_state_value("dirty_seq", disk_dirty):
                        disk_dirty = None
                    if (
                        disk_dirty is None
                        or disk_dirty > state["flushed_seq"]
                    ):
                        state["pending_writes"] = True
                if disk.get("seeded") is True:
                    state["seeded"] = True
                disk_decided = disk.get("external_decided_at")
                if not isinstance(disk_decided, str):
                    disk_decided = None
                state_decided = state.get("external_decided_at")
                if not isinstance(state_decided, str):
                    state_decided = None
                if (disk_decided or "") > (state_decided or ""):
                    state["external_pending"] = bool(
                        disk.get("external_pending")
                    )
                    state["external_decided_at"] = disk_decided
                disk_write = disk.get("last_write_at")
                if not isinstance(disk_write, str):
                    disk_write = None
                state_write = state.get("last_write_at")
                if not isinstance(state_write, str):
                    state_write = None
                if (disk_write or "") > (state_write or ""):
                    for key in (
                        "last_write_at", "last_write_rows",
                        "last_seen_mtime", "last_seen_size", "last_error",
                    ):
                        value = disk.get(key)
                        state[key] = (
                            value if _valid_state_value(key, value) else None
                        )
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
            tmp.replace(path)
    except Exception as e:
        logger.warning("Mirror state %s not written (%s).", path, e)
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
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
        # A cabinet unit's Location is already the derived canonical value —
        # canonicalizing a borrower literally named like a place word would
        # silently turn the name back into a token. Non-cabinet rows keep
        # canonical_place so stored place text normalizes once more.
        location = (
            display_location(device)
            if is_registered(device)
            else canonical_place(display_location(device))
        )
        rows.append([
            (device.pm_number or "").strip(),
            (device.name or "").strip(),
            (device.device_type or "").strip(),
            (device.manufacturer or "").strip(),
            (device.model or "").strip(),
            (device.serial_number or "").strip(),
            device.calibration_due.isoformat() if device.calibration_due else "",
            location,
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


def _stored_place(location: str | None) -> str | None:
    """Stored ``location`` for a sheet Location cell.

    Whole-word in-locker spellings ("Cabinet", "locker room", the token
    itself) collapse to the in-locker token — the row is then registerable
    instead of stranded in limbo (not a registrant name, not a locker
    place). Everything else keeps ``canonical_place`` (person names,
    free-text places, the maintenance token).
    """
    if is_in_locker_location(location or ""):
        return in_locker_token()
    return canonical_place(location) or None


# ---------------------------------------------------------------------------
# Status reporting
# ---------------------------------------------------------------------------

_last_write_lock = threading.Lock()
_last_write: dict | None = None


def _remember_write(result: dict) -> None:
    global _last_write
    snapshot = {
        "at": _now_iso(),
        # Categories only — the raw adapter text carries paths/header names
        # and this snapshot is exposed on the public /api/health.
        "error": (
            _error_category(str(result["error"]))
            if result.get("error")
            else None
        ),
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


def _flag_dirty(state: dict) -> None:
    """Record a new dirty event on the state.

    ``pending_writes`` is the flag the tick flushes on; ``dirty_seq`` is
    the generation ``_save_state`` compares with ``flushed_seq`` so a
    dirty event that raced a flush is not mistaken for the stale flag a
    pre-flush save left on disk.
    """
    state["pending_writes"] = True
    state["dirty_seq"] = max(
        state.get("dirty_seq") or 0, _alloc_dirty_seq()
    )


def mark_dirty() -> None:
    """Flag that the database catalog changed and the mirror owes a write."""
    state = _load_state()
    _flag_dirty(state)
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
            # Only person names seed the registrant list. "other" place
            # cells still exclude the whole-word locker spellings
            # ("Cabinet", "locker room" — places, not people) and the
            # literal word "maintenance" for sites whose configured
            # SMART_LOCKER_MAINTENANCE_TOKEN is a different word.
            if (
                row.location
                and place_kind(row.location) == "other"
                and not is_in_locker_location(row.location)
                and row.location.strip().casefold() != "maintenance"
            ):
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
                try:
                    DeviceRepository.update_metadata(session, existing, **updates)
                    # The maintenance word on a cabinet unit does the same
                    # thing as the dashboard action, adoption included.
                    apply_place_word(session, existing, row.location)
                    session.commit()
                except (IntegrityError, OperationalError, CatalogError):
                    # One bad row — or a raced borrow/locked-database flush
                    # inside apply_place_word — must not abort the whole
                    # adoption (and be retried on every tick).
                    session.rollback()
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
                device.location = _stored_place(row.location)
                session.commit()
            except IntegrityError:
                session.rollback()
                continue
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
    the next tick retries. The reader gate makes the abandonment bounded:
    while a wedged reader holds it, later reads fail fast as a timeout
    instead of leaking another thread on a dead share.
    """
    try:
        return _call_with_timeout(
            lambda: read_catalog_rows(path), _IO_TIMEOUT_SECONDS,
            gate=_reader_lock,
        )
    except TimeoutError:
        return None, "timeout"


def _stat_path(path: Path):
    """Stat ``path`` inside the reader I/O timebox.

    A dead ``hard``-mounted CIFS share blocks ``stat`` in-kernel; running it
    in the gated worker bounds the wait so a later tick (or the route that
    called us) is never wedged behind it.

    Raises:
        FileNotFoundError: the file does not exist.
        TimeoutError: stat did not finish in the I/O window — or a wedged
            reader still holds the gate.
        OSError: any other stat failure.
    """
    return _call_with_timeout(
        path.stat, _IO_TIMEOUT_SECONDS, gate=_reader_lock
    )


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
    if path is None:
        return [], "Mirror file is not available."
    # No existence pre-check: a stat on a dead share blocks in-kernel. The
    # timeboxed read produces the "File not found" error instead.
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
    if path is None:
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
                    # A unit registered after the last baseline write diffs
                    # as "added": its catalog fields stay SQLite-owned, but
                    # the maintenance word in Location still applies like
                    # it does on a "changed" diff.
                    try:
                        if apply_place_word(session, device, row.location):
                            session.commit()
                            applied += 1
                            continue
                        session.rollback()
                    except (IntegrityError, OperationalError, CatalogError):
                        session.rollback()
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
                        device.location = _stored_place(row.location)
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
                # The flush inside _apply_field can raise before commit is
                # reached (locked SQLite, a raced borrow in to_maintenance)
                # — the guard covers the field write and the commit alike.
                try:
                    applied_row = _apply_field(
                        session, device, diff["field"], row
                    )
                    if applied_row:
                        session.commit()
                    else:
                        session.rollback()
                except (IntegrityError, OperationalError, CatalogError):
                    session.rollback()
                    skipped += 1
                    continue
                if applied_row:
                    applied += 1
                else:
                    skipped += 1

    state["external_pending"] = False
    state["external_decided_at"] = _now_iso()
    _flag_dirty(state)
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
        device.location = _stored_place(row.location)
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
            # A cabinet unit's place is derived — except the maintenance
            # word, which does the same thing as the dashboard action.
            return apply_place_word(session, device, row.location)
        device.location = _stored_place(row.location)
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
    _flag_dirty(state)
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

        # The flush is not gated on ``seeded``: an adopted sheet's pending
        # write regenerates it, and a confirmed-missing path gets its mirror
        # file created while still unseeded. But an unseeded path where a
        # file DOES exist is a foreign workbook we never overwrite — the
        # read error is already in result["error"].
        if state.get("pending_writes"):
            if state.get("external_pending"):
                result["skipped"] = "external_changes"
            elif state["seeded"] or result.get("file_missing"):
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
        # Public JSON carries the category only — raw adapter text has
        # filesystem paths and header names.
        sync_status.record_error(trigger, _error_category(str(result["error"])))
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
    """First sight of the sheet: adopt its rows, or owe it a write.

    ``seeded`` means a real catalog sheet was adopted. A missing file only
    sets ``pending_writes`` — the share may simply not be mounted yet, and
    burning adoption then would flag the real sheet as a foreign edit when
    it appears later. A sheet that exists but is not a catalog workbook
    (no PM column, locked, unreadable) is never adopted — it is left alone
    and reported instead of being overwritten.
    """
    try:
        _stat_path(path)
    except FileNotFoundError:
        # No sheet yet — the pending write below creates the mirror file.
        _flag_dirty(state)
        result["file_missing"] = True
        return
    except OSError as e:
        err = "timeout" if isinstance(e, TimeoutError) else "unavailable"
        state["last_error"] = err
        result["error"] = err
        return
    parsed, err = _read_catalog(path)
    if err:
        state["last_error"] = _error_category(err)
        result["error"] = err
        return
    adopted = _adopt(engine, parsed)
    state["seeded"] = True
    _flag_dirty(state)
    # Baseline for hand-edit detection is the sheet as adopted — not the
    # regenerated rows — so the pending first write is not mistaken for a
    # hand edit.
    state["last_write_rows"] = _canonical_rows(parsed)
    try:
        st = _stat_path(path)
        state["last_seen_mtime"] = st.st_mtime
        state["last_seen_size"] = st.st_size
    except OSError:
        state["last_seen_mtime"] = None
        state["last_seen_size"] = None
    result["seeded"] = True
    result["adopted"] = adopted
    logger.info("Mirror adopted %d catalog row(s) from %s.", adopted, path)


def _detect(path: Path, workbook: WorkbookAdapter, state: dict, result: dict) -> None:
    """Compare the file to what the Pi last wrote; flag hand edits."""
    try:
        st = _stat_path(path)
    except FileNotFoundError:
        # File missing: nothing left to review — the database wins trivially
        # and a pending write recreates the mirror.
        state["last_seen_mtime"] = None
        state["last_seen_size"] = None
        state["external_pending"] = False
        state["external_decided_at"] = _now_iso()
        return
    except OSError as e:
        # Any other stat failure (dead share, wedged reader, I/O timeout)
        # must NOT clear a held review — the quarantine survives so a later
        # flush can never destroy unreviewed edits.
        err = "timeout" if isinstance(e, TimeoutError) else "unavailable"
        state["last_error"] = err
        result["error"] = err
        return
    if (
        st.st_mtime == state.get("last_seen_mtime")
        and st.st_size == state.get("last_seen_size")
    ):
        result["external"] = bool(state.get("external_pending"))
        return
    # Stamp the decision BEFORE the read: an admin dismiss/apply saved
    # mid-read carries a newer decided_at and wins the _save_state merge.
    state["external_decided_at"] = _now_iso()
    sheet_rows, err = _read_sheet(workbook)
    if err:
        state["last_error"] = _error_category(err)
        result["error"] = err
        return
    state["last_seen_mtime"] = st.st_mtime
    state["last_seen_size"] = st.st_size
    state["last_error"] = None
    written = state.get("last_write_rows") or []
    if _fingerprint(sheet_rows) != _fingerprint(written):
        state["external_pending"] = True
        result["external"] = True
        logger.info("Mirror %s changed outside the Pi — edits held for review.", path)
    else:
        state["external_pending"] = False


class _WriterBusy(Exception):
    """A write worker already holds (or waits on) the writer lock."""


# POSIX mount bases: a mirror file that should live on a share must never be
# created under an unmounted mountpoint — the write would land on the SD card
# and be masked when the mount returns (a shadow file).
_POSIX_MOUNT_ROOTS = frozenset(("/mnt", "/media", "/run/media"))


def _refuse_shadow_create(path: Path, create_only: bool) -> None:
    """Refuse a mirror creation that would land on the wrong filesystem.

    Only creation is guarded — rewriting an existing file is not a shadow
    write. Refused when the file appeared mid-tick while we are still
    unseeded (a real foreign sheet must be adopted, not rewritten), when the
    parent directory is missing, or — POSIX only — when an ancestor that is
    a direct child of /mnt, /media, or /run/media exists but is not a mount.

    Runs inside the write worker's I/O timebox: a hung stat there abandons
    the worker instead of wedging the tick.

    Raises:
        OSError: the write must defer instead of creating the file here.
    """
    if path.exists():
        if create_only:
            raise OSError(
                f"mirror file {path} appeared after adoption check — "
                "write deferred"
            )
        return
    if not path.parent.exists():
        raise OSError(f"mirror directory unavailable: {path.parent}")
    if os.name != "posix":
        return
    # Walk upward: the first mounted ancestor wins — a path under
    # /media/<user>/<stick> is fine while the stick is mounted even though
    # the /media/<user> dir above it is not itself a mount.
    for ancestor in path.parents:
        if os.path.ismount(ancestor):
            break
        if str(ancestor.parent) in _POSIX_MOUNT_ROOTS and ancestor.exists():
            raise OSError(
                f"mirror path {ancestor} exists but is not a mount — "
                "write deferred"
            )


def _flush(
    engine, path: Path, workbook: WorkbookAdapter, state: dict, result: dict
) -> None:
    """Regenerate the catalog sheet from the database. Serialized + timeboxed.

    ``expected_mtime`` binds the write to the stat ``_detect`` just took:
    a file that changed since refuses the write as a hand edit.
    """
    with Session(engine) as session:
        expected = _expected_rows(session)

    result["write_attempted"] = True
    expected_mtime = state.get("last_seen_mtime")
    create_only = not state["seeded"]

    def _write() -> bool:
        # A worker parked on the lock would write its own stale snapshot;
        # pending_writes stays set and the next tick retries instead.
        if not _writer_lock.acquire(blocking=False):
            raise _WriterBusy()
        try:
            _refuse_shadow_create(path, create_only)
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
        logger.warning(
            "Mirror write for %s skipped — a previous write worker is "
            "still running (wedged share?).",
            path,
        )
        result["write_attempted"] = False
        result["skipped"] = "writer_busy"
        state["last_error"] = "busy"
        return
    except TimeoutError:
        logger.warning("Mirror write timed out after %ss (%s).", _IO_TIMEOUT_SECONDS, path)
        # The abandoned worker can still land the write later; adopt the
        # intended rows as the baseline now so the late write is not flagged
        # as hand edits. pending_writes stays set — the next tick retries
        # (writer_busy while the worker is alive, then an idempotent rewrite).
        state["last_write_rows"] = expected
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
        st = _stat_path(path)
        state["last_seen_mtime"] = st.st_mtime
        state["last_seen_size"] = st.st_size
    except OSError:
        state["last_seen_mtime"] = None
        state["last_seen_size"] = None
    # A successful write IS the seed: this file's baseline is ours, so the
    # next tick detects hand edits against it instead of re-adopting the
    # file we just created (which would silently bless edits made between
    # creation and the following tick).
    state["seeded"] = True
    state["pending_writes"] = False
    # The flush covered every dirty event up to this generation — a disk
    # pending flag with a higher dirty_seq is a later event and survives.
    state["flushed_seq"] = state.get("dirty_seq") or 0
    state["external_pending"] = False
    state["external_decided_at"] = _now_iso()
    state["last_write_at"] = _now_iso()
    state["last_write_rows"] = expected
    state["last_error"] = None
    result["flushed"] = True
    result["written"] = len(expected)
    logger.info("Mirror wrote %d catalog row(s) to %s.", len(expected), path)


def _call_with_timeout(
    fn, timeout: float, gate: threading.Lock | None = None
):
    """Run ``fn`` in a worker thread and abort waiting after ``timeout``.

    The worker cannot be killed; the caller returns so the kiosk is not
    frozen on hung CIFS I/O. When ``gate`` is given the worker takes it
    non-blocking: while a wedged worker still holds it, the next call fails
    fast with ``TimeoutError`` instead of spawning (and leaking) another
    thread — so at most one worker is ever hung on the share.
    """
    box: list = []
    err: list = []

    def _run() -> None:
        if gate is not None and not gate.acquire(blocking=False):
            err.append(TimeoutError("workbook I/O busy"))
            return
        try:
            box.append(fn())
        except Exception as exc:
            err.append(exc)
        finally:
            if gate is not None:
                gate.release()

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
