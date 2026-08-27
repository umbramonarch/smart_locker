"""
File: sync_source.py
Description: Manually trigger source Excel import from the device catalog
             spreadsheet, then write locker Location back.
             Updates catalog metadata on existing locker devices; never
             inserts new locker rows. Dry-run skips both DB and Excel writes.
Project: smart_locker/scripts
Notes: Usage: python -m scripts.sync_source [--file path] [--dry-run]
       Defaults to SMART_LOCKER_SOURCE_EXCEL_PATH from .env if --file is omitted.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.logging_config import setup_logging
from config.settings import SOURCE_EXCEL_PATH
from smart_locker.database.engine import get_engine, init_db
from smart_locker.sync.source_import import import_from_source_excel


def main() -> None:
    """Parse CLI arguments and trigger a source Excel import.

    Reads the device catalog spreadsheet (from ``--file`` or the
    ``SMART_LOCKER_SOURCE_EXCEL_PATH`` env var) and updates catalog
    metadata on locker devices already in SQLite. Supports dry-run preview.

    Returns:
        None. Import summary is printed to stdout.
    """
    parser = argparse.ArgumentParser(description="Update locker catalog from source Excel.")
    parser.add_argument("--file", default=None, help="Path to source Excel (default: from env)")
    parser.add_argument("--sheet", default=None, help="Sheet name (default: first sheet)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()

    setup_logging()

    source_path = args.file or SOURCE_EXCEL_PATH
    if not source_path:
        print("ERROR: No source path. Use --file or set SMART_LOCKER_SOURCE_EXCEL_PATH.")
        return

    init_db()

    print(f"Importing from: {source_path}")
    result = import_from_source_excel(
        engine=get_engine(),
        source_path=source_path,
        sheet_name=args.sheet,
        dry_run=args.dry_run,
    )

    if args.dry_run:
        print("[DRY RUN] No changes written to database.")
    else:
        from smart_locker.sync.location_writeback import write_location_with_engine

        write_location_with_engine(get_engine(), source_path)

    print(
        f"\nDone: {result.imported} imported, {result.updated} updated, "
        f"{result.unchanged} unchanged, {result.non_locker_skipped} not in locker, "
        f"{result.errors} errors."
    )
    for detail in result.error_details:
        print(f"  ERROR: {detail}")


if __name__ == "__main__":
    main()
