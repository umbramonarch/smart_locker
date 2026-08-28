"""
File: helpers.py
Description: Shared helpers for kiosk API tests (catalog workbook, dashboard
             admin headers).
Project: smart_locker/tests/api
Notes: Imported by tests/api/test_*.py. Not a pytest plugin.
"""


def dashboard_admin_headers(secret: str) -> dict:
    """JSON + X-Smart-Locker-Admin for dashboard bind/unbind."""
    return {
        "Content-Type": "application/json",
        "X-Smart-Locker-Admin": secret,
    }


def catalog_workbook(tmp_path, rows):
    """Write a temporary device-list.xlsx and return its path."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    path = tmp_path / "device-list.xlsx"
    wb.save(path)
    return path
