"""
File: test_harness_smoke.py
Description: Smoke tests proving the E2E harness works end-to-end: a simulated
             tap travels dev/tap -> fake reader -> NFC bridge -> tap_router ->
             session manager -> SSE, and lands in the shared test database.
Project: smart_locker/tests/e2e
"""

from tests.e2e.helpers import add_device, add_user, get_device

CARD_UID = "04A1B2C3D4"


def test_tap_logs_in_and_broadcasts_auth_success(e2e):
    h = e2e()
    user_id = add_user(h, CARD_UID, display_name="Smoke User")

    h.tap(CARD_UID)

    payload = h.wait_event("auth_success")
    assert payload["user"]["id"] == user_id
    assert payload["user"]["name"] == "Smoke User"

    session = h.client.get("/api/session").json()
    assert session["active"] is True
    assert session["user"]["id"] == user_id


def test_second_tap_ends_session(e2e):
    h = e2e()
    add_user(h, CARD_UID)

    h.tap(CARD_UID)
    h.wait_event("auth_success")
    h.tap(CARD_UID)

    h.wait_event("session_ended")
    assert h.client.get("/api/session").json()["active"] is False


def test_public_borrow_limit_matches_kiosk_enforcement(e2e, monkeypatch):
    """The displayed limit is the one that refuses a second sticker borrow."""
    monkeypatch.setattr("config.settings.MAX_BORROWS", 1)
    monkeypatch.setattr("smart_locker.services.locker_service.MAX_BORROWS", 1)
    h = e2e()
    add_user(h, CARD_UID)
    first = add_device(h, pm_number="PM-101", tag_uid="AABBCC01")
    second = add_device(h, pm_number="PM-102", locker_slot=2, tag_uid="AABBCC02")

    config = h.client.get("/api/config")
    assert config.status_code == 200
    assert config.json()["max_borrows"] == 1

    h.tap(CARD_UID)
    h.wait_event("auth_success")
    h.tap("AABBCC01")
    assert h.wait_event("device_action")["success"] is True
    h.tap("AABBCC02")
    refused = h.wait_event("device_action")
    assert refused["success"] is False
    assert refused["action"] == "refused"
    assert get_device(h, first).current_borrower_id is not None
    assert get_device(h, second).current_borrower_id is None


def test_borrowed_device_records_match_across_kiosk_and_dashboard(e2e):
    """Both clients see the same status; only kiosk labels its own loan 'You'."""
    h = e2e()
    user_id = add_user(h, CARD_UID, display_name="Record Borrower")
    device_id = add_device(h, pm_number="PM-103", tag_uid="AABBCC03")

    h.tap(CARD_UID)
    h.wait_event("auth_success")
    h.tap("AABBCC03")
    assert h.wait_event("device_action")["success"] is True

    kiosk = h.client.get("/api/devices")
    dashboard = h.client.get("/api/dashboard/devices")
    assert kiosk.status_code == dashboard.status_code == 200
    own = next(row for row in kiosk.json() if row["id"] == device_id)
    public = next(row for row in dashboard.json() if row["pm_number"] == "PM-103")
    assert own["status"] == public["status"] == "borrowed"
    assert own["has_tag"] is public["has_tag"] is True
    assert own["borrower_name"] == "You"
    assert public["borrower_name"] == "Record Borrower"
    assert "tag_hmac" not in own and "tag_hmac" not in public
    assert "image_path" not in public
    assert get_device(h, device_id).current_borrower_id == user_id
