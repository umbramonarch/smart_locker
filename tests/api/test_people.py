"""
File: test_people.py
Description: People feature API tests — the dashboard-secret gate and the
             kiosk admin-session gate both edit roles, activate/deactivate,
             add a person, and arm card replacement. Covers the last-admin and
             still-holding-device refusals, that no payload carries card
             credentials, and that a live session follows the user row's
             current role/active flag (deactivation ends it, demotion locks
             out the admin gate).
Project: smart_locker/tests/api
Notes: Run with: python -m pytest tests/api/test_people.py -v
       The card tap that completes an arm lives in tests/e2e/test_people.py —
       the FakeNFCReader drives it through the real bridge.
"""

from smart_locker.api.app_context import PendingTagBind
from smart_locker.database.models import DeviceStatus, UserRole
from smart_locker.database.repositories import DeviceRepository, UserRepository
from smart_locker.services.user_service import update_person

from tests.api.helpers import dashboard_admin_headers


def _hold_device(db_session, user, device):
    """Mark ``device`` as borrowed by ``user`` without running borrow logic.

    Commits: a refused request rolls the request session back on the shared
    StaticPool connection, which would otherwise drop the fixture rows too.
    """
    device.status = DeviceStatus.BORROWED
    device.current_borrower_id = user.id
    db_session.commit()


class TestKioskPeopleApi:
    """Kiosk admin-session gate for the People overlay endpoints."""

    def test_list_people_requires_session(self, client, mock_context):
        """GET /api/admin/users returns 401 without a kiosk session."""
        resp = client.get("/api/admin/users")
        assert resp.status_code == 401

    def test_list_people_rejects_lan(self, lan_client, mock_context, admin_user):
        """A LAN client cannot read the people list even during a session."""
        mock_context.session_mgr.start_session(admin_user)
        resp = lan_client.get("/api/admin/users")
        assert resp.status_code == 403

    def test_list_people_rejects_non_admin(self, client, mock_context, test_user):
        """GET /api/admin/users returns 403 for a normal user session."""
        mock_context.session_mgr.start_session(test_user)
        resp = client.get("/api/admin/users")
        assert resp.status_code == 403

    def test_list_people_admin_payload(self, client, mock_context, admin_user, test_user):
        """The list carries id, role, and active — never card credentials."""
        mock_context.session_mgr.start_session(admin_user)
        resp = client.get("/api/admin/users")
        assert resp.status_code == 200
        rows = {u["display_name"]: u for u in resp.json()}
        assert rows["Test User"]["id"] == test_user.id
        assert rows["Test User"]["role"] == "user"
        assert rows["Admin User"]["role"] == "admin"
        for row in rows.values():
            assert "uid_hmac" not in row
            assert "encrypted_card_uid" not in row

    def test_edit_person_requires_session(self, client, mock_context, test_user):
        resp = client.patch(f"/api/admin/users/{test_user.id}", json={"role": "admin"})
        assert resp.status_code == 401

    def test_edit_person_rejects_non_admin(
        self, client, mock_context, test_user, admin_user
    ):
        """A normal-user session cannot edit people rows."""
        mock_context.session_mgr.start_session(test_user)
        resp = client.patch(f"/api/admin/users/{test_user.id}", json={"role": "admin"})
        assert resp.status_code == 403

    def test_edit_person_role_change(
        self, client, mock_context, admin_user, test_user, db_session
    ):
        """Admin promotes a user; the row and the response reflect the role."""
        mock_context.session_mgr.start_session(admin_user)
        resp = client.patch(f"/api/admin/users/{test_user.id}", json={"role": "admin"})
        assert resp.status_code == 200
        assert resp.json()["role"] == "admin"
        db_session.expire_all()
        assert UserRepository.find_by_id(db_session, test_user.id).role.value == "admin"

    def test_edit_person_deactivate_reactivate(
        self, client, mock_context, admin_user, test_user, db_session
    ):
        """Active flag toggles both directions through the PATCH."""
        mock_context.session_mgr.start_session(admin_user)
        resp = client.patch(
            f"/api/admin/users/{test_user.id}", json={"is_active": False}
        )
        assert resp.status_code == 200
        assert resp.json()["is_active"] is False
        db_session.expire_all()
        assert UserRepository.find_by_id(db_session, test_user.id).is_active is False

        resp = client.patch(
            f"/api/admin/users/{test_user.id}", json={"is_active": True}
        )
        assert resp.status_code == 200
        assert resp.json()["is_active"] is True

    def test_edit_person_unknown_user(self, client, mock_context, admin_user):
        mock_context.session_mgr.start_session(admin_user)
        resp = client.patch("/api/admin/users/99999", json={"role": "admin"})
        assert resp.status_code == 404

    def test_edit_person_bad_role(self, client, mock_context, admin_user, test_user):
        mock_context.session_mgr.start_session(admin_user)
        resp = client.patch(f"/api/admin/users/{test_user.id}", json={"role": "root"})
        assert resp.status_code == 422

    def test_edit_person_empty_body(self, client, mock_context, admin_user, test_user):
        """An edit with neither role nor is_active is a 422 no-op."""
        mock_context.session_mgr.start_session(admin_user)
        resp = client.patch(f"/api/admin/users/{test_user.id}", json={})
        assert resp.status_code == 422

    def test_deactivate_person_holding_device_is_409(
        self, client, mock_context, admin_user, test_user, test_devices, db_session
    ):
        """A borrower cannot be deactivated — the loan must be returned first."""
        _hold_device(db_session, test_user, test_devices[0])
        mock_context.session_mgr.start_session(admin_user)
        resp = client.patch(
            f"/api/admin/users/{test_user.id}", json={"is_active": False}
        )
        assert resp.status_code == 409
        assert "borrowed" in resp.json()["detail"].lower()
        db_session.expire_all()
        assert UserRepository.find_by_id(db_session, test_user.id).is_active is True

    def test_last_admin_cannot_deactivate_or_demote(
        self, client, mock_context, admin_user, db_session
    ):
        """The only active admin cannot lose admin or be deactivated."""
        db_session.commit()  # refuse-path rollbacks must not drop the fixture row
        mock_context.session_mgr.start_session(admin_user)
        resp = client.patch(
            f"/api/admin/users/{admin_user.id}", json={"is_active": False}
        )
        assert resp.status_code == 409
        resp = client.patch(f"/api/admin/users/{admin_user.id}", json={"role": "user"})
        assert resp.status_code == 409
        db_session.expire_all()
        admin = UserRepository.find_by_id(db_session, admin_user.id)
        assert admin.role.value == "admin"
        assert admin.is_active is True

    def test_second_admin_allows_deactivate(
        self, client, mock_context, admin_user, test_user, db_session
    ):
        """With another active admin, demoting/deactivating one is allowed."""
        test_user.role = UserRole.ADMIN
        db_session.commit()
        mock_context.session_mgr.start_session(admin_user)
        resp = client.patch(f"/api/admin/users/{test_user.id}", json={"role": "user"})
        assert resp.status_code == 200
        assert resp.json()["role"] == "user"

    def test_add_person_requires_admin(
        self, client, mock_context, test_user
    ):
        """POST /api/admin/users needs an admin session."""
        resp = client.post("/api/admin/users", json={"name": "New Hire"})
        assert resp.status_code == 401
        mock_context.session_mgr.start_session(test_user)
        resp = client.post("/api/admin/users", json={"name": "New Hire"})
        assert resp.status_code == 403

    def test_add_person_arms_card_window(
        self, client, mock_context, admin_user
    ):
        """A valid add arms the 60s window with the name and chosen role."""
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(
            "/api/admin/users", json={"name": "New Hire", "role": "admin"}
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        pending = mock_context.pending_registration
        assert pending is not None
        assert pending.display_name == "New Hire"
        assert pending.role == "admin"
        assert pending.replace_user_id is None
        assert pending.from_dashboard is False

    def test_add_person_blank_name_422(self, client, mock_context, admin_user):
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post("/api/admin/users", json={"name": "   "})
        assert resp.status_code == 422
        assert mock_context.pending_registration is None

    def test_add_person_bad_role_422(self, client, mock_context, admin_user):
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(
            "/api/admin/users", json={"name": "New Hire", "role": "root"}
        )
        assert resp.status_code == 422
        assert mock_context.pending_registration is None

    def test_add_person_conflict_with_pending_bind(
        self, client, mock_context, admin_user
    ):
        """An armed sticker-bind window blocks a new card window."""
        mock_context.session_mgr.start_session(admin_user)
        mock_context.pending_tag_bind = PendingTagBind(device_id=1)
        resp = client.post("/api/admin/users", json={"name": "New Hire"})
        assert resp.status_code == 409
        assert mock_context.pending_registration is None
        assert mock_context.pending_tag_bind is not None

    def test_replace_card_arms_window(
        self, client, mock_context, admin_user, test_user
    ):
        """Replace-card arms a window carrying the target user's id."""
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(f"/api/admin/users/{test_user.id}/replace-card")
        assert resp.status_code == 200
        pending = mock_context.pending_registration
        assert pending is not None
        assert pending.replace_user_id == test_user.id
        assert pending.display_name == "Test User"
        assert pending.from_dashboard is False

    def test_replace_card_unknown_user(self, client, mock_context, admin_user):
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post("/api/admin/users/99999/replace-card")
        assert resp.status_code == 404
        assert mock_context.pending_registration is None

    def test_replace_card_requires_admin(
        self, client, mock_context, test_user
    ):
        resp = client.post(f"/api/admin/users/{test_user.id}/replace-card")
        assert resp.status_code == 401
        mock_context.session_mgr.start_session(test_user)
        resp = client.post(f"/api/admin/users/{test_user.id}/replace-card")
        assert resp.status_code == 403

    def test_edit_person_role_with_surrounding_whitespace(
        self, client, mock_context, admin_user, test_user, db_session
    ):
        """PATCH {"role": " admin"} behaves like the POST path's parse."""
        mock_context.session_mgr.start_session(admin_user)
        resp = client.patch(
            f"/api/admin/users/{test_user.id}", json={"role": " admin"}
        )
        assert resp.status_code == 200
        assert resp.json()["role"] == "admin"
        db_session.expire_all()
        assert UserRepository.find_by_id(db_session, test_user.id).role.value == "admin"

    def test_edit_person_whitespace_only_role_422(
        self, client, mock_context, admin_user, test_user
    ):
        """Whitespace-only strips to empty — still a bad role, not a no-op."""
        mock_context.session_mgr.start_session(admin_user)
        resp = client.patch(f"/api/admin/users/{test_user.id}", json={"role": "  "})
        assert resp.status_code == 422


class TestStaleSession:
    """require_session re-reads the user row — a live session follows the
    live is_active/role, not the snapshot taken at login."""

    def test_deactivated_user_session_is_ended(
        self, client, mock_context, test_user, db_session
    ):
        """A deactivated user's live session dies on the next gated call."""
        mock_context.session_mgr.start_session(test_user)
        update_person(db_session, test_user, is_active=False)
        db_session.commit()

        resp = client.get("/api/devices")
        assert resp.status_code == 401
        assert not mock_context.session_mgr.has_active_session
        events = [c.args[0] for c in mock_context.broadcast_sse.call_args_list]
        assert {
            "event": "session_ended", "reason": "account_inactive"
        } in events

    def test_deactivated_admin_session_is_ended(
        self, client, mock_context, admin_user, db_session
    ):
        """Same for an admin session — ended before the admin gate runs."""
        UserRepository.create(
            db_session,
            display_name="Second Admin",
            uid_hmac="adm2-hmac",
            encrypted_card_uid="enc-adm2",
            role="admin",
        )
        mock_context.session_mgr.start_session(admin_user)
        update_person(db_session, admin_user, is_active=False)
        db_session.commit()

        resp = client.get("/api/admin/users")
        assert resp.status_code == 401
        assert not mock_context.session_mgr.has_active_session

    def test_demoted_admin_cannot_self_restore(
        self, client, mock_context, admin_user, db_session
    ):
        """A demoted admin's live session loses admin rights immediately —
        including the PATCH that would restore its own role."""
        UserRepository.create(
            db_session,
            display_name="Second Admin",
            uid_hmac="adm2-hmac",
            encrypted_card_uid="enc-adm2",
            role="admin",
        )
        db_session.commit()
        mock_context.session_mgr.start_session(admin_user)
        update_person(db_session, admin_user, role="user")
        db_session.commit()

        # The session survives, but the fresh role is USER.
        resp = client.get("/api/admin/users")
        assert resp.status_code == 403
        # Self-restore is refused by the same live-role check.
        resp = client.patch(
            f"/api/admin/users/{admin_user.id}", json={"role": "admin"}
        )
        assert resp.status_code == 403
        db_session.expire_all()
        admin = UserRepository.find_by_id(db_session, admin_user.id)
        assert admin.role == UserRole.USER


class TestDashboardPeopleApi:
    """Dashboard-secret gate for the People editor endpoints."""

    def test_users_list_includes_id_without_card_fields(
        self, client, test_user, dashboard_secret
    ):
        """The people list feeds the editor: id present, credentials absent."""
        resp = client.get(
            "/api/dashboard/users",
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        row = next(u for u in resp.json() if u["display_name"] == "Test User")
        assert row["id"] == test_user.id
        assert row["role"] == "user"
        assert row["is_active"] is True
        assert "uid_hmac" not in row
        assert "encrypted_card_uid" not in row

    def test_patch_requires_secret(self, client, test_user):
        """PATCH fails closed: no secret configured -> 401."""
        resp = client.patch(
            f"/api/dashboard/users/{test_user.id}", json={"role": "admin"}
        )
        assert resp.status_code == 401

    def test_patch_wrong_secret_401(
        self, client, test_user, dashboard_secret
    ):
        resp = client.patch(
            f"/api/dashboard/users/{test_user.id}",
            json={"role": "admin"},
            headers=dashboard_admin_headers("wrong"),
        )
        assert resp.status_code == 401

    def test_patch_role_and_deactivate(
        self, client, admin_user, test_user, dashboard_secret, db_session
    ):
        """Dashboard PATCH edits role and active flag through the same rules."""
        resp = client.patch(
            f"/api/dashboard/users/{test_user.id}",
            json={"role": "admin", "is_active": True},
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        assert resp.json()["role"] == "admin"
        db_session.expire_all()
        assert UserRepository.find_by_id(db_session, test_user.id).role.value == "admin"

    def test_last_admin_protection_on_dashboard(
        self, client, admin_user, dashboard_secret
    ):
        """The last-admin refusal applies through the dashboard gate too."""
        resp = client.patch(
            f"/api/dashboard/users/{admin_user.id}",
            json={"is_active": False},
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 409
        assert "last active admin" in resp.json()["detail"].lower()

    def test_add_person_requires_secret(self, client, mock_context):
        resp = client.post("/api/dashboard/users", json={"name": "New Hire"})
        assert resp.status_code == 401
        assert mock_context.pending_registration is None

    def test_add_person_arms_dashboard_window(
        self, client, mock_context, dashboard_secret
    ):
        """A dashboard arm is marked from_dashboard so kiosk cancel spares it."""
        resp = client.post(
            "/api/dashboard/users",
            json={"name": "Remote Hire", "role": "user"},
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        pending = mock_context.pending_registration
        assert pending is not None
        assert pending.display_name == "Remote Hire"
        assert pending.role == "user"
        assert pending.from_dashboard is True

    def test_add_person_refused_during_kiosk_session(
        self, client, mock_context, admin_user, dashboard_secret
    ):
        """The dashboard cannot arm a card window while a session owns the reader."""
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(
            "/api/dashboard/users",
            json={"name": "Remote Hire"},
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 409
        assert mock_context.pending_registration is None

    def test_replace_card_arms_dashboard_window(
        self, client, mock_context, test_user, dashboard_secret
    ):
        resp = client.post(
            f"/api/dashboard/users/{test_user.id}/replace-card",
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 200
        pending = mock_context.pending_registration
        assert pending is not None
        assert pending.replace_user_id == test_user.id
        assert pending.from_dashboard is True

    def test_replace_card_refused_during_kiosk_session(
        self, client, mock_context, admin_user, test_user, dashboard_secret
    ):
        mock_context.session_mgr.start_session(admin_user)
        resp = client.post(
            f"/api/dashboard/users/{test_user.id}/replace-card",
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 409
        assert mock_context.pending_registration is None

    def test_replace_card_unknown_404(
        self, client, mock_context, dashboard_secret
    ):
        resp = client.post(
            "/api/dashboard/users/99999/replace-card",
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert resp.status_code == 404

    def test_public_cancel_preserves_dashboard_card_window(
        self, client, mock_context, dashboard_secret
    ):
        """POST /api/register/cancel must not kill a dashboard-armed card window."""
        client.post(
            "/api/dashboard/users",
            json={"name": "Remote Hire"},
            headers=dashboard_admin_headers(dashboard_secret),
        )
        assert mock_context.pending_registration is not None
        resp = client.post("/api/register/cancel")
        assert resp.status_code == 200
        assert resp.json()["cancelled"] is False
        assert mock_context.pending_registration is not None
        assert mock_context.pending_registration.from_dashboard is True
