#!/usr/bin/env bash
# File: run-native.sh
# Description: Fast "native run" path for the Smart Locker simulation harness.
#              Exports the fake-reader env flags and sim/data/ paths, then
#              launches the real app. No QEMU, no PC/SC daemon, no hardware.
# Project: smart_locker/sim/native
# Notes: For Windows dev machines use the sibling run-native.ps1 instead.
#        Both scripts read keys from .env at the repo root — copy
#        sim/.env.sim.example -> .env and fill in the generated keys first.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

# ---------------------------------------------------------------------------
# Pre-flight: .env must exist and contain both keys
# ---------------------------------------------------------------------------
ENV_FILE="$REPO_ROOT/.env"
if [ ! -f "$ENV_FILE" ]; then
  cat >&2 <<EOF
ERROR: .env not found at $REPO_ROOT/.env

Quick start:
  cp sim/.env.sim.example .env
  python -m scripts.generate_key      # paste the two output lines into .env
  python -m scripts.init_db
  python -m scripts.enroll_card --name "Sim User" --role admin \\
         --uid "\${SMART_LOCKER_FAKE_DEFAULT_UID:-AABBCCDD}"
EOF
  exit 1
fi

# Check both keys are non-empty (they must be set, not just the placeholder)
_check_key() {
  local var="$1"
  local val
  val="$(grep -E "^${var}=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d '[:space:]')"
  if [ -z "$val" ] || echo "$val" | grep -qi "REPLACE_WITH"; then
    echo "ERROR: $var is not set in .env — run: python -m scripts.generate_key" >&2
    exit 1
  fi
}
_check_key "SMART_LOCKER_ENC_KEY"
_check_key "SMART_LOCKER_HMAC_KEY"

# ---------------------------------------------------------------------------
# Simulation env overrides
# These take precedence over any matching value in .env because they are set
# in the process environment before the app calls load_dotenv().
# ---------------------------------------------------------------------------
export SMART_LOCKER_FAKE_READER=1
export SMART_LOCKER_FAKE_DEFAULT_UID="${SMART_LOCKER_FAKE_DEFAULT_UID:-AABBCCDD}"
export SMART_LOCKER_SOURCE_EXCEL_PATH="$REPO_ROOT/sim/data/Messmittelliste.sample.xlsx"
export SMART_LOCKER_PHOTO_INPUT_PATH="$REPO_ROOT/sim/data/photos"
export SMART_LOCKER_EXCEL_AUTO_EXPORT=0    # avoid writing back to the sample xlsx

# ---------------------------------------------------------------------------
# Resolve the venv Python (handles both Linux/Mac and Windows Git Bash paths)
# ---------------------------------------------------------------------------
PYTHON=""
for candidate in \
    "$REPO_ROOT/venv/bin/python" \
    "$REPO_ROOT/venv/Scripts/python" \
    "$REPO_ROOT/venv/Scripts/python.exe"; do
  if [ -f "$candidate" ]; then
    PYTHON="$candidate"
    break
  fi
done
if [ -z "$PYTHON" ]; then
  cat >&2 <<EOF
ERROR: venv not found. Create it first:
  python3 -m venv venv
  venv/bin/pip install -r requirements.txt      # Linux / QEMU guest
  .\venv\Scripts\pip install -r requirements.txt # Windows PowerShell
EOF
  exit 1
fi

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
echo "==> Smart Locker — native simulation run"
echo "    repo    : $REPO_ROOT"
echo "    python  : $PYTHON"
echo "    reader  : FAKE  (SMART_LOCKER_FAKE_READER=1)"
echo "    uid     : $SMART_LOCKER_FAKE_DEFAULT_UID"
echo "    source  : $SMART_LOCKER_SOURCE_EXCEL_PATH"
echo "    photos  : $SMART_LOCKER_PHOTO_INPUT_PATH"
echo ""
echo "    Kiosk UI   -> http://localhost:8000"
echo "    Inject tap -> POST /api/dev/tap           (curl or any REST client)"
echo "                  GET  /api/dev/status        (confirms fake_reader: true)"
echo "                  F2 key or bottom-right button in the browser"
echo "    Import now -> python -m scripts.sync_source  (separate terminal)"
echo ""

cd "$REPO_ROOT"
exec "$PYTHON" -m smart_locker.app
