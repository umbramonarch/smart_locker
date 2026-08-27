"""
File: test_kiosk_exit.py
Description: Contract tests for admin Exit kiosk and Shut down: panel copy,
             confirm dialogs, API posts, sudoers poweroff, and sudoers refresh
             on update.sh so an existing Pi can gain the rule without a
             full reinstall.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_kiosk_exit.py -v
       Frontend is vanilla HTML/JS; deploy files are asserted as text.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "smart_locker" / "frontend"
DEPLOY_INSTALL = ROOT / "deploy" / "install"


def _html() -> str:
    return (FRONTEND / "index.html").read_text(encoding="utf-8")


def _js() -> str:
    return (FRONTEND / "app.js").read_text(encoding="utf-8")


def _sudoers() -> str:
    return (DEPLOY_INSTALL / "sudoers-smart-locker").read_text(encoding="utf-8")


class TestAdminExitAndShutdownButtons:
    """Hidden admin panel exposes Exit kiosk and Shut down."""

    def test_exit_kiosk_button_present(self):
        """Admin panel has an Exit kiosk action (closes Chromium, service stays)."""
        html = _html()
        assert 'id="admin-exit-kiosk"' in html
        assert '<span class="admin-btn-label">Exit kiosk</span>' in html
        assert "Chromium" in html or "desktop" in html.lower() or "service stays" in html.lower()

    def test_shutdown_button_present(self):
        """Admin panel has a Shut down action that powers off the Pi."""
        html = _html()
        assert 'id="admin-shutdown"' in html
        assert '<span class="admin-btn-label">Shut down</span>' in html

    def test_end_session_still_present(self):
        """Exit kiosk is not a rename of End Session."""
        html = _html()
        assert 'id="admin-end-session"' in html
        assert '<span class="admin-btn-label">End Session</span>' in html

    def test_js_confirms_before_exit_kiosk(self):
        """Exit kiosk asks for confirmation, then POSTs the admin endpoint."""
        js = _js()
        assert "adminExitKiosk" in js
        assert "/api/admin/exit-kiosk" in js
        assert "confirm(" in js
        # Must mention that the service stays / Chromium closes, not poweroff.
        assert "admin-exit-kiosk" in js

    def test_js_confirms_before_shutdown(self):
        """Shut down asks for confirmation, then POSTs the admin endpoint."""
        js = _js()
        assert "adminShutdown" in js
        assert "/api/admin/shutdown" in js
        assert "admin-shutdown" in js

    def test_demo_mode_does_not_post_exit_or_shutdown(self):
        """?demo must not close a developer browser or call poweroff."""
        js = _js()
        assert "adminExitKiosk" in js
        assert "adminShutdown" in js
        assert "Exit kiosk is Pi only" in js
        assert "Shut down is Pi only" in js

    def test_power_overlay_freezes_sse(self):
        """Shut down overlay must ignore card SSE the same way the update overlay does."""
        js = _js()
        show = js[js.index("function showPowerOverlay"): js.index("function hidePowerOverlay")]
        hide = js[js.index("function hidePowerOverlay"): js.index("async function adminShutdown")]
        assert "S.updating = true" in show
        assert "S.updating = false" in hide


class TestSudoersPoweroff:
    """Passwordless sudo is limited to the exact poweroff command."""

    def test_sudoers_allows_systemctl_poweroff(self):
        """sudoers drop-in lists /usr/bin/systemctl poweroff with no wildcard."""
        text = _sudoers()
        assert "/usr/bin/systemctl poweroff" in text
        assert "*" not in text.split("NOPASSWD:", 1)[-1]

    def test_sudoers_still_allows_update_unit(self):
        """Existing Update now rule stays; poweroff is an extra exact command."""
        text = _sudoers()
        assert "systemd-run --collect --unit=smart-locker-update" in text
        assert "/bin/bash __APP_DIR__/deploy/install/update.sh" in text

    def test_apply_sudoers_script_exists_and_validates(self):
        """A one-shot script refreshes /etc/sudoers.d/smart-locker via visudo."""
        script = DEPLOY_INSTALL / "apply-sudoers.sh"
        assert script.is_file(), "apply-sudoers.sh is the SSH-once path for an existing Pi"
        text = script.read_text(encoding="utf-8")
        assert "visudo" in text
        assert "/etc/sudoers.d/smart-locker" in text
        assert "sudoers-smart-locker" in text

    def test_update_sh_refreshes_sudoers(self):
        """Later update.sh applies must refresh sudoers so poweroff keeps working."""
        text = (DEPLOY_INSTALL / "update.sh").read_text(encoding="utf-8")
        assert "apply-sudoers.sh" in text

    def test_install_does_not_chown_tree_to_service_account(self):
        """C3: the passwordless updater must stay root-owned, not service-writable."""
        text = (DEPLOY_INSTALL / "install.sh").read_text(encoding="utf-8")
        assert 'chown -R "$APP_USER:$APP_GROUP" "$APP_DIR"' not in text
        assert 'chown -R root:root "$APP_DIR"' in text
        assert "$APP_DIR/logs" in text
        assert "smart_locker/frontend/images" in text
        assert "chmod 1775" in text

    def test_update_does_not_chown_tree_to_service_account(self):
        """C3: update.sh must not hand the launcher back to the service account."""
        text = (DEPLOY_INSTALL / "update.sh").read_text(encoding="utf-8")
        assert 'chown -R "$APP_USER":"$APP_USER" "$APP_DIR"' not in text
        assert 'chown -R root:root "$APP_DIR"' in text
        assert "$APP_DIR/logs" in text
        assert "smart_locker/frontend/images" in text

    def test_install_sh_uses_apply_sudoers(self):
        """Fresh install still writes sudoers (via the shared apply script)."""
        text = (DEPLOY_INSTALL / "install.sh").read_text(encoding="utf-8")
        assert "apply-sudoers.sh" in text
