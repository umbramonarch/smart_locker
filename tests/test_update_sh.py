"""
File: test_update_sh.py
Description: Tests for deploy/install/update.sh USB-tree discovery, incoming
             skip/preserve, version refuse, and missing-wheel overlay text.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_update_sh.py -v
       Sources update.sh with SMART_LOCKER_UPDATE_LIB=1 (no systemd).
       A few tests run the script until it refuses before stop/backup.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UPDATE_SH = ROOT / "deploy" / "install" / "update.sh"
RSYNC = shutil.which("rsync")


def _plant_tree(root: Path, *, version: str | None = None, reqs: str | None = None) -> Path:
    """Create a minimal smart_locker repo root at ``root``."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "smart_locker").mkdir(parents=True, exist_ok=True)
    (root / "smart_locker" / "app.py").write_text("# kiosk app\n", encoding="utf-8")
    (root / "requirements.txt").write_text(
        reqs if reqs is not None else "fastapi>=0.115.0\nnewpkg>=1.0.0\n",
        encoding="utf-8",
    )
    (root / "deploy" / "install").mkdir(parents=True, exist_ok=True)
    (root / "deploy" / "install" / "update.sh").write_text(
        "#!/usr/bin/env bash\n# stub\n", encoding="utf-8"
    )
    if version is not None:
        (root / "VERSION").write_text(version + "\n", encoding="utf-8")
    return root


def _make_app_dir(tmp_path: Path, *, version: str = "1.0.0") -> Path:
    """Fake Pi app dir with a venv python stub and empty search roots."""
    app = tmp_path / "pi"
    (app / "logs").mkdir(parents=True)
    (app / "backups").mkdir()
    (app / "VERSION").write_text(version + "\n", encoding="utf-8")
    venv = app / "venv" / "bin"
    venv.mkdir(parents=True)
    py = venv / "python"
    py.write_text("#!/bin/sh\nexec python3 \"$@\"\n", encoding="utf-8")
    py.chmod(py.stat().st_mode | stat.S_IEXEC)
    pip = venv / "pip"
    pip.write_text("#!/bin/sh\necho pip-stub\nexit 0\n", encoding="utf-8")
    pip.chmod(pip.stat().st_mode | stat.S_IEXEC)
    (app / "deploy" / "wheelhouse").mkdir(parents=True)
    (app / "media").mkdir()
    (app / "cifs-updates").mkdir()
    return app


def _lib_env(app_dir: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["SMART_LOCKER_UPDATE_LIB"] = "1"
    env["SMART_LOCKER_DIR"] = str(app_dir)
    env["SMART_LOCKER_USB_MEDIA_ROOT"] = str(app_dir / "media")
    env["SMART_LOCKER_UPDATE_DIR"] = str(app_dir / "cifs-updates")
    if extra:
        env.update(extra)
    return env


def source_lib(
    snippet: str, app_dir: Path, extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Source update.sh in lib mode and run ``snippet``."""
    script = f"source {shlex.quote(str(UPDATE_SH))}\n{snippet}\n"
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=_lib_env(app_dir, extra),
    )


def run_update(
    app_dir: Path, extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run update.sh against a fake Pi dir (refuses before systemd on these tests)."""
    env = os.environ.copy()
    env["SMART_LOCKER_DIR"] = str(app_dir)
    env["SMART_LOCKER_USB_MEDIA_ROOT"] = str(app_dir / "media")
    env["SMART_LOCKER_UPDATE_DIR"] = str(app_dir / "cifs-updates")
    if extra:
        env.update(extra)
    return subprocess.run(
        ["bash", str(UPDATE_SH)],
        capture_output=True,
        text=True,
        env=env,
    )


def _status(app_dir: Path) -> dict:
    path = app_dir / "logs" / "update-status.json"
    return json.loads(path.read_text(encoding="utf-8"))


class TestUpdateShContract:
    """PRESERVE / INCOMING_SKIP stay complete; USB path must not require HMAC."""

    def test_preserve_list_covers_runtime_paths(self):
        text = UPDATE_SH.read_text(encoding="utf-8")
        for item in (
            '".env"',
            '"smart_locker.db"',
            '"smart_locker.db-wal"',
            '"smart_locker.db-shm"',
            '"last_sync.json"',
            '"logs"',
            '"venv"',
            '"deploy/wheelhouse"',
            '"deploy/system-packages"',
            '"backups"',
            '".update-staging"',
            '".git"',
            '"smart_locker/frontend/images"',
            '"VERSION"',
        ):
            assert item in text

    def test_incoming_skip_skips_windows_runtime_copies(self):
        text = UPDATE_SH.read_text(encoding="utf-8")
        assert "INCOMING_SKIP=" in text
        for item in (".env", "venv", "logs", "backups", "smart_locker.db"):
            assert f'"{item}"' in text

    def test_openssl_not_required_before_tarball_path(self):
        text = UPDATE_SH.read_text(encoding="utf-8")
        assert "openssl is required only for the signed-tarball fallback" in text
        pre, _, post = text.partition('SOURCE_KIND" = "TARBALL"')
        assert "command -v openssl" not in pre
        assert "command -v openssl" in post

    def test_no_udev_auto_apply(self):
        text = UPDATE_SH.read_text(encoding="utf-8")
        assert "udev" not in text.lower()
        assert "inotify" not in text.lower()

    def test_admin_button_says_usb(self):
        html = (ROOT / "smart_locker" / "frontend" / "index.html").read_text(
            encoding="utf-8"
        )
        assert "Software Update" in html
        assert "USB" in html
        js = (ROOT / "smart_locker" / "frontend" / "app.js").read_text(encoding="utf-8")
        assert "USB stick" in js


class TestRepoTreeDiscovery:
    """USB first, then local locker-updates, then CIFS tree, then tarball."""

    def test_is_repo_tree_requires_app_reqs_and_update_sh(self, tmp_path):
        app = _make_app_dir(tmp_path)
        good = _plant_tree(tmp_path / "good")
        proc = source_lib(
            f"is_repo_tree {shlex.quote(str(good))}; echo good:$?\n"
            f"is_repo_tree {shlex.quote(str(tmp_path / 'missing'))} || echo bad:$?\n",
            app,
        )
        assert proc.returncode == 0, proc.stderr
        assert "good:0" in proc.stdout
        assert "bad:1" in proc.stdout

    def test_finds_project_folder_on_usb_stick(self, tmp_path):
        app = _make_app_dir(tmp_path)
        stick = app / "media" / "locker" / "SMARTLOCKER"
        _plant_tree(stick / "smart_locker", version="2.0.0")
        proc = source_lib("discover_update_source\n", app)
        assert proc.returncode == 0, proc.stderr
        kind, path = proc.stdout.strip().split("\t", 1)
        assert kind == "TREE"
        assert Path(path).resolve() == (stick / "smart_locker").resolve()

    def test_finds_repo_at_usb_stick_root(self, tmp_path):
        app = _make_app_dir(tmp_path)
        stick = app / "media" / "locker" / "STICK"
        _plant_tree(stick, version="2.0.0")
        proc = source_lib("discover_update_source\n", app)
        assert proc.returncode == 0, proc.stderr
        kind, path = proc.stdout.strip().split("\t", 1)
        assert kind == "TREE"
        assert Path(path).resolve() == stick.resolve()

    def test_finds_tree_in_locker_updates_subfolder(self, tmp_path):
        app = _make_app_dir(tmp_path)
        nested = app / "media" / "locker" / "STICK" / "locker-updates" / "payload"
        _plant_tree(nested, version="2.0.0")
        proc = source_lib("discover_update_source\n", app)
        assert proc.returncode == 0, proc.stderr
        kind, path = proc.stdout.strip().split("\t", 1)
        assert kind == "TREE"
        assert Path(path).resolve() == nested.resolve()

    def test_usb_tree_wins_over_cifs_tarball(self, tmp_path):
        app = _make_app_dir(tmp_path)
        _plant_tree(app / "media" / "locker" / "STICK", version="2.0.0")
        tarball = app / "cifs-updates" / "smart-locker-9.0.0.tar.gz"
        tarball.write_bytes(b"not-a-real-tarball")
        proc = source_lib("discover_update_source\n", app)
        assert proc.returncode == 0, proc.stderr
        kind, path = proc.stdout.strip().split("\t", 1)
        assert kind == "TREE"
        assert "STICK" in path

    def test_local_locker_updates_when_no_usb(self, tmp_path):
        app = _make_app_dir(tmp_path)
        _plant_tree(app / "locker-updates" / "payload", version="2.0.0")
        proc = source_lib("discover_update_source\n", app)
        assert proc.returncode == 0, proc.stderr
        kind, path = proc.stdout.strip().split("\t", 1)
        assert kind == "TREE"
        assert "locker-updates" in path

    def test_cifs_tarball_is_last_resort(self, tmp_path):
        app = _make_app_dir(tmp_path)
        tarball = app / "cifs-updates" / "smart-locker-9.0.0.tar.gz"
        tarball.write_bytes(b"not-a-real-tarball")
        proc = source_lib("discover_update_source\n", app)
        assert proc.returncode == 0, proc.stderr
        kind, path = proc.stdout.strip().split("\t", 1)
        assert kind == "TARBALL"
        assert path.endswith("smart-locker-9.0.0.tar.gz")

    def test_nothing_found(self, tmp_path):
        app = _make_app_dir(tmp_path)
        proc = source_lib("discover_update_source || echo NONE:$?\n", app)
        assert proc.returncode == 0, proc.stderr
        assert "NONE:1" in proc.stdout


class TestVersionCompare:
    """Equal → not older; cheap sort -V refuse; hashes are not ordered."""

    def test_equal_is_not_older(self, tmp_path):
        app = _make_app_dir(tmp_path)
        proc = source_lib(
            "if version_is_older 1.2.0 1.2.0; then echo older; else echo not_older; fi\n",
            app,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "not_older"

    def test_semver_older_is_refused(self, tmp_path):
        app = _make_app_dir(tmp_path)
        proc = source_lib(
            "if version_is_older 1.0.0 2.0.0; then echo older; else echo not_older; fi\n"
            "if version_is_older 2.0.0 1.0.0; then echo older2; else echo newer; fi\n",
            app,
        )
        assert proc.returncode == 0, proc.stderr
        assert "older" in proc.stdout.splitlines()
        assert "newer" in proc.stdout.splitlines()

    def test_git_hashes_are_not_ordered(self, tmp_path):
        app = _make_app_dir(tmp_path)
        proc = source_lib(
            "if version_is_older abcdef0 1234567; then echo older; else echo not_older; fi\n",
            app,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "not_older"

    def test_tree_version_reads_version_file(self, tmp_path):
        app = _make_app_dir(tmp_path)
        tree = _plant_tree(tmp_path / "tree", version="2.3.4")
        proc = source_lib(f"tree_version {shlex.quote(str(tree))}\n", app)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "2.3.4"


@pytest.mark.skipif(RSYNC is None, reason="rsync required to copy an incoming tree")
class TestIncomingPreserve:
    """Windows copies of .env / db / venv / logs / backups must not be staged."""

    def test_copy_incoming_tree_skips_runtime_files(self, tmp_path):
        app = _make_app_dir(tmp_path)
        src = _plant_tree(tmp_path / "usb", version="2.0.0")
        (src / ".env").write_text("SMART_LOCKER_ENC_KEY=from-windows\n", encoding="utf-8")
        (src / "smart_locker.db").write_text("not-a-db\n", encoding="utf-8")
        (src / "other.db").write_text("also-skip\n", encoding="utf-8")
        (src / "venv").mkdir()
        (src / "venv" / "win.bin").write_text("nope\n", encoding="utf-8")
        (src / "logs").mkdir()
        (src / "logs" / "noise.log").write_text("nope\n", encoding="utf-8")
        (src / "backups").mkdir()
        (src / "backups" / "old.tgz").write_text("nope\n", encoding="utf-8")
        (src / "deploy" / "wheelhouse").mkdir(parents=True)
        (src / "deploy" / "wheelhouse" / "fastapi-0.1.0-py3-none-any.whl").write_bytes(
            b"whl"
        )
        (src / "keep.txt").write_text("payload\n", encoding="utf-8")
        dest = tmp_path / "staging"
        proc = source_lib(
            f"copy_incoming_tree {shlex.quote(str(src))} {shlex.quote(str(dest))}\n",
            app,
        )
        assert proc.returncode == 0, proc.stderr
        assert (dest / "keep.txt").read_text(encoding="utf-8") == "payload\n"
        assert (dest / "smart_locker" / "app.py").is_file()
        assert (dest / "requirements.txt").is_file()
        assert not (dest / ".env").exists()
        assert not (dest / "smart_locker.db").exists()
        assert not (dest / "other.db").exists()
        assert not (dest / "venv").exists()
        assert not (dest / "logs").exists()
        assert not (dest / "backups").exists()
        assert not (dest / "deploy" / "wheelhouse" / "fastapi-0.1.0-py3-none-any.whl").exists()


class TestMissingWheels:
    """Overlay message names packages and tells the operator to rebuild the kit."""

    def test_list_missing_wheels_names_packages(self, tmp_path):
        app = _make_app_dir(tmp_path)
        reqs = tmp_path / "requirements.txt"
        reqs.write_text(
            "pyscard>=2.0.7\nfastapi>=0.115.0\npython-dotenv>=1.0.0\nnewpkg>=1.0\n",
            encoding="utf-8",
        )
        wheels = tmp_path / "wheelhouse"
        wheels.mkdir()
        (wheels / "fastapi-0.115.0-py3-none-any.whl").write_bytes(b"whl")
        proc = source_lib(
            f"list_missing_wheels {shlex.quote(str(reqs))} {shlex.quote(str(wheels))}\n",
            app,
        )
        assert proc.returncode == 0, proc.stderr
        names = proc.stdout.strip()
        assert "newpkg" in names
        assert "python-dotenv" in names or "python_dotenv" in names
        assert "fastapi" not in names
        assert "pyscard" not in names

    def test_list_missing_wheels_parses_pip_log(self, tmp_path):
        app = _make_app_dir(tmp_path)
        reqs = tmp_path / "requirements.txt"
        reqs.write_text("fastapi>=0.115.0\n", encoding="utf-8")
        wheels = tmp_path / "wheelhouse"
        wheels.mkdir()
        (wheels / "fastapi-0.115.0-py3-none-any.whl").write_bytes(b"whl")
        pip_log = tmp_path / "pip.log"
        pip_log.write_text(
            "ERROR: Could not find a version that satisfies the requirement httpx>=0.27.0 "
            "(from versions: none)\n"
            "ERROR: No matching distribution found for httpx>=0.27.0\n",
            encoding="utf-8",
        )
        proc = source_lib(
            "list_missing_wheels "
            f"{shlex.quote(str(reqs))} {shlex.quote(str(wheels))} "
            f"{shlex.quote(str(pip_log))}\n",
            app,
        )
        assert proc.returncode == 0, proc.stderr
        assert "httpx" in proc.stdout

    def test_missing_wheels_message_is_actionable(self, tmp_path):
        app = _make_app_dir(tmp_path)
        proc = source_lib(
            'missing_wheels_message "fastapi, newpkg" "1.0.0"; echo\n',
            app,
        )
        assert proc.returncode == 0, proc.stderr
        msg = proc.stdout.strip()
        assert msg.startswith("Missing wheels: fastapi, newpkg.")
        assert "build-wheelhouse.sh" in msg
        assert "copy that folder onto the Pi" in msg
        assert "still on 1.0.0" in msg


class TestUpdateShScript:
    """End of the script before systemd: idle / same / older / missing wheels / HMAC."""

    def test_idle_when_nothing_found(self, tmp_path):
        app = _make_app_dir(tmp_path)
        proc = run_update(app)
        assert proc.returncode == 0, proc.stderr
        status = _status(app)
        assert status["state"] == "idle"
        assert "USB" in status["message"]

    def test_up_to_date_same_version(self, tmp_path):
        app = _make_app_dir(tmp_path, version="2.0.0")
        _plant_tree(app / "media" / "locker" / "STICK", version="2.0.0")
        proc = run_update(app)
        assert proc.returncode == 0, proc.stderr
        assert _status(app)["state"] == "up_to_date"

    def test_refuses_older_tree(self, tmp_path):
        app = _make_app_dir(tmp_path, version="2.0.0")
        _plant_tree(app / "media" / "locker" / "STICK", version="1.0.0")
        proc = run_update(app)
        assert proc.returncode == 1
        status = _status(app)
        assert status["state"] == "failed"
        assert "older" in status["message"]

    @pytest.mark.skipif(RSYNC is None, reason="rsync required to stage the USB tree")
    def test_missing_wheels_status_names_packages(self, tmp_path):
        app = _make_app_dir(tmp_path, version="1.0.0")
        _plant_tree(
            app / "media" / "locker" / "STICK",
            version="2.0.0",
            reqs="fastapi>=0.115.0\nnewpkg>=1.0.0\n",
        )
        proc = run_update(app)
        assert proc.returncode == 1
        status = _status(app)
        assert status["state"] == "failed"
        assert "Missing wheels:" in status["message"]
        assert "newpkg" in status["message"]
        assert "fastapi" in status["message"]
        assert "build-wheelhouse.sh" in status["message"]
        assert "USB" in status["message"]

    @pytest.mark.skipif(RSYNC is None, reason="rsync required to stage the USB tree")
    def test_usb_tree_does_not_require_hmac(self, tmp_path):
        app = _make_app_dir(tmp_path, version="1.0.0")
        _plant_tree(app / "media" / "locker" / "STICK", version="2.0.0")
        proc = run_update(app, extra={"SMART_LOCKER_UPDATE_HMAC_KEY": ""})
        assert proc.returncode == 1
        status = _status(app)
        assert status["state"] == "failed"
        assert "HMAC" not in status["message"]
        assert "Missing wheels:" in status["message"]

    def test_tarball_fallback_still_requires_hmac(self, tmp_path):
        app = _make_app_dir(tmp_path, version="1.0.0")
        tarball = app / "cifs-updates" / "smart-locker-9.0.0.tar.gz"
        tarball.write_bytes(b"unsigned")
        proc = run_update(app)
        assert proc.returncode == 1
        status = _status(app)
        assert status["state"] == "failed"
        assert "unsigned" in status["message"].lower() or "hmac" in status["message"].lower()
