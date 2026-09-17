"""
File: test_loan_race.py
Description: Deterministic loan-mutation race tests: borrow-vs-maintenance,
             borrow-vs-unbind, and concurrent bind-arm serialization.
             Proves the MR A finding: maintenance/unbind/bind-arm run outside
             the loan locks, so a stale read can strand a borrower, drop a
             sticker from a live loan, or silently overwrite a bind target.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_loan_race.py -v -p no:cacheprovider
       Uses a throwaway file DB (WAL) so sessions see committed state.
       Determinism via threading.Events only — no sleeps. The bind test
       forces its losing interleave with spied check/assign rendezvous.
"""

import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import smart_locker.api.app_context as ctx_module
import smart_locker.api.routes as routes
from smart_locker.api.app_context import assign_pending_tag_bind
from smart_locker.api.routes import (
    MaintenanceRequest,
    set_device_maintenance,
    start_device_tag_bind,
    unbind_device_tag,
)
from smart_locker.auth.session_manager import SessionManager
from smart_locker.database.engine import (
    get_session_factory,
    init_db,
    reset_engine,
)
from smart_locker.database.models import Device, DeviceStatus, User
from smart_locker.database.repositories import DeviceRepository, UserRepository
from smart_locker.services.locker_service import LockerService
from smart_locker.services.user_admin_lock import user_admin_lock


@pytest.fixture()
def loan_race_db(tmp_path):
    """File-backed DB with an admin, a borrower, and two tagged devices.

    Yields (factory, admin_id, borrower_id, dev1_id, dev2_id). Each racer
    uses its own session so identity maps are per-thread, as in requests.
    """
    reset_engine()
    url = f"sqlite:///{tmp_path}/loan_race.db"
    init_db(url)
    factory = get_session_factory(url)
    setup = factory()
    admin = UserRepository.create(
        setup,
        display_name="Admin",
        uid_hmac="h-adm",
        encrypted_card_uid="e-adm",
        role="admin",
    )
    borrower = UserRepository.create(
        setup, display_name="Borrower", uid_hmac="h-bor", encrypted_card_uid="e-bor"
    )
    dev1 = DeviceRepository.create(
        setup, name="Meter", device_type="Meter", pm_number="PM-LOAN-1"
    )
    DeviceRepository.bind_tag(setup, dev1, "tag-loan-1")
    dev2 = DeviceRepository.create(
        setup, name="Scope", device_type="Scope", pm_number="PM-LOAN-2"
    )
    DeviceRepository.bind_tag(setup, dev2, "tag-loan-2")
    setup.commit()
    ids = (admin.id, borrower.id, dev1.id, dev2.id)
    setup.close()
    yield (factory, *ids)
    reset_engine()


def _session_for(factory, user_id):
    """Start a kiosk session for a user, like the NFC login path does.

    The loading session is closed: only already-loaded column attributes
    (id, role, display name) are read afterwards, so the stress test does
    not exhaust the pool.
    """
    mgr = SessionManager(timeout_seconds=600)
    db = factory()
    try:
        return mgr.start_session(db.get(User, user_id))
    finally:
        db.close()


def test_maintenance_after_borrow_commits_refused(loan_race_db):
    """Maintenance that read AVAILABLE must 409 once the borrow commits.

    Interleaving: maintenance pre-reads (pins AVAILABLE), another session
    completes the borrow, maintenance proceeds. Without the in-lock
    re-read, maintenance commits MAINTENANCE over a live loan: the
    borrower stays assigned but return rejects the row (not BORROWED).
    """
    factory, admin_id, borrower_id, dev_id, _dev2_id = loan_race_db
    db_maint = factory()

    preread = DeviceRepository.find_by_id(db_maint, dev_id)  # pins AVAILABLE
    assert preread is not None

    db_borrow = factory()
    assert LockerService.borrow_device(
        db_borrow, _session_for(factory, borrower_id), dev_id
    )
    db_borrow.commit()
    db_borrow.close()

    with pytest.raises(HTTPException) as exc_info:
        set_device_maintenance(
            dev_id,
            MaintenanceRequest(maintenance=True),
            db_maint,
            _session_for(factory, admin_id),
        )
    assert exc_info.value.status_code == 409

    check = factory()
    try:
        device = check.get(Device, dev_id)
        assert device.status == DeviceStatus.BORROWED
        assert device.current_borrower_id == borrower_id
    finally:
        check.close()


def test_maintenance_serializes_on_loan_lock(loan_race_db):
    """Maintenance must wait for the shared loan lock.

    The lock holder finishing a borrow must not be overtaken by a
    maintenance commit landing between the borrow check and its commit.
    """
    factory, admin_id, _borrower_id, dev_id, _dev2_id = loan_race_db
    started = threading.Event()
    result = {}

    def maintain():
        started.set()
        result["response"] = set_device_maintenance(
            dev_id,
            MaintenanceRequest(maintenance=True),
            factory(),
            _session_for(factory, admin_id),
        )

    user_admin_lock.acquire()
    try:
        thread = threading.Thread(target=maintain)
        thread.start()
        assert started.wait(timeout=15)
        thread.join(timeout=5)
        assert thread.is_alive()
    finally:
        user_admin_lock.release()
    thread.join(timeout=15)
    assert not thread.is_alive()

    assert result["response"] == {"success": True, "status": "maintenance"}


def test_borrow_after_unbind_refused(loan_race_db):
    """Borrow must refuse a device unbound after the route tag check.

    Interleaving: route checks tag presence (pins tag), another session
    unbinds + commits, borrow proceeds. Without the in-service tag
    recheck, the service re-reads AVAILABLE and lends an untagged unit.
    """
    factory, admin_id, borrower_id, dev_id, _dev2_id = loan_race_db
    db_borrow = factory()

    preread = DeviceRepository.find_by_id(db_borrow, dev_id)  # route tag check
    assert preread is not None and preread.tag_hmac is not None

    db_admin = factory()
    assert unbind_device_tag(
        dev_id, db_admin, _session_for(factory, admin_id)
    ) == {"success": True}
    db_admin.commit()
    db_admin.close()

    assert (
        LockerService.borrow_device(
            db_borrow, _session_for(factory, borrower_id), dev_id
        )
        is False
    )

    check = factory()
    try:
        device = check.get(Device, dev_id)
        assert device.status == DeviceStatus.AVAILABLE
        assert device.current_borrower_id is None
        assert device.tag_hmac is None
    finally:
        check.close()


def test_unbind_serializes_on_loan_lock(loan_race_db):
    """Unbind must wait for the shared loan lock.

    A borrow committing between the unbind guard refresh and the unbind
    commit would otherwise strand a borrowed+untagged loan whose sticker
    no longer returns it.
    """
    factory, admin_id, _borrower_id, dev_id, _dev2_id = loan_race_db
    started = threading.Event()
    result = {}

    def unbind():
        started.set()
        result["response"] = unbind_device_tag(
            dev_id, factory(), _session_for(factory, admin_id)
        )

    user_admin_lock.acquire()
    try:
        thread = threading.Thread(target=unbind)
        thread.start()
        assert started.wait(timeout=15)
        thread.join(timeout=5)
        assert thread.is_alive()
    finally:
        user_admin_lock.release()
    thread.join(timeout=15)
    assert not thread.is_alive()

    assert result["response"] == {"success": True}


def test_concurrent_bind_arm_single_winner(loan_race_db, monkeypatch):
    """Two concurrent bind arms must not both report success.

    The conflict check and the assignment must be atomic: the loser gets
    409 and the winner's target stays armed. Spies pause the first arm
    between its check and its assignment until the second arm has
    checked, forcing the losing interleave on unfixed code; on fixed
    code the second arm blocks on the lock, the wait expires, and the
    arms serialize.
    """
    factory, admin_id, _borrower_id, dev1_id, dev2_id = loan_race_db
    ctx = SimpleNamespace(pending_tag_bind=None, pending_registration=None)
    monkeypatch.setattr(ctx_module, "context", ctx)

    real_assign = routes.assign_pending_tag_bind
    real_conflict = routes._pending_nfc_conflict
    entered1 = threading.Event()
    checked2 = threading.Event()
    calls = {"assign": 0, "check": 0}
    calls_lock = threading.Lock()

    def spy_assign(c, bind):
        with calls_lock:
            calls["assign"] += 1
            first = calls["assign"] == 1
        if first:
            entered1.set()
            # Expires only when the second arm is locked out (fixed code).
            checked2.wait(timeout=5)
        return real_assign(c, bind)

    def spy_conflict():
        outcome = real_conflict()
        with calls_lock:
            calls["check"] += 1
            if calls["check"] == 2:
                checked2.set()
        return outcome

    monkeypatch.setattr(routes, "assign_pending_tag_bind", spy_assign)
    monkeypatch.setattr(routes, "_pending_nfc_conflict", spy_conflict)

    outcomes = {}

    def arm(slot, device_id):
        db = factory()
        try:
            start_device_tag_bind(device_id, db, _session_for(factory, admin_id))
            outcomes[slot] = "armed"
        except HTTPException as exc:
            outcomes[slot] = exc.status_code
        finally:
            db.close()

    first = threading.Thread(target=arm, args=(0, dev1_id))
    first.start()
    assert entered1.wait(timeout=15)
    second = threading.Thread(target=arm, args=(1, dev2_id))
    second.start()
    first.join(timeout=25)
    second.join(timeout=25)
    assert not first.is_alive()
    assert not second.is_alive()

    try:
        assert sorted(outcomes.values(), key=str) == [409, "armed"], outcomes
        winner_id = dev1_id if outcomes[0] == "armed" else dev2_id
        assert ctx.pending_tag_bind is not None
        assert ctx.pending_tag_bind.device_id == winner_id
    finally:
        assign_pending_tag_bind(ctx, None)
