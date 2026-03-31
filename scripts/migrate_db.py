"""
File: migrate_db.py
Description: Database migration script. Adds columns and tables introduced after
             the initial schema. Handles column additions (locker_slot,
             description, image_path, pm_number, manufacturer, model, barcode,
             calibration_due) and table creation (registrants for self-service
             registration name list). Safe to run multiple times — skips
             columns and tables that already exist.
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
    ]

    for table, column, col_type in migrations:
        if _column_exists(cur, table, column):
            print(f"  SKIP  {table}.{column} (already exists)")
        else:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            print(f"  ADD   {table}.{column} {col_type}")

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
