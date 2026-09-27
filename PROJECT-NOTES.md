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

python -m pytest tests/ -v           # ~371 items, no NFC hardware

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
- **Locker share (Pi: `/mnt/locker`):** the mirror workbook and photos live at the share root (`deploy/.env.pi.example`). Share down ≠ kiosk down. Software updates are USB `locker-updates/` only — not a tarball drop on the share.
- **SQLite catalog:** `devices` holds every catalog device; `locker_slot IS NOT NULL` = physical locker unit, the rest are non-locker rows with editable `location`/owner. Dashboard Inventory and Locker both read SQLite. Register Device promotes a registerable catalog row (no slot, `location` = in-locker token) into a free slot. The dashboard editor adds/edits/removes catalog rows (removing a borrowed device and editing a locker unit's location are 409).
- **Workbook mirror:** `smart_locker/sync/mirror.py` — the Pi rewrites the whole sheet from `devices` on change (async worker, I/O timeout, atomic `WorkbookAdapter.write_sheet`). `SMART_LOCKER_MIRROR_PATH` wins, then legacy `SMART_LOCKER_SOURCE_EXCEL_PATH`, then `smart_locker_catalog.xlsx` next to the DB. First sight of an existing sheet adopts its rows. Hand edits are diffed against the last Pi-written baseline and listed on the dashboard — admin **apply** or **dismiss**, never a silent merge. Locked/missing file → `pending_writes` in `mirror_state.json`, retried by the tick (`SMART_LOCKER_MIRROR_SYNC_SECONDS`, min 5). Locker Location is derived (available → `SMART_LOCKER_IN_LOCKER_TOKEN`, borrowed → borrower name, maintenance → `SMART_LOCKER_MAINTENANCE_TOKEN`); non-locker rows carry their `location`. Dashboard Inventory owner edit is **public** for non-locker PMs.
- **Maintenance:** dashboard admin row actions — `POST /api/dashboard/devices/{pm}/maintenance` (cabinet units only; 409 while borrowed) and `POST .../back-in-service` (requires the new `calibration_due`; a stale date stores fine and the calibration gate keeps borrow closed). The maintenance word in a sheet Location cell does the same thing — `device_catalog.apply_place_word` covers hand-edit apply and adoption; there is no sheet path back to service (a cell cannot supply the date).
- **Asset label:** `SMART_LOCKER_ASSET_LABEL` (default `PM number`) is the kiosk/dashboard noun. Storage and JSON stay `pm_number`. Public `GET /api/config` returns `{ "asset_label": ... }`.
- **NFC device tags:** same ACR1252U as work cards. Store `devices.tag_hmac` only (same HMAC key as `users.uid_hmac`). Kiosk `GET /api/devices` and dashboard Locker JSON may expose `has_tag: bool`, never the digest. Idle tap of a **borrowed** sticker returns it (no work card; slot overlay); available tags do not borrow from idle.
- **Calibration:** `devices.calibration_due` gates new loans — due today or overdue refuses borrow **and** handover with the reason on the refusal message; return always works. `SMART_LOCKER_CALIBRATION_WARN_DAYS` (default 14) sets the due-soon badge window on kiosk cards and dashboard cells; `GET /api/devices`, `/api/dashboard/devices`, and `/api/dashboard/inventory` carry `calibration_state` (`ok`/`due_soon`/`due`/`overdue`/null) and `calibration_days_left`. Borrow/transfer return a `LoanOutcome` (truthy on success, `reason` on refusal) so every client prints the same "why".
- **Photos:** filename stem = device **model**. `scripts/update_device.py --auto` matches the **model** too (87V.jpg → every "87V" unit); `--pm`/`--batch` stay PM-keyed.
- **Pi updates:** `python -m scripts.copy_update` (optional `--dest D:\\`) writes gitignored
  `locker-updates/` and copies it onto a USB stick. Hidden-admin **Software Update** applies
  `$APP_DIR/locker-updates` (USB `locker-updates/` is copied there first). That is the only
  software-update path. Missing wheels are **warned** by `copy_update` (does not abort);
  pip failure on the Pi rolls back. Git reset is not a full update.
- **Hidden admin:** idle screen, tap the **clock 5× within 3 s**. With **no admin enrolled** the 5-tap opens **First Admin Setup** instead (name → required dashboard password → tap the card; `POST /api/setup` is **loopback-only** and arms a 60 s window that enrolls the card as `admin`; the typed password is written to service-owned `dashboard.secret`, mode `640` — `.env` stays root-owned because `update.sh` parses it as root; an env `SMART_LOCKER_DASHBOARD_ADMIN_SECRET` wins over the file). **Register User** returns to idle after enroll (next work-card tap logs in). **Register Device** promotes a registerable catalog row (no slot, `location` = in-locker token) into a free slot + NFC. List shows **name + PM**; tagged rows say **Replace tag**. CLI bind: `python -m scripts.enroll_device_tag --pm PM-001`. **Stop system** (`/api/admin/stop-system`, old name `/api/admin/exit-kiosk` still answered) closes Chromium, stops the in-flight `smart-locker-update` unit so it cannot restart the box, then `systemctl stop smart-locker` (stays stopped until the next boot — needs the sudoers drop-in); **Shut down** is `systemctl poweroff` (needs `/etc/sudoers.d/smart-locker` via `apply-sudoers.sh`). Software update is authorized by the dashboard secret header or a loopback admin session; first boot (no admin, no secret) allows it **on the kiosk only** — LAN always needs the secret. Dashboard (`http://<pi>:8000/dashboard`): public GET **Inventory** and **Locker** (both SQLite; Tagged / No tag) stay unauthenticated, and the **non-locker** owner POST is public. Catalog CRUD, mirror apply/dismiss, bind/unbind, and gated users/tx/owners GETs need `SMART_LOCKER_DASHBOARD_ADMIN_SECRET` (header `X-Smart-Locker-Admin`); 401 if unset (fail closed). The dashboard **Admin** button reveals the overlay (catalog editor, mirror diffs, users, last 500 transactions, unbind / arm-bind) — it is not authorization (`overlay=true` is not auth); while Setup is open the dashboard shows the kiosk-side steps instead. Share launcher: `SMART_LOCKER_PUBLIC_URL` + `SMART_LOCKER_DASHBOARD_SHARE_PATH` writes `dashboard.url`. Health: `/api/health`.
- **Entry / layout:** `smart_locker/app.py`, `config/`, `scripts/`, `deploy/`, `tests/`, `GUIDE.md`. Frontend: `smart_locker/frontend/`.
- **Logging:** `config/logging_config.py` → `logs/smart_locker.log` (5 MB × 5) + stdout INFO.
- **Style:** every Python/JS/CSS/HTML file has a `File:` / `Description:` / `Project:` / `Notes:` header. Python: Google docstrings. JS: JSDoc.
