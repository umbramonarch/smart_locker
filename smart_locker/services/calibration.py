"""
File: calibration.py
Description: Calibration due-date state for the catalog. Pure date math: a
             badge ("due soon") inside the warning window, and a borrow block
             on the due date and after. A return is never blocked.
Project: smart_locker/services
Notes: ``Device.calibration_due`` is a ``date``; JSON rows carry an ISO
       string. Both are accepted — an unparseable value counts as no date.
"""

import logging
from datetime import date, datetime
from typing import Literal

from config.settings import calibration_warn_days

logger = logging.getLogger(__name__)

CalibrationState = Literal["ok", "due_soon", "due", "overdue"]


def _as_date(due: date | datetime | str | None) -> date | None:
    """Normalise a calibration due value to ``date``.

    Args:
        due: ``date``, ``datetime`` (``.date()`` applied), ISO string
            (``date.fromisoformat``), or None.

    Returns:
        The due date, or None when absent or unparseable.
    """
    if due is None:
        return None
    if isinstance(due, datetime):
        return due.date()
    if isinstance(due, date):
        return due
    if isinstance(due, str):
        text = due.strip()
        try:
            return date.fromisoformat(text)
        except ValueError:
            pass
        # Sheet-style strings (DD.MM.YYYY, DD/MM/YYYY) share the catalog
        # parser so a caller handing us sheet text does not fail open.
        from smart_locker.sync.catalog_sheet import parse_date

        return parse_date(text)
    return None


def days_left(
    due: date | datetime | str | None,
    today: date | None = None,
) -> int | None:
    """Days until the calibration date (negative when overdue).

    Args:
        due: Calibration due value (date/datetime/ISO string) or None.
        today: Reference date; defaults to ``date.today()``.

    Returns:
        ``(due - today).days``, or None when there is no usable date.
    """
    d = _as_date(due)
    if d is None:
        return None
    return (d - (today or date.today())).days


def calibration_state(
    due: date | datetime | str | None,
    today: date | None = None,
) -> CalibrationState | None:
    """Classify a calibration date against the warn window.

    Args:
        due: Calibration due value (date/datetime/ISO string) or None.
        today: Reference date; defaults to ``date.today()``.

    Returns:
        ``"overdue"`` when due < today, ``"due"`` when due == today,
        ``"due_soon"`` when due is within ``calibration_warn_days()``,
        ``"ok"`` beyond that, or None when there is no usable date.
    """
    n = days_left(due, today)
    if n is None:
        return None
    if n < 0:
        return "overdue"
    if n == 0:
        return "due"
    if n <= calibration_warn_days():
        return "due_soon"
    return "ok"


def borrow_block_reason(
    due: date | datetime | str | None,
    today: date | None = None,
) -> str | None:
    """The kiosk refusal clause when calibration blocks a borrow, else None.

    On the due date and after, a unit cannot start a new loan — borrow and
    handover are refused; return is unaffected.

    Args:
        due: Calibration due value (date/datetime/ISO string) or None.
        today: Reference date; defaults to ``date.today()``.

    Returns:
        A short reason clause, or None when the unit may be borrowed.
    """
    d = _as_date(due)
    if d is None:
        return None
    n = days_left(d, today)
    if n is None or n > 0:
        return None
    if n == 0:
        return "calibration due today"
    return f"calibration overdue (due {d.isoformat()})"


def calibration_fields(
    due: date | datetime | str | None,
    today: date | None = None,
) -> dict:
    """API payload fields for one device's calibration state.

    Args:
        due: Calibration due value (date/datetime/ISO string) or None.
        today: Reference date; defaults to ``date.today()``.

    Returns:
        dict: ``{"calibration_state": ..., "calibration_days_left": ...}``.
    """
    # One reference date for both fields — a request straddling midnight must
    # not pair a state computed before rollover with a days_left after it.
    today = today or date.today()
    return {
        "calibration_state": calibration_state(due, today),
        "calibration_days_left": days_left(due, today),
    }
