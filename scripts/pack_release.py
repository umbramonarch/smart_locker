"""
File: pack_release.py
Description: Pack a git-tracked source tree into a signed release tarball
             (smart-locker-<id>.tar.gz plus HMAC sidecar) for update.sh.
Project: smart_locker/scripts
Notes: Usage:
         python -m scripts.pack_release
         python -m scripts.pack_release <ref>
         python -m scripts.pack_release <ref> <output-dir>
       Defaults: ref=HEAD, output-dir=cwd. Refuses a dirty working tree.
       HMAC-SHA256 sidecar uses SMART_LOCKER_UPDATE_HMAC_KEY (env string,
       same as openssl dgst -hmac in update.sh). git archive emits tracked
       files only — no .env, venv, or database.
"""

import hashlib
import hmac
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

KEY_ENV_VAR = "SMART_LOCKER_UPDATE_HMAC_KEY"


def sign_file(tarball_path: str) -> str:
    """Write an HMAC-SHA256 sidecar next to ``tarball_path``.

    Uses ``SMART_LOCKER_UPDATE_HMAC_KEY`` as a UTF-8 string (the same bytes
    ``update.sh`` passes to ``openssl dgst -sha256 -hmac``). Writes
    ``<tarball_path>.hmac`` with the hex digest and a trailing newline.

    Args:
        tarball_path: Path to the release tarball.

    Returns:
        Path of the written sidecar.

    Raises:
        SystemExit: If the key is missing or the tarball does not exist.
    """
    key = os.getenv(KEY_ENV_VAR)
    if not key:
        print(
            f"Missing {KEY_ENV_VAR} in .env — generate one with: python -m scripts.generate_key",
            file=sys.stderr,
        )
        raise SystemExit(1)

    if not os.path.isfile(tarball_path):
        print(f"Tarball not found: {tarball_path}", file=sys.stderr)
        raise SystemExit(1)

    with open(tarball_path, "rb") as f:
        digest = hmac.new(key.encode("utf-8"), f.read(), hashlib.sha256).hexdigest()

    sidecar_path = tarball_path + ".hmac"
    with open(sidecar_path, "w", encoding="utf-8") as f:
        f.write(digest + "\n")

    return sidecar_path


def _sanitize_version_id(version_id: str) -> str:
    """Make a ``git describe`` string safe for a tarball filename.

    Replaces ``/`` and any whitespace with ``-`` so refs like ``feature/foo``
    do not create subdirectories in the output path.

    Args:
        version_id: Raw ``git describe --tags --always`` output.

    Returns:
        Filename-safe version id.
    """
    return "".join("-" if ch == "/" or ch.isspace() else ch for ch in version_id)


def _run_git(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run git with ``args`` in the current working directory.

    Args:
        args: git arguments after the executable name.

    Returns:
        Completed process (stdout/stderr captured as text).

    Raises:
        SystemExit: If git is not on PATH.
    """
    try:
        return subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        print("git is required but was not found on PATH.", file=sys.stderr)
        raise SystemExit(1)


def _git_or_exit(args: list[str], fallback: str) -> str:
    """Run git and return stripped stdout, or exit 1 with stderr.

    Args:
        args: git arguments after the executable name.
        fallback: Message if git produced no stderr/stdout on failure.

    Returns:
        Stripped stdout from a successful git command.

    Raises:
        SystemExit: If git is missing or the command fails.
    """
    result = _run_git(args)
    if result.returncode != 0:
        print((result.stderr or result.stdout or fallback).strip(), file=sys.stderr)
        raise SystemExit(1)
    return result.stdout.strip()


def main() -> None:
    """Pack ``<ref>`` into a signed ``smart-locker-<id>.tar.gz`` and sidecar.

    Requires a clean git work tree. Version id comes from
    ``git describe --tags --always <ref>``. The archive is tracked files
    only (``git archive``), then HMAC-signed.

    Raises:
        SystemExit: Exit 2 on wrong argc; exit 1 if git is missing, this is
            not a repository, the tree is dirty, describe/archive fails, or
            signing fails.
    """
    if len(sys.argv) > 3:
        print(
            "Usage: python -m scripts.pack_release [<ref> [<output-dir>]]",
            file=sys.stderr,
        )
        raise SystemExit(2)

    ref = sys.argv[1] if len(sys.argv) >= 2 else "HEAD"
    output_dir = sys.argv[2] if len(sys.argv) == 3 else os.getcwd()

    status = _run_git(["status", "--porcelain"])
    if status.returncode != 0:
        print(
            (status.stderr or status.stdout or "git status failed.").strip(),
            file=sys.stderr,
        )
        raise SystemExit(1)
    if status.stdout.strip():
        print("Refusing to pack: git working tree is dirty.", file=sys.stderr)
        raise SystemExit(1)

    version_id = _sanitize_version_id(
        _git_or_exit(
            ["describe", "--tags", "--always", ref],
            fallback=f"Could not describe ref {ref!r}.",
        )
    )
    if not version_id:
        print(f"Could not describe ref {ref!r}.", file=sys.stderr)
        raise SystemExit(1)

    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    tarball_path = str(out_dir / f"smart-locker-{version_id}.tar.gz")
    _git_or_exit(
        [
            "archive",
            "--format=tar.gz",
            "--prefix=smart-locker/",
            "-o",
            tarball_path,
            ref,
        ],
        fallback="git archive failed.",
    )

    sidecar_path = sign_file(tarball_path)
    print(tarball_path)
    print(sidecar_path)


if __name__ == "__main__":
    main()
