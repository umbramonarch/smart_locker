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
│   ├── settings.py              # Central config (DB path, reader name, timeouts)
│   └── logging_config.py        # Rotating file + console logging
├── smart_locker/
│   ├── app.py                   # Main entry point
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
│       ├── locker_service.py    # Borrow/return operations
│       └── user_service.py      # User enrollment, public/admin views
├── scripts/
│   ├── generate_key.py          # Generate encryption + HMAC keys
│   ├── init_db.py               # Create database tables
│   └── enroll_card.py           # Enroll a new NFC card user
├── tests/                       # Unit tests (48 tests)
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

## Touch Display UI (Planned)

The system is designed for a kiosk-style setup with a touch display. The `smart_locker/ui/` package is the designated location for the future graphical interface.

**Intended user flow:**

1. User taps NFC card on the ACR1252U reader
2. System authenticates and shows a welcome screen on the touch display
3. Touch display shows two panels:
   - **Borrow**: list of available devices — tap a device to borrow it
   - **Return**: list of devices currently borrowed by this user — tap to return
4. User removes card or session times out after inactivity

**Current state:** The backend (NFC reading, authentication, device tracking, borrow/return logic, transaction logging) is fully implemented and tested. The UI layer (`smart_locker/ui/`) is a placeholder ready for a framework such as PyQt6, Kivy, or a local web UI to be integrated. All business logic is in the services layer and can be called directly from any UI.

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

All 48 tests run without NFC hardware (in-memory SQLite, no reader needed).
