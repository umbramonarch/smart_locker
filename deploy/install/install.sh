#!/usr/bin/env bash
# install.sh — provision a Raspberry Pi to run Smart Locker as a kiosk appliance.
# ----------------------------------------------------------------------------
# Run as root from inside the cloned/copied repo:
#     sudo deploy/install/install.sh
#
# It is idempotent — safe to re-run. It will:
#   1. Install OS packages (pcscd, libccid, cifs-utils, chromium, ...) when online.
#   2. Create the Python virtualenv and install deps (offline from deploy/wheelhouse
#      if present, otherwise from PyPI).
#   3. Enable pcscd (PC/SC daemon for the ACR1252U reader).
#   4. Install + enable the smart-locker systemd service.
#   5. Scaffold the M: CIFS mount point and credentials file.
#   6. Install the Chromium kiosk autostart entry for the app user.
#
# Overridable via environment variables:
#   SMART_LOCKER_DIR   (default: the repo this script lives in)
#   SMART_LOCKER_USER  (default: the owner of that repo directory)
#   SMART_LOCKER_MOUNT (default: /mnt/locker)
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root:  sudo $0" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="${SMART_LOCKER_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
APP_USER="${SMART_LOCKER_USER:-$(stat -c '%U' "$APP_DIR")}"
APP_GROUP="$(id -gn "$APP_USER")"
MOUNT_POINT="${SMART_LOCKER_MOUNT:-/mnt/locker}"
VENV_DIR="$APP_DIR/venv"
WHEELHOUSE="$APP_DIR/deploy/wheelhouse"

echo "==> Smart Locker install"
echo "    app dir : $APP_DIR"
echo "    user    : $APP_USER:$APP_GROUP"
echo "    mount   : $MOUNT_POINT"
echo

# --- 0. Detect connectivity (the company Pi runs offline; the build host is online) ---
ONLINE=0
if timeout 4 getent hosts deb.debian.org >/dev/null 2>&1; then
  ONLINE=1
fi
[ "$ONLINE" -eq 1 ] && echo "==> Network: online"  || echo "==> Network: OFFLINE (skipping apt; packages must be pre-installed)"

# --- 1. OS packages ---
PKGS="pcscd pcsc-tools libccid cifs-utils chromium unclutter curl python3-venv python3-pip"
if [ "$ONLINE" -eq 1 ]; then
  echo "==> Installing OS packages: $PKGS"
  apt-get update -y
  # 'chromium' is the package on Bookworm; fall back to 'chromium-browser' on older OS.
  if ! apt-get install -y --no-install-recommends $PKGS; then
    echo "    'chromium' not found — retrying with 'chromium-browser'."
    apt-get install -y --no-install-recommends pcscd pcsc-tools libccid cifs-utils chromium-browser unclutter curl python3-venv python3-pip
  fi
else
  echo "==> Verifying required commands are present (offline)"
  MISSING=""
  for cmd in pcscd mount.cifs curl python3; do
    command -v "$cmd" >/dev/null 2>&1 || MISSING="$MISSING $cmd"
  done
  if ! command -v chromium >/dev/null 2>&1 && ! command -v chromium-browser >/dev/null 2>&1; then
    MISSING="$MISSING chromium"
  fi
  if [ -n "$MISSING" ]; then
    echo "    WARNING: missing packages:$MISSING"
    echo "    Pre-install them on a build host with internet, then re-image. Continuing."
  fi
fi

# --- 2. Python virtualenv + dependencies ---
if [ ! -d "$VENV_DIR" ]; then
  echo "==> Creating virtualenv at $VENV_DIR"
  sudo -u "$APP_USER" python3 -m venv "$VENV_DIR"
fi
echo "==> Installing Python dependencies"
if ls "$WHEELHOUSE"/*.whl >/dev/null 2>&1; then
  echo "    using offline wheelhouse: $WHEELHOUSE"
  sudo -u "$APP_USER" "$VENV_DIR/bin/pip" install --no-index --find-links "$WHEELHOUSE" -r "$APP_DIR/requirements.txt"
elif [ "$ONLINE" -eq 1 ]; then
  sudo -u "$APP_USER" "$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt"
else
  echo "    WARNING: offline and no wheelhouse found at $WHEELHOUSE."
  echo "    Run deploy/install/build-wheelhouse.sh on an online aarch64 host first. Skipping."
fi

# --- 3. Enable the PC/SC daemon (ACR1252U reader) ---
echo "==> Enabling pcscd"
systemctl enable --now pcscd || echo "    (could not start pcscd now — it is socket-activated and will start on demand)"

# --- 4. systemd service (paths/user substituted to match this install) ---
echo "==> Installing systemd service: smart-locker.service"
SERVICE_SRC="$APP_DIR/deploy/systemd/smart-locker.service"
SERVICE_DST="/etc/systemd/system/smart-locker.service"
sed \
  -e "s#^User=.*#User=$APP_USER#" \
  -e "s#^Group=.*#Group=$APP_GROUP#" \
  -e "s#^WorkingDirectory=.*#WorkingDirectory=$APP_DIR#" \
  -e "s#^ExecStart=.*#ExecStart=$VENV_DIR/bin/python -m smart_locker.app#" \
  -e "s#^Documentation=.*#Documentation=file://$APP_DIR/GUIDE.md#" \
  "$SERVICE_SRC" > "$SERVICE_DST"
systemctl daemon-reload
systemctl enable smart-locker.service
echo "    (start it with: sudo systemctl start smart-locker  — do this AFTER filling .env)"

# --- 4b. Sudoers + update script (powers the in-app "Update now" button) ---
echo "==> Installing sudoers drop-in for self-service updates"
chmod +x "$APP_DIR/deploy/install/update.sh"
SUDOERS_TMP="$(mktemp)"
sed \
  -e "s#__APP_USER__#$APP_USER#g" \
  -e "s#__APP_DIR__#$APP_DIR#g" \
  "$APP_DIR/deploy/install/sudoers-smart-locker" > "$SUDOERS_TMP"
if visudo -cf "$SUDOERS_TMP" >/dev/null 2>&1; then
  install -m 0440 -o root -g root "$SUDOERS_TMP" /etc/sudoers.d/smart-locker
  echo "    installed /etc/sudoers.d/smart-locker (lets the app restart itself for updates)"
else
  echo "    WARNING: generated sudoers failed validation — NOT installed. The 'Update now'"
  echo "             button will be disabled until this is fixed; updates can still run via SSH."
fi
rm -f "$SUDOERS_TMP"

# --- 5. M: CIFS mount scaffolding ---
echo "==> Scaffolding CIFS mount at $MOUNT_POINT"
mkdir -p "$MOUNT_POINT"
install -d -m 700 /etc/smart-locker
if [ ! -f /etc/smart-locker/cifs-credentials ]; then
  cp "$APP_DIR/deploy/mount/cifs-credentials.example" /etc/smart-locker/cifs-credentials
  chown root:root /etc/smart-locker/cifs-credentials
  chmod 600 /etc/smart-locker/cifs-credentials
  echo "    created /etc/smart-locker/cifs-credentials — EDIT IT with the real share login."
else
  echo "    /etc/smart-locker/cifs-credentials already exists — left unchanged."
fi
echo "    Then add the fstab line from deploy/mount/fstab.snippet and run: sudo mount $MOUNT_POINT"

# --- 6. Chromium kiosk autostart for the app user ---
echo "==> Installing kiosk autostart for $APP_USER"
chmod +x "$APP_DIR/deploy/kiosk/start-kiosk.sh"
AUTOSTART_DIR="/home/$APP_USER/.config/autostart"
sudo -u "$APP_USER" mkdir -p "$AUTOSTART_DIR"
sed "s#^Exec=.*#Exec=$APP_DIR/deploy/kiosk/start-kiosk.sh#" \
  "$APP_DIR/deploy/kiosk/smart-locker-kiosk.desktop" > "$AUTOSTART_DIR/smart-locker-kiosk.desktop"
chown "$APP_USER:$APP_GROUP" "$AUTOSTART_DIR/smart-locker-kiosk.desktop"

# --- 7. Ensure the app user owns its writable dirs (logs, db, served images) ---
echo "==> Fixing ownership of $APP_DIR"
mkdir -p "$APP_DIR/logs" "$APP_DIR/smart_locker/frontend/images"
chown -R "$APP_USER:$APP_GROUP" "$APP_DIR"

cat <<EOF

==> Done. Remaining manual steps (see GUIDE.md for the full walkthrough):
    1. cp deploy/.env.pi.example .env   &&  edit .env  (paths are pre-filled)
    2. python -m scripts.generate_key   ->  paste the two keys into .env
    3. Edit /etc/smart-locker/cifs-credentials with the real M: share login
    4. Add the fstab line from deploy/mount/fstab.snippet, then: sudo mount $MOUNT_POINT
    5. $VENV_DIR/bin/python -m scripts.init_db
    6. $VENV_DIR/bin/python -m scripts.enroll_card --name "Your Name" --role admin
    7. sudo systemctl start smart-locker   (and reboot to test the kiosk autostart)
EOF
