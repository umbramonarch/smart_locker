"""
File: sync_source.py
Description: Run one catalog-mirror tick by hand — adopt the sheet on first
             sight, detect hand edits, and flush pending writes. The SQLite
             catalog is the source of truth; this just forces the cycle the
             background scheduler already runs.
Project: smart_locker/scripts
Notes: Usage: python -m scripts.sync_source [--diffs]
       The mirror path comes from SMART_LOCKER_MIRROR_PATH, the legacy
       SMART_LOCKER_SOURCE_EXCEL_PATH, or smart_locker_catalog.xlsx next to
       the database.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.logging_config import setup_logging
from smart_locker.database.engine import get_engine, init_db


def _fmt_side(value) -> str:
    """Render one side of a diff for the console.

    ``changed`` diffs carry a single cell string; ``added``/``removed``
    diffs carry the whole canonical row as a list — join those cells into
    one readable ``a | b | c`` line instead of a Python list repr.
    """
    if value is None:
        return "-"
    if isinstance(value, (list, tuple)):
        cells = ["" if c is None else str(c) for c in value]
        return " | ".join(cells) if cells else "-"
    return str(value)


def main() -> None:
    """Parse CLI arguments and run a mirror tick (or list sheet diffs).

    Returns:
        None. The tick summary is printed to stdout.
    """
    parser = argparse.ArgumentParser(description="Sync the catalog mirror workbook.")
    parser.add_argument(
        "--diffs",
        action="store_true",
        help="List hand edits found in the sheet instead of syncing",
    )
    args = parser.parse_args()

    setup_logging()
    init_db()

    from smart_locker.sync import mirror

    if args.diffs:
        diffs, err = mirror.external_diffs()
        if err:
            print(f"Mirror unavailable: {err}")
            return
        if not diffs:
            print("No hand edits pending.")
            return
        for diff in diffs:
            print(f"  {diff['kind']}: {diff['pm_number']} "
                  f"{diff.get('field') or ''} "
                  f"sheet={_fmt_side(diff.get('sheet'))} "
                  f"db={_fmt_side(diff.get('database'))}")
        return

    result = mirror.tick(get_engine(), trigger="manual")
    print(f"Mirror tick: {result}")


if __name__ == "__main__":
    main()
