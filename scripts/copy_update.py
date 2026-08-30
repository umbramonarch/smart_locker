"""
File: copy_update.py
Description: Package needed repo files into gitignored locker-updates/ and
             copy that folder onto a USB stick for Pi Software Update.
Project: smart_locker/scripts
Notes: Usage:
         python -m scripts.copy_update
         python -m scripts.copy_update --dest D:\\
       Always writes <repo>/locker-updates/. --dest is a drive or folder;
       the payload lands at <dest>/locker-updates. On Windows, a single
       removable drive is used when --dest is omitted. Warns about
       requirements.txt packages that have no matching wheel in
       deploy/wheelhouse (does not abort). Unpacked tree only — not an archive.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PAYLOAD_DIRNAME = "locker-updates"

# Needed on the Pi for an in-field update. Runtime secrets and venv stay off.
PAYLOAD_DIRS = ("smart_locker", "config", "scripts", "deploy")
PAYLOAD_FILES = (
    "requirements.txt",
    "VERSION",
    ".python-version",
    "GUIDE.md",
    "PROJECT-NOTES.md",
    "README.md",
    "CHANGELOG.md",
)

SKIP_DIR_NAMES = {
    ".git",
    ".venv",
    "venv",
    "logs",
    "backups",
    "locker-updates",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "node_modules",
}
SKIP_FILE_NAMES = {".env"}


def missing_wheel_names(
    requirements_path: Path, wheelhouse: Path
) -> list[str]:
    """Return requirement names that have no matching ``.whl`` in ``wheelhouse``.

    ``pyscard`` is omitted (Pi installs it from a ``.deb``, not the wheelhouse).

    Args:
        requirements_path: ``requirements.txt``.
        wheelhouse: ``deploy/wheelhouse`` directory.

    Returns:
        Display names from the requirements file, in file order.
    """
    if not requirements_path.is_file():
        return []

    def norm(name: str) -> str:
        name = re.split(r"[<>=!~;\[]", name.strip(), 1)[0].strip()
        return re.sub(r"[-_.]+", "-", name).lower()

    def wheel_dist(filename: str) -> str:
        match = re.match(r"^(.+?)-(\d.*)\.whl$", filename)
        if match:
            return norm(match.group(1))
        return norm(filename.rsplit(".whl", 1)[0])

    names: list[str] = []
    for raw in requirements_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name = re.split(r"[<>=!~;\[]", line, 1)[0].strip()
        if not name or name.lower() == "pyscard":
            continue
        names.append(name)

    wheels = (
        {wheel_dist(path.name) for path in wheelhouse.glob("*.whl")}
        if wheelhouse.is_dir()
        else set()
    )
    missing: list[str] = []
    seen: set[str] = set()
    for name in names:
        key = norm(name)
        if key not in wheels and key not in seen:
            missing.append(name)
            seen.add(key)
    return missing


def warn_missing_wheels(
    repo: Path, *, file: object | None = None
) -> list[str]:
    """Print a warning for packages with no wheel; never aborts the copy.

    Args:
        repo: Repository root.
        file: Stream for the warning (default stderr).

    Returns:
        Missing package names.
    """
    out = file if file is not None else sys.stderr
    missing = missing_wheel_names(
        repo / "requirements.txt", repo / "deploy" / "wheelhouse"
    )
    if not missing:
        return missing
    joined = ", ".join(missing)
    print(
        f"WARNING: missing wheels for: {joined}. "
        "Rebuild deploy/wheelhouse on Windows "
        "(deploy/install/build-wheelhouse.sh) and re-run this command "
        "if the Pi does not already have them.",
        file=out,
    )
    return missing


def payload_ignore(_directory: str, names: list[str]) -> set[str]:
    """Ignore runtime files and nested payload folders while copying.

    Args:
        _directory: Current directory (unused; shutil callback).
        names: Entry names in that directory.

    Returns:
        Names to skip.
    """
    skipped = set()
    for name in names:
        if name in SKIP_DIR_NAMES or name in SKIP_FILE_NAMES:
            skipped.add(name)
            continue
        if name.endswith(".db") or name.endswith(".db-wal") or name.endswith(".db-shm"):
            skipped.add(name)
    return skipped


def write_version_file(dest: Path, repo: Path) -> str:
    """Write a VERSION marker from git describe, an existing VERSION, or mtime.

    Args:
        dest: Payload directory.
        repo: Repository root (for git / VERSION).

    Returns:
        Version string written.
    """
    version = ""
    existing = repo / "VERSION"
    if existing.is_file():
        version = existing.read_text(encoding="utf-8").strip()
    if not version:
        try:
            result = subprocess.run(
                ["git", "describe", "--tags", "--always"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                version = result.stdout.strip().replace("/", "-")
        except FileNotFoundError:
            version = ""
    if not version:
        reqs = repo / "requirements.txt"
        stamp = int(reqs.stat().st_mtime) if reqs.is_file() else 0
        version = f"mtime-{stamp}"
    (dest / "VERSION").write_text(version + "\n", encoding="utf-8")
    return version


def copy_payload(repo: Path, dest: Path) -> None:
    """Copy needed repo files into ``dest`` (a locker-updates tree).

    Args:
        repo: Repository root.
        dest: Destination payload directory.

    Raises:
        FileNotFoundError: If ``repo`` is not a smart_locker tree.
    """
    if not (repo / "smart_locker" / "app.py").is_file():
        raise FileNotFoundError(f"Not a smart_locker repo: {repo}")
    if dest.resolve() == repo.resolve():
        raise ValueError("Refusing to copy the live repo onto itself.")

    dest.mkdir(parents=True, exist_ok=True)
    for name in PAYLOAD_DIRS:
        src = repo / name
        if not src.is_dir():
            continue
        target = dest / name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(src, target, ignore=payload_ignore)
    for name in PAYLOAD_FILES:
        src = repo / name
        if src.is_file():
            shutil.copy2(src, dest / name)
    write_version_file(dest, repo)


def usb_payload_dir(dest: Path) -> Path:
    """Return ``dest/locker-updates`` unless ``dest`` is already that folder.

    Args:
        dest: Drive root or an explicit locker-updates path.

    Returns:
        Directory that should hold the repo tree.
    """
    dest = dest.expanduser()
    if dest.name == PAYLOAD_DIRNAME:
        return dest
    return dest / PAYLOAD_DIRNAME


def list_removable_drives() -> list[Path]:
    """Return Windows removable drive roots (empty on other platforms).

    Returns:
        Paths such as ``D:\\``.
    """
    if sys.platform != "win32":
        return []
    import ctypes

    drives: list[Path] = []
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    for index in range(26):
        if not bitmask & (1 << index):
            continue
        letter = f"{chr(ord('A') + index)}:\\"
        drive_type = ctypes.windll.kernel32.GetDriveTypeW(letter)
        # DRIVE_REMOVABLE = 2
        if drive_type == 2:
            drives.append(Path(letter))
    return drives


def resolve_usb_dest(dest_arg: str | None) -> Path | None:
    """Pick a USB destination from ``--dest`` or a single removable drive.

    Args:
        dest_arg: ``--dest`` value, or None to auto-detect.

    Returns:
        Payload directory, or None if nothing to copy onto (repo-only).

    Raises:
        SystemExit: If several removable drives exist and ``--dest`` is omitted.
    """
    if dest_arg:
        return usb_payload_dir(Path(dest_arg))
    drives = list_removable_drives()
    if len(drives) == 1:
        return usb_payload_dir(drives[0])
    if len(drives) > 1:
        letters = ", ".join(str(path) for path in drives)
        print(
            f"Several removable drives: {letters}. Pass --dest D:\\",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return None


def main(argv: list[str] | None = None) -> int:
    """Fill locker-updates/ and optionally copy it onto a USB stick.

    Args:
        argv: CLI arguments without the program name.

    Returns:
        Process exit code (0 on success even when wheels are missing).
    """
    parser = argparse.ArgumentParser(
        description=(
            "Copy needed smart_locker files into locker-updates/ and onto a USB stick."
        )
    )
    parser.add_argument(
        "--dest",
        help="USB drive or folder (payload written to <dest>/locker-updates).",
    )
    parser.add_argument(
        "--repo",
        default=str(REPO_ROOT),
        help="Repository root (default: this checkout).",
    )
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()

    local = repo / PAYLOAD_DIRNAME
    if local.exists():
        shutil.rmtree(local)
    copy_payload(repo, local)
    print(f"Wrote {local}")

    usb = resolve_usb_dest(args.dest)
    if usb is not None:
        if usb.exists():
            shutil.rmtree(usb)
        shutil.copytree(local, usb)
        print(f"Wrote {usb}")
    else:
        print(
            "No USB destination (pass --dest D:\\, or plug in one removable drive).",
            file=sys.stderr,
        )

    warn_missing_wheels(repo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
