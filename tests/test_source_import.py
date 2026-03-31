"""
File: test_source_import.py
Description: Tests for the source Excel import module. Validates column
             auto-detection, schrank filtering, device insert/update logic,
             metadata-only updates, date parsing from German Excel formats,
             and registrant name extraction from the "Aktueller Einsatzort"
             column for the self-service registration name list.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_source_import.py -v
"""

import tempfile
from pathlib import Path

import pytest
from openpyxl import Workbook

from smart_locker.database.models import DeviceStatus
from smart_locker.database.repositories import DeviceRepository, RegistrantRepository, UserRepository
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
            name="Fluke 87V",
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
            name="Fluke 87V",
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


class TestLocationColumn:
    """Tests for the 'Aktueller Einsatzort' column — device location and borrower detection."""

    def test_schrank_location_sets_available(self, db_session):
        """Device with 'Schrank' in location column is imported as AVAILABLE."""
        path = _create_test_excel([
            ["Equipment", "Hersteller", "Typbezeichnung",
             "Platz Messmittelschrank", "Aktueller Einsatzort"],
            ["PM-001", "Fluke", "87V", "Schrank 1", "Messmittelschrank"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path)
            assert result.imported == 1

            device = DeviceRepository.find_by_pm(db_session, "PM-001")
            assert device.status == DeviceStatus.AVAILABLE
            assert device.current_borrower_id is None
        finally:
            path.unlink(missing_ok=True)

    def test_name_in_location_sets_borrowed(self, db_session):
        """Device with a person's name in location column is imported as BORROWED."""
        path = _create_test_excel([
            ["Equipment", "Hersteller", "Typbezeichnung",
             "Platz Messmittelschrank", "Aktueller Einsatzort"],
            ["PM-001", "Fluke", "87V", "Schrank 1", "Max Müller"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path)
            assert result.imported == 1

            device = DeviceRepository.find_by_pm(db_session, "PM-001")
            assert device.status == DeviceStatus.BORROWED
            # Borrower not in our system — no linked user
            assert device.current_borrower_id is None
        finally:
            path.unlink(missing_ok=True)

    def test_known_borrower_linked(self, db_session):
        """When the borrower name matches a registered user, the device is linked."""
        UserRepository.create(
            db_session,
            encrypted_card_uid="dummy_enc_aabb",
            uid_hmac="aabb" * 16,
            display_name="Max Müller",
            role="user",
        )
        db_session.commit()

        path = _create_test_excel([
            ["Equipment", "Hersteller", "Typbezeichnung",
             "Platz Messmittelschrank", "Aktueller Einsatzort"],
            ["PM-001", "Fluke", "87V", "Schrank 1", "Max Müller"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path)
            assert result.imported == 1

            device = DeviceRepository.find_by_pm(db_session, "PM-001")
            assert device.status == DeviceStatus.BORROWED
            assert device.current_borrower_id is not None

            user = UserRepository.find_by_display_name(db_session, "Max Müller")
            assert device.current_borrower_id == user.id
        finally:
            path.unlink(missing_ok=True)

    def test_no_location_column_defaults_available(self, db_session):
        """Without a location column, devices default to AVAILABLE."""
        path = _create_test_excel([
            ["Equipment", "Hersteller", "Typbezeichnung", "Platz Messmittelschrank"],
            ["PM-001", "Fluke", "87V", "Schrank 1"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path)
            assert result.imported == 1

            device = DeviceRepository.find_by_pm(db_session, "PM-001")
            assert device.status == DeviceStatus.AVAILABLE
        finally:
            path.unlink(missing_ok=True)

    def test_update_existing_device_status(self, db_session):
        """Re-importing with a changed location updates an existing device's status."""
        # Create an AVAILABLE device
        DeviceRepository.create(
            db_session,
            name="PM-001 Fluke 87V",
            device_type="general",
            pm_number="PM-001",
            manufacturer="Fluke",
            model="87V",
        )
        db_session.commit()

        # Import with a person's name in location → should become BORROWED
        path = _create_test_excel([
            ["Equipment", "Hersteller", "Typbezeichnung",
             "Platz Messmittelschrank", "Aktueller Einsatzort"],
            ["PM-001", "Fluke", "87V", "Schrank 1", "Anna Schmidt"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path)
            assert result.updated == 1

            device = DeviceRepository.find_by_pm(db_session, "PM-001")
            assert device.status == DeviceStatus.BORROWED
        finally:
            path.unlink(missing_ok=True)

    def test_borrower_lookup_case_insensitive(self, db_session):
        """Borrower name matching is case-insensitive."""
        UserRepository.create(
            db_session,
            encrypted_card_uid="dummy_enc_ccdd",
            uid_hmac="ccdd" * 16,
            display_name="Anna Schmidt",
            role="user",
        )
        db_session.commit()

        path = _create_test_excel([
            ["Equipment", "Hersteller", "Typbezeichnung",
             "Platz Messmittelschrank", "Aktueller Einsatzort"],
            ["PM-001", "Fluke", "87V", "Schrank 1", "anna schmidt"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path)
            assert result.imported == 1

            device = DeviceRepository.find_by_pm(db_session, "PM-001")
            assert device.current_borrower_id is not None
        finally:
            path.unlink(missing_ok=True)


class TestRegistrantExtraction:
    """Tests for registrant name extraction from the 'Aktueller Einsatzort' column.

    During source import, unique person names (non-schrank values) from the
    location column across ALL rows are added to the registrants table for
    use in the self-service registration name list.
    """

    def test_names_extracted_from_all_rows(self, db_session):
        """Person names are extracted from ALL rows, not just schrank-filtered ones."""
        path = _create_test_excel([
            ["Equipment", "Hersteller", "Typbezeichnung",
             "Platz Messmittelschrank", "Aktueller Einsatzort"],
            # Schrank device with person name
            ["PM-001", "Fluke", "87V", "Schrank 1", "Max Müller"],
            # Non-schrank device (skipped for device import but name still extracted)
            ["PM-002", "Keysight", "34465A", "Labor 3", "Anna Schmidt"],
            # Another schrank device with schrank location (not a person name)
            ["PM-003", "Tektronix", "TBS2104X", "Schrank 2", "Messmittelschrank"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path)

            # Both person names should be in registrants, schrank value should not
            assert result.registrants_added == 2
            registrants = RegistrantRepository.get_all(db_session)
            names = [r.display_name for r in registrants]
            assert "Max Müller" in names
            assert "Anna Schmidt" in names
            assert "Messmittelschrank" not in names
        finally:
            path.unlink(missing_ok=True)

    def test_duplicate_names_deduplicated(self, db_session):
        """Duplicate names in the Excel are stored only once in registrants."""
        path = _create_test_excel([
            ["Equipment", "Hersteller", "Typbezeichnung",
             "Platz Messmittelschrank", "Aktueller Einsatzort"],
            ["PM-001", "Fluke", "87V", "Schrank 1", "Max Müller"],
            ["PM-002", "Keysight", "34465A", "Schrank 2", "Max Müller"],
            ["PM-003", "Tektronix", "TBS2104X", "Schrank 3", "Anna Schmidt"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path)

            # Only 2 unique names, not 3
            assert result.registrants_added == 2
            registrants = RegistrantRepository.get_all(db_session)
            assert len(registrants) == 2
        finally:
            path.unlink(missing_ok=True)

    def test_registrant_sync_additive(self, db_session):
        """Subsequent imports add new names but keep existing ones."""
        # First import with two names
        path1 = _create_test_excel([
            ["Equipment", "Platz Messmittelschrank", "Aktueller Einsatzort"],
            ["PM-001", "Schrank 1", "Max Müller"],
            ["PM-002", "Schrank 2", "Anna Schmidt"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result1 = import_from_source_excel(get_engine(), path1)
            assert result1.registrants_added == 2
        finally:
            path1.unlink(missing_ok=True)

        # Second import with one new name and one existing
        path2 = _create_test_excel([
            ["Equipment", "Platz Messmittelschrank", "Aktueller Einsatzort"],
            ["PM-003", "Schrank 3", "Anna Schmidt"],
            ["PM-004", "Schrank 4", "Lisa Weber"],
        ])
        try:
            result2 = import_from_source_excel(get_engine(), path2)
            # Only Lisa Weber is new; Anna Schmidt already exists
            assert result2.registrants_added == 1

            registrants = RegistrantRepository.get_all(db_session)
            names = [r.display_name for r in registrants]
            assert len(names) == 3
            assert "Max Müller" in names
            assert "Anna Schmidt" in names
            assert "Lisa Weber" in names
        finally:
            path2.unlink(missing_ok=True)

    def test_schrank_values_excluded(self, db_session):
        """Values containing 'schrank' are not treated as person names."""
        path = _create_test_excel([
            ["Equipment", "Platz Messmittelschrank", "Aktueller Einsatzort"],
            ["PM-001", "Schrank 1", "Messmittelschrank"],
            ["PM-002", "Schrank 2", "Schrank A"],
            ["PM-003", "Schrank 3", "Max Müller"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path)

            # Only "Max Müller" should be added (schrank values excluded)
            assert result.registrants_added == 1
            registrant = RegistrantRepository.find_by_name(db_session, "Max Müller")
            assert registrant is not None
        finally:
            path.unlink(missing_ok=True)

    def test_no_location_column_no_registrants(self, db_session):
        """Without a location column, no registrant names are extracted."""
        path = _create_test_excel([
            ["Equipment", "Hersteller", "Platz Messmittelschrank"],
            ["PM-001", "Fluke", "Schrank 1"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path)
            assert result.registrants_added == 0
            registrants = RegistrantRepository.get_all(db_session)
            assert len(registrants) == 0
        finally:
            path.unlink(missing_ok=True)
