"""
File: excel_sync.py
Description: On-demand in-memory Excel export of device, transaction, and user
             data for the admin download endpoint.
Project: smart_locker/sync
Notes: ``export_to_excel_bytes()`` delegates to ``_build_workbook()``, which
       queries the database and assembles the openpyxl Workbook in memory.
       Three sheets are produced: Devices (including Tagged Yes/No, never
       tag_hmac), Transactions, and Users.
"""

import io

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from sqlalchemy import select
from sqlalchemy.orm import Session

from smart_locker.database.models import Device, TransactionLog, User

def _build_workbook(engine) -> Workbook:
    """Query the database and build a styled three-sheet Excel workbook.

    Creates sheets for Devices (inventory with status and borrower),
    Transactions (borrow/return history in reverse chronological order),
    and Users (registered users with role and registration date). Each
    sheet has bold blue headers and auto-sized column widths.

    This is a private helper — callers should use ``export_to_excel_bytes()``.

    Args:
        engine: SQLAlchemy Engine instance to query data from.

    Returns:
        An openpyxl Workbook ready to be serialised to bytes.
    """
    with Session(engine) as session:
        devices = session.execute(
            select(Device).order_by(Device.locker_slot, Device.name)
        ).scalars().all()

        transactions = session.execute(
            select(TransactionLog).order_by(TransactionLog.timestamp.desc())
        ).scalars().all()

        users = session.execute(
            select(User).order_by(User.display_name)
        ).scalars().all()

        wb = Workbook()

        # Shared header styling — bold white text on steel-blue background
        header_font = Font(bold=True)
        header_fill = PatternFill(
            start_color="D9E1F2", end_color="D9E1F2", fill_type="solid"
        )

        # --- Devices sheet ---------------------------------------------------
        ws = wb.active
        ws.title = "Devices"
        headers = [
            "PM Number", "Name", "Type", "Manufacturer", "Model",
            "Serial Number", "Locker Slot", "Status", "Tagged",
            "Current Borrower", "Description", "Calibration Due",
        ]
        ws.append(headers)
        for cell in ws[1]:
            cell.font = header_font
            cell.fill = header_fill

        for d in devices:
            borrower = ""
            if d.current_borrower is not None:
                borrower = d.current_borrower.display_name
            ws.append([
                d.pm_number,
                d.name,
                d.device_type,
                d.manufacturer,
                d.model,
                d.serial_number,
                d.locker_slot,
                d.status.value,
                "Yes" if d.tag_hmac else "No",
                borrower,
                d.description,
                d.calibration_due,
            ])

        _auto_width(ws)

        # --- Transactions sheet -----------------------------------------------
        ws2 = wb.create_sheet("Transactions")
        txn_headers = [
            "Date", "User", "Device", "Type", "Performed By", "Notes",
        ]
        ws2.append(txn_headers)
        for cell in ws2[1]:
            cell.font = header_font
            cell.fill = header_fill

        for t in transactions:
            user_name = t.user.display_name if t.user else ""
            device_name = t.device.name if t.device else ""
            admin_name = t.performed_by.display_name if t.performed_by else ""
            ws2.append([
                t.timestamp.strftime("%Y-%m-%d %H:%M:%S") if t.timestamp else "",
                user_name,
                device_name,
                t.transaction_type.value,
                admin_name,
                t.notes,
            ])

        _auto_width(ws2)

        # --- Users sheet ------------------------------------------------------
        ws3 = wb.create_sheet("Users")
        user_headers = [
            "ID", "Display Name", "Role", "Active", "Registered At",
        ]
        ws3.append(user_headers)
        for cell in ws3[1]:
            cell.font = header_font
            cell.fill = header_fill

        for u in users:
            ws3.append([
                u.id,
                u.display_name,
                u.role.value,
                "Yes" if u.is_active else "No",
                u.created_at.strftime("%Y-%m-%d %H:%M:%S") if u.created_at else "",
            ])

        _auto_width(ws3)

    return wb


def _auto_width(ws) -> None:
    """Auto-size every column in a worksheet based on cell content length.

    Iterates all columns, measures the longest string value, and sets
    the column width to that length plus padding (capped at 40
    characters to prevent excessively wide columns).

    Args:
        ws: An openpyxl Worksheet to resize.

    Returns:
        None. Column widths are modified in place.
    """
    for col in ws.columns:
        max_len = 0
        col_letter = col[0].column_letter
        for cell in col:
            val = str(cell.value) if cell.value is not None else ""
            max_len = max(max_len, len(val))
        # +3 for padding, capped at 40 to prevent overly wide columns
        ws.column_dimensions[col_letter].width = min(max_len + 3, 40)


def export_to_excel_bytes(engine) -> bytes:
    """Export current data as an in-memory Excel file (raw bytes).

    Builds the three-sheet workbook and serialises it to a ``BytesIO``
    buffer. The admin download endpoint returns those bytes as an HTTP
    response without writing to disk.

    Args:
        engine: SQLAlchemy Engine instance to query data from.

    Returns:
        Raw bytes of the ``.xlsx`` file, suitable for streaming in an
        HTTP response with content-type
        ``application/vnd.openxmlformats-officedocument.spreadsheetml.sheet``.
    """
    wb = _build_workbook(engine)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
