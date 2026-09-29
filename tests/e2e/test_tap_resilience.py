"""
File: test_tap_resilience.py
Description: Bridge-survival E2E tests. A dispatch-level failure (handler
             flush error, auto-commit failure, missing ENC key) must emit the
             window's *_failed SSE and leave the NFC bridge alive — the next
             tap is still processed. Also covers the dispatch-time pending
             clears: an expired registration drops the leftover overlay, and
             end_kiosk_session's expected_pending guard spares windows armed
             after the snapshot.
Project: smart_locker/tests/e2e
Notes: Commit failures are injected by monkeypatching Session.commit for one
       call; the enc-key path is exercised by clearing key_manager's cache
       plus the env var.
"""

import time

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

import smart_locker.auth.tap_router as tap_router

from smart_locker.api.app_context import (
    PendingRegistration,
    PendingTagBind,
    assign_pending_registration,
    assign_pending_tag_bind,
)
from smart_locker.database.models import DeviceStatus
from smart_locker.database.repositories import (
    DeviceRepository,
    RegistrantRepository,
    UserRepository,
)
from smart_locker.security.key_manager import KeyManager, key_manager

from tests.e2e.helpers import add_device, add_user, get_device, uid_hmac_for

WORK_UID = "04EE00000101"          # ordinary work card
ADMIN_UID = "04EE00000102"         # admin work card
REUSE_UID = "04EE00000103"         # card of a deactivated user row
NEW_CARD_UID = "04EE00000104"      # fresh card for self-registration
TAG_UID = "04EE00000105"           # sticker on an available device
SECOND_TAG_UID = "04EE00000106"    # sticker that fails to bind


def _add_registrant(h, name: str) -> None:
    with h.db() as db:
        RegistrantRepository.add_names(db, {name})
        db.commit()


def test_deactivated_user_card_is_refused_during_registration(e2e):
    """Re-tapping a deactivated user's card during a card window is a clean
    refusal — the card still belongs to its person row, so enroll never runs
    (previously this died on the uid_hmac unique constraint)."""
    h = e2e()
    add_user(h, REUSE_UID, display_name="Old User")
    with h.db() as db:
        user = UserRepository.find_by_uid_hmac(db, uid_hmac_for(REUSE_UID))
        user.is_active = False
        db.commit()
    _add_registrant(h, "Old User")

    r = h.client.post("/api/register", json={"name": "Old User"})
    assert r.status_code == 200
    assert h.ctx.pending_registration is not None

    h.tap(REUSE_UID)
    payload = h.wait_event("registration_failed")
    assert "already registered" in payload["reason"].lower()
    assert h.ctx.pending_registration is None

    # No second user row was created for the same card.
    with h.db() as db:
        users = UserRepository.list_all(db)
        assert len(users) == 1


def test_enroll_flush_failure_fails_registration_and_bridge_survives(
    e2e, monkeypatch
):
    """A flush-level failure inside enroll_user (e.g. database locked) must
    roll back, emit registration_failed, clear the window, and keep the
    bridge alive — same contract as the tag-bind dispatch failure."""
    h = e2e()
    add_user(h, WORK_UID, display_name="Worker")
    _add_registrant(h, "New Person")

    def boom(*args, **kwargs):
        raise OperationalError(
            "INSERT users", {}, Exception("database is locked")
        )

    monkeypatch.setattr(UserRepository, "create", boom)

    r = h.client.post("/api/register", json={"name": "New Person"})
    assert r.status_code == 200

    h.tap(NEW_CARD_UID)
    payload = h.wait_event("registration_failed")
    assert "try again" in payload["reason"].lower()
    assert h.ctx.pending_registration is None

    # The bridge survived: an ordinary work-card tap still logs in.
    h.tap(WORK_UID)
    login = h.wait_event("auth_success")
    assert login["user"]["name"] == "Worker"


def test_missing_enc_key_breaks_registration_only(e2e, monkeypatch):
    """Without SMART_LOCKER_ENC_KEY a plain work-card tap still logs in (the
    key is resolved lazily), while a registration tap gets registration_failed
    and the bridge keeps running."""
    h = e2e()
    user_id = add_user(h, WORK_UID, display_name="Worker")
    _add_registrant(h, "New Person")

    monkeypatch.delenv("SMART_LOCKER_ENC_KEY")
    monkeypatch.setattr(key_manager, "_enc_key", None)

    # Plain taps never touch the AES key.
    h.tap(WORK_UID)
    login = h.wait_event("auth_success")
    assert login["user"]["id"] == user_id
    h.client.post("/api/session/end")
    h.wait_event("session_ended")

    r = h.client.post("/api/register", json={"name": "New Person"})
    assert r.status_code == 200
    h.tap(NEW_CARD_UID)
    payload = h.wait_event("registration_failed")
    assert "try again" in payload["reason"].lower()
    assert h.ctx.pending_registration is None

    with h.db() as db:
        assert (
            UserRepository.find_by_uid_hmac(db, uid_hmac_for(NEW_CARD_UID))
            is None
        )

    # Bridge still alive after the failed enrollment.
    h.tap(WORK_UID)
    h.wait_event("auth_success")


def test_sticker_tap_during_card_window_never_resolves_enc_key(
    e2e, monkeypatch
):
    """A sticker tap during an armed card window must not resolve the AES key:
    the provider runs only in the card lane. A stray sticker while
    SMART_LOCKER_ENC_KEY is broken used to fail the whole tap with
    registration_failed and kill the window."""
    h = e2e()
    add_device(
        h, name="Tagged Tool", pm_number="PM-EK", locker_slot=3, tag_uid=TAG_UID
    )
    _add_registrant(h, "New Person")

    enc_reads = []
    broken = {"on": True}

    def gated_enc_key(self):
        enc_reads.append(1)
        if broken["on"]:
            raise EnvironmentError("missing SMART_LOCKER_ENC_KEY")
        return b"\x01" * 32

    monkeypatch.setattr(KeyManager, "enc_key", property(gated_enc_key))

    r = h.client.post("/api/register", json={"name": "New Person"})
    assert r.status_code == 200
    assert h.ctx.pending_registration is not None

    h.tap(TAG_UID)
    h.wait_event("device_tag_idle")

    # The provider was never invoked and the card window is still armed.
    assert enc_reads == []
    assert h.ctx.pending_registration is not None
    h.assert_no_event("registration_failed", within=1.0)

    # In the card lane the provider does run — unbreak the key and enroll.
    broken["on"] = False
    h.tap(NEW_CARD_UID)
    h.wait_event("registration_success")
    assert enc_reads != []
    assert h.ctx.pending_registration is None


def test_sticker_fallthrough_db_fault_keeps_card_window(e2e, monkeypatch):
    """A DB fault inside the device-tag fall-through reports a generic
    device_action error — it must not escalate to registration_failed, must
    not clear the armed card window, and the next card tap still completes."""
    h = e2e()
    add_device(
        h, name="Tagged Tool", pm_number="PM-FT", locker_slot=4, tag_uid=TAG_UID
    )
    _add_registrant(h, "New Person")

    r = h.client.post("/api/register", json={"name": "New Person"})
    assert r.status_code == 200

    real_handle_insert = tap_router.handle_insert
    state = {"fail": True}

    def flaky_handle_insert(*args, **kwargs):
        if state["fail"]:
            state["fail"] = False
            raise OperationalError(
                "SELECT devices", {}, Exception("database is locked")
            )
        return real_handle_insert(*args, **kwargs)

    monkeypatch.setattr(tap_router, "handle_insert", flaky_handle_insert)

    h.tap(TAG_UID)
    payload = h.wait_event("device_action")
    assert payload["success"] is False
    assert payload["action"] == "error"
    assert "went wrong" in payload["message"].lower()

    # Not a card-window outcome: the window stays armed, the latch stays empty.
    assert h.ctx.pending_registration is not None
    assert h.ctx.last_card_result is None
    h.assert_no_event("registration_failed", within=1.0)

    h.tap(NEW_CARD_UID)
    h.wait_event("registration_success")
    assert h.ctx.pending_registration is None


def test_commit_failure_on_borrow_reports_error_and_next_tap_works(e2e, monkeypatch):
    """A commit-level failure (database is locked) on the borrow path emits a
    device_action failure SSE, rolls the change back, and leaves the session
    and bridge usable."""
    h = e2e()
    add_user(h, WORK_UID, display_name="Worker")
    device_id = add_device(
        h, name="Meter", pm_number="PM-C1", locker_slot=1, tag_uid=TAG_UID
    )

    h.tap(WORK_UID)
    h.wait_event("auth_success")

    real_commit = Session.commit
    state = {"armed": True}

    def flaky_commit(self):
        if state["armed"]:
            state["armed"] = False
            raise OperationalError("COMMIT", {}, Exception("database is locked"))
        return real_commit(self)

    monkeypatch.setattr(Session, "commit", flaky_commit)

    h.tap(TAG_UID)
    payload = h.wait_event("device_action")
    assert payload["success"] is False

    # The failed borrow rolled back — the device is still available and the
    # mirror-dirty flag was discarded with the transaction.
    device = get_device(h, device_id)
    assert device.status == DeviceStatus.AVAILABLE
    assert device.current_borrower_id is None

    # Same session, next tap completes the borrow.
    h.tap(TAG_UID)
    payload = h.wait_event("device_action")
    assert payload["success"] is True
    assert payload["action"] == "borrow"
    assert get_device(h, device_id).status == DeviceStatus.BORROWED


def test_expired_registration_drops_leftover_overlay(e2e):
    """An expired registration window drops the leftover admin overlay before
    the tap is classified — matching the pre-consolidation _end_leftover_session
    call site."""
    h = e2e()
    add_user(h, ADMIN_UID, display_name="Admin User", role="admin")
    add_device(h, name="Meter", pm_number="PM-E1", locker_slot=1, tag_uid=TAG_UID)

    r = h.client.post("/api/admin/session")
    assert r.status_code == 200
    assert h.ctx.admin_overlay_open is True

    r = h.client.post("/api/admin/register", json={"name": "New Person"})
    assert r.status_code == 200
    assert h.ctx.pending_registration is not None

    h.ctx.pending_registration.created_at = time.monotonic() - 120

    # A tag tap after the window lapsed classifies at idle — and the overlay
    # flag is dropped with the leftover session.
    h.tap(TAG_UID)
    h.wait_event("device_tag_idle")
    assert h.ctx.pending_registration is None
    assert h.ctx.admin_overlay_open is False
    assert h.client.get("/api/session").json()["overlay"] is False


def test_end_kiosk_session_expected_pending_spares_newer_window(e2e):
    """expected_pending clears only the snapshot object — a window an HTTP
    route armed after the dispatch snapshot survives the session end."""
    h = e2e()
    old = PendingRegistration(display_name="Old Window")
    assign_pending_registration(h.ctx, old)
    newer = PendingRegistration(display_name="New Window")
    assign_pending_registration(h.ctx, newer)

    h.ctx.end_kiosk_session(emit=False, expected_pending=(old, None))
    assert h.ctx.pending_registration is newer

    # The default call still clears unconditionally.
    h.ctx.end_kiosk_session(emit=False)
    assert h.ctx.pending_registration is None


def test_end_kiosk_session_preserves_dashboard_armed_windows(e2e):
    """A kiosk session end clears kiosk-armed pending windows, but a remote
    (from_dashboard) arm is not kiosk-session state — it must survive for
    both the registration and the tag-bind window."""
    h = e2e()

    # Kiosk-armed windows are kiosk-session state: both clear.
    assign_pending_registration(
        h.ctx, PendingRegistration(display_name="Kiosk Window")
    )
    assign_pending_tag_bind(h.ctx, PendingTagBind(device_id=1))
    h.ctx.end_kiosk_session(emit=False)
    assert h.ctx.pending_registration is None
    assert h.ctx.pending_tag_bind is None

    # Dashboard-armed windows are remote state: both survive a session end.
    reg = PendingRegistration(display_name="Remote Window", from_dashboard=True)
    bind = PendingTagBind(device_id=2, from_dashboard=True)
    assign_pending_registration(h.ctx, reg)
    assign_pending_tag_bind(h.ctx, bind)
    h.ctx.end_kiosk_session(emit=False)
    assert h.ctx.pending_registration is reg
    assert h.ctx.pending_tag_bind is bind


def test_dispatch_exception_during_bind_emits_tag_bind_failed(e2e, monkeypatch):
    """A non-ValueError fault inside the bind handler (e.g. a flush error)
    produces tag_bind_failed, clears the window, and leaves the bridge live."""
    h = e2e()
    add_user(h, ADMIN_UID, display_name="Admin User", role="admin")
    device_id = add_device(h, name="Bind Target", pm_number="PM-BX", locker_slot=2)

    h.client.post("/api/admin/session")
    r = h.client.post(f"/api/admin/devices/{device_id}/bind-tag")
    assert r.status_code == 200
    assert h.ctx.pending_tag_bind is not None

    def boom(*args, **kwargs):
        raise OperationalError(
            "UPDATE devices", {}, Exception("database is locked")
        )

    monkeypatch.setattr(DeviceRepository, "bind_tag", boom)

    h.tap(SECOND_TAG_UID)
    payload = h.wait_event("tag_bind_failed")
    assert "try again" in payload["reason"].lower()
    assert h.ctx.pending_tag_bind is None
    assert get_device(h, device_id).tag_hmac is None

    # Bridge alive: the next work-card tap still reaches dispatch (here: the
    # active admin session logs out).
    h.tap(ADMIN_UID)
    h.wait_event("session_ended")
