"""
File: test_source_import.py
Description: Tests for the source Excel import module. Validates column
             auto-detection, catalog-only updates (never insert locker rows),
             metadata-only updates, date parsing from common Excel formats,
             and registrant list replace from the Location
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
from smart_locker.security.hashing import compute_uid_hmac
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
        assert find_column(["Make", "Model"], ["manufacturer", "make"]) == 0

    def test_location_preferred_over_owner(self):
        """Location wins when both Owner and Location headers exist."""
        from smart_locker.sync.source_import import location_candidates

        headers = ["Equipment", "Owner", "Location"]
        idx = find_column(headers, location_candidates())
        assert idx == 2


class TestPmNormalizeAndInLocker:
    """I20 whole-word locker match; I21 casefold and Excel 1001.0."""

    def test_blocker_is_not_in_locker(self):
        """is_in_locker_location('Blocker') is False."""
        from smart_locker.sync.source_import import is_in_locker_location

        assert is_in_locker_location("Blocker") is False
        assert is_in_locker_location("Locker") is True

    def test_normalize_pm_float_and_case(self):
        """PM-001 vs pm-001 share a key; 1001.0 becomes 1001."""
        from smart_locker.sync.source_import import normalize_pm, pm_match_key

        assert normalize_pm(1001.0) == "1001"
        assert normalize_pm("1001.0") == "1001"
        assert pm_match_key("PM-001") == pm_match_key("pm-001")


class TestEnvIntFallback:
    """I18: non-numeric interval env falls back to 6."""

    def test_non_numeric_falls_back_to_default(self, monkeypatch):
        from config.settings import _env_int

        monkeypatch.setenv("SMART_LOCKER_SOURCE_SYNC_INTERVAL_HOURS", "6h")
        assert _env_int("SMART_LOCKER_SOURCE_SYNC_INTERVAL_HOURS", 6, minimum=1) == 6



class TestParseDate:
    """Tests for date parsing from day-month-year and ISO Excel formats."""
    def test_none(self):
        """Verify None input returns None."""
        assert parse_date(None) is None

    def test_day_month_year_format(self):
        """Verify DD.MM.YYYY day-month-year format is parsed correctly."""
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
    """Tests for the source Excel import pipeline — catalog update only, never insert."""

    def test_new_pm_is_not_inserted(self, db_session):
        """Excel PMs that are not already locker devices are skipped, not inserted."""
        path = _create_test_excel([
            ["Equipment", "Manufacturer", "Model", "Slot"],
            ["PM-001", "Fluke", "87V", "Bay 1"],
            ["PM-002", "Keysight", "34465A", "Lab 3"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path)
            assert result.imported == 0
            assert result.non_locker_skipped == 2
            assert result.errors == 0
            assert DeviceRepository.find_by_pm(db_session, "PM-001") is None
            assert DeviceRepository.find_by_pm(db_session, "PM-002") is None
        finally:
            path.unlink(missing_ok=True)

    def test_catalog_updates_without_slot_column(self, db_session):
        """A Slot column is unused; an existing locker PM still gets catalog updates."""
        DeviceRepository.create(
            db_session,
            name="Fluke 87V",
            device_type="general",
            pm_number="PM-001",
            manufacturer="Fluke",
            model="87V",
            locker_slot=4,
        )
        db_session.commit()

        path = _create_test_excel([
            ["Equipment", "Manufacturer", "Model"],
            ["PM-001", "Fluke", "87-V MAX"],
            ["PM-NEW", "Keysight", "34465A"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path)
            assert result.imported == 0
            assert result.updated == 1
            assert result.non_locker_skipped == 1

            device = DeviceRepository.find_by_pm(db_session, "PM-001")
            assert device.model == "87-V MAX"
            assert device.locker_slot == 4
            assert DeviceRepository.find_by_pm(db_session, "PM-NEW") is None
        finally:
            path.unlink(missing_ok=True)

    def test_reimport_skips_serial_already_used_by_another_device(self, db_session):
        """A catalog serial that belongs to a different locker PM must not abort sync."""
        DeviceRepository.create(
            db_session,
            name="Fluke 87V",
            device_type="general",
            pm_number="PM-001",
            locker_slot=1,
        )
        DeviceRepository.create(
            db_session,
            name="Other",
            device_type="general",
            pm_number="PM-002",
            serial_number="SN-SHARED",
            locker_slot=2,
        )
        db_session.commit()

        path = _create_test_excel([
            ["Equipment", "Manufacturer", "Model", "Serial Number"],
            ["PM-001", "Fluke", "87-V MAX", "SN-SHARED"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path)
            assert result.errors == 0
            assert result.updated == 1
            db_session.expire_all()
            assert DeviceRepository.find_by_pm(db_session, "PM-001").serial_number is None
            assert DeviceRepository.find_by_pm(db_session, "PM-001").model == "87-V MAX"
            assert DeviceRepository.find_by_pm(db_session, "PM-002").serial_number == "SN-SHARED"
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
            ["Equipment", "Manufacturer", "Model", "Slot"],
            ["PM-001", "Fluke", "87V", "Bay 1"],
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
            ["Equipment", "Manufacturer", "Model", "Slot"],
            ["PM-001", "Fluke", "87-V MAX", "Bay 1"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path)
            assert result.updated == 1

            device = DeviceRepository.find_by_pm(db_session, "PM-001")
            assert device.model == "87-V MAX"
        finally:
            path.unlink(missing_ok=True)

    def test_missing_catalog_columns_do_not_wipe_sqlite(self, db_session):
        """Serial/Type/Calibration absent from Excel must not clear SQLite values."""
        from datetime import date

        DeviceRepository.create(
            db_session,
            name="Fluke 87V",
            device_type="Multimeter",
            pm_number="PM-001",
            serial_number="FL-87V-007",
            manufacturer="Fluke",
            model="87V",
            calibration_due=date(2026, 6, 30),
            locker_slot=1,
        )
        db_session.commit()

        path = _create_test_excel([
            ["Equipment", "Manufacturer", "Model"],
            ["PM-001", "Fluke", "87-V MAX"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path)
            assert result.updated == 1
            device = DeviceRepository.find_by_pm(db_session, "PM-001")
            assert device.model == "87-V MAX"
            assert device.serial_number == "FL-87V-007"
            assert device.device_type == "Multimeter"
            assert device.calibration_due == date(2026, 6, 30)
        finally:
            path.unlink(missing_ok=True)

    def test_empty_catalog_cells_do_not_wipe_sqlite(self, db_session):
        """A Serial/Type/Calibration column with a blank cell does not clear SQLite."""
        from datetime import date

        DeviceRepository.create(
            db_session,
            name="Fluke 87V",
            device_type="Multimeter",
            pm_number="PM-001",
            serial_number="FL-87V-007",
            manufacturer="Fluke",
            model="87V",
            calibration_due=date(2026, 6, 30),
            locker_slot=1,
        )
        db_session.commit()

        path = _create_test_excel([
            ["Equipment", "Type", "Serial", "Manufacturer", "Model", "Calibration due"],
            ["PM-001", None, None, "Fluke", "87-V MAX", None],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path)
            assert result.updated == 1
            device = DeviceRepository.find_by_pm(db_session, "PM-001")
            assert device.model == "87-V MAX"
            assert device.serial_number == "FL-87V-007"
            assert device.device_type == "Multimeter"
            assert device.calibration_due == date(2026, 6, 30)
        finally:
            path.unlink(missing_ok=True)

    def test_file_not_found(self):
        """Nonexistent file returns error result without crashing."""
        result = import_from_source_excel(None, "/nonexistent/file.xlsx")
        assert result.errors == 1
        assert "not found" in result.error_details[0].lower()

    def test_dry_run_no_writes(self, db_session):
        """Dry run reports skipped new PMs and writes nothing."""
        path = _create_test_excel([
            ["Equipment", "Slot"],
            ["PM-001", "Bay 1"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path, dry_run=True)
            assert result.imported == 0
            assert result.non_locker_skipped == 1
            assert DeviceRepository.find_by_pm(db_session, "PM-001") is None
        finally:
            path.unlink(missing_ok=True)

    def test_dry_run_reports_update_and_unchanged_without_writing(self, db_session):
        """Dry run splits existing devices into would-update vs unchanged, no writes."""
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
            ["Equipment", "Manufacturer", "Model", "Slot"],
            ["PM-001", "Fluke", "87-V MAX", "Bay 1"],   # model changed -> would update
            ["PM-002", "Keysight", "34465A", "Bay 2"],  # not in locker -> skipped
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path, dry_run=True)
            assert (result.imported, result.updated, result.unchanged) == (0, 1, 0)
            assert result.non_locker_skipped == 1

            assert DeviceRepository.find_by_pm(db_session, "PM-001").model == "87V"
            assert DeviceRepository.find_by_pm(db_session, "PM-002") is None
        finally:
            path.unlink(missing_ok=True)

    def test_english_column_headers(self, db_session):
        """Column headers are auto-detected on an existing locker PM."""
        DeviceRepository.create(
            db_session,
            name="Old",
            device_type="general",
            pm_number="PM-001",
            manufacturer="Old",
            model="Old",
            locker_slot=1,
        )
        db_session.commit()

        path = _create_test_excel([
            ["Equipment", "Manufacturer", "Model", "Serial Number",
             "Barcode", "Slot", "Category"],
            ["PM-001", "Rohde & Schwarz", "RTB2004", "SN-12345", "BC-001", "Bay 1", "Oscilloscope"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path)
            assert result.updated == 1
            assert result.imported == 0

            device = DeviceRepository.find_by_pm(db_session, "PM-001")
            assert device.manufacturer == "Rohde & Schwarz"
            assert device.serial_number == "SN-12345"
            assert device.device_type == "Oscilloscope"
            assert device.barcode is None
            assert device.locker_slot == 1
        finally:
            path.unlink(missing_ok=True)


class TestLocationColumn:
    """Location is not locker membership. Sync never inserts; status stays locker-local."""

    def test_new_pm_not_inserted_even_with_location(self, db_session):
        """A person name in Location does not create a locker row."""
        path = _create_test_excel([
            ["Equipment", "Manufacturer", "Model",
             "Slot", "Location"],
            ["PM-001", "Fluke", "87V", "Bay 1", "Alice"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path)
            assert result.imported == 0
            assert DeviceRepository.find_by_pm(db_session, "PM-001") is None
        finally:
            path.unlink(missing_ok=True)

    def test_reimport_preserves_tag_hmac(self, db_session, hmac_key):
        """Re-import must not overwrite tag_hmac; leftover barcode is ignored."""
        device = DeviceRepository.create(
            db_session,
            name="Fluke 87V",
            device_type="general",
            pm_number="PM-001",
            manufacturer="Fluke",
            model="87V",
            serial_number="SN-OLD",
            barcode="OLD-BC",
        )
        digest = compute_uid_hmac("AABBCCDD", hmac_key)
        DeviceRepository.bind_tag(db_session, device, digest)
        db_session.commit()

        path = _create_test_excel([
            ["Equipment", "Manufacturer", "Model", "Serial Number",
             "Barcode", "Slot", "Location"],
            ["PM-001", "Fluke", "87V", "SN-NEW", "NEW-BC", "Bay 1", "Bob"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path)
            assert result.updated == 1

            db_session.expire_all()
            device = DeviceRepository.find_by_pm(db_session, "PM-001")
            assert device.tag_hmac == digest
            assert device.serial_number == "SN-NEW"
            assert device.barcode == "OLD-BC"
            assert device.status == DeviceStatus.AVAILABLE
            assert device.current_borrower_id is None
        finally:
            path.unlink(missing_ok=True)

    def test_reimport_does_not_overwrite_existing_status(self, db_session):
        """Stale Excel Location must not replace locker status on an existing PM."""
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
            ["Equipment", "Manufacturer", "Model",
             "Slot", "Location"],
            ["PM-001", "Fluke", "87V", "Bay 1", "Bob"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path)
            assert result.unchanged == 1

            db_session.expire_all()
            device = DeviceRepository.find_by_pm(db_session, "PM-001")
            assert device.status == DeviceStatus.AVAILABLE
            assert device.current_borrower_id is None
        finally:
            path.unlink(missing_ok=True)

    def test_reimport_does_not_undo_kiosk_borrow(self, db_session):
        """A kiosk borrow survives re-import of a sheet that still says Locker."""
        user = UserRepository.create(
            db_session,
            display_name="Bob",
            uid_hmac="dd" * 16,
            encrypted_card_uid="enc_bob",
            role="user",
        )
        device = DeviceRepository.create(
            db_session,
            name="Fluke 87V",
            device_type="general",
            pm_number="PM-001",
            manufacturer="Fluke",
            model="87V",
            serial_number="SN-OLD",
            barcode="OLD-BC",
        )
        DeviceRepository.borrow(db_session, device, user.id)
        db_session.commit()

        path = _create_test_excel([
            ["Equipment", "Manufacturer", "Model", "Serial Number",
             "Slot", "Location"],
            ["PM-001", "Fluke", "87V", "SN-NEW", "Bay 1", "Locker"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path)
            assert result.updated == 1

            db_session.expire_all()
            device = DeviceRepository.find_by_pm(db_session, "PM-001")
            assert device.status == DeviceStatus.BORROWED
            assert device.current_borrower_id == user.id
            assert device.serial_number == "SN-NEW"
            assert device.barcode == "OLD-BC"
        finally:
            path.unlink(missing_ok=True)

    def test_borrower_name_still_extracted_as_registrant(self, db_session):
        """Location person names still feed the self-register list when Sync skips the PM."""
        path = _create_test_excel([
            ["Equipment", "Manufacturer", "Model",
             "Slot", "Location"],
            ["PM-001", "Fluke", "87V", "Bay 1", "bob"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path)
            assert result.imported == 0
            assert result.registrants_added == 1
            assert DeviceRepository.find_by_pm(db_session, "PM-001") is None
        finally:
            path.unlink(missing_ok=True)


class TestRegistrantExtraction:
    """Tests for registrant name extraction from the 'Location' column.

    During source import, unique person names (not in-locker values) from the
    location column across ALL rows become the registrants table for
    the self-service registration name list. Names that leave Location
    are removed.
    """

    def test_names_extracted_from_all_rows(self, db_session):
        """Person names are extracted from ALL rows, not only locker rows."""
        path = _create_test_excel([
            ["Equipment", "Manufacturer", "Model",
             "Slot", "Location"],
            # Locker row with a person name
            ["PM-001", "Fluke", "87V", "Bay 1", "Alice"],
            # Non-locker device (skipped for device import but name still extracted)
            ["PM-002", "Keysight", "34465A", "Lab 3", "Bob"],
            # Locker location token (not a person name)
            ["PM-003", "Tektronix", "TBS2104X", "Bay 2", "Locker"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path)

            # Both person names should be in registrants, locker token should not
            assert result.registrants_added == 2
            registrants = RegistrantRepository.get_all(db_session)
            names = [r.display_name for r in registrants]
            assert "Alice" in names
            assert "Bob" in names
            assert "Locker" not in names
        finally:
            path.unlink(missing_ok=True)

    def test_duplicate_names_deduplicated(self, db_session):
        """Duplicate names in the Excel are stored only once in registrants."""
        path = _create_test_excel([
            ["Equipment", "Manufacturer", "Model",
             "Slot", "Location"],
            ["PM-001", "Fluke", "87V", "Bay 1", "Alice"],
            ["PM-002", "Keysight", "34465A", "Bay 2", "Alice"],
            ["PM-003", "Tektronix", "TBS2104X", "Bay 3", "Bob"],
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

    def test_registrant_sync_replaces_list(self, db_session):
        """Subsequent imports drop names that left Excel Location."""
        path1 = _create_test_excel([
            ["Equipment", "Slot", "Location"],
            ["PM-001", "Bay 1", "Alice"],
            ["PM-002", "Bay 2", "Bob"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result1 = import_from_source_excel(get_engine(), path1)
            assert result1.registrants_added == 2
        finally:
            path1.unlink(missing_ok=True)

        path2 = _create_test_excel([
            ["Equipment", "Slot", "Location"],
            ["PM-003", "Bay 3", "Bob"],
            ["PM-004", "Bay 4", "Carol"],
        ])
        try:
            result2 = import_from_source_excel(get_engine(), path2)
            assert result2.registrants_added == 1

            registrants = RegistrantRepository.get_all(db_session)
            names = [r.display_name for r in registrants]
            assert len(names) == 2
            assert "Alice" not in names
            assert "Bob" in names
            assert "Carol" in names
        finally:
            path2.unlink(missing_ok=True)

    def test_locker_location_values_excluded(self, db_session):
        """In-locker Location values are not treated as person names."""
        path = _create_test_excel([
            ["Equipment", "Slot", "Location"],
            ["PM-001", "Bay 1", "Locker"],
            ["PM-002", "Bay 2", "Cabinet A"],
            ["PM-003", "Bay 3", "Alice"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path)

            # Only "Alice" should be added (in-locker values excluded)
            assert result.registrants_added == 1
            registrant = RegistrantRepository.find_by_name(db_session, "Alice")
            assert registrant is not None
        finally:
            path.unlink(missing_ok=True)

    def test_no_location_column_no_registrants(self, db_session):
        """Without a location column, no registrant names are extracted."""
        path = _create_test_excel([
            ["Equipment", "Manufacturer", "Slot"],
            ["PM-001", "Fluke", "Bay 1"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path)
            assert result.registrants_added == 0
            registrants = RegistrantRepository.get_all(db_session)
            assert len(registrants) == 0
        finally:
            path.unlink(missing_ok=True)

    def test_no_location_column_preserves_existing_registrants(self, db_session):
        """Missing Location column must keep self-register names already in SQLite."""
        RegistrantRepository.add_names(db_session, {"Alice", "Bob"})
        db_session.commit()
        path = _create_test_excel([
            ["Equipment", "Manufacturer", "Slot"],
            ["PM-001", "Fluke", "Bay 1"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path)
            assert result.registrants_added == 0
            db_session.expire_all()
            names = {r.display_name for r in RegistrantRepository.get_all(db_session)}
            assert names == {"Alice", "Bob"}
        finally:
            path.unlink(missing_ok=True)

    def test_sync_names_failure_is_not_clean_success(self, db_session, monkeypatch):
        """If registrant prune/sync fails, import must surface an error count."""
        path = _create_test_excel([
            ["Equipment", "Slot", "Location"],
            ["PM-001", "Bay 1", "Alice"],
        ])
        try:
            from smart_locker.database.engine import get_engine

            def boom(session, names):
                raise RuntimeError("prune failed")

            monkeypatch.setattr(RegistrantRepository, "sync_names", boom)
            result = import_from_source_excel(get_engine(), path)
            assert result.errors >= 1
            assert any("Registrant sync" in d for d in result.error_details)
        finally:
            path.unlink(missing_ok=True)


class TestSiteHeaderAliases:
    """Extra Excel header names come from env; built-in English aliases still match."""

    def test_id_header_extra_matches_inventory_column(self, db_session, monkeypatch):
        """SMART_LOCKER_ID_HEADERS adds a join-key alias used by Sync."""
        monkeypatch.setenv("SMART_LOCKER_ID_HEADERS", "Inventory No")
        DeviceRepository.create(
            db_session,
            name="Old",
            device_type="general",
            pm_number="PM-001",
            manufacturer="Old",
            model="Old",
            locker_slot=1,
        )
        db_session.commit()
        path = _create_test_excel([
            ["Inventory No", "Manufacturer", "Model"],
            ["PM-001", "Fluke", "87V"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path)
            assert result.updated == 1
            device = DeviceRepository.find_by_pm(db_session, "PM-001")
            assert device.manufacturer == "Fluke"
            assert device.model == "87V"
        finally:
            path.unlink(missing_ok=True)

    def test_builtin_equipment_header_still_matches(self, db_session, monkeypatch):
        """A site extra must not drop the built-in Equipment alias."""
        monkeypatch.setenv("SMART_LOCKER_ID_HEADERS", "Inventory No")
        DeviceRepository.create(
            db_session,
            name="Old",
            device_type="general",
            pm_number="PM-001",
            manufacturer="Old",
            model="Old",
            locker_slot=1,
        )
        db_session.commit()
        path = _create_test_excel([
            ["Equipment", "Manufacturer", "Model"],
            ["PM-001", "Keysight", "34465A"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path)
            assert result.updated == 1
            assert DeviceRepository.find_by_pm(db_session, "PM-001").manufacturer == "Keysight"
        finally:
            path.unlink(missing_ok=True)

    def test_location_header_extra_extracts_registrants(self, db_session, monkeypatch):
        """SMART_LOCKER_LOCATION_HEADERS adds a Location-column alias."""
        monkeypatch.setenv("SMART_LOCKER_LOCATION_HEADERS", "Whereabouts")
        path = _create_test_excel([
            ["Equipment", "Whereabouts"],
            ["PM-001", "Alice"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path)
            assert result.registrants_added == 1
            assert RegistrantRepository.find_by_name(db_session, "Alice") is not None
        finally:
            path.unlink(missing_ok=True)

    def test_custom_in_locker_token_is_not_a_person(self, db_session, monkeypatch):
        """SMART_LOCKER_IN_LOCKER_TOKEN is not added to the registrant list."""
        monkeypatch.setenv("SMART_LOCKER_IN_LOCKER_TOKEN", "At base")
        path = _create_test_excel([
            ["Equipment", "Location"],
            ["PM-001", "At base"],
            ["PM-002", "Alice"],
        ])
        try:
            from smart_locker.database.engine import get_engine
            result = import_from_source_excel(get_engine(), path)
            assert result.registrants_added == 1
            names = [r.display_name for r in RegistrantRepository.get_all(db_session)]
            assert "Alice" in names
            assert "At base" not in names
        finally:
            path.unlink(missing_ok=True)


class TestImportEngineAndSavepoints:
    """I14: use the passed engine; a row flush error must not poison later rows."""

    def test_import_uses_passed_engine(self, db_session, tmp_path):
        """Catalog updates go to the engine argument, not the global factory."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        from smart_locker.database.models import Base

        other = create_engine(
            f"sqlite:///{tmp_path / 'other.db'}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(other)
        with Session(other) as session:
            DeviceRepository.create(
                session,
                name="Isolated",
                device_type="general",
                pm_number="PM-ISO",
                manufacturer="OldMfr",
                model="X",
                locker_slot=1,
            )
            session.commit()

        DeviceRepository.create(
            db_session,
            name="Decoy",
            device_type="general",
            pm_number="PM-ISO",
            manufacturer="Decoy",
            model="Y",
            locker_slot=1,
        )
        db_session.commit()

        path = _create_test_excel([
            ["Equipment", "Manufacturer", "Model"],
            ["PM-ISO", "NewMfr", "Z"],
        ])
        try:
            result = import_from_source_excel(other, path)
            assert result.updated == 1
            assert result.errors == 0
        finally:
            path.unlink(missing_ok=True)

        with Session(other) as session:
            isolated = DeviceRepository.find_by_pm(session, "PM-ISO")
            assert isolated is not None
            assert isolated.manufacturer == "NewMfr"
        decoy = DeviceRepository.find_by_pm(db_session, "PM-ISO")
        assert decoy.manufacturer == "Decoy"

    def test_row_integrity_error_does_not_block_later_rows(
        self, db_session, monkeypatch
    ):
        """A unique-serial flush failure on one PM still commits the next row."""
        DeviceRepository.create(
            db_session,
            name="First",
            device_type="general",
            pm_number="PM-001",
            serial_number="SN-A",
            manufacturer="Old",
            locker_slot=1,
        )
        DeviceRepository.create(
            db_session,
            name="Second",
            device_type="general",
            pm_number="PM-002",
            serial_number="SN-B",
            manufacturer="Old",
            locker_slot=2,
        )
        db_session.commit()

        monkeypatch.setattr(
            DeviceRepository,
            "find_by_serial",
            staticmethod(lambda session, serial: None),
        )

        path = _create_test_excel([
            ["Equipment", "Manufacturer", "Serial"],
            ["PM-001", "Keep", "SN-B"],
            ["PM-002", "NewMfr", "SN-B-ok"],
        ])
        try:
            from smart_locker.database.engine import get_engine

            result = import_from_source_excel(get_engine(), path)
            assert result.errors == 1
            assert result.updated == 1
        finally:
            path.unlink(missing_ok=True)

        db_session.expire_all()
        first = DeviceRepository.find_by_pm(db_session, "PM-001")
        second = DeviceRepository.find_by_pm(db_session, "PM-002")
        assert first.serial_number == "SN-A"
        assert second.manufacturer == "NewMfr"
