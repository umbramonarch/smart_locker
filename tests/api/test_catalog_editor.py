"""
File: test_catalog_editor.py
Description: Catalog-editor normalization and migration-index tests — values
             the dashboard types are stored in the canonical forms the mirror
             sheet round-trips (PM float tails, stripped text, "general" type,
             derived place no-ops), and databases that grew through migrations
             get unique pm/serial indexes.
Project: smart_locker/tests/api
Notes: Run with: python -m pytest tests/api/test_catalog_editor.py -v
"""

import sqlite3

import pytest

from smart_locker.database.repositories import DeviceRepository
from smart_locker.services.device_catalog import set_place

from tests.api.helpers import dashboard_admin_headers


@pytest.fixture()
def catalog_device(db_session):
    """One non-locker catalog row (locker_slot NULL, stored place)."""
    device = DeviceRepository.create(
        db_session,
        name="Van kit",
        device_type="Tool",
        pm_number="PM-999",
        serial_number="SN-9",
    )
    device.location = "Workshop"
    db_session.commit()
    return device


@pytest.fixture()
def locker_device(db_session):
    """One registered cabinet unit (slot assigned, AVAILABLE)."""
    device = DeviceRepository.create(
        db_session,
        name="Scope",
        device_type="Oscilloscope",
        pm_number="PM-001",
        serial_number="SN-ABC",
        locker_slot=1,
    )
    db_session.commit()
    return device


def _legacy_db(path, rows=()):
    """Write a pre-migration devices table: pm/serial columns, no uniques."""
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE devices ("
        "id INTEGER PRIMARY KEY, pm_number TEXT, serial_number TEXT)"
    )
    for pm in rows:
        con.execute("INSERT INTO devices (pm_number) VALUES (?)", (pm,))
    con.commit()
    con.close()


def _index_names(path):
    con = sqlite3.connect(str(path))
    names = {row[1] for row in con.execute("PRAGMA index_list(devices)")}
    con.close()
    return names


class TestCatalogEditorNormalization:
    """Dashboard-typed values land in the canonical forms the sheet reads."""

    def test_add_normalizes_excel_float_tail(
        self, client, db_session, dashboard_secret
    ):
        """A typed "1001.0" stores "1001" — the text the sheet reads back."""
        resp = client.post(
            "/api/dashboard/devices",
            json={"pm_number": "1001.0", "name": "Hand meter"},
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        assert resp.json()["device"]["pm_number"] == "1001"
        db_session.expire_all()
        row = DeviceRepository.find_by_pm(db_session, "1001")
        assert row is not None
        assert row.pm_number == "1001"

    def test_add_normalized_pm_still_dedupes(
        self, client, db_session, dashboard_secret
    ):
        """"1001.0" then "1001" is one catalog row, not two."""
        headers = dashboard_admin_headers(dashboard_secret)
        first = client.post(
            "/api/dashboard/devices",
            json={"pm_number": "1001.0", "name": "Hand meter"},
            headers=headers,
        )
        assert first.status_code == 200
        second = client.post(
            "/api/dashboard/devices",
            json={"pm_number": "1001", "name": "Same meter"},
            headers=headers,
        )
        assert second.status_code == 409
        db_session.expire_all()
        matches = [
            d for d in DeviceRepository.list_all(db_session)
            if d.pm_number == "1001"
        ]
        assert len(matches) == 1

    def test_edit_blank_device_type_stores_general(
        self, client, catalog_device, db_session, dashboard_secret
    ):
        """An empty Type cell reads back as "general" — store it that way."""
        resp = client.patch(
            "/api/dashboard/devices/PM-999",
            json={"device_type": ""},
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        assert resp.json()["device"]["device_type"] == "general"
        db_session.expire_all()
        assert catalog_device.device_type == "general"

    def test_edit_derived_location_on_locker_row_is_noop(
        self, client, locker_device, dashboard_secret
    ):
        """Resending the derived place on a cabinet unit is a no-op."""
        resp = client.patch(
            "/api/dashboard/devices/PM-001",
            json={"location": "Locker"},
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        assert resp.json()["changed"] is False
        assert resp.json()["device"]["location"] == "Locker"

    def test_edit_different_location_on_locker_row_is_409(
        self, client, locker_device, dashboard_secret
    ):
        """A different place on a cabinet unit is still refused."""
        resp = client.patch(
            "/api/dashboard/devices/PM-001",
            json={"location": "Field truck"},
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 409


class TestSetPlaceNoop:
    """set_place marks the mirror only when the stored place changes."""

    def test_same_value_marks_nothing(self, db_session):
        """A re-send of the stored place skips flush and the dirty flag."""
        device = DeviceRepository.create(
            db_session, name="Kit", device_type="Tool", pm_number="PM-1"
        )
        db_session.commit()

        stored = set_place(db_session, device, "Bench")
        assert stored == "Bench"
        assert db_session.info.get("mirror_dirty_pending") is True
        db_session.commit()  # after_commit pops the flag like production

        stored = set_place(db_session, device, "Bench")
        assert stored == "Bench"
        assert not db_session.info.get("mirror_dirty_pending")


class TestFindBySerial:
    """Serial lookup is exact first, then case-folded."""

    def test_serial_lookup_folds_case(self, db_session, locker_device):
        """Stored "SN-ABC" is found by a typed "sn-abc"."""
        assert locker_device.serial_number == "SN-ABC"
        found = DeviceRepository.find_by_serial(db_session, "sn-abc")
        assert found is not None
        assert found.id == locker_device.id
        exact = DeviceRepository.find_by_serial(db_session, "SN-ABC")
        assert exact is not None and exact.id == locker_device.id
        assert DeviceRepository.find_by_serial(db_session, "sn-other") is None


class TestMigrateUniqueIndexes:
    """Upgraded databases get pm/serial uniqueness as plain indexes."""

    def test_migrate_creates_unique_indexes_and_is_idempotent(
        self, tmp_path, monkeypatch
    ):
        """migrate() creates both indexes; a second run is a clean no-op."""
        import scripts.migrate_db as migrate_db

        db_file = tmp_path / "locker.db"
        _legacy_db(db_file)

        monkeypatch.setattr(migrate_db, "DB_PATH", str(db_file))
        migrate_db.migrate()
        migrate_db.migrate()

        names = _index_names(db_file)
        assert "ix_devices_pm_number" in names
        assert "ix_devices_serial_number" in names

    def test_migrate_survives_preexisting_duplicates(
        self, tmp_path, monkeypatch, capsys
    ):
        """Duplicate pm rows warn instead of crashing the migration."""
        import scripts.migrate_db as migrate_db

        db_file = tmp_path / "locker.db"
        _legacy_db(db_file, rows=("PM-1", "PM-1"))

        monkeypatch.setattr(migrate_db, "DB_PATH", str(db_file))
        migrate_db.migrate()

        names = _index_names(db_file)
        assert "ix_devices_pm_number" not in names
        assert "ix_devices_serial_number" in names
        assert "WARN" in capsys.readouterr().out
