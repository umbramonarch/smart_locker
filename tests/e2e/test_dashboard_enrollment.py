"""
File: test_dashboard_enrollment.py
Description: End-to-end tests for the dashboard-armed user enrollment flow:
             POST /api/dashboard/users/register arms a 60s window, a real
             (fake-reader) card tap enrolls, and the public token status
             endpoint reflects pending/success/failed/cancelled. The kiosk
             cancel path must not clear a dashboard-armed window.
Project: smart_locker/tests/e2e
Notes: Run with: python -m pytest tests/e2e/test_dashboard_enrollment.py -v
       Uses the shared fake-reader E2E harness (context fixture).
"""
from tests.e2e.helpers import add_device, add_user, uid_hmac_for
from smart_locker.database.repositories import UserRepository
from smart_locker.security.encryption import decrypt
from smart_locker.security.key_manager import key_manager

NEW_CARD_UID = "0707AAAA0001"
ENROLLED_UID = "0A0A0A0A0A01"
TAG_UID = "0B0B0B0B0B01"


def _arm(h, name="Dashboard Hire"):
    r = h.client.post(
        "/api/dashboard/users/register", json={"display_name": name}
    )
    assert r.status_code == 200, r.text
    return r.json()["enrollment_id"]


def test_dashboard_arm_then_card_tap_enrolls(e2e):
    """Arm via dashboard API -> fake card tap -> user row + success status."""
    h = e2e()
    token = _arm(h)

    # Nothing exists until the physical tap.
    with h.db() as db:
        assert UserRepository.list_all(db) == []
    status = h.client.get(
        f"/api/dashboard/users/register/{token}"
    ).json()
    assert status["state"] == "pending"
    # Public status carries no card identity material.
    for bad in ("uid_hmac", "encrypted_card_uid", "tag_hmac", NEW_CARD_UID):
        assert bad not in status

    h.tap(NEW_CARD_UID)
    payload = h.wait_event("registration_success")
    assert payload["user"]["name"] == "Dashboard Hire"
    assert payload["user"]["role"] == "user"

    status = h.client.get(
        f"/api/dashboard/users/register/{token}"
    ).json()
    assert status["state"] == "success"
    assert status["user_id"] == payload["user"]["id"]

    with h.db() as db:
        user = UserRepository.find_by_uid_hmac(db, uid_hmac_for(NEW_CARD_UID))
        assert user is not None
        assert user.display_name == "Dashboard Hire"
        assert user.role.value == "user"


def test_dashboard_arm_tap_enrolled_card_fails(e2e):
    """A card already enrolled (even inactive) cannot re-enroll."""
    h = e2e()
    existing = add_user(h, ENROLLED_UID, display_name="Taken Card")
    token = _arm(h, "Another Name")

    h.tap(ENROLLED_UID)
    payload = h.wait_event("registration_failed")
    assert "already registered" in payload["reason"].lower()

    status = h.client.get(
        f"/api/dashboard/users/register/{token}"
    ).json()
    assert status["state"] == "failed"
    with h.db() as db:
        assert len(UserRepository.list_all(db)) == 1


def test_dashboard_arm_tap_inactive_card_fails(e2e):
    """Soft-removed cards stay enrolled — the tap refuses cleanly."""
    h = e2e()
    uid = add_user(h, ENROLLED_UID, display_name="Gone User")
    with h.db() as db:
        u = UserRepository.find_by_id(db, uid)
        u.is_active = False
        db.commit()
    token = _arm(h, "Fresh Name")
    h.tap(ENROLLED_UID)
    payload = h.wait_event("registration_failed")
    assert "already registered" in payload["reason"].lower()


def test_dashboard_arm_tap_device_tag_fails(e2e):
    """A bound sticker cannot enroll as a work card."""
    h = e2e()
    add_device(h, name="Tagged Cam", pm_number="PM-TAG", tag_uid=TAG_UID)
    token = _arm(h)
    h.tap(TAG_UID)
    payload = h.wait_event("registration_failed")
    assert "bound to a device" in payload["reason"].lower()


def test_dashboard_arm_cancel_before_tap(e2e):
    """Cancelling by token clears the window; a later tap does nothing."""
    h = e2e()
    token = _arm(h)
    out = h.client.post(f"/api/dashboard/users/register/{token}/cancel")
    assert out.json()["state"] == "cancelled"
    assert h.ctx.pending_registration is None

    h.tap(NEW_CARD_UID)
    # Tap now flows as an unknown card — never an enrollment.
    h.assert_no_event("registration_success", within=2.0)
    with h.db() as db:
        assert UserRepository.list_all(db) == []


def test_dashboard_arm_conflicts_with_session_and_bind(e2e):
    """Active kiosk session or pending bind refuses the arm with 409."""
    h = e2e()
    add_user(h, ENROLLED_UID, display_name="Admin One", role="admin")
    h.tap(ENROLLED_UID)
    h.wait_event("auth_success")

    r = h.client.post(
        "/api/dashboard/users/register", json={"display_name": "Late"}
    )
    assert r.status_code == 409
    assert h.ctx.pending_registration is None

    # End the session; arm a dashboard bind; the register arm then 409s.
    h.client.post("/api/session/end")
    d = add_device(h, name="Bind Me", pm_number="PM-BIND", tag_uid=None)
    r = h.client.post("/api/dashboard/bind-tag", json={"pm_number": "PM-BIND"})
    assert r.status_code == 200, r.text
    r = h.client.post(
        "/api/dashboard/users/register", json={"display_name": "Late"}
    )
    assert r.status_code == 409


def test_kiosk_cancel_preserves_dashboard_window(e2e):
    """POST /api/register/cancel cannot clear a dashboard-armed window."""
    h = e2e()
    token = _arm(h)
    r = h.client.post("/api/register/cancel")
    assert r.status_code == 200
    assert h.ctx.pending_registration is not None
    assert h.ctx.pending_registration.enrollment_id == token


def test_removed_user_card_cannot_login(e2e):
    """Soft-remove via the public endpoint -> the physical card denies auth.

    The tap must surface auth_failed, no session opens, and the user row +
    enrollment card binding survive in SQLite (soft delete keeps history).
    """
    h = e2e()
    uid = add_user(h, ENROLLED_UID, display_name="Soon Gone")

    # A real login works first — baseline.
    h.tap(ENROLLED_UID)
    assert h.wait_event("auth_success")["user"]["id"] == uid
    h.client.post("/api/session/end")

    r = h.client.post(f"/api/dashboard/users/{uid}/remove")
    assert r.status_code == 200
    assert r.json()["removed"] is True

    # The card now fails auth — no session is created.
    h.tap(ENROLLED_UID)
    h.wait_event("auth_failed")
    assert h.client.get("/api/session").json()["active"] is False

    with h.db() as db:
        user = UserRepository.find_by_uid_hmac(db, uid_hmac_for(ENROLLED_UID))
        assert user is not None           # row + card digest preserved
        assert user.is_active is False    # soft-removed only
        assert user.display_name == "Soon Gone"


def test_dashboard_enrollment_success_only_after_commit(e2e):
    """The success status must reflect a committed row — not a pre-commit
    optimistic write. After the tap, the token status and the DB agree."""
    h = e2e()
    token = _arm(h, "Committed User")
    h.tap(NEW_CARD_UID)
    h.wait_event("registration_success")
    status = h.client.get(
        f"/api/dashboard/users/register/{token}"
    ).json()
    assert status["state"] == "success"
    with h.db() as db:
        u = UserRepository.find_by_id(db, status["user_id"])
        assert u is not None and u.is_active
        # The stored card binding matches the tapped UID (HMAC + AES).
        assert u.uid_hmac == uid_hmac_for(NEW_CARD_UID)
        # AES-256-GCM blob decrypts back to the tapped card UID.
        assert decrypt(u.encrypted_card_uid, key_manager.enc_key) \
            == NEW_CARD_UID
        # Public status never leaks identity material.
        for bad in ("uid_hmac", "encrypted_card_uid", NEW_CARD_UID, u.uid_hmac[:12]):
            assert bad not in str(status)
