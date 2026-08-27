"""
File: test_appliance.py
Description: Unit tests for kiosk exit (stop Chromium, leave the backend up)
             and appliance poweroff (sudo -n systemctl poweroff). No real
             processes are killed; subprocess and os.kill are mocked.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_appliance.py -v
"""

from unittest.mock import MagicMock

import pytest

from smart_locker.services import appliance


class TestExitKiosk:
    """Exit kiosk stops only the Chromium started by start-kiosk.sh."""

    def test_unavailable_without_pgrep(self, monkeypatch):
        monkeypatch.setattr(appliance.shutil, "which", lambda name: None)
        with pytest.raises(appliance.ApplianceUnavailable, match="Raspberry Pi"):
            appliance.exit_kiosk()

    def test_unavailable_when_no_kiosk_process(self, monkeypatch):
        monkeypatch.setattr(
            appliance.shutil, "which", lambda name: "/usr/bin/pgrep" if name == "pgrep" else None
        )

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 1
            result.stdout = ""
            result.stderr = ""
            return result

        monkeypatch.setattr(appliance.subprocess, "run", fake_run)
        with pytest.raises(appliance.ApplianceUnavailable, match="not running"):
            appliance.exit_kiosk()

    def test_sends_sigterm_to_kiosk_pids_only(self, monkeypatch):
        """pgrep pattern is the kiosk profile marker, not a blanket chromium kill."""
        seen_cmds = []
        killed = []

        monkeypatch.setattr(
            appliance.shutil,
            "which",
            lambda name: f"/usr/bin/{name}" if name in ("pgrep",) else None,
        )

        def fake_run(cmd, **kwargs):
            seen_cmds.append(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = "101\n102\n"
            result.stderr = ""
            return result

        monkeypatch.setattr(appliance.subprocess, "run", fake_run)
        monkeypatch.setattr(appliance.os, "kill", lambda pid, sig: killed.append((pid, sig)))

        appliance.exit_kiosk()

        assert seen_cmds, "pgrep must run"
        pgrep_cmd = seen_cmds[0]
        joined = " ".join(pgrep_cmd)
        assert "pgrep" in joined
        assert appliance.KIOSK_PROFILE_MARKER in joined
        assert "chromium" not in joined.lower() or appliance.KIOSK_PROFILE_MARKER in joined
        assert killed == [(101, appliance.signal.SIGTERM), (102, appliance.signal.SIGTERM)]


class TestShutdown:
    """Shut down is an exact sudoers-whitelisted systemctl poweroff."""

    def test_unavailable_without_systemctl(self, monkeypatch):
        monkeypatch.setattr(appliance.shutil, "which", lambda name: None)
        with pytest.raises(appliance.ApplianceUnavailable, match="Raspberry Pi"):
            appliance.shutdown()

    def test_invokes_exact_sudoers_command(self, monkeypatch):
        monkeypatch.setattr(
            appliance.shutil, "which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None
        )
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        monkeypatch.setattr(appliance.subprocess, "run", fake_run)
        appliance.shutdown()

        assert captured["cmd"] == ["sudo", "-n", "/usr/bin/systemctl", "poweroff"]
        assert "*" not in captured["cmd"]

    def test_sudo_failure_raises(self, monkeypatch):
        monkeypatch.setattr(
            appliance.shutil, "which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None
        )

        def fake_run(cmd, **kwargs):
            raise appliance.subprocess.CalledProcessError(
                1, cmd, output="", stderr="sudo: a password is required"
            )

        monkeypatch.setattr(appliance.subprocess, "run", fake_run)
        with pytest.raises(appliance.ApplianceError, match="password|sudo|poweroff"):
            appliance.shutdown()
