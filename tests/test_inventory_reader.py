"""
File: test_inventory_reader.py
Description: Tests for the dashboard Inventory tab Excel reader. Reads the
             full catalog sheet live (not SQLite). Share missing or locked
             is an error for Inventory only.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_inventory_reader.py -v
"""

from pathlib import Path

import pytest
from openpyxl import Workbook

from smart_locker.sync.inventory_reader import (
    InventoryReadError,
    read_inventory,
)
from smart_locker.sync.source_import import CatalogReadError


def _workbook(path: Path, rows: list[list]) -> Path:
    """Write a temporary catalog workbook and return its path."""
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    wb.save(path)
    return path


class TestReadInventoryFromExcel:
    """Inventory is the full Excel sheet, including PMs that are not locker rows."""

    def test_returns_all_excel_rows(self, tmp_path):
        """Every data row with a PM is returned, locker or not."""
        path = _workbook(tmp_path / "device-list.xlsx", [
            ["PM", "Name", "Manufacturer", "Model", "Serial", "Location", "Calibration due"],
            ["PM-001", "Scope", "Keysight", "DSOX", "SN-1", "Locker", "2026-01-01"],
            ["PM-999", "Van kit", "Fluke", "87V", "SN-9", "Workshop", None],
        ])
        rows = read_inventory(path)
        assert [r.pm_number for r in rows] == ["PM-001", "PM-999"]
        van = rows[1]
        assert van.name == "Van kit"
        assert van.manufacturer == "Fluke"
        assert van.model == "87V"
        assert van.serial_number == "SN-9"
        assert van.location == "Workshop"

    def test_location_column_is_owner_or_place(self, tmp_path):
        """Location is the Excel cell (person or place), not SQLite status."""
        path = _workbook(tmp_path / "device-list.xlsx", [
            ["Equipment", "Name", "Current location"],
            ["PM-010", "Calibrator", "Alex Johnson"],
        ])
        rows = read_inventory(path)
        assert len(rows) == 1
        assert rows[0].location == "Alex Johnson"

    def test_missing_file_raises(self, tmp_path):
        """Share down / missing workbook is an error, not an empty list."""
        with pytest.raises((InventoryReadError, CatalogReadError)):
            read_inventory(tmp_path / "no-such.xlsx")

    def test_no_pm_column_raises(self, tmp_path):
        """A sheet with no join-key column cannot be shown as Inventory."""
        path = _workbook(tmp_path / "device-list.xlsx", [
            ["Name", "Notes"],
            ["Scope", "skip me"],
        ])
        with pytest.raises((InventoryReadError, CatalogReadError)):
            read_inventory(path)

    def test_skips_blank_pm_rows(self, tmp_path):
        """Empty PM cells are ignored."""
        path = _workbook(tmp_path / "device-list.xlsx", [
            ["PM", "Name", "Location"],
            ["PM-001", "Scope", "Locker"],
            [None, "orphan", "Workshop"],
        ])
        rows = read_inventory(path)
        assert [r.pm_number for r in rows] == ["PM-001"]
