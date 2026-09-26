#!/usr/bin/env bash
# start-kiosk.sh — launch the Smart Locker UI fullscreen in Chromium (kiosk mode).
# ----------------------------------------------------------------------------
# Waits for the backend to answer on http://localhost:8000, disables screen
# blanking, hides the cursor, then runs Chromium fullscreen. Auto-detects the
# Chromium binary (chromium vs chromium-browser) so it works on both current
# (Bookworm) and older (Bullseye/Buster) Raspberry Pi OS. Runs from the kiosk
# user's graphical session (started by the XDG autostart entry).
set -euo pipefail

URL="http://localhost:8000"

# 1. Find the Chromium binary.
CHROME=""
for candidate in chromium chromium-browser chromium-browser-stable; do
  if command -v "$candidate" >/dev/null 2>&1; then
    CHROME="$candidate"
    break
  fi
done
if [ -z "$CHROME" ]; then
  echo "start-kiosk: no Chromium binary found (tried chromium, chromium-browser)." >&2
  echo "             install it with: sudo apt install -y chromium" >&2
  exit 1
fi

# 2. Disable screen blanking / power management (X11). Harmless if xset is absent.
if command -v xset >/dev/null 2>&1; then
  xset s off || true
  xset s noblank || true
  xset -dpms || true
fi

# 3. Hide the mouse cursor when idle (optional; skipped if unclutter is not installed).
if command -v unclutter >/dev/null 2>&1; then
  unclutter -idle 0.5 -root &
fi

# 4. Wait (up to ~60s) for the backend to be ready before opening the browser.
echo "start-kiosk: waiting for $URL ..."
for _ in $(seq 1 60); do
  if curl -sf -o /dev/null "$URL"; then
    echo "start-kiosk: backend is up."
    break
  fi
  sleep 1
done

# 5. Launch Chromium in kiosk mode. A dedicated profile keeps it clean across reboots.
PROFILE_DIR="${HOME}/.config/smart-locker-kiosk"
mkdir -p "$PROFILE_DIR"

# Force the lite UI on the appliance. The Pi 4 GPU (VideoCore VI) cannot hold a
# smooth frame rate with the full blur/backdrop-filter effects regardless of RAM,
# and the in-page auto-detector only trips on <=4 GB devices — so an 8 GB Pi 4
# would otherwise run the heavy UI. ?lite persists per-profile via localStorage.
exec "$CHROME" \
  --kiosk \
  --user-data-dir="$PROFILE_DIR" \
  --password-store=basic \
  --noerrdialogs \
  --disable-infobars \
  --disable-session-crashed-bubble \
  --disable-features=TranslateUI \
  --disable-pinch \
  --overscroll-history-navigation=0 \
  --check-for-update-interval=31536000 \
  --no-first-run \
  "${URL}/?lite"
