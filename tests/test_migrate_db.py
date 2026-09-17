"""
File: test_migrate_db.py
Description: Tests for the database migration script — the locker-slot index
             migrates from UNIQUE to plain (shared slots) without data loss.
             Safe to rerun on an already-migrated database.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_migrate_db.py -v
       Uses a temporary file database; no NFC hardware.
"""

import sqlite3
from pathlib import Path

import scripts.migrate_db as migrate_db


def _old_style_db(path) -> None:
    """Create a pre-Unit-6 Pi database: UNIQUE slot index plus one row."""
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE devices ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "name VARCHAR(100), "
        "locker_slot INTEGER)"
    )
    con.execute(
        "CREATE UNIQUE INDEX ix_devices_locker_slot ON devices (locker_slot)"
    )
    con.execute("INSERT INTO devices (name, locker_slot) VALUES ('Meter', 3)")
    con.commit()
    con.close()


def _index_sql(path, index_name: str) -> str | None:
    con = sqlite3.connect(path)
    try:
        row = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            (index_name,),
        ).fetchone()
        return row[0] if row else None
    finally:
        con.close()


class TestLockerSlotIndexMigration:
    """The old UNIQUE slot index becomes plain; rows survive the migration."""

    def test_unique_index_migrates_to_plain_without_data_loss(
        self, tmp_path, monkeypatch
    ):
        """Old Pi DB: UNIQUE dropped, plain index created, rows untouched."""
        db_path = tmp_path / "migrate.db"
        _old_style_db(db_path)
        monkeypatch.setattr(migrate_db, "DB_PATH", str(db_path))

        migrate_db.migrate()

        sql = _index_sql(db_path, "ix_devices_locker_slot")
        assert sql is not None
        assert "UNIQUE" not in sql.upper()
        con = sqlite3.connect(db_path)
        try:
            rows = con.execute(
                "SELECT name, locker_slot FROM devices"
            ).fetchall()
            assert rows == [("Meter", 3)]
            con.execute(
                "INSERT INTO devices (name, locker_slot) VALUES ('Scope', 3)"
            )
            con.commit()
            assert con.execute(
                "SELECT COUNT(*) FROM devices WHERE locker_slot = 3"
            ).fetchone()[0] == 2
        finally:
            con.close()

    def test_migration_is_idempotent(self, tmp_path, monkeypatch):
        """Rerunning migrate keeps the plain index and the rows."""
        db_path = tmp_path / "migrate.db"
        _old_style_db(db_path)
        monkeypatch.setattr(migrate_db, "DB_PATH", str(db_path))

        migrate_db.migrate()
        migrate_db.migrate()

        sql = _index_sql(db_path, "ix_devices_locker_slot")
        assert sql is not None
        assert "UNIQUE" not in sql.upper()
        con = sqlite3.connect(db_path)
        try:
            assert con.execute(
                "SELECT name, locker_slot FROM devices"
            ).fetchall() == [("Meter", 3)]
        finally:
            con.close()


class TestQuickstartDocumentsMigration:
    """The dev quickstart tells upgraders about the shared-slot migration."""

    def test_guide_quickstart_mentions_migrate_db(self):
        """GUIDE §17 includes the migrate_db step for pre-shared-slot DBs."""
        guide = (
            Path(__file__).resolve().parents[1] / "GUIDE.md"
        ).read_text(encoding="utf-8")
        assert "python -m scripts.migrate_db" in guide
        assert "shared" in guide.split("## 17.", 1)[1].lower()
