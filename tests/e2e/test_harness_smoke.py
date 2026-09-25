"""
File: test_harness_smoke.py
Description: Smoke tests proving the E2E harness works end-to-end: a simulated
             tap travels dev/tap -> fake reader -> NFC bridge -> tap_router ->
             session manager -> SSE, and lands in the shared test database.
Project: smart_locker/tests/e2e
"""

from tests.e2e.helpers import add_user

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
