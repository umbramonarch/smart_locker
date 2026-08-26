# Smart Locker System

Equipment borrowing/returning system using NFC work cards. Users tap their card on an ACR1252U NFC reader to authenticate, then borrow or return devices on a touch-display kiosk UI. All transactions are logged in a SQLite database. Card UIDs are encrypted (AES-256-GCM) and looked up by HMAC-SHA256 — only admins can view raw card UIDs.

## Requirements

- **Raspberry Pi 4** (2 GB+), 64-bit Raspberry Pi OS — production target. (Also runs on
  Windows/macOS for development.)
- A running **PC/SC** service: `pcscd` on Linux/Pi, the **Smart Card** service on Windows
- ACR1252U NFC reader (USB)
- Python 3.11+
- NFC cards (MIFARE Classic, Ultralight, NTAG, DESFire — any card type with a UID)
- For the appliance: a touch display, and a CIFS/SMB file share for the device Excel.
  No internet is needed at runtime. See **GUIDE.md** and **`deploy/`**.

## Git workflow

`main` is the releasable branch. Never commit product work directly on `main`. Every change
goes through a **short-lived branch** and a merge request. Delete the branch after merge.

Work branches (cut from `main`): `feature/`, `fix/`, `refactor/`, `docs/`, `chore/`,
`hotfix/`, `spike/`.

- **Tags** `vX.Y.Z` on the commit you actually shipped.
- **`hotfix/*`** from that tag (or from `main` if that tag *is* `main`). Minimal MR into
  `main` (and into `release/x.y` if that line is still supported), then tag a new patch.
- **`release/x.y`** only when vX.Y is in the field and you still need patches there while
  `main` has moved. Merge hotfix into `main` **and** into the living `release/x.y` if both
  exist.

Pytest is the merge gate for `main`. Run it on the work branch before opening the MR.

## Project Structure

```
smart_locker/
├── config/
│   ├── settings.py              # Central config (DB path, reader name, timeouts, Excel/photo paths)
│   └── logging_config.py        # Rotating file + console logging
├── smart_locker/
│   ├── app.py                   # Entry point: FastAPI + uvicorn web server + background NFC listener
│   ├── api/
│   │   ├── routes.py            # REST endpoints + SSE stream (session, devices, registration, admin, dashboard)
│   │   ├── server.py            # FastAPI app factory + static file serving
│   │   └── app_context.py       # Shared app state (session manager, SSE queue, pending registration)
│   ├── frontend/
│   │   ├── index.html           # Kiosk UI — 6 screens + overlays
│   │   ├── style.css            # kiosk theme (#009641 on #181d24)
│   │   ├── app.js               # Kiosk state machine, API calls, NFC-driven navigation
│   │   ├── dashboard.html       # Read-only network dashboard (served at /dashboard)
│   │   ├── dashboard.css        # Dashboard styling
│   │   ├── dashboard.js         # Dashboard data fetch + sort/filter, 30s auto-refresh
│   │   └── images/              # Device photos + hero background
│   ├── nfc/                     # NFC reader interface (pyscard + APDU)
│   │   ├── apdu.py              # APDU command definitions + response parsing
│   │   ├── card_observer.py     # Card insert/remove detection
│   │   ├── reader_observer.py   # Reader connect/disconnect detection
│   │   ├── reader.py            # High-level NFCReader class
│   │   └── exceptions.py        # NFC-specific exceptions
│   ├── auth/
│   │   ├── authenticator.py     # Card UID → user lookup via HMAC
│   │   ├── session_manager.py   # Single-user session lifecycle + inactivity timeout
│   │   └── tap_router.py        # Classify UID (work card / device tag / unknown); auto-intent
│   ├── security/
│   │   ├── encryption.py        # AES-256-GCM encrypt/decrypt
│   │   ├── hashing.py           # HMAC-SHA256 for card UID fingerprinting
│   │   └── key_manager.py       # Key loading from environment
│   ├── database/
│   │   ├── models.py            # ORM models: User, Registrant, Device, TransactionLog
│   │   ├── engine.py            # SQLAlchemy engine + session factory
│   │   └── repositories.py      # CRUD: User / Registrant / Device / Transaction repositories
│   ├── services/
│   │   ├── locker_service.py    # Borrow/return rules (per-user limit, admin overrides)
│   │   └── user_service.py      # User enrollment, public/admin views
│   └── sync/
│       ├── excel_sync.py        # On-demand / auto Excel export (Devices / Transactions / Users)
│       ├── source_import.py     # Import company device master list (schrank only, DE/EN headers)
│       ├── scheduler.py         # Source import: startup + 6h interval (+ file-watch on local FS)
│       ├── photo_watcher.py     # Auto-assign device photos by model number
│       └── fs_utils.py          # Detect network (CIFS/NFS) paths so watchers skip unreliable inotify
├── deploy/                      # Raspberry Pi provisioning: systemd, CIFS mount, kiosk, offline install
│   ├── install/                 # install.sh (one-shot setup) + build-wheelhouse.sh (offline wheels)
│   ├── systemd/                 # smart-locker.service (backend autostart)
│   ├── kiosk/                   # start-kiosk.sh + autostart .desktop (Chromium fullscreen)
│   ├── mount/                   # CIFS fstab snippet + credentials template
│   ├── system-packages/         # Offline .deb packages (pyscard — no aarch64 PyPI wheel)
│   └── .env.pi.example          # Pi environment template (share paths pre-filled)
├── scripts/
│   ├── generate_key.py          # Generate AES-256 + HMAC-SHA256 keys for .env
│   ├── init_db.py               # Create database tables
│   ├── migrate_db.py            # Add columns/tables to an existing DB (run after schema changes)
│   ├── enroll_card.py           # Enroll a new NFC card user (reader tap, or --uid HEX for no hardware)
│   ├── enroll_device_tag.py     # Bind an NFC sticker to an existing device (--pm, optional --uid / --force)
│   ├── import_devices.py        # Bulk device import from Excel (German + English headers)
│   ├── update_device.py         # Update device fields / match photos by PM number
│   ├── sync_source.py           # Manually trigger source Excel import
│   └── pack_release.py          # Pack a signed release (tracked-file snapshot + HMAC sidecar)
├── tests/                       # hardware-free pytest suite
├── requirements.txt
├── .env.example
├── PROJECT-NOTES.md                    # project rules
├── GUIDE.md                     # Step-by-step setup and usage guide
└── README.md
```

## Build Status

| Layer | Status | Notes |
|---|---|---|
| NFC reader (pyscard) | ✅ Done | Card insert/remove, UID reading, retry logic |
| Authentication | ✅ Done | HMAC lookup, single-user session lifecycle |
| Security (AES/HMAC) | ✅ Done | AES-256-GCM encryption, two-key management |
| Database & ORM | ✅ Done | SQLAlchemy models, repositories, extended device schema |
| Business logic | ✅ Done | Borrow/return rules, admin overrides, per-user borrow limit |
| FastAPI REST API | ✅ Done | Session, device, registration, admin, dashboard endpoints + SSE |
| Self-registration | ✅ Done | Approved-name list + NFC tap; admin manual registration |
| Excel export | ✅ Done | On-demand `.xlsx` (Devices + Transactions + Users) — replaces old auto-sync |
| Source import | ✅ Done | Startup + 6h interval + file-watch on local FS; schrank filter, DE/EN headers |
| Device import | ✅ Done | German + English Excel headers, PM-based dedup, schrank auto-numbering |
| Photo import | ✅ Done | By PM number (`update_device`) or by model (photo watcher) |
| Web dashboard | ✅ Done | Read-only `/dashboard` — devices, transactions, users; 30s auto-refresh |
| Frontend UI | ✅ Done | 6-screen kiosk UI + overlays |
| Unit tests | ✅ Done | ~234 tests across 16 modules, all hardware-free |
| NFC device tags | ✅ Done | Same ACR1252U; `devices.tag_hmac`; auto borrow/return after login |
| Calibration alerts | 🔲 Future | Calibration dates stored; notification system not yet built |
| Kiosk deployment | ✅ Done | Raspberry Pi appliance: systemd service, CIFS mount, Chromium kiosk, offline install (`deploy/`) |

## Quick Start

**Raspberry Pi appliance (production):** copy the repo onto the Pi, then
`sudo bash deploy/install/install.sh` sets up packages, the venv, the NFC daemon, the systemd
service, the CIFS mount, and the Chromium kiosk. Full walkthrough in **GUIDE.md**;
artifact reference in **`deploy/README.md`**.

**Development (any OS):**

```bash
# 1. Create a virtualenv and install dependencies
python -m venv venv && source venv/bin/activate   # Windows: .\venv\Scripts\Activate
pip install -r requirements.txt

# 2. Generate encryption keys
python -m scripts.generate_key

# 3. Create .env from the template, then paste the generated keys into it
cp .env.example .env                               # Windows: Copy-Item .env.example .env

# 4. Initialize the database
python -m scripts.init_db

# 5. Enroll an admin card
#    With an ACR1252U reader connected — tap the card when prompted:
python -m scripts.enroll_card --name "Your Name" --role admin
#    No hardware? Supply the UID directly (hex) — no reader needed:
python -m scripts.enroll_card --name "Your Name" --role admin --uid AABBCCDD

# 6. Run the system (web UI on http://localhost:8000)
python -m smart_locker.app
```

See **GUIDE.md** for detailed step-by-step instructions.

## System Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  Touch Display (Chromium kiosk)          Any browser on the network    │
│  ┌────────────────────────────┐         ┌───────────────────────────┐ │
│  │ Kiosk UI  (frontend/)      │         │ Dashboard  (/dashboard)   │ │
│  │ index.html · app.js        │         │ read-only · no auth       │ │
│  │ 6 screens · green theme    │         │ devices/transactions/users│ │
│  └─────────────┬──────────────┘         └─────────────┬─────────────┘ │
│   REST (fetch) │  SSE (NFC/session events)            │ REST          │
│  ┌─────────────▼──────────────────────────────────────▼─────────────┐ │
│  │  FastAPI Backend  (smart_locker/app.py + api/routes.py)          │ │
│  │  /api/session · /api/devices · /api/devices/{id}/borrow|return   │ │
│  │  /api/register · /api/admin/* · /api/dashboard/* · /api/events   │ │
│  └──────┬────────────────────┬───────────────────────┬─────────────┘ │
│  ┌──────▼───────────┐  ┌──────▼──────────────┐  ┌──────▼────────────┐ │
│  │ SQLite + ORM     │  │ NFC reader (ACR1252U)│  │ Excel sync        │ │
│  │ users · devices  │  │ background listener  │  │ source import     │ │
│  │ registrants      │  │ tap → HMAC → auth    │  │ on-demand export  │ │
│  │ transaction_logs │  └──────────────────────┘  └───────────────────┘ │
│  └──────────────────┘                                                   │
└──────────────────────────────────────────────────────────────────────┘
```

## Touch Display UI

The system runs as a kiosk: FastAPI serves the frontend as static files in a fullscreen Chromium browser. The NFC reader listens in the background; card taps push an event to the browser via Server-Sent Events (SSE), which drives the authentication and registration flows.

**Session model — tap-and-go:** the **work card** is tapped briefly to authenticate (not left on the reader). After login, tap an **NFC sticker on the device** (same reader) to borrow or return, or pick the unit on screen. Sessions end via the "End Session" button, the inactivity timeout, or a **work-card** tap (a device tag does not log you out).

**Screens (6 + overlays):**

1. **Idle** — animated NFC ring, "Tap your card to begin", marquee ticker, live clock, "Register your card" entry
2. **Register** — self-service: search/select your approved name → tap card → success/error
3. **Auth failed** — red flash, "Card not recognized", auto-dismisses
4. **Main menu** — welcome + **Tap the device**; Borrow / Return as *or pick on screen*; End Session
5. **Borrow** — device grid; available = tappable, borrowed/maintenance show borrower info
6. **Return** — device grid; the user's borrowed items highlighted

Overlays: **device detail** (photo, specs, confirm), **inactivity** countdown, and a **hidden admin panel** (5× tap on the clock) with Borrow/Return/Sync/Register User/**Register Device**/Export/End-Session shortcuts.

**Theme:** green (`#009641`) on dark charcoal (`#181d24`).

## Web Dashboard

A read-only dashboard is served at **`/dashboard`** for anyone on the local network — no authentication required. It shows the device inventory (slot, PM number, type, status, borrower, calibration due — filterable/sortable), transaction history (last 500), and registered users, auto-refreshing every 30 seconds. This replaced the old auto-synced Excel file (which suffered Windows file-locking issues); use **Export to Excel** from the admin panel for a downloadable snapshot.

## Self-Registration

New users can enroll their own card without an admin at the kiosk:

1. On the idle screen, tap **"Register your card"**.
2. Search and select your name from the approved list (`GET /api/registrants`). Approved names come from the **"Aktueller Einsatzort"** column during source Excel import (stored in the `registrants` table).
3. Submit (`POST /api/register`). If your name isn't on the list, registration is refused ("Contact an admin").
4. Tap your NFC card within the registration window (default 60s) — the card is enrolled under your approved name.

Admins can also register anyone manually from the hidden admin panel (`POST /api/admin/register`), bypassing the approved-name check. After enroll the kiosk returns to idle; the next work-card tap logs that user in.

## Security Design

- **Two separate 32-byte keys**: one for AES-256-GCM encryption, one for HMAC-SHA256.
- **HMAC for database lookup**: a deterministic digest allows indexed O(1) card lookups without decrypting every row.
- **AES-GCM for storage**: random nonce per encryption — the same UID produces different ciphertext each time.
- **Admin-only decryption**: only admin users can view raw card UIDs.
- **UID never logged**: card UIDs are never written to log files — events are logged as "Card inserted on \<reader\>" with no UID. UIDs are masked even in enrollment output (e.g. `04**********80`).

## Device Photos

Two ways to attach device photos (both copy into `smart_locker/frontend/images/` and update the device's `image_path`):

```bash
# By PM number (manual / batch / auto) — primary CLI:
python -m scripts.update_device --list                       # list devices + image status
python -m scripts.update_device --pm PM-042 --image scope.jpg --description "4-ch 500MHz scope"
python -m scripts.update_device --auto                        # auto-match PM-001.jpg, PM-002.png, …
python -m scripts.update_device --batch updates.txt           # batch from file
```

A background **photo watcher** also auto-assigns photos by **device model**: drop an image named exactly after the model (e.g. `87V.jpg` matches every model "87V" device) into the folder set by `SMART_LOCKER_PHOTO_INPUT_PATH`. The watcher is disabled when that variable is empty.

## NFC Device Tags

Cheap NFC stickers on locker devices use the same ACR1252U as work cards (no USB barcode scanner).

- **Storage:** `devices.tag_hmac` (HMAC-SHA256 of the sticker UID, same key as work cards). The raw UID is never stored or logged.
- **Flow:** tap work card → tap sticker (or pick on screen). Auto-intent from device status: borrow if available, return if you hold it. Session stays open for several devices. A work-card tap still logs out.
- **Register Device** (hidden admin panel) binds a sticker to an existing Excel/schrank row. The list shows **name + PM** because duplicate names exist. CLI: `python -m scripts.enroll_device_tag --pm PM-001` (or `--uid HEX`).
- Excel barcode is unused leftover; re-import does **not** overwrite `tag_hmac`, locker status, or the current borrower.

## Running Tests

```bash
python -m pytest tests/ -v
```

All tests run without NFC hardware (in-memory SQLite, no reader needed).
