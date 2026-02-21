"""Database migration script.

Adds columns introduced after the initial schema:
  - devices.locker_slot   (INTEGER, nullable)
  - devices.description   (TEXT, nullable)
  - devices.image_path    (VARCHAR(255), nullable)

Safe to run multiple times — skips columns that already exist.

Usage:
    python -m scripts.migrate_db
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import DB_PATH


def _column_exists(cursor: sqlite3.Cursor, table: str, column: str) -> bool:
    cursor.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cursor.fetchall())


def migrate() -> None:
    print(f"Migrating database: {DB_PATH}")
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    migrations = [
        ("devices", "locker_slot", "INTEGER"),
        ("devices", "description", "TEXT"),
        ("devices", "image_path", "VARCHAR(255)"),
    ]

    for table, column, col_type in migrations:
        if _column_exists(cur, table, column):
            print(f"  SKIP  {table}.{column} (already exists)")
        else:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            print(f"  ADD   {table}.{column} {col_type}")

    con.commit()
    con.close()
    print("Migration complete.")


if __name__ == "__main__":
    migrate()
