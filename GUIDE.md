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
SMART_LOCKER_MAX_BORROWS=5
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

| Device Name       | Serial Number | Type        | Locker | Description             | Image         |
|-------------------|---------------|-------------|--------|-------------------------|---------------|
| Multimeter        | SN-001        | measurement | 1      | Fluke 87V digital meter | multimeter.jpg|
| Oscilloscope      | SN-002        | measurement | 2      | Rigol DS1054Z 50MHz     | oscilloscope.jpg|
| Soldering Station | SN-003        | tool        | 3      | Hakko FX-888D           | soldering.jpg |

The **Description** column provides a short text shown on the touch display when a user taps a device. The **Image** column contains the filename of a device photo stored in the `static/devices/` directory. Both columns are optional — devices without them will display with a placeholder image and no description.

```powershell
# First, preview what will be imported (no changes written):
python -m scripts.import_devices --file "path\to\devices.xlsx" --dry-run

# Import all devices:
python -m scripts.import_devices --file "path\to\devices.xlsx"
```

The script auto-detects common column names (Name, Device Name, Serial, S/N, Type, Category, Slot, Description, Image, Photo, etc.). If your columns have different headers, map them explicitly:

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

This starts the FastAPI backend server and the NFC reader listener. The touch display UI is served as a web page and should be opened in a browser running in kiosk mode (see Step 6b).

Expected terminal output:
```
Smart Locker starting...
NFC reader ready: ACS ACR1252 Dual Reader PICC 0
Server running at http://localhost:8000
Press Ctrl+C to exit.
```

### Step 6b: Launch the Touch Display (Kiosk Browser)

Open the UI in a fullscreen kiosk browser on the touch display:

```powershell
# Using Chromium/Chrome in kiosk mode (fullscreen, no address bar):
chrome --kiosk http://localhost:8000
```

### Session Flow (Tap-and-Go)

The NFC card is **tapped and removed** — it is not left on the reader. The card's only purpose is to authenticate. After authentication, all interaction happens on the touch display.

1. **Tap your card** → NFC reader reads UID → system authenticates → touch display shows welcome screen
2. **Interact on touch display** → Borrow devices, return devices, view device info
3. **Session ends** via one of:
   - **"End Session" button** on the touch display
   - **Inactivity timeout** (default 120 seconds with no touch interaction)
   - **New card tap** by a different user (overrides the current session)

### Touch Display Screens

**Screen A — Idle (no active session)**
- Dark background, system logo
- "Tap your card to begin"
- Subtle ambient animation (glow/pulse)

**Screen B — Authentication Failed**
- "Card not recognized. Contact an administrator."
- Auto-returns to Screen A after 3 seconds

**Screen C — Main Menu (after successful tap)**
- "Welcome, [Name]!" with entrance animation
- Three large touch buttons:
  - **Borrow** → Screen D
  - **Return** → Screen E
  - **End Session** → logs out, returns to Screen A

**Screen D — Borrow View**
- Grid of **all devices** organized by locker slot
- Each device card shows: **photo**, name, type, slot number
- **Available devices**: full color, tappable → Screen F (detail + confirm borrow)
- **Unavailable devices** (borrowed by someone): greyed out, tappable → Screen G (shows who has it)
- **Maintenance devices**: greyed out with maintenance indicator
- If user has reached the borrow limit (default 5) → warning: "Borrow limit reached (5/5)"
- Back button → Screen C

**Screen E — Return View**
- Grid of **all devices** organized by locker slot
- **Devices borrowed by this user**: highlighted/accented, tappable → Screen H (detail + confirm return)
- **Devices borrowed by someone else**: greyed out, tappable → Screen G (shows who has it)
- **Available devices** (already in locker): greyed out, non-actionable
- **Validation**: If user taps a device that isn't theirs → error: "This device is held by [Name], not you."
- Back button → Screen C

**Screen F — Borrow Detail + Confirm**
- Large device photo, name, type, serial number, locker slot, description
- "Borrow this device?" → **Confirm** / **Cancel**
- On confirm → success animation → back to Screen D (list refreshes)

**Screen G — Device Info (read-only)**
- Large device photo, name, type, serial number, locker slot, description
- "Currently borrowed by: [Name]"
- **Close** → back to previous screen

**Screen H — Return Detail + Confirm**
- Large device photo, name, type, serial number, locker slot, description
- "Return this device to slot [X]?" → **Confirm** / **Cancel**
- On confirm → success animation → back to Screen E (list refreshes)

### Borrow/Return Rules

- **Borrow limit**: Each user can borrow up to `SMART_LOCKER_MAX_BORROWS` devices (default 5). Configurable in `.env`.
- **Return ownership**: Only the borrower can return their own device. Admins can return any device on behalf of any user.
- **Admin returns**: When an admin returns a device on behalf of someone, the transaction log records both the original borrower and the admin who performed the return.
- **Open-access locker**: There is no physical locking mechanism. The locker is open-access and the system is purely for tracking who has what.

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
| `SMART_LOCKER_MAX_BORROWS` | `5` | Maximum devices a user can borrow at once |

---

## What's Implemented vs. What's Next

### Implemented (Backend)
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
- Bulk device import from Excel
- 48 unit tests

### Next: Database Updates
- Add `description` column to `devices` table (text from Excel file)
- Add `image_path` column to `devices` table (device photo filename)
- Add `performed_by_id` column to `transaction_logs` table (tracks which admin performed a return on behalf of another user — `user_id` = original borrower, `performed_by_id` = admin)
- Add `SMART_LOCKER_MAX_BORROWS` to settings (configurable borrow limit, default 5)

### Next: Backend API (FastAPI)
- REST API layer exposing the existing services to the frontend:
  - `POST /api/auth/tap` — NFC UID → authenticate → start session
  - `GET /api/devices` — list all devices with status, photos, descriptions
  - `POST /api/borrow/{device_id}` — borrow a device
  - `POST /api/return/{device_id}` — return a device
  - `POST /api/session/end` — end the current session
  - `GET /api/session` — get current session info (user, borrowed count)
- NFC reader runs as a background listener, pushes card-tap events to the API

### Next: Touch Display UI (Web-Based)
- **Architecture**: FastAPI backend serves a modern web frontend; touch display runs a browser in kiosk mode (fullscreen Chromium with `--kiosk` flag)
- **Design style**: Dark premium theme, smooth animations, bold typography — inspired by modern web design (e.g. landonorris.com). The UI should feel professional and visually polished, not like a generic admin panel.
- **Screen flow**: Idle → Auth → Main Menu (Borrow / Return / End Session) → Device grid with photos → Detail view with confirm — see Step 6 above for full screen descriptions
- **Device display**: Each device shown as a card with photo, name, type, and locker slot. Tapping reveals full detail including description. Unavailable devices are greyed out but tappable to see who has them.

### Frontend Design Tools

The frontend design (visual mockups, component layouts, animations) can be created using AI-powered design tools before implementation:

- **v0.dev** (by Vercel) — generates React/Next.js UI components from text prompts. Good for quickly prototyping screen layouts, card grids, and dark-themed interfaces.
- **Galileo AI** — generates full UI designs from text descriptions. Produces high-fidelity mockups that can be exported and implemented.
- **Figma + AI plugins** — use Figma for manual design with AI-assisted features (auto-layout, AI-generated assets). Most control over the final look.
- **Bolt.new** — AI full-stack app generator. Can scaffold a complete frontend with animations and responsive layouts from a prompt.
- **Lovable** — AI web app builder. Generates polished frontends from descriptions, good for dark-themed dashboards and kiosk UIs.

**Recommended workflow**: Design the screens in one of these tools first → export or screenshot the design → implement the HTML/CSS/JS to match, with the FastAPI backend providing the data.

### Future (Not Yet Built)
- Device management admin interface
- MIFARE sector data reading (APDU commands are defined but not wired into the flow)
- Multi-reader support
