# PROJECT-NOTES.md

## Project

- **Name:** smart_locker
- **Purpose (one sentence):** Offline Raspberry Pi kiosk: tap an NFC work card, then borrow or return equipment (NFC sticker on the same reader, or pick on screen); SQLite on the Pi, Excel on the locker file share.
- **Kind:** mixed — FastAPI service + vanilla kiosk UI + Raspberry Pi appliance (`deploy/`)

## Stack

- **Languages:** Python (backend, scripts, tests) + vanilla HTML/CSS/JS (kiosk and `/dashboard`)
- **Language pack(s) to follow:** python (TypeScript-React is not used)
- **Conventions:** Google docstrings, existing layout (`smart_locker/` package at repo root, `requirements.txt` not `pyproject.toml`)
- **Important versions:** Python 3.11+ on Windows/dev (floor); **Python 3.13 / cp313** on Raspberry Pi OS trixie (production pin, also `.python-version`). pytest 8. No Node runtime for the product.

## How to build / test / flash

```text
python -m venv venv
.\venv\Scripts\Activate
pip install -r requirements.txt
python -m scripts.generate_key
Copy-Item .env.example .env          # then paste ENC / HMAC keys
python -m scripts.init_db
python -m scripts.enroll_card --name "Name" --role admin
python -m smart_locker.app           # kiosk API + UI on :8000

python -m pytest tests/ -v           # ~594 items, no NFC hardware

# Pi (offline): copy tree + wheelhouse + .debs, then
sudo bash deploy/install/install.sh
# then .env from deploy/.env.pi.example, init_db, enroll admin, mount the locker share, start service
```

- **CI:** CI `.git/workflows/ci.yml` on merge requests and on push to `main`. Matrix Python 3.11 and 3.13: `pip install -r requirements.txt` then `python -m pytest tests/ -v`. No ruff. No `pip install -e .`. Pytest on the work branch remains the merge gate.
- **Manual verification:** real ACR1252U work-card tap, then device-sticker borrow/return, Riverdi touch screen, CIFS import/export. Checklist: `deploy/PI-VALIDATION-CHECKLIST.md`.

## Hardware risk

- **Applies?** yes — production is a Raspberry Pi 4 + ACR1252U USB NFC reader + Riverdi 10.1" HDMI/USB touch panel (panel has its own 7–14 V PSU). Official Pi 4 5 V / 3 A PSU; phone chargers under-voltage the board.
- **If yes:** do not flash random OS images over the appliance SD without a backup. Do not put the SQLite database on the CIFS mount. Do not enable `SMART_LOCKER_FAKE_READER` on the Pi. Recovery: `update.sh` auto-rollback, or re-run `install.sh` from USB + restore `backups/`. Never guess GPIO/pin numbers — this product is USB + HDMI, not a HAT.

## Do

- Follow the personal engineering playbook (work type → checklist). Full method: `D:\projects\guide` — start at `00-start-here.md`.
- Write tests from the stated acceptance / bug / current behavior only.
- Keep diffs on-Issue. New dependencies need an explicit why, pin, and license.
- Work on a short-lived branch from `main` (`feature/`, `fix/`, `refactor/`, `docs/`, `chore/`, `hotfix/`, `spike/`). MR into `main`. Delete the branch after merge.
- Keep `deploy/*.sh`, `*.service`, `*.desktop` LF (`.gitattributes`). Run Pi scripts as `sudo bash …`.
- Log card events as "Card inserted on \<reader\>" and sticker taps as "Device tag on \<reader\>" — never a raw UID.

## Don't

- Don't invent requirements, extra features, or extra files.
- Don't commit to `main` or create `develop`.
- Don't guess pin numbers, voltages, clocks, or fuse/lock bits.
- Don't add frameworks or services not in this file.
- Don't put SQLite on the CIFS share (WAL is unreliable there).
- Don't “fix” CIFS file-watchers with watchdog `PollingObserver`.
- Don't enable the fake NFC reader in production.
- Don't `source` `.env` from root scripts (`update.sh` parses KEY=VALUE on purpose).
- Don't run uvicorn with multiple workers (in-memory session + SSE + NFC).
- Don't reorganize `smart_locker/` into plugins until that work is an explicit session.

## Repo-specific

- **Secrets:** `.env` (gitignored). Two keys: `SMART_LOCKER_ENC_KEY` (AES-256-GCM), `SMART_LOCKER_HMAC_KEY` (HMAC-SHA256 card lookup).
- **Locker share (Pi: `/mnt/locker`):** Excel import, Excel export, and photos live at the share root (`deploy/.env.pi.example`). Share down ≠ kiosk down. Software updates are USB `locker-updates/` only — not a tarball drop on the share.
- **Excel import:** `smart_locker/sync/source_import.py` — catalog refresh for PMs already in SQLite (never inserts locker rows). English headers and aliases (case-insensitive); extra aliases via `SMART_LOCKER_ID_HEADERS` / `SMART_LOCKER_LOCATION_HEADERS`. Re-import never overwrites `locker_slot` / `image_path` / `description` / `tag_hmac` / `status` / `current_borrower_id`. Catalog fields (name, type, serial, manufacturer, model, calibration) still update. Person names in Location replace the self-register list (names that leave Excel are removed). `devices.barcode` is unused leftover (not imported). A Slot column is unused. Scheduler: startup + every 6 hours (`SMART_LOCKER_SOURCE_SYNC_INTERVAL_HOURS`) + admin Sync; last-sync persisted next to the DB.
- **Excel write-back:** `smart_locker/sync/location_writeback.py` — after borrow/return, Register Device, and each source import, the Pi writes **only** the Location column by PM. Available → `SMART_LOCKER_IN_LOCKER_TOKEN` (default `Locker`); borrowed → borrower name. Other columns/sheets stay. Locked or missing workbook: log + retry, never crash the kiosk. Do not edit Location in Excel or on the dashboard for locker devices (the Pi overwrites that cell from kiosk borrow/return). Dashboard Inventory owner edit is for **non-locker** PMs only.
- **Asset label:** `SMART_LOCKER_ASSET_LABEL` (default `PM number`) is the kiosk/dashboard noun. Storage and JSON stay `pm_number`. Public `GET /api/config` returns `{ "asset_label": ... }`.
- **NFC device tags:** same ACR1252U as work cards. Store `devices.tag_hmac` only (same HMAC key as `users.uid_hmac`). Kiosk `GET /api/devices` and dashboard Locker JSON may expose `has_tag: bool`, never the digest; Excel export is Tagged Yes/No. Idle tap of a **borrowed** sticker returns it (no work card; slot overlay); available tags do not borrow from idle.
- **Excel auto-export** only if `SMART_LOCKER_EXCEL_AUTO_EXPORT=1` (off in the Pi template; admin Export Excel stays).
- **Photos:** filename stem = device **model**. `scripts/update_device.py --auto` matches **PM number** — different scheme.
- **Pi updates:** `python -m scripts.copy_update` (optional `--dest D:\\`) writes gitignored
  `locker-updates/` and copies it onto a USB stick. Hidden-admin **Software Update** applies
  `$APP_DIR/locker-updates` (USB `locker-updates/` is copied there first). That is the only
  software-update path. Missing wheels are **warned** by `copy_update` (does not abort);
  pip failure on the Pi rolls back. Git reset is not a full update.
- **Hidden admin:** idle screen, tap the **clock 5× within 3 s**. **Register User** returns to idle after enroll (next work-card tap logs in; **Register as admin** switch on the manual step). **Users** lists active users: **Replace card** (60 s tap window, old card stops working) and **Deactivate** (refused for the last admin or a user still holding devices; deactivated names are blocked from Register User). **Register Device** is PM + free slot + NFC (catalog from Excel; Sync never inserts). List shows **name + PM**; tagged rows say **Replace tag**. CLI bind: `python -m scripts.enroll_device_tag --pm PM-001`. **Exit kiosk** closes Chromium (service stays); **Shut down** is `systemctl poweroff` (needs `/etc/sudoers.d/smart-locker` via `apply-sudoers.sh`). Dashboard (`http://<pi>:8000/dashboard`): public GET **Inventory** (live Excel) and **Locker** (SQLite; Tagged / No tag) stay unauthenticated. Owner POST and owners GET are public (Inventory, non-locker PMs only; owners list = token + registrant names only, skipping names that match a deactivated user). Bind/unbind and gated users/tx GETs need `SMART_LOCKER_DASHBOARD_ADMIN_SECRET` (header `X-Smart-Locker-Admin`); 401 if unset (fail closed). Same 5-tap on the dashboard clock reveals users, last 500 transactions, unbind / arm-bind in the client — it is not authorization (`overlay=true` is not auth). Share launcher: `SMART_LOCKER_PUBLIC_URL` + `SMART_LOCKER_DASHBOARD_SHARE_PATH` writes `dashboard.url`. Health: `/api/health`.
- **Entry / layout:** `smart_locker/app.py`, `config/`, `scripts/`, `deploy/`, `tests/`, `GUIDE.md`. Frontend: `smart_locker/frontend/`.
- **Logging:** `config/logging_config.py` → `logs/smart_locker.log` (5 MB × 5) + stdout INFO.
- **Style:** every Python/JS/CSS/HTML file has a `File:` / `Description:` / `Project:` / `Notes:` header. Python: Google docstrings. JS: JSDoc.
