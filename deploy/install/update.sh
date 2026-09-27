#!/usr/bin/env bash
#
# File: update.sh
# Description: Offline software update for the Smart Locker Pi appliance.
#              Finds locker-updates/ (USB copy into $APP_DIR/locker-updates, or
#              that folder already on disk), then snapshots DB + code, swaps in
#              the new tree, installs deps from the existing Pi wheelhouse, runs
#              DB migrations, restarts the service, and health-checks /api/health.
#              If the new version does not come up, the previous code and database
#              are restored. The only payload is an unpacked locker-updates tree
#              (python -m scripts.copy_update). Missing wheels are warned at
#              USB-prep (copy_update); pip failure here rolls back.
# Project: smart_locker/deploy
# Notes: Run on the Pi as: sudo bash deploy/install/update.sh   (the admin-panel
#        "Software Update" button runs exactly this). Use `sudo bash <script>` —
#        copying via exFAT from Windows strips the +x bit and `sudo <path>` then
#        fails with "command not found". A single-reader kiosk cannot be truly
#        hitless — one process owns the NFC reader and the SQLite DB — so this
#        trades a brief restart (seconds, invisible between card taps) for a SAFE,
#        self-reverting update on a box no one is standing next to.
#        Must stay LF (enforced by .gitattributes); CRLF breaks it on the Pi.
#        PRESERVE keeps runtime files (.env, DB, last_sync.json, mirror_state.json
#        and the default-path smart_locker_catalog.xlsx mirror, venv, logs,
#        backups, wheelhouse, deploy/system-packages, device photos) across
#        rsync --delete;
#        committed UI images from the incoming tree are overlaid afterwards without
#        --delete. The rollback snapshot additionally carries the venv and the
#        images dir so a revert restores the exact dependency set and the
#        previous release's UI assets, and rollback re-applies the same runtime
#        ownership/permission fixups as the swap so the restored service can
#        boot. Extra .whl files in the incoming locker-updates tree are copied
#        into the Pi wheelhouse when the Pi does not already have that filename.
#        Missing wheels are not a refuse; pip failure after backup rolls back.
#        Set SMART_LOCKER_UPDATE_LIB=1 before sourcing this file from tests.
#
set -Eeuo pipefail

# --- Configuration (override via env) ---------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${SMART_LOCKER_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# Load the operator's .env so this script honors the same service knobs the
# app uses everywhere else (SMART_LOCKER_KEEP_BACKUPS, SMART_LOCKER_USER, ...).
# The transient systemd-run unit that launches this script carries no
# environment of its own. Apply source is USB locker-updates/ then
# $APP_DIR/locker-updates — not a CIFS drop.
#
# This script runs as root, but .env is owned by the app's non-root service
# account -- so it is read as plain KEY=VALUE data (never `source`d/`.`-ed),
# which would hand root-level shell execution to anyone who can write .env.
if [ -f "$APP_DIR/.env" ]; then
  set -a
  while IFS='=' read -r _env_key _env_val; do
    [[ "$_env_key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    # Match python-dotenv closely enough for our keys: trim whitespace, a
    # fully-quoted value keeps an inner '#', otherwise ' #' starts a comment.
    # (SMART_LOCKER_USER="locker" used to export the quotes literally.)
    _env_val="${_env_val#"${_env_val%%[![:space:]]*}"}"
    _env_val="${_env_val%"${_env_val##*[![:space:]]}"}"
    if [[ "$_env_val" =~ ^\"(.*)\"[[:space:]]*(\#.*)?$ ]] \
      || [[ "$_env_val" =~ ^\'(.*)\'[[:space:]]*(\#.*)?$ ]]; then
      _env_val="${BASH_REMATCH[1]}"
    else
      _env_val="${_env_val%%[[:space:]]#*}"
      _env_val="${_env_val%"${_env_val##*[![:space:]]}"}"
    fi
    export "$_env_key=$_env_val"
  done < <(grep -vE '^[[:space:]]*(#|$)' "$APP_DIR/.env")
  set +a
fi

# Do not stat APP_DIR for the service account: the application tree is
# root-owned so the passwordless sudoers target is not user-writable.
if [ -z "${SMART_LOCKER_USER:-}" ]; then
  _svc_user="$(systemctl show -p User --value "${SERVICE:-smart-locker}.service" 2>/dev/null || true)"
  if [ -n "$_svc_user" ] && [ "$_svc_user" != "-" ] && [ "$_svc_user" != "root" ]; then
    SMART_LOCKER_USER="$_svc_user"
  elif [ -d "$APP_DIR/logs" ]; then
    _log_user="$(stat -c '%U' "$APP_DIR/logs" 2>/dev/null || true)"
    if [ -n "$_log_user" ] && [ "$_log_user" != "root" ]; then
      SMART_LOCKER_USER="$_log_user"
    fi
  fi
fi
APP_USER="${SMART_LOCKER_USER:-locker}"
export SMART_LOCKER_USER="$APP_USER"
VENV_DIR="$APP_DIR/venv"
PY="$VENV_DIR/bin/python"
WHEELHOUSE="$APP_DIR/deploy/wheelhouse"
REQUIREMENTS="$APP_DIR/requirements.txt"
SERVICE="${SMART_LOCKER_SERVICE:-smart-locker}"
HEALTH_URL="${SMART_LOCKER_HEALTH_URL:-http://127.0.0.1:8000/api/health}"
HEALTH_TIMEOUT="${SMART_LOCKER_HEALTH_TIMEOUT:-45}"   # seconds to wait for a healthy boot

# Raspberry Pi OS auto-mounts USB sticks at /media/<user>/<label>. Not an .env
# key on the appliance — overridable so the e2e sandbox injects a fake media root.
USB_MEDIA_ROOT="${SMART_LOCKER_USB_MEDIA_ROOT:-/media}"
# Apply source: Windows copy_update payload, or USB locker-updates copied here.
LOCAL_UPDATES="$APP_DIR/locker-updates"

DB_PATH="${SMART_LOCKER_DB_PATH:-$APP_DIR/smart_locker.db}"
BACKUP_DIR="$APP_DIR/backups"
STAGING_DIR="$APP_DIR/.update-staging"
LOG_FILE="$APP_DIR/logs/update.log"
STATUS_FILE="$APP_DIR/logs/update-status.json"
VERSION_FILE="$APP_DIR/VERSION"
KEEP_BACKUPS="${SMART_LOCKER_KEEP_BACKUPS:-5}"

# Runtime paths preserved across the code swap (never overwritten by a release).
# dashboard.secret holds the Setup-typed dashboard password — service-owned,
# not root-owned .env (the service cannot rewrite .env inside the sticky
# root-owned app dir, and must not: this script parses .env as root).
PRESERVE=(".env" "dashboard.secret" "smart_locker.db" "smart_locker.db-wal"
          "smart_locker.db-shm"
          "last_sync.json" "mirror_state.json" "smart_locker_catalog.xlsx"
          "logs" "venv" "deploy/wheelhouse" "deploy/system-packages"
          "backups" ".update-staging" ".git" "smart_locker/frontend/images" "VERSION"
          "locker-updates")

# Skip these from an incoming tree even if they were copied onto the stick.
INCOMING_SKIP=(".env" "dashboard.secret" "venv" "logs" "backups" "smart_locker.db"
               "smart_locker.db-wal"
               "smart_locker.db-shm" "deploy/wheelhouse" "deploy/system-packages"
               ".update-staging" ".git"
               "mirror_state.json" "smart_locker_catalog.xlsx")
# USB → $APP_DIR/locker-updates may include extra wheels the Pi does not have.
PAYLOAD_SKIP=(".env" "dashboard.secret" "venv" "logs" "backups" "smart_locker.db"
              "smart_locker.db-wal"
              "smart_locker.db-shm" ".update-staging" ".git" "locker-updates"
              "mirror_state.json" "smart_locker_catalog.xlsx")

# The rollback snapshot keeps the same runtime files out of the tar EXCEPT the
# venv and the images dir: pip runs before the health gate, so a rollback must
# restore the exact dependency set and the previous release's committed UI
# assets — not leave old code running on the new payload's venv/images. Both
# stay in PRESERVE for the forward swap (an incoming tree must not overwrite
# the venv, and --delete must not remove device photos).
BACKUP_SKIP=()
for p in "${PRESERVE[@]}"; do
  case "$p" in
    venv|smart_locker/frontend/images) ;;
    *) BACKUP_SKIP+=("$p") ;;
  esac
done

mkdir -p "$BACKUP_DIR" "$APP_DIR/logs"

# --- Logging ----------------------------------------------------------------
log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_FILE"; }

_status_python() {
  if [ -x "${PY:-}" ]; then
    printf '%s' "$PY"
  elif command -v python3 >/dev/null 2>&1; then
    command -v python3
  else
    command -v python
  fi
}

write_status() {  # write_status <state> <message> [version]
  local state="$1" msg="$2" ver="${3:-${NEW_VERSION:-${CUR_VERSION:-unknown}}}" at
  at="$(date '+%Y-%m-%dT%H:%M:%S%z')"
  local pybin
  pybin="$(_status_python)" || pybin=""
  if [ -n "$pybin" ]; then
    "$pybin" - "$STATUS_FILE" "$state" "$msg" "$ver" "$at" <<'PY' || true
import json, sys
path, state, msg, ver, at = sys.argv[1:6]
payload = {"state": state, "message": msg, "version": ver, "at": at}
with open(path, "w", encoding="utf-8") as fh:
    fh.write(json.dumps(payload) + "\n")
PY
    return 0
  fi
  printf '{"state":"%s","message":"%s","version":"%s","at":"%s"}\n' \
    "$state" "$msg" "$ver" "$at" > "$STATUS_FILE" || true
}

BACKED_UP=0
ROLLBACK_RAN=0
CODE_BACKUP=""
DB_BACKUP=""
OLD_VERSION=""
SOURCE_KIND=""
SOURCE_PATH=""

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

# --- Ownership/permissions after any rsync into $APP_DIR --------------------
# Shared by the swap and the rollback so both leave the tree service-ready.
# Newly written files are root-owned (this unit runs as root). Keep it that
# way: do NOT chown the tree to the service account. Only runtime dirs
# (logs, photos, backups) and the SQLite files are service-writable; .env is
# group-readable by the service account.
apply_runtime_permissions() {
  chown -R root:root "$APP_DIR" 2>/dev/null || true
  chmod -R u=rwX,go=rX "$APP_DIR" 2>/dev/null || true
  chmod +x "$APP_DIR/deploy/install/"*.sh "$APP_DIR/deploy/kiosk/start-kiosk.sh" 2>/dev/null || true
  chown root:"$(id -gn "$APP_USER" 2>/dev/null || echo root)" "$APP_DIR" 2>/dev/null || true
  chmod 1775 "$APP_DIR" 2>/dev/null || true
  mkdir -p "$APP_DIR/logs" "$APP_DIR/smart_locker/frontend/images" "$APP_DIR/backups"
  chown -R "$APP_USER":"$APP_USER" "$APP_DIR/logs" 2>/dev/null || true
  chown -R "$APP_USER":"$APP_USER" "$APP_DIR/backups" 2>/dev/null || true
  if [ -f "$APP_DIR/.env" ]; then
    chown root:"$(id -gn "$APP_USER" 2>/dev/null || echo root)" "$APP_DIR/.env" 2>/dev/null || true
    chmod 640 "$APP_DIR/.env" 2>/dev/null || true
  fi
  # The Setup-typed dashboard password lives service-owned (never root-owned
  # like .env) so the kiosk can keep writing it inside the sticky app dir.
  if [ -f "$APP_DIR/dashboard.secret" ]; then
    chown "$APP_USER:$(id -gn "$APP_USER" 2>/dev/null || echo "$APP_USER")" "$APP_DIR/dashboard.secret" 2>/dev/null || true
    chmod 640 "$APP_DIR/dashboard.secret" 2>/dev/null || true
  fi
  local f
  # The env-overridden mirror paths expand to "" when unset — the -e guard
  # skips those and any file that does not exist yet.
  for f in "$DB_PATH" "$DB_PATH-wal" "$DB_PATH-shm" "$APP_DIR/last_sync.json" \
           "$APP_DIR/mirror_state.json" "$APP_DIR/smart_locker_catalog.xlsx" \
           "${SMART_LOCKER_MIRROR_STATE_PATH:-}" "${SMART_LOCKER_MIRROR_PATH:-}"; do
    if [ -e "$f" ]; then
      chown "$APP_USER":"$APP_USER" "$f" 2>/dev/null || true
    fi
  done
  chown -R "$APP_USER":"$APP_USER" "$APP_DIR/smart_locker/frontend/images" 2>/dev/null || true
}

# Re-render the systemd unit from the tree now in place. An update that only
# swaps $APP_DIR would otherwise leave the installed unit untouched forever —
# e.g. this tree drops NoNewPrivileges, which had blocked every `sudo -n`
# call (update, stop, poweroff) on the appliance. Missing/privileged paths
# warn instead of failing the whole update — a stale unit only loses the
# new unit flags, not the release.
refresh_service_unit() {
  local src="$APP_DIR/deploy/systemd/smart-locker.service"
  local dst="${SMART_LOCKER_UNIT_DST:-/etc/systemd/system/smart-locker.service}"
  [ -f "$src" ] || return 0
  local grp
  grp="$(id -gn "$APP_USER" 2>/dev/null || echo "$APP_USER")"
  if sed \
    -e "s#^User=.*#User=$APP_USER#" \
    -e "s#^Group=.*#Group=$grp#" \
    -e "s#^WorkingDirectory=.*#WorkingDirectory=$APP_DIR#" \
    -e "s#^ExecStart=.*#ExecStart=$VENV_DIR/bin/python -m smart_locker.app#" \
    -e "s#^Documentation=.*#Documentation=file://$APP_DIR/GUIDE.md#" \
    "$src" > "$dst"; then
    systemctl daemon-reload 2>/dev/null || true
    log "Refreshed systemd unit at $dst."
  else
    log "WARNING: could not refresh $dst — unit-level changes need install.sh."
  fi
}

# --- Rollback (restore the pre-update code + DB, restart on the old version) -
rollback() {
  # Only the top-level shell restores: under `set -E` the ERR trap is
  # inherited by $(...) and ( ... ) subshells, so a restore must never run
  # inside a substitution and then again in the parent.
  if [ "${BASH_SUBSHELL:-0}" -gt 0 ]; then
    return 1
  fi
  # A second restore on top of a completed one is a no-op.
  if [ "$ROLLBACK_RAN" = "1" ]; then
    return 0
  fi
  ROLLBACK_RAN=1
  log "ROLLBACK: restoring previous version."
  # One rollback only: a failure inside the restore must not re-enter on_err
  # and start a second restore on top of the first. The restore itself is
  # best-effort (set +e): one bad step must not skip the DB restore, the
  # service restart, or the status write — that would leave the box stopped
  # with a stale "updating" status forever.
  trap - ERR
  set +e
  rm -rf "$BACKUP_DIR"/.restore-* 2>/dev/null
  sudo systemctl stop "$SERVICE" 2>/dev/null || true
  if [ -n "$CODE_BACKUP" ] && [ -f "$CODE_BACKUP" ]; then
    # tar-over-APP_DIR would only overwrite: files the failed version added
    # (and only it has) would survive, leaving a mixed old/new tree. Extract
    # the snapshot aside and rsync --delete it back so the code tree is
    # exactly what it was. BACKUP_SKIP keeps .env, the DB, logs, backups,
    # wheelhouse and locker-updates untouched — the venv and the images dir
    # ARE restored so the old code gets its own dependencies and UI assets.
    local restore_dir="$BACKUP_DIR/.restore-$STAMP"
    rm -rf "$restore_dir"
    mkdir -p "$restore_dir"
    tar -xzf "$CODE_BACKUP" -C "$restore_dir"
    # --delete only onto a snapshot that still looks like a tree — a corrupt
    # or truncated backup must not wipe the current code into nothing.
    if is_repo_tree "$restore_dir"; then
      local restore_excludes=()
      for p in "${BACKUP_SKIP[@]}"; do restore_excludes+=( --exclude="/$p" ); done
      rsync -a --delete "${restore_excludes[@]}" "$restore_dir"/ "$APP_DIR"/
      log "Restored code from $CODE_BACKUP"
    else
      log "WARNING: $CODE_BACKUP did not extract to a valid tree — code left as-is."
    fi
    rm -rf "$restore_dir"
  fi
  if [ -n "$DB_BACKUP" ] && [ -f "$DB_BACKUP" ]; then
    rm -f "$DB_PATH-wal" "$DB_PATH-shm"
    cp -f "$DB_BACKUP" "$DB_PATH"
    log "Restored DB from $DB_BACKUP"
  fi
  if [ -n "$OLD_VERSION" ]; then
    printf '%s\n' "$OLD_VERSION" > "$VERSION_FILE"
  fi
  # The restore left the tree root:root again — re-apply the same runtime
  # ownership the swap performs, or the service account cannot read .env or
  # write logs/, the DB, or device photos. The unit is re-rendered too so a
  # rollback restores the unit the old code shipped with.
  apply_runtime_permissions
  refresh_service_unit
  sudo systemctl start "$SERVICE" 2>/dev/null || true
  if wait_for_health; then
    log "Rollback healthy — running previous version ${OLD_VERSION:-?}."
    write_status "rolled_back" "Update failed; reverted to previous version and recovered." "$OLD_VERSION"
  else
    log "WARNING: service did not report healthy after rollback — check 'journalctl -u $SERVICE'."
    write_status "rollback_unhealthy" "Update failed and the service is not healthy after rollback — manual check needed." "$OLD_VERSION"
  fi
}

on_err() {
  local line="$1"
  # Never re-enter: a failure inside this handler (or inside rollback) must
  # not fire the ERR trap again and stack another restore on top.
  trap - ERR
  # The ERR trap is inherited by $(...) and ( ... ) subshells under set -E:
  # this handler would fire there AND again in the parent. Only the
  # top-level shell does the work — a subshell exits nonzero so the
  # parent's own ERR trap runs the one real on_err.
  if [ "${BASH_SUBSHELL:-0}" -gt 0 ]; then
    exit 1
  fi
  log "ERROR on line $line."
  if [ "$BACKED_UP" = "1" ]; then
    rollback
  else
    write_status "failed" "Update aborted before any change was applied (line $line)."
  fi
  rm -rf "$STAGING_DIR"
  exit 1
}

# --- Incoming tree discovery ---------------------------------------------------
is_repo_tree() {
  local d="${1:-}"
  [ -n "$d" ] && [ -d "$d" ] \
    && [ -f "$d/smart_locker/app.py" ] \
    && [ -f "$d/requirements.txt" ] \
    && [ -f "$d/deploy/install/update.sh" ]
}

# Look for a repo root at dir, dir/smart_locker, dir/locker-updates/..., or a child dir.
find_tree_under() {
  local root="${1:-}" cand
  [ -n "$root" ] && [ -d "$root" ] || return 1
  if is_repo_tree "$root"; then
    printf '%s\n' "$root"
    return 0
  fi
  if is_repo_tree "$root/smart_locker"; then
    printf '%s\n' "$root/smart_locker"
    return 0
  fi
  if [ -d "$root/locker-updates" ]; then
    if is_repo_tree "$root/locker-updates"; then
      printf '%s\n' "$root/locker-updates"
      return 0
    fi
    if is_repo_tree "$root/locker-updates/smart_locker"; then
      printf '%s\n' "$root/locker-updates/smart_locker"
      return 0
    fi
    cand="$(_newest_child_tree "$root/locker-updates" || true)"
    if [ -n "$cand" ]; then
      printf '%s\n' "$cand"
      return 0
    fi
  fi
  cand="$(_newest_child_tree "$root" || true)"
  if [ -n "$cand" ]; then
    printf '%s\n' "$cand"
    return 0
  fi
  return 1
}

_newest_child_tree() {
  local parent="${1:-}" cand best="" best_m=0 m
  [ -n "$parent" ] && [ -d "$parent" ] || return 1
  local _old_nullglob
  _old_nullglob="$(shopt -p nullglob || true)"
  shopt -s nullglob
  for cand in "$parent"/*; do
    [ -d "$cand" ] || continue
    is_repo_tree "$cand" || continue
    m="$(_tree_mtime "$cand")"
    if [ "$m" -ge "$best_m" ]; then
      best_m="$m"
      best="$cand"
    fi
  done
  eval "$_old_nullglob"
  if [ -n "$best" ]; then
    printf '%s\n' "$best"
    return 0
  fi
  return 1
}

_tree_mtime() {
  local d="$1"
  stat -c '%Y' "$d/requirements.txt" 2>/dev/null || echo 0
}

# Newest USB locker-updates tree under USB_MEDIA_ROOT (/media/*/* and /media/*).
find_usb_tree() {
  local root="${1:-$USB_MEDIA_ROOT}"
  [ -d "$root" ] || return 1
  local cand tree best="" best_m=0 m
  local _old_nullglob
  _old_nullglob="$(shopt -p nullglob || true)"
  shopt -s nullglob
  for cand in "$root"/*/* "$root"/*; do
    [ -d "$cand" ] || continue
    tree=""
    if [ -d "$cand/locker-updates" ]; then
      tree="$(find_tree_under "$cand/locker-updates" || true)"
    fi
    if [ -z "$tree" ] && [ "$(basename "$cand")" = "locker-updates" ]; then
      tree="$(find_tree_under "$cand" || true)"
    fi
    [ -n "$tree" ] || continue
    m="$(_tree_mtime "$tree")"
    if [ "$m" -ge "$best_m" ]; then
      best_m="$m"
      best="$tree"
    fi
  done
  eval "$_old_nullglob"
  if [ -n "$best" ]; then
    printf '%s\n' "$best"
    return 0
  fi
  return 1
}

# Search order: USB locker-updates → local $APP_DIR/locker-updates.
# Prints: TREE<TAB>path
discover_update_source() {
  local tree
  tree="$(find_usb_tree "$USB_MEDIA_ROOT" || true)"
  if [ -n "$tree" ]; then
    printf 'TREE\t%s\n' "$tree"
    return 0
  fi
  tree="$(find_tree_under "$LOCAL_UPDATES" || true)"
  if [ -n "$tree" ]; then
    printf 'TREE\t%s\n' "$tree"
    return 0
  fi
  return 1
}

tree_version() {
  local d="${1:-}" ver
  [ -n "$d" ] && [ -d "$d" ] || return 1
  if [ -f "$d/VERSION" ]; then
    ver="$(tr -d '[:space:]' < "$d/VERSION")"
    if [ -n "$ver" ]; then
      printf '%s\n' "$ver"
      return 0
    fi
  fi
  if [ -d "$d/.git" ] && command -v git >/dev/null 2>&1; then
    ver="$(git -C "$d" describe --tags --always 2>/dev/null || true)"
    ver="${ver//\//-}"
    ver="$(printf '%s' "$ver" | tr -s '[:space:]' '-')"
    if [ -n "$ver" ]; then
      printf '%s\n' "$ver"
      return 0
    fi
  fi
  printf 'mtime-%s\n' "$(_tree_mtime "$d")"
}

# Return 0 if incoming should be refused as older than current.
# Equal versions are not older (caller treats those as up_to_date).
# Hex-only ids (git hashes) are not ordered; only equality is used there.
version_is_older() {
  local incoming="${1:-}" current="${2:-}"
  [ -n "$incoming" ] && [ -n "$current" ] || return 1
  [ "$incoming" = "$current" ] && return 1
  [ "$current" = "0" ] && return 1
  if [[ "$incoming" =~ ^mtime- ]] || [[ "$current" =~ ^mtime- ]]; then
    if [[ "$incoming" =~ ^mtime-([0-9]+)$ ]]; then
      local in_m="${BASH_REMATCH[1]}"
      if [[ "$current" =~ ^mtime-([0-9]+)$ ]]; then
        [ "$in_m" -lt "${BASH_REMATCH[1]}" ]
        return
      fi
    fi
    return 1
  fi
  if [[ "$incoming" =~ ^[0-9a-f]{7,}$ ]] && [[ "$current" =~ ^[0-9a-f]{7,}$ ]]; then
    return 1
  fi
  local first
  first="$(printf '%s\n%s\n' "$incoming" "$current" | sort -V | head -n1)"
  [ "$first" = "$incoming" ]
}

copy_tree_with_skip() {
  local src="${1:-}" dest="${2:-}"
  shift 2
  [ -n "$src" ] && [ -d "$src" ] || return 1
  [ -n "$dest" ] || return 1
  mkdir -p "$dest"
  local excludes=() p
  for p in "$@"; do
    excludes+=( --exclude="/$p" --exclude="/$p/" )
  done
  excludes+=( --exclude='*.db' )
  rsync -a "${excludes[@]}" "$src"/ "$dest"/
}

copy_incoming_tree() {
  copy_tree_with_skip "$1" "$2" "${INCOMING_SKIP[@]}"
}

copy_payload_tree() {
  copy_tree_with_skip "$1" "$2" "${PAYLOAD_SKIP[@]}"
}

copy_new_wheels_from_incoming() {
  local src="${1:-}"
  local src_wh="$src/deploy/wheelhouse"
  [ -d "$src_wh" ] || return 1
  mkdir -p "$WHEELHOUSE"
  local copied=0 f base
  local _old_nullglob
  _old_nullglob="$(shopt -p nullglob || true)"
  shopt -s nullglob
  for f in "$src_wh"/*.whl; do
    [ -f "$f" ] || continue
    base="$(basename "$f")"
    if [ ! -f "$WHEELHOUSE/$base" ]; then
      cp -f "$f" "$WHEELHOUSE/$base"
      copied=$((copied + 1))
      log "Copied new wheel $base into the Pi wheelhouse."
    fi
  done
  eval "$_old_nullglob"
  [ "$copied" -gt 0 ]
}

# --- Sourced by tests: stop before mutating the live appliance --------------
if [ "${SMART_LOCKER_UPDATE_LIB:-}" = "1" ]; then
  return 0 2>/dev/null || exit 0
fi

trap 'on_err $LINENO' ERR

# ============================================================================
# 1. Preconditions
# ============================================================================
STAMP="$(date '+%Y%m%d-%H%M%S')"
[ -x "$PY" ] || { echo "venv python not found at $PY — run install.sh first." >&2; exit 2; }
CUR_VERSION="$( [ -f "$VERSION_FILE" ] && tr -d '[:space:]' < "$VERSION_FILE" || echo "0" )"
[ -n "$CUR_VERSION" ] || CUR_VERSION="0"
OLD_VERSION="$CUR_VERSION"

log "=== Smart Locker update check (current version: $CUR_VERSION) ==="
write_status "checking" "Looking for locker-updates/ (USB, then $APP_DIR/locker-updates)."

# Fail closed before stop/backup: the USB/local copy and the code swap need rsync.
if ! command -v rsync >/dev/null 2>&1; then
  log "rsync not found on PATH — refusing to update (needed to copy the incoming tree off USB and swap it in). Service was NOT stopped; still running $CUR_VERSION."
  write_status "failed" "rsync not found; update refused; still on $CUR_VERSION."
  exit 1
fi

FOUND="$(discover_update_source || true)"
if [ -z "$FOUND" ]; then
  log "No locker-updates tree on USB or $LOCAL_UPDATES — nothing to do."
  write_status "idle" "No locker-updates tree found; staying on $CUR_VERSION."
  exit 0
fi
SOURCE_KIND="${FOUND%%$'\t'*}"
SOURCE_PATH="${FOUND#*$'\t'}"

# ============================================================================
# 2. Identify the incoming version and refuse same/older
# ============================================================================
NEW_VERSION="$(tree_version "$SOURCE_PATH")"

if [ "$NEW_VERSION" = "$CUR_VERSION" ]; then
  log "Already on this tree ($CUR_VERSION) — nothing to do."
  write_status "up_to_date" "Already running this version ($CUR_VERSION)."
  exit 0
fi
if version_is_older "$NEW_VERSION" "$CUR_VERSION"; then
  log "Incoming tree $NEW_VERSION is older than running $CUR_VERSION — refusing."
  write_status "failed" "Incoming tree $NEW_VERSION is older than $CUR_VERSION; update refused."
  exit 1
fi
log "New release available: $CUR_VERSION -> $NEW_VERSION ($SOURCE_KIND $SOURCE_PATH)"
write_status "updating" "Copying $NEW_VERSION onto local disk."

# USB → $APP_DIR/locker-updates so the stick can be unplugged.
_src_abs="$(cd "$SOURCE_PATH" && pwd)"
mkdir -p "$LOCAL_UPDATES"
_local_abs="$(cd "$LOCAL_UPDATES" && pwd)"
case "$_src_abs" in
  "$_local_abs"|"$_local_abs"/*)
    # Already under $LOCAL_UPDATES (a nested payload tree). rm -rf'ing
    # $LOCAL_UPDATES here would destroy SOURCE_PATH before it is copied —
    # a descendant source never triggers the staging copy.
    ;;
  *)
    log "Copying incoming tree to $LOCAL_UPDATES (USB can be unplugged after this)."
    rm -rf "$LOCAL_UPDATES"
    copy_payload_tree "$SOURCE_PATH" "$LOCAL_UPDATES"
    SOURCE_PATH="$LOCAL_UPDATES"
    log "Incoming tree is at $LOCAL_UPDATES."
    ;;
esac

# ============================================================================
# 3. Stage onto local disk (USB can be unplugged after this)
# ============================================================================
rm -rf "$STAGING_DIR"; mkdir -p "$STAGING_DIR"

log "Copying incoming tree from $SOURCE_PATH to $STAGING_DIR (skipping .env, databases, venv, logs, backups, wheelhouse)..."
copy_incoming_tree "$SOURCE_PATH" "$STAGING_DIR"
log "Incoming tree staged at $STAGING_DIR."
[ -f "$STAGING_DIR/requirements.txt" ] || { log "Staged tree looks invalid (no requirements.txt) — aborting."; write_status "failed" "Incoming tree is invalid."; rm -rf "$STAGING_DIR"; exit 1; }
write_status "updating" "Applying $NEW_VERSION."

# Copy extra wheels the Pi does not already have. Do not refuse if some are still
# missing — python -m scripts.copy_update warned on Windows; pip failure rolls back.
REQS_FOR_UPDATE="$(mktemp)"
grep -vi '^pyscard' "$STAGING_DIR/requirements.txt" > "$REQS_FOR_UPDATE"
trap 'rm -f "$REQS_FOR_UPDATE"' EXIT
copy_new_wheels_from_incoming "$SOURCE_PATH" || true
write_status "updating" "Applying $NEW_VERSION. USB stick can be unplugged."

# ============================================================================
# 4. Backup (the rollback point) — from here on, failure triggers rollback
# ============================================================================
CODE_BACKUP="$BACKUP_DIR/code-$STAMP.tar.gz"
TAR_EXCLUDES=()
for p in "${BACKUP_SKIP[@]}"; do TAR_EXCLUDES+=( --exclude="./$p" ); done
# No subshell: under `set -E` the ERR trap also fires inside ( ... ) and then
# again in the parent when the subshell exits nonzero — a backup/migrate
# failure would run on_err (and rollback) twice.
# --warning=no-file-changed: the service still runs while this snapshot is
# taken, so a __pycache__ rewrite mid-tar must not abort a healthy update.
tar --warning=no-file-changed --exclude='*/__pycache__' \
  -czf "$CODE_BACKUP" "${TAR_EXCLUDES[@]}" -C "$APP_DIR" .
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
# PRESERVE skipped this dir so gitignored device photos survive --delete.
# Overlay committed UI assets from the staged release without removing photos.
if [ -d "$STAGING_DIR/smart_locker/frontend/images" ]; then
  mkdir -p "$APP_DIR/smart_locker/frontend/images"
  rsync -a "$STAGING_DIR/smart_locker/frontend/images/" "$APP_DIR/smart_locker/frontend/images/"
fi
apply_runtime_permissions
refresh_service_unit
log "New code in place."

# ============================================================================
# 6. Dependencies (offline wheelhouse) + DB migrations
# ============================================================================
# Installing from the Pi wheelhouse. On failure, ERR trap → rollback.
log "Installing dependencies from offline wheelhouse..."
"$VENV_DIR/bin/pip" install --ignore-installed --no-index --find-links "$WHEELHOUSE" -r "$REQS_FOR_UPDATE" >>"$LOG_FILE" 2>&1

log "Running database migrations..."
# The documented entry point run from the app root — a plain `cd` in this
# shell, not a `( cd && ... )` subshell, so the ERR trap fires once and the
# script's internal sys.path shim is not depended on. Everything past this
# point already uses absolute paths.
cd "$APP_DIR"
"$PY" -m scripts.migrate_db >>"$LOG_FILE" 2>&1

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
  # Prune old backups, keep the most recent KEEP_BACKUPS of each kind. A prune
  # hiccup must not fail a run that already wrote "success".
  ls -1t "$BACKUP_DIR"/code-*.tar.gz 2>/dev/null | tail -n +"$((KEEP_BACKUPS+1))" | xargs -r rm -f || true
  ls -1t "$BACKUP_DIR"/db-*.sqlite   2>/dev/null | tail -n +"$((KEEP_BACKUPS+1))" | xargs -r rm -f || true
  rm -rf "$BACKUP_DIR"/.restore-* 2>/dev/null || true
  exit 0
else
  log "New version did NOT become healthy within ${HEALTH_TIMEOUT}s — rolling back."
  rollback
  rm -rf "$STAGING_DIR"
  exit 1
fi
