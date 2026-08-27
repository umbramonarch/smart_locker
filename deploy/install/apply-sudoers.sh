#!/usr/bin/env bash
#
# File: apply-sudoers.sh
# Description: Render deploy/install/sudoers-smart-locker into
#              /etc/sudoers.d/smart-locker (mode 0440, visudo -cf first).
# Project: smart_locker/deploy
# Notes: Run as: sudo bash deploy/install/apply-sudoers.sh
#        install.sh and update.sh call this. On an existing Pi whose sudoers
#        predates the Shut down button, SSH once and run this after the code
#        swap — Software Update via the old update.sh does not refresh sudoers.
#        Keep LF line endings.
#
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root with bash:" >&2
  echo "    sudo bash deploy/install/apply-sudoers.sh" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${SMART_LOCKER_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
APP_USER="${SMART_LOCKER_USER:-$(stat -c '%U' "$APP_DIR")}"
TEMPLATE="$SCRIPT_DIR/sudoers-smart-locker"

if [ ! -f "$TEMPLATE" ]; then
  echo "sudoers template not found: $TEMPLATE" >&2
  exit 1
fi

SUDOERS_TMP="$(mktemp)"
trap 'rm -f "$SUDOERS_TMP"' EXIT

sed \
  -e "s#__APP_USER__#$APP_USER#g" \
  -e "s#__APP_DIR__#$APP_DIR#g" \
  "$TEMPLATE" > "$SUDOERS_TMP"

if visudo -cf "$SUDOERS_TMP" >/dev/null 2>&1; then
  install -m 0440 -o root -g root "$SUDOERS_TMP" /etc/sudoers.d/smart-locker
  echo "    installed /etc/sudoers.d/smart-locker (updates + poweroff for $APP_USER)"
else
  echo "    WARNING: generated sudoers failed validation — NOT installed." >&2
  echo "             Admin Software Update / Shut down need this drop-in." >&2
  exit 1
fi
