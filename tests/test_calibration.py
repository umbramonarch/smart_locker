"""
File: test_calibration.py
Description: Tests for smart_locker/services/calibration.py — due-soon /
             overdue state boundaries, days_left, ISO-string tolerance, and
             the SMART_LOCKER_CALIBRATION_WARN_DAYS env parsing semantics.
Project: smart_locker/tests
Notes: Warn-only alerts; borrowing is never blocked. Fixed ``today`` is
       2026-09-14 so boundaries are deterministic.
"""

from datetime import date, datetime

import pytest

from smart_locker.services.calibration import (
    calibration_fields,
    calibration_state,
    days_left,
)

TODAY = date(2026, 9, 14)
WARN = 14


class TestDaysLeft:
    """days_left = (due - today).days; None without a date."""

    def test_no_date_is_none(self):
        """Missing calibration date yields None."""
        assert days_left(None, today=TODAY) is None

    def test_past_is_negative(self):
        """Yesterday is -1."""
        assert days_left(date(2026, 9, 13), today=TODAY) == -1

    def test_today_is_zero(self):
        """Today is 0."""
        assert days_left(TODAY, today=TODAY) == 0

    def test_future_positive(self):
        """2026-09-29 is 15 days out."""
        assert days_left(date(2026, 9, 29), today=TODAY) == 15

    def test_datetime_input(self):
        """datetime inputs are accepted via .date()."""
        assert days_left(datetime(2026, 9, 20, 12, 30), today=TODAY) == 6

    def test_iso_string_input(self):
        """ISO strings parse; bad strings are tolerated as None."""
        assert days_left("2026-09-20", today=TODAY) == 6
        assert days_left("not-a-date", today=TODAY) is None
        assert days_left("", today=TODAY) is None


class TestCalibrationState:
    """overdue < today; due_soon within warn_days (0 allowed); else ok."""

    def test_no_date_is_none(self):
        """No calibration date → no state."""
        assert calibration_state(None, WARN, today=TODAY) is None

    def test_overdue(self):
        """2026-09-13 (yesterday) is overdue."""
        assert calibration_state(date(2026, 9, 13), WARN, today=TODAY) == "overdue"

    def test_today_is_due_soon(self):
        """2026-09-14 (today) is due_soon, not overdue."""
        assert calibration_state(TODAY, WARN, today=TODAY) == "due_soon"

    def test_warn_boundary_is_due_soon(self):
        """2026-09-28 (today + 14) is still due_soon."""
        assert calibration_state(date(2026, 9, 28), WARN, today=TODAY) == "due_soon"

    def test_past_warn_boundary_is_ok(self):
        """2026-09-29 (today + 15) is ok."""
        assert calibration_state(date(2026, 9, 29), WARN, today=TODAY) == "ok"

    def test_warn_days_zero(self):
        """warn_days=0 flags only today."""
        assert calibration_state(TODAY, 0, today=TODAY) == "due_soon"
        assert calibration_state(date(2026, 9, 15), 0, today=TODAY) == "ok"

    def test_fields_shape(self):
        """calibration_fields returns the two API keys."""
        fields = calibration_fields(date(2026, 9, 20), WARN, today=TODAY)
        assert fields == {
            "calibration_state": "due_soon",
            "calibration_days_left": 6,
        }
        assert calibration_fields(None, WARN, today=TODAY) == {
            "calibration_state": None,
            "calibration_days_left": None,
        }


class TestWarnDaysEnv:
    """SMART_LOCKER_CALIBRATION_WARN_DAYS parsing matches _env_int semantics."""

    def test_default_and_clamp(self, monkeypatch):
        """Unset → 14; 0 accepted; negative clamps to 0; bad value → 14."""
        from config.settings import _env_int

        monkeypatch.delenv("SMART_LOCKER_CALIBRATION_WARN_DAYS", raising=False)
        assert _env_int("SMART_LOCKER_CALIBRATION_WARN_DAYS", 14, minimum=0) == 14
        monkeypatch.setenv("SMART_LOCKER_CALIBRATION_WARN_DAYS", "0")
        assert _env_int("SMART_LOCKER_CALIBRATION_WARN_DAYS", 14, minimum=0) == 0
        monkeypatch.setenv("SMART_LOCKER_CALIBRATION_WARN_DAYS", "-3")
        assert _env_int("SMART_LOCKER_CALIBRATION_WARN_DAYS", 14, minimum=0) == 0
        monkeypatch.setenv("SMART_LOCKER_CALIBRATION_WARN_DAYS", "soon")
        assert _env_int("SMART_LOCKER_CALIBRATION_WARN_DAYS", 14, minimum=0) == 14
