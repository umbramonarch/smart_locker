"""
File: test_tap_registration.py
Description: Registration and device-tag-bind E2E tests driven by real NFC
             taps through the FakeNFCReader -> bridge -> _dispatch_insert
             pipeline. Covers self-registration (approved-name gate, enrolled
             card / device-tag rejection, window expiry, cancel), admin-
             initiated registration, and admin / dashboard tag-bind windows —
             including how work cards and borrowed-device tags interact with
             an armed bind window.
Project: smart_locker/tests/e2e
Notes: PendingRegistration / PendingTagBind live on the real AppContext
       (h.ctx). Bind-window intercept semantics come from
       tap_router.dispatch_insert, invoked by AppContext._dispatch_insert:
       a work card tapped during an armed bind logs in when no session is
       active (auth_success clears the window) and is ignored while a session
       is active; a borrowed-device tag performs an unattended return and
       leaves the window armed.
"""

from contextlib import contextmanager

import smart_locker.database.engine as engine_module

from smart_locker.database.models import DeviceStatus
from smart_locker.database.repositories import (
    DeviceRepository,
    RegistrantRepository,
    UserRepository,
)

from tests.e2e.helpers import add_device, add_user, get_device, uid_hmac_for

# Distinct UIDs — one per scenario so each tap classifies exactly as intended.
NEW_CARD_UID = "04EE00000001"       # fresh card completing self-registration
ENROLLED_UID = "04EE00000002"       # card already bound to a user row
DEVICE_TAG_UID = "04EE00000003"     # sticker already bound to a device row
WORK_UID = "04EE00000004"           # ordinary work card
ADMIN_UID = "04EE00000005"          # admin work card (for /api/admin/session)
ADMIN_REG_UID = "04EE00000006"      # card enrolled via admin registration
STICKER_UID = "04EE00000007"        # sticker bound via the kiosk admin bind
DASH_STICKER_UID = "04EE00000008"   # sticker bound via the dashboard bind
BORROWED_TAG_UID = "04EE00000009"   # sticker on a BORROWED device
UNKNOWN_UID = "04EEDDCCBBAA"        # no user or device row

DASHBOARD_HEADER = "X-Smart-Locker-Admin"
DASHBOARD_SECRET = "s3cret"


def _add_registrant(h, name: str) -> None:
    """Seed one approved name in the registrants table (the approved name list)."""
    with h.db() as db:
        RegistrantRepository.add_names(db, {name})
        db.commit()


def _start_admin_session(h) -> None:
    """Open a backend admin session via the 5-tap overlay endpoint."""
    r = h.client.post("/api/admin/session")
    assert r.status_code == 200, f"admin/session failed: {r.status_code} {r.text}"
    assert h.client.get("/api/session").json()["active"] is True


def _arm_dashboard_bind(h, pm_number: str) -> None:
    """Arm a 60s bind window via the dashboard admin-secret endpoint."""
    r = h.client.post(
        "/api/dashboard/bind-tag",
        json={"pm_number": pm_number},
        headers={DASHBOARD_HEADER: DASHBOARD_SECRET},
    )
    assert r.status_code == 200, f"dashboard/bind-tag failed: {r.status_code} {r.text}"


def _add_borrowed_device(
    h, tag_uid: str, borrower_id: int, name: str, pm_number: str, locker_slot: int
) -> int:
    """Create a BORROWED device with a bound sticker and a borrower link."""
    with h.db() as db:
        device = DeviceRepository.create(
            db,
            name=name,
            device_type="Tool",
            pm_number=pm_number,
            locker_slot=locker_slot,
            status="borrowed",
            current_borrower_id=borrower_id,
        )
        DeviceRepository.bind_tag(db, device, uid_hmac_for(tag_uid))
        db.commit()
        return device.id


def test_self_registration_tap_enrolls_user(e2e):
    """Approved name + fresh card tap -> registration_success, then the card
    authenticates like any enrolled work card."""
    h = e2e()
    _add_registrant(h, "New Person")

    r = h.client.post("/api/register", json={"name": "New Person"})
    assert r.status_code == 200
    assert r.json()["success"] is True
    assert h.ctx.pending_registration is not None

    h.tap(NEW_CARD_UID)
    payload = h.wait_event("registration_success")
    assert payload["user"]["name"] == "New Person"
    assert payload["user"]["role"] == "user"
    user_id = payload["user"]["id"]
    assert h.ctx.pending_registration is None

    # The user row exists with the HMAC of the tapped card.
    with h.db() as db:
        user = UserRepository.find_by_uid_hmac(db, uid_hmac_for(NEW_CARD_UID))
        assert user is not None
        assert user.id == user_id
        assert user.display_name == "New Person"

    # The freshly enrolled card now logs in normally.
    h.tap(NEW_CARD_UID)
    login = h.wait_event("auth_success")
    assert login["user"]["id"] == user_id
    assert login["user"]["name"] == "New Person"


def test_self_registration_tap_uses_one_database_session(e2e, monkeypatch):
    """The registration intercept classifies and enrolls in one transaction."""
    session_entries = 0
    original_get_session = engine_module.get_session

    @contextmanager
    def counting_get_session(*args, **kwargs):
        nonlocal session_entries
        session_entries += 1
        with original_get_session(*args, **kwargs) as db_session:
            yield db_session

    # The NFC bridge imports get_session during app startup, so patch before
    # booting the real bridge. Reset after the HTTP setup; only the tap counts.
    monkeypatch.setattr(engine_module, "get_session", counting_get_session)
    h = e2e()
    _add_registrant(h, "New Person")
    r = h.client.post("/api/register", json={"name": "New Person"})
    assert r.status_code == 200
    session_entries = 0

    h.tap(NEW_CARD_UID)
    payload = h.wait_event("registration_success")

    assert payload["user"]["name"] == "New Person"
    assert session_entries == 1


def test_enrolled_card_tap_during_registration_fails(e2e):
    """A card already enrolled to a user is rejected as 'already registered'."""
    h = e2e()
    add_user(h, ENROLLED_UID, display_name="Existing User")
    _add_registrant(h, "New Person")

    r = h.client.post("/api/register", json={"name": "New Person"})
    assert r.status_code == 200

    h.tap(ENROLLED_UID)
    payload = h.wait_event("registration_failed")
    assert "already registered" in payload["reason"].lower()
    assert h.ctx.pending_registration is None

    # No second user was enrolled; the card still belongs to Existing User.
    with h.db() as db:
        users = UserRepository.list_all(db)
        assert len(users) == 1
        assert users[0].display_name == "Existing User"
    assert h.client.get("/api/session").json()["active"] is False


def test_device_tag_tap_during_registration_keeps_window(e2e):
    """A sticker tap does not consume a card window: it falls through to
    ordinary handling and the armed registration still completes on the
    next card tap."""
    h = e2e()
    add_device(
        h,
        name="Tagged Camera",
        pm_number="PM-T1",
        locker_slot=1,
        tag_uid=DEVICE_TAG_UID,
    )
    _add_registrant(h, "New Person")

    r = h.client.post("/api/register", json={"name": "New Person"})
    assert r.status_code == 200

    h.tap(DEVICE_TAG_UID)
    payload = h.wait_event("device_tag_idle")
    assert "work card" in payload["message"].lower()
    # The card window survived the stray sticker tap.
    assert h.ctx.pending_registration is not None

    with h.db() as db:
        assert (
            UserRepository.find_by_uid_hmac(db, uid_hmac_for(DEVICE_TAG_UID))
            is None
        )

    h.tap(NEW_CARD_UID)
    h.wait_event("registration_success")
    assert h.ctx.pending_registration is None


def test_register_rejects_name_not_on_approved_list(e2e):
    """POST /api/register with an unlisted name -> 403 and nothing armed."""
    h = e2e()

    r = h.client.post("/api/register", json={"name": "Not Listed"})

    assert r.status_code == 403
    assert h.ctx.pending_registration is None
    assert h.ctx.pending_tag_bind is None


def test_expired_registration_window_drops_and_tap_logs_in(e2e):
    """A tap after the 60s window classifies normally instead of enrolling."""
    h = e2e()
    user_id = add_user(h, WORK_UID, display_name="Known Worker")
    _add_registrant(h, "New Person")

    r = h.client.post("/api/register", json={"name": "New Person"})
    assert r.status_code == 200
    assert h.ctx.pending_registration is not None

    # Age the window past REGISTRATION_TIMEOUT_SECONDS (monotonic clock).
    h.ctx.pending_registration.created_at -= 120

    h.tap(WORK_UID)
    payload = h.wait_event("auth_success")
    assert payload["user"]["id"] == user_id
    assert h.ctx.pending_registration is None
    assert h.client.get("/api/session").json()["active"] is True


def test_register_cancel_clears_window_and_tap_classifies_normally(e2e):
    """POST /api/register/cancel drops the window; the next tap is a normal
    classification (unknown_tag while a session is active)."""
    h = e2e()
    add_user(h, ADMIN_UID, display_name="Admin User", role="admin")
    _add_registrant(h, "New Person")

    r = h.client.post("/api/register", json={"name": "New Person"})
    assert r.status_code == 200
    assert h.ctx.pending_registration is not None

    r = h.client.post("/api/register/cancel")
    assert r.status_code == 200
    assert r.json()["cancelled"] is True
    assert h.ctx.pending_registration is None

    # With an admin session active, an unknown UID emits unknown_tag — proof
    # the cancelled registration window no longer intercepts taps.
    _start_admin_session(h)

    h.tap(UNKNOWN_UID)
    payload = h.wait_event("unknown_tag")
    assert payload["event"] == "unknown_tag"
    assert h.ctx.pending_registration is None


def test_admin_register_enrolls_arbitrary_name(e2e):
    """Admin-initiated registration skips the approved-name list."""
    h = e2e()
    add_user(h, ADMIN_UID, display_name="Admin User", role="admin")
    _start_admin_session(h)

    r = h.client.post("/api/admin/register", json={"name": "Unlisted Person"})
    assert r.status_code == 200
    assert r.json()["success"] is True
    assert h.ctx.pending_registration is not None

    h.tap(ADMIN_REG_UID)
    payload = h.wait_event("registration_success")
    assert payload["user"]["name"] == "Unlisted Person"
    assert payload["user"]["role"] == "user"
    assert h.ctx.pending_registration is None

    with h.db() as db:
        user = UserRepository.find_by_uid_hmac(db, uid_hmac_for(ADMIN_REG_UID))
        assert user is not None
        assert user.display_name == "Unlisted Person"

    # The leftover overlay session is ended so the next work-card tap is a
    # login, not a logout. broadcast_sse defers the queue put via
    # call_soon_threadsafe, so observing registration_success guarantees
    # _end_leftover_session() has already run.
    assert h.client.get("/api/session").json()["active"] is False


def test_admin_bind_tag_tap_binds_sticker(e2e):
    """Kiosk admin bind window + sticker tap -> tag_bind_success and the row
    carries the sticker HMAC."""
    h = e2e()
    add_user(h, ADMIN_UID, display_name="Admin User", role="admin")
    device_id = add_device(h, name="Bind Camera", pm_number="PM-B1", locker_slot=2)
    _start_admin_session(h)

    r = h.client.post(f"/api/admin/devices/{device_id}/bind-tag")
    assert r.status_code == 200
    assert r.json()["success"] is True
    assert h.ctx.pending_tag_bind is not None
    assert h.ctx.pending_tag_bind.device_id == device_id

    h.tap(STICKER_UID)
    payload = h.wait_event("tag_bind_success")
    assert payload["device_id"] == device_id
    assert payload["device_name"] == "Bind Camera"
    assert payload["pm_number"] == "PM-B1"
    assert h.ctx.pending_tag_bind is None

    assert get_device(h, device_id).tag_hmac == uid_hmac_for(STICKER_UID)

    # The bind touch()es the session — it stays active, so /api/devices works.
    devices = h.client.get("/api/devices").json()
    row = next(d for d in devices if d["id"] == device_id)
    assert row["has_tag"] is True


def test_work_card_login_during_armed_bind_clears_window(e2e, monkeypatch):
    """With no session, a work card tapped during an armed bind passes the
    intercept, logs in, and auth_success clears the bind window."""
    monkeypatch.setenv("SMART_LOCKER_DASHBOARD_ADMIN_SECRET", DASHBOARD_SECRET)
    h = e2e()
    user_id = add_user(h, WORK_UID, display_name="Bind Login User")
    device_id = add_device(h, name="Bind Target", pm_number="PM-B2", locker_slot=3)

    # Dashboard arm needs no kiosk session — the only way to hold a bind
    # window while logged out.
    _arm_dashboard_bind(h, "PM-B2")
    assert h.ctx.pending_tag_bind is not None

    h.tap(WORK_UID)
    payload = h.wait_event("auth_success")
    assert payload["user"]["id"] == user_id

    # auth_success cleared the armed bind window; the card was not bound.
    assert h.ctx.pending_tag_bind is None
    assert get_device(h, device_id).tag_hmac is None


def test_work_card_during_armed_bind_with_session_keeps_window(e2e):
    """While a session is active, a work card tapped during an armed bind is
    ignored entirely — no logout, no bind event, window kept."""
    h = e2e()
    add_user(h, ADMIN_UID, display_name="Admin User", role="admin")
    add_user(h, WORK_UID, display_name="Worker")
    device_id = add_device(h, name="Bind Target", pm_number="PM-B3", locker_slot=4)
    _start_admin_session(h)

    r = h.client.post(f"/api/admin/devices/{device_id}/bind-tag")
    assert r.status_code == 200
    assert h.ctx.pending_tag_bind is not None

    h.tap(WORK_UID)

    # The intercept returns early: nothing is broadcast for this tap.
    seen = h.assert_no_event("tag_bind_success", within=2.5)
    names = {e.get("event") for e in seen}
    assert "tag_bind_failed" not in names
    assert "session_ended" not in names
    assert "auth_success" not in names

    assert h.ctx.pending_tag_bind is not None
    assert h.ctx.pending_tag_bind.device_id == device_id
    assert h.client.get("/api/session").json()["active"] is True


def test_borrowed_tag_during_armed_bind_returns_and_keeps_window(
    e2e, monkeypatch
):
    """A borrowed-device sticker tapped during an armed bind performs an
    unattended return; device_action does not clear the bind window."""
    monkeypatch.setenv("SMART_LOCKER_DASHBOARD_ADMIN_SECRET", DASHBOARD_SECRET)
    h = e2e()
    borrower_id = add_user(h, WORK_UID, display_name="Borrower")
    target_id = add_device(h, name="Bind Target", pm_number="PM-B4", locker_slot=5)
    borrowed_id = _add_borrowed_device(
        h,
        BORROWED_TAG_UID,
        borrower_id,
        name="Loaned Drill",
        pm_number="PM-L1",
        locker_slot=6,
    )

    _arm_dashboard_bind(h, "PM-B4")
    assert h.ctx.pending_tag_bind is not None

    h.tap(BORROWED_TAG_UID)
    payload = h.wait_event("device_action")
    assert payload["action"] == "return"
    assert payload["success"] is True
    assert payload["device_id"] == borrowed_id

    device = get_device(h, borrowed_id)
    assert device.status == DeviceStatus.AVAILABLE
    assert device.current_borrower_id is None

    # The armed bind window survives the unattended return...
    assert h.ctx.pending_tag_bind is not None
    assert h.ctx.pending_tag_bind.device_id == target_id

    # ...and the next sticker tap still completes the bind.
    h.tap(DASH_STICKER_UID)
    bound = h.wait_event("tag_bind_success")
    assert bound["device_id"] == target_id
    assert get_device(h, target_id).tag_hmac == uid_hmac_for(DASH_STICKER_UID)


def test_dashboard_bind_survives_register_cancel_and_completes(
    e2e, monkeypatch
):
    """A from_dashboard bind cannot be cleared by the public cancel endpoint
    and completes normally on the sticker tap."""
    monkeypatch.setenv("SMART_LOCKER_DASHBOARD_ADMIN_SECRET", DASHBOARD_SECRET)
    h = e2e()
    device_id = add_device(h, name="Dash Cam", pm_number="PM-D1", locker_slot=7)

    # Without the secret header the endpoint fails closed.
    r = h.client.post("/api/dashboard/bind-tag", json={"pm_number": "PM-D1"})
    assert r.status_code == 401

    _arm_dashboard_bind(h, "PM-D1")
    bind = h.ctx.pending_tag_bind
    assert bind is not None
    assert bind.device_id == device_id
    assert bind.from_dashboard is True

    # Public cancel reports nothing pending and must not clear the window.
    r = h.client.post("/api/register/cancel")
    assert r.status_code == 200
    assert r.json()["cancelled"] is False
    assert h.ctx.pending_tag_bind is not None

    h.tap(DASH_STICKER_UID)
    payload = h.wait_event("tag_bind_success")
    assert payload["device_id"] == device_id
    assert get_device(h, device_id).tag_hmac == uid_hmac_for(DASH_STICKER_UID)
    assert h.ctx.pending_tag_bind is None
