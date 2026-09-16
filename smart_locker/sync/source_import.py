"""
File: source_import.py
Description: Source Excel import — reads the device catalog spreadsheet and
             refreshes catalog metadata on locker devices already in SQLite.
             Never inserts a locker row (Register Device does that). Column
             headers are matched by common English names (and a few aliases).
Project: smart_locker/sync
Notes: Called by the scheduler, ``python -m scripts.sync_source``, or
       POST /api/admin/sync-source. Status, borrower, slot, image,
       description, and tag_hmac are never overwritten. A Slot/cabinet
       column is unused. lookup_catalog_by_pm is the Register Device lookup;
       list_in_locker_catalog is its "pick from the Excel locker list" feed.
"""

import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zipfile import BadZipFile

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from config.settings import id_header_extras, in_locker_token, location_header_extras
from smart_locker.database.repositories import DeviceRepository

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Column auto-detection (English names, plus common spreadsheet aliases)
# ---------------------------------------------------------------------------

PM_CANDIDATES = [
    "equipment", "pm number", "pm", "pm_number", "equipment number",
    "equipmentnumber", "asset number", "asset",
]
NAME_CANDIDATES = [
    "name", "device name", "device", "equipment name", "item", "item name",
]
SERIAL_CANDIDATES = [
    "serial", "serial number", "s/n", "sn", "serial no", "serial_number",
    "serialnumber",
]
TYPE_CANDIDATES = [
    "type", "device type", "category", "device_type", "kind",
]
SLOT_CANDIDATES = [
    "slot", "locker slot", "locker", "locker_slot", "bay", "cabinet",
]
DESC_CANDIDATES = [
    "description", "desc", "details", "notes",
]
IMAGE_CANDIDATES = [
    "image", "photo", "image_path", "photo_path", "img", "picture", "filename",
]
MANUFACTURER_CANDIDATES = [
    "manufacturer", "make", "brand",
]
MODEL_CANDIDATES = [
    "model", "model name", "type designation",
]
CALIBRATION_CANDIDATES = [
    "calibration due", "calibration_due", "next calibration",
    "calibration date", "cal due",
]
LOCATION_CANDIDATES = [
    "location", "current location", "current owner", "owner",
    "assigned to", "held by",
]

# Whole-word in-locker markers (not substrings — "locker" is not in "blocker").
_IN_LOCKER_MARKERS = ("locker", "cabinet")


def _merge_aliases(base: list[str], extras: list[str]) -> list[str]:
    """Return extras then built-in names, de-duplicated, lowercased.

    Args:
        base: Built-in candidate list.
        extras: Site aliases from env.

    Returns:
        Combined candidate names for ``find_column``.
    """
    seen: set[str] = set()
    out: list[str] = []
    for name in list(extras) + list(base):
        key = (name or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def pm_candidates() -> list[str]:
    """Join-key header names: built-in English aliases plus env extras.

    Returns:
        Candidate list used by import, Register Device, and Location write-back.
    """
    return _merge_aliases(PM_CANDIDATES, id_header_extras())


def location_candidates() -> list[str]:
    """Location-column header names: built-in aliases plus env extras.

    Returns:
        Candidate list used by import (registrants) and Location write-back.
    """
    return _merge_aliases(LOCATION_CANDIDATES, location_header_extras())


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ImportResult:
    """Summary of a source import run with per-category counts.

    ``imported`` stays 0 — Sync never inserts locker rows. ``non_locker_skipped``
    is Excel PMs that are not already in SQLite. Also replaces the registrant
    list with person names currently in the Location column.
    """
    imported: int = 0
    updated: int = 0
    unchanged: int = 0
    non_locker_skipped: int = 0
    errors: int = 0
    error_details: list[str] = field(default_factory=list)
    registrants_added: int = 0


@dataclass(frozen=True)
class CatalogRow:
    """Catalog fields copied from one Excel PM row into a locker device.

    ``present`` names the catalog fields whose Excel columns exist on this
    sheet so import can skip missing columns instead of wiping SQLite.
    """

    pm_number: str
    name: str
    device_type: str
    serial_number: str | None
    manufacturer: str | None
    model: str | None
    calibration_due: date | None
    present: frozenset[str] = field(default_factory=frozenset)


class CatalogReadError(Exception):
    """Source Excel is missing, locked, or has no PM column."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_pm(value) -> str:
    """Canonical PM text: strip, drop Excel ``1001.0`` float tails.

    Args:
        value: Raw Excel or SQLite PM cell.

    Returns:
        Stripped PM string, integer-valued floats without ``.0``.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).strip()
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value == int(value):
            return str(int(value))
        return str(value).strip()
    text = str(value).strip()
    if text.endswith(".0"):
        head = text[:-2]
        if head.isdigit() or (head.startswith("-") and head[1:].isdigit()):
            return head
    return text


def pm_match_key(value) -> str:
    """Case-folded join key for Inventory / write-back / import.

    Args:
        value: Raw PM cell or SQLite ``pm_number``.

    Returns:
        ``normalize_pm`` then ``casefold``.
    """
    return normalize_pm(value).casefold()


def is_own_locker_location(value: str) -> bool:
    """Return True when a Location cell is exactly this kiosk's locker.

    Case-insensitive exact match on ``in_locker_token()``; surrounding
    whitespace is ignored. Other cabinets are not this locker, so they do
    not match. Used for the Register Device pick list.

    Args:
        value: Location cell text.

    Returns:
        True if the text is exactly the in-locker token.
    """
    text = (value or "").strip().lower()
    if not text:
        return False
    token = (in_locker_token() or "").strip().lower()
    return bool(token) and text == token


def is_in_locker_location(value: str) -> bool:
    """Return True when a Location cell means the device is in a locker.

    Broad place-vs-person check for registrant extraction: exact
    ``in_locker_token()`` match, or a whole-word locker/cabinet marker.
    ``"Blocker"`` is not in-locker. A "Cabinet A" cell is a place, not a
    person — but only ``is_own_locker_location`` decides what belongs in
    this kiosk's Register Device pick list.

    Args:
        value: Location cell text.

    Returns:
        True if the text is a locker location, not a person name.
    """
    if is_own_locker_location(value):
        return True
    text = (value or "").strip().lower()
    if not text:
        return False
    return any(
        re.search(rf"(?<![a-z]){re.escape(marker)}(?![a-z])", text)
        for marker in _IN_LOCKER_MARKERS
    )


def find_column(headers: list[str], candidates: list[str]) -> int | None:
    """Find a column by candidate priority, then header order.

    ``location`` is preferred over ``owner`` when both headers exist.

    Args:
        headers: List of column header strings from the Excel file.
        candidates: Possible header names, highest priority first.

    Returns:
        Zero-based column index if found, or None if no match.
    """
    lower_headers = [
        (i, (header or "").strip().lower())
        for i, header in enumerate(headers)
    ]
    for cand in candidates:
        key = (cand or "").strip().lower()
        if not key:
            continue
        for i, header in lower_headers:
            if header == key:
                return i
    return None


def parse_date(value) -> date | None:
    """Parse a date from an Excel cell value (datetime object or string).

    Handles native ``datetime``/``date`` objects (common in openpyxl) and
    string values in DD.MM.YYYY, YYYY-MM-DD, or DD/MM/YYYY format.

    Args:
        value: Cell value from openpyxl — may be datetime, date, str, or None.

    Returns:
        A ``date`` object, or None if the value is empty or unparseable.
    """
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _detect_columns(
    headers: list[str],
    overrides: dict[str, str] | None = None,
) -> dict[str, int | None]:
    """Detect column indices from headers, with optional manual overrides.

    For each field (pm, name, serial, type, slot, etc.), tries to match the
    override value first (if provided), then falls back to the predefined
    candidate lists for auto-detection.

    Args:
        headers: List of column header strings from the Excel file.
        overrides: Optional mapping of field names to explicit header strings
                   (e.g. ``{"pm": "Equipment Nr"}``).

    Returns:
        Dict mapping field names to their detected column index (or None).
    """
    ov = overrides or {}
    return {
        "pm":           find_column(headers, [ov["pm"]] if ov.get("pm") else pm_candidates()),
        "name":         find_column(headers, [ov["name"]] if ov.get("name") else NAME_CANDIDATES),
        "serial":       find_column(headers, [ov["serial"]] if ov.get("serial") else SERIAL_CANDIDATES),
        "type":         find_column(headers, [ov["type"]] if ov.get("type") else TYPE_CANDIDATES),
        "slot":         find_column(headers, [ov["slot"]] if ov.get("slot") else SLOT_CANDIDATES),
        "desc":         find_column(headers, DESC_CANDIDATES),
        "image":        find_column(headers, IMAGE_CANDIDATES),
        "manufacturer": find_column(headers, [ov["manufacturer"]] if ov.get("manufacturer") else MANUFACTURER_CANDIDATES),
        "model":        find_column(headers, [ov["model"]] if ov.get("model") else MODEL_CANDIDATES),
        "calibration":  find_column(headers, [ov["calibration"]] if ov.get("calibration") else CALIBRATION_CANDIDATES),
        "location":     find_column(headers, [ov["location"]] if ov.get("location") else location_candidates()),
    }


def _cell_str(row, idx: int | None) -> str | None:
    """Read a cell as a stripped string, or None.

    Args:
        row: A tuple of cell values from openpyxl (one row of data).
        idx: Column index to read, or None to skip.

    Returns:
        The cell value as a stripped string, or None if the index is None,
        the cell is None, or the stripped string is empty.
    """
    if idx is None or row[idx] is None:
        return None
    val = str(row[idx]).strip()
    return val if val else None


def _load_rows(
    path: Path,
    sheet_name: str | None = None,
) -> tuple[list | None, str | None]:
    """Copy the workbook to a temp file and return ``(rows, error)``.

    Copying first lets the read succeed when Excel has the share file open.

    Args:
        path: Path to the source ``.xlsx``.
        sheet_name: Sheet to read, or None for the active sheet.

    Returns:
        ``(rows, None)`` on success, or ``(None, error_message)``.
    """
    if not path.exists():
        return None, f"File not found: {path}"

    tmp_fd, tmp_path_str = tempfile.mkstemp(suffix=".xlsx")
    tmp_path = Path(tmp_path_str)
    try:
        os.close(tmp_fd)
        tmp_fd = -1
        shutil.copy2(path, tmp_path)
    except PermissionError:
        tmp_path.unlink(missing_ok=True)
        return None, f"Source file locked: {path}"
    except OSError as e:
        tmp_path.unlink(missing_ok=True)
        return None, f"Source file unavailable: {path} ({e})"
    finally:
        if tmp_fd >= 0:
            os.close(tmp_fd)

    try:
        wb = load_workbook(tmp_path, read_only=True, data_only=True)
        if sheet_name:
            if sheet_name not in wb.sheetnames:
                wb.close()
                return None, (
                    f"Sheet '{sheet_name}' not found. Available: {wb.sheetnames}"
                )
            ws = wb[sheet_name]
        else:
            ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        wb.close()
        return rows, None
    except (OSError, BadZipFile, InvalidFileException) as e:
        return None, f"Source workbook unreadable: {e}"
    finally:
        tmp_path.unlink(missing_ok=True)


def _catalog_from_row(
    row,
    cols: dict[str, int | None],
    compose_name: bool,
    default_type: str,
) -> CatalogRow | None:
    """Parse one Excel data row into catalog fields, or None if PM is empty.

    Args:
        row: Tuple of cell values.
        cols: Column index map from ``_detect_columns``.
        compose_name: True when the sheet has no name column (use manufacturer + model).
        default_type: Fallback device_type.

    Returns:
        CatalogRow, or None when the PM cell is empty.
    """
    pm_number = normalize_pm(_cell_str(row, cols["pm"]) or "")
    if not pm_number:
        return None

    manufacturer = _cell_str(row, cols["manufacturer"])
    model_val = _cell_str(row, cols["model"])
    name_cell = _cell_str(row, cols["name"])
    present: set[str] = set()
    if cols["name"] is not None and name_cell:
        present.add("name")
    if cols["type"] is not None and row[cols["type"]]:
        present.add("device_type")
    if cols["serial"] is not None and _cell_str(row, cols["serial"]):
        present.add("serial_number")
    if cols["manufacturer"] is not None and manufacturer:
        present.add("manufacturer")
    if cols["model"] is not None and model_val:
        present.add("model")

    if compose_name:
        name_parts = []
        if manufacturer:
            name_parts.append(manufacturer)
        if model_val:
            name_parts.append(model_val)
        name = " ".join(name_parts) if name_parts else pm_number
    else:
        name = name_cell or pm_number

    device_type = default_type
    if cols["type"] is not None and row[cols["type"]]:
        device_type = str(row[cols["type"]]).strip()

    calibration_due = None
    if cols["calibration"] is not None:
        calibration_due = parse_date(row[cols["calibration"]])
        if calibration_due is not None:
            present.add("calibration_due")

    return CatalogRow(
        pm_number=pm_number,
        name=name,
        device_type=device_type,
        serial_number=_cell_str(row, cols["serial"]),
        manufacturer=manufacturer,
        model=model_val,
        calibration_due=calibration_due,
        present=frozenset(present),
    )


def _load_catalog(
    source_path: str | Path,
    sheet_name: str | None,
    column_overrides: dict[str, str] | None,
) -> tuple[list, dict]:
    """Load source rows and detect columns, or raise ``CatalogReadError``.

    Args:
        source_path: Path to ``device-list.xlsx``.
        sheet_name: Sheet to read (default: active sheet).
        column_overrides: Optional header-name overrides.

    Returns:
        ``(rows, cols)`` — raw rows including the header row and the
        detected column map.

    Raises:
        CatalogReadError: File missing, locked, empty, or no PM column.
    """
    path = Path(source_path)
    rows, err = _load_rows(path, sheet_name)
    if err:
        raise CatalogReadError(err)
    if not rows or len(rows) < 2:
        raise CatalogReadError("Source Excel has no data rows.")

    headers = [str(h).strip() if h else "" for h in rows[0]]
    cols = _detect_columns(headers, column_overrides)
    if cols["pm"] is None:
        raise CatalogReadError(f"Could not find PM/equipment column. Headers: {headers}")
    return rows, cols


def lookup_catalog_by_pm(
    source_path: str | Path,
    pm_number: str,
    sheet_name: str | None = None,
    default_type: str = "general",
    column_overrides: dict[str, str] | None = None,
) -> CatalogRow | None:
    """Return catalog fields for one PM from the source Excel, or None.

    Args:
        source_path: Path to ``device-list.xlsx``.
        pm_number: Equipment number to match (stripped; compared as stored).
        sheet_name: Sheet to read (default: active sheet).
        default_type: Device type when the sheet has no category column.
        column_overrides: Optional header-name overrides.

    Returns:
        CatalogRow if the PM is on the sheet, otherwise None.

    Raises:
        CatalogReadError: File missing, locked, empty, or no PM column.
    """
    rows, cols = _load_catalog(source_path, sheet_name, column_overrides)

    want = pm_match_key(pm_number)
    compose_name = cols["name"] is None
    for row in rows[1:]:
        catalog = _catalog_from_row(row, cols, compose_name, default_type)
        if catalog is not None and pm_match_key(catalog.pm_number) == want:
            return catalog
    return None


def list_in_locker_catalog(
    source_path: str | Path,
    sheet_name: str | None = None,
    default_type: str = "general",
    column_overrides: dict[str, str] | None = None,
) -> list[CatalogRow]:
    """Return catalog rows whose Location cell is this kiosk's locker.

    Args:
        source_path: Path to ``device-list.xlsx``.
        sheet_name: Sheet to read (default: active sheet).
        default_type: Device type when the sheet has no category column.
        column_overrides: Optional header-name overrides.

    Returns:
        ``CatalogRow`` for every data row whose Location passes
        ``is_own_locker_location`` (exact in-locker token — other cabinets
        are excluded); empty list when the sheet has no Location column.

    Raises:
        CatalogReadError: File missing, locked, empty, or no PM column.
    """
    rows, cols = _load_catalog(source_path, sheet_name, column_overrides)
    if cols["location"] is None:
        return []

    out: list[CatalogRow] = []
    seen: set[str] = set()
    compose_name = cols["name"] is None
    for row in rows[1:]:
        if not is_own_locker_location(_cell_str(row, cols["location"]) or ""):
            continue
        catalog = _catalog_from_row(row, cols, compose_name, default_type)
        if catalog is None:
            continue
        key = pm_match_key(catalog.pm_number)
        if key in seen:
            logger.warning(
                "Duplicate PM %s in locker catalog — keeping first row.",
                catalog.pm_number,
            )
            continue
        seen.add(key)
        out.append(catalog)
    return out


# ---------------------------------------------------------------------------
# Main import function
# ---------------------------------------------------------------------------

def import_from_source_excel(
    engine,
    source_path: str | Path,
    sheet_name: str | None = None,
    dry_run: bool = False,
    default_type: str = "general",
    column_overrides: dict[str, str] | None = None,
) -> ImportResult:
    """Refresh catalog metadata on locker devices that already exist in SQLite.

    Excel PMs that are not already locker rows are counted as skipped and
    never inserted. A Slot/cabinet column is ignored. Status, borrower,
    locker_slot, image_path, description, and tag_hmac are never overwritten.

    Args:
        engine: SQLAlchemy engine.
        source_path: Path to the .xlsx file.
        sheet_name: Specific sheet to read (default: active sheet).
        dry_run: If True, parse and report but don't write to DB.
        default_type: Default device_type when no type column is found.
        column_overrides: Dict mapping field names to header strings for manual column mapping.

    Returns:
        ImportResult with counts.
    """
    result = ImportResult()
    path = Path(source_path)

    logger.info("Reading source Excel: %s", path)
    rows, err = _load_rows(path, sheet_name)
    if err:
        logger.warning("%s", err)
        result.errors = 1
        result.error_details.append(err)
        return result

    if not rows or len(rows) < 2:
        logger.warning("Source Excel has no data rows.")
        return result

    headers = [str(h).strip() if h else "" for h in rows[0]]
    cols = _detect_columns(headers, column_overrides)

    if cols["pm"] is None:
        result.errors = 1
        result.error_details.append(f"Could not find PM/equipment column. Headers: {headers}")
        return result

    compose_name = cols["name"] is None

    # --- Registrant extraction: collect unique person names from ALL rows ---
    registrant_names: set[str] = set()
    if cols["location"] is not None:
        for row in rows[1:]:
            location = _cell_str(row, cols["location"])
            if location and not is_in_locker_location(location):
                registrant_names.add(location.strip())

    if registrant_names:
        logger.info(
            "Found %d unique registrant name(s) in the Location column.",
            len(registrant_names),
        )

    parsed: list[CatalogRow] = []
    for row in rows[1:]:
        catalog = _catalog_from_row(row, cols, compose_name, default_type)
        if catalog is None:
            continue
        parsed.append(catalog)

    logger.info("Parsed %d Excel PM row(s).", len(parsed))

    from sqlalchemy.orm import Session as EngineSession

    from smart_locker.sync.excel_sync import export_to_excel

    if engine is None:
        result.errors += 1
        result.error_details.append("No database engine.")
        return result

    session = EngineSession(engine)
    try:
        for catalog in parsed:
            try:
                existing = DeviceRepository.find_by_pm(session, catalog.pm_number)
                if existing is None:
                    result.non_locker_skipped += 1
                    continue
                serial = catalog.serial_number
                if "serial_number" in catalog.present and serial:
                    holder = DeviceRepository.find_by_serial(session, serial)
                    if holder is not None and holder.id != existing.id:
                        serial = None
                updates: dict = {}
                if "name" in catalog.present:
                    updates["name"] = catalog.name
                if "device_type" in catalog.present and catalog.device_type:
                    updates["device_type"] = catalog.device_type
                if "serial_number" in catalog.present and serial:
                    updates["serial_number"] = serial
                if "manufacturer" in catalog.present and catalog.manufacturer:
                    updates["manufacturer"] = catalog.manufacturer
                if "model" in catalog.present and catalog.model:
                    updates["model"] = catalog.model
                if "calibration_due" in catalog.present and catalog.calibration_due is not None:
                    updates["calibration_due"] = catalog.calibration_due
                changed = DeviceRepository.update_metadata(
                    session,
                    existing,
                    **updates,
                )
                if changed:
                    result.updated += 1
                else:
                    result.unchanged += 1
                if dry_run:
                    session.rollback()
                else:
                    session.commit()
            except Exception as e:
                session.rollback()
                result.errors += 1
                result.error_details.append(f"PM {catalog.pm_number}: {e}")
                logger.error("Import error for PM %s: %s", catalog.pm_number, e)
    finally:
        session.close()

    if dry_run:
        logger.info(
            "Source import DRY RUN: %d would update, %d unchanged, "
            "%d not in locker, %d errors (nothing written).",
            result.updated, result.unchanged, result.non_locker_skipped, result.errors,
        )
        return result

    if cols["location"] is not None:
        from smart_locker.database.repositories import RegistrantRepository

        try:
            with EngineSession(engine) as reg_session:
                added = RegistrantRepository.sync_names(reg_session, registrant_names)
                result.registrants_added = added
                reg_session.commit()
        except Exception as e:
            logger.warning("Registrant name sync failed: %s", e)
            result.errors += 1
            result.error_details.append(f"Registrant sync: {e}")

    if result.updated > 0:
        from config.settings import EXCEL_AUTO_EXPORT, EXCEL_SYNC_PATH
        if EXCEL_AUTO_EXPORT:
            try:
                export_to_excel(engine, EXCEL_SYNC_PATH)
            except Exception as e:
                logger.warning("Excel sync after import failed: %s", e)

    logger.info(
        "Source import done: %d updated, %d unchanged, %d not in locker, %d errors, "
        "%d registrants added.",
        result.updated, result.unchanged, result.non_locker_skipped, result.errors,
        result.registrants_added,
    )
    return result

