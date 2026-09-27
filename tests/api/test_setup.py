"""
File: test_setup.py
Description: Tests for first-boot Setup: GET /api/setup open only while the
             database has no active admin, the loopback + dashboard-secret
             gates on the arm POST, and the dashboard.secret persistence of
             the first password.
Project: smart_locker/tests/api
Notes: Run with: python -m pytest tests/api/test_setup.py -v
       The completing card tap itself is covered end-to-end in
       tests/e2e/test_setup_first_admin.py.
"""
import os
import stat
import threading

from smart_locker.api.app_context import PendingRegistration, PendingTagBind

from config.settings import (
    DASHBOARD_ADMIN_SECRET_ENV_VAR,
    dashboard_secret_path,
)


class TestSetupState:
    """GET /api/setup reports whether first-boot Setup is open."""

    def test_health_ok_on_empty_db(self, client, mock_context):
        """An empty database still serves a healthy kiosk."""
        resp = client.get("/api/health")
        assert resp.status_code == 200

    def test_setup_needed_on_empty_db(self, client, mock_context):
        resp = client.get("/api/setup")
        assert resp.status_code == 200
        assert resp.json() == {"needed": True, "secret_set": False}

    def test_setup_needed_reports_configured_secret(
        self, client, mock_context, dashboard_secret
    ):
        resp = client.get("/api/setup")
        assert resp.status_code == 200
        assert resp.json() == {"needed": True, "secret_set": True}

    def test_setup_needed_reports_secret_file(self, client, mock_context):
        """A Setup-written dashboard.secret file counts as configured."""
        dashboard_secret_path().write_text("typed-at-kiosk\n", encoding="utf-8")
        resp = client.get("/api/setup")
        assert resp.status_code == 200
        assert resp.json() == {"needed": True, "secret_set": True}

    def test_setup_closed_once_admin_exists(self, client, mock_context, admin_user):
        resp = client.get("/api/setup")
        assert resp.status_code == 200
        assert resp.json()["needed"] is False


class TestStartSetup:
    """POST /api/setup arms a card window that enrolls an admin."""

    def test_start_setup_arms_admin_registration(self, client, mock_context):
        resp = client.post(
            "/api/setup", json={"name": "First Admin", "password": "pw"}
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        pending = mock_context.pending_registration
        assert pending is not None
        assert pending.display_name == "First Admin"
        assert pending.role == "admin"
        assert mock_context.pending_tag_bind is None

    def test_start_setup_rejects_existing_admin(self, client, mock_context, admin_user):
        resp = client.post(
            "/api/setup", json={"name": "Second Admin", "password": "pw"}
        )
        assert resp.status_code == 404
        assert mock_context.pending_registration is None

    def test_start_setup_conflicts_with_pending_registration(
        self, client, mock_context
    ):
        mock_context.pending_registration = PendingRegistration("Someone")
        resp = client.post(
            "/api/setup", json={"name": "First Admin", "password": "pw"}
        )
        assert resp.status_code == 409
        assert mock_context.pending_registration.display_name == "Someone"

    def test_start_setup_conflicts_with_pending_tag_bind(self, client, mock_context):
        mock_context.pending_tag_bind = PendingTagBind(device_id=7)
        resp = client.post(
            "/api/setup", json={"name": "First Admin", "password": "pw"}
        )
        assert resp.status_code == 409
        assert mock_context.pending_registration is None

    def test_start_setup_refuses_lan(self, lan_client, mock_context):
        """LAN cannot arm Setup — no credential plant, no window squatting."""
        resp = lan_client.post(
            "/api/setup", json={"name": "Remote Arm", "password": "planted"}
        )
        assert resp.status_code == 403
        assert mock_context.pending_registration is None
        assert not dashboard_secret_path().exists()

    def test_start_setup_requires_secret_once_configured(
        self, client, mock_context, dashboard_secret
    ):
        resp = client.post("/api/setup", json={"name": "First Admin"})
        assert resp.status_code == 401
        assert mock_context.pending_registration is None

    def test_start_setup_rejects_wrong_secret(
        self, client, mock_context, dashboard_secret
    ):
        resp = client.post(
            "/api/setup",
            json={"name": "First Admin"},
            headers={"X-Smart-Locker-Admin": "wrong"},
        )
        assert resp.status_code == 401
        assert mock_context.pending_registration is None

    def test_start_setup_non_ascii_header_is_401_not_500(
        self, client, mock_context, dashboard_secret
    ):
        """A latin-1 header must not crash secrets.compare_digest."""
        resp = client.post(
            "/api/setup",
            json={"name": "First Admin"},
            headers={"X-Smart-Locker-Admin": "päss".encode("latin-1")},
        )
        assert resp.status_code == 401
        assert mock_context.pending_registration is None

    def test_start_setup_accepts_secret_header(
        self, client, mock_context, dashboard_secret
    ):
        resp = client.post(
            "/api/setup",
            json={"name": "First Admin"},
            headers={"X-Smart-Locker-Admin": dashboard_secret},
        )
        assert resp.status_code == 200
        assert mock_context.pending_registration.role == "admin"

    def test_start_setup_rejects_blank_name(self, client, mock_context):
        """A whitespace-only name must not enroll an empty-named admin."""
        resp = client.post(
            "/api/setup", json={"name": "   ", "password": "pw"}
        )
        assert resp.status_code == 422
        assert mock_context.pending_registration is None

    def test_start_setup_rejects_missing_password(self, client, mock_context):
        """No secret configured + no password typed would brick dashboard admin."""
        resp = client.post("/api/setup", json={"name": "First Admin"})
        assert resp.status_code == 422
        assert mock_context.pending_registration is None
        assert not dashboard_secret_path().exists()

    def test_start_setup_rejects_blank_password(self, client, mock_context):
        resp = client.post(
            "/api/setup", json={"name": "First Admin", "password": "   "}
        )
        assert resp.status_code == 422
        assert mock_context.pending_registration is None

    def test_start_setup_reader_unavailable(self, client, mock_context):
        """Arming with a dead reader would strand the operator in a dead window."""
        mock_context.reader.is_running = False
        resp = client.post(
            "/api/setup", json={"name": "First Admin", "password": "pw"}
        )
        assert resp.status_code == 503
        assert mock_context.pending_registration is None

    def test_start_setup_reader_absent(self, client, mock_context):
        mock_context.reader = None
        resp = client.post(
            "/api/setup", json={"name": "First Admin", "password": "pw"}
        )
        assert resp.status_code == 503
        assert mock_context.pending_registration is None

    def test_concurrent_setup_arms_only_one_wins(self, mock_context):
        """Two racing POSTs must not both arm — the pending-window check and
        the assignment are one critical section. The lock, not SQLite, is the
        boundary under test, so arm_pending_registration is raced directly."""
        from smart_locker.api.app_context import arm_pending_registration

        conflicts, armed = [], []

        def arm():
            outcome = arm_pending_registration(
                mock_context, PendingRegistration("Racer", role="admin")
            )
            (conflicts if outcome else armed).append(outcome)

        threads = [threading.Thread(target=arm) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(armed) == 1
        assert len(conflicts) == 3
        pending = mock_context.pending_registration
        assert pending is not None and pending.role == "admin"


class TestSetupPassword:
    """The first password persists to the service-owned dashboard.secret file."""

    def test_password_written_to_secret_file(self, client, mock_context):
        resp = client.post(
            "/api/setup", json={"name": "First Admin", "password": "s3cret pw"}
        )
        assert resp.status_code == 200

        path = dashboard_secret_path()
        assert path.exists()
        # The file stores the raw password (trimmed), not a KEY=VALUE line —
        # it is never parsed by update.sh as root the way .env is.
        assert path.read_text(encoding="utf-8").strip() == "s3cret pw"
        assert resp.text.find("s3cret pw") == -1
        # The running process accepts the password without a restart.
        assert os.environ[DASHBOARD_ADMIN_SECRET_ENV_VAR] == "s3cret pw"

        if os.name == "posix":
            assert stat.S_IMODE(path.stat().st_mode) == 0o640

    def test_secret_file_survives_env_override(
        self, client, mock_context, dashboard_secret
    ):
        """An env-configured secret wins over the file and is not overwritten."""
        resp = client.post(
            "/api/setup",
            json={"name": "First Admin", "password": "new-password"},
            headers={"X-Smart-Locker-Admin": dashboard_secret},
        )
        assert resp.status_code == 200
        assert not dashboard_secret_path().exists()
        assert os.environ[DASHBOARD_ADMIN_SECRET_ENV_VAR] == dashboard_secret

    def test_password_rejects_newline(self, client, mock_context):
        resp = client.post(
            "/api/setup", json={"name": "First Admin", "password": "pw\ninject=1"}
        )
        assert resp.status_code == 500
        assert not dashboard_secret_path().exists()
        assert mock_context.pending_registration is None
