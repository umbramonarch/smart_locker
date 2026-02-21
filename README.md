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
│   ├── settings.py              # Central config (DB path, reader name, timeouts, borrow limit)
│   └── logging_config.py        # Rotating file + console logging
├── smart_locker/
│   ├── app.py                   # Main entry point (FastAPI server + NFC listener)
│   ├── api/                     # REST API endpoints (planned)
│   │   ├── routes.py            # /api/auth, /api/devices, /api/borrow, /api/return
│   │   └── schemas.py           # Request/response models
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
│   │   ├── models.py            # ORM models (User, Device, TransactionLog)
│   │   ├── engine.py            # SQLAlchemy engine + session factory
│   │   └── repositories.py      # CRUD operations
│   └── services/                # Business logic
│       ├── locker_service.py    # Borrow/return operations (5-device limit, admin overrides)
│       └── user_service.py      # User enrollment, public/admin views
├── frontend/                    # Touch display web UI (planned)
│   └── ...                      # HTML/CSS/JS — dark theme, animations, device photos
├── static/
│   └── devices/                 # Device photos (referenced by image_path in DB)
├── scripts/
│   ├── generate_key.py          # Generate encryption + HMAC keys
│   ├── init_db.py               # Create database tables
│   ├── enroll_card.py           # Enroll a new NFC card user
│   └── import_devices.py        # Bulk import devices from Excel
├── tests/                       # Unit tests (52 tests)
├── requirements.txt
├── .env.example
└── GUIDE.md                     # Step-by-step setup and usage guide
```

## Quick Start

```powershell
# 1. Install dependencies
pip install -r requirements.txt

# 2. Generate encryption keys
python -m scripts.generate_key

# 3. Create .env file with the generated keys
# (copy output from step 2 into .env)

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
┌──────────────────────────────────────────────────────┐
│            Touch Display (Chromium Kiosk Mode)        │
│  ┌────────────────────────────────────────────────┐  │
│  │  Frontend (HTML / CSS / JS)                    │  │
│  │  Dark premium theme, smooth animations,        │  │
│  │  device photos, touch-optimized UI             │  │
│  └──────────────────┬─────────────────────────────┘  │
│                     │ REST API                        │
│  ┌──────────────────▼─────────────────────────────┐  │
│  │  Backend (FastAPI)                             │  │
│  │  /api/auth/tap · /api/devices · /api/borrow    │  │
│  │  /api/return · /api/session/end                │  │
│  └──────┬───────────────────────────┬─────────────┘  │
│         │                           │                 │
│  ┌──────▼──────────┐  ┌────────────▼──────────┐      │
│  │  SQLite (SQLAlchemy)│  │  NFC Reader (ACR1252U)│   │
│  │  users · devices    │  │  Background listener  │   │
│  │  transaction_logs   │  │  Tap-and-go auth      │   │
│  └─────────────────────┘  └───────────────────────┘   │
└──────────────────────────────────────────────────────┘
```

## Touch Display UI (Web-Based)

The system runs as a kiosk: a FastAPI backend serves a web frontend displayed in a fullscreen browser on the touch display. The NFC reader listens in the background for card taps.

**Session model — tap-and-go:** The NFC card is tapped briefly to authenticate (not left on the reader). After authentication, all interaction happens on the touch display. Sessions end via an "End Session" button, inactivity timeout (default 120s), or a new card tap by a different user.

**Screen flow:**

1. **Idle** — dark screen, "Tap your card to begin"
2. **Auth failed** — "Card not recognized" (auto-dismisses after 3s)
3. **Main menu** — "Welcome, [Name]!" with three buttons: **Borrow**, **Return**, **End Session**
4. **Borrow view** — grid of all devices by locker slot with photos. Available devices are tappable; unavailable devices are greyed out but tappable to see who has them. Borrow limit: 5 devices per user (configurable).
5. **Return view** — grid of all devices by locker slot. User's borrowed devices are highlighted and tappable to return. Other users' devices are greyed out but tappable to see who has them. Validation prevents returning a device you don't have.
6. **Device detail** — large photo, name, type, serial, slot, description. Confirm borrow/return or view borrower info.

**Design:** Dark premium theme with smooth animations and bold typography, optimized for touch interaction. Device cards display photos, names, types, and slot numbers. Frontend design can be prototyped using AI tools (v0.dev, Galileo AI, Bolt.new, Lovable, or Figma) and then implemented to match.

**Rules:**
- Open-access locker — no physical locks, system is purely for tracking
- Max 5 borrows per user (configurable via `SMART_LOCKER_MAX_BORROWS`)
- Only the borrower can return their own device; admins can return on behalf of anyone
- Admin returns log both the admin and the original borrower in the transaction record
- Device list (names, serials, types, descriptions, photos) is managed via Excel import

**Current state:** The backend (NFC reading, authentication, device tracking, borrow/return logic, transaction logging) is fully implemented and tested. What remains is the FastAPI API layer and the frontend UI.

## Security Design

- **Two separate 32-byte keys**: one for AES-256-GCM encryption, one for HMAC-SHA256
- **HMAC for database lookup**: deterministic digest allows indexed O(1) card lookups without decrypting every row
- **AES-GCM for storage**: random nonce per encryption — same UID produces different ciphertext each time
- **Admin-only decryption**: only admin users can view raw card UIDs via the admin info API
- **UID masking**: card UIDs are never shown in full on screen or in log files — only a masked form (e.g. `04**********80`) is displayed

## Running Tests

```powershell
python -m pytest tests/ -v
```

All 52 tests run without NFC hardware (in-memory SQLite, no reader needed).
