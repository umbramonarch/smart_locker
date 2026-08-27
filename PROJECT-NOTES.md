# PROJECT-NOTES.md

Filled from `D:\projects\guide\templates\PROJECT-NOTES.md`. No placeholders.

---

# PROJECT-NOTES.md

## Project

- **Name:** smart_locker
- **Purpose (one sentence):** Offline Raspberry Pi kiosk: tap an NFC work card, then borrow or return equipment (NFC sticker on the same reader, or pick on screen); SQLite on the Pi, Excel on the locker file share.
- **Kind:** mixed — FastAPI service + vanilla kiosk UI + Raspberry Pi appliance (`deploy/`)

## Stack

- **Languages:** Python (backend, scripts, tests) + vanilla HTML/CSS/JS (kiosk and `/dashboard`)
- **Language pack(s) to follow:** python (`D:\projects\guide\language-packs\python.md`), with this repo’s existing layout (`smart_locker/` package at repo root, `requirements.txt` not `pyproject.toml`)
- **Important versions:** Python 3.11+ on Windows/dev; **Python 3.13 / cp313** on Raspberry Pi OS trixie. pytest 8. No Node runtime for the product.

## How to build / test / flash

```text
python -m venv venv
.\venv\Scripts\Activate
pip install -r requirements.txt
python -m scripts.generate_key
Copy-Item .env.example .env          # then paste ENC / HMAC / UPDATE_HMAC keys
python -m scripts.init_db
python -m scripts.enroll_card --name "Name" --role admin
python -m smart_locker.app           # kiosk API + UI on :8000

python -m pytest tests/ -v           # ~298 items, 22 files, no NFC hardware

# Pi (offline): copy tree + wheelhouse + .debs, then
sudo bash deploy/install/install.sh
# then .env from deploy/.env.pi.example, init_db, enroll admin, mount the locker share, start service
```

- **CI:** none in this repo. Pytest on the work branch is the merge gate for `main`.
- **Manual verification:** real ACR1252U work-card tap, then device-sticker borrow/return, Riverdi touch screen, CIFS import/export. Checklist: `deploy/PI-VALIDATION-CHECKLIST.md`.

## Hardware risk

- **Applies?** yes — production is a Raspberry Pi 4 + ACR1252U USB NFC reader + Riverdi 10.1" HDMI/USB touch panel (panel has its own 7–14 V PSU). Official Pi 4 5 V / 3 A PSU; phone chargers under-voltage the board.
- **If yes:** do not flash random OS images over the appliance SD without a backup. Do not put the SQLite database on the CIFS mount. Do not enable `SMART_LOCKER_FAKE_READER` on the Pi. Recovery: `update.sh` auto-rollback, or re-run `install.sh` from USB + restore `backups/`. Never guess GPIO/pin numbers — this product is USB + HDMI, not a HAT.

## Do

- Follow the personal engineering playbook (work type → checklist). Full method: `D:\projects\guide\00-start-here.md`
- Write tests from the human's stated acceptance / bug / current behavior only.
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

- **Secrets:** `.env` (gitignored). Three keys: `SMART_LOCKER_ENC_KEY` (AES-256-GCM), `SMART_LOCKER_HMAC_KEY` (HMAC-SHA256 card lookup), `SMART_LOCKER_UPDATE_HMAC_KEY` (release HMAC; openssl uses the env **string**, not decoded 32 bytes).
- **Locker share (Pi: `/mnt/locker`):** Excel import, Excel export, photos, and signed updates live at the share root (`deploy/.env.pi.example`). Share down ≠ kiosk down.
- **Excel import:** `smart_locker/sync/source_import.py` — catalog refresh for PMs already in SQLite (never inserts locker rows). DE/EN headers. Re-import never overwrites `locker_slot` / `image_path` / `description` / `tag_hmac` / `status` / `current_borrower_id`. Catalog fields (name, type, serial, manufacturer, model, calibration) still update. `devices.barcode` is unused leftover (not imported). Platz/Schrank unused. Scheduler: startup + every 6 hours (`SMART_LOCKER_SOURCE_SYNC_INTERVAL_HOURS`) + admin Sync; last-sync persisted next to the DB.
- **Excel write-back:** `smart_locker/sync/einsatzort_writeback.py` — after borrow/return, Register Device, and each source import, the Pi writes **only** Aktueller Einsatzort by PM. Available → `Schrank`; borrowed → borrower name. Other columns/sheets stay. Locked or missing workbook: log + retry, never crash the kiosk. Do not edit Aktueller Einsatzort in Excel for locker devices (the Pi overwrites that cell).
- **NFC device tags:** same ACR1252U as work cards. Store `devices.tag_hmac` only (same HMAC key as `users.uid_hmac`). Kiosk `GET /api/devices` may expose `has_tag: bool`, never the digest; dashboard JSON and Excel export omit `tag_hmac`. Idle tap of a **borrowed** sticker returns it (no work card; slot overlay); available tags do not borrow from idle.
- **Excel auto-export** only if `SMART_LOCKER_EXCEL_AUTO_EXPORT=1` (off in the Pi template; admin Export Excel stays).
- **Photos:** filename stem = device **model**. `scripts/update_device.py --auto` matches **PM number** — different scheme.
- **Pi updates:** signed tarball from `python -m scripts.pack_release` only (copy `.tar.gz` + `.hmac` to `/mnt/locker/locker-updates`). `update.sh` still does stop / backup / pip / migrate / health / rollback, then refreshes sudoers. Git reset is not a full update.
- **Hidden admin:** idle screen, tap the **clock 5× within 3 s**. **Register User** returns to idle after enroll (next work-card tap logs in). **Register Device** is PM + free slot + NFC (catalog from Excel; Sync never inserts). List shows **name + PM**. CLI bind: `python -m scripts.enroll_device_tag --pm PM-001`. **Exit kiosk** closes Chromium (service stays); **Shut down** is `systemctl poweroff` (needs `/etc/sudoers.d/smart-locker` via `apply-sudoers.sh`). Dashboard: `http://<pi>:8000/dashboard` (no login). Health: `/api/health`.
- **Entry / layout:** `smart_locker/app.py`, `config/`, `scripts/`, `deploy/`, `tests/`, `GUIDE.md`. Frontend: `smart_locker/frontend/`.
- **Logging:** `config/logging_config.py` → `logs/smart_locker.log` (5 MB × 5) + stdout INFO.
- **Style:** every Python/JS/CSS/HTML file has a `File:` / `Description:` / `Project:` / `Notes:` header. Python: Google docstrings. JS: JSDoc.
