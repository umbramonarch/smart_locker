"""Source Excel import — reads the company device master list and syncs to DB.

Extracts new devices and updates metadata on existing ones. Only devices
assigned to a locker (slot column starting with "schrank") are imported.

Can be called programmatically (by the scheduler) or via CLI scripts.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook
from sqlalchemy.orm import Session

from smart_locker.database.models import Device
from smart_locker.database.repositories import DeviceRepository

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Column auto-detection candidates (German + English)
# ---------------------------------------------------------------------------

PM_CANDIDATES = [
    "equipment", "pm number", "pm", "pm_number", "equipment number",
    "equipmentnumber", "inventarnummer",
]
NAME_CANDIDATES = [
    "name", "device name", "device", "equipment name", "item", "item name",
]
SERIAL_CANDIDATES = [
    "serial", "serial number", "s/n", "sn", "serial no", "serial_number",
    "serialnumber", "hersteller-serialnummer", "herstellerseriennummer",
    "seriennummer",
]
TYPE_CANDIDATES = [
    "type", "device type", "category", "device_type", "kind", "kategorie",
]
SLOT_CANDIDATES = [
    "slot", "locker slot", "locker", "locker_slot", "bay",
    "platz messmittelschrank", "platz", "messmittelschrank", "schrank",
]
DESC_CANDIDATES = [
    "description", "desc", "details", "notes", "beschreibung",
]
IMAGE_CANDIDATES = [
    "image", "photo", "image_path", "photo_path", "img", "picture", "filename",
]
MANUFACTURER_CANDIDATES = [
    "manufacturer", "make", "brand", "hersteller",
]
MODEL_CANDIDATES = [
    "model", "model name", "type designation",
    "typbezeichnung", "typ", "modell",
]
BARCODE_CANDIDATES = [
    "barcode", "bar code", "barcodenummer",
]
CALIBRATION_CANDIDATES = [
    "calibration due", "calibration_due", "next calibration",
    "datum der nächsten kalibrierung", "datum der nachsten kalibrierung",
    "datum der n\u00e4chsten kalibrierung",
    "nächste kalibrierung", "kalibrierung", "kalibrierdatum",
]


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ImportResult:
    """Summary of a source import run."""
    imported: int = 0
    updated: int = 0
    unchanged: int = 0
    non_locker_skipped: int = 0
    errors: int = 0
    error_details: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def find_column(headers: list[str], candidates: list[str]) -> int | None:
    """Find a column index by trying multiple possible header names (case-insensitive)."""
    lower_candidates = [c.lower() for c in candidates]
    for i, header in enumerate(headers):
        if header and header.strip().lower() in lower_candidates:
            return i
    return None


def parse_date(value) -> date | None:
    """Parse a date from an Excel cell value (datetime object or string)."""
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
    """Detect column indices from headers, with optional manual overrides."""
    ov = overrides or {}
    return {
        "pm":           find_column(headers, [ov["pm"]] if ov.get("pm") else PM_CANDIDATES),
        "name":         find_column(headers, [ov["name"]] if ov.get("name") else NAME_CANDIDATES),
        "serial":       find_column(headers, [ov["serial"]] if ov.get("serial") else SERIAL_CANDIDATES),
        "type":         find_column(headers, [ov["type"]] if ov.get("type") else TYPE_CANDIDATES),
        "slot":         find_column(headers, [ov["slot"]] if ov.get("slot") else SLOT_CANDIDATES),
        "desc":         find_column(headers, DESC_CANDIDATES),
        "image":        find_column(headers, IMAGE_CANDIDATES),
        "manufacturer": find_column(headers, [ov["manufacturer"]] if ov.get("manufacturer") else MANUFACTURER_CANDIDATES),
        "model":        find_column(headers, [ov["model"]] if ov.get("model") else MODEL_CANDIDATES),
        "barcode":      find_column(headers, [ov["barcode"]] if ov.get("barcode") else BARCODE_CANDIDATES),
        "calibration":  find_column(headers, [ov["calibration"]] if ov.get("calibration") else CALIBRATION_CANDIDATES),
    }


def _cell_str(row, idx: int | None) -> str | None:
    """Read a cell as a stripped string, or None."""
    if idx is None or row[idx] is None:
        return None
    val = str(row[idx]).strip()
    return val if val else None


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
    """Import new devices and update existing ones from the company source Excel.

    Only devices with a slot column value starting with "schrank" are imported.
    Existing devices (matched by PM number) get metadata updated; status, borrower,
    locker_slot, image_path, and description are never overwritten.

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

    if not path.exists():
        logger.warning("Source Excel not found: %s", path)
        result.errors = 1
        result.error_details.append(f"File not found: {path}")
        return result

    logger.info("Reading source Excel: %s", path)
    wb = load_workbook(path, read_only=True, data_only=True)

    if sheet_name:
        if sheet_name not in wb.sheetnames:
            result.errors = 1
            result.error_details.append(f"Sheet '{sheet_name}' not found. Available: {wb.sheetnames}")
            wb.close()
            return result
        ws = wb[sheet_name]
    else:
        ws = wb.active

    # Read all rows
    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    if len(rows) < 2:
        logger.warning("Source Excel has no data rows.")
        return result

    # Parse headers
    headers = [str(h).strip() if h else "" for h in rows[0]]
    cols = _detect_columns(headers, column_overrides)

    if cols["pm"] is None:
        result.errors = 1
        result.error_details.append(f"Could not find PM/equipment column. Headers: {headers}")
        return result

    slot_idx = cols["slot"]
    pm_idx = cols["pm"]
    compose_name = cols["name"] is None

    # --- First pass: build schrank auto-numbering ---
    schrank_row_indices: list[int] = []
    if slot_idx is not None:
        for data_idx, row in enumerate(rows[1:]):
            if row[slot_idx]:
                val = str(row[slot_idx]).strip().lower()
                if val.startswith("schrank"):
                    schrank_row_indices.append(data_idx)

    schrank_slot_map = {idx: slot_num for slot_num, idx in enumerate(schrank_row_indices, start=1)}

    # --- Second pass: parse rows, filter to schrank only ---
    parsed_devices: list[dict] = []
    for data_idx, row in enumerate(rows[1:]):
        pm_number = _cell_str(row, pm_idx)
        if not pm_number:
            continue

        # Only import devices assigned to a locker
        if slot_idx is None:
            result.non_locker_skipped += 1
            continue
        raw_slot = str(row[slot_idx]).strip().lower() if row[slot_idx] else ""
        if not raw_slot.startswith("schrank"):
            result.non_locker_skipped += 1
            continue

        manufacturer = _cell_str(row, cols["manufacturer"])
        model_val = _cell_str(row, cols["model"])

        if compose_name:
            name_parts = [pm_number]
            if manufacturer:
                name_parts.append(manufacturer)
            if model_val:
                name_parts.append(model_val)
            name = " ".join(name_parts)
        else:
            name = _cell_str(row, cols["name"]) or pm_number

        serial = _cell_str(row, cols["serial"])

        device_type = default_type
        if cols["type"] is not None and row[cols["type"]]:
            device_type = str(row[cols["type"]]).strip()

        locker_slot = schrank_slot_map.get(data_idx)

        description = _cell_str(row, cols["desc"])
        image_path = _cell_str(row, cols["image"])
        barcode = _cell_str(row, cols["barcode"])

        calibration_due = None
        if cols["calibration"] is not None:
            calibration_due = parse_date(row[cols["calibration"]])

        parsed_devices.append({
            "pm_number": pm_number,
            "name": name,
            "serial_number": serial,
            "device_type": device_type,
            "locker_slot": locker_slot,
            "description": description,
            "image_path": image_path,
            "manufacturer": manufacturer,
            "model": model_val,
            "barcode": barcode,
            "calibration_due": calibration_due,
        })

    logger.info(
        "Parsed %d locker devices (%d non-locker skipped).",
        len(parsed_devices), result.non_locker_skipped,
    )

    if not parsed_devices or dry_run:
        return result

    # --- Import to database ---
    from smart_locker.database.engine import get_session
    from smart_locker.sync.excel_sync import export_to_excel

    with get_session() as session:
        for d in parsed_devices:
            try:
                existing = DeviceRepository.find_by_pm(session, d["pm_number"])
                if existing is None:
                    # New device — insert
                    DeviceRepository.create(
                        session,
                        name=d["name"],
                        device_type=d["device_type"],
                        pm_number=d["pm_number"],
                        serial_number=d["serial_number"],
                        locker_slot=d["locker_slot"],
                        description=d["description"],
                        image_path=d["image_path"],
                        manufacturer=d["manufacturer"],
                        model=d["model"],
                        barcode=d["barcode"],
                        calibration_due=d["calibration_due"],
                    )
                    result.imported += 1
                else:
                    # Existing device — update metadata only
                    changed = DeviceRepository.update_metadata(
                        session,
                        existing,
                        name=d["name"],
                        device_type=d["device_type"],
                        serial_number=d["serial_number"],
                        manufacturer=d["manufacturer"],
                        model=d["model"],
                        barcode=d["barcode"],
                        calibration_due=d["calibration_due"],
                    )
                    if changed:
                        result.updated += 1
                    else:
                        result.unchanged += 1
            except Exception as e:
                result.errors += 1
                result.error_details.append(f"PM {d['pm_number']}: {e}")
                logger.error("Import error for PM %s: %s", d["pm_number"], e)

    # Trigger output Excel sync
    if result.imported > 0 or result.updated > 0:
        try:
            export_to_excel(engine)
        except Exception as e:
            logger.warning("Excel sync after import failed: %s", e)

    logger.info(
        "Source import done: %d imported, %d updated, %d unchanged, %d errors.",
        result.imported, result.updated, result.unchanged, result.errors,
    )
    return result
