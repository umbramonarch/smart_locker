"""
File: test_device_registration.py
Description: Tests for admin Register Device: look up a PM in the Excel
             catalog, assign a locker slot label, and insert one SQLite
             row. Sync must never create locker devices.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_device_registration.py -v
       Uses a temporary workbook; no NFC hardware.
"""

import tempfile
from datetime import date
from pathlib import Path

import pytest
from openpyxl import Workbook

from smart_locker.database.models import DeviceStatus
from smart_locker.database.repositories import DeviceRepository
from smart_locker.services.device_registration import (
    AlreadyRegistered,
    CatalogUnavailable,
    InvalidSlot,
    UnknownPm,
    register_locker_device,
    set_locker_slot,
    unregistered_locker_rows,
)
from smart_locker.sync.source_import import (
    CatalogReadError,
    lookup_catalog_by_pm,
    pm_match_key,
)


def _create_excel(rows: list[list]) -> Path:
    """Write a temporary workbook (first row = headers)."""
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    path = Path(tempfile.mktemp(suffix=".xlsx"))
    wb.save(path)
    return path


_HEADERS = [
    "Equipment", "Manufacturer", "Model", "Serial Number",
    "Category", "Calibration Due",
]


class TestLookupCatalogByPm:
    """Excel catalog lookup used by Register Device."""

    def test_known_pm_returns_catalog_fields(self):
        """A PM in the sheet yields name, type, manufacturer, model, serial, cal."""
        path = _create_excel([
            _HEADERS,
            ["PM-001", "Fluke", "87V", "SN-123", "Multimeter", "15.03.2026"],
        ])
        try:
            row = lookup_catalog_by_pm(path, "PM-001")
            assert row is not None
            assert row.pm_number == "PM-001"
            assert row.manufacturer == "Fluke"
            assert row.model == "87V"
            assert row.serial_number == "SN-123"
            assert row.device_type == "Multimeter"
            assert row.calibration_due == date(2026, 3, 15)
            assert "Fluke" in row.name and "87V" in row.name
        finally:
            path.unlink(missing_ok=True)

    def test_unknown_pm_returns_none(self):
        """A PM that is not in the sheet is not a catalog hit."""
        path = _create_excel([
            ["Equipment", "Manufacturer"],
            ["PM-001", "Fluke"],
        ])
        try:
            assert lookup_catalog_by_pm(path, "PM-999") is None
        finally:
            path.unlink(missing_ok=True)

    def test_missing_file_raises(self):
        """Share down / missing workbook must not invent a catalog row."""
        with pytest.raises(CatalogReadError):
            lookup_catalog_by_pm("/nonexistent/device-list.xlsx", "PM-001")


class TestRegisterLockerDevice:
    """PM + slot creates one locker row from Excel; failures leave the DB empty."""

    def test_known_pm_and_free_slot_creates_from_excel(self, db_session):
        """Register Device copies catalog fields and stores the chosen slot."""
        path = _create_excel([
            _HEADERS,
            ["PM-001", "Fluke", "87V", "SN-123", "Multimeter", "15.03.2026"],
        ])
        try:
            device = register_locker_device(db_session, path, "PM-001", locker_slot=3)
            db_session.commit()

            found = DeviceRepository.find_by_pm(db_session, "PM-001")
            assert found is not None
            assert found.id == device.id
            assert found.locker_slot == 3
            assert found.manufacturer == "Fluke"
            assert found.model == "87V"
            assert found.serial_number == "SN-123"
            assert found.device_type == "Multimeter"
            assert found.calibration_due == date(2026, 3, 15)
            assert found.status == DeviceStatus.AVAILABLE
            assert found.current_borrower_id is None
            assert found.tag_hmac is None
        finally:
            path.unlink(missing_ok=True)

    def test_register_copies_photo_from_same_model_sibling(self, db_session):
        """A new unit of a model that already has a photo reuses that image_path."""
        DeviceRepository.create(
            db_session,
            name="Fluke 87V",
            device_type="Multimeter",
            pm_number="PM-OLD",
            model="87V",
            locker_slot=1,
            image_path="images/87V.jpg",
        )
        db_session.commit()
        path = _create_excel([
            ["Equipment", "Manufacturer", "Model"],
            ["PM-NEW", "Fluke", "87V"],
        ])
        try:
            device = register_locker_device(db_session, path, "PM-NEW", locker_slot=2)
            assert device.image_path == "images/87V.jpg"
        finally:
            path.unlink(missing_ok=True)

    def test_unknown_pm_fails_with_no_ghost_row(self, db_session):
        """Unknown PM errors; SQLite must not gain a placeholder device."""
        path = _create_excel([
            ["Equipment", "Manufacturer"],
            ["PM-001", "Fluke"],
        ])
        try:
            with pytest.raises(UnknownPm):
                register_locker_device(db_session, path, "PM-999", locker_slot=1)
            db_session.rollback()
            assert DeviceRepository.find_by_pm(db_session, "PM-999") is None
        finally:
            path.unlink(missing_ok=True)

    def test_missing_workbook_fails_with_no_ghost_row(self, db_session):
        """Share down must not create a locker row."""
        with pytest.raises(CatalogUnavailable):
            register_locker_device(
                db_session, "/nonexistent/device-list.xlsx", "PM-001", locker_slot=1
            )
        db_session.rollback()
        assert DeviceRepository.find_by_pm(db_session, "PM-001") is None

    def test_shared_slot_accepts_second_pm(self, db_session):
        """A slot already used by a locker device accepts another PM."""
        DeviceRepository.create(
            db_session, name="Existing", device_type="general",
            pm_number="PM-OLD", locker_slot=2,
        )
        db_session.commit()
        path = _create_excel([
            ["Equipment", "Manufacturer", "Model"],
            ["PM-NEW", "Keysight", "34465A"],
        ])
        try:
            device = register_locker_device(db_session, path, "PM-NEW", locker_slot=2)
            db_session.commit()
            assert device.locker_slot == 2
            assert len(DeviceRepository.find_by_slot(db_session, 2)) == 2
        finally:
            path.unlink(missing_ok=True)

    def test_already_registered_pm_fails(self, db_session):
        """A PM already in the locker cannot be registered again."""
        DeviceRepository.create(
            db_session, name="Fluke 87V", device_type="general",
            pm_number="PM-001", locker_slot=1,
        )
        db_session.commit()
        path = _create_excel([
            ["Equipment", "Manufacturer"],
            ["PM-001", "Fluke"],
        ])
        try:
            with pytest.raises(AlreadyRegistered):
                register_locker_device(db_session, path, "PM-001", locker_slot=4)
        finally:
            path.unlink(missing_ok=True)

    def test_pm_unique_race_maps_to_already_registered(self, db_session, monkeypatch):
        """A PM-unique IntegrityError (same-PM race) is AlreadyRegistered."""
        from sqlalchemy.exc import IntegrityError

        def _race(*_args, **_kwargs):
            raise IntegrityError(
                "INSERT INTO devices", {},
                Exception("UNIQUE constraint failed: devices.pm_number"),
            )

        monkeypatch.setattr(DeviceRepository, "create", _race)
        path = _create_excel([
            ["Equipment", "Manufacturer"],
            ["PM-001", "Fluke"],
        ])
        try:
            with pytest.raises(AlreadyRegistered, match="already in the locker"):
                register_locker_device(db_session, path, "PM-001", locker_slot=1)
        finally:
            path.unlink(missing_ok=True)

    def test_non_pm_integrity_error_propagates(self, db_session, monkeypatch):
        """A non-PM IntegrityError is unexpected — it propagates, unmapped."""
        from sqlalchemy.exc import IntegrityError

        def _race(*_args, **_kwargs):
            raise IntegrityError(
                "INSERT INTO devices", {},
                Exception("UNIQUE constraint failed: devices.tag_hmac"),
            )

        monkeypatch.setattr(DeviceRepository, "create", _race)
        path = _create_excel([
            ["Equipment", "Manufacturer"],
            ["PM-001", "Fluke"],
        ])
        try:
            with pytest.raises(IntegrityError):
                register_locker_device(db_session, path, "PM-001", locker_slot=1)
        finally:
            path.unlink(missing_ok=True)

    def test_invalid_slot_fails(self, db_session):
        """Slot numbers must be >= 1."""
        path = _create_excel([
            ["Equipment", "Manufacturer"],
            ["PM-001", "Fluke"],
        ])
        try:
            with pytest.raises(InvalidSlot):
                register_locker_device(db_session, path, "PM-001", locker_slot=0)
            assert DeviceRepository.find_by_pm(db_session, "PM-001") is None
        finally:
            path.unlink(missing_ok=True)

    def test_slot_over_cap_is_invalid(self, db_session):
        """locker_slot=999999 is InvalidSlot (I22)."""
        path = _create_excel([
            ["Equipment", "Manufacturer"],
            ["PM-001", "Fluke"],
        ])
        try:
            with pytest.raises(InvalidSlot):
                register_locker_device(db_session, path, "PM-001", locker_slot=999999)
        finally:
            path.unlink(missing_ok=True)

    def test_location_does_not_mark_new_row_borrowed(self, db_session):
        """Registering into the locker always starts AVAILABLE, even if Excel names a person."""
        path = _create_excel([
            ["Equipment", "Manufacturer", "Model", "Location"],
            ["PM-001", "Fluke", "87V", "Bob"],
        ])
        try:
            device = register_locker_device(db_session, path, "PM-001", locker_slot=1)
            assert device.status == DeviceStatus.AVAILABLE
            assert device.current_borrower_id is None
        finally:
            path.unlink(missing_ok=True)

    def test_duplicate_pm_prefers_locker_row(self, db_session):
        """With duplicate PM rows, registration copies the Locker row the picker shows."""
        path = _create_excel([
            ["Equipment", "Name", "Location", "Calibration Due"],
            ["PM-001", "Other Meter", "Jack B.", "15.03.2030"],
            ["PM-001", "Locker Meter", "Locker", "15.03.2020"],
        ])
        try:
            shown = unregistered_locker_rows(db_session, path)
            assert [r.pm_number for r in shown] == ["PM-001"]
            assert shown[0].name == "Locker Meter"
            device = register_locker_device(db_session, path, "PM-001", locker_slot=1)
            assert device.name == "Locker Meter"
            assert device.calibration_due == date(2020, 3, 15)
        finally:
            path.unlink(missing_ok=True)

    def test_non_locker_pm_still_registers_from_only_row(self, db_session):
        """A PM with no Locker row falls back to its only sheet row."""
        path = _create_excel([
            ["Equipment", "Name", "Location", "Calibration Due"],
            ["PM-001", "Field Meter", "Jack B.", "15.03.2030"],
        ])
        try:
            device = register_locker_device(db_session, path, "PM-001", locker_slot=1)
            assert device.name == "Field Meter"
            assert device.calibration_due == date(2030, 3, 15)
        finally:
            path.unlink(missing_ok=True)

    def test_duplicate_locker_rows_register_first_row(self, db_session, caplog):
        """Two Locker rows sharing a PM register sheet-order first row's metadata."""
        import logging

        path = _create_excel([
            ["Equipment", "Name", "Location", "Calibration Due"],
            ["PM-001", "First Meter", "Locker", "15.03.2020"],
            ["PM-001", "Second Meter", "Locker", "15.03.2030"],
        ])
        try:
            with caplog.at_level(logging.WARNING):
                device = register_locker_device(db_session, path, "PM-001", locker_slot=1)
            assert device.name == "First Meter"
            assert device.calibration_due == date(2020, 3, 15)
            assert any("keeping first row" in r.message for r in caplog.records)
        finally:
            path.unlink(missing_ok=True)


class TestSetLockerSlot:
    """Admin can reassign a slot; moving onto an occupied one shares it."""

    def test_reassign_to_free_slot(self, db_session):
        """Change slot on an existing locker row."""
        device = DeviceRepository.create(
            db_session, name="Fluke 87V", device_type="general",
            pm_number="PM-001", locker_slot=1,
        )
        db_session.commit()
        set_locker_slot(db_session, device, 5)
        db_session.commit()
        db_session.expire_all()
        assert DeviceRepository.find_by_pm(db_session, "PM-001").locker_slot == 5

    def test_move_to_occupied_slot_shares_it(self, db_session):
        """Slots are shared — moving onto another device's slot is allowed."""
        a = DeviceRepository.create(
            db_session, name="A", device_type="general",
            pm_number="PM-A", locker_slot=1,
        )
        DeviceRepository.create(
            db_session, name="B", device_type="general",
            pm_number="PM-B", locker_slot=2,
        )
        db_session.commit()
        set_locker_slot(db_session, a, 2)
        db_session.commit()
        db_session.expire_all()
        assert DeviceRepository.find_by_pm(db_session, "PM-A").locker_slot == 2
        assert len(DeviceRepository.find_by_slot(db_session, 2)) == 2

    def test_move_to_invalid_slot_fails(self, db_session):
        """Out-of-range moves are still InvalidSlot."""
        device = DeviceRepository.create(
            db_session, name="A", device_type="general",
            pm_number="PM-A", locker_slot=1,
        )
        db_session.commit()
        with pytest.raises(InvalidSlot):
            set_locker_slot(db_session, device, 0)
        db_session.rollback()
        with pytest.raises(InvalidSlot):
            set_locker_slot(db_session, device, 999999)
        db_session.rollback()
        assert DeviceRepository.find_by_pm(db_session, "PM-A").locker_slot == 1


class TestUnregisteredLockerRows:
    """Register Device pick list: in-locker Excel rows minus registered PMs."""

    def test_excludes_registered_pm_normalised(self, db_session):
        """Excel 1001.0 matches SQLite PM 1001 via pm_match_key."""
        DeviceRepository.create(
            db_session, name="Existing", device_type="general",
            pm_number="1001", locker_slot=1,
        )
        db_session.commit()
        path = _create_excel([
            ["Equipment", "Name", "Location"],
            [1001.0, "Zeta Scope", "Locker"],
            ["PM-002", "Beta Meter", "Locker"],
            ["PM-003", "Alpha Probe", "Jack B."],
        ])
        try:
            rows = unregistered_locker_rows(db_session, path)
            assert [r.pm_number for r in rows] == ["PM-002"]
        finally:
            path.unlink(missing_ok=True)

    def test_sorted_by_name(self, db_session):
        """Rows come back sorted by name (then PM)."""
        path = _create_excel([
            ["Equipment", "Name", "Location"],
            ["PM-002", "Zeta Scope", "Locker"],
            ["PM-001", "Alpha Meter", "Locker"],
        ])
        try:
            rows = unregistered_locker_rows(db_session, path)
            assert [r.pm_number for r in rows] == ["PM-001", "PM-002"]
        finally:
            path.unlink(missing_ok=True)

    def test_missing_workbook_is_unavailable(self, db_session):
        """Share down surfaces as CatalogUnavailable."""
        with pytest.raises(CatalogUnavailable):
            unregistered_locker_rows(db_session, "/nonexistent/device-list.xlsx")

    def test_same_name_tie_break_orders_by_pm(self, db_session):
        """Equal names (case-insensitive) break ties by PM string."""
        path = _create_excel([
            ["Equipment", "Name", "Location"],
            ["PM-002", "Scope", "Locker"],
            ["PM-001", "scope", "Locker"],
        ])
        try:
            rows = unregistered_locker_rows(db_session, path)
            assert [r.pm_number for r in rows] == ["PM-001", "PM-002"]
        finally:
            path.unlink(missing_ok=True)

    def test_all_excluded_returns_empty(self, db_session):
        """Every in-locker PM registered means an empty pick list."""
        DeviceRepository.create(
            db_session, name="Scope", device_type="general",
            pm_number="PM-001", locker_slot=1,
        )
        DeviceRepository.create(
            db_session, name="Meter", device_type="general",
            pm_number="PM-002", locker_slot=2,
        )
        db_session.commit()
        path = _create_excel([
            ["Equipment", "Name", "Location"],
            ["PM-001", "Scope", "Locker"],
            ["PM-002", "Meter", "Locker"],
        ])
        try:
            assert unregistered_locker_rows(db_session, path) == []
        finally:
            path.unlink(missing_ok=True)


class TestSharedPmEquality:
    """Register and the pick list share pm_match_key equality."""

    def test_find_by_pm_and_pick_list_agree_on_case_and_whitespace(self, db_session):
        """A case/whitespace PM variant matches both the register pre-check and the pick list."""
        DeviceRepository.create(
            db_session, name="Scope", device_type="general",
            pm_number="PM-001", locker_slot=1,
        )
        db_session.commit()
        assert DeviceRepository.find_by_pm(db_session, "  pm-001 ") is not None
        path = _create_excel([
            ["Equipment", "Name", "Location"],
            ["  pm-001 ", "Scope", "Locker"],
            ["PM-002", "Meter", "Locker"],
        ])
        try:
            rows = unregistered_locker_rows(db_session, path)
            assert [r.pm_number for r in rows] == ["PM-002"]
        finally:
            path.unlink(missing_ok=True)

    def test_unicode_casefold_pair_matches_both_paths(self, db_session):
        """ß/SS agree via casefold on both paths (SQLite lower() alone would miss)."""
        assert pm_match_key("PM-ß") == pm_match_key("pm-ss")
        DeviceRepository.create(
            db_session, name="Scope", device_type="general",
            pm_number="PM-ß", locker_slot=1,
        )
        db_session.commit()
        assert DeviceRepository.find_by_pm(db_session, "pm-ss") is not None
        path = _create_excel([
            ["Equipment", "Name", "Location"],
            ["pm-ss", "Scope", "Locker"],
            ["PM-002", "Meter", "Locker"],
        ])
        try:
            rows = unregistered_locker_rows(db_session, path)
            assert [r.pm_number for r in rows] == ["PM-002"]
        finally:
            path.unlink(missing_ok=True)
