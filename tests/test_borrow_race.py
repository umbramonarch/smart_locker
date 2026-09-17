"""
File: test_borrow_race.py
Description: Deterministic borrow-vs-borrow and transfer-vs-transfer race
             tests for the identity-map staleness fix. Reproduces the MR 27
             adversarial finding: a route pre-read pins the Device row in the
             session identity map, so the in-lock guard re-read saw stale
             AVAILABLE status and a second borrow overwrote the first holder.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_borrow_race.py -v -p no:cacheprovider
       Uses a throwaway file DB (WAL) so two sessions see committed state.
       Determinism via threading.Events only — no sleeps.
"""

import threading

import pytest
from sqlalchemy import select

from smart_locker.auth.session_manager import SessionManager
from smart_locker.database.engine import (
    get_session_factory,
    init_db,
    reset_engine,
)
from smart_locker.database.models import Device, TransactionLog, TransactionType, User
from smart_locker.database.repositories import DeviceRepository, UserRepository
from smart_locker.services.locker_service import LockerService


@pytest.fixture()
def race_db(tmp_path):
    """File-backed DB with two users and one available device.

    Yields (factory, u1_id, u2_id, u3_id, dev_id). Each racer uses its own
    session so identity maps are per-thread, as in production requests.
    """
    reset_engine()
    url = f"sqlite:///{tmp_path}/race.db"
    init_db(url)
    factory = get_session_factory(url)
    setup = factory()
    u1 = UserRepository.create(
        setup, display_name="U1", uid_hmac="h-u1", encrypted_card_uid="e-u1"
    )
    u2 = UserRepository.create(
        setup, display_name="U2", uid_hmac="h-u2", encrypted_card_uid="e-u2"
    )
    u3 = UserRepository.create(
        setup, display_name="U3", uid_hmac="h-u3", encrypted_card_uid="e-u3"
    )
    dev = DeviceRepository.create(
        setup, name="Meter", device_type="Meter", pm_number="PM-RACE"
    )
    DeviceRepository.bind_tag(setup, dev, "tag-race")
    setup.commit()
    ids = (u1.id, u2.id, u3.id, dev.id)
    setup.close()
    yield (factory, *ids)
    reset_engine()


def _session_for(factory, user_id):
    """Start a kiosk session for a user, like the NFC login path does."""
    mgr = SessionManager(timeout_seconds=600)
    return mgr.start_session(factory().get(User, user_id))


def test_borrow_vs_borrow_loser_refused(race_db):
    """A loser that pre-read AVAILABLE must be refused after the winner commits.

    Interleaving: loser pre-reads (pins AVAILABLE), winner borrows + commits,
    loser attempts. Without the in-lock expire_all, the loser's guard re-read
    is an identity-map hit (no SQL) and steals the borrow.
    """
    factory, u1_id, u2_id, _u3_id, dev_id = race_db
    winner_session = _session_for(factory, u1_id)
    loser_session = _session_for(factory, u2_id)
    db_loser = factory()

    preread_done = threading.Event()
    winner_done = threading.Event()
    result = {}

    def loser():
        device = DeviceRepository.find_by_id(db_loser, dev_id)  # route pre-read
        assert device is not None
        preread_done.set()
        assert winner_done.wait(timeout=15)
        result["loser_ok"] = LockerService.borrow_device(
            db_loser, loser_session, dev_id
        )

    thread = threading.Thread(target=loser)
    thread.start()
    assert preread_done.wait(timeout=15)

    db_winner = factory()
    DeviceRepository.find_by_id(db_winner, dev_id)  # route pre-read
    result["winner_ok"] = LockerService.borrow_device(
        db_winner, winner_session, dev_id
    )
    winner_done.set()
    thread.join(timeout=15)
    assert not thread.is_alive()

    assert result["winner_ok"] is True
    assert result["loser_ok"] is False

    check = factory()
    try:
        holder = check.get(Device, dev_id).current_borrower_id
        borrows = (
            check.execute(
                select(TransactionLog).where(
                    TransactionLog.transaction_type == TransactionType.BORROW
                )
            )
            .scalars()
            .all()
        )
    finally:
        check.close()
    assert holder == u1_id
    assert len(borrows) == 1
    assert borrows[0].user_id == u1_id


def test_transfer_vs_transfer_credits_fresh_holder(race_db):
    """A stale pre-read must not credit the transfer return to the old holder.

    Device borrowed by U1. Loser (U3) pre-reads (pins holder U1), winner (U2)
    transfers + commits, loser transfers. The loser's transfer is legitimate
    against fresh state, but its return row must credit U2 — without the
    in-lock expire_all it credits stale U1, corrupting the audit trail.
    """
    factory, u1_id, u2_id, u3_id, dev_id = race_db

    setup = factory()
    DeviceRepository.find_by_id(setup, dev_id)
    assert LockerService.borrow_device(setup, _session_for(factory, u1_id), dev_id)
    setup.close()

    winner_session = _session_for(factory, u2_id)
    loser_session = _session_for(factory, u3_id)
    db_loser = factory()

    preread_done = threading.Event()
    winner_done = threading.Event()
    result = {}

    def loser():
        device = DeviceRepository.find_by_id(db_loser, dev_id)  # route pre-read
        assert device is not None
        preread_done.set()
        assert winner_done.wait(timeout=15)
        result["loser_ok"] = LockerService.transfer_device(
            db_loser, loser_session, dev_id
        )

    thread = threading.Thread(target=loser)
    thread.start()
    assert preread_done.wait(timeout=15)

    db_winner = factory()
    DeviceRepository.find_by_id(db_winner, dev_id)  # route pre-read
    result["winner_ok"] = LockerService.transfer_device(
        db_winner, winner_session, dev_id
    )
    winner_done.set()
    thread.join(timeout=15)
    assert not thread.is_alive()

    assert result["winner_ok"] is True
    assert result["loser_ok"] is True

    check = factory()
    try:
        holder = check.get(Device, dev_id).current_borrower_id
        returns = (
            check.execute(
                select(TransactionLog)
                .where(TransactionLog.transaction_type == TransactionType.RETURN)
                .order_by(TransactionLog.id)
            )
            .scalars()
            .all()
        )
    finally:
        check.close()
    assert holder == u3_id
    assert [row.user_id for row in returns] == [u1_id, u2_id]
