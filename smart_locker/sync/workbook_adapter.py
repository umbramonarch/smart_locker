"""
File: workbook_adapter.py
Description: Workbook port for copied reads and atomic active-sheet edits.
Project: smart_locker/sync
Notes: Keeps openpyxl and workbook filesystem handling out of sync policy.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from zipfile import BadZipFile

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

WRITE_RETRY_ATTEMPTS = 3
_RETRY_DELAY_SECONDS = 1.0


class WorkbookStaleError(Exception):
    """The source workbook changed after its edit copy was made."""


@dataclass(frozen=True)
class WorkbookRows:
    """Rows read from one workbook sheet, or the reason they were unavailable."""

    rows: list | None
    error: str | None = None


class WorkbookAdapter:
    """Open, read, and edit one xlsx workbook without catalog policy."""

    def __init__(self, path: str | Path) -> None:
        """Store the workbook path.

        Args:
            path: Source ``.xlsx`` file.
        """
        self.path = Path(path)

    def read_rows(self, sheet_name: str | None = None) -> WorkbookRows:
        """Read values from a copied workbook so an open share file can be read.

        Args:
            sheet_name: Sheet to read, or None for the active sheet.

        Returns:
            WorkbookRows with all cell values or an existing user-facing error.
        """
        if not self.path.exists():
            return WorkbookRows(None, f"File not found: {self.path}")

        try:
            with self._copied_workbook() as work_path:
                try:
                    wb = load_workbook(work_path, read_only=True, data_only=True)
                    try:
                        if sheet_name:
                            if sheet_name not in wb.sheetnames:
                                return WorkbookRows(
                                    None,
                                    f"Sheet '{sheet_name}' not found. Available: {wb.sheetnames}",
                                )
                            ws = wb[sheet_name]
                        else:
                            ws = wb.active
                        return WorkbookRows(list(ws.iter_rows(values_only=True)))
                    finally:
                        wb.close()
                except (OSError, BadZipFile, InvalidFileException) as exc:
                    return WorkbookRows(None, f"Source workbook unreadable: {exc}")
        except PermissionError:
            return WorkbookRows(None, f"Source file locked: {self.path}")
        except OSError as exc:
            return WorkbookRows(None, f"Source file unavailable: {self.path} ({exc})")

    def edit_active_sheet(self, edit: Callable[[object], bool]) -> bool:
        """Copy, edit, and atomically replace the workbook when ``edit`` changes it.

        Args:
            edit: Receives the active worksheet and returns True when it changed.

        Returns:
            True when a changed workbook was saved and replaced; False when
            ``edit`` made no changes.

        Raises:
            WorkbookStaleError: The source changed after it was copied.
            OSError: The workbook could not be copied, loaded, saved, or replaced.
        """
        mtime = self.path.stat().st_mtime
        dest_path: Path | None = None
        wb = None
        try:
            with self._copied_workbook() as work_path:
                wb = load_workbook(work_path)
                if not edit(wb.active):
                    return False

                dest_fd, dest_str = tempfile.mkstemp(
                    suffix=".xlsx", dir=self.path.parent
                )
                os.close(dest_fd)
                dest_path = Path(dest_str)
                wb.save(dest_path)
                wb.close()
                wb = None
                try:
                    if self.path.stat().st_mtime != mtime:
                        raise WorkbookStaleError()
                except FileNotFoundError:
                    raise WorkbookStaleError() from None
                self._replace_into(dest_path)
                dest_path = None
                return True
        finally:
            if wb is not None:
                try:
                    wb.close()
                except Exception:
                    pass
            if dest_path is not None:
                dest_path.unlink(missing_ok=True)

    def _replace_into(self, staged_path: Path) -> None:
        """Replace the source workbook with a staged file, retrying a file lock."""
        for attempt in range(1, WRITE_RETRY_ATTEMPTS + 1):
            try:
                staged_path.replace(self.path)
                return
            except PermissionError:
                if attempt == WRITE_RETRY_ATTEMPTS:
                    raise
                time.sleep(_RETRY_DELAY_SECONDS)

    @contextmanager
    def _copied_workbook(self):
        """Yield a temporary copy of the source workbook and delete it afterward."""
        work_fd, work_str = tempfile.mkstemp(suffix=".xlsx")
        os.close(work_fd)
        work_path = Path(work_str)
        try:
            shutil.copy2(self.path, work_path)
            yield work_path
        finally:
            work_path.unlink(missing_ok=True)


def configured_workbook() -> WorkbookAdapter | None:
    """Build an adapter for the currently configured source workbook.

    The setting is deliberately read when a boundary starts workbook work,
    rather than when this module is imported. This preserves live configuration
    selection and lets tests point it at a temporary workbook.

    Returns:
        Adapter for the configured workbook, or None when no source is set.
    """
    import config.settings as settings

    path = settings.SOURCE_EXCEL_PATH
    return WorkbookAdapter(path) if path else None
