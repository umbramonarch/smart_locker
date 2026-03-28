"""Manually trigger source Excel import.

Usage:
    python -m scripts.sync_source                      # uses SMART_LOCKER_SOURCE_EXCEL_PATH
    python -m scripts.sync_source --file path/to/file   # explicit path
    python -m scripts.sync_source --dry-run             # preview without writing
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
    parser = argparse.ArgumentParser(description="Import new devices from source Excel.")
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

    print(
        f"\nDone: {result.imported} imported, {result.updated} updated, "
        f"{result.unchanged} unchanged, {result.non_locker_skipped} non-locker skipped, "
        f"{result.errors} errors."
    )
    for detail in result.error_details:
        print(f"  ERROR: {detail}")


if __name__ == "__main__":
    main()
