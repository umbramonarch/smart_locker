"""
File: catalog_sheet.py
Description: Catalog workbook parsing — header auto-detection (English names
             plus aliases), row→catalog-field mapping, and the PM join-key
             normalizers shared by the mirror writer and the adoption reader.
Project: smart_locker/sync
Notes: Pure parsing helpers — no database or policy here. The mirror decides
       what a cell means; this module only turns sheet text into fields.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from config.settings import id_header_extras, in_locker_token, location_header_extras
from smart_locker.sync.workbook_adapter import WorkbookAdapter

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

# Canonical headers the mirror writes — every one is accepted by the
# candidate lists above so a regenerated sheet always parses back.
MIRROR_HEADERS = [
    "PM Number", "Name", "Type", "Manufacturer", "Model",
    "Serial Number", "Calibration Due", "Location",
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
        Candidate list used by the mirror reader and writer.
    """
    return _merge_aliases(PM_CANDIDATES, id_header_extras())


def location_candidates() -> list[str]:
    """Location-column header names: built-in aliases plus env extras.

    Returns:
        Candidate list used by the mirror reader and writer.
    """
    return _merge_aliases(LOCATION_CANDIDATES, location_header_extras())


@dataclass(frozen=True)
class CatalogRow:
    """Catalog fields parsed from one workbook row.

    ``present`` names the catalog fields whose columns exist on this
    sheet so callers can skip missing columns instead of wiping fields.
    """

    pm_number: str
    name: str
    device_type: str
    serial_number: str | None
    manufacturer: str | None
    model: str | None
    calibration_due: date | None
    location: str | None = None
    present: frozenset[str] = field(default_factory=frozenset)


class CatalogReadError(Exception):
    """The workbook is missing, locked, or has no PM column."""


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
    """Case-folded join key for catalog row matching.

    Args:
        value: Raw PM cell or SQLite ``pm_number``.

    Returns:
        ``normalize_pm`` then ``casefold``.
    """
    return normalize_pm(value).casefold()


def is_in_locker_location(value: str) -> bool:
    """Return True when a Location cell means the device is in the locker.

    Exact ``in_locker_token()`` match, or a whole-word locker/cabinet marker.
    ``"Blocker"`` is not in-locker. Used when *reading* a sheet that was not
    written by the Pi; values the Pi writes itself use canonical tokens.

    Args:
        value: Location cell text.

    Returns:
        True if the text is a locker location, not a person name.
    """
    text = (value or "").strip().lower()
    if not text:
        return False
    token = (in_locker_token() or "").strip().lower()
    if token and text == token:
        return True
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


def detect_columns(
    headers: list[str],
    overrides: dict[str, str] | None = None,
) -> dict[str, int | None]:
    """Detect column indices from headers, with optional manual overrides.

    For each catalog field, tries to match the
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
        "manufacturer": find_column(headers, [ov["manufacturer"]] if ov.get("manufacturer") else MANUFACTURER_CANDIDATES),
        "model":        find_column(headers, [ov["model"]] if ov.get("model") else MODEL_CANDIDATES),
        "calibration":  find_column(headers, [ov["calibration"]] if ov.get("calibration") else CALIBRATION_CANDIDATES),
        "location":     find_column(headers, [ov["location"]] if ov.get("location") else location_candidates()),
    }


def _cell_str(row, idx: int | None) -> str | None:
    """Read a cell as a stripped string, or None.

    Uses ``stored_cell_text`` so a value the Pi wrote with a text-indicator
    apostrophe (``safe_cell_value`` on formula-marker text) reads back as
    the stored text — not the escaped form.

    Args:
        row: A tuple of cell values from openpyxl (one row of data).
        idx: Column index to read, or None to skip.

    Returns:
        The cell value as a stripped string, or None if the index is None,
        the cell is None, or the stripped string is empty.
    """
    if idx is None or row[idx] is None:
        return None
    val = stored_cell_text(row[idx])
    return val if val else None


def cell_text(value) -> str:
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


_FORMULA_MARKERS = frozenset("=+-@")


def stored_cell_text(value) -> str:
    """Normalize a cell, stripping a leading text-indicator apostrophe.

    Args:
        value: Raw openpyxl cell value.

    Returns:
        Stripped text comparable to a wanted string.
    """
    text = cell_text(value)
    if len(text) >= 2 and text[0] == "'" and text[1] in _FORMULA_MARKERS:
        return text[1:]
    return text


def safe_cell_value(value):
    """Return a workbook-safe value: text that cannot become a formula.

    Args:
        value: String (or None) to store.

    Returns:
        ``None`` for empty text, a ``date`` untouched, or the string with a
        leading apostrophe when it starts with a formula marker.
    """
    if isinstance(value, date):
        return value
    text = cell_text(value)
    if not text:
        return None
    if text[:1] in _FORMULA_MARKERS:
        text = "'" + text
    return text


def load_rows(
    path: Path,
    sheet_name: str | None = None,
) -> tuple[list | None, str | None]:
    """Copy the workbook to a temp file and return ``(rows, error)``.

    Copying first lets the read succeed when Excel has the share file open.

    Args:
        path: Path to the source ``.xlsx``.
        sheet_name: Sheet to read, or None for the catalog sheet.

    Returns:
        ``(rows, None)`` on success, or ``(None, error_message)``.
    """
    read = WorkbookAdapter(path).read_rows(sheet_name)
    return read.rows, read.error


def catalog_from_row(
    row,
    cols: dict[str, int | None],
    compose_name: bool,
    default_type: str,
) -> CatalogRow | None:
    """Parse one Excel data row into catalog fields, or None if PM is empty.

    Args:
        row: Tuple of cell values.
        cols: Column index map from ``detect_columns``.
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
        location=_cell_str(row, cols["location"]),
        present=frozenset(present),
    )


def read_catalog_rows(
    path: Path,
    sheet_name: str | None = None,
    column_overrides: dict[str, str] | None = None,
) -> tuple[list[CatalogRow], str | None]:
    """Parse a workbook's catalog sheet into catalog rows.

    Args:
        path: Workbook path.
        sheet_name: Sheet to read, or None for the catalog sheet.
        column_overrides: Optional header-name overrides.

    Returns:
        ``(rows, None)`` on success, or ``([], error_message)`` when the
        file is missing, locked, empty, or has no PM column.
    """
    rows, err = load_rows(Path(path), sheet_name)
    if err:
        return [], err
    if not rows:
        return [], "Workbook is empty."
    headers = [str(h).strip() if h else "" for h in rows[0]]
    cols = detect_columns(headers, column_overrides)
    if cols["pm"] is None:
        return [], f"Could not find PM/equipment column. Headers: {headers}"
    if len(rows) < 2:
        return [], None
    compose_name = cols["name"] is None
    parsed = [
        catalog_from_row(row, cols, compose_name, "general")
        for row in rows[1:]
    ]
    return [r for r in parsed if r is not None], None
