#!/usr/bin/env bash
#
# File: update.sh
# Description: Offline software update for the Smart Locker Pi appliance.
#              Picks up a signed release tarball from the locker share, then
#              snapshots DB + code, swaps in the new tree, installs deps from
#              the wheelhouse, runs DB migrations, restarts the service, and
#              health-checks /api/health. If the new version does not come up,
#              the previous code and database are restored.
# Project: smart_locker/deploy
# Notes: Run on the Pi as: sudo bash deploy/install/update.sh   (the admin-panel
#        "Update now" button runs exactly this). Use `sudo bash <script>` — copying
#        via exFAT from Windows strips the +x bit and `sudo <path>` then fails with
#        "command not found". A single-reader kiosk cannot be truly hitless — one
#        process owns the NFC reader and the SQLite DB — so this trades a brief
#        restart (seconds, invisible between card taps) for a SAFE, self-reverting
#        update on a box no one is standing next to.
#        Must stay LF (enforced by .gitattributes); CRLF breaks it on the Pi.
#        PRESERVE keeps runtime files (.env, DB, last_sync.json, venv, logs, backups, wheelhouse,
#        deploy/system-packages, device photos) across rsync --delete; committed
#        UI images from the release are overlaid afterwards without --delete.
#
set -Eeuo pipefail

# --- Configuration (override via env) ---------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${SMART_LOCKER_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# Load the operator's .env so this script honors the same share/service knobs the
# app uses everywhere else (SMART_LOCKER_UPDATE_DIR, SMART_LOCKER_KEEP_BACKUPS,
# ...) — one config file to edit, not a second one. The transient systemd-run
# unit that launches this script carries no environment of its own, so without
# this the update folder could never be changed except by editing this script.
#
# This script runs as root, but .env is owned by the app's non-root service
# account -- so it is read as plain KEY=VALUE data (never `source`d/`.`-ed),
# which would hand root-level shell execution to anyone who can write .env.
if [ -f "$APP_DIR/.env" ]; then
  set -a
  while IFS='=' read -r _env_key _env_val; do
    [[ "$_env_key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    export "$_env_key=$_env_val"
  done < <(grep -vE '^[[:space:]]*(#|$)' "$APP_DIR/.env")
  set +a
fi

APP_USER="$(stat -c '%U' "$APP_DIR" 2>/dev/null || echo root)"   # the update unit runs as root; restore this owner after applying
VENV_DIR="$APP_DIR/venv"
PY="$VENV_DIR/bin/python"
WHEELHOUSE="$APP_DIR/deploy/wheelhouse"
REQUIREMENTS="$APP_DIR/requirements.txt"
SERVICE="${SMART_LOCKER_SERVICE:-smart-locker}"
HEALTH_URL="${SMART_LOCKER_HEALTH_URL:-http://127.0.0.1:8000/api/health}"
HEALTH_TIMEOUT="${SMART_LOCKER_HEALTH_TIMEOUT:-45}"   # seconds to wait for a healthy boot

# Where releases are dropped (a folder on the locker share). Each release is a
# tarball named smart-locker-<version>.tar.gz. The Pi reads this folder over
# the LAN; it does not need the internet.
UPDATE_DIR="${SMART_LOCKER_UPDATE_DIR:-/mnt/locker/locker-updates}"

DB_PATH="${SMART_LOCKER_DB_PATH:-$APP_DIR/smart_locker.db}"
BACKUP_DIR="$APP_DIR/backups"
STAGING_DIR="$APP_DIR/.update-staging"
LOG_FILE="$APP_DIR/logs/update.log"
STATUS_FILE="$APP_DIR/logs/update-status.json"
VERSION_FILE="$APP_DIR/VERSION"
KEEP_BACKUPS="${SMART_LOCKER_KEEP_BACKUPS:-5}"

# Runtime paths preserved across the code swap (never overwritten by a release).
PRESERVE=(".env" "smart_locker.db" "smart_locker.db-wal" "smart_locker.db-shm"
          "last_sync.json" "logs" "venv" "deploy/wheelhouse" "deploy/system-packages"
          "backups" ".update-staging" ".git" "smart_locker/frontend/images" "VERSION")

mkdir -p "$BACKUP_DIR" "$APP_DIR/logs"

# --- Logging ----------------------------------------------------------------
log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_FILE"; }

write_status() {  # write_status <state> <message>
  local state="$1" msg="$2" ver="${NEW_VERSION:-${CUR_VERSION:-unknown}}"
  printf '{"state":"%s","message":"%s","version":"%s","at":"%s"}\n' \
    "$state" "$msg" "$ver" "$(date '+%Y-%m-%dT%H:%M:%S%z')" > "$STATUS_FILE" || true
}

BACKED_UP=0
CODE_BACKUP=""
DB_BACKUP=""
OLD_VERSION=""

# --- Health check -----------------------------------------------------------
wait_for_health() {  # returns 0 if /api/health reports status:ok within timeout
  local deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if curl -fsS --max-time 4 "$HEALTH_URL" 2>/dev/null | grep -qE '"status" *: *"ok"'; then
      return 0
    fi
    sleep 2
  done
  return 1
}

# --- Consistent DB snapshot (sqlite online-backup; safe with WAL) -----------
backup_db() {
  [ -f "$DB_PATH" ] || { log "No DB at $DB_PATH yet — skipping DB backup."; return 0; }
  DB_BACKUP="$BACKUP_DIR/db-$STAMP.sqlite"
  "$PY" - "$DB_PATH" "$DB_BACKUP" <<'PYEOF'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(src); d = sqlite3.connect(dst)
with d:
    s.backup(d)          # online backup API — consistent even with an active WAL
s.close(); d.close()
PYEOF
  log "DB snapshot -> $DB_BACKUP"
}

# --- Rollback (restore the pre-update code + DB, restart on the old version) -
rollback() {
  log "ROLLBACK: restoring previous version."
  sudo systemctl stop "$SERVICE" 2>/dev/null || true
  if [ -n "$CODE_BACKUP" ] && [ -f "$CODE_BACKUP" ]; then
    tar -xzf "$CODE_BACKUP" -C "$APP_DIR"
    log "Restored code from $CODE_BACKUP"
  fi
  if [ -n "$DB_BACKUP" ] && [ -f "$DB_BACKUP" ]; then
    rm -f "$DB_PATH-wal" "$DB_PATH-shm"
    cp -f "$DB_BACKUP" "$DB_PATH"
    log "Restored DB from $DB_BACKUP"
  fi
  [ -n "$OLD_VERSION" ] && printf '%s\n' "$OLD_VERSION" > "$VERSION_FILE"
  sudo systemctl start "$SERVICE" 2>/dev/null || true
  if wait_for_health; then
    log "Rollback healthy — running previous version ${OLD_VERSION:-?}."
    write_status "rolled_back" "Update failed; reverted to previous version and recovered."
  else
    log "WARNING: service did not report healthy after rollback — check 'journalctl -u $SERVICE'."
    write_status "rollback_unhealthy" "Update failed and the service is not healthy after rollback — manual check needed."
  fi
}

on_err() {
  local line="$1"
  log "ERROR on line $line."
  if [ "$BACKED_UP" = "1" ]; then
    rollback
  else
    write_status "failed" "Update aborted before any change was applied (line $line)."
  fi
  rm -rf "$STAGING_DIR"
  exit 1
}
trap 'on_err $LINENO' ERR

# ============================================================================
# 1. Preconditions
# ============================================================================
STAMP="$(date '+%Y%m%d-%H%M%S')"
[ -x "$PY" ] || { echo "venv python not found at $PY — run install.sh first." >&2; exit 2; }
CUR_VERSION="$( [ -f "$VERSION_FILE" ] && cat "$VERSION_FILE" || echo "0" )"
OLD_VERSION="$CUR_VERSION"

log "=== Smart Locker update check (current version: $CUR_VERSION) ==="
write_status "checking" "Looking for a new release on the share."

# Fail closed before stop/backup: later HMAC verify needs openssl, the code
# swap needs rsync. Missing either would leave the kiosk down with no swap.
if ! command -v rsync >/dev/null 2>&1; then
  log "rsync not found on PATH — refusing to update (needed to swap in the new tree). Service was NOT stopped; still running $CUR_VERSION."
  write_status "failed" "rsync not found; update refused; still on $CUR_VERSION."
  exit 1
fi
if ! command -v openssl >/dev/null 2>&1; then
  log "openssl not found on PATH — refusing to update (needed to verify the release HMAC). Service was NOT stopped; still running $CUR_VERSION."
  write_status "failed" "openssl not found; update refused; still on $CUR_VERSION."
  exit 1
fi

if [ ! -d "$UPDATE_DIR" ]; then
  log "Update folder $UPDATE_DIR is not reachable (share down?) — nothing to do."
  write_status "idle" "Update share not reachable; staying on $CUR_VERSION."
  exit 0
fi

# ============================================================================
# 2. Find the newest release tarball and decide whether it is newer
# ============================================================================
# Newest by version-sorted filename: smart-locker-<version>.tar.gz
# Newest by mtime (not sort -V): pack_release names files from git describe,
# which may be a short hash that does not version-sort.
TARBALL="$(ls -1t "$UPDATE_DIR"/smart-locker-*.tar.gz 2>/dev/null | head -n1 || true)"
if [ -z "$TARBALL" ]; then
  log "No release tarball in $UPDATE_DIR — nothing to do."
  write_status "idle" "No release found on the share; staying on $CUR_VERSION."
  exit 0
fi
NEW_VERSION="$(basename "$TARBALL" | sed -E 's/^smart-locker-(.*)\.tar\.gz$/\1/')"

if [ "$NEW_VERSION" = "$CUR_VERSION" ]; then
  log "Already on the latest release ($CUR_VERSION) — nothing to do."
  write_status "up_to_date" "Already running the latest release ($CUR_VERSION)."
  exit 0
fi
log "New release available: $CUR_VERSION -> $NEW_VERSION ($TARBALL)"
write_status "updating" "Applying $NEW_VERSION."

# Mandatory integrity + authenticity check. A plain checksum sitting next to the
# tarball on the same share only proves self-consistency -- anyone with SMB
# write access to the share could forge both files together. Instead this
# requires an HMAC-SHA256 sidecar keyed with SMART_LOCKER_UPDATE_HMAC_KEY, a
# secret shared only between whoever signs releases (scripts.pack_release) and
# this Pi's .env -- so a tarball dropped without the key cannot pass. Missing
# key, missing sidecar, or a mismatch all refuse the update (fail closed).
[ -n "${SMART_LOCKER_UPDATE_HMAC_KEY:-}" ] \
  || { log "SMART_LOCKER_UPDATE_HMAC_KEY not set — refusing to apply an unverifiable release. Generate one with: python -m scripts.generate_key"; write_status "failed" "Update HMAC key not configured; refused."; rm -rf "$STAGING_DIR"; exit 1; }
[ -f "$TARBALL.hmac" ] \
  || { log "No $TARBALL.hmac sidecar — refusing to apply an unsigned release. Pack it with: python -m scripts.pack_release"; write_status "failed" "Release is unsigned; refused."; rm -rf "$STAGING_DIR"; exit 1; }

log "Verifying release signature..."
EXPECTED_HMAC="$(tr -d '[:space:]' < "$TARBALL.hmac")"
ACTUAL_HMAC="$(openssl dgst -sha256 -hmac "$SMART_LOCKER_UPDATE_HMAC_KEY" "$TARBALL" | awk '{print $NF}')"
[ "$EXPECTED_HMAC" = "$ACTUAL_HMAC" ] \
  || { log "Signature FAILED — refusing to apply $TARBALL."; write_status "failed" "Release signature did not match; refused."; rm -rf "$STAGING_DIR"; exit 1; }
log "Signature OK."

# ============================================================================
# 3. Stage the new code (strip the tarball's top-level dir)
# ============================================================================
rm -rf "$STAGING_DIR"; mkdir -p "$STAGING_DIR"
tar -xzf "$TARBALL" -C "$STAGING_DIR" --strip-components=1
[ -f "$STAGING_DIR/requirements.txt" ] || { log "Staged release looks invalid (no requirements.txt) — aborting."; write_status "failed" "Release archive is invalid."; rm -rf "$STAGING_DIR"; exit 1; }

# ============================================================================
# 3b. Offline wheelhouse preflight (BEFORE stop/rsync — kiosk stays on old version)
# ============================================================================
# Mirror install.sh: filter pyscard (system .deb, never in the wheelhouse), require a
# wheelhouse, and pip --dry-run so a stale/wrong-ABI kit fails closed without touching
# the live tree. The Pi is never-networked in production — if the preserved wheelhouse
# cannot satisfy the staged release's requirements, refuse the update now.
# deploy/wheelhouse is in PRESERVE, so the kit on disk is the one that must work.
REQS_FOR_UPDATE="$(mktemp)"
grep -vi '^pyscard' "$STAGING_DIR/requirements.txt" > "$REQS_FOR_UPDATE"
# Keep cleaned up on every exit path (success, refuse, or ERR rollback).
trap 'rm -f "$REQS_FOR_UPDATE"' EXIT
if [ ! -d "$WHEELHOUSE" ] || ! ls "$WHEELHOUSE"/*.whl >/dev/null 2>&1; then
  log "FATAL: no wheelhouse at $WHEELHOUSE — cannot safely update deps in-field."
  log "    The Pi has no internet; a release without a matching offline wheelhouse is refused."
  log "    Service was NOT stopped; live code was NOT swapped — still running $CUR_VERSION."
  write_status "failed" "No wheelhouse; update refused; still on $CUR_VERSION."
  rm -rf "$STAGING_DIR"
  exit 1
fi
log "Preflight: verifying wheelhouse satisfies staged requirements (pip --dry-run)..."
DRY_LOG="$(mktemp)"
# --ignore-installed: system-site-packages must not mask an incomplete wheelhouse.
if ! "$VENV_DIR/bin/pip" install --dry-run --ignore-installed --no-index --find-links "$WHEELHOUSE" -r "$REQS_FOR_UPDATE" >"$DRY_LOG" 2>&1; then
  log "FATAL: wheelhouse cannot satisfy the staged requirements.txt for this Python."
  log "    (typical cause: a stale cp311 wheelhouse on a cp313/trixie Pi.)"
  log "    Rebuild with deploy/install/build-wheelhouse.sh on a machine with internet,"
  log "    recopy deploy/wheelhouse/ to the Pi, then retry. Service was NOT stopped;"
  log "    live code was NOT swapped — still running $CUR_VERSION."
  log "    --- pip dry-run output (last 30 lines) ---"
  tail -n 30 "$DRY_LOG" >>"$LOG_FILE" 2>&1 || true
  rm -f "$DRY_LOG"
  write_status "failed" "Wheelhouse cannot satisfy requirements; update refused; still on $CUR_VERSION."
  rm -rf "$STAGING_DIR"
  exit 1
fi
rm -f "$DRY_LOG"
log "Preflight OK — wheelhouse can satisfy staged requirements."

# ============================================================================
# 4. Backup (the rollback point) — from here on, failure triggers rollback
# ============================================================================
CODE_BACKUP="$BACKUP_DIR/code-$STAMP.tar.gz"
TAR_EXCLUDES=()
for p in "${PRESERVE[@]}"; do TAR_EXCLUDES+=( --exclude="./$p" ); done
( cd "$APP_DIR" && tar -czf "$CODE_BACKUP" "${TAR_EXCLUDES[@]}" . )
log "Code snapshot -> $CODE_BACKUP"
backup_db
BACKED_UP=1

# ============================================================================
# 5. Apply: stop service, sync new code in (preserving runtime files)
# ============================================================================
log "Stopping $SERVICE for the swap..."
sudo systemctl stop "$SERVICE"

RSYNC_EXCLUDES=()
for p in "${PRESERVE[@]}"; do RSYNC_EXCLUDES+=( --exclude="/$p" ); done
rsync -a --delete "${RSYNC_EXCLUDES[@]}" "$STAGING_DIR"/ "$APP_DIR"/
# The transient update unit runs as root, so newly written files are root-owned;
# hand the tree back to the service account (runtime dirs were preserved anyway).
chown -R "$APP_USER":"$APP_USER" "$APP_DIR" 2>/dev/null || true
# PRESERVE skipped this dir so gitignored device photos survive --delete.
# Overlay committed UI assets from the staged release without removing photos.
if [ -d "$STAGING_DIR/smart_locker/frontend/images" ]; then
  mkdir -p "$APP_DIR/smart_locker/frontend/images"
  rsync -a "$STAGING_DIR/smart_locker/frontend/images/" "$APP_DIR/smart_locker/frontend/images/"
  chown -R "$APP_USER":"$APP_USER" "$APP_DIR/smart_locker/frontend/images" 2>/dev/null || true
fi
log "New code in place."

# ============================================================================
# 6. Dependencies (offline wheelhouse) + DB migrations
# ============================================================================
# Preflight already proved the wheelhouse resolves REQS_FOR_UPDATE; install for real.
# On failure, ERR trap → rollback (BACKED_UP=1) restores previous code + restarts service.
log "Installing dependencies from offline wheelhouse..."
"$VENV_DIR/bin/pip" install --ignore-installed --no-index --find-links "$WHEELHOUSE" -r "$REQS_FOR_UPDATE" >>"$LOG_FILE" 2>&1

log "Running database migrations..."
( cd "$APP_DIR" && "$PY" -m scripts.migrate_db >>"$LOG_FILE" 2>&1 )

# ============================================================================
# 7. Restart and HEALTH-GATE
# ============================================================================
printf '%s\n' "$NEW_VERSION" > "$VERSION_FILE"
log "Starting $SERVICE on $NEW_VERSION..."
sudo systemctl start "$SERVICE"

if wait_for_health; then
  log "=== Update OK: now running $NEW_VERSION (healthy). ==="
  write_status "success" "Updated to $NEW_VERSION and verified healthy."
  trap - ERR
  # Refresh sudoers from the new tree (adds poweroff). Failure here must not
  # roll back a healthy update — document SSH: sudo bash deploy/install/apply-sudoers.sh
  if [ -f "$APP_DIR/deploy/install/apply-sudoers.sh" ]; then
    if bash "$APP_DIR/deploy/install/apply-sudoers.sh"; then
      log "Refreshed /etc/sudoers.d/smart-locker."
    else
      log "WARNING: sudoers refresh failed — Shut down from the admin panel needs: sudo bash deploy/install/apply-sudoers.sh"
    fi
  fi
  rm -rf "$STAGING_DIR"
  # Prune old backups, keep the most recent KEEP_BACKUPS of each kind.
  ls -1t "$BACKUP_DIR"/code-*.tar.gz 2>/dev/null | tail -n +"$((KEEP_BACKUPS+1))" | xargs -r rm -f
  ls -1t "$BACKUP_DIR"/db-*.sqlite   2>/dev/null | tail -n +"$((KEEP_BACKUPS+1))" | xargs -r rm -f
  exit 0
else
  log "New version did NOT become healthy within ${HEALTH_TIMEOUT}s — rolling back."
  rollback
  rm -rf "$STAGING_DIR"
  exit 1
fi
