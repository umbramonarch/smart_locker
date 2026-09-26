"""
File: update_sandbox.py
Description: End-to-end sandbox for deploy/install/update.sh. Builds a
             throwaway appliance tree — old version, .env, real SQLite DB,
             stub venv — puts fake systemctl/sudo/curl/pip on PATH, and runs
             the REAL update script under bash exactly as the Pi runs it.
Project: smart_locker/tests/e2e
Notes: No systemd and no locker service exist on a dev/CI box, so systemctl,
       sudo, and curl are fakes and pip is a stub; discovery, version checks,
       the SQLite online backup, the rsync code swap, ``python -m
       scripts.migrate_db`` (the real script, copied into the payload), the
       /api/health gate, and rollback all run for real. An rsync shim is
       installed only on hosts without rsync (Git Bash on Windows); where a
       real rsync exists it is used. Payload markers: a BOOT_FAIL file in the
       incoming tree makes the fake service never come up, PIP_FAIL fails the
       pip step, and a payload migrate_db.py exiting nonzero fails the
       migration. SMART_LOCKER_UPDATE_LIB is the script's own seam and is NOT
       used — every test runs the whole script end to end.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE_SH = REPO_ROOT / "deploy" / "install" / "update.sh"


def _posix(p) -> str:
    """Return a POSIX-style path usable inside MSYS2/Git Bash.

    ``D:\\x\\y`` becomes ``/d/x/y``; POSIX hosts are returned unchanged.
    """
    s = str(p)
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", s)
    if m:
        return "/" + m.group(1).lower() + "/" + m.group(2).replace("\\", "/")
    return s.replace("\\", "/")


def find_bash() -> str | None:
    """Locate a usable bash, skipping the WindowsApps/System32 WSL stubs."""
    candidates = [shutil.which("bash")]
    git = shutil.which("git")
    if git:
        root = Path(git).resolve().parent.parent  # <Git>/cmd/git.exe or <Git>/bin/git.exe
        candidates += [root / "usr" / "bin" / "bash.exe", root / "bin" / "bash.exe"]
    candidates += ["/usr/bin/bash", "C:/Program Files/Git/bin/bash.exe"]
    seen = set()
    for cand in candidates:
        if not cand:
            continue
        c = str(cand)
        if c in seen or not Path(c).exists():
            continue
        seen.add(c)
        low = c.lower().replace("/", "\\")
        if "system32" in low or "windowsapps" in low:
            continue  # WSL launcher — not the shell we can drive here
        try:
            out = subprocess.run(
                [c, "--version"], capture_output=True, text=True, timeout=10
            )
        except OSError:
            continue
        if out.returncode == 0 and re.search(
            r"cygwin|msys|mingw|linux-gnu|darwin", out.stdout
        ):
            return c
    return None


def gnu_tools_available(bash: str) -> bool:
    """Probe the GNU-isms update.sh relies on (``sort -V``, ``xargs -r``,
    GNU tar's ``--warning``, ``stat -c``). The script itself only ever runs
    on the Pi's Debian userland; a stock BSD toolchain (e.g. macOS) would
    misbehave rather than fail cleanly, so the suite skips such hosts."""
    probe = (
        'printf "10\\n9\\n" | sort -V | head -n1 | grep -qx 9'
        ' && printf "" | xargs -r true'
        ' && tar --warning=no-file-changed --version >/dev/null 2>&1'
        ' && stat -c %Y . >/dev/null 2>&1'
    )
    try:
        return (
            subprocess.run(
                [bash, "-c", probe], capture_output=True, timeout=20
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


# --- Fake system boundary (written into <tmp>/bin and prepended to PATH) ----

_FAKE_SUDO = """\
#!/usr/bin/env bash
# Fake sudo: strip -n and exec the command (which resolves to our fakes).
args=()
for a in "$@"; do
  case "$a" in
    -n) ;;
    *) args+=("$a") ;;
  esac
done
exec "${args[@]}"
"""

_FAKE_SYSTEMCTL = """\
#!/usr/bin/env bash
# Fake systemctl: the locker "service" is a state file under $SL_E2E_ROOT.
# `start` only brings it up when the installed code can boot — a BOOT_FAIL
# file at the app root (shipped by a bad payload) keeps it down. A
# FORCE_START_FAIL marker at the sandbox root keeps even the rolled-back
# service down, driving the rollback_unhealthy outcome.
printf 'systemctl %s\\n' "$*" >> "$SL_E2E_ROOT/systemctl.log"
case "${1:-}" in
  stop)
    printf 'stopped\\n' > "$SL_E2E_ROOT/service.state" ;;
  start)
    if [ -f "$SL_E2E_ROOT/FORCE_START_FAIL" ] || \\
       [ -f "$SMART_LOCKER_DIR/BOOT_FAIL" ]; then
      printf 'failed\\n' > "$SL_E2E_ROOT/service.state"
    else
      printf 'running\\n' > "$SL_E2E_ROOT/service.state"
    fi ;;
esac
exit 0
"""

_FAKE_CURL = """\
#!/usr/bin/env bash
# Fake curl: answers the updater's /api/health probe from the fake service
# state. When the service is "running" it reports the installed tree's
# VERSION file, so a rollback is observable as the old version answering.
# Every emitted answer is appended to curl.log so tests see exactly what the
# health gate saw — including which URL was probed.
state="stopped"
[ -f "$SL_E2E_ROOT/service.state" ] && state="$(cat "$SL_E2E_ROOT/service.state")"
if [ "$state" = "running" ]; then
  ver=""
  [ -f "$SMART_LOCKER_DIR/VERSION" ] && \\
    ver="$(tr -d '[:space:]' < "$SMART_LOCKER_DIR/VERSION")"
  body="$(printf '{"status":"ok","version":"%s"}' "${ver:-unknown}")"
  printf '%s | curl %s\\n' "$body" "$*" >> "$SL_E2E_ROOT/curl.log"
  printf '%s\\n' "$body"
  exit 0
fi
exit 7
"""

_FAKE_PIP = """\
#!/usr/bin/env bash
# venv pip stub: logs the install; fails when the new tree carries PIP_FAIL.
# A real install mutates the venv — drop a marker (also on failure: a
# half-installed venv is the dangerous rollback case) so tests can prove the
# venv itself was restored.
printf 'pip %s\\n' "$*" >> "$SL_E2E_ROOT/pip.log"
printf 'mutated\\n' > "$SMART_LOCKER_DIR/venv/PIP_MUTATED"
if [ -f "$SMART_LOCKER_DIR/PIP_FAIL" ]; then
  echo "pip: PIP_FAIL marker present — simulating wheel install failure" >&2
  exit 1
fi
exit 0
"""


_NEXT_REAL = """\
# Resolve the real tool: the first executable of this name on PATH that is
# not this shim itself. String compare cannot spot the shim — MSYS aliases
# the Windows temp dir as /tmp — so use file identity (-ef).
real=""
_oldifs="$IFS"; IFS=':'
for _d in $PATH; do
  _cand="$_d/__NAME__"
  [ -x "$_cand" ] || continue
  [ "$_cand" -ef "$0" ] && continue
  real="$_cand"
  break
done
IFS="$_oldifs"
"""


def _fake_tar() -> str:
    """tar that delegates to the real one — except a TAR_CREATE_FAIL marker
    under the sandbox root forces archive *creation* to fail (extraction
    still works), covering the code-snapshot step's ERR path for real."""
    return (
        """\
#!/usr/bin/env bash
if [ -f "$SL_E2E_ROOT/TAR_CREATE_FAIL" ]; then
  for a in "$@"; do
    case "$a" in -c*|--create)
      echo "tar: forced create failure for e2e" >&2
      exit 1 ;;
    esac
  done
fi
"""
        + _NEXT_REAL.replace("__NAME__", "tar")
        + """\
if [ -z "$real" ]; then echo "tar shim: real tar not found on PATH" >&2; exit 127; fi
exec "$real" "$@"
"""
    )


def _perm_wrap(name: str) -> str:
    """chown/chmod shim: records the call in perms.log, then delegates to the
    real tool found later on PATH (never this shim). Git Bash cannot apply
    POSIX ownership, but the logged calls prove rollback re-applies the
    runtime fixups; on the Pi they apply for real."""
    return (
        f"""\
#!/usr/bin/env bash
printf '%s %s\\n' "{name}" "$*" >> "$SL_E2E_ROOT/perms.log"
"""
        + _NEXT_REAL.replace("__NAME__", name)
        + """\
if [ -n "$real" ]; then exec "$real" "$@"; fi
exit 0
"""
    )

_VENV_PYTHON = """\
#!/usr/bin/env bash
# venv python -> the interpreter running pytest. Under MSYS2 the script's
# env carries POSIX paths; a native Python needs Windows ones, so translate
# the DB path env var before exec (argv paths are auto-converted by MSYS2).
if command -v cygpath >/dev/null 2>&1; then
  if [ -n "${SMART_LOCKER_DB_PATH:-}" ]; then
    export SMART_LOCKER_DB_PATH="$(cygpath -w "$SMART_LOCKER_DB_PATH")"
  fi
fi
exec "__PY__" "$@"
"""

_RSYNC_SHIM = """\
#!/usr/bin/env bash
# Minimal rsync stand-in for hosts without rsync (e.g. Git Bash). Supports
# exactly what update.sh uses: `rsync -a [--delete] [--exclude=PAT]... SRC/ DST/`.
# Excludes: `/path` anchors to the transfer root (children included), other
# patterns match the basename (e.g. `*.db`). Excluded dest entries are never
# deleted, like real rsync. Approximation: dir mtimes/ownership are not
# preserved, and a non-excluded dir deleted wholesale can take a kept
# `*.db` with it — neither occurs in the trees these tests build.
set -euo pipefail
delete=0
excludes=()
pos=()
for a in "$@"; do
  case "$a" in
    --delete) delete=1 ;;
    --exclude=*) excludes+=("${a#--exclude=}") ;;
    -*) ;;
    *) pos+=("$a") ;;
  esac
done
if [ "${#pos[@]}" -lt 2 ]; then
  echo "rsync-shim: need SRC DST" >&2
  exit 2
fi
src="${pos[0]%/}"
dst="${pos[1]%/}"

_excluded() {
  local rel="$1" pat p base
  base="${rel##*/}"
  for pat in "${excludes[@]}"; do
    p="${pat%/}"
    case "$p" in
      /*)
        p="${p#/}"
        if [ "$rel" = "$p" ] || [ "${rel#"$p/"}" != "$rel" ]; then
          return 0
        fi ;;
      *)
        if [[ "$base" == $p ]]; then
          return 0
        fi ;;
    esac
  done
  return 1
}

mkdir -p "$dst"
while IFS= read -r -d '' it; do
  rel="${it#./}"
  if _excluded "$rel"; then continue; fi
  if [ -d "$src/$rel" ]; then
    mkdir -p "$dst/$rel"
  else
    mkdir -p "$(dirname "$dst/$rel")"
    cp -p "$src/$rel" "$dst/$rel"
  fi
done < <(cd "$src" && find . -mindepth 1 -print0)

if [ "$delete" = "1" ]; then
  while IFS= read -r -d '' it; do
    rel="${it#./}"
    if _excluded "$rel"; then continue; fi
    if [ ! -e "$src/$rel" ] && [ ! -L "$src/$rel" ]; then
      rm -rf "$dst/$rel"
    fi
  done < <(cd "$dst" && find . -mindepth 1 -depth -print0)
fi
exit 0
"""

_APPLY_SUDOERS_STUB = """\
#!/usr/bin/env bash
# Payload stub for apply-sudoers.sh — proves update.sh ran the refresh step.
echo "apply-sudoers ran" >> "$SMART_LOCKER_DIR/logs/sudoers.log"
"""

_MIGRATE_FAIL = """\
import sys

print("migrate_db: forced failure for e2e", flush=True)
sys.exit(1)
"""


class UpdateSandbox:
    """One throwaway appliance: old code tree, runtime files, fake services."""

    def __init__(self, root: Path, bash: str, *, health_timeout: int = 6):
        self.bash = bash
        self.root = root
        self.app = root / "app"            # SMART_LOCKER_DIR
        self.bin = root / "bin"            # prepended to PATH
        self.db = self.app / "smart_locker.db"
        self.env_file = self.app / ".env"
        self.updates_dir = self.app / "locker-updates"
        self.health_timeout = health_timeout

    # -- construction ------------------------------------------------------

    def build(self, *, include_calibration_column: bool = True) -> "UpdateSandbox":
        self.bin.mkdir(parents=True)
        self.app.mkdir(parents=True)
        (self.app / "logs").mkdir()
        self._write_fakes()
        self._write_venv()
        self._write_tree(
            self.app,
            {
                "VERSION": "1.0.0\n",
                "smart_locker/__init__.py": "",
                "smart_locker/app.py": 'APP_MARK = "old-1.0.0"\n',
                # A committed UI asset and a runtime device photo — a rollback
                # must keep the photo while removing payload-only assets.
                "smart_locker/frontend/images/hero_bg.jpg": "bg\n",
                "smart_locker/frontend/images/device_photo.jpg": "photo\n",
                "requirements.txt": "fastapi>=0.100\n",
                "deploy/install/update.sh": UPDATE_SH.read_text(encoding="utf-8"),
                "deploy/install/apply-sudoers.sh": _APPLY_SUDOERS_STUB,
                "scripts/__init__.py": "",
                "scripts/migrate_db.py": self._real("scripts/migrate_db.py"),
                "config/__init__.py": "",
                "config/settings.py": self._real("config/settings.py"),
            },
        )
        self.env_file.write_text(
            "E2E_MARKER=keepme\nSMART_LOCKER_KEEP_BACKUPS=5\n",
            encoding="utf-8",
            newline="\n",
        )
        self.create_db(include_calibration_column=include_calibration_column)
        # The pre-update appliance is running the old version.
        (self.root / "service.state").write_text("running\n", encoding="utf-8")
        return self

    def _real(self, rel: str) -> str:
        return (REPO_ROOT / rel).read_text(encoding="utf-8")

    def _write_fakes(self) -> None:
        fakes = {
            "sudo": _FAKE_SUDO,
            "systemctl": _FAKE_SYSTEMCTL,
            "curl": _FAKE_CURL,
            "tar": _fake_tar(),
            "chown": _perm_wrap("chown"),
            "chmod": _perm_wrap("chmod"),
        }
        have_rsync = (
            subprocess.run(
                [self.bash, "-c", "command -v rsync"],
                capture_output=True,
            ).returncode
            == 0
        )
        if not have_rsync:
            fakes["rsync"] = _RSYNC_SHIM
        for name, content in fakes.items():
            p = self.bin / name
            p.write_text(content, encoding="utf-8", newline="\n")
            os.chmod(p, 0o755)

    def _write_venv(self) -> None:
        vbin = self.app / "venv" / "bin"
        vbin.mkdir(parents=True)
        py = vbin / "python"
        py.write_text(
            _VENV_PYTHON.replace("__PY__", _posix(sys.executable)),
            encoding="utf-8",
            newline="\n",
        )
        os.chmod(py, 0o755)
        pip = vbin / "pip"
        pip.write_text(_FAKE_PIP, encoding="utf-8", newline="\n")
        os.chmod(pip, 0o755)

    def _write_tree(self, base: Path, files: dict[str, str]) -> None:
        for rel, content in files.items():
            p = base / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8", newline="\n")

    def create_db(self, *, include_calibration_column: bool = True) -> None:
        """Create the appliance SQLite file. Fully migrated by default so the
        payload's real migrate_db.py is a no-op; drop ``calibration_due`` to
        make the migration visibly add a column."""
        cal_col = "calibration_due DATE," if include_calibration_column else ""
        con = sqlite3.connect(self.db)
        con.executescript(
            f"""
            CREATE TABLE devices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pm_number VARCHAR(50) NOT NULL UNIQUE,
                name VARCHAR(100) NOT NULL,
                device_type VARCHAR(50) NOT NULL,
                serial_number VARCHAR(100),
                manufacturer VARCHAR(100),
                model VARCHAR(100),
                barcode VARCHAR(100),
                tag_hmac VARCHAR(64),
                locker_slot INTEGER,
                description TEXT,
                image_path VARCHAR(255),
                {cal_col}
                status VARCHAR(20) NOT NULL DEFAULT 'available',
                current_borrower_id INTEGER,
                created_at DATETIME
            );
            CREATE UNIQUE INDEX ix_devices_tag_hmac ON devices (tag_hmac);
            CREATE UNIQUE INDEX ix_devices_locker_slot ON devices (locker_slot);
            CREATE TABLE registrants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                display_name VARCHAR(100) NOT NULL UNIQUE,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO devices (pm_number, name, device_type, locker_slot, status)
            VALUES ('PM-1', 'Scope', 'Meter', 3, 'available');
            """
        )
        con.commit()
        con.close()

    # -- payloads ----------------------------------------------------------

    def _payload_files(
        self,
        version: str,
        *,
        boot_fail: bool = False,
        pip_fail: bool = False,
        migrate_fail: bool = False,
    ) -> dict[str, str]:
        files = {
            "VERSION": f"{version}\n",
            "smart_locker/__init__.py": "",
            "smart_locker/app.py": f'APP_MARK = "new-{version}"\n',
            # Only in the new tree — a rollback must remove it again.
            "smart_locker/new_feature.py": "NEW = True\n",
            # Committed UI asset added by the release — a rollback must
            # remove it while leaving runtime photos untouched.
            "smart_locker/frontend/images/new_ui.png": "png\n",
            "requirements.txt": "fastapi>=0.100\n",
            "deploy/install/update.sh": UPDATE_SH.read_text(encoding="utf-8"),
            "deploy/install/apply-sudoers.sh": _APPLY_SUDOERS_STUB,
            "scripts/__init__.py": "",
            "scripts/migrate_db.py": _MIGRATE_FAIL
            if migrate_fail
            else self._real("scripts/migrate_db.py"),
            "config/__init__.py": "",
            "config/settings.py": self._real("config/settings.py"),
        }
        if boot_fail:
            files["BOOT_FAIL"] = "1\n"
        if pip_fail:
            files["PIP_FAIL"] = "1\n"
        return files

    def write_payload(
        self,
        version: str,
        *,
        boot_fail: bool = False,
        pip_fail: bool = False,
        migrate_fail: bool = False,
    ) -> Path:
        """Drop a locker-updates/ incoming tree into the appliance."""
        shutil.rmtree(self.updates_dir, ignore_errors=True)
        self._write_tree(
            self.updates_dir,
            self._payload_files(
                version,
                boot_fail=boot_fail,
                pip_fail=pip_fail,
                migrate_fail=migrate_fail,
            ),
        )
        return self.updates_dir

    def write_usb_payload(self, version: str, *, stick: str = "USBSTICK", **kw) -> Path:
        """Drop a repo tree under the fake media root (<root>/media/<stick>/
        locker-updates) — exercises find_usb_tree and the USB→local staging
        copy."""
        dest = self.root / "media" / stick / "locker-updates"
        self._write_tree(dest, self._payload_files(version, **kw))
        return dest

    def write_nested_payload(self, version: str, *, child: str = "nested-tree", **kw) -> Path:
        """Repo tree at locker-updates/<child>/ — the apply source is a
        DESCENDANT of locker-updates; the staging copy must not delete its
        own source."""
        shutil.rmtree(self.updates_dir, ignore_errors=True)
        dest = self.updates_dir / child
        self._write_tree(dest, self._payload_files(version, **kw))
        return dest

    def write_garbage_payload(self) -> Path:
        """A locker-updates/ dir that is not a valid repo tree."""
        shutil.rmtree(self.updates_dir, ignore_errors=True)
        self._write_tree(
            self.updates_dir, {"README.txt": "not an update tree\n"}
        )
        return self.updates_dir

    def remove_payload(self) -> None:
        shutil.rmtree(self.updates_dir, ignore_errors=True)

    # -- run ---------------------------------------------------------------

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        for k in [k for k in env if k.upper().startswith("SMART_LOCKER_")]:
            del env[k]
        path_key = next((k for k in env if k.upper() == "PATH"), "PATH")
        env[path_key] = str(self.bin) + os.pathsep + env.get(path_key, "")
        env["SL_E2E_ROOT"] = _posix(self.root)
        env["SMART_LOCKER_DIR"] = _posix(self.app)
        env["SMART_LOCKER_DB_PATH"] = _posix(self.db)
        # Fake Pi media root: keeps a real /media (or a mounted stick on a
        # Linux dev box) out of the sandbox so discovery is deterministic.
        env["SMART_LOCKER_USB_MEDIA_ROOT"] = _posix(self.root / "media")
        env["SMART_LOCKER_SERVICE"] = "e2e-locker"
        env["SMART_LOCKER_USER"] = "locker"
        env["SMART_LOCKER_HEALTH_URL"] = "http://127.0.0.1:9/api/health"
        env["SMART_LOCKER_HEALTH_TIMEOUT"] = str(self.health_timeout)
        return env

    def run_update(self, *, timeout: int = 120) -> subprocess.CompletedProcess:
        """Run the repo's deploy/install/update.sh end-to-end in this sandbox."""
        return subprocess.run(
            [self.bash, _posix(UPDATE_SH)],
            env=self._env(),
            cwd=str(self.root),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
        )

    # -- inspection --------------------------------------------------------

    def status_json(self) -> dict:
        p = self.app / "logs" / "update-status.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def update_log(self) -> str:
        p = self.app / "logs" / "update.log"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def systemctl_calls(self) -> list[str]:
        p = self.root / "systemctl.log"
        return p.read_text(encoding="utf-8").splitlines() if p.exists() else []

    def curl_answers(self) -> list[str]:
        """Every health answer the fake curl emitted, in order — what the
        health gate actually saw (the version the service was reporting)."""
        p = self.root / "curl.log"
        return p.read_text(encoding="utf-8").splitlines() if p.exists() else []

    def perms_log(self) -> list[str]:
        """chown/chmod calls the updater made, in order."""
        p = self.root / "perms.log"
        return p.read_text(encoding="utf-8").splitlines() if p.exists() else []

    def service_state(self) -> str:
        p = self.root / "service.state"
        return p.read_text(encoding="utf-8").strip() if p.exists() else ""

    def version(self) -> str:
        p = self.app / "VERSION"
        return p.read_text(encoding="utf-8").strip() if p.exists() else ""

    def path(self, rel: str) -> Path:
        return self.app / rel

    def device_rows(self) -> list[tuple]:
        con = sqlite3.connect(self.db)
        rows = con.execute(
            "SELECT pm_number, name, locker_slot, status FROM devices ORDER BY id"
        ).fetchall()
        con.close()
        return rows

    def device_columns(self) -> list[str]:
        con = sqlite3.connect(self.db)
        cols = [r[1] for r in con.execute("PRAGMA table_info(devices)")]
        con.close()
        return cols
