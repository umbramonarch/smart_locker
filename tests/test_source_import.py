"""
File: test_source_import.py
Description: Tests for the source Excel import module. Validates column
             auto-detection, schrank filtering, device insert/update logic,
             metadata-only updates, and date parsing from German Excel formats.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_source_import.py -v
"""

import tempfile
from pathlib import Path

import pytest
from openpyxl import Workbook

from smart_locker.database.repositories import DeviceRepository
from smart_locker.sync.source_import import (
    ImportResult,
    find_column,
    import_from_source_excel,
    parse_date,
)


def _create_test_excel(rows: list[list], sheet_name: str = "Sheet1") -> Path:
    """Create a temporary Excel file with the given rows (first row = headers)."""
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name
    for row in rows:
        ws.append(row)
    path = Path(tempfile.mktemp(suffix=".xlsx"))
    wb.save(path)
    return path


class TestFindColumn:
    """Tests for column auto-detection from Excel headers."""
    def test_exact_match(self):
        """Verify exact lowercase match finds the correct column index."""
        assert find_column(["Name", "PM", "Serial"], ["pm"]) == 1

    def test_case_insensitive(self):
        """Verify header matching is case-insensitive."""
        assert find_column(["NAME", "Equipment", "Serial"], ["equipment"]) == 1

    def test_not_found(self):
        """Verify None is returned when no candidate matches any header."""
        assert find_column(["Name", "Serial"], ["pm"]) is None

    def test_multiple_candidates(self):
        """Verify the first matching candidate from the list is returned."""
        assert find_column(["Hersteller", "Model"], ["manufacturer", "hersteller"]) == 0


class TestParseDate:
    """Tests for date parsing from German and ISO Excel formats."""
    def test_none(self):
        """Verify None input returns None."""
        assert parse_date(None) is None

    def test_german_format(self):
        """Verify DD.MM.YYYY German date format is parsed correctly."""
        d = parse_date("15.03.2025")
        assert d is not None
        assert d.year == 2025 and d.month == 3 and d.day == 15

    def test_iso_format(self):
        """Verify YYYY-MM-DD ISO date format is parsed correctly."""
        d = parse_date("2025-03-15")
        assert d is not None
        assert d.year == 2025

    def test_empty_string(self):
        """Verify empty string returns None."""
        assert parse_date("") is None


class TestImportFromSourceExcel:
    """Tests for the full source Excel import pipeline — insert, update, skip, dry-run."""
    def test_import_new_devices(self, db_session):
        """New schrank devices are inserted into the database."""
        path = _create_test_excel([
            ["Equipment", "Hersteller", "Typbezeichnung", "Platz Messmittelschrank"],
            ["PM-001", "Fluke", "87V", "Schrank 1"],
            ["PM-002", "Keysight", "34465A", "Schrank 2"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path)
            assert result.imported == 2
            assert result.errors == 0

            d1 = DeviceRepository.find_by_pm(db_session, "PM-001")
            assert d1 is not None
            assert d1.manufacturer == "Fluke"
            assert d1.locker_slot == 1

            d2 = DeviceRepository.find_by_pm(db_session, "PM-002")
            assert d2 is not None
            assert d2.locker_slot == 2
        finally:
            path.unlink(missing_ok=True)

    def test_skip_non_schrank(self, db_session):
        """Rows without 'schrank' in slot column are skipped."""
        path = _create_test_excel([
            ["Equipment", "Platz Messmittelschrank"],
            ["PM-001", "Schrank 1"],
            ["PM-002", "Labor 3"],
            ["PM-003", ""],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path)
            assert result.imported == 1
            assert result.non_locker_skipped == 2
        finally:
            path.unlink(missing_ok=True)

    def test_skip_duplicates_unchanged(self, db_session):
        """Existing devices with identical data are counted as unchanged."""
        DeviceRepository.create(
            db_session,
            name="PM-001 Fluke 87V",
            device_type="general",
            pm_number="PM-001",
            manufacturer="Fluke",
            model="87V",
        )
        db_session.commit()

        path = _create_test_excel([
            ["Equipment", "Hersteller", "Typbezeichnung", "Platz Messmittelschrank"],
            ["PM-001", "Fluke", "87V", "Schrank 1"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path)
            assert result.imported == 0
            assert result.unchanged == 1
        finally:
            path.unlink(missing_ok=True)

    def test_update_existing_metadata(self, db_session):
        """Existing devices get metadata updated when source data changes."""
        DeviceRepository.create(
            db_session,
            name="PM-001 Fluke 87V",
            device_type="general",
            pm_number="PM-001",
            manufacturer="Fluke",
            model="87V",
        )
        db_session.commit()

        path = _create_test_excel([
            ["Equipment", "Hersteller", "Typbezeichnung", "Platz Messmittelschrank"],
            ["PM-001", "Fluke", "87-V MAX", "Schrank 1"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path)
            assert result.updated == 1

            device = DeviceRepository.find_by_pm(db_session, "PM-001")
            assert device.model == "87-V MAX"
        finally:
            path.unlink(missing_ok=True)

    def test_file_not_found(self):
        """Nonexistent file returns error result without crashing."""
        result = import_from_source_excel(None, "/nonexistent/file.xlsx")
        assert result.errors == 1
        assert "not found" in result.error_details[0].lower()

    def test_dry_run_no_writes(self, db_session):
        """Dry run parses but does not write to the database."""
        path = _create_test_excel([
            ["Equipment", "Platz Messmittelschrank"],
            ["PM-001", "Schrank 1"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path, dry_run=True)
            assert result.imported == 0

            device = DeviceRepository.find_by_pm(db_session, "PM-001")
            assert device is None
        finally:
            path.unlink(missing_ok=True)

    def test_german_column_headers(self, db_session):
        """German column headers are auto-detected."""
        path = _create_test_excel([
            ["Equipment", "Hersteller", "Typbezeichnung", "Hersteller-Serialnummer",
             "Barcodenummer", "Platz Messmittelschrank", "Kategorie"],
            ["PM-001", "Rohde & Schwarz", "RTB2004", "SN-12345", "BC-001", "Schrank 1", "Oscilloscope"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path)
            assert result.imported == 1

            device = DeviceRepository.find_by_pm(db_session, "PM-001")
            assert device.manufacturer == "Rohde & Schwarz"
            assert device.serial_number == "SN-12345"
            assert device.barcode == "BC-001"
            assert device.device_type == "Oscilloscope"
        finally:
            path.unlink(missing_ok=True)
