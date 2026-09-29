"""
File: test_people.py
Description: People E2E tests driven by real NFC taps through the
             FakeNFCReader -> bridge -> _dispatch_insert pipeline. Covers the
             kiosk-admin and dashboard-secret arms for add-person and
             replace-card: enrollment with a chosen role, card rebinding,
             already-registered and device-tag refusals, idempotent same-card
             replacement, and that a sticker tap does not consume a card
             window. Also verifies a deactivated card stops authenticating.
Project: smart_locker/tests/e2e
Notes: The pending card window lives on the real AppContext (h.ctx). People
       windows are PendingRegistration with a role and, for replace, a
       replace_user_id — the same dispatch lane self-registration uses, so
       every resolution emits registration_success / registration_failed.
"""

from smart_locker.database.repositories import UserRepository

from tests.e2e.helpers import add_device, add_user, get_user, uid_hmac_for

# Distinct UIDs — one per scenario so each tap classifies exactly as intended.
ADMIN_UID = "04EF00000001"          # admin work card (for /api/admin/session)
OLD_CARD_UID = "04EF00000002"       # person's current card being replaced
OTHER_CARD_UID = "04EF00000003"     # card already bound to someone else
NEW_CARD_UID = "04EF00000004"       # fresh card completing add/replace
SECOND_NEW_UID = "04EF00000005"     # second fresh card (post-failure retry)
DEVICE_TAG_UID = "04EF00000006"     # sticker bound to a device row

DASHBOARD_HEADER = "X-Smart-Locker-Admin"
DASHBOARD_SECRET = "s3cret"


def _start_admin_session(h) -> None:
    """Open a backend admin session via the 5-tap overlay endpoint."""
    r = h.client.post("/api/admin/session")
    assert r.status_code == 200, f"admin/session failed: {r.status_code} {r.text}"
    assert h.client.get("/api/session").json()["active"] is True


def _dashboard_headers() -> dict:
    return {DASHBOARD_HEADER: DASHBOARD_SECRET}


def test_kiosk_add_person_tap_enrolls_with_role(e2e):
    """People add-person on the kiosk arms the reader; the tapped card enrolls
    a new user with the chosen role, and the leftover admin session ends."""
    h = e2e()
    add_user(h, ADMIN_UID, display_name="Admin User", role="admin")
    _start_admin_session(h)

    r = h.client.post("/api/admin/users", json={"name": "New Manager", "role": "admin"})
    assert r.status_code == 200
    assert h.ctx.pending_registration is not None
    assert h.ctx.pending_registration.role == "admin"
    assert h.ctx.pending_registration.replace_user_id is None

    h.tap(NEW_CARD_UID)
    payload = h.wait_event("registration_success")
    assert payload["user"]["name"] == "New Manager"
    assert payload["user"]["role"] == "admin"
    assert h.ctx.pending_registration is None

    with h.db() as db:
        user = UserRepository.find_by_uid_hmac(db, uid_hmac_for(NEW_CARD_UID))
        assert user is not None
        assert user.display_name == "New Manager"
        assert user.role.value == "admin"

    # The card window ended the leftover admin overlay session — the next
    # work-card tap is a login, not a logout.
    assert h.client.get("/api/session").json()["active"] is False

    h.tap(NEW_CARD_UID)
    login = h.wait_event("auth_success")
    assert login["user"]["name"] == "New Manager"


def test_dashboard_add_person_tap_enrolls(e2e, monkeypatch):
    """Dashboard add-person arms the cabinet reader remotely; the physical
    card tap on the kiosk completes the enrollment."""
    monkeypatch.setenv("SMART_LOCKER_DASHBOARD_ADMIN_SECRET", DASHBOARD_SECRET)
    h = e2e()

    r = h.client.post(
        "/api/dashboard/users",
        json={"name": "Remote Hire"},
        headers=_dashboard_headers(),
    )
    assert r.status_code == 200
    assert h.ctx.pending_registration is not None
    assert h.ctx.pending_registration.from_dashboard is True

    h.tap(NEW_CARD_UID)
    payload = h.wait_event("registration_success")
    assert payload["user"]["name"] == "Remote Hire"
    assert payload["user"]["role"] == "user"
    assert h.ctx.pending_registration is None

    with h.db() as db:
        user = UserRepository.find_by_uid_hmac(db, uid_hmac_for(NEW_CARD_UID))
        assert user is not None
        assert user.display_name == "Remote Hire"


def test_replace_card_tap_rebinds_user(e2e, monkeypatch):
    """Replace-card rebinds the person to the tapped card: the row keeps its
    id/role, the old card no longer authenticates, the new card does."""
    monkeypatch.setenv("SMART_LOCKER_DASHBOARD_ADMIN_SECRET", DASHBOARD_SECRET)
    h = e2e()
    user_id = add_user(h, OLD_CARD_UID, display_name="Card Holder", role="user")

    r = h.client.post(
        f"/api/dashboard/users/{user_id}/replace-card",
        headers=_dashboard_headers(),
    )
    assert r.status_code == 200
    pending = h.ctx.pending_registration
    assert pending is not None
    assert pending.replace_user_id == user_id

    h.tap(NEW_CARD_UID)
    payload = h.wait_event("registration_success")
    assert payload["user"]["id"] == user_id
    assert h.ctx.pending_registration is None

    user = get_user(h, user_id)
    assert user.uid_hmac == uid_hmac_for(NEW_CARD_UID)
    assert user.encrypted_card_uid != f"enc:{OLD_CARD_UID}"
    assert user.display_name == "Card Holder"

    # The old card is unbound — an idle tap is an unknown card.
    h.tap(OLD_CARD_UID)
    h.wait_event("auth_failed")

    # The new card logs in as the same person.
    h.tap(NEW_CARD_UID)
    login = h.wait_event("auth_success")
    assert login["user"]["id"] == user_id
    assert login["user"]["name"] == "Card Holder"


def test_replace_card_with_another_persons_card_fails(e2e, monkeypatch):
    """A replacement cannot steal a card already enrolled to someone else —
    the tap fails, the target keeps the old card, the window is consumed."""
    monkeypatch.setenv("SMART_LOCKER_DASHBOARD_ADMIN_SECRET", DASHBOARD_SECRET)
    h = e2e()
    target_id = add_user(h, OLD_CARD_UID, display_name="Target User")
    add_user(h, OTHER_CARD_UID, display_name="Other User")

    r = h.client.post(
        f"/api/dashboard/users/{target_id}/replace-card",
        headers=_dashboard_headers(),
    )
    assert r.status_code == 200

    h.tap(OTHER_CARD_UID)
    payload = h.wait_event("registration_failed")
    assert "someone else" in payload["reason"].lower()
    assert h.ctx.pending_registration is None

    user = get_user(h, target_id)
    assert user.uid_hmac == uid_hmac_for(OLD_CARD_UID)
    assert user.encrypted_card_uid == f"enc:{OLD_CARD_UID}"


def test_replace_card_same_card_is_idempotent(e2e, monkeypatch):
    """Tapping the person's current card during replace is a no-op success —
    the window closes and the row is untouched."""
    monkeypatch.setenv("SMART_LOCKER_DASHBOARD_ADMIN_SECRET", DASHBOARD_SECRET)
    h = e2e()
    user_id = add_user(h, OLD_CARD_UID, display_name="Same Card")

    r = h.client.post(
        f"/api/dashboard/users/{user_id}/replace-card",
        headers=_dashboard_headers(),
    )
    assert r.status_code == 200

    h.tap(OLD_CARD_UID)
    payload = h.wait_event("registration_success")
    assert payload["user"]["id"] == user_id
    assert h.ctx.pending_registration is None

    user = get_user(h, user_id)
    assert user.uid_hmac == uid_hmac_for(OLD_CARD_UID)


def test_kiosk_replace_card_tap_rebinds(e2e):
    """The kiosk-admin replace-card arm takes the same dispatch lane; the
    physical tap rebinds and ends the leftover admin session."""
    h = e2e()
    add_user(h, ADMIN_UID, display_name="Admin User", role="admin")
    user_id = add_user(h, OLD_CARD_UID, display_name="Kiosk Card Holder")
    _start_admin_session(h)

    r = h.client.post(f"/api/admin/users/{user_id}/replace-card")
    assert r.status_code == 200
    assert h.ctx.pending_registration.replace_user_id == user_id

    h.tap(NEW_CARD_UID)
    payload = h.wait_event("registration_success")
    assert payload["user"]["id"] == user_id
    assert h.ctx.pending_registration is None
    assert get_user(h, user_id).uid_hmac == uid_hmac_for(NEW_CARD_UID)
    assert h.client.get("/api/session").json()["active"] is False


def test_device_sticker_does_not_consume_card_window(e2e, monkeypatch):
    """A sticker tapped while a replace-card window is armed falls through to
    ordinary handling — the card window still completes on the next card."""
    monkeypatch.setenv("SMART_LOCKER_DASHBOARD_ADMIN_SECRET", DASHBOARD_SECRET)
    h = e2e()
    user_id = add_user(h, OLD_CARD_UID, display_name="Card Holder")
    add_device(h, name="Tagged Tool", pm_number="PM-T9", locker_slot=1,
               tag_uid=DEVICE_TAG_UID)

    r = h.client.post(
        f"/api/dashboard/users/{user_id}/replace-card",
        headers=_dashboard_headers(),
    )
    assert r.status_code == 200

    h.tap(DEVICE_TAG_UID)
    h.wait_event("device_tag_idle")
    assert h.ctx.pending_registration is not None
    assert get_user(h, user_id).uid_hmac == uid_hmac_for(OLD_CARD_UID)

    h.tap(NEW_CARD_UID)
    payload = h.wait_event("registration_success")
    assert payload["user"]["id"] == user_id
    assert get_user(h, user_id).uid_hmac == uid_hmac_for(NEW_CARD_UID)


def test_deactivated_person_card_stops_authenticating(e2e, monkeypatch):
    """Deactivation via the dashboard PATCH means the very next tap of that
    card fails auth — the row still exists but the door is closed."""
    monkeypatch.setenv("SMART_LOCKER_DASHBOARD_ADMIN_SECRET", DASHBOARD_SECRET)
    h = e2e()
    user_id = add_user(h, OLD_CARD_UID, display_name="Leaving User")

    r = h.client.patch(
        f"/api/dashboard/users/{user_id}",
        json={"is_active": False},
        headers=_dashboard_headers(),
    )
    assert r.status_code == 200
    assert r.json()["is_active"] is False

    h.tap(OLD_CARD_UID)
    h.wait_event("auth_failed")
    assert h.client.get("/api/session").json()["active"] is False

    # Reactivating restores the card at once.
    r = h.client.patch(
        f"/api/dashboard/users/{user_id}",
        json={"is_active": True},
        headers=_dashboard_headers(),
    )
    assert r.status_code == 200

    h.tap(OLD_CARD_UID)
    login = h.wait_event("auth_success")
    assert login["user"]["id"] == user_id


def test_second_dashboard_arm_conflicts(e2e, monkeypatch):
    """A second dashboard card-window arm is refused while one owns the
    reader — the first window keeps its name and stays armed."""
    monkeypatch.setenv("SMART_LOCKER_DASHBOARD_ADMIN_SECRET", DASHBOARD_SECRET)
    h = e2e()
    add_user(h, ADMIN_UID, display_name="Admin User", role="admin")

    r = h.client.post(
        "/api/dashboard/users",
        json={"name": "Window Holder"},
        headers=_dashboard_headers(),
    )
    assert r.status_code == 200

    # A second arm while the window owns the reader is refused.
    r = h.client.post(
        "/api/dashboard/users",
        json={"name": "Second Arm"},
        headers=_dashboard_headers(),
    )
    assert r.status_code == 409
    pending = h.ctx.pending_registration
    assert pending is not None
    assert pending.display_name == "Window Holder"
