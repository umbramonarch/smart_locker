# Smart Locker — Step-by-Step Setup and Usage Guide

## Prerequisites

Before starting, make sure you have:

1. **Python 3.11+** installed
2. **ACR1252U NFC reader** plugged into a USB port
3. **Windows Smart Card service** running (see Step 0 below)
4. One or more NFC cards (MIFARE Classic, Ultralight, NTAG215, DESFire, etc.)

---

## Step 0: Verify the Smart Card Service

The NFC reader communicates through the Windows Smart Card (PC/SC) service. It must be running.

```powershell
# Check if the service is running
Get-Service SCardSvr

# If Status is "Stopped", start it:
Start-Service SCardSvr

# To make it start automatically on boot:
Set-Service SCardSvr -StartupType Automatic
```

You should see `Status: Running`.

---

## Step 1: Set Up the Virtual Environment

```powershell
cd "D:\Python Projects\smart_locker"

# Create virtual environment (if not already created)
python -m venv venv

# Activate it
.\venv\Scripts\Activate

# Install dependencies
pip install -r requirements.txt
```

**Verify pyscard installed correctly:**

```powershell
python -c "from smartcard.System import readers; print(readers())"
```

Expected output (with reader plugged in):
```
['ACS ACR1252 Dual Reader PICC 0', 'ACS ACR1252 Dual Reader SAM 0']
```

If you see an empty list `[]`, check that the reader is plugged in and the Smart Card service is running.

---

## Step 2: Generate Encryption Keys

```powershell
python -m scripts.generate_key
```

Output will look like:
```
Add these to your .env file:

SMART_LOCKER_ENC_KEY=aBcDeFgH...==
SMART_LOCKER_HMAC_KEY=xYzAbCdE...==
```

Create a `.env` file in the project root:

```powershell
# Copy the .env.example as a starting point
Copy-Item .env.example .env
```

Then open `.env` in a text editor and paste the two key values from the output:

```
SMART_LOCKER_ENC_KEY=aBcDeFgH...==
SMART_LOCKER_HMAC_KEY=xYzAbCdE...==
SMART_LOCKER_DB_PATH=smart_locker.db
SMART_LOCKER_READER_NAME=ACR1252
SMART_LOCKER_SESSION_TIMEOUT=120
```

**IMPORTANT:** Keep the `.env` file secret. It contains your encryption keys. Never commit it to version control (it's already in `.gitignore`).

---

## Step 3: Initialize the Database

```powershell
python -m scripts.init_db
```

Expected output:
```
Database initialized successfully.
```

This creates `smart_locker.db` in the project root with three tables: `users`, `devices`, `transaction_logs`.

**Verify** (optional):

```powershell
python -c "import sqlite3; conn = sqlite3.connect('smart_locker.db'); print(conn.execute('SELECT name FROM sqlite_master WHERE type=\"table\"').fetchall())"
```

Expected: `[('users',), ('devices',), ('transaction_logs',)]`

---

## Step 4: Enroll Your First Card (Admin)

Make sure your NFC reader is plugged in, then run:

```powershell
python -m scripts.enroll_card --name "Your Name" --role admin
```

When you see `Place card on reader...`, tap your NFC card on the reader. Hold it steady for 1-2 seconds.

Expected output:
```
Reader: ACS ACR1252 Dual Reader PICC 0
Place card on reader...
Card detected (UID: A1****D4)
Enrolled: Your Name (id=1, role=admin)
```

Note: The card UID is masked for security. Only the first 2 and last 2 hex characters are shown. The full UID is encrypted and stored in the database — only admins can decrypt it.

**Enroll additional users** (as regular users):

```powershell
python -m scripts.enroll_card --name "Colleague Name" --role user
```

**Troubleshooting:**
- `Timeout — no card detected.` → Card wasn't tapped within 30 seconds. Try again.
- `Could not read card UID.` → Card was removed too quickly. Hold it steady.
- `ReaderNotFoundError` → Reader not plugged in or Smart Card service not running.

---

## Step 5: Add Devices to the System

### Option A: Bulk Import from Excel (recommended for 500+ devices)

Prepare your `.xlsx` file with at minimum two columns: one for **device name** and one for **serial number**. Optionally include **device type** and **locker slot** columns.

Example Excel layout:

| Device Name       | Serial Number | Type        | Locker |
|-------------------|---------------|-------------|--------|
| Multimeter        | SN-001        | measurement | 1      |
| Oscilloscope      | SN-002        | measurement | 2      |
| Soldering Station | SN-003        | tool        | 3      |

```powershell
# First, preview what will be imported (no changes written):
python -m scripts.import_devices --file "path\to\devices.xlsx" --dry-run

# Import all devices:
python -m scripts.import_devices --file "path\to\devices.xlsx"
```

The script auto-detects common column names (Name, Device Name, Serial, S/N, Type, Category, Slot, etc.). If your columns have different headers, map them explicitly:

```powershell
python -m scripts.import_devices --file devices.xlsx --name-col "Equipment" --serial-col "S/N" --type-col "Category"
```

If your sheet has no type column, set a default for all devices:

```powershell
python -m scripts.import_devices --file devices.xlsx --default-type "equipment"
```

To read a specific sheet (default is the first sheet):

```powershell
python -m scripts.import_devices --file devices.xlsx --sheet "Inventory"
```

Duplicates are automatically skipped (by serial number), so it's safe to re-run the import if you add new rows to the Excel file.

### Option B: Add a few devices manually

For adding individual devices without an Excel file:

```powershell
python -c "
import sys; sys.path.insert(0, '.')
from dotenv import load_dotenv; load_dotenv()
from smart_locker.database.engine import get_session, init_db
from smart_locker.database.repositories import DeviceRepository

init_db()
with get_session() as session:
    DeviceRepository.create(session, name='Multimeter', device_type='measurement', serial_number='SN-001', locker_slot=1)
    DeviceRepository.create(session, name='Oscilloscope', device_type='measurement', serial_number='SN-002', locker_slot=2)
    print('Devices added successfully.')
"
```

---

## Step 6: Run the Smart Locker System

```powershell
python -m smart_locker.app
```

Expected output:
```
Smart Locker ready. Tap your card to begin.
Press Ctrl+C to exit.
```

### Usage Flow

1. **Tap your card** → System authenticates you and shows your name, borrowed devices, and available devices
2. **Remove your card** → Session ends with a goodbye message
3. **Press Ctrl+C** → Shuts down the system

Example session:
```
Welcome, anas.alshaer!
You have 0 borrowed device(s).

3 device(s) available to borrow:
  [1] Multimeter (measurement)
  [2] Oscilloscope (measurement)
  [3] Soldering Station (tool)

Remove card to end session.

Goodbye, anas.alshaer!
Tap your card to begin.
```

---

## Step 7: Run the Test Suite

Tests run without any NFC hardware — they use in-memory SQLite and mock data.

```powershell
# Run all 48 tests
python -m pytest tests/ -v

# Run a specific test file
python -m pytest tests/test_security.py -v

# Run with short output
python -m pytest tests/ --tb=short
```

---

## Step 8: View Logs

Logs are written to the `logs/` directory with rotation (5 MB per file, 5 backups):

```powershell
# View the log file
Get-Content logs\smart_locker.log

# Follow the log in real-time
Get-Content logs\smart_locker.log -Wait
```

---

## Configuration Reference

All settings are in `.env` (loaded by `config/settings.py`):

| Variable | Default | Description |
|----------|---------|-------------|
| `SMART_LOCKER_ENC_KEY` | (required) | AES-256 encryption key, base64-encoded |
| `SMART_LOCKER_HMAC_KEY` | (required) | HMAC-SHA256 key, base64-encoded |
| `SMART_LOCKER_DB_PATH` | `smart_locker.db` | SQLite database file path |
| `SMART_LOCKER_READER_NAME` | `ACR1252` | Substring filter for NFC reader name |
| `SMART_LOCKER_SESSION_TIMEOUT` | `120` | Session inactivity timeout (seconds) |

---

## What's Implemented vs. What's Next

### Implemented
- NFC card UID reading (any card type)
- Card enrollment with encrypted storage
- User authentication via HMAC lookup
- Session management with inactivity timeout
- Device tracking (available/borrowed/maintenance)
- Transaction logging (borrow/return)
- Admin vs. regular user roles
- AES-256-GCM encryption of card UIDs
- UID masking in logs and on screen (never displayed in full)
- Reader connect/disconnect detection
- 48 unit tests

### Future (Not Yet Built)
- **Touch display UI** (`smart_locker/ui/`): The backend is complete — the UI package is a placeholder ready for a graphical framework. Options include:
  - **PyQt6** — native desktop widgets, good touch support
  - **Kivy** — designed for touch/multitouch, cross-platform
  - **Local web UI** — Flask/FastAPI serving HTML to a browser in kiosk mode
  - The UI would show a welcome screen after card tap, list available/borrowed devices, and let the user tap to borrow or return
- Borrow/return commands during active session (currently the console app shows info only; the services layer `LockerService.borrow_device()` / `return_device()` is ready to be called from a UI)
- Device management CLI or web interface
- MIFARE sector data reading (APDU commands are defined but not wired into the flow)
- Multi-reader support
