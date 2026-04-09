# Smart Locker System

Equipment borrowing/returning system using NFC work cards. Users tap their card on an ACR1252U NFC reader to authenticate, then borrow or return devices. All transactions are logged in a SQLite database. Card data is encrypted (AES-256-GCM) — only admins can view raw card UIDs.

## Requirements

- Windows with the **Smart Card** service running
- ACR1252U NFC reader (USB)
- Python 3.11+
- NFC cards (MIFARE Classic, Ultralight, NTAG, DESFire — any card type with a UID)

## Project Structure

```
smart_locker/
├── config/
│   ├── settings.py              # Central config (DB path, reader name, timeouts, sync paths)
│   └── logging_config.py        # Rotating file + console logging
├── smart_locker/
│   ├── app.py                   # Entry point: web server (FastAPI + uvicorn) or CLI mode
│   ├── api/                     # FastAPI REST API
│   │   ├── routes.py            # Session, device, borrow/return, registration, admin, dashboard endpoints + SSE
│   │   ├── server.py            # FastAPI app factory + static file serving
│   │   └── app_context.py       # Shared app context (session manager, SSE queue, pending registration)
│   ├── frontend/                # Touch display web UI + network dashboard
│   │   ├── index.html           # Kiosk HTML structure (6 screens + registration + admin panel)
│   │   ├── style.css            # Phoenix Contact green theme, animations, layout
│   │   ├── app.js               # State machine, API calls, UI behaviour (demo mode included)
│   │   ├── dashboard.html       # Network-wide device/transaction/user dashboard (public)
│   │   ├── dashboard.js         # Dashboard logic and API calls
│   │   ├── dashboard.css        # Dashboard styling
│   │   └── images/              # Device photos (matched by model number)
│   ├── nfc/                     # NFC reader interface (pyscard + APDU)
│   │   ├── apdu.py              # APDU command definitions
│   │   ├── card_observer.py     # Card insert/remove detection
│   │   ├── reader_observer.py   # Reader connect/disconnect detection
│   │   ├── reader.py            # High-level NFCReader class
│   │   └── exceptions.py        # NFC-specific exceptions
│   ├── auth/                    # Authentication
│   │   ├── authenticator.py     # Card UID → user lookup via HMAC
│   │   └── session_manager.py   # Single-user session lifecycle
│   ├── security/                # Cryptography
│   │   ├── encryption.py        # AES-256-GCM encrypt/decrypt
│   │   ├── hashing.py           # HMAC-SHA256 for card UID fingerprinting
│   │   └── key_manager.py       # Key loading from environment
│   ├── database/                # Data layer
│   │   ├── models.py            # ORM models (User, Device, TransactionLog, Registrant)
│   │   ├── engine.py            # SQLAlchemy engine + session factory
│   │   └── repositories.py      # CRUD operations
│   ├── services/                # Business logic
│   │   ├── locker_service.py    # Borrow/return operations (5-device limit, admin overrides)
│   │   └── user_service.py      # User enrollment, public/admin views
│   └── sync/                    # Data synchronization
│       ├── excel_sync.py        # On-demand Excel export (admin download + CLI scripts)
│       ├── source_import.py     # Source Excel import (devices + registrants from OneDrive)
│       ├── scheduler.py         # Startup import, file watcher, daily cron job
│       └── photo_watcher.py     # Auto-import device photos by model number
├── scripts/
│   ├── generate_key.py          # Generate encryption + HMAC keys
│   ├── init_db.py               # Create database tables
│   ├── migrate_db.py            # Add columns to existing DB (run after schema changes)
│   ├── enroll_card.py           # Enroll a new NFC card user
│   ├── import_devices.py        # Bulk import devices from Excel (German + English headers)
│   ├── update_device.py         # Update device fields (image, description, etc.) by PM number
│   └── sync_source.py           # Manually trigger source Excel import
├── tests/                       # Unit tests (130 tests, no hardware needed)
├── requirements.txt
├── .env.example
├── GUIDE.md                     # Step-by-step setup and usage guide
└── README.md
```

## Build Status

| Layer | Status | Notes |
|---|---|---|
| NFC reader (pyscard) | ✅ Done | Card insert/remove, UID reading, retry logic |
| Authentication | ✅ Done | HMAC lookup, session lifecycle |
| Security (AES/HMAC) | ✅ Done | AES-256-GCM encryption, key management |
| Database & ORM | ✅ Done | SQLAlchemy models, repositories, extended device schema |
| Business logic | ✅ Done | Borrow/return rules, admin overrides, borrow limit |
| FastAPI REST API | ✅ Done | Session, device, borrow/return, registration, admin, dashboard endpoints + SSE |
| Web dashboard | ✅ Done | Public network-wide device/transaction/user dashboard (replaces shared Excel) |
| On-demand Excel export | ✅ Done | Admin download endpoint generates .xlsx in memory (no auto-sync) |
| Source Excel import | ✅ Done | Startup import, file watcher, daily cron job from OneDrive |
| Device import | ✅ Done | German + English Excel headers, PM-based dedup, schrank auto-numbering |
| Self-registration | ✅ Done | Approved name list from source Excel, searchable/filterable selection UI |
| Admin panel | ✅ Done | Hidden 5-tap gesture, manual registration, sync trigger, Excel download |
| Auto photo import | ✅ Done | Watchdog monitors input folder, matches photos by model number |
| Phoenix Contact theme | ✅ Done | Signature green (#009641) on dark charcoal (#181d24), improved readability |
| Unit tests | ✅ Done | 130 tests, all passing, no hardware required |
| Frontend UI | ✅ Done | Kiosk UI with registration, admin panel, animations, demo mode |
| Barcode scanner | 🔲 Planned | Barcode values stored per device. USB scanner will emulate keyboard input to identify devices in shared lockers. |
| Calibration alerts | 🔲 Future | Calibration dates stored; notification system not yet built |
| Kiosk deployment | 🔲 Future | Auto-start, Chromium kiosk mode, Windows service |

## Quick Start

```powershell
# 1. Install dependencies
pip install -r requirements.txt

# 2. Generate encryption keys
python -m scripts.generate_key

# 3. Create .env file with the generated keys
Copy-Item .env.example .env
# then paste the generated keys into .env

# 4. Initialize database
python -m scripts.init_db

# 5. Enroll a card (requires NFC reader)
python -m scripts.enroll_card --name "Your Name" --role admin

# 6. Run the system
python -m smart_locker.app
```

See **GUIDE.md** for detailed step-by-step instructions.

## System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Touch Display (Chromium Kiosk Mode)             │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  Frontend  (smart_locker/frontend/)                   │  │
│  │  index.html · style.css · app.js                     │  │
│  │  Phoenix Contact green theme · clip-path animations    │  │
│  │  6+ screens · touch-optimized · Space Grotesk font    │  │
│  └─────────────────────┬─────────────────────────────────┘  │
│          REST API       │       SSE / WebSocket              │
│        (fetch calls)    │    (NFC tap → navigate)            │
│  ┌──────────────────────▼─────────────────────────────────┐  │
│  │  FastAPI Backend  (smart_locker/app.py)                │  │
│  │  POST /api/auth/tap   · GET /api/devices               │  │
│  │  POST /api/borrow     · POST /api/return               │  │
│  │  POST /api/session/end · GET /api/session              │  │
│  │  GET  /api/events  ← SSE stream for NFC events         │  │
│  └──────┬──────────────────────────┬──────────────────────┘  │
│         │                          │                          │
│  ┌──────▼──────────────┐  ┌────────▼──────────────────┐      │
│  │  SQLite + SQLAlchemy │  │  NFC Reader (ACR1252U)    │      │
│  │  users · devices     │  │  Background listener       │      │
│  │  transaction_logs    │  │  Tap → HMAC → auth        │      │
│  │  registrants         │  └───────────────────────────┘      │
│  └──────┬───────────────┘                                       │
│         │                                                        │
│  ┌──────▼───────────────────────────────────────────────┐      │
│  │  Web Dashboard  (/dashboard.html — public, no auth)   │      │
│  │  Devices · Transactions · Users (replaces shared Excel)│     │
│  └───────────────────────────────┬──────────────────────┘      │
│                                  │                               │
│  ┌───────────────────────────────▼──────────────────────┐      │
│  │  Source Excel Import (OneDrive → DB)                   │      │
│  │  Startup + file watcher + daily cron (6 AM)           │      │
│  └──────────────────────────────────────────────────────┘      │
└─────────────────────────────────────────────────────────────┘
```

## Touch Display UI

The system runs as a kiosk: FastAPI serves the frontend as static files, displayed in a fullscreen Chromium browser. The NFC reader listens in the background; card taps push an event to the browser via Server-Sent Events (SSE), which triggers the authentication flow.

**Session model — tap-and-go:** The NFC card is tapped briefly to authenticate (not left on the reader). After authentication, all interaction happens on the touch display. Sessions end via the "End Session" button, the 120-second inactivity timeout, or a new card tap.

**Screen flow:**

1. **Idle** — animated NFC icon, "Tap your card to begin", marquee ticker, live clock
2. **Auth failed** — red flash, "Card not recognized", auto-dismisses after 3s
3. **Main menu** — "Welcome, [Name]!" with clip-path text reveal, Borrow / Return / End Session buttons
4. **Borrow view** — device grid sorted by slot. Available = tappable; borrowed/maintenance = greyed out but tappable (shows borrower info)
5. **Return view** — same grid. User's items highlighted in cyan; others greyed out
6. **Device detail** — full-screen overlay, large photo, serial / type / slot, confirm button

**Design highlights:**
- Phoenix Contact green (`#009641`) accent on dark charcoal (`#181d24`) background
- `clip-path: circle()` reveal transitions between every screen — inspired by landonorris.com
- Space Grotesk display font, Inter body font
- Staggered device card entrance animations
- Custom lagged cursor, Web Audio API click sounds, scanline overlay

## Security Design

- **Two separate 32-byte keys**: one for AES-256-GCM encryption, one for HMAC-SHA256
- **HMAC for database lookup**: deterministic digest allows indexed O(1) card lookups without decrypting every row
- **AES-GCM for storage**: random nonce per encryption — same UID produces different ciphertext each time
- **Admin-only decryption**: only admin users can view raw card UIDs
- **UID never logged**: card UIDs are never written to log files — events are logged as "Card inserted on \<reader\>" with no UID information. UIDs are masked in enrollment output only (e.g. `04**********80`)

## Barcode Scanner Plan

The system stores a barcode value per device. The planned barcode scanner workflow:

- **Shared lockers**: Multiple devices of the same type (e.g., 5 current probes) share one locker. The barcode identifies the specific device.
- **Hardware**: USB barcode scanner (keyboard emulation) connected to the kiosk PC.
- **Flow**: NFC authenticate → select Borrow/Return → scan device barcode → system matches `devices.barcode` → completes transaction.
- **Implementation**: Barcode input listener in `app.js` (detects rapid keystrokes ending in Enter) + `GET /api/devices/barcode/{barcode}` endpoint.

## Updating Device Images & Descriptions

```powershell
# List all devices (shows locker slot, PM number, current image status):
python -m scripts.update_device --list

# Auto-match: name photos by PM number (PM-001.jpg, PM-002.png, etc.),
# place them in smart_locker/frontend/images/, then run:
python -m scripts.update_device --auto

# Set image and description for a single device:
python -m scripts.update_device --pm PM-042 --image oscilloscope.jpg --description "4-ch 500MHz scope"

# Batch update from file:
python -m scripts.update_device --batch updates.txt
```

Place device photos in `smart_locker/frontend/images/`. Name them by PM number for `--auto` matching. The script auto-syncs changes to the Excel file.

## Running Tests

```powershell
python -m pytest tests/ -v
```

All 130 tests run without NFC hardware (in-memory SQLite, no reader needed).
