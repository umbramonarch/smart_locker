"""
File: test_pack_release.py
Description: Tests for pack_release — HMAC sidecar round-trip, dirty-tree
             refusal, and a clean git-archive tarball plus sidecar.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_pack_release.py -v
       Uses tmp_path git repos only; does not pack this working tree.
"""

import hashlib
import hmac
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from scripts.pack_release import main as pack_release_main
from scripts.pack_release import sign_file


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git in ``cwd`` and fail the test if the command fails.

    Args:
        cwd: Repository working tree.
        *args: git arguments after the executable name.

    Returns:
        Completed process with captured text output.
    """
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_git_repo(root: Path) -> None:
    """Create a committed git repo at ``root`` with a tracked file.

    Args:
        root: Empty directory that becomes the repository.
    """
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "hello.txt").write_text("tracked-file\n", encoding="utf-8")
    _git(root, "add", "hello.txt")
    _git(root, "commit", "-m", "init")


class TestSignFile:
    """HMAC sidecar round-trip for ``sign_file``."""

    def test_sign_file_hmac_round_trip(self, tmp_path, monkeypatch):
        key = "test-update-hmac-key"
        monkeypatch.setenv("SMART_LOCKER_UPDATE_HMAC_KEY", key)
        tarball = tmp_path / "smart-locker-test.tar.gz"
        data = b"release-bytes"
        tarball.write_bytes(data)

        sidecar = Path(sign_file(str(tarball)))

        assert sidecar == tmp_path / "smart-locker-test.tar.gz.hmac"
        expected = hmac.new(key.encode("utf-8"), data, hashlib.sha256).hexdigest()
        assert sidecar.read_text(encoding="utf-8") == expected + "\n"


class TestPackRelease:
    """Dirty-tree refusal and clean git-archive pack + HMAC sidecar."""

    def test_pack_release_refuses_dirty_tree(self, tmp_path, monkeypatch):
        _init_git_repo(tmp_path)
        (tmp_path / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sys, "argv", ["pack_release"])

        with pytest.raises(SystemExit) as excinfo:
            pack_release_main()

        assert excinfo.value.code != 0

    def test_pack_release_clean_repo_tarball_and_hmac(self, tmp_path, monkeypatch):
        key = "test-update-hmac-key"
        monkeypatch.setenv("SMART_LOCKER_UPDATE_HMAC_KEY", key)

        repo = tmp_path / "repo"
        out = tmp_path / "out"
        repo.mkdir()
        out.mkdir()
        _init_git_repo(repo)
        (repo / ".gitignore").write_text(".env\nvenv/\n", encoding="utf-8")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-m", "ignore secrets and venv")
        # Ignored so porcelain stays empty while they still exist on disk.
        (repo / ".env").write_text("SHOULD_NOT_BE_PACKED=1\n", encoding="utf-8")
        venv_dir = repo / "venv"
        venv_dir.mkdir()
        (venv_dir / "not-a-wheel.txt").write_text("wheelhouse-stand-in\n", encoding="utf-8")

        monkeypatch.chdir(repo)
        monkeypatch.setattr(sys, "argv", ["pack_release", "HEAD", str(out)])
        pack_release_main()

        tarballs = list(out.glob("smart-locker-*.tar.gz"))
        assert len(tarballs) == 1
        tarball = tarballs[0]
        sidecar = Path(str(tarball) + ".hmac")
        assert sidecar.is_file()

        with tarfile.open(tarball, "r:gz") as tf:
            names = tf.getnames()
        assert "smart-locker/hello.txt" in names
        assert all(
            name == "smart-locker" or name.startswith("smart-locker/")
            for name in names
        )
        assert not any(
            part == ".env" or part == "venv" for name in names for part in name.split("/")
        )

        data = tarball.read_bytes()
        expected = hmac.new(key.encode("utf-8"), data, hashlib.sha256).hexdigest()
        assert sidecar.read_text(encoding="utf-8").strip() == expected
