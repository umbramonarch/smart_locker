"""
File: test_setup_first_admin.py
Description: First-boot Setup E2E. POST /api/setup arms a 60s card window;
             the next real tap through the FakeNFCReader -> bridge ->
             _dispatch_insert pipeline enrolls that card as the first admin
             and closes Setup. No approved-name list is consulted.
Project: smart_locker/tests/e2e
Notes: PendingRegistration.role == "admin" is what makes the enroll write an
       admin row; the SSE payload and the users table are the assertions.
"""

from smart_locker.database.models import UserRole
from smart_locker.database.repositories import UserRepository

from tests.e2e.helpers import uid_hmac_for

SETUP_CARD_UID = "04EE00000010"     # card tapped on the Setup screen


def test_setup_tap_enrolls_first_admin(e2e):
    """Empty DB -> Setup arms -> card tap -> admin row, Setup closed."""
    h = e2e()

    # The appliance is healthy and Setup is open on an empty database.
    assert h.client.get("/api/health").status_code == 200
    assert h.client.get("/api/setup").json() == {
        "needed": True,
        "secret_set": False,
    }

    r = h.client.post("/api/setup", json={"name": "First Admin"})
    assert r.status_code == 200
    assert r.json()["success"] is True
    assert h.ctx.pending_registration is not None
    assert h.ctx.pending_registration.role == "admin"

    h.tap(SETUP_CARD_UID)
    payload = h.wait_event("registration_success")
    assert payload["user"]["name"] == "First Admin"
    assert payload["user"]["role"] == "admin"
    assert h.ctx.pending_registration is None
    # The raw UID must not appear in the broadcast event.
    assert SETUP_CARD_UID not in str(payload)

    with h.db() as db:
        user = UserRepository.find_by_uid_hmac(db, uid_hmac_for(SETUP_CARD_UID))
        assert user is not None
        assert user.display_name == "First Admin"
        assert user.role == UserRole.ADMIN

    # One active admin exists: Setup is closed for good.
    assert h.client.get("/api/setup").json()["needed"] is False
    r = h.client.post("/api/setup", json={"name": "Late Admin"})
    assert r.status_code == 404

    # No leftover session/overlay: the fresh admin card logs in normally.
    assert h.client.get("/api/session").json()["active"] is False
    h.tap(SETUP_CARD_UID)
    login = h.wait_event("auth_success")
    assert login["user"]["name"] == "First Admin"


def test_setup_window_expires_and_tap_is_normal(e2e, monkeypatch):
    """An expired Setup window is dropped; the tap then classifies as a
    normal (unknown) card instead of enrolling an admin."""
    import smart_locker.api.app_context as app_context

    h = e2e()
    r = h.client.post("/api/setup", json={"name": "First Admin"})
    assert r.status_code == 200

    # Age the armed window past REGISTRATION_TIMEOUT_SECONDS.
    pending = h.ctx.pending_registration
    monkeypatch.setattr(
        pending, "created_at", pending.created_at - app_context.REGISTRATION_TIMEOUT_SECONDS - 1
    )

    h.tap(SETUP_CARD_UID)
    payload = h.wait_event("auth_failed")
    assert h.ctx.pending_registration is None

    with h.db() as db:
        assert UserRepository.find_by_uid_hmac(db, uid_hmac_for(SETUP_CARD_UID)) is None
        assert UserRepository.first_active_admin(db) is None
    assert h.client.get("/api/setup").json()["needed"] is True
