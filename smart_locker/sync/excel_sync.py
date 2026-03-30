"""
File: excel_sync.py
Description: Automatic Excel sync — exports device, transaction, and user data
             on every database change. Registers SQLAlchemy after_flush and
             after_commit event listeners so the Excel file stays in sync
             without manual intervention.
Project: smart_locker/sync
Notes: If the output file is locked (open in Excel), the export writes to a
       temporary file in the same directory, then attempts to replace the
       target. If the target is still locked, the pending file is kept and
       the next export attempt will try again. Three sheets are exported:
       Devices, Transactions, and Users.
"""

import logging
import tempfile
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from smart_locker.database.models import Device, TransactionLog, User

logger = logging.getLogger(__name__)

# Module-level references set by register_auto_sync() — used by the event
# listeners to call export_to_excel() without passing engine/path each time
_engine_ref = None          # SQLAlchemy Engine instance
_sync_path: Path | None = None  # Absolute path to the output Excel file


def export_to_excel(engine=None, output_path: str | Path | None = None) -> None:
    """Export current device, transaction, and user data to an Excel file.

    Generates a three-sheet workbook (Devices, Transactions, Users) with
    styled headers and auto-sized columns. Can be called standalone with
    explicit engine/path, or uses the module-level references set by
    ``register_auto_sync()``.

    Args:
        engine: SQLAlchemy Engine to query data from. Falls back to the
                module-level ``_engine_ref`` if None.
        output_path: Destination file path. Falls back to the module-level
                     ``_sync_path`` if None.

    Returns:
        None. The Excel file is written to disk.
    """
    eng = engine or _engine_ref
    path = Path(output_path) if output_path else _sync_path
    if eng is None or path is None:
        return

    with Session(eng) as session:
        devices = session.execute(
            select(Device).order_by(Device.locker_slot, Device.name)
        ).scalars().all()

        transactions = session.execute(
            select(TransactionLog).order_by(TransactionLog.timestamp.desc())
        ).scalars().all()

        wb = Workbook()

        # --- Devices sheet ---
        ws = wb.active
        ws.title = "Devices"
        headers = [
            "PM Number", "Name", "Type", "Manufacturer", "Model",
            "Serial Number", "Barcode", "Locker Slot", "Status",
            "Current Borrower", "Description", "Calibration Due",
        ]
        ws.append(headers)

        # Header styling
        header_font = Font(bold=True)
        header_fill = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
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
                d.barcode,
                d.locker_slot,
                d.status.value,
                borrower,
                d.description,
                d.calibration_due,
            ])

        # Auto-width columns
        for col in ws.columns:
            max_len = 0
            col_letter = col[0].column_letter
            for cell in col:
                val = str(cell.value) if cell.value is not None else ""
                max_len = max(max_len, len(val))
            # +3 for padding, capped at 40 to prevent overly wide columns
            ws.column_dimensions[col_letter].width = min(max_len + 3, 40)

        # --- Transactions sheet ---
        ws2 = wb.create_sheet("Transactions")
        txn_headers = ["Date", "User", "Device", "Type", "Performed By", "Notes"]
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

        for col in ws2.columns:
            max_len = 0
            col_letter = col[0].column_letter
            for cell in col:
                val = str(cell.value) if cell.value is not None else ""
                max_len = max(max_len, len(val))
            ws2.column_dimensions[col_letter].width = min(max_len + 3, 40)

        # --- Users sheet ---
        users = session.execute(
            select(User).order_by(User.display_name)
        ).scalars().all()

        ws3 = wb.create_sheet("Users")
        user_headers = ["ID", "Display Name", "Role", "Active", "Registered At"]
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

        for col in ws3.columns:
            max_len = 0
            col_letter = col[0].column_letter
            for cell in col:
                val = str(cell.value) if cell.value is not None else ""
                max_len = max(max_len, len(val))
            ws3.column_dimensions[col_letter].width = min(max_len + 3, 40)

        # Write to a temp file first, then replace the target. This avoids
        # partial writes and works around Windows file locking when the
        # output file is open in Excel.
        tmp_fd, tmp_path_str = tempfile.mkstemp(
            suffix=".xlsx", dir=path.parent
        )
        import os
        os.close(tmp_fd)
        tmp_path = Path(tmp_path_str)

        try:
            wb.save(tmp_path)
            # Attempt atomic replace of the target file
            tmp_path.replace(path)
            logger.debug(
                "Excel sync: %s updated (%d devices, %d transactions)",
                path, len(devices), len(transactions),
            )
        except PermissionError:
            # Target is locked by Excel — keep the temp file so the data
            # is preserved on disk. The next export will overwrite it.
            logger.warning(
                "Excel sync: %s is locked (open in Excel). "
                "Latest data written to %s instead.",
                path, tmp_path,
            )


def register_auto_sync(engine, output_path: str | Path) -> None:
    """Register SQLAlchemy event listeners for automatic Excel export.

    After any commit that touches Device or TransactionLog rows, the Excel
    file is regenerated. Also performs an initial export immediately.
    """
    global _engine_ref, _sync_path
    _engine_ref = engine
    _sync_path = Path(output_path)

    @event.listens_for(Session, "after_flush")
    def _mark_sync_needed(session, flush_context):
        """Flag the session if any relevant model was modified.

        Args:
            session: The SQLAlchemy session that just flushed.
            flush_context: Flush context provided by the event system.

        Returns:
            None. Sets ``session.info["_excel_sync"]`` to True if relevant.
        """
        for obj in list(session.new) + list(session.dirty) + list(session.deleted):
            if isinstance(obj, (Device, TransactionLog, User)):
                session.info["_excel_sync"] = True
                return

    @event.listens_for(Session, "after_commit")
    def _sync_on_commit(session):
        """Export to Excel if the commit included relevant changes.

        Args:
            session: The SQLAlchemy session that just committed.

        Returns:
            None. Triggers ``export_to_excel()`` if flagged by ``_mark_sync_needed``.
        """
        if session.info.pop("_excel_sync", False):
            try:
                export_to_excel()
            except Exception as e:
                logger.warning("Excel sync failed after commit: %s", e)

    # Initial export so the file exists on startup
    try:
        export_to_excel()
        logger.info("Excel sync registered: %s", _sync_path)
    except Exception as e:
        logger.warning("Excel sync initial export failed: %s", e)
