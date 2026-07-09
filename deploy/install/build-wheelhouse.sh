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
fi

# --- Post-build assertion ---------------------------------------------------
# pip download with --abi/--python-version (or a matching native host) will already
# error out under set -e if a package has no compatible wheel — so a clean exit past
# here already means pip resolved everything. This assertion is the belt-and-suspenders
# guard for a *later-tampered* or partially-completed wheelhouse folder: it fails loud
# if no cp313/abi3/py3-none wheel is present at all. It does NOT prove every requirement
# is satisfiable (only pip's own resolver can); it proves the folder isn't empty/garbage.
if ! ls "$DEST"/*.whl 2>/dev/null | grep -E "(-${PYTHON_ABI}-|abi3-|py3-none)" | grep -q .; then
  echo "    FATAL: built wheelhouse has no ${PYTHON_ABI}-/abi3/py3-none wheels." >&2
  echo "    Something is wrong with pip's --platform/--abi flags or requirements.txt." >&2
  echo "    Refusing to ship a broken offline kit — investigate before copying to the Pi." >&2
  exit 1
fi

COUNT="$(ls -1 "$DEST"/*.whl "$DEST"/*.tar.gz 2>/dev/null | wc -l | tr -d ' ')"
echo "==> Done. $COUNT package file(s) staged in $DEST"

# --- Auto-download the python3-pyscard .deb for trixie/arm64 ----------------
# pyscard has NO aarch64 PyPI wheel (Windows/macOS only). The Debian `.deb` for
# python3-pyscard ships the compiled extension against the system Python, which is
# what install.sh installs offline via dpkg -i (visible to the venv through
# --system-site-packages). This auto-download makes build-wheelhouse.sh produce a
# COMPLETE offline kit in one run — no separate manual step to forget.
#
# Prefer the known-good trixie package (2.2.2-1 — matches what apt installs on a
# trixie Pi). The pool also has newer builds (e.g. 2.3.x for sid/forky) that are
# NOT what trixie ships; "latest" would grab those and risk ABI/dep mismatch.
# If the pinned URL 404s, fall back to scraping the pool for any arm64 build.
PYSCARD_POOL="https://deb.debian.org/debian/pool/main/p/pyscard/"
PINNED_DEB="python3-pyscard_2.2.2-1_arm64.deb"
echo "==> Fetching python3-pyscard arm64 .deb for trixie offline installs..."
DEB_NAME="$PINNED_DEB"
DEB_PATH="$SYSDEST/$DEB_NAME"
if [ -f "$DEB_PATH" ]; then
  echo "    $DEB_NAME already present in $SYSDEST — leaving it."
elif curl -fsSL -o "$DEB_PATH" "${PYSCARD_POOL}${DEB_NAME}"; then
  find "$SYSDEST" -maxdepth 1 -name 'python3-pyscard_*_arm64.deb' ! -name "$DEB_NAME" -delete 2>/dev/null || true
  echo "    saved to $DEB_PATH"
else
  rm -f "$DEB_PATH" 2>/dev/null || true
  echo "    pinned $PINNED_DEB not available — scraping pool for an arm64 build..."
  DEB_NAME="$(curl -fsSL "$PYSCARD_POOL" 2>/dev/null \
    | grep -oE 'python3-pyscard_[0-9][^"]*_arm64\.deb' \
    | sort -V | tail -n 1 || true)"
  if [ -n "$DEB_NAME" ]; then
    DEB_PATH="$SYSDEST/$DEB_NAME"
    if curl -fsSL -o "$DEB_PATH" "${PYSCARD_POOL}${DEB_NAME}"; then
      find "$SYSDEST" -maxdepth 1 -name 'python3-pyscard_*_arm64.deb' ! -name "$DEB_NAME" -delete 2>/dev/null || true
      echo "    saved to $DEB_PATH"
    else
      rm -f "$DEB_PATH" 2>/dev/null || true
      echo "    WARNING: download failed for ${PYSCARD_POOL}${DEB_NAME}" >&2
      echo "    Falling back to manual staging — see deploy/system-packages/README.md." >&2
    fi
  else
    echo "    WARNING: could not resolve a python3-pyscard arm64 .deb from $PYSCARD_POOL." >&2
    echo "    Falling back to manual staging — see deploy/system-packages/README.md." >&2
  fi
fi

echo ""
echo "==> Offline kit ready: $DEST (wheels) + $SYSDEST (pyscard .deb)"
echo "    Copy the whole deploy/ tree onto the Pi (USB stick), then on the Pi run:"
echo "        sudo bash deploy/install/install.sh"
echo "    (Use 'sudo bash <script>', not 'sudo <script>' — the +x bit is stripped when"
echo "    copying via exFAT from Windows; 'bash <script>' does not rely on it.)"
