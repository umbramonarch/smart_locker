#!/usr/bin/env bash
# build-wheelhouse.sh — download all Python dependencies as wheels for OFFLINE install.
# ----------------------------------------------------------------------------
# The production Pi has no internet, so its venv cannot reach PyPI. Run this ONCE on a
# machine WITH internet, then copy deploy/wheelhouse/ onto the Pi (SD card / USB stick).
# install.sh installs from it with: pip install --no-index --find-links deploy/wheelhouse
#
# IMPORTANT: This must be built for the SAME Python version the target Pi uses.
# Current target: Python 3.13 (as used on recent Raspberry Pi OS "trixie" images).
# When the Pi's OS/Python changes, update PYTHON_VERSION / PYTHON_ABI below.
#
# Works from TWO kinds of host:
#   1. An aarch64 Linux machine (a real Pi with temporary internet, or another arm64
#      board) — downloads natively for whatever Python/arch it's running on.
#   2. ANY other machine (e.g. your Windows/x86_64 dev PC via Git Bash) — pip's
#      --platform/--python-version/--implementation/--abi flags let it fetch prebuilt
#      Linux aarch64 wheels for Python 3.13 without needing to run on that architecture
#      at all. This is the expected path when the Pi itself must never touch a network.
#
# pyscard is deliberately EXCLUDED here — it has no prebuilt Linux aarch64 wheel on
# PyPI at all (only Windows/macOS). It is installed separately from a Debian .deb
# package (python3-pyscard, arm64) — this script ALSO auto-downloads that .deb into
# deploy/system-packages/, so running this one script produces a complete offline kit.
# See deploy/system-packages/README.md for the version rationale.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
DEST="$HERE/../wheelhouse"
SYSDEST="$HERE/../system-packages"
mkdir -p "$DEST" "$SYSDEST"

# Target Python version for the Pi (update this when the Pi's OS/Python changes).
# This Pi (trixie image) uses Python 3.13.
PYTHON_VERSION="313"
PYTHON_ABI="cp313"

# Requirements minus pyscard (handled separately — see header comment above).
REQS_NO_PYSCARD="$(mktemp)"
grep -vi '^pyscard' "$REPO/requirements.txt" > "$REQS_NO_PYSCARD"
trap 'rm -f "$REQS_NO_PYSCARD"' EXIT

HOST_ARCH="$(uname -m)"
if [ "$HOST_ARCH" = "aarch64" ] || [ "$HOST_ARCH" = "arm64" ]; then
  echo "==> Native aarch64 host detected ($(python3 --version 2>&1)) — downloading directly."
  # Native path trusts the host Python to pick compatible wheels. If the host is NOT
  # on the target Python version we'd silently ship a broken kit for the Pi, so guard
  # it: fail loud if the host Python != target. (To build cp313 wheels on a non-cp313
  # aarch64 host, run the cross-download path by invoking this script from x86.)
  HOST_PY="$("$(command -v python3)" -c 'import sys; print("%d%d" % sys.version_info[:2])')"
  if [ "$HOST_PY" != "$PYTHON_VERSION" ]; then
    echo "    FATAL: native aarch64 host is Python $HOST_PY, but target is $PYTHON_VERSION." >&2
    echo "    Either run this on a ${PYTHON_VERSION} host, or run it from x86_64/Windows" >&2
    echo "    (the cross-download path below targets ${PYTHON_ABI} correctly)." >&2
    exit 1
  fi
  # Wipe old wheels first so a rebuild can't leave mixed-ABI debris from a prior
  # build (e.g. cp311 wheels lingering next to new cp313 ones).
  rm -f "$DEST"/*.whl "$DEST"/*.tar.gz 2>/dev/null || true
  python3 -m pip download -r "$REQS_NO_PYSCARD" -d "$DEST"
else
  echo "==> Host is $HOST_ARCH, not aarch64 — cross-downloading Linux/aarch64 wheels for Python ${PYTHON_VERSION}."
  echo "    (verified: every package in requirements.txt except pyscard publishes a"
  echo "    manylinux_aarch64 + ${PYTHON_ABI} wheel, so this works with no compilation, no QEMU,"
  echo "    and no aarch64 hardware needed on this machine.)"
  rm -f "$DEST"/*.whl "$DEST"/*.tar.gz 2>/dev/null || true
  python3 -m pip download \
    --platform manylinux2014_aarch64 \
    --python-version "${PYTHON_VERSION}" \
    --implementation cp \
    --abi "${PYTHON_ABI}" \
    --only-binary=:all: \
    -r "$REQS_NO_PYSCARD" -d "$DEST"
  # Environment markers (sys_platform / platform_python_implementation) are still
  # evaluated on the *host*. Building on Windows/macOS therefore SKIPS Linux-only
  # deps such as uvloop (uvicorn[standard] extra: sys_platform != 'win32'). The Pi
  # is Linux aarch64 — pull those explicitly so the kit is complete without apt/network.
  echo "==> Pulling Linux-only transitive wheels the host markers would skip..."
  python3 -m pip download \
    --platform manylinux2014_aarch64 \
    --python-version "${PYTHON_VERSION}" \
    --implementation cp \
    --abi "${PYTHON_ABI}" \
    --only-binary=:all: \
    -d "$DEST" \
    "uvloop>=0.15.1"
  if ! ls "$DEST"/uvloop-*.whl >/dev/null 2>&1; then
    echo "    FATAL: uvloop wheel missing after explicit pull (uvicorn[standard] on Linux)." >&2
    exit 1
  fi
fi

# --- Post-build: empty/garbage guard + FULL resolver check -------------------
# The Pi production path is never-networked and does not use apt. The wheelhouse
# must therefore contain every pip-installable dep (direct + transitive) for
# requirements.txt except pyscard. `pip download` already fails under set -e if a
# package has no compatible wheel; the dry-run below re-resolves against ONLY this
# folder (--no-index --ignore-installed) so a partial/corrupt DEST cannot ship.
if ! ls "$DEST"/*.whl >/dev/null 2>&1; then
  echo "    FATAL: wheelhouse is empty after pip download — nothing to ship." >&2
  exit 1
fi
echo "==> Verifying wheelhouse fully resolves requirements (pip --dry-run, no index)..."
DRY_LOG="$(mktemp)"
if [ "$HOST_ARCH" = "aarch64" ] || [ "$HOST_ARCH" = "arm64" ]; then
  # Native host already matches target Python (asserted above). Markers match Linux
  # so this is the definitive check (includes uvloop for uvicorn[standard]).
  if ! python3 -m pip install --dry-run --ignore-installed --no-index --find-links "$DEST" \
      -r "$REQS_NO_PYSCARD" >"$DRY_LOG" 2>&1; then
    echo "    FATAL: wheelhouse cannot satisfy requirements.txt (resolver dry-run failed)." >&2
    echo "    --- pip dry-run output (last 40 lines) ---" >&2
    tail -n 40 "$DRY_LOG" >&2 || true
    rm -f "$DRY_LOG"
    exit 1
  fi
else
  # Cross-build host: resolve with platform tags. NOTE: host environment markers
  # still apply (Windows will not *require* uvloop here) — that is why we force-
  # download Linux-only wheels above and assert their files exist.
  if ! python3 -m pip install --dry-run --ignore-installed --no-index --find-links "$DEST" \
      --platform manylinux2014_aarch64 \
      --python-version "${PYTHON_VERSION}" \
      --implementation cp \
      --abi "${PYTHON_ABI}" \
      --only-binary=:all: \
      -r "$REQS_NO_PYSCARD" >"$DRY_LOG" 2>&1; then
    echo "    FATAL: wheelhouse cannot satisfy requirements.txt for Linux aarch64/${PYTHON_ABI}." >&2
    echo "    (A missing transitive wheel would show here.)" >&2
    echo "    --- pip dry-run output (last 40 lines) ---" >&2
    tail -n 40 "$DRY_LOG" >&2 || true
    rm -f "$DRY_LOG"
    exit 1
  fi
  if ! ls "$DEST"/uvloop-*.whl >/dev/null 2>&1; then
    echo "    FATAL: cross-built wheelhouse is missing uvloop (Linux-only uvicorn extra)." >&2
    exit 1
  fi
fi
rm -f "$DRY_LOG"
echo "    OK: every non-pyscard requirement (and its deps) is present as a wheel."

# Hard-require the packages that historically failed mid-install on the Pi when a
# stale (cp311) wheelhouse was used. SQLAlchemy has no abi3 build — it must be
# an exact cp313 wheel. cryptography is often "already satisfied" from
# /usr/lib/python3/dist-packages on the Pi and can mask an incomplete kit; we still
# require its wheel so --ignore-installed install is self-contained.
# Wheel filenames are usually lowercase (sqlalchemy-…); accept either case.
if ! ls "$DEST"/sqlalchemy-*-cp313-*.whl >/dev/null 2>&1 \
   && ! ls "$DEST"/SQLAlchemy-*-cp313-*.whl >/dev/null 2>&1; then
  echo "    FATAL: no SQLAlchemy cp313 wheel in $DEST." >&2
  echo "    That is exactly the package that failed offline install on trixie" >&2
  echo "    ('No matching distribution found for SQLAlchemy>=2.0.0'). Rebuild." >&2
  exit 1
fi
if ! ls "$DEST"/cryptography-*.whl >/dev/null 2>&1; then
  echo "    FATAL: no cryptography wheel in $DEST (must not rely on system crypto)." >&2
  exit 1
fi

COUNT="$(ls -1 "$DEST"/*.whl "$DEST"/*.tar.gz 2>/dev/null | wc -l | tr -d ' ')"
echo "==> Done. $COUNT package file(s) staged in $DEST"

# --- Offline OS .debs (what a trixie Full image still needs beyond the base OS) ---
# Captured from a real Pi online install of install.sh's apt list (trixie, arm64):
#   NEW: libccid, pcscd, libintl-perl, libpcsc-perl, pcsc-tools, python3-pyscard, unclutter
#   ALREADY on Full: cifs-utils, curl, python3-venv, python3-pip, chromium*
# Production has no apt — stage the NEW packages here for dpkg -i on the Pi.
# Versions pinned to what trixie served on that validation run (not "latest"/sid).
DEB_BASE="https://deb.debian.org/debian/pool/main"
# "url path relative to pool/main"  "filename"
# shellcheck disable=SC2034
OFFLINE_OS_DEBS=(
  "c/ccid|libccid_1.6.2-1_arm64.deb"
  "p/pcsc-lite|pcscd_2.3.3-1_arm64.deb"
  "libi/libintl-perl|libintl-perl_1.35-1_all.deb"
  "p/pcsc-perl|libpcsc-perl_1.4.16-1+b3_arm64.deb"
  "p/pcsc-tools|pcsc-tools_1.7.3-1_arm64.deb"
  "p/pyscard|python3-pyscard_2.2.2-1_arm64.deb"
  "u/unclutter|unclutter_8-25+nmu1_arm64.deb"
)

fetch_deb() {
  # fetch_deb <pool_subdir> <filename>
  local subdir="$1" name="$2"
  local path="$SYSDEST/$name"
  local url="${DEB_BASE}/${subdir}/${name}"
  if [ -f "$path" ]; then
    echo "    $name already present — leaving it."
    return 0
  fi
  echo "    fetching $name ..."
  if curl -fsSL -o "$path" "$url"; then
    echo "    saved $path"
    return 0
  fi
  rm -f "$path" 2>/dev/null || true
  echo "    FATAL: could not download $url" >&2
  echo "    Offline Pi has no apt; fix network or stage this .deb by hand." >&2
  return 1
}

echo "==> Fetching offline OS .debs for trixie/arm64 into $SYSDEST ..."
for entry in "${OFFLINE_OS_DEBS[@]}"; do
  subdir="${entry%%|*}"
  name="${entry##*|}"
  fetch_deb "$subdir" "$name" || exit 1
done

# Hard-require the NFC-critical debs (pcscd + pyscard).
for must in pcscd_ python3-pyscard_ libccid_; do
  if ! ls "$SYSDEST"/${must}*.deb >/dev/null 2>&1; then
    echo "    FATAL: missing required offline package matching ${must}*.deb in $SYSDEST" >&2
    exit 1
  fi
done
DEB_COUNT="$(ls -1 "$SYSDEST"/*.deb 2>/dev/null | wc -l | tr -d ' ')"

echo ""
echo "==> Offline kit ready (no apt required on the Pi):"
echo "    wheels : $DEST  ($COUNT files — full requirements.txt except pyscard + deps)"
echo "    os debs: $SYSDEST  ($DEB_COUNT .deb files — pcscd stack, unclutter, pyscard)"
echo "    Copy the whole project (incl. deploy/) onto the Pi via USB, then run:"
echo "        sudo bash deploy/install/install.sh"
echo "    ALWAYS use 'sudo bash …' — never 'sudo deploy/install/install.sh'."
echo "    (exFAT/Windows strips +x → sudo reports 'command not found' without bash.)"
