#!/usr/bin/env bash
# build-wheelhouse.sh — download all Python dependencies as wheels for OFFLINE install.
# ----------------------------------------------------------------------------
# The production Pi has no internet, so its venv cannot reach PyPI. Run this ONCE on a
# machine WITH internet that matches the Pi's architecture (aarch64 / 64-bit Pi OS) and
# Python version, then bake deploy/wheelhouse/ into the SD image. install.sh installs
# from it with: pip install --no-index --find-links deploy/wheelhouse
#
# Easiest: run it on the same Pi while it still has a temporary internet connection,
# BEFORE you move it into the offline company network.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
DEST="$HERE/../wheelhouse"

mkdir -p "$DEST"
echo "==> Downloading wheels for $(python3 --version 2>&1) on $(uname -m)"
echo "    requirements: $REPO/requirements.txt"
echo "    destination : $DEST"
python3 -m pip download -r "$REPO/requirements.txt" -d "$DEST"

COUNT="$(ls -1 "$DEST"/*.whl "$DEST"/*.tar.gz 2>/dev/null | wc -l | tr -d ' ')"
echo "==> Done. $COUNT package file(s) staged in $DEST"
echo "    Commit/copy this folder onto the SD image, then run deploy/install/install.sh on the Pi."
