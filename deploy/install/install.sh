#!/usr/bin/env bash
# install.sh — provision a Raspberry Pi to run Smart Locker as a kiosk appliance.
# ----------------------------------------------------------------------------
# ALWAYS run as:
#     sudo bash deploy/install/install.sh
# Never:
#     sudo deploy/install/install.sh          # "command not found" if +x stripped
#     sudo ./deploy/install/install.sh        # same when +x missing (exFAT/Windows)
# `bash <script>` does not need the +x bit. After a successful run this script
# re-chmods itself so a later `sudo ./deploy/install/install.sh` also works.
#
# It is idempotent — safe to re-run. It will:
#   1. Install OS packages (pcscd, libccid, …) via apt when online, or via
#      deploy/system-packages/*.deb when offline (no apt in production).
#   2. Create the Python virtualenv and install deps from deploy/wheelhouse
#      (offline kit) — fail closed if the kit is missing/stale.
#   3. Enable pcscd (PC/SC daemon for the ACR1252U reader) + polkit rule.
#   4. Install + enable the smart-locker systemd service.
#   5. Scaffold the CIFS mount point and credentials file.
#   6. Install the Chromium kiosk autostart entry for the app user.
#
# Overridable via environment variables:
#   SMART_LOCKER_DIR   (default: the repo this script lives in)
#   SMART_LOCKER_USER  (default: systemd service User, then logs owner,
#                       then APP_DIR owner if not root, else locker)
#   SMART_LOCKER_MOUNT (default: /mnt/locker)
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root with bash (required — do not omit 'bash'):" >&2
  echo "    sudo bash deploy/install/install.sh" >&2
  echo "" >&2
  echo "If you saw:  sudo: deploy/install/install.sh: command not found" >&2
  echo "you used 'sudo <path>' without bash, or the +x bit was stripped by exFAT." >&2
  echo "'sudo bash deploy/install/install.sh' always works." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Restore +x on this script (and siblings) so a future direct exec also works
# after an exFAT copy. Harmless if already executable.
chmod +x "$SCRIPT_DIR/install.sh" "$SCRIPT_DIR/update.sh" "$SCRIPT_DIR/build-wheelhouse.sh" "$SCRIPT_DIR/apply-sudoers.sh" 2>/dev/null || true
APP_DIR="${SMART_LOCKER_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
# After C3 the tree is root-owned. Never take the service account from
# stat APP_DIR when that owner is root (USB re-install would write User=root).
if [ -z "${SMART_LOCKER_USER:-}" ]; then
  _svc_user="$(systemctl show -p User --value smart-locker.service 2>/dev/null || true)"
  if [ -n "$_svc_user" ] && [ "$_svc_user" != "-" ] && [ "$_svc_user" != "root" ]; then
    SMART_LOCKER_USER="$_svc_user"
  elif [ -d "$APP_DIR/logs" ]; then
    _log_user="$(stat -c '%U' "$APP_DIR/logs" 2>/dev/null || true)"
    if [ -n "$_log_user" ] && [ "$_log_user" != "root" ]; then
      SMART_LOCKER_USER="$_log_user"
    fi
  fi
  if [ -z "${SMART_LOCKER_USER:-}" ]; then
    _dir_user="$(stat -c '%U' "$APP_DIR" 2>/dev/null || true)"
    if [ -n "$_dir_user" ] && [ "$_dir_user" != "root" ]; then
      SMART_LOCKER_USER="$_dir_user"
    fi
  fi
fi
APP_USER="${SMART_LOCKER_USER:-locker}"
export SMART_LOCKER_USER="$APP_USER"
APP_GROUP="$(id -gn "$APP_USER")"
MOUNT_POINT="${SMART_LOCKER_MOUNT:-/mnt/locker}"
VENV_DIR="$APP_DIR/venv"
WHEELHOUSE="$APP_DIR/deploy/wheelhouse"
SYSDEST="$APP_DIR/deploy/system-packages"

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
[ "$ONLINE" -eq 1 ] && echo "==> Network: online"  || echo "==> Network: OFFLINE (no apt — OS .debs from deploy/system-packages/ + Full image)"

# --- 1. OS packages ---
# python3-pyscard is here (not in requirements.txt/wheelhouse) because it has no
# prebuilt Linux aarch64 wheel on PyPI — see deploy/system-packages/README.md.
PKGS="pcscd pcsc-tools libccid cifs-utils chromium unclutter curl python3-venv python3-pip python3-pyscard"
if [ "$ONLINE" -eq 1 ]; then
  echo "==> Installing OS packages: $PKGS"
  apt-get update -y
  # 'chromium' is the package on Bookworm; fall back to 'chromium-browser' on older OS.
  if ! apt-get install -y --no-install-recommends $PKGS; then
    echo "    'chromium' not found — retrying with 'chromium-browser'."
    apt-get install -y --no-install-recommends pcscd pcsc-tools libccid cifs-utils chromium-browser unclutter curl python3-venv python3-pip python3-pyscard
  fi
else
  # Production is never-networked and does not use apt. Stage OS .debs via
  # build-wheelhouse.sh into deploy/system-packages/ (pcscd stack, unclutter,
  # python3-pyscard — the packages a trixie Full image does NOT already ship).
  # Base tools (python3, curl, chromium, cifs-utils, python3-venv) come from the
  # Full image itself.
  echo "==> Offline OS packages from $SYSDEST (no apt)"
  if ls "$SYSDEST"/*.deb >/dev/null 2>&1; then
    # Prefer dependency-friendly order (libccid before pcscd, perl deps before
    # pcsc-tools, pyscard last among NFC bits). Any leftover .debs install after.
    echo "==> dpkg -i staged .deb files..."
    DEB_ORDER=()
    for pat in \
      'libccid_*.deb' \
      'pcscd_*.deb' \
      'libintl-perl_*.deb' \
      'libpcsc-perl_*.deb' \
      'pcsc-tools_*.deb' \
      'python3-pyscard_*.deb' \
      'unclutter_*.deb'
    do
      for f in "$SYSDEST"/$pat; do
        [ -f "$f" ] && DEB_ORDER+=("$f")
      done
    done
    for f in "$SYSDEST"/*.deb; do
      [ -f "$f" ] || continue
      already=0
      for g in "${DEB_ORDER[@]+"${DEB_ORDER[@]}"}"; do
        [ "$f" = "$g" ] && already=1 && break
      done
      [ "$already" -eq 0 ] && DEB_ORDER+=("$f")
    done
    if [ "${#DEB_ORDER[@]}" -eq 0 ] || ! dpkg -i "${DEB_ORDER[@]}"; then
      echo "    FATAL: dpkg -i of deploy/system-packages/*.deb failed (no apt to fix)." >&2
      echo "    Rebuild the offline kit with deploy/install/build-wheelhouse.sh and recopy." >&2
      exit 1
    fi
  else
    echo "    WARNING: no .deb files in $SYSDEST — relying on the OS image alone."
  fi
  echo "==> Verifying required commands are present (offline, no apt)"
  MISSING=""
  for cmd in pcscd mount.cifs curl python3; do
    command -v "$cmd" >/dev/null 2>&1 || MISSING="$MISSING $cmd"
  done
  if ! command -v chromium >/dev/null 2>&1 && ! command -v chromium-browser >/dev/null 2>&1; then
    MISSING="$MISSING chromium"
  fi
  if [ -n "$MISSING" ]; then
    echo "    FATAL: missing OS tools:$MISSING" >&2
    echo "    Expected on Raspberry Pi OS Full: chromium, curl, python3, cifs-utils." >&2
    echo "    Expected from deploy/system-packages/*.deb: pcscd (+ libccid, pcsc-tools)." >&2
    echo "    Re-run build-wheelhouse.sh, recopy deploy/system-packages/, re-image if needed." >&2
    exit 1
  fi
  if ! python3 -c "import smartcard" >/dev/null 2>&1; then
    echo "    FATAL: python3-pyscard not importable after offline dpkg." >&2
    echo "    Need python3-pyscard_*_arm64.deb in deploy/system-packages/ (build-wheelhouse.sh)." >&2
    exit 1
  fi
  echo "==> Offline OS package check OK (incl. import smartcard)."
fi

# --- 2. Python virtualenv + dependencies ---
# --system-site-packages: python3-pyscard is installed at the SYSTEM level (via apt/dpkg
# above, not pip — it has no aarch64 wheel to put in the venv/wheelhouse), so the venv
# needs visibility into system packages to see it.
# A venv created before this script gained --system-site-packages (or any venv that
# predates pyscard moving to a system-level apt/dpkg install) cannot see python3-pyscard,
# even though the package IS installed. Re-running this "idempotent" script must not leave
# that stale venv in place — detect it via pyvenv.cfg and rebuild it.
if [ -d "$VENV_DIR" ] && ! grep -q '^include-system-site-packages = true$' "$VENV_DIR/pyvenv.cfg" 2>/dev/null; then
  echo "==> Existing virtualenv at $VENV_DIR predates --system-site-packages (pyscard would be invisible) — recreating it"
  rm -rf "$VENV_DIR"
fi
if [ ! -d "$VENV_DIR" ]; then
  echo "==> Creating virtualenv at $VENV_DIR"
  sudo -u "$APP_USER" python3 -m venv --system-site-packages "$VENV_DIR"
fi
echo "==> Installing Python dependencies"
# pyscard is excluded here — always installed via apt/dpkg above, never via pip.
REQS_NO_PYSCARD="$(mktemp)"
grep -vi '^pyscard' "$APP_DIR/requirements.txt" > "$REQS_NO_PYSCARD"
# mktemp defaults to mode 0600, root-owned (this whole script runs as root). The pip
# install below runs as APP_USER via sudo -u, which could not otherwise READ this file.
chmod 644 "$REQS_NO_PYSCARD"
if ls "$WHEELHOUSE"/*.whl >/dev/null 2>&1; then
  echo "    using offline wheelhouse: $WHEELHOUSE"
  # Preflight: do the EXACT validation pip would do, but WITHOUT installing anything.
  # `pip install --dry-run --no-index --find-links` resolves every requirement against
  # the wheelhouse and reports any unresolvable one. This is the only preflight that
  # catches the real failure mode: a stale cp311 kit on a cp313 Pi PASSes an
  # existence-grep (any py3-none wheel satisfies "(-cp313-|abi3-|py3-none)"), but
  # `pip --dry-run` correctly fails on SQLAlchemy (no abi3 build, needs exact cp313).
  # Reuse the same filtered reqs file for dry-run and the real install (pyscard is
  # never in the wheelhouse — installed separately as a system .deb).
  # --ignore-installed: do NOT let system-site-packages (e.g. distro cryptography)
  # mask an incomplete wheelhouse. The kit must be self-contained for every pip
  # requirement except pyscard (system .deb, filtered out of REQS_NO_PYSCARD).
  DRY_LOG="$(mktemp)"
  if ! sudo -u "$APP_USER" "$VENV_DIR/bin/pip" install --dry-run --ignore-installed --no-index --find-links "$WHEELHOUSE" -r "$REQS_NO_PYSCARD" >"$DRY_LOG" 2>&1; then
    echo "    FATAL: wheelhouse cannot satisfy requirements.txt for this Python" >&2
    echo "    (typical cause: a cp311 wheelhouse on a cp313/trixie Pi — pip finds no" >&2
    echo "    cp313 wheel for a binary package like SQLAlchemy)." >&2
    echo "    Rebuild with deploy/install/build-wheelhouse.sh on a machine with internet," >&2
    echo "    then recopy deploy/wheelhouse/ to the Pi before running this script." >&2
    echo "    --- pip dry-run output (last 30 lines) ---" >&2
    tail -n 30 "$DRY_LOG" >&2 || true
    rm -f "$DRY_LOG" "$REQS_NO_PYSCARD"
    exit 1
  fi
  rm -f "$DRY_LOG"
  sudo -u "$APP_USER" "$VENV_DIR/bin/pip" install --ignore-installed --no-index --find-links "$WHEELHOUSE" -r "$REQS_NO_PYSCARD"
elif [ "$ONLINE" -eq 1 ]; then
  sudo -u "$APP_USER" "$VENV_DIR/bin/pip" install -r "$REQS_NO_PYSCARD"
else
  # Offline + NO wheelhouse: fail loud rather than silently "skipping" and leaving a
  # half-installed venv. The previous "Skipping" path hid exactly the failure mode
  # the preflight above is designed to catch — a missing/stale wheelhouse is not
  # something to paper over.
  echo "    FATAL: offline and no wheelhouse found at $WHEELHOUSE." >&2
  echo "    Run deploy/install/build-wheelhouse.sh on a machine with internet," >&2
  echo "    then recopy deploy/wheelhouse/ and deploy/system-packages/ to the Pi." >&2
  exit 1
fi
rm -f "$REQS_NO_PYSCARD"
if "$VENV_DIR/bin/python" -c "import smartcard" >/dev/null 2>&1; then
  echo "==> Verified: the venv can import smartcard (pyscard) via --system-site-packages."
else
  echo "    WARNING: the venv cannot import smartcard — the NFC reader will not work."
  echo "    Check that python3-pyscard is installed (see deploy/system-packages/README.md)."
  echo "    If it IS installed, the venv itself may be missing --system-site-packages —"
  echo "    delete it and re-run this script: rm -rf $VENV_DIR && sudo bash $SCRIPT_DIR/install.sh"
fi

# --- 3. Enable the PC/SC daemon (ACR1252U reader) ---
echo "==> Enabling pcscd"
systemctl enable --now pcscd || echo "    (could not start pcscd now — it is socket-activated and will start on demand)"

# --- 3b. pcscd group, socket group, and polkit rule (critical for the NFC
# reader on Raspberry Pi OS trixie and beyond) -----------------------------------
# On trixie, SCardEstablishContext() returns "Access denied" (0x8010006A) even
# when /run/pcscd/pcscd.comm is world-writable and the app user is in the pcscd
# group — polkit gates access_pcsc/access_card, and a non-console session (SSH
# or a systemd service) gets denied by default. The base image also does not
# always create the 'pcscd' group. The block below:
#   (1) creates the 'pcscd' group if missing (groupadd is a standard core
#       command — no extra package needed, online or offline);
#   (2) adds the app user to it;
#   (3) sets SocketGroup=pcscd on pcscd.socket via a drop-in so the socket
#       created on next activation has the right group;
#   (4) installs a polkit rule granting pcscd/plugdev group members access —
#       the actual authorization fix on trixie;
#   (5) restarts pcscd.socket + pcscd.service so all of the above is live
#       immediately, no reboot required.
# We deliberately do NOT chmod the socket mode: pcscd.socket's own SocketMode=
# (typically 0666) is the authority for the mode, and the polkit rule is the
# authority for access — tightening the mode here would be overridden on every
# (re)activation and is unnecessary. The chgrp on a pre-existing socket is the
# only best-effort live fix; the drop-in handles future activations.
if ! getent group pcscd >/dev/null 2>&1; then
  groupadd --system pcscd 2>/dev/null || true
fi
if getent group pcscd >/dev/null 2>&1; then
  usermod -a -G pcscd "$APP_USER" 2>/dev/null || true
  echo "==> Added $APP_USER to pcscd group (required for NFC reader)"
fi
for sock in /run/pcscd/pcscd.comm /var/run/pcscd/pcscd.comm; do
  if [ -S "$sock" ]; then
    chgrp pcscd "$sock" 2>/dev/null || true
    break
  fi
done
mkdir -p /etc/systemd/system/pcscd.socket.d
cat > /etc/systemd/system/pcscd.socket.d/smart-locker.conf << 'EOC'
[Socket]
SocketGroup=pcscd
EOC
systemctl daemon-reload || true
echo "==> Configured pcscd.socket to use group pcscd"
# polkit rule — needed for SSH and service users on trixie. polkit ships on the
# desktop image (no extra package to install, online or offline).
mkdir -p /etc/polkit-1/rules.d
cat > /etc/polkit-1/rules.d/50-smart-locker-pcsc.rules << 'EOR'
polkit.addRule(function(action, subject) {
    if ((action.id == "org.debian.pcsc-lite.access_pcsc" ||
         action.id == "org.debian.pcsc-lite.access_card") &&
        (subject.isInGroup("pcscd") || subject.isInGroup("plugdev"))) {
        return polkit.Result.YES;
    }
});
EOR
systemctl try-restart polkit 2>/dev/null || true
echo "==> Installed polkit rule for pcscd/plugdev groups (fixes SCardEstablishContext for SSH/service)"
# Restart last so the group, socket drop-in, and polkit rule are all live after install.sh.
# This lets the user run pcsc_scan immediately after the script finishes, with no reboot.
systemctl restart pcscd.socket pcscd.service 2>/dev/null || systemctl restart pcscd 2>/dev/null || true
echo "==> Restarted pcscd so the NFC setup is live immediately (no reboot needed)"

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

# --- 4b. Sudoers + update script (powers in-app Update / Shut down) ---
echo "==> Installing sudoers drop-in for self-service updates and poweroff"
chmod +x "$APP_DIR/deploy/install/update.sh" "$APP_DIR/deploy/install/apply-sudoers.sh"
if ! bash "$APP_DIR/deploy/install/apply-sudoers.sh"; then
  echo "    WARNING: sudoers was NOT installed. Software Update and Shut down from the"
  echo "             admin panel will fail until: sudo bash deploy/install/apply-sudoers.sh"
fi

# --- 5. CIFS mount scaffolding ---
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

# --- 7. Root-own the application tree; only runtime dirs are service-writable ---
# The sudoers rule lets the service account run deploy/install/update.sh as root.
# Exact argv is not a privilege boundary if that script is user-writable.
echo "==> Setting ownership of $APP_DIR (root tree, service-writable runtime dirs)"
mkdir -p "$APP_DIR/logs" "$APP_DIR/smart_locker/frontend/images" "$APP_DIR/backups"
chown -R root:root "$APP_DIR"
chmod -R u=rwX,go=rX "$APP_DIR"
chmod +x "$APP_DIR/deploy/install/"*.sh "$APP_DIR/deploy/kiosk/start-kiosk.sh" 2>/dev/null || true
# SQLite WAL files are created next to the DB (default: APP_DIR). Sticky
# group-write on APP_DIR lets the service create .db-wal without unlinking
# root-owned files. deploy/install/ stays 755 root, so update.sh cannot be replaced.
chown root:"$APP_GROUP" "$APP_DIR"
chmod 1775 "$APP_DIR"
chown -R "$APP_USER:$APP_GROUP" "$APP_DIR/logs"
chown -R "$APP_USER:$APP_GROUP" "$APP_DIR/smart_locker/frontend/images"
chown -R "$APP_USER:$APP_GROUP" "$APP_DIR/backups"
if [ -f "$APP_DIR/.env" ]; then
  chown root:"$APP_GROUP" "$APP_DIR/.env"
  chmod 640 "$APP_DIR/.env"
fi
for f in "$APP_DIR/smart_locker.db" "$APP_DIR/smart_locker.db-wal" "$APP_DIR/smart_locker.db-shm" "$APP_DIR/last_sync.json"; do
  if [ -e "$f" ]; then
    chown "$APP_USER:$APP_GROUP" "$f"
  fi
done

cat <<EOF

==> Done. Remaining manual steps (see GUIDE.md for the full walkthrough):
    1. cp deploy/.env.pi.example .env   &&  edit .env  (paths are pre-filled)
    2. python -m scripts.generate_key   ->  paste ENC and HMAC keys into .env
    3. Edit /etc/smart-locker/cifs-credentials with the real locker share login
    4. Add the fstab line from deploy/mount/fstab.snippet, then: sudo mount $MOUNT_POINT
    5. $VENV_DIR/bin/python -m scripts.init_db
    6. $VENV_DIR/bin/python -m scripts.enroll_card --name "Your Name" --role admin
    7. sudo systemctl start smart-locker   (and reboot to test the kiosk autostart)
EOF
