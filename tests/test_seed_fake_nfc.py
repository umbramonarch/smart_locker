"""
File: test_seed_fake_nfc.py
Description: scripts/seed_fake_nfc.py — dev-harness seed on a real temp-file
             SQLite database: preset card UIDs become users, preset tag UIDs
             bind to locker units, reruns are no-ops, and the script refuses
             to run unless SMART_LOCKER_FAKE_READER is enabled.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_seed_fake_nfc.py -v
"""

import sys

import pytest

import scripts.seed_fake_nfc as seed
from smart_locker.database.engine import (
    get_session,
    reset_engine,
)
from smart_locker.database.repositories import DeviceRepository, UserRepository
from smart_locker.nfc.fake_reader import FAKE_CARD_UIDS, FAKE_TAG_UIDS
from smart_locker.security.hashing import compute_uid_hmac
from smart_locker.security.key_manager import key_manager


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """A real temp-file SQLite DB; seed's init_db()/get_session() land here.

    The engine is a module-level singleton — pointing ``DATABASE_URL`` at the
    temp file and resetting between tests keeps the script's writes visible.
    """
    monkeypatch.setattr(
        "smart_locker.database.engine.DATABASE_URL",
        f"sqlite:///{tmp_path.as_posix()}/seed.db",
    )
    reset_engine()
    from smart_locker.database.engine import init_db

    init_db()
    yield tmp_path
    reset_engine()


@pytest.fixture()
def argv(monkeypatch):
    """Run seed.main() with a clean argv."""

    def _set(*args: str) -> None:
        monkeypatch.setattr(sys, "argv", ["seed_fake_nfc.py", *args])

    return _set


def _locker_unit(pm: str, slot: int) -> None:
    with get_session() as session:
        DeviceRepository.create(
            session, name=f"Meter {pm}", device_type="Tool",
            pm_number=pm, locker_slot=slot,
        )


def _users() -> list:
    with get_session() as session:
        return UserRepository.list_all(session)


def _tag_owner(uid: str):
    with get_session() as session:
        return DeviceRepository.find_by_tag_hmac(
            session, compute_uid_hmac(uid, key_manager.hmac_key)
        )


def test_seed_refuses_without_fake_reader(db, argv, monkeypatch):
    """The seed is dev-only — no SMART_LOCKER_FAKE_READER, no writes."""
    monkeypatch.delenv("SMART_LOCKER_FAKE_READER", raising=False)
    argv()
    with pytest.raises(SystemExit):
        seed.main()
    assert _users() == []


def test_seed_enrolls_preset_users(db, argv, monkeypatch):
    """Each preset card UID enrolls a user with the seeded name/role."""
    monkeypatch.setenv("SMART_LOCKER_FAKE_READER", "1")
    argv()
    seed.main()

    with get_session() as session:
        for uid, (name, role) in zip(FAKE_CARD_UIDS, seed.PRESET_PEOPLE):
            user = UserRepository.find_by_uid_hmac(
                session, compute_uid_hmac(uid, key_manager.hmac_key)
            )
            assert user is not None
            assert user.display_name == name
            assert user.role.value == role


def test_seed_binds_preset_tags_by_slot_order(db, argv, monkeypatch):
    """Without --pm, tag UIDs bind to the first untagged locker units."""
    monkeypatch.setenv("SMART_LOCKER_FAKE_READER", "1")
    _locker_unit("PM-101", 2)
    _locker_unit("PM-102", 1)  # earlier slot wins despite later creation
    argv()
    seed.main()

    assert _tag_owner(FAKE_TAG_UIDS[0]).pm_number == "PM-102"
    assert _tag_owner(FAKE_TAG_UIDS[1]).pm_number == "PM-101"
    # Only two locker units exist — the third preset tag stays unbound.
    assert _tag_owner(FAKE_TAG_UIDS[2]) is None


def test_seed_binds_tags_to_named_pms(db, argv, monkeypatch):
    """--pm targets specific locker units, in flag order."""
    monkeypatch.setenv("SMART_LOCKER_FAKE_READER", "1")
    _locker_unit("PM-201", 5)
    _locker_unit("PM-202", 6)
    argv("--pm", "PM-201", "--pm", "PM-202")
    seed.main()

    assert _tag_owner(FAKE_TAG_UIDS[0]).pm_number == "PM-201"
    assert _tag_owner(FAKE_TAG_UIDS[1]).pm_number == "PM-202"


def test_seed_is_idempotent(db, argv, monkeypatch):
    """A second run enrolls nothing twice and rebinds nothing."""
    monkeypatch.setenv("SMART_LOCKER_FAKE_READER", "1")
    _locker_unit("PM-301", 1)
    argv()
    seed.main()
    first_users = len(_users())
    first_tag = _tag_owner(FAKE_TAG_UIDS[0]).pm_number

    seed.main()
    assert len(_users()) == first_users
    assert _tag_owner(FAKE_TAG_UIDS[0]).pm_number == first_tag


def test_seed_unknown_pm_errors_but_keeps_going(db, argv, monkeypatch, capsys):
    """A bad --pm reports and exits nonzero; the valid PM still gets the
    first free preset tag (bad entries don't leave a hole)."""
    monkeypatch.setenv("SMART_LOCKER_FAKE_READER", "1")
    _locker_unit("PM-401", 1)
    argv("--pm", "PM-MISSING", "--pm", "PM-401")
    with pytest.raises(SystemExit):
        seed.main()

    assert _tag_owner(FAKE_TAG_UIDS[0]).pm_number == "PM-401"
    assert "No device with PM 'PM-MISSING'" in capsys.readouterr().out
