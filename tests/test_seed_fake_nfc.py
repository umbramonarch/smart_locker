"""
File: test_seed_fake_nfc.py
Description: Tests for the dev-only fake-NFC seed script: the
             SMART_LOCKER_FAKE_READER footgun guard, the seed UID constants
             matching the dev-tap panel constants in app.js, and idempotent
             enroll/bind against a temporary database.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_seed_fake_nfc.py -v
"""

import re
import sys
from pathlib import Path

import pytest

from scripts import seed_fake_nfc
from smart_locker.database.engine import get_session, init_db, reset_engine
from smart_locker.database.models import User
from smart_locker.database.repositories import DeviceRepository, UserRepository
from smart_locker.security.hashing import compute_uid_hmac

FRONTEND = Path(__file__).resolve().parents[1] / "smart_locker" / "frontend"

EXPECTED_CARD_UIDS = ["040000A1", "040000A2", "040000A3"]
EXPECTED_TAG_UIDS = ["050000B1", "050000B2", "050000B3"]

NAMES = ["Fake Admin", "Fake User 2", "Fake User 3"]
ROLES = ["admin", "user", "user"]
PMS = ["PM-001", "PM-002", "PM-003"]


def _panel_uids(name: str) -> list:
    """Extract a `const NAME = [...]` UID array from the initDevTap block."""
    js = (FRONTEND / "app.js").read_text(encoding="utf-8")
    block = js.split("(function initDevTap()", 1)[1]
    match = re.search(r"const %s = \[(.*?)\];" % name, block, re.DOTALL)
    assert match, f"{name} constant missing from the dev-tap block"
    return re.findall(r"'([0-9A-Fa-f]+)'", match.group(1))


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """Point the engine singleton at a throwaway SQLite file."""
    monkeypatch.setenv("SMART_LOCKER_FAKE_READER", "1")
    reset_engine()
    init_db(f"sqlite:///{tmp_path}/seed.db")
    yield
    reset_engine()


def _register_devices(pms=PMS):
    """Create registered locker devices for the given PMs."""
    with get_session() as session:
        for slot, pm in enumerate(pms, start=1):
            DeviceRepository.create(
                session,
                name=f"Device {pm}",
                device_type="Multimeter",
                pm_number=pm,
                locker_slot=slot,
            )


def _user_count() -> int:
    with get_session() as session:
        return len(UserRepository.list_all(session))


class TestFakeFlagGuard:
    """The script refuses to run unless the fake reader is enabled."""

    @pytest.mark.parametrize("value", [None, "", "0", "off", "nope"])
    def test_guard_refuses_without_truthy_flag(self, monkeypatch, capsys, value):
        """Unset or falsy SMART_LOCKER_FAKE_READER aborts before touching the DB."""
        monkeypatch.setattr(sys, "argv", ["seed_fake_nfc"])
        if value is None:
            monkeypatch.delenv("SMART_LOCKER_FAKE_READER", raising=False)
        else:
            monkeypatch.setenv("SMART_LOCKER_FAKE_READER", value)
        with pytest.raises(SystemExit) as exc:
            seed_fake_nfc.main()
        assert exc.value.code != 0
        assert "SMART_LOCKER_FAKE_READER" in capsys.readouterr().out


class TestUidConstantsMatchPanel:
    """Seed UIDs are verbatim the panel's preset UIDs (drift fails here)."""

    def test_card_uids_match_panel(self):
        """Seed card UIDs equal the panel's FAKE_CARD_UIDS and the pinned list."""
        assert seed_fake_nfc.FAKE_CARD_UIDS == _panel_uids("FAKE_CARD_UIDS")
        assert seed_fake_nfc.FAKE_CARD_UIDS == EXPECTED_CARD_UIDS

    def test_tag_uids_match_panel(self):
        """Seed tag UIDs equal the panel's FAKE_TAG_UIDS and the pinned list."""
        assert seed_fake_nfc.FAKE_TAG_UIDS == _panel_uids("FAKE_TAG_UIDS")
        assert seed_fake_nfc.FAKE_TAG_UIDS == EXPECTED_TAG_UIDS


class TestIdempotentSeed:
    """Enroll/bind against a tmp DB; reruns are no-op successes."""

    def test_seed_enrolls_users_and_binds_tags(self, tmp_db, hmac_key):
        """One run creates 3 users (first admin) and binds the 3 tags."""
        _register_devices()
        seed_fake_nfc.seed(NAMES, ROLES, PMS)
        with get_session() as session:
            for uid, name, role in zip(EXPECTED_CARD_UIDS, NAMES, ROLES):
                user = UserRepository.find_by_uid_hmac(
                    session, compute_uid_hmac(uid, hmac_key)
                )
                assert user is not None
                assert user.display_name == name
                assert user.role.value == role
            for uid, pm in zip(EXPECTED_TAG_UIDS, PMS):
                device = DeviceRepository.find_by_pm(session, pm)
                assert device.tag_hmac == compute_uid_hmac(uid, hmac_key)

    def test_rerun_is_noop_success(self, tmp_db, hmac_key, capsys):
        """A second identical run succeeds without duplicating anything."""
        _register_devices()
        seed_fake_nfc.seed(NAMES, ROLES, PMS)
        seed_fake_nfc.seed(NAMES, ROLES, PMS)  # must not raise
        assert _user_count() == 3
        out = capsys.readouterr().out
        assert "already enrolled" in out
        assert "already bound" in out
        with get_session() as session:
            for uid, pm in zip(EXPECTED_TAG_UIDS, PMS):
                device = DeviceRepository.find_by_pm(session, pm)
                assert device.tag_hmac == compute_uid_hmac(uid, hmac_key)

    def test_missing_pm_fails_without_creating_device(self, tmp_db):
        """An unregistered PM aborts with an error and creates no device row."""
        _register_devices(PMS[:2])  # PM-003 missing
        with pytest.raises(SystemExit) as exc:
            seed_fake_nfc.seed(NAMES, ROLES, PMS)
        assert exc.value.code != 0
        with get_session() as session:
            assert DeviceRepository.find_by_pm(session, "PM-003") is None
        # Users enroll first, so a later rerun (after Register Device) only binds.
        assert _user_count() == 3

    def test_different_existing_tag_refuses(self, tmp_db, hmac_key):
        """A device already carrying another tag keeps it; seed aborts loudly."""
        from smart_locker.auth.tap_router import bind_uid_to_device

        _register_devices()
        with get_session() as session:
            device = DeviceRepository.find_by_pm(session, "PM-001")
            bind_uid_to_device(session, device, "060000C1", hmac_key)
        with pytest.raises(SystemExit):
            seed_fake_nfc.seed(NAMES, ROLES, PMS)
        with get_session() as session:
            device = DeviceRepository.find_by_pm(session, "PM-001")
            assert device.tag_hmac == compute_uid_hmac("060000C1", hmac_key)

    def test_main_seeds_with_flag_and_tmp_db(self, tmp_db, monkeypatch):
        """End-to-end: main() with the flag set enrolls the default names/PMs."""
        monkeypatch.setattr(sys, "argv", ["seed_fake_nfc"])
        _register_devices()
        seed_fake_nfc.main()  # must not raise
        assert _user_count() == 3
        with get_session() as session:
            assert (
                session.query(User)
                .filter(User.display_name == "Fake Admin")
                .one_or_none()
                is not None
            )
