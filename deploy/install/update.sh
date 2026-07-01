#!/usr/bin/env bash
#
# File: update.sh
# Description: Safe, offline software update for the Smart Locker Pi appliance.
#              Picks up a release tarball delivered onto the M: share (the Pi
#              never contacts git host/the internet), then applies it with a full
#              rollback safety net: snapshot the DB + current code, swap in the
#              new code, install deps from the offline wheelhouse, run DB
#              migrations, restart the service, and HEALTH-GATE on /api/health.
#              If the new version does not come up healthy, the previous code
#              and database are restored automatically and the service is
#              restarted on the old version.
# Project: smart_locker/deploy
# Notes: Run on the Pi as: sudo deploy/install/update.sh   (the admin-panel
#        "Update now" button runs exactly this). A single-reader kiosk cannot be
#        truly hitless — one process owns the NFC reader and the SQLite DB — so
#        this trades a brief restart (seconds, invisible between card taps) for
#        a SAFE, self-reverting update on a box no one is standing next to.
#        Must stay LF (enforced by .gitattributes); CRLF breaks it on the Pi.
#
set -Eeuo pipefail

# --- Configuration (override via env) ---------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${SMART_LOCKER_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# Load the operator's .env so this script honors the same M:/service knobs the
# app uses everywhere else (SMART_LOCKER_UPDATE_DIR, SMART_LOCKER_KEEP_BACKUPS,
# ...) — one config file to edit, not a second one. The transient systemd-run
# unit that launches this script carries no environment of its own, so without
# this the update folder could never be changed except by editing this script.
if [ -f "$APP_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$APP_DIR/.env"
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

# Where releases are dropped (a folder on the M: CIFS share). Each release is a
# tarball named smart-locker-<version>.tar.gz (exactly what git host's "Download
# source" gives you for a tag). The Pi reads this folder over the LAN; it never
# needs git host or the internet.
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
          "logs" "venv" "deploy/wheelhouse" "backups" ".update-staging" ".git"
          "smart_locker/frontend/images" "VERSION")

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

if [ ! -d "$UPDATE_DIR" ]; then
  log "Update folder $UPDATE_DIR is not reachable (M: share down?) — nothing to do."
  write_status "idle" "Update share not reachable; staying on $CUR_VERSION."
  exit 0
fi

# ============================================================================
# 2. Find the newest release tarball and decide whether it is newer
# ============================================================================
# Newest by version-sorted filename: smart-locker-<version>.tar.gz
TARBALL="$(ls -1 "$UPDATE_DIR"/smart-locker-*.tar.gz 2>/dev/null | sort -V | tail -n1 || true)"
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

# Optional integrity check: if a sibling <tarball>.sha256 exists, it must match.
if [ -f "$TARBALL.sha256" ]; then
  log "Verifying checksum..."
  ( cd "$UPDATE_DIR" && sha256sum -c "$(basename "$TARBALL").sha256" >/dev/null ) \
    || { log "Checksum FAILED — refusing to apply $TARBALL."; write_status "failed" "Release checksum did not match; refused."; rm -rf "$STAGING_DIR"; exit 1; }
  log "Checksum OK."
fi

# ============================================================================
# 3. Stage the new code (strip the tarball's top-level dir)
# ============================================================================
rm -rf "$STAGING_DIR"; mkdir -p "$STAGING_DIR"
tar -xzf "$TARBALL" -C "$STAGING_DIR" --strip-components=1
[ -f "$STAGING_DIR/requirements.txt" ] || { log "Staged release looks invalid (no requirements.txt) — aborting."; write_status "failed" "Release archive is invalid."; rm -rf "$STAGING_DIR"; exit 1; }

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
log "New code in place."

# ============================================================================
# 6. Dependencies (offline wheelhouse) + DB migrations
# ============================================================================
if [ -d "$WHEELHOUSE" ] && ls "$WHEELHOUSE"/*.whl >/dev/null 2>&1; then
  log "Installing dependencies from offline wheelhouse..."
  "$VENV_DIR/bin/pip" install --no-index --find-links "$WHEELHOUSE" -r "$REQUIREMENTS" >>"$LOG_FILE" 2>&1
else
  log "No wheelhouse — skipping dependency install (assuming unchanged deps)."
fi

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
