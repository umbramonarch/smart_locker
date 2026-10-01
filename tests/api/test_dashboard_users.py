"""
File: test_dashboard_users.py
Description: Tests for POST /api/dashboard/users/{user_id}/name — the public
             user rename endpoint — plus the stable ``id`` field on
             GET /api/dashboard/users.
Project: smart_locker/tests/api
Notes: Run with: python -m pytest tests/api/test_dashboard_users.py -v
"""
from smart_locker.api.app_context import AppContext
from smart_locker.database.models import DeviceStatus, UserRole
from smart_locker.database.repositories import (
    DeviceRepository,
    TransactionRepository,
    UserRepository,
)
from tests.api.helpers import catalog_workbook


def _rename(client, user_id, payload):
    return client.post(f"/api/dashboard/users/{user_id}/name", json=payload)


class TestUserRename:
    """Rename validation, persistence, and session write-through."""

    def test_rename_success_trims(
        self, client, lan_client, db_session, test_user
    ):
        """LAN + loopback both work; whitespace trims; DB and list update."""
        db_session.commit()
        resp = _rename(lan_client, test_user.id, {"display_name": "  Ada Lovelace  "})
        assert resp.status_code == 200
        assert resp.json() == {
            "ok": True, "id": test_user.id, "display_name": "Ada Lovelace",
        }
        db_session.expire_all()
        assert UserRepository.find_by_id(db_session, test_user.id).display_name == "Ada Lovelace"
        users = client.get("/api/dashboard/users").json()
        row = next(u for u in users if u["id"] == test_user.id)
        assert row["display_name"] == "Ada Lovelace"
        # No private identity fields on the public payload.
        assert "uid_hmac" not in row
        assert "encrypted_card_uid" not in row
        owners = client.get("/api/dashboard/owners").json()
        assert "Ada Lovelace" in owners["names"]

    def test_rename_preserves_identity_fields(
        self, client, db_session, test_user, test_devices
    ):
        """uid/hmac/encrypted/role/activity/loan/transaction ids are unchanged."""
        DeviceRepository.borrow(db_session, test_devices[0], test_user.id)
        txn = TransactionRepository.log_borrow(
            db_session, test_user.id, test_devices[0].id
        )
        db_session.commit()
        before = {
            "uid_hmac": test_user.uid_hmac,
            "encrypted_card_uid": test_user.encrypted_card_uid,
            "role": test_user.role,
            "is_active": test_user.is_active,
        }

        resp = _rename(client, test_user.id, {"display_name": "Renamed"})
        assert resp.status_code == 200

        db_session.expire_all()
        user = UserRepository.find_by_id(db_session, test_user.id)
        for key, value in before.items():
            assert getattr(user, key) == value
        db_session.expire_all()
        device = DeviceRepository.find_by_id(db_session, test_devices[0].id)
        assert device.current_borrower_id == test_user.id
        assert device.status == DeviceStatus.BORROWED
        # The borrowed device now reports the new borrower name on the dashboard.
        listed = client.get("/api/dashboard/devices").json()
        row = next(d for d in listed if d["pm_number"] == "PM-001")
        assert row["borrower_name"] == "Renamed"
        txns = client.get("/api/dashboard/transactions").json()
        assert txns[0]["user_name"] == "Renamed"
        assert TransactionRepository.get_device_history(
            db_session, test_devices[0].id
        )[0].id == txn.id

    def test_rename_updates_live_kiosk_session(
        self, client, mock_context, db_session, test_user
    ):
        """A matching active session's cached label updates; no logout."""
        db_session.commit()
        mock_context.session_mgr.start_session(test_user)
        resp = _rename(client, test_user.id, {"display_name": "In Session"})
        assert resp.status_code == 200
        session = mock_context.session_mgr.current_session
        assert session is not None
        assert session.user.display_name == "In Session"
        assert session.user.role == UserRole.USER

    def test_rename_leaves_other_sessions(
        self, client, mock_context, db_session, test_user, admin_user
    ):
        """Renaming a different user does not touch the active session."""
        db_session.commit()
        mock_context.session_mgr.start_session(admin_user)
        resp = _rename(client, test_user.id, {"display_name": "Someone Else"})
        assert resp.status_code == 200
        assert mock_context.session_mgr.current_session.user.display_name == "Admin User"

    def test_rename_empty_and_whitespace_422(self, client, test_user):
        assert _rename(client, test_user.id, {"display_name": ""}).status_code == 422
        assert _rename(client, test_user.id, {"display_name": "   "}).status_code == 422

    def test_rename_over_100_and_non_string_422(self, client, db_session, test_user):
        assert _rename(client, test_user.id, {"display_name": "x" * 101}).status_code == 422
        # Over-100 raw input that trims inside the range is accepted.
        assert _rename(client, test_user.id, {"display_name": "  " + "x" * 99 + " "}).status_code == 200
        # Non-string JSON never becomes a name.
        resp = _rename(client, test_user.id, {"display_name": 123})
        assert resp.status_code == 422
        db_session.expire_all()
        assert UserRepository.find_by_id(db_session, test_user.id).display_name == "x" * 99

    def test_rename_extra_field_forbidden_422(self, client, test_user):
        """role/is_active injection is forbidden — rename is name-only."""
        resp = _rename(
            client, test_user.id,
            {"display_name": "Ada", "role": "admin"},
        )
        assert resp.status_code == 422
        assert _rename(
            client, test_user.id, {"display_name": "Ada", "is_active": False}
        ).status_code == 422

    def test_rename_missing_user_404(self, client, test_user):
        assert _rename(client, test_user.id + 999, {"display_name": "Ghost"}).status_code == 404

    def test_rename_duplicate_name_409(
        self, client, db_session, test_user, admin_user
    ):
        """Case-insensitive collision with another user is refused."""
        db_session.commit()
        resp = _rename(client, test_user.id, {"display_name": "admin user"})
        assert resp.status_code == 409
        db_session.expire_all()
        assert UserRepository.find_by_id(db_session, test_user.id).display_name == "Test User"

    def test_rename_self_same_name_ok(self, client, test_user):
        """Renaming to the same name (any case) is a no-op success."""
        assert _rename(client, test_user.id, {"display_name": "Test User"}).status_code == 200
        assert _rename(client, test_user.id, {"display_name": "test user"}).status_code == 200

    def test_rename_schedules_workbook_writeback(
        self, client, db_session, test_user, test_devices, tmp_path, monkeypatch
    ):
        """A borrowed unit's Location cell picks up the new name on flush."""
        from smart_locker.sync.location_writeback import flush_scheduled_writeback

        path = catalog_workbook(tmp_path, [
            ["Equipment", "Name", "Location"],
            ["PM-001", "Camera", "Test User"],
        ])
        monkeypatch.setattr("config.settings.SOURCE_EXCEL_PATH", str(path))
        DeviceRepository.borrow(db_session, test_devices[0], test_user.id)
        db_session.commit()

        resp = _rename(client, test_user.id, {"display_name": "New Owner"})
        assert resp.status_code == 200
        flush_scheduled_writeback()

        from openpyxl import load_workbook
        ws = load_workbook(path).active
        assert ws.cell(row=2, column=3).value == "New Owner"

    def test_rename_succeeds_when_share_down(
        self, client, db_session, test_user, monkeypatch
    ):
        """A missing workbook cannot fail the DB rename — write-back defers."""
        monkeypatch.setattr(
            "config.settings.SOURCE_EXCEL_PATH", "/nonexistent/share/device-list.xlsx"
        )
        db_session.commit()
        resp = _rename(client, test_user.id, {"display_name": "Offline Rename"})
        assert resp.status_code == 200
        db_session.expire_all()
        assert UserRepository.find_by_id(
            db_session, test_user.id
        ).display_name == "Offline Rename"


def _set_role(client, user_id, payload):
    return client.post(f"/api/dashboard/users/{user_id}/role", json=payload)


class TestUserRole:
    """Role change: public, role-only, last-active-admin guard, session cut."""

    def test_promote_and_demote_lan_and_loopback(
        self, client, lan_client, db_session, admin_user, test_user
    ):
        """LAN promotes, loopback demotes — another active admin exists."""
        db_session.commit()
        resp = _set_role(lan_client, test_user.id, {"role": "admin"})
        assert resp.status_code == 200
        assert resp.json() == {
            "ok": True, "id": test_user.id, "role": "admin"
        }
        db_session.expire_all()
        assert UserRepository.find_by_id(db_session, test_user.id).role == UserRole.ADMIN

        resp = _set_role(client, test_user.id, {"role": "user"})
        assert resp.status_code == 200
        db_session.expire_all()
        assert UserRepository.find_by_id(db_session, test_user.id).role == UserRole.USER

    def test_role_change_preserves_identity_and_history(
        self, client, db_session, test_user, admin_user, test_devices
    ):
        """Name/activity/card/loan/history survive; response omits identity."""
        DeviceRepository.borrow(db_session, test_devices[0], test_user.id)
        txn = TransactionRepository.log_borrow(
            db_session, test_user.id, test_devices[0].id
        )
        db_session.commit()
        before = {
            "display_name": test_user.display_name,
            "uid_hmac": test_user.uid_hmac,
            "encrypted_card_uid": test_user.encrypted_card_uid,
            "is_active": test_user.is_active,
        }
        resp = _set_role(client, test_user.id, {"role": "admin"})
        assert resp.status_code == 200
        assert "uid_hmac" not in resp.json()
        assert "encrypted_card_uid" not in resp.json()
        db_session.expire_all()
        user = UserRepository.find_by_id(db_session, test_user.id)
        for k, v in before.items():
            assert getattr(user, k) == v
        assert user.borrowed_devices
        history = TransactionRepository.get_device_history(
            db_session, test_devices[0].id
        )
        assert [tx.id for tx in history] == [txn.id]

    def test_role_unknown_user_404(self, client):
        assert _set_role(client, 9999, {"role": "admin"}).status_code == 404

    def test_role_invalid_and_extra_field_422(self, client, test_user):
        db = _set_role(client, test_user.id, {"role": "superuser"})
        assert db.status_code == 422
        extra = _set_role(
            client, test_user.id, {"role": "admin", "is_active": False}
        )
        assert extra.status_code == 422

    def test_last_active_admin_demotion_409(
        self, client, db_session, admin_user, mock_context
    ):
        """The only active admin cannot demote themselves."""
        db_session.commit()
        resp = _set_role(client, admin_user.id, {"role": "user"})
        assert resp.status_code == 409
        db_session.expire_all()
        assert UserRepository.find_by_id(
            db_session, admin_user.id
        ).role == UserRole.ADMIN

    def test_inactive_admin_does_not_protect_last_active(
        self, client, db_session, admin_user
    ):
        """Inactive admins don't count: last ACTIVE admin still 409s."""
        inactive = UserRepository.create(
            db_session, display_name="Old Admin", uid_hmac="old-hmac",
            encrypted_card_uid="e", role="admin",
        )
        inactive.is_active = False
        db_session.commit()
        resp = _set_role(client, admin_user.id, {"role": "user"})
        assert resp.status_code == 409
        db_session.expire_all()
        assert UserRepository.find_by_id(
            db_session, admin_user.id
        ).role == UserRole.ADMIN

    def test_inactive_admin_demotion_allowed(
        self, client, db_session, admin_user
    ):
        """Demoting an INACTIVE admin is fine — the active one remains."""
        inactive = UserRepository.create(
            db_session, display_name="Old Admin", uid_hmac="old-hmac",
            encrypted_card_uid="e", role="admin",
        )
        inactive.is_active = False
        db_session.commit()
        resp = _set_role(client, inactive.id, {"role": "user"})
        assert resp.status_code == 200
        db_session.expire_all()
        assert UserRepository.find_by_id(
            db_session, inactive.id
        ).role == UserRole.USER

    def test_noop_same_role_keeps_session(
        self, client, db_session, mock_context, admin_user, test_user
    ):
        """Idempotent set must not end an active kiosk session."""
        mock_context.session_mgr.start_session(admin_user)
        db_session.commit()
        resp = _set_role(client, admin_user.id, {"role": "admin"})
        assert resp.status_code == 200
        assert mock_context.session_mgr.current_session is not None

    def test_role_change_ends_matching_session(
        self, client, db_session, mock_context, admin_user, test_user
    ):
        """An actual role change cuts the changed user's kiosk session."""
        test_user.role = UserRole.ADMIN
        mock_context.session_mgr.start_session(test_user)
        mock_context.admin_overlay_open = True
        from smart_locker.api.app_context import (
            PendingTagBind,
            assign_pending_tag_bind,
        )
        assign_pending_tag_bind(mock_context, PendingTagBind(device_id=1))
        db_session.commit()
        resp = _set_role(client, test_user.id, {"role": "user"})
        assert resp.status_code == 200
        assert mock_context.session_mgr.current_session is None
        assert mock_context.admin_overlay_open is False
        assert mock_context.pending_tag_bind is None

    def test_role_change_leaves_other_sessions(
        self, client, db_session, mock_context, admin_user, test_user
    ):
        """Changing a logged-out user's role keeps the active session."""
        test_user.role = UserRole.ADMIN
        mock_context.session_mgr.start_session(admin_user)
        db_session.commit()
        resp = _set_role(client, test_user.id, {"role": "user"})
        assert resp.status_code == 200
        sess = mock_context.session_mgr.current_session
        assert sess is not None and sess.user.id == admin_user.id


def _remove(client, user_id):
    return client.post(f"/api/dashboard/users/{user_id}/remove")


class TestUserRemove:
    """Soft-remove: keep rows/history; last-admin and borrower guards."""

    def test_remove_marks_inactive_keeps_everything(
        self, client, db_session, test_user, test_devices
    ):
        """is_active=False only — card, name, loans, history all preserved."""
        DeviceRepository.borrow(db_session, test_devices[1], test_user.id)
        db_session.commit()
        # Return it so removal is allowed, leaving real history behind.
        DeviceRepository.return_device(db_session, test_devices[1])
        TransactionRepository.log_return(
            db_session, test_user.id, test_devices[1].id
        )
        db_session.commit()
        hmac_before = test_user.uid_hmac
        resp = _remove(client, test_user.id)
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "id": test_user.id, "removed": True}
        db_session.expire_all()
        user = UserRepository.find_by_id(db_session, test_user.id)
        assert user.is_active is False
        assert user.uid_hmac == hmac_before
        assert user.display_name == test_user.display_name
        history = TransactionRepository.get_device_history(
            db_session, test_devices[1].id
        )
        assert len(history) == 1

    def test_remove_missing_user_404(self, client):
        assert _remove(client, 9999).status_code == 404

    def test_remove_inactive_is_idempotent(
        self, client, db_session, test_user
    ):
        test_user.is_active = False
        db_session.commit()
        resp = _remove(client, test_user.id)
        assert resp.status_code == 200
        assert resp.json()["removed"] is True

    def test_remove_last_active_admin_409(
        self, client, db_session, admin_user
    ):
        db_session.commit()
        resp = _remove(client, admin_user.id)
        assert resp.status_code == 409
        assert "last active admin" in resp.json()["detail"]
        db_session.expire_all()
        assert UserRepository.find_by_id(
            db_session, admin_user.id
        ).is_active is True

    def test_remove_borrower_409(
        self, client, db_session, test_user, test_devices
    ):
        """A user holding a borrowed unit must return it first."""
        DeviceRepository.borrow(db_session, test_devices[0], test_user.id)
        db_session.commit()
        resp = _remove(client, test_user.id)
        assert resp.status_code == 409
        assert "borrowed" in resp.json()["detail"].lower()
        db_session.expire_all()
        assert UserRepository.find_by_id(
            db_session, test_user.id
        ).is_active is True

    def test_remove_ends_matching_kiosk_session(
        self, client, db_session, mock_context, test_user
    ):
        mock_context.session_mgr.start_session(test_user)
        db_session.commit()
        assert _remove(client, test_user.id).status_code == 200
        assert mock_context.session_mgr.current_session is None

    def test_inactive_user_cannot_borrow_through_stale_session(
        self, db_session, test_user, test_devices
    ):
        """The borrow boundary rechecks DB activity, not just the session copy."""
        from copy import copy

        from smart_locker.auth.session_manager import UserSession
        from smart_locker.services.locker_service import LockerService

        stale_user = copy(test_user)
        stale_user.is_active = True
        test_user.is_active = False
        db_session.commit()

        outcome = LockerService.borrow_device(
            db_session, UserSession(user=stale_user), test_devices[0].id
        )
        assert outcome.success is False
        assert outcome.reason == "user is no longer active"
        db_session.expire_all()
        device = DeviceRepository.find_by_id(db_session, test_devices[0].id)
        assert device.status == DeviceStatus.AVAILABLE
        assert device.current_borrower_id is None

    def test_remove_leaves_other_sessions(
        self, client, db_session, mock_context, admin_user, test_user
    ):
        mock_context.session_mgr.start_session(admin_user)
        db_session.commit()
        assert _remove(client, test_user.id).status_code == 200
        sess = mock_context.session_mgr.current_session
        assert sess is not None and sess.user.id == admin_user.id


class TestUserRegisterArm:
    """Dashboard enrollment arm — token, conflicts, status, cancel."""

    def test_arm_creates_no_user_and_returns_token(
        self, client, mock_context, db_session
    ):
        db_session.commit()
        resp = client.post(
            "/api/dashboard/users/register",
            json={"display_name": "New Hire"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["enrollment_id"]
        assert data["expires_in"] == 60
        pending = mock_context.pending_registration
        assert pending is not None
        assert pending.from_dashboard is True
        assert pending.enrollment_id == data["enrollment_id"]
        # No user row until the card is tapped.
        assert UserRepository.list_all(db_session) == []
        # No private fields on the public payload.
        assert "uid_hmac" not in data

    def test_arm_active_session_conflict_409(
        self, client, mock_context, test_user, db_session
    ):
        mock_context.session_mgr.start_session(test_user)
        db_session.commit()
        resp = client.post(
            "/api/dashboard/users/register", json={"display_name": "X Person"}
        )
        assert resp.status_code == 409

    def test_arm_pending_window_conflict_409(self, client, mock_context):
        from smart_locker.api.app_context import (
            PendingTagBind,
            assign_pending_tag_bind,
        )
        assign_pending_tag_bind(mock_context, PendingTagBind(device_id=1))
        resp = client.post(
            "/api/dashboard/users/register", json={"display_name": "X Person"}
        )
        assert resp.status_code == 409

    def test_arm_validation_errors(self, client, mock_context):
        assert client.post(
            "/api/dashboard/users/register", json={"display_name": "   "}
        ).status_code == 422
        assert client.post(
            "/api/dashboard/users/register",
            json={"display_name": "x" * 101},
        ).status_code == 422
        assert client.post(
            "/api/dashboard/users/register",
            json={"display_name": "Ok", "role": "admin"},
        ).status_code == 422

    def test_arm_duplicate_name_409(
        self, client, db_session, test_user, mock_context
    ):
        """Case-insensitive duplicates incl. inactive users refuse."""
        test_user.is_active = False
        db_session.commit()
        resp = client.post(
            "/api/dashboard/users/register",
            json={"display_name": test_user.display_name.upper()},
        )
        assert resp.status_code == 409

    def test_status_pending_and_expired(
        self, client, mock_context
    ):
        resp = client.post(
            "/api/dashboard/users/register", json={"display_name": "Poll Me"}
        )
        token = resp.json()["enrollment_id"]
        status = client.get(f"/api/dashboard/users/register/{token}").json()
        assert status["state"] == "pending"
        # Force expiry -> status reports failed/timeout and clears only it.
        mock_context.pending_registration.created_at -= 3600
        status = client.get(f"/api/dashboard/users/register/{token}").json()
        assert status["state"] == "failed"
        assert mock_context.pending_registration is None

    def test_status_unknown_token_404(self, client, mock_context):
        assert (
            client.get("/api/dashboard/users/register/deadbeef").status_code
            == 404
        )

    def test_cancel_clears_own_window(self, client, mock_context):
        resp = client.post(
            "/api/dashboard/users/register", json={"display_name": "Gone"}
        )
        token = resp.json()["enrollment_id"]
        cancel = client.post(
            f"/api/dashboard/users/register/{token}/cancel"
        )
        assert cancel.json()["state"] == "cancelled"
        assert mock_context.pending_registration is None
        # finished state is idempotent
        again = client.post(
            f"/api/dashboard/users/register/{token}/cancel"
        )
        assert again.json()["state"] == "cancelled"

    def test_cancel_wrong_or_newer_token_404(self, client, mock_context):
        resp = client.post(
            "/api/dashboard/users/register", json={"display_name": "First"}
        )
        token1 = resp.json()["enrollment_id"]
        assert client.post(
            "/api/dashboard/users/register/wrong/cancel"
        ).status_code == 404
        assert mock_context.pending_registration is not None
        assert mock_context.pending_registration.enrollment_id == token1

    def test_superseded_token_is_404_and_old_status_safe(
        self, client, mock_context
    ):
        """Cancelling window 1 then arming window 2: a late status write for
        window 1 must not clobber window 2's pending status."""
        resp1 = client.post(
            "/api/dashboard/users/register", json={"display_name": "Old"}
        )
        t1 = resp1.json()["enrollment_id"]
        old_pending = mock_context.pending_registration

        # Cancel old, arm new.
        client.post(f"/api/dashboard/users/register/{t1}/cancel")
        resp2 = client.post(
            "/api/dashboard/users/register", json={"display_name": "New"}
        )
        t2 = resp2.json()["enrollment_id"]

        # Simulate the old window's bridge callback arriving late — the real
        # AppContext helper, invoked against the mock context object.
        AppContext._set_enrollment_status(
            mock_context, old_pending, "failed", "late write"
        )

        # The NEW window's status is untouched — GET still pending.
        status = client.get(f"/api/dashboard/users/register/{t2}").json()
        assert status["state"] == "pending"
        assert mock_context.pending_registration.enrollment_id == t2
        # Old token's status was replaced by the new window -> 404.
        assert client.get(
            f"/api/dashboard/users/register/{t1}"
        ).status_code == 404

    def test_kiosk_cancel_cannot_clear_dashboard_window(
        self, client, mock_context
    ):
        resp = client.post(
            "/api/dashboard/users/register", json={"display_name": "Safe"}
        )
        assert resp.status_code == 200
        out = client.post("/api/register/cancel")
        assert out.status_code == 200
        assert mock_context.pending_registration is not None
        assert (
            mock_context.pending_registration.enrollment_id
            == resp.json()["enrollment_id"]
        )

    def test_arm_without_context_503(self, client):
        import smart_locker.api.app_context as ctx_module
        saved = ctx_module.context
        ctx_module.context = None
        try:
            assert client.post(
                "/api/dashboard/users/register",
                json={"display_name": "No Ctx"},
            ).status_code == 503
        finally:
            ctx_module.context = saved
