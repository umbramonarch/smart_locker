"""
File: test_migrate_db.py
Description: Migration test for scripts/migrate_db.py — the locker_slot index
             is rebuilt from UNIQUE to non-unique inside one transaction:
             duplicate slot INSERTs then work, tag uniqueness still refuses,
             a failure mid-rebuild leaves the UNIQUE index intact, and a
             second run is a no-op. Rows and other indexes survive.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_migrate_db.py -v
       Uses a temp SQLite file; monkeypatches migrate_db.DB_PATH.
"""
import sqlite3

import pytest


def _legacy_db(path):
    """Create the pre-shared-slots schema: unique slot index, two rows."""
    con = sqlite3.connect(path)
    con.execute("""
        CREATE TABLE devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pm_number VARCHAR(50) NOT NULL UNIQUE,
            name VARCHAR(100) NOT NULL,
            device_type VARCHAR(50) NOT NULL,
            locker_slot INTEGER,
            tag_hmac VARCHAR(64)
        )
    """)
    con.execute("CREATE UNIQUE INDEX ix_devices_tag_hmac ON devices (tag_hmac)")
    con.execute("CREATE UNIQUE INDEX ix_devices_locker_slot ON devices (locker_slot)")
    con.execute(
        "INSERT INTO devices (pm_number, name, device_type, locker_slot) "
        "VALUES ('PM-1', 'Scope', 'Tool', 3)"
    )
    con.execute(
        "INSERT INTO devices (pm_number, name, device_type, locker_slot) "
        "VALUES ('PM-2', 'Drill', 'Tool', 5)"
    )
    con.commit()
    con.close()


def _slot_index_flag(path):
    """(exists, unique) for ix_devices_locker_slot."""
    con = sqlite3.connect(path)
    rows = con.execute("PRAGMA index_list(devices)").fetchall()
    con.close()
    row = next((r for r in rows if r[1] == "ix_devices_locker_slot"), None)
    return (row is not None, bool(row[2]) if row else None)


def _index_names(path):
    con = sqlite3.connect(path)
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()
    con.close()
    return {r[0] for r in rows}


def _row_count(path):
    con = sqlite3.connect(path)
    n = con.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
    con.close()
    return n


def test_locker_slot_index_rebuilt_nonunique(tmp_path, monkeypatch, capsys):
    """Old unique index -> non-unique; idempotent; rows/other indexes kept."""
    from scripts import migrate_db

    db = tmp_path / "old.db"
    _legacy_db(db)
    monkeypatch.setattr(migrate_db, "DB_PATH", str(db))

    migrate_db.migrate()
    assert _slot_index_flag(db) == (True, False)
    # tag_hmac keeps its unique index.
    assert "ix_devices_tag_hmac" in _index_names(db)

    con = sqlite3.connect(db)
    # Duplicate slots now insert freely; duplicate tag_hmac still refuses.
    con.execute(
        "INSERT INTO devices (pm_number, name, device_type, locker_slot) "
        "VALUES ('PM-3', 'Meter', 'Tool', 3)"
    )
    con.commit()
    rows = con.execute(
        "SELECT pm_number FROM devices WHERE locker_slot = 3 ORDER BY pm_number"
    ).fetchall()
    assert [r[0] for r in rows] == ["PM-1", "PM-3"]
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO devices (pm_number, name, device_type, tag_hmac) "
            "VALUES ('PM-4', 'Dupe', 'Tool', 'same-hmac')"
        )
        con.execute(
            "INSERT INTO devices (pm_number, name, device_type, tag_hmac) "
            "VALUES ('PM-5', 'Dupe2', 'Tool', 'same-hmac')"
        )
    con.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO devices (pm_number, name, device_type) "
            "VALUES ('PM-1', 'DupePm', 'Tool')"
        )
    con.rollback()
    con.close()

    # Second run is a no-op: still non-unique, all rows intact.
    migrate_db.migrate()
    out = capsys.readouterr().out
    assert "SKIP  ix_devices_locker_slot (already non-unique)" in out
    assert _slot_index_flag(db) == (True, False)
    assert _row_count(db) == 3


def test_failed_rebuild_keeps_unique_index(tmp_path, monkeypatch):
    """If CREATE INDEX fails after DROP, the UNIQUE index must survive.

    The rebuild runs inside one explicit transaction; forcing the CREATE to
    raise must roll the DROP back — the legacy constraint is never lost
    without its replacement.
    """
    from scripts import migrate_db

    db = tmp_path / "old.db"
    _legacy_db(db)
    monkeypatch.setattr(migrate_db, "DB_PATH", str(db))

    real_connect = sqlite3.connect

    class FailingCursor:
        """Cursor wrapper that fails only the non-unique CREATE INDEX."""

        def __init__(self, cur):
            self._cur = cur

        def execute(self, sql, *a, **k):
            if sql.strip() == (
                "CREATE INDEX ix_devices_locker_slot ON devices (locker_slot)"
            ):
                raise sqlite3.OperationalError("injected disk failure")
            return self._cur.execute(sql, *a, **k)

        def __getattr__(self, name):
            return getattr(self._cur, name)

    class FlakyConnection(sqlite3.Connection):
        def cursor(self, *a, **k):
            return FailingCursor(super().cursor(*a, **k))

    def flaky_connect(*a, **k):
        k["factory"] = FlakyConnection
        return real_connect(*a, **k)

    monkeypatch.setattr(migrate_db.sqlite3, "connect", flaky_connect)

    with pytest.raises(sqlite3.OperationalError, match="injected disk failure"):
        migrate_db.migrate()

    # The UNIQUE index survived — the rebuild's transaction rolled back.
    assert _slot_index_flag(db) == (True, True)
    assert _row_count(db) == 2
    con = sqlite3.connect(db)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO devices (pm_number, name, device_type, locker_slot) "
            "VALUES ('PM-9', 'Blocked', 'Tool', 3)"
        )
    con.rollback()
    con.close()


def test_missing_index_is_created_nonunique(tmp_path, monkeypatch):
    """A DB with no slot index gets the non-unique one."""
    from scripts import migrate_db

    db = tmp_path / "bare.db"
    con = sqlite3.connect(db)
    con.execute("""
        CREATE TABLE devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pm_number VARCHAR(50) NOT NULL UNIQUE,
            name VARCHAR(100) NOT NULL,
            device_type VARCHAR(50) NOT NULL,
            locker_slot INTEGER
        )
    """)
    con.commit()
    con.close()
    monkeypatch.setattr(migrate_db, "DB_PATH", str(db))

    migrate_db.migrate()
    assert _slot_index_flag(db) == (True, False)


def test_already_nonunique_index_is_untouched(tmp_path, monkeypatch, capsys):
    """A non-unique slot index skips the rebuild entirely."""
    from scripts import migrate_db

    db = tmp_path / "new.db"
    con = sqlite3.connect(db)
    con.execute("""
        CREATE TABLE devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pm_number VARCHAR(50) NOT NULL UNIQUE,
            name VARCHAR(100) NOT NULL,
            device_type VARCHAR(50) NOT NULL,
            locker_slot INTEGER
        )
    """)
    con.execute(
        "CREATE INDEX ix_devices_locker_slot ON devices (locker_slot)"
    )
    con.commit()
    con.close()
    monkeypatch.setattr(migrate_db, "DB_PATH", str(db))

    migrate_db.migrate()
    assert "SKIP  ix_devices_locker_slot" in capsys.readouterr().out
    assert _slot_index_flag(db) == (True, False)
