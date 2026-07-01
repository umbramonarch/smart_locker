#!/usr/bin/env bash
# build-wheelhouse.sh — download all Python dependencies as wheels for OFFLINE install.
# ----------------------------------------------------------------------------
# The production Pi has no internet, so its venv cannot reach PyPI. Run this ONCE on a
# machine WITH internet, then copy deploy/wheelhouse/ onto the Pi (SD card / USB stick).
# install.sh installs from it with: pip install --no-index --find-links deploy/wheelhouse
#
# Works from TWO kinds of host:
#   1. An aarch64 Linux machine (a real Pi with temporary internet, or another arm64
#      board) — downloads natively for whatever Python/arch it's running on.
#   2. ANY other machine (e.g. your Windows/x86_64 dev PC via Git Bash) — pip's
#      --platform/--python-version/--implementation/--abi flags let it fetch prebuilt
#      Linux aarch64 wheels for Python 3.11 without needing to run on that architecture
#      at all. This is the expected path when the Pi itself must never touch a network.
#
# pyscard is deliberately EXCLUDED here — it has no prebuilt Linux aarch64 wheel on
# PyPI at all (only Windows/macOS). It is installed separately from a Debian .deb
# package (python3-pyscard, arm64) — see deploy/system-packages/README.md.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
DEST="$HERE/../wheelhouse"
mkdir -p "$DEST"

# Requirements minus pyscard (handled separately — see header comment above).
REQS_NO_PYSCARD="$(mktemp)"
grep -vi '^pyscard' "$REPO/requirements.txt" > "$REQS_NO_PYSCARD"
trap 'rm -f "$REQS_NO_PYSCARD"' EXIT

HOST_ARCH="$(uname -m)"
if [ "$HOST_ARCH" = "aarch64" ] || [ "$HOST_ARCH" = "arm64" ]; then
  echo "==> Native aarch64 host detected ($(python3 --version 2>&1)) — downloading directly."
  python3 -m pip download -r "$REQS_NO_PYSCARD" -d "$DEST"
else
  echo "==> Host is $HOST_ARCH, not aarch64 — cross-downloading Linux/aarch64 wheels for Python 3.11."
  echo "    (verified: every package in requirements.txt except pyscard publishes a"
  echo "    manylinux_aarch64 + cp311 wheel, so this works with no compilation, no QEMU,"
  echo "    and no aarch64 hardware needed on this machine.)"
  python3 -m pip download \
    --platform manylinux2014_aarch64 \
    --python-version 311 \
    --implementation cp \
    --abi cp311 \
    --only-binary=:all: \
    -r "$REQS_NO_PYSCARD" -d "$DEST"
fi

COUNT="$(ls -1 "$DEST"/*.whl "$DEST"/*.tar.gz 2>/dev/null | wc -l | tr -d ' ')"
echo "==> Done. $COUNT package file(s) staged in $DEST"
echo "    Also grab the pyscard .deb — see deploy/system-packages/README.md — then copy"
echo "    both onto the Pi (SD card / USB stick) before running deploy/install/install.sh."
