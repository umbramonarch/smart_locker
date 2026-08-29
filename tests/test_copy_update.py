"""
File: test_copy_update.py
Description: Tests for python -m scripts.copy_update — payload copy into
             locker-updates/, USB dest, skip runtime files, and missing-wheel
             warnings that do not abort.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_copy_update.py -v
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from scripts import copy_update


def _plant_repo(root: Path, *, reqs: str | None = None) -> Path:
    """Create a minimal checkout that copy_update will accept."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "smart_locker").mkdir()
    (root / "smart_locker" / "app.py").write_text("# app\n", encoding="utf-8")
    (root / "config").mkdir()
    (root / "config" / "settings.py").write_text("# settings\n", encoding="utf-8")
    (root / "scripts").mkdir()
    (root / "scripts" / "copy_update.py").write_text("# copy\n", encoding="utf-8")
    (root / "deploy" / "install").mkdir(parents=True)
    (root / "deploy" / "install" / "update.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (root / "deploy" / "wheelhouse").mkdir(parents=True)
    (root / "requirements.txt").write_text(
        reqs if reqs is not None else "fastapi>=0.115.0\nnewpkg>=1.0.0\n",
        encoding="utf-8",
    )
    (root / "GUIDE.md").write_text("guide\n", encoding="utf-8")
    (root / ".env").write_text("SMART_LOCKER_ENC_KEY=secret\n", encoding="utf-8")
    (root / "smart_locker.db").write_text("db\n", encoding="utf-8")
    (root / "venv").mkdir()
    (root / "venv" / "win.bin").write_text("nope\n", encoding="utf-8")
    (root / "logs").mkdir()
    (root / "logs" / "noise.log").write_text("nope\n", encoding="utf-8")
    (root / "backups").mkdir()
    (root / "backups" / "old.tgz").write_text("nope\n", encoding="utf-8")
    return root


class TestMissingWheelNames:
    """Wheel warnings name packages and skip pyscard."""

    def test_names_packages_without_wheels(self, tmp_path):
        repo = _plant_repo(tmp_path / "repo")
        (repo / "deploy" / "wheelhouse" / "fastapi-0.115.0-py3-none-any.whl").write_bytes(
            b"whl"
        )
        (repo / "requirements.txt").write_text(
            "pyscard>=2.0.7\nfastapi>=0.115.0\npython-dotenv>=1.0.0\nnewpkg>=1.0\n",
            encoding="utf-8",
        )
        missing = copy_update.missing_wheel_names(
            repo / "requirements.txt", repo / "deploy" / "wheelhouse"
        )
        assert "newpkg" in missing
        assert "python-dotenv" in missing
        assert "fastapi" not in missing
        assert "pyscard" not in missing

    def test_warn_does_not_raise(self, tmp_path):
        repo = _plant_repo(tmp_path / "repo")
        buf = io.StringIO()
        missing = copy_update.warn_missing_wheels(repo, file=buf)
        assert "newpkg" in missing
        text = buf.getvalue()
        assert text.startswith("WARNING: missing wheels for:")
        assert "newpkg" in text
        assert "build-wheelhouse.sh" in text


class TestCopyPayload:
    """locker-updates/ is a repo tree without live runtime files."""

    def test_copies_needed_files_skips_runtime(self, tmp_path):
        repo = _plant_repo(tmp_path / "repo")
        dest = tmp_path / "locker-updates"
        copy_update.copy_payload(repo, dest)
        assert (dest / "smart_locker" / "app.py").is_file()
        assert (dest / "requirements.txt").is_file()
        assert (dest / "deploy" / "install" / "update.sh").is_file()
        assert (dest / "VERSION").is_file()
        assert not (dest / ".env").exists()
        assert not (dest / "smart_locker.db").exists()
        assert not (dest / "venv").exists()
        assert not (dest / "logs").exists()
        assert not (dest / "backups").exists()

    def test_copies_wheelhouse_wheels_when_present(self, tmp_path):
        repo = _plant_repo(tmp_path / "repo")
        (repo / "deploy" / "wheelhouse" / "fastapi-0.1.0-py3-none-any.whl").write_bytes(
            b"whl"
        )
        dest = tmp_path / "locker-updates"
        copy_update.copy_payload(repo, dest)
        assert (dest / "deploy" / "wheelhouse" / "fastapi-0.1.0-py3-none-any.whl").is_file()


class TestMain:
    """CLI writes repo locker-updates/ and optional --dest."""

    def test_main_writes_local_and_dest(self, tmp_path, monkeypatch):
        repo = _plant_repo(tmp_path / "repo")
        usb = tmp_path / "E_DRIVE"
        usb.mkdir()
        code = copy_update.main(["--repo", str(repo), "--dest", str(usb)])
        assert code == 0
        local = repo / "locker-updates"
        assert (local / "smart_locker" / "app.py").is_file()
        assert (usb / "locker-updates" / "smart_locker" / "app.py").is_file()
        assert not (usb / "locker-updates" / ".env").exists()

    def test_main_returns_zero_when_wheels_missing(self, tmp_path):
        repo = _plant_repo(tmp_path / "repo")
        assert copy_update.main(["--repo", str(repo)]) == 0
        assert (repo / "locker-updates" / "requirements.txt").is_file()

    def test_auto_detect_single_removable(self, tmp_path, monkeypatch):
        repo = _plant_repo(tmp_path / "repo")
        drive = tmp_path / "D_DRIVE"
        drive.mkdir()
        monkeypatch.setattr(copy_update, "list_removable_drives", lambda: [drive])
        assert copy_update.main(["--repo", str(repo)]) == 0
        assert (drive / "locker-updates" / "smart_locker" / "app.py").is_file()

    def test_several_drives_require_dest(self, tmp_path, monkeypatch):
        repo = _plant_repo(tmp_path / "repo")
        monkeypatch.setattr(
            copy_update,
            "list_removable_drives",
            lambda: [tmp_path / "D", tmp_path / "E"],
        )
        with pytest.raises(SystemExit) as excinfo:
            copy_update.main(["--repo", str(repo)])
        assert excinfo.value.code == 2
