"""
File: test_setup.py
Description: Tests for first-boot Setup: GET/POST /api/setup open only while
             the database has no active admin, the dashboard-secret gate once
             a password exists, and the .env persistence of the first password.
Project: smart_locker/tests/api
Notes: Run with: python -m pytest tests/api/test_setup.py -v
       The completing card tap itself is covered end-to-end in
       tests/e2e/test_setup_first_admin.py.
"""
import os
import stat

from smart_locker.api.app_context import PendingRegistration, PendingTagBind

from config.settings import DASHBOARD_ADMIN_SECRET_ENV_VAR


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

    def test_setup_closed_once_admin_exists(self, client, mock_context, admin_user):
        resp = client.get("/api/setup")
        assert resp.status_code == 200
        assert resp.json()["needed"] is False


class TestStartSetup:
    """POST /api/setup arms a card window that enrolls an admin."""

    def test_start_setup_arms_admin_registration(self, client, mock_context):
        resp = client.post("/api/setup", json={"name": "First Admin"})
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        pending = mock_context.pending_registration
        assert pending is not None
        assert pending.display_name == "First Admin"
        assert pending.role == "admin"
        assert mock_context.pending_tag_bind is None

    def test_start_setup_rejects_existing_admin(self, client, mock_context, admin_user):
        resp = client.post("/api/setup", json={"name": "Second Admin"})
        assert resp.status_code == 404
        assert mock_context.pending_registration is None

    def test_start_setup_conflicts_with_pending_registration(
        self, client, mock_context
    ):
        mock_context.pending_registration = PendingRegistration("Someone")
        resp = client.post("/api/setup", json={"name": "First Admin"})
        assert resp.status_code == 409
        assert mock_context.pending_registration.display_name == "Someone"

    def test_start_setup_conflicts_with_pending_tag_bind(self, client, mock_context):
        mock_context.pending_tag_bind = PendingTagBind(device_id=7)
        resp = client.post("/api/setup", json={"name": "First Admin"})
        assert resp.status_code == 409
        assert mock_context.pending_registration is None

    def test_start_setup_lan_arm_is_not_enrollment(self, lan_client, mock_context):
        """A LAN caller may open the window; only the physical tap enrolls."""
        resp = lan_client.post("/api/setup", json={"name": "Remote Arm"})
        assert resp.status_code == 200
        assert mock_context.pending_registration.role == "admin"

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


class TestSetupPassword:
    """The first password persists to .env as the dashboard admin secret."""

    def test_password_written_to_env_file(
        self, client, mock_context, tmp_path, monkeypatch
    ):
        env_path = tmp_path / ".env"
        monkeypatch.setenv("SMART_LOCKER_ENV_PATH", str(env_path))
        resp = client.post(
            "/api/setup", json={"name": "First Admin", "password": "s3cret pw"}
        )
        assert resp.status_code == 200

        text = env_path.read_text(encoding="utf-8")
        assert f'{DASHBOARD_ADMIN_SECRET_ENV_VAR}="s3cret pw"' in text
        assert resp.text.find("s3cret pw") == -1
        # The running process accepts the password without a restart.
        assert os.environ[DASHBOARD_ADMIN_SECRET_ENV_VAR] == "s3cret pw"

        if os.name == "posix":
            assert stat.S_IMODE(env_path.stat().st_mode) == 0o640

    def test_password_appends_to_existing_env(
        self, client, mock_context, tmp_path, monkeypatch
    ):
        env_path = tmp_path / ".env"
        env_path.write_text(
            'SMART_LOCKER_DB_PATH="/opt/smart-locker/smart_locker.db"\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("SMART_LOCKER_ENV_PATH", str(env_path))
        resp = client.post(
            "/api/setup", json={"name": "First Admin", "password": "pw"}
        )
        assert resp.status_code == 200
        text = env_path.read_text(encoding="utf-8")
        assert 'SMART_LOCKER_DB_PATH="/opt/smart-locker/smart_locker.db"' in text
        assert f'{DASHBOARD_ADMIN_SECRET_ENV_VAR}="pw"' in text

    def test_existing_secret_is_not_overwritten(
        self, client, mock_context, tmp_path, monkeypatch, dashboard_secret
    ):
        env_path = tmp_path / ".env"
        env_path.write_text(
            f'{DASHBOARD_ADMIN_SECRET_ENV_VAR}="{dashboard_secret}"\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("SMART_LOCKER_ENV_PATH", str(env_path))
        resp = client.post(
            "/api/setup",
            json={"name": "First Admin", "password": "new-password"},
            headers={"X-Smart-Locker-Admin": dashboard_secret},
        )
        assert resp.status_code == 200
        text = env_path.read_text(encoding="utf-8")
        assert "new-password" not in text
        assert dashboard_secret in text

    def test_missing_password_arms_without_write(
        self, client, mock_context, tmp_path, monkeypatch
    ):
        """Name-only Setup still works — the secret stays unset."""
        env_path = tmp_path / ".env"
        monkeypatch.setenv("SMART_LOCKER_ENV_PATH", str(env_path))
        resp = client.post("/api/setup", json={"name": "First Admin"})
        assert resp.status_code == 200
        assert not env_path.exists()
        assert DASHBOARD_ADMIN_SECRET_ENV_VAR not in os.environ
