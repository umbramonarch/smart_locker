"""Bulk device import from Excel (.xlsx) files.

Reads devices from an Excel sheet and inserts them into the database,
skipping duplicates (by serial number).

Usage:
    python -m scripts.import_devices --file devices.xlsx

    # With custom column mapping (if your headers differ):
    python -m scripts.import_devices --file devices.xlsx --name-col "Device Name" --serial-col "S/N"

    # With optional type and slot columns:
    python -m scripts.import_devices --file devices.xlsx --type-col "Category" --slot-col "Locker"

    # Specify which sheet to read (default: first sheet):
    python -m scripts.import_devices --file devices.xlsx --sheet "Sheet2"

    # Dry run — preview what would be imported without writing to DB:
    python -m scripts.import_devices --file devices.xlsx --dry-run
"""

import argparse
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openpyxl import load_workbook

from config.logging_config import setup_logging
from smart_locker.database.engine import get_session, init_db
from smart_locker.database.repositories import DeviceRepository


def _find_column(headers: list[str], candidates: list[str]) -> int | None:
    """Find a column index by trying multiple possible header names (case-insensitive)."""
    for i, header in enumerate(headers):
        if header and header.strip().lower() in [c.lower() for c in candidates]:
            return i
    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bulk import devices from an Excel file."
    )
    parser.add_argument("--file", required=True, help="Path to the .xlsx file")
    parser.add_argument("--sheet", default=None, help="Sheet name (default: first sheet)")
    parser.add_argument("--name-col", default=None, help="Column header for device name")
    parser.add_argument("--serial-col", default=None, help="Column header for serial number")
    parser.add_argument("--type-col", default=None, help="Column header for device type (optional)")
    parser.add_argument("--slot-col", default=None, help="Column header for locker slot (optional)")
    parser.add_argument(
        "--default-type", default="general", help="Default device type if no type column (default: general)"
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to database")
    args = parser.parse_args()

    setup_logging()

    # Load Excel file
    file_path = Path(args.file)
    if not file_path.exists():
        print(f"ERROR: File not found: {file_path}")
        return

    print(f"Reading: {file_path}")
    wb = load_workbook(file_path, read_only=True, data_only=True)

    if args.sheet:
        if args.sheet not in wb.sheetnames:
            print(f"ERROR: Sheet '{args.sheet}' not found. Available: {wb.sheetnames}")
            return
        ws = wb[args.sheet]
    else:
        ws = wb.active
    print(f"Sheet: {ws.title}")

    # Read all rows
    rows = list(ws.iter_rows(values_only=True))
    if len(rows) < 2:
        print("ERROR: Sheet has no data rows (need header + at least one data row).")
        return

    # Parse headers (first row)
    headers = [str(h).strip() if h else "" for h in rows[0]]
    print(f"Headers found: {headers}")

    # Auto-detect or use explicit column mapping
    name_candidates = ["name", "device name", "device", "equipment", "equipment name", "item", "item name"]
    serial_candidates = ["serial", "serial number", "s/n", "sn", "serial no", "serial_number", "serialnumber"]
    type_candidates = ["type", "device type", "category", "device_type", "kind"]
    slot_candidates = ["slot", "locker slot", "locker", "locker_slot", "bay"]

    name_idx = _find_column(headers, [args.name_col] if args.name_col else name_candidates)
    serial_idx = _find_column(headers, [args.serial_col] if args.serial_col else serial_candidates)
    type_idx = _find_column(headers, [args.type_col] if args.type_col else type_candidates)
    slot_idx = _find_column(headers, [args.slot_col] if args.slot_col else slot_candidates)

    if name_idx is None:
        print(f"ERROR: Could not find a 'name' column. Headers: {headers}")
        print("Use --name-col to specify the column header for device name.")
        return
    if serial_idx is None:
        print(f"ERROR: Could not find a 'serial number' column. Headers: {headers}")
        print("Use --serial-col to specify the column header for serial number.")
        return

    print(f"Mapping: name='{headers[name_idx]}' (col {name_idx + 1}), serial='{headers[serial_idx]}' (col {serial_idx + 1})", end="")
    if type_idx is not None:
        print(f", type='{headers[type_idx]}' (col {type_idx + 1})", end="")
    if slot_idx is not None:
        print(f", slot='{headers[slot_idx]}' (col {slot_idx + 1})", end="")
    print()

    # Parse data rows
    devices = []
    skipped_empty = 0
    for row_num, row in enumerate(rows[1:], start=2):
        name = str(row[name_idx]).strip() if row[name_idx] else ""
        serial = str(row[serial_idx]).strip() if row[serial_idx] else ""

        if not name or not serial:
            skipped_empty += 1
            continue

        device_type = args.default_type
        if type_idx is not None and row[type_idx]:
            device_type = str(row[type_idx]).strip()

        locker_slot = None
        if slot_idx is not None and row[slot_idx]:
            try:
                locker_slot = int(row[slot_idx])
            except (ValueError, TypeError):
                pass

        devices.append({
            "name": name,
            "serial_number": serial,
            "device_type": device_type,
            "locker_slot": locker_slot,
            "row": row_num,
        })

    print(f"\nParsed {len(devices)} devices from {len(rows) - 1} data rows ({skipped_empty} skipped due to empty name/serial).")

    if not devices:
        print("Nothing to import.")
        return

    # Preview first 10
    print("\nPreview (first 10):")
    print(f"  {'Row':<5} {'Name':<30} {'Serial':<25} {'Type':<15} {'Slot'}")
    print(f"  {'---':<5} {'---':<30} {'---':<25} {'---':<15} {'---'}")
    for d in devices[:10]:
        slot_str = str(d["locker_slot"]) if d["locker_slot"] is not None else "-"
        print(f"  {d['row']:<5} {d['name']:<30} {d['serial_number']:<25} {d['device_type']:<15} {slot_str}")
    if len(devices) > 10:
        print(f"  ... and {len(devices) - 10} more")

    if args.dry_run:
        print("\n[DRY RUN] No changes written to database.")
        return

    # Import to database
    init_db()
    imported = 0
    duplicates = 0
    errors = 0

    with get_session() as session:
        # Get existing serial numbers to skip duplicates
        existing = DeviceRepository.list_all(session)
        existing_serials = {d.serial_number for d in existing}

        for d in devices:
            if d["serial_number"] in existing_serials:
                duplicates += 1
                continue
            try:
                DeviceRepository.create(
                    session,
                    name=d["name"],
                    device_type=d["device_type"],
                    serial_number=d["serial_number"],
                    locker_slot=d["locker_slot"],
                )
                existing_serials.add(d["serial_number"])
                imported += 1
            except Exception as e:
                errors += 1
                print(f"  ERROR row {d['row']}: {e}")

    print(f"\nDone: {imported} imported, {duplicates} duplicates skipped, {errors} errors.")


if __name__ == "__main__":
    main()
