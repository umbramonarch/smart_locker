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


def test_seed_rerun_binds_only_unbound_tags(db, argv, monkeypatch):
    """A rerun after a new unit registers pairs only the still-free preset
    UID with it — live bindings are never re-paired (that used to exit 1)."""
    monkeypatch.setenv("SMART_LOCKER_FAKE_READER", "1")
    _locker_unit("PM-501", 1)
    _locker_unit("PM-502", 2)
    argv()
    seed.main()
    assert _tag_owner(FAKE_TAG_UIDS[0]).pm_number == "PM-501"
    assert _tag_owner(FAKE_TAG_UIDS[1]).pm_number == "PM-502"
    assert _tag_owner(FAKE_TAG_UIDS[2]) is None

    _locker_unit("PM-503", 3)
    seed.main()  # exits 0 — no SystemExit

    assert _tag_owner(FAKE_TAG_UIDS[0]).pm_number == "PM-501"
    assert _tag_owner(FAKE_TAG_UIDS[1]).pm_number == "PM-502"
    assert _tag_owner(FAKE_TAG_UIDS[2]).pm_number == "PM-503"


def test_seed_pm_never_clobbers_a_different_tag(db, argv, monkeypatch):
    """--pm onto a unit already carrying a non-preset sticker errors and
    leaves the existing binding untouched (it used to silently overwrite)."""
    monkeypatch.setenv("SMART_LOCKER_FAKE_READER", "1")
    _locker_unit("PM-601", 1)
    with get_session() as session:
        device = DeviceRepository.find_by_pm(session, "PM-601")
        DeviceRepository.bind_tag(
            session,
            device,
            compute_uid_hmac("AABBCCDD", key_manager.hmac_key),
        )
    argv("--pm", "PM-601")
    with pytest.raises(SystemExit):
        seed.main()

    assert _tag_owner("AABBCCDD").pm_number == "PM-601"
    assert _tag_owner(FAKE_TAG_UIDS[0]) is None


def test_seed_duplicate_pm_keeps_first_preset_tag(db, argv, monkeypatch):
    """--pm PM-x --pm PM-x binds the first preset UID; the second pair
    errors instead of silently rebinding the row to the next UID."""
    monkeypatch.setenv("SMART_LOCKER_FAKE_READER", "1")
    _locker_unit("PM-701", 1)
    argv("--pm", "PM-701", "--pm", "PM-701")
    with pytest.raises(SystemExit):
        seed.main()

    assert _tag_owner(FAKE_TAG_UIDS[0]).pm_number == "PM-701"
    assert _tag_owner(FAKE_TAG_UIDS[1]) is None


def test_seed_notes_preset_tags_left_unbound(db, argv, monkeypatch, capsys):
    """Fewer untagged units than free preset UIDs: the leftover stickers
    get a NOTE instead of silently never binding."""
    monkeypatch.setenv("SMART_LOCKER_FAKE_READER", "1")
    _locker_unit("PM-801", 1)
    argv()
    seed.main()

    out = capsys.readouterr().out
    assert "NOTE" in out
    assert "2 preset sticker(s) stay unbound" in out


def test_seed_notes_stale_rows_after_hmac_rotation(
    db, argv, monkeypatch, capsys
):
    """After an HMAC key rotation the old digests miss: new rows enroll
    under the same names, each flagged with a stale-row NOTE."""
    monkeypatch.setenv("SMART_LOCKER_FAKE_READER", "1")
    argv()
    seed.main()
    first_count = len(_users())

    class _RotatedKeys:
        enc_key = b"\x01" * 32
        hmac_key = b"\x09" * 32

    # The real key_manager caches keys process-wide, so a rotation is
    # simulated by stubbing the seed module's handle.
    monkeypatch.setattr(seed, "key_manager", _RotatedKeys())
    seed.main()

    assert len(_users()) == first_count * 2
    out = capsys.readouterr().out
    assert out.count("stale row with this name exists") == len(seed.PRESET_PEOPLE)


def test_seed_missing_keys_exit_with_error_not_traceback(
    db, argv, monkeypatch, capsys
):
    """Unset keys exit 1 with an ERROR line, not an uncaught traceback."""
    monkeypatch.setenv("SMART_LOCKER_FAKE_READER", "1")
    argv()

    class _MissingKeys:
        @property
        def enc_key(self):
            raise EnvironmentError(
                "Missing environment variable 'SMART_LOCKER_ENC_KEY'."
            )

        @property
        def hmac_key(self):
            raise EnvironmentError(
                "Missing environment variable 'SMART_LOCKER_HMAC_KEY'."
            )

    # The real key_manager caches keys process-wide, so deleting the env
    # vars cannot reliably trigger the failure — stub the handle instead.
    monkeypatch.setattr(seed, "key_manager", _MissingKeys())
    with pytest.raises(SystemExit):
        seed.main()
    assert "ERROR:" in capsys.readouterr().out
