"""
File: calibration.py
Description: Calibration due-soon / overdue state for kiosk and dashboard
             alerts. Pure date math — warn only, never blocks borrowing.
             Fix the date in Excel + Sync to clear an alert.
Project: smart_locker/services
Notes: ``Device.calibration_due`` is a ``date``; dashboard inventory rows carry
       an ISO string. Both are accepted (bad strings tolerated as no date).
"""

import logging
from datetime import date, datetime
from typing import Literal

logger = logging.getLogger(__name__)

CalibrationState = Literal["ok", "due_soon", "overdue"]


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
        try:
            return date.fromisoformat(due.strip())
        except ValueError:
            return None
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
    warn_days: int,
    today: date | None = None,
) -> CalibrationState | None:
    """Classify a calibration date against ``warn_days``.

    Args:
        due: Calibration due value (date/datetime/ISO string) or None.
        warn_days: Days ahead of the due date to flag "due_soon"
            (0 = only today counts).
        today: Reference date; defaults to ``date.today()``.

    Returns:
        ``"overdue"`` when due < today, ``"due_soon"`` when
        ``today <= due <= today + warn_days``, ``"ok"`` beyond that,
        or None when there is no usable date.
    """
    n = days_left(due, today)
    if n is None:
        return None
    if n < 0:
        return "overdue"
    if n <= warn_days:
        return "due_soon"
    return "ok"


def calibration_fields(
    due: date | datetime | str | None,
    warn_days: int,
    today: date | None = None,
) -> dict:
    """API payload fields for one device's calibration state.

    Args:
        due: Calibration due value (date/datetime/ISO string) or None.
        warn_days: Due-soon window in days.
        today: Reference date; defaults to ``date.today()``.

    Returns:
        dict: ``{"calibration_state": ..., "calibration_days_left": ...}``.
    """
    return {
        "calibration_state": calibration_state(due, warn_days, today),
        "calibration_days_left": days_left(due, today),
    }
