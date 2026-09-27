# Changelog

Keep user-facing bullets. Internal refactors stay out unless they change how someone operates the device or app.

## [Unreleased]

Work on `main` after the `v0.2.0` tag. Merging to `main` is not a release; the next ship gets a `vX.Y.Z` tag.

### Added

- `POST /api/admin/exit-kiosk` is kept as an alias of Stop system so cached older kiosk pages still work after an update.
- SQLite is now the device catalog of record: every device is a `devices` row and `locker_slot` marks the cabinet units. Dashboard Inventory and Locker both read the database — a down share no longer breaks either tab.
- The catalog sheet on the share is now a hidden, Pi-written **mirror** (`SMART_LOCKER_MIRROR_PATH`, default `smart_locker_catalog.xlsx` next to the DB). The Pi rewrites it after every catalog change; a locked or missing file defers the write and the tick retries — the kiosk never waits on workbook I/O. An existing sheet is adopted into SQLite on first sight.
- Hand edits to the mirror are detected and held for review: the dashboard shows a banner plus a diff list, and an admin chooses **Apply** (sheet → SQLite) or **Keep database** (the next write overwrites the sheet). Nothing is merged silently.
- Dashboard **Admin** button in the header (secret-prompted): catalog editor (add / edit / remove device), mirror diff review, users, transactions, and NFC bind/unbind. The dashboard 5-tap is gone; the kiosk's own 5-tap stays.
- Owner change on a **non-locker** device from dashboard Inventory is now public — no admin password. Location on a locker unit is derived from borrow state and cannot be edited anywhere.
- Register Device promotes a registerable catalog row (no slot, place = in-locker word) into a free slot; the kiosk list offers the registerable rows.
- Mirror status/diff endpoints under `/api/dashboard/mirror*` and `GET /api/admin/devices/registerable`; `POST /api/admin/sync-source` now runs one mirror tick.
- Calibration gate: on the due date and after, a unit cannot start a new loan — borrow and handover are refused and the refusal says why; return always works. Before the due date the kiosk card and dashboard cells show a due-soon badge (`SMART_LOCKER_CALIBRATION_WARN_DAYS`, default 14). Device feeds carry `calibration_state`/`calibration_days_left`.

### Removed

- Excel export (`GET /api/admin/export-excel`), `SMART_LOCKER_EXCEL_AUTO_EXPORT`, the `smart_locker_data.xlsx` export workbook, and the 6-hour source-import interval — replaced by the mirror tick (`SMART_LOCKER_MIRROR_SYNC_SECONDS`).

### Fixed

- First-admin Setup on the installed Pi: the dashboard password no longer writes to root-owned `.env` (which failed every time under the hardened install). It now lands in the service-owned `dashboard.secret` file (mode `640`), survives updates, and is picked up on the next boot.
- `POST /api/setup` is loopback-only: a LAN caller can no longer plant a dashboard password or occupy the physical card window the kiosk needs.
- `POST /api/admin/update` no longer has an unauthenticated LAN first-boot path; only the kiosk itself can run it before an admin/secret exists.
- **Stop system** now still stops the service if closing the kiosk browser fails, and first stops an in-flight `smart-locker-update` unit so it cannot restart the box.
- The systemd unit no longer sets `NoNewPrivileges=true`, which had made every `sudo -n` call (update, stop, poweroff) fail on the appliance.
- Setup refuses blank/whitespace admin names, a missing dashboard password while no secret exists, and arming while the NFC reader is down.

### Changed

- First-boot Setup now **requires** a dashboard password (a blank one would have left every admin-gated dashboard route permanently 401 with no UI path to set it). The kiosk Setup screen starts its countdown only after the backend confirms the arm, and the dashboard shows guided kiosk-side steps instead of arming Setup remotely.

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
