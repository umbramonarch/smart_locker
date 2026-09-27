"""
File: migrate_db.py
Description: Database migration script. Adds columns and tables introduced after
             the initial schema. Handles column additions (locker_slot,
             description, image_path, pm_number, manufacturer, model, barcode,
             calibration_due, tag_hmac) and table creation (registrants for
             self-service registration name list). Safe to run multiple times
             — skips columns, indexes, and tables that already exist.
Project: smart_locker/scripts
Notes: Usage: python -m scripts.migrate_db
       Uses raw SQLite PRAGMA introspection, not SQLAlchemy Alembic.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import DB_PATH


def _column_exists(cursor: sqlite3.Cursor, table: str, column: str) -> bool:
    """Check whether a column already exists in a SQLite table.

    Uses ``PRAGMA table_info`` to inspect the table schema and avoids
    duplicate ALTER TABLE errors during migration.

    Args:
        cursor: An open SQLite cursor.
        table: Name of the table to inspect.
        column: Column name to look for.

    Returns:
        True if the column exists, False otherwise.
    """
    cursor.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cursor.fetchall())


def _table_exists(cursor: sqlite3.Cursor, table_name: str) -> bool:
    """Check whether a table already exists in the SQLite database.

    Queries the ``sqlite_master`` system table for a matching table name.

    Args:
        cursor: An open SQLite cursor.
        table_name: Name of the table to look for.

    Returns:
        True if the table exists, False otherwise.
    """
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    )
    return cursor.fetchone() is not None


def _index_exists(cursor: sqlite3.Cursor, index_name: str) -> bool:
    """Check whether a named index already exists.

    Args:
        cursor: An open SQLite cursor.
        index_name: Name of the index to look for.

    Returns:
        True if the index exists, False otherwise.
    """
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
        (index_name,),
    )
    return cursor.fetchone() is not None


def migrate() -> None:
    """Apply pending column migrations to the Smart Locker database.

    Iterates over a list of (table, column, type) tuples and adds each
    column via ALTER TABLE if it does not already exist. Safe to run
    multiple times — existing columns are skipped.

    Returns:
        None. Progress is printed to stdout.
    """
    print(f"Migrating database: {DB_PATH}")
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    # Each tuple is (table_name, column_name, SQLite_column_type)
    migrations = [
        ("devices", "locker_slot", "INTEGER"),
        ("devices", "description", "TEXT"),
        ("devices", "image_path", "VARCHAR(255)"),
        ("devices", "pm_number", "VARCHAR(50)"),
        ("devices", "manufacturer", "VARCHAR(100)"),
        ("devices", "model", "VARCHAR(100)"),
        ("devices", "barcode", "VARCHAR(100)"),
        ("devices", "calibration_due", "DATE"),
        ("devices", "tag_hmac", "VARCHAR(64)"),
        ("devices", "location", "VARCHAR(200)"),
    ]

    for table, column, col_type in migrations:
        if _column_exists(cur, table, column):
            print(f"  SKIP  {table}.{column} (already exists)")
        else:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            print(f"  ADD   {table}.{column} {col_type}")

    # Unique index: many unbound devices may share NULL tag_hmac.
    if _index_exists(cur, "ix_devices_tag_hmac"):
        print("  SKIP  ix_devices_tag_hmac (already exists)")
    else:
        cur.execute(
            "CREATE UNIQUE INDEX ix_devices_tag_hmac ON devices (tag_hmac)"
        )
        print("  CREATE UNIQUE INDEX ix_devices_tag_hmac")

    # Unique locker slots (SQLite UNIQUE still allows multiple NULLs).
    if _index_exists(cur, "ix_devices_locker_slot"):
        print("  SKIP  ix_devices_locker_slot (already exists)")
    else:
        cur.execute(
            "CREATE UNIQUE INDEX ix_devices_locker_slot ON devices (locker_slot)"
        )
        print("  CREATE UNIQUE INDEX ix_devices_locker_slot")

    # Unique PM/serial: ALTER TABLE-added columns cannot carry UNIQUE, so
    # databases that grew through migrations get the constraint as a
    # separate index. Pre-existing duplicates warn instead of crashing.
    for idx, col in (
        ("ix_devices_pm_number", "pm_number"),
        ("ix_devices_serial_number", "serial_number"),
    ):
        if _index_exists(cur, idx):
            print(f"  SKIP  {idx} (already exists)")
            continue
        try:
            cur.execute(f"CREATE UNIQUE INDEX {idx} ON devices ({col})")
            print(f"  CREATE UNIQUE INDEX {idx}")
        except sqlite3.IntegrityError:
            print(
                f"  WARN  {idx} NOT created — duplicate {col} values exist; "
                "dedupe manually"
            )

    # --- Table creation: registrants (self-service registration name list) ---
    if _table_exists(cur, "registrants"):
        print("  SKIP  registrants table (already exists)")
    else:
        cur.execute("""
            CREATE TABLE registrants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                display_name VARCHAR(100) NOT NULL UNIQUE,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        print("  CREATE registrants table")

    con.commit()
    con.close()
    print("Migration complete.")


if __name__ == "__main__":
    migrate()
