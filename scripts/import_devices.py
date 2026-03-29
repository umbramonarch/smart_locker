"""
File: import_devices.py
Description: Bulk device import from Excel (.xlsx) files. Reads devices from
             an Excel sheet and inserts/updates them in the database, skipping
             non-locker devices (only "schrank" slot values are imported).
             Supports German and English column headers with auto-detection.
Project: smart_locker/scripts
Notes: Usage: python -m scripts.import_devices --file devices.xlsx [--dry-run]
       Column headers can be overridden via CLI flags (--pm-col, --serial-col,
       --manufacturer-col, etc.). Duplicates are skipped by PM number.
"""

import argparse
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.logging_config import setup_logging
from smart_locker.database.engine import get_engine, init_db
from smart_locker.sync.source_import import (
    ImportResult,
    find_column,
    import_from_source_excel,
)


def main() -> None:
    """Parse CLI arguments and run a bulk device import from an Excel file.

    Reads an Excel file, auto-detects column headers (German or English),
    filters to locker-assigned ("schrank") devices, and inserts or updates
    them in the database. Supports dry-run mode and column override flags.

    Returns:
        None. Import summary is printed to stdout.
    """
    parser = argparse.ArgumentParser(
        description="Bulk import devices from an Excel file."
    )
    parser.add_argument("--file", required=True, help="Path to the .xlsx file")
    parser.add_argument("--sheet", default=None, help="Sheet name (default: first sheet)")
    # Column mapping overrides
    parser.add_argument("--pm-col", default=None, help="Column header for PM/equipment number")
    parser.add_argument("--name-col", default=None, help="Column header for device name")
    parser.add_argument("--serial-col", default=None, help="Column header for serial number")
    parser.add_argument("--type-col", default=None, help="Column header for device type/category")
    parser.add_argument("--slot-col", default=None, help="Column header for locker slot")
    parser.add_argument("--manufacturer-col", default=None, help="Column header for manufacturer")
    parser.add_argument("--model-col", default=None, help="Column header for model/type designation")
    parser.add_argument("--barcode-col", default=None, help="Column header for barcode")
    parser.add_argument("--calibration-col", default=None, help="Column header for calibration due date")
    # Defaults
    parser.add_argument(
        "--default-type", default="general", help="Default device type if no type column (default: general)"
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to database")
    args = parser.parse_args()

    setup_logging()

    file_path = Path(args.file)
    if not file_path.exists():
        print(f"ERROR: File not found: {file_path}")
        return

    # Build column overrides from CLI args
    overrides: dict[str, str] = {}
    for field_name, arg_val in [
        ("pm", args.pm_col),
        ("name", args.name_col),
        ("serial", args.serial_col),
        ("type", args.type_col),
        ("slot", args.slot_col),
        ("manufacturer", args.manufacturer_col),
        ("model", args.model_col),
        ("barcode", args.barcode_col),
        ("calibration", args.calibration_col),
    ]:
        if arg_val:
            overrides[field_name] = arg_val

    init_db()

    print(f"Importing from: {file_path}")
    result = import_from_source_excel(
        engine=get_engine(),
        source_path=file_path,
        sheet_name=args.sheet,
        dry_run=args.dry_run,
        default_type=args.default_type,
        column_overrides=overrides or None,  # Pass None if no overrides to use defaults
    )

    if args.dry_run:
        print("[DRY RUN] No changes written to database.")

    print(
        f"\nDone: {result.imported} imported, {result.updated} updated, "
        f"{result.unchanged} unchanged, {result.non_locker_skipped} non-locker skipped, "
        f"{result.errors} errors."
    )
    for detail in result.error_details:
        print(f"  ERROR: {detail}")


if __name__ == "__main__":
    main()
