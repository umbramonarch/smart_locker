"""
File: test_migrate_db.py
Description: scripts/migrate_db.py slot-index migration — a database that
             predates shared slots carries ``ix_devices_locker_slot`` as a
             UNIQUE index; migrate() must drop and recreate it as a plain
             index, idempotently, on a real temp-file SQLite database.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_migrate_db.py -v
"""

import sqlite3

import pytest

import scripts.migrate_db as migrate_db


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    """Point migrate_db at a temp-file SQLite database."""
    path = tmp_path / "mig.db"
    monkeypatch.setattr(migrate_db, "DB_PATH", str(path))
    return path


def _bootstrap_devices(path, unique_slot_index: bool) -> None:
    """Create the devices table with the pre-migration index shape."""
    con = sqlite3.connect(path)
    cur = con.cursor()
    cur.execute(
        "CREATE TABLE devices ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  name VARCHAR(200) NOT NULL,"
        "  device_type VARCHAR(100) NOT NULL DEFAULT 'general',"
        "  serial_number VARCHAR(100),"
        "  locker_slot INTEGER)"
    )
    kind = "UNIQUE INDEX" if unique_slot_index else "INDEX"
    cur.execute(
        f"CREATE {kind} ix_devices_locker_slot ON devices (locker_slot)"
    )
    con.commit()
    con.close()


def _slot_index_unique(path) -> bool | None:
    """Return the slot index's UNIQUE flag, or None when the index is absent."""
    con = sqlite3.connect(path)
    cur = con.cursor()
    cur.execute(
        'SELECT "unique" FROM pragma_index_list(\'devices\') '
        "WHERE name='ix_devices_locker_slot'"
    )
    row = cur.fetchone()
    con.close()
    return None if row is None else bool(row[0])


def test_migrate_drops_unique_slot_index(db_path):
    """A UNIQUE slot index is downgraded so two units may share a number."""
    _bootstrap_devices(db_path, unique_slot_index=True)
    con = sqlite3.connect(db_path)
    con.execute("INSERT INTO devices (name, locker_slot) VALUES ('a', 1)")
    con.execute("INSERT INTO devices (name, locker_slot) VALUES ('b', 2)")
    con.commit()
    con.close()

    migrate_db.migrate()

    assert _slot_index_unique(db_path) is False
    con = sqlite3.connect(db_path)
    con.execute("INSERT INTO devices (name, locker_slot) VALUES ('c', 1)")
    con.commit()
    con.close()


def test_migrate_keeps_plain_slot_index(db_path):
    """An already-migrated (non-unique) index is left alone."""
    _bootstrap_devices(db_path, unique_slot_index=False)
    migrate_db.migrate()
    assert _slot_index_unique(db_path) is False


def test_migrate_creates_slot_index_when_missing(db_path):
    """A database without the index gets the non-unique version."""
    _bootstrap_devices(db_path, unique_slot_index=False)
    con = sqlite3.connect(db_path)
    con.execute("DROP INDEX ix_devices_locker_slot")
    con.commit()
    con.close()

    migrate_db.migrate()
    assert _slot_index_unique(db_path) is False


def test_migrate_slot_index_idempotent(db_path):
    """A second migrate run is a no-op on the slot index."""
    _bootstrap_devices(db_path, unique_slot_index=True)
    migrate_db.migrate()
    migrate_db.migrate()
    assert _slot_index_unique(db_path) is False
