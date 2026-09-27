"""
File: test_update_device.py
Description: scripts/update_device.py ``calibration_due`` handling — ISO and
             day-first strings land in the Date column as real ``date``
             objects, an unparseable value is skipped with a printed SKIP
             line, a blank value clears the date, and the Device column
             validator rejects a garbage string at assignment time.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_update_device.py -v
       ``_update_one`` is called directly so the mirror is only flagged dirty
       (``mark_dirty`` writes the conftest-isolated mirror_state.json) — the
       workbook tick in ``update_device()``/``_flush_mirror()`` is not run.
"""

from datetime import date

import pytest

import scripts.update_device as update_script
from smart_locker.database.engine import get_session, init_db, reset_engine
from smart_locker.database.models import Device
from smart_locker.database.repositories import DeviceRepository


@pytest.fixture()
def db(tmp_path):
    """A real temp-file SQLite DB.

    The engine is a module-level singleton: initializing it here with the
    temp-file URL means ``_update_one``'s own ``init_db()``/``get_session()``
    calls reuse this same engine.
    """
    reset_engine()
    init_db(f"sqlite:///{tmp_path.as_posix()}/t.db")
    yield
    reset_engine()


def _mk_device(pm="PM-X", cal=None):
    """Seed one catalog device through the repository."""
    with get_session() as session:
        DeviceRepository.create(
            session,
            name=f"Meter {pm}",
            device_type="Tool",
            pm_number=pm,
            locker_slot=1,
            calibration_due=cal,
        )


def _calibration_due(pm="PM-X"):
    """Re-read the device fresh from the DB and return its calibration_due."""
    with get_session() as session:
        device = DeviceRepository.find_by_pm(session, pm)
        return device.calibration_due


def test_iso_date_string_sets_calibration_due(db):
    """--field calibration_due --value 2026-09-27 stores a real date."""
    _mk_device()

    assert (
        update_script._update_one("PM-X", {"calibration_due": "2026-09-27"})
        is True
    )

    stored = _calibration_due()
    assert type(stored) is date  # a date, not the raw string
    assert stored == date(2026, 9, 27)


def test_day_first_date_string_sets_calibration_due(db):
    """Sheet-style '27.09.2026' parses to the same date."""
    _mk_device()

    assert (
        update_script._update_one("PM-X", {"calibration_due": "27.09.2026"})
        is True
    )

    assert _calibration_due() == date(2026, 9, 27)


def test_garbage_date_value_is_skipped(db, capsys):
    """An unparseable value prints SKIP and leaves the stored date alone."""
    _mk_device(cal=date(2030, 1, 15))

    assert (
        update_script._update_one("PM-X", {"calibration_due": "not a date"})
        is True
    )

    out = capsys.readouterr().out
    assert "SKIP" in out
    assert "not a date" in out
    assert _calibration_due() == date(2030, 1, 15)


def test_blank_date_value_clears_calibration_due(db):
    """A blank value clears the date instead of erroring."""
    _mk_device(cal=date(2030, 1, 15))

    assert (
        update_script._update_one("PM-X", {"calibration_due": "   "}) is True
    )

    assert _calibration_due() is None


class TestCalibrationDueValidator:
    """The Device validator is the guard behind the script's pre-parse."""

    def test_constructor_rejects_garbage_string(self):
        """Device(calibration_due='bogus') raises instead of reaching Date."""
        with pytest.raises(ValueError, match="calibration_due must be a date"):
            Device(calibration_due="bogus")

    def test_assignment_rejects_garbage_string(self):
        """setattr of an unparseable string raises the same ValueError."""
        device = Device(pm_number="PM-V", name="V", device_type="Tool")
        with pytest.raises(ValueError, match="calibration_due must be a date"):
            device.calibration_due = "bogus"

    def test_validator_accepts_date_and_blank(self):
        """The validator still lets through real dates and blank clears."""
        device = Device(
            pm_number="PM-W",
            name="W",
            device_type="Tool",
            calibration_due="2026-09-27",
        )
        assert device.calibration_due == date(2026, 9, 27)
        device.calibration_due = ""
        assert device.calibration_due is None
