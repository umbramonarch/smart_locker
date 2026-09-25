"""
File: test_tap_devices.py
Description: End-to-end device-tag tap coverage through the real NFC bridge.
             Boots create_app() via the ``e2e`` factory — no AppContext mocks —
             so every assertion rides dev/tap -> FakeNFCReader -> bridge loop
             -> tap_router -> SessionManager/LockerService -> SSE + SQLite.
Project: smart_locker/tests/e2e
Notes: Each test gets a fresh in-memory StaticPool DB via the ``e2e`` fixture.
       Taps are asynchronous: always wait_event() for the SSE that proves the
       bridge finished before asserting DB state.
"""

from smart_locker.database.models import Device, DeviceStatus, TransactionType
from smart_locker.database.repositories import TransactionRepository
from tests.e2e.helpers import add_device, add_user, get_device

# Simulated UIDs — hex-like strings, unique within each test. The DB is fresh
# per test, so constants can repeat safely across tests.
CARD_USER = "0A10000001"
CARD_A = "0A1000000A"
CARD_B = "0A1000000B"
CARD_HOLDER = "0A1000000C"
TAG_AVAILABLE = "0A20000001"
TAG_BORROW = "0A20000002"
TAG_OWNED = "0A20000003"
TAG_UNATTENDED = "0A20000004"
TAG_HANDOVER = "0A20000005"
TAG_OVERLAY = "0A20000006"
TAG_MAINTENANCE = "0A20000007"


def _login(h, card_uid: str) -> dict:
    """Tap a work card and wait for the session to open."""
    h.tap(card_uid)
    return h.wait_event("auth_success")


def _mark_borrowed(h, device_id: int, borrower_id: int) -> None:
    """Seed a device as BORROWED by ``borrower_id`` directly in the shared DB."""
    with h.db() as db:
        device = db.get(Device, device_id)
        device.status = DeviceStatus.BORROWED
        device.current_borrower_id = borrower_id
        db.commit()


def _device_txns(h, device_id: int) -> list[dict]:
    """TransactionLog rows for one device, most recent first, as plain dicts."""
    with h.db() as db:
        rows = TransactionRepository.get_device_history(db, device_id)
        return [
            {
                "user_id": t.user_id,
                "type": t.transaction_type,
                "notes": t.notes,
                "performed_by_id": t.performed_by_id,
            }
            for t in rows
        ]


def test_idle_tap_on_available_tag_shows_hint_only(e2e):
    """Idle kiosk + available sticker: hint SSE, no session, no borrow."""
    h = e2e()
    device_id = add_device(
        h, name="Fluke 87V", pm_number="PM-9001", locker_slot=21,
        tag_uid=TAG_AVAILABLE,
    )

    h.tap(TAG_AVAILABLE)

    payload = h.wait_event("device_tag_idle")
    assert payload["message"] == "Tap your work card first."

    assert h.client.get("/api/session").json()["active"] is False
    device = get_device(h, device_id)
    assert device.status == DeviceStatus.AVAILABLE
    assert device.current_borrower_id is None
    assert _device_txns(h, device_id) == []


def test_session_tap_on_available_tag_borrows_device(e2e):
    """Logged-in + available sticker: auto-intent borrow, DB and audit row."""
    h = e2e()
    user_id = add_user(h, CARD_USER, display_name="Borrower B")
    device_id = add_device(
        h, name="Scope", pm_number="PM-9002", locker_slot=22, tag_uid=TAG_BORROW
    )

    _login(h, CARD_USER)
    h.tap(TAG_BORROW)

    payload = h.wait_event("device_action")
    assert payload["success"] is True
    assert payload["action"] == "borrow"
    assert payload["message"] == "Scope borrowed."
    assert payload["device_id"] == device_id
    assert payload["device_name"] == "Scope"
    assert payload["locker_slot"] == 22

    device = get_device(h, device_id)
    assert device.status == DeviceStatus.BORROWED
    assert device.current_borrower_id == user_id

    txns = _device_txns(h, device_id)
    assert len(txns) == 1
    assert txns[0]["type"] == TransactionType.BORROW
    assert txns[0]["user_id"] == user_id
    assert txns[0]["performed_by_id"] is None

    # Auto-intent keeps the kiosk session open for the next tap.
    assert h.client.get("/api/session").json()["active"] is True


def test_session_tap_on_own_borrowed_tag_returns_device(e2e):
    """Second tap of a sticker on a device the same user holds returns it."""
    h = e2e()
    user_id = add_user(h, CARD_USER, display_name="Borrower B")
    device_id = add_device(
        h, name="Camera", pm_number="PM-9003", locker_slot=23, tag_uid=TAG_OWNED
    )

    _login(h, CARD_USER)
    h.tap(TAG_OWNED)
    borrow = h.wait_event("device_action")
    assert borrow["action"] == "borrow"

    h.tap(TAG_OWNED)

    returned = h.wait_event("device_action")
    assert returned["success"] is True
    assert returned["action"] == "return"
    assert returned["message"] == "Camera returned."
    assert returned["device_id"] == device_id
    assert returned["locker_slot"] == 23

    device = get_device(h, device_id)
    assert device.status == DeviceStatus.AVAILABLE
    assert device.current_borrower_id is None

    txns = _device_txns(h, device_id)
    assert [t["type"] for t in txns] == [
        TransactionType.RETURN,
        TransactionType.BORROW,
    ]
    assert all(t["user_id"] == user_id for t in txns)
    assert all(t["performed_by_id"] is None for t in txns)


def test_idle_tap_on_borrowed_tag_returns_unattended(e2e):
    """Idle kiosk + sticker on a borrowed device: cardless unattended return."""
    h = e2e()
    user_id = add_user(h, CARD_HOLDER, display_name="Away User")
    device_id = add_device(
        h, name="Drill", pm_number="PM-9004", locker_slot=24,
        tag_uid=TAG_UNATTENDED,
    )
    _mark_borrowed(h, device_id, user_id)

    # No work-card login — the kiosk is idle when the sticker is tapped.
    h.tap(TAG_UNATTENDED)

    payload = h.wait_event("device_action")
    assert payload["success"] is True
    assert payload["action"] == "return"
    assert payload["message"] == "Drill returned."
    assert payload["device_id"] == device_id
    assert payload["locker_slot"] == 24

    device = get_device(h, device_id)
    assert device.status == DeviceStatus.AVAILABLE
    assert device.current_borrower_id is None
    assert h.client.get("/api/session").json()["active"] is False

    txns = _device_txns(h, device_id)
    assert len(txns) == 1
    # The original borrower stays on the log; notes record no card was shown.
    assert txns[0]["type"] == TransactionType.RETURN
    assert txns[0]["user_id"] == user_id
    assert txns[0]["notes"] == "returned at kiosk without card"


def test_tap_on_other_users_device_requests_handover_then_transfer(e2e):
    """User B taps a tag held by A: handover SSE, then POST transfer succeeds."""
    h = e2e()
    user_a = add_user(h, CARD_A, display_name="User A")
    user_b = add_user(h, CARD_B, display_name="User B")
    device_id = add_device(
        h, name="Analyzer", pm_number="PM-9005", locker_slot=25,
        tag_uid=TAG_HANDOVER,
    )
    _mark_borrowed(h, device_id, user_a)

    _login(h, CARD_B)
    h.tap(TAG_HANDOVER)

    payload = h.wait_event("handover_requested")
    assert payload["device_id"] == device_id
    assert payload["device_name"] == "Analyzer"
    assert payload["current_holder_id"] == user_a
    assert payload["current_holder_name"] == "User A"
    assert payload["user_id"] == user_b
    assert payload["user_name"] == "User B"

    # The kiosk confirms the handover via the transfer endpoint (B's session).
    resp = h.client.post(f"/api/devices/{device_id}/transfer")
    assert resp.status_code == 200
    assert resp.json() == {
        "success": True,
        "message": "Analyzer transferred to you.",
    }

    device = get_device(h, device_id)
    assert device.status == DeviceStatus.BORROWED
    assert device.current_borrower_id == user_b

    # One return row for A and one borrow row for B preserve the audit trail.
    txns = _device_txns(h, device_id)
    assert len(txns) == 2
    by_type = {t["type"]: t for t in txns}
    assert by_type[TransactionType.RETURN]["user_id"] == user_a
    assert by_type[TransactionType.RETURN]["notes"] == "transferred to User B"
    assert by_type[TransactionType.BORROW]["user_id"] == user_b
    assert by_type[TransactionType.BORROW]["notes"] == "transferred from User A"


def test_admin_overlay_suppresses_device_auto_intent(e2e):
    """Overlay open: sticker tap shows a hint instead of borrowing/returning."""
    h = e2e()
    add_user(h, CARD_USER, display_name="Borrower B")
    device_id = add_device(
        h, name="Suppressed", pm_number="PM-9006", locker_slot=26,
        tag_uid=TAG_OVERLAY,
    )

    _login(h, CARD_USER)
    h.ctx.admin_overlay_open = True
    assert h.client.get("/api/session").json()["overlay"] is True

    h.tap(TAG_OVERLAY)

    payload = h.wait_event("device_tag_idle")
    assert (
        payload["message"]
        == "Close ADMIN MODE or use Borrow to check out a device."
    )
    h.assert_no_event("device_action", within=1.0)

    device = get_device(h, device_id)
    assert device.status == DeviceStatus.AVAILABLE
    assert _device_txns(h, device_id) == []

    # The next work-card tap still ends the session and clears the flag.
    h.tap(CARD_USER)
    h.wait_event("session_ended")
    assert h.ctx.admin_overlay_open is False
    assert h.client.get("/api/session").json()["active"] is False


def test_session_tap_on_maintenance_tag_reports_borrow_failure(e2e):
    """MAINTENANCE is not borrowable: session tap emits a failed device_action."""
    h = e2e()
    add_user(h, CARD_USER, display_name="Borrower B")
    device_id = add_device(
        h, name="In Repair", pm_number="PM-9007", locker_slot=27,
        tag_uid=TAG_MAINTENANCE, status="maintenance",
    )

    _login(h, CARD_USER)
    h.tap(TAG_MAINTENANCE)

    payload = h.wait_event("device_action")
    assert payload["success"] is False
    assert payload["action"] == "borrow"
    assert payload["message"] == "Could not borrow In Repair."
    assert payload["device_id"] == device_id
    assert payload["locker_slot"] == 27

    device = get_device(h, device_id)
    assert device.status == DeviceStatus.MAINTENANCE
    assert device.current_borrower_id is None
    assert _device_txns(h, device_id) == []

    # The failed action leaves the kiosk session open.
    assert h.client.get("/api/session").json()["active"] is True
