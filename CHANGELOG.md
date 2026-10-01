# Changelog

Keep user-facing bullets. Internal refactors stay out unless they change how someone operates the device or app.

## [Unreleased]

Work on `main` after the `v0.2.0` tag. Merging to `main` is not a release; the next ship gets a `vX.Y.Z` tag.

### Added

- Calibration gate: on the due date and after, a unit cannot start a new loan — borrow and handover are refused and the refusal says why; return always works. Before the due date the kiosk card and dashboard cells show a due-soon badge (`SMART_LOCKER_CALIBRATION_WARN_DAYS`, default 14). Device feeds carry `calibration_state`/`calibration_days_left`.
- Locker slots are shared: several devices may sit in the same slot, and an occupied slot stays selectable on the Register Device / change-slot pickers. Existing databases keep their rows — `scripts/migrate_db.py` rebuilds the old unique slot index as non-unique.
- On-screen keyboard on the kiosk for text fields (register search, admin name, bind search). `keyboard.js`; kiosk inputs use `inputmode="none"`.
- Admin **Stop system** closes Chromium then stops `smart-locker` via sudoers (`systemctl stop`), leaving the Pi powered on. `/api/admin/exit-kiosk` is kept as an alias for cached pages.

### Fixed

### Changed

- Dashboard is fully public: the `SMART_LOCKER_DASHBOARD_ADMIN_SECRET` / `X-Smart-Locker-Admin` header is removed. Every `/api/dashboard/*` endpoint (catalog GETs, owner POST, bind/unbind, user rename, users/transactions/owners GETs) answers without a secret; bind/unbind keep their reader-conflict and kiosk-session checks. A 5-tap clock gesture (8 s window) opens the overlay; each user row has a **Rename** action (`POST /api/dashboard/users/{id}/name`, name only) plus a role `select` + **Save** per row (`POST /api/dashboard/users/{id}/role`, `admin`/`user`; last active admin demotion is 409, and an actual change ends that user's active kiosk session). A per-row **Remove** soft-disables a user (`POST /api/dashboard/users/{id}/remove` — keeps rows/history; 409 on last active admin or outstanding borrowed units) and **Add user** arms a 60 s physical-card enrollment window (`POST /api/dashboard/users/register` + token status/cancel). Renames update kiosk borrower labels and the borrowed unit's Excel Location on the next write-back.

- Dashboard share launcher now writes only `dashboard.url`; the generated `dashboard.html` redirect is removed because the Windows shortcut is enough to open the live page.
- Pi **Software Update** is only `python -m scripts.copy_update` → gitignored `locker-updates/` on a USB stick (or already at `$APP_DIR/locker-updates`). `update.sh` copies USB `locker-updates` into `$APP_DIR/locker-updates`, then stop / backup / rsync-preserve / pip / migrate / health / rollback. Missing wheels are warned at copy time; pip failure on the Pi rolls back. The signed `pack_release` tarball + HMAC sidecar and CIFS `SMART_LOCKER_UPDATE_DIR` drop are removed.

## [0.2.0] — 2026-08-28

Second tagged appliance ship (`v0.2.0`). NFC stickers, Location write-back, dashboard tabs, and house bootstrap (changelog, ADRs, MIT, pytest CI).

### Added

- NFC stickers on locker devices use the same ACR1252U as work cards: tap a work card, then tap the sticker (or pick on screen) to borrow or return.
- Idle tap of a **borrowed** sticker returns it without a work card and shows the slot overlay. Available stickers do not borrow from idle.
- Register Device in the hidden admin panel: PM number + free slot + NFC. Catalog comes from Excel; Sync never inserts locker rows. Tagged rows say **Replace tag**.
- After borrow/return, Register Device, and each source import, the Pi writes **only** the Location column by PM (`Locker` when in, borrower name when out).
- Dashboard tabs: **Inventory** (live Excel), **Locker** (SQLite, Tagged / No tag), **Display** (what the kiosk shows). Share launcher writes `dashboard.html` and `dashboard.url` when configured.
- Locker availability overlay with PM numbers on kiosk cards.
- Admin **Exit kiosk** (closes Chromium, service stays) and **Shut down** (`systemctl poweroff` via sudoers).
- Signed `pack_release` tarball + HMAC sidecar for Pi updates from the locker share.
- MIT license (`LICENSE`).
- Keep a Changelog, architecture decision records (ADRs 0001–0009), and git Issue/MR templates.
- CI CI runs `python -m pytest tests/ -v` on Python 3.11 and 3.13 via `requirements.txt`.

### Fixed

- Excel re-import no longer wipes locker occupancy or tag bindings from missing catalog fields.
- Register User no longer leaves a leftover admin session; the next work-card tap logs in.
- Kiosk session and NFC stay on loopback; LAN browsers use the dashboard, not the Riverdi session.

### Changed

- Dashboard owner edit is for **non-locker** PMs on Inventory only. The Pi owns Location for locker devices.
- Dashboard mutations and gated users/transactions/owners GETs need `SMART_LOCKER_DASHBOARD_ADMIN_SECRET` (header `X-Smart-Locker-Admin`); 401 if unset (fail closed). The 5-tap clock overlay is a UI reveal, not authorization.
- Source Excel import runs at startup and every 6 hours (last-sync persisted). Re-import never overwrites locker slot, image, description, tag binding, status, or current borrower.
- Person names in Excel Location replace the self-register list; names that leave Location are removed.
- `devices.barcode` is unused leftover and is no longer imported or shown.
- Offline Pi kit hardened for Raspberry Pi OS trixie (Python 3.13 / cp313 wheelhouse, pcscd polkit, no-apt install).
- README and PROJECT-NOTES.md record the house playbook, language pack, status, and license. GUIDE.md stays the operator guide.

## [0.1.0] — 2026-08-20

First tagged appliance ship (`v0.1.0`). Tap an NFC work card, then borrow or return equipment on the kiosk.

### Added

- Offline Raspberry Pi 4 kiosk: FastAPI + Chromium, ACR1252U work-card tap, touch UI for borrow/return.
- SQLite on the Pi SD card; device catalog Excel on the locker CIFS share.
- AES-256-GCM storage of card UIDs and HMAC-SHA256 lookup. UIDs are never written to logs.
- Hidden admin panel (tap the idle clock 5× within 3 s): Sync, Register User, Export Excel, End Session.
- Self-registration from an approved-name list.
- Offline install from USB (wheelhouse + `.deb`s), systemd service, CIFS mount, Chromium kiosk.
- Network dashboard at `/dashboard` for catalog visibility.
