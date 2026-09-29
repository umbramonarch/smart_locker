"""
File: test_tap_session.py
Description: Session-lifecycle E2E tests driven by simulated NFC taps. Boots
             the real app via the e2e harness — FakeNFCReader, the NFC bridge
             loop, tap_router, and SessionManager all run for real against a
             shared in-memory SQLite — and asserts the SSE events and
             /api/session state each tap produces, including timeout, kiosk
             state cleanup, and LAN lockout.
Project: smart_locker/tests/e2e
Notes: Event names follow tap_router._handle_idle / _handle_logged_in: an
       unknown UID at idle emits ``auth_failed`` (``unknown_tag`` is the
       in-session variant), and any work card tapped during a session ends it
       (``session_ended`` reason=card_tap) rather than switching users. Both
       ``auth_success`` and ``session_ended`` clear admin_overlay_open and
       pending_tag_bind after the typed tap_router.dispatch_insert outcome is
       applied by AppContext._dispatch_insert.
"""

from smart_locker.api.app_context import PendingRegistration, PendingTagBind
from smart_locker.database.models import User
from smart_locker.nfc.reader_observer import ReaderEvent, ReaderEventType

from tests.e2e.helpers import add_user

WORK_UID = "04AA00000001"
OTHER_UID = "04AA00000002"
INACTIVE_UID = "04AA00000003"
TIMEOUT_UID = "04AA00000004"
LAN_UID = "04AA00000005"
UNKNOWN_UID = "04DEADBEEF99"


def _set_user_active(h, user_id: int, active: bool) -> None:
    """Flip ``User.is_active`` on the shared DB (UserRepository.create has no flag arg)."""
    with h.db() as db:
        user = db.get(User, user_id)
        assert user is not None
        user.is_active = active
        db.commit()


def test_unknown_card_at_idle_broadcasts_auth_failed(e2e):
    h = e2e()

    h.tap(UNKNOWN_UID)

    # Idle unknown taps are auth_failed; unknown_tag is only emitted while a
    # session is active (tap_router._handle_idle fall-through).
    payload = h.wait_event("auth_failed")
    assert payload["event"] == "auth_failed"
    assert h.client.get("/api/session").json()["active"] is False


def test_unknown_tag_during_session_keeps_session_active(e2e):
    h = e2e()
    add_user(h, WORK_UID, display_name="Session User")

    h.tap(WORK_UID)
    h.wait_event("auth_success")

    h.tap(UNKNOWN_UID)
    payload = h.wait_event("unknown_tag")
    assert payload["message"] == "Unknown tag."
    # The tap touch()es the session instead of ending it.
    assert h.client.get("/api/session").json()["active"] is True


def test_work_card_tap_starts_session_and_broadcasts_auth_success(e2e):
    h = e2e()
    user_id = add_user(h, WORK_UID, display_name="E2E Worker", role="admin")

    h.tap(WORK_UID)

    payload = h.wait_event("auth_success")
    assert payload["user"] == {
        "id": user_id,
        "name": "E2E Worker",
        "role": "admin",
    }

    session = h.client.get("/api/session").json()
    assert session["active"] is True
    assert session["user"]["id"] == user_id
    assert session["user"]["role"] == "admin"
    assert session["overlay"] is False


def test_other_users_card_ends_session_without_switching(e2e):
    """A second work card always logs out — it does not switch users."""
    h = e2e()
    add_user(h, WORK_UID, display_name="First User", role="user")
    add_user(h, OTHER_UID, display_name="Second User", role="admin")

    h.tap(WORK_UID)
    h.wait_event("auth_success")

    h.tap(OTHER_UID)
    payload = h.wait_event("session_ended")
    assert payload["reason"] == "card_tap"

    session = h.client.get("/api/session").json()
    assert session["active"] is False
    assert session["user"] is None
    # No follow-up auth_success: the tap was a logout, not a user switch.
    h.assert_no_event("auth_success", within=1.5)
    assert h.client.get("/api/session").json()["active"] is False


def test_inactive_user_card_fails_auth(e2e):
    h = e2e()
    user_id = add_user(h, INACTIVE_UID, display_name="Inactive User")
    _set_user_active(h, user_id, False)

    h.tap(INACTIVE_UID)

    # Inactive users still classify as WORK_CARD but are refused with the
    # same opaque auth_failed as an unknown card.
    h.wait_event("auth_failed")
    assert h.client.get("/api/session").json()["active"] is False


def test_session_timeout_broadcasts_session_timeout(e2e, monkeypatch):
    # Read by AppContext at construction, so patch before booting.
    monkeypatch.setattr("smart_locker.api.app_context.SESSION_TIMEOUT_SECONDS", 1)
    h = e2e()
    add_user(h, TIMEOUT_UID, display_name="Timeout User")

    h.tap(TIMEOUT_UID)
    h.wait_event("auth_success")
    assert h.client.get("/api/session").json()["active"] is True

    # The bridge checks the expired-session transition on every ~0.5s poll.
    payload = h.wait_event("session_timeout", timeout=8)
    assert payload["event"] == "session_timeout"
    assert h.client.get("/api/session").json()["active"] is False


def test_session_end_clears_overlay_and_pending_tag_bind(e2e):
    h = e2e()
    add_user(h, WORK_UID, display_name="Overlay User")

    # Arm overlay + bind before login. A work card passes the bind intercept
    # (binds are for stickers), logs in, and auth_success clears both flags.
    h.ctx.admin_overlay_open = True
    h.ctx.pending_tag_bind = PendingTagBind(device_id=1)

    h.tap(WORK_UID)
    h.wait_event("auth_success")
    assert h.ctx.admin_overlay_open is False
    assert h.ctx.pending_tag_bind is None

    # Re-open only the overlay — a pending bind would swallow the logout tap
    # ("work card during bind keeps the window" branch in _dispatch_insert).
    h.ctx.admin_overlay_open = True

    h.tap(WORK_UID)
    payload = h.wait_event("session_ended")
    assert payload["reason"] == "card_tap"
    assert h.ctx.admin_overlay_open is False
    assert h.ctx.pending_tag_bind is None
    assert h.client.get("/api/session").json()["active"] is False


def test_http_session_end_preserves_dashboard_pending_state(e2e):
    """Session end drops kiosk-armed windows, but a dashboard-armed window is
    not kiosk-session state — it survives for the remote arm to complete."""
    h = e2e()
    add_user(h, WORK_UID)
    h.tap(WORK_UID)
    h.wait_event("auth_success")
    h.ctx.admin_overlay_open = True
    h.ctx.pending_registration = PendingRegistration("Pending User")
    bind = PendingTagBind(device_id=1, from_dashboard=True)
    h.ctx.pending_tag_bind = bind

    response = h.client.post("/api/session/end")

    assert response.status_code == 200
    assert h.wait_event("session_ended") == {
        "event": "session_ended", "reason": "explicit"
    }
    assert h.ctx.admin_overlay_open is False
    assert h.ctx.pending_registration is None
    assert h.ctx.pending_tag_bind is bind
    assert h.client.get("/api/session").json()["active"] is False


def test_reader_disconnect_preserves_dashboard_pending_state(e2e):
    """Reader disconnect ends the kiosk session like an explicit end: kiosk
    windows clear, the remote-armed bind survives."""
    h = e2e()
    add_user(h, WORK_UID)
    h.tap(WORK_UID)
    h.wait_event("auth_success")
    h.ctx.admin_overlay_open = True
    h.ctx.pending_registration = PendingRegistration("Pending User")
    bind = PendingTagBind(device_id=1, from_dashboard=True)
    h.ctx.pending_tag_bind = bind

    h.ctx.reader._event_queue.put(
        ReaderEvent(ReaderEventType.DISCONNECTED, "fake-reader")
    )

    assert h.wait_event("reader_disconnected") == {"event": "reader_disconnected"}
    assert h.ctx.admin_overlay_open is False
    assert h.ctx.pending_registration is None
    assert h.ctx.pending_tag_bind is bind
    assert h.client.get("/api/session").json()["active"] is False


def test_dev_status_reports_running_fake_reader(e2e):
    h = e2e()

    r = h.client.get("/api/dev/status")

    assert r.status_code == 200
    assert r.json()["fake_reader"] is True


def test_lan_client_cannot_inject_taps_or_read_session(lan):
    h = lan
    user_id = add_user(h, LAN_UID, display_name="LAN User")

    assert h.lan_client.post("/api/dev/tap", json={"uid": LAN_UID}).status_code == 403
    assert h.lan_client.get("/api/session").status_code == 403

    # The kiosk path is unaffected: a loopback tap still runs the full pipeline.
    h.tap(LAN_UID)
    payload = h.wait_event("auth_success")
    assert payload["user"]["id"] == user_id
    assert h.client.get("/api/session").json()["active"] is True

    # LAN stays locked out even while a kiosk session is live.
    assert h.lan_client.get("/api/session").status_code == 403
