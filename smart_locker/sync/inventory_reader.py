"""
File: inventory_reader.py
Description: Read the full catalog Excel for the dashboard Inventory tab.
             Live share file, not SQLite. Missing or locked workbook is an
             error so the tab can fail without taking Locker down.
Project: smart_locker/sync
Notes: Reuses source_import column detection. This module is read-only and
       does not insert or update locker rows. Owner POST lives on the
       dashboard route (SMART_LOCKER_DASHBOARD_ADMIN_SECRET), not here.
"""

from dataclasses import dataclass
from pathlib import Path

from smart_locker.sync.source_import import (
    CatalogReadError,
    _catalog_from_row,
    _cell_str,
    _detect_columns,
    _load_rows,
)

InventoryReadError = CatalogReadError


@dataclass(frozen=True)
class InventoryRow:
    """One catalog row as shown on the dashboard Inventory tab."""

    pm_number: str
    name: str
    manufacturer: str | None
    model: str | None
    serial_number: str | None
    location: str | None
    calibration_due: str | None


def read_inventory(
    source_path: str | Path,
    sheet_name: str | None = None,
    column_overrides: dict[str, str] | None = None,
) -> list[InventoryRow]:
    """Return every Excel catalog row that has a PM.

    Args:
        source_path: Path to ``device-list.xlsx`` on the locker share.
        sheet_name: Sheet to read, or None for the active sheet.
        column_overrides: Optional header-name overrides.

    Returns:
        Inventory rows in sheet order. PMs that are not locker devices
        are included.

    Raises:
        InventoryReadError: File missing, locked, empty, or no PM column.
    """
    path = Path(source_path)
    rows, err = _load_rows(path, sheet_name)
    if err:
        raise InventoryReadError(err)
    if not rows or len(rows) < 2:
        raise InventoryReadError("Source Excel has no data rows.")

    headers = [str(h).strip() if h else "" for h in rows[0]]
    cols = _detect_columns(headers, column_overrides)
    if cols["pm"] is None:
        raise InventoryReadError(
            f"Could not find PM/equipment column. Headers: {headers}"
        )

    compose_name = cols["name"] is None
    out: list[InventoryRow] = []
    for row in rows[1:]:
        catalog = _catalog_from_row(row, cols, compose_name, "general")
        if catalog is None:
            continue
        cal = (
            catalog.calibration_due.isoformat()
            if catalog.calibration_due is not None
            else None
        )
        out.append(
            InventoryRow(
                pm_number=catalog.pm_number,
                name=catalog.name,
                manufacturer=catalog.manufacturer,
                model=catalog.model,
                serial_number=catalog.serial_number,
                location=_cell_str(row, cols["location"]),
                calibration_due=cal,
            )
        )
    return out
