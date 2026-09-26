"""
File: test_dashboard_transactions.py
Description: End-to-end dashboard audit-feed coverage through the real app.
Project: smart_locker/tests/e2e
Notes: Seeds an audit trail in the harness database and verifies the protected
       HTTP route maps eagerly loaded borrower, device, and admin relationships.
"""

from smart_locker.database.repositories import TransactionRepository
from tests.e2e.helpers import add_device, add_user


DASHBOARD_HEADER = "X-Smart-Locker-Admin"
DASHBOARD_SECRET = "dashboard-audit-secret"


def test_dashboard_transactions_returns_populated_relation_json(e2e, monkeypatch):
    """The protected audit feed maps borrower, device, and performer details."""
    monkeypatch.setenv("SMART_LOCKER_DASHBOARD_ADMIN_SECRET", DASHBOARD_SECRET)
    h = e2e()
    borrower_id = add_user(h, "0A30000001", display_name="Audit Borrower")
    performer_id = add_user(h, "0A30000002", display_name="Audit Admin", role="admin")
    device_id = add_device(
        h,
        name="Audit Scope",
        pm_number="PM-AUDIT-1",
        locker_slot=31,
    )

    with h.db() as db:
        TransactionRepository.log_borrow(db, borrower_id, device_id, notes="checked out")
        TransactionRepository.log_return(
            db,
            borrower_id,
            device_id,
            notes="returned by admin",
            performed_by_id=performer_id,
        )
        db.commit()

    response = h.client.get(
        "/api/dashboard/transactions",
        headers={DASHBOARD_HEADER: DASHBOARD_SECRET},
    )

    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 2
    returned = next(row for row in rows if row["transaction_type"] == "return")
    assert returned["timestamp"] is not None
    assert returned["user_name"] == "Audit Borrower"
    assert returned["device_name"] == "Audit Scope"
    assert returned["performed_by"] == "Audit Admin"
    assert returned["notes"] == "returned by admin"
