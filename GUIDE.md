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
cd path\to\smart_locker

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

### Option A: Bulk Import from Excel (recommended)

Prepare your `.xlsx` file with at minimum a **PM/equipment number** column. The script auto-detects both German and English column headers.

**Supported columns (auto-detected):**

| Excel Column | German Name | Maps To | Required? |
|---|---|---|---|
| Equipment | Equipment | `pm_number` | **Yes** — primary device identifier |
| Category | Kategorie | `device_type` | No (defaults to "general") |
| Description | Beschreibung | `description` | No |
| Manufacturer | Hersteller | `manufacturer` | No |
| Type designation | Typbezeichnung | `model` | No |
| Serial number | Hersteller-serialnummer | `serial_number` | No |
| Barcode | Barcode | `barcode` | No |
| Locker placement | Platz Messmittelschrank | `locker_slot` | No |
| Calibration date | Datum der nächsten Kalibrierung | `calibration_due` | No |

**Name auto-composition:** If no dedicated "name" column exists, the device name is composed as `"{PM number} {Manufacturer} {Model}"` (e.g., "PM-042 Keysight DSOX3054T").

**Locker slot auto-numbering:** Devices with "schrank*" values (e.g., "schrank1") are auto-numbered 1 through N in spreadsheet order.

```powershell
# First, preview what will be imported (no changes written):
python -m scripts.import_devices --file "path\to\devices.xlsx" --dry-run

# Import all devices:
python -m scripts.import_devices --file "path\to\devices.xlsx"
```

The script prints the detected column mapping before importing. If auto-detection picks the wrong column, override it explicitly:

```powershell
python -m scripts.import_devices --file devices.xlsx --pm-col "Equipment" --type-col "Kategorie" --manufacturer-col "Hersteller"
```

All available overrides: `--pm-col`, `--name-col`, `--serial-col`, `--type-col`, `--slot-col`, `--manufacturer-col`, `--model-col`, `--barcode-col`, `--calibration-col`.

To read a specific sheet (default is the first sheet):

```powershell
python -m scripts.import_devices --file devices.xlsx --sheet "Inventory"
```

Duplicates are automatically skipped (by PM number), so it's safe to re-run the import if you add new rows to the Excel file. After import, the data is automatically exported to `smart_locker_data.xlsx`.

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
    DeviceRepository.create(session, name='PM-001 Fluke 87V', device_type='Multimeter', pm_number='PM-001', serial_number='SN-001', manufacturer='Fluke', model='87V', locker_slot=1)
    DeviceRepository.create(session, name='PM-002 Rigol DS1054Z', device_type='Oscilloscope', pm_number='PM-002', serial_number='SN-002', manufacturer='Rigol', model='DS1054Z', locker_slot=2)
    print('Devices added successfully.')
"
```

### Step 5c: Add Device Images and Descriptions

After importing devices, you can add images and descriptions using the update script.

**1. Place your photos** in `smart_locker/frontend/images/`. Use dark-background product-style photos for best results on the kiosk UI. Supported formats: `.jpg`, `.png`, `.webp`.

**2. Update each device:**

```powershell
# See all devices and their current image/description status:
python -m scripts.update_device --list

# Set image and description for a single device:
python -m scripts.update_device --pm PM-042 --image oscilloscope.jpg --description "4-channel 500MHz digital oscilloscope"

# Set just the image:
python -m scripts.update_device --pm PM-042 --image oscilloscope.jpg

# Set any field:
python -m scripts.update_device --pm PM-042 --field manufacturer --value "Keysight"
```

The `--image` flag auto-prepends `images/` if you only provide a filename. Changes are immediately synced to the Excel file.

**3. Batch update** — for updating many devices at once, create a text file with one update per line:

```
# updates.txt — format: PM_NUMBER field value
PM-001 image_path images/oscilloscope.jpg
PM-001 description 4-channel 500MHz digital oscilloscope
PM-002 image_path images/power_supply.jpg
PM-002 description Triple-output programmable DC power supply
PM-003 image_path images/multimeter.jpg
```

Then run:

```powershell
python -m scripts.update_device --batch updates.txt
```

---

## Step 6: Run the Smart Locker System

```powershell
python -m smart_locker.app
```

This starts the NFC reader listener and the main event loop. Tap your enrolled card to authenticate, tap again to log out.

Expected terminal output:
```
Smart Locker starting...
NFC reader ready: ACS ACR1252 Dual Reader PICC 0
Smart Locker ready. Tap your card to begin.
Press Ctrl+C to exit.
```

> **Note:** By default, the system starts in web server mode — FastAPI serves the kiosk UI at `http://localhost:8000`. Use `python -m smart_locker.app --cli` for the terminal-only NFC loop (no web UI).

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
   - **Second card tap** — tap your card again to log out
   - **Inactivity timeout** — 120 seconds of no touch interaction (silent security backstop)

### Touch Display Screens

**Screen 1 — Idle (no active session)**
- Animated NFC icon with pulsing rings and cyan glow
- "TAP YOUR CARD" in large display font with text-reveal animation
- Scrolling marquee ticker at the bottom, live clock in the top-right

**Screen 2 — Authentication Failed**
- Red flash overlay, "Card Not Recognized" error card
- Auto-returns to Screen 1 after 3 seconds with a depleting progress bar

**Screen 3 — Main Menu (after successful tap)**
- "WELCOME BACK, [FIRSTNAME]" with clip-path text entrance animation
- Three large touch buttons: **Borrow** · **Return** · **End Session**
- User initials avatar and role badge (User / Admin) in top-right

**Screen 4 — Borrow View**
- Grid of all devices organised by locker slot, with staggered card entrance
- **Available** devices: full colour, tappable → Device Detail (confirm borrow)
- **Borrowed** devices: greyed out, tappable → Device Detail (shows borrower name)
- **Maintenance** devices: amber badge, greyed out
- Badge showing current borrow count vs. limit (e.g. `1 / 5 borrowed`)

**Screen 5 — Return View**
- Same grid layout as Borrow View
- User's own items highlighted with cyan border glow
- Other users' items greyed out but tappable (to see who has them)

**Screen 6 — Device Detail (overlay)**
- Full-screen overlay slides up over the grid
- Left half: device photo (or slot-number placeholder)
- Right half: slot tag, device name, type, serial, status, description, borrower info
- Confirm button: green (borrow) or cyan (return) or disabled (not actionable)

**Inactivity Warning**
- Appears 10 seconds before the session timeout
- Large countdown number, "Stay Active" button dismisses it and resets the timer

### Borrow/Return Rules

- **Borrow limit**: Each user can borrow up to `SMART_LOCKER_MAX_BORROWS` devices (default 5). Configurable in `.env`.
- **Return ownership**: Only the borrower can return their own device. Admins can return any device on behalf of any user.
- **Admin returns**: When an admin returns a device on behalf of someone, the transaction log records both the original borrower and the admin who performed the return.
- **Open-access locker**: There is no physical locking mechanism. The locker is open-access and the system is purely for tracking who has what.

---

## Step 7: Run the Test Suite

Tests run without any NFC hardware — they use in-memory SQLite and mock data.

```powershell
# Run all 73 tests
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

## Step 9: Excel Data File

The system automatically exports all device and transaction data to an Excel file (`smart_locker_data.xlsx` by default) every time the database changes. This happens automatically — no manual steps needed.

**Two sheets:**
- **Devices** — PM Number, Name, Type, Manufacturer, Model, Serial, Barcode, Locker Slot, Status, Current Borrower, Description, Calibration Due
- **Transactions** — Date, User, Device, Borrow/Return, Performed By (admin), Notes

The file is regenerated on:
- App startup
- Every borrow or return
- After a bulk device import

If the file is open in Excel when a sync happens, the system logs a warning and retries on the next change. Close the file to allow the sync to proceed.

To change the output path, set `SMART_LOCKER_EXCEL_PATH` in your `.env`.

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
| `SMART_LOCKER_EXCEL_PATH` | `smart_locker_data.xlsx` | Auto-synced Excel export file |
| `SMART_LOCKER_API_HOST` | `0.0.0.0` | FastAPI server bind address |
| `SMART_LOCKER_API_PORT` | `8000` | FastAPI server port |

---

## What's Built vs. What's Next

### ✅ Built — Backend Core

- NFC card UID reading (any card type with a UID)
- Card enrollment with AES-256-GCM encrypted storage
- User authentication via HMAC-SHA256 card fingerprint lookup
- Session management with inactivity timeout
- Device tracking with extended schema (PM number, manufacturer, model, barcode, calibration date, locker slot, status)
- Transaction logging (borrow / return, with admin-return attribution)
- Admin vs. regular user roles with permission enforcement
- Borrow limit enforcement (default 5 devices per user, configurable)
- UID masking in logs and on screen (never displayed in full)
- Reader connect/disconnect detection and retry logic
- Bulk device import from Excel with German column auto-detection (Equipment, Hersteller, Typbezeichnung, Kategorie, etc.)
- Automatic Excel sync — database changes export to `smart_locker_data.xlsx` in real-time (Devices + Transactions sheets)
- FastAPI REST API with SSE event stream for NFC → browser bridge
- 73 unit tests — all passing, no NFC hardware required

### ✅ Built — Frontend UI

- `smart_locker/frontend/index.html` — 6-screen HTML structure
- `smart_locker/frontend/style.css` — full styling: colors, animations, layout
- `smart_locker/frontend/app.js` — state machine, API stubs, all UI behaviour
- Clip-path wipe transitions between every screen
- Animated NFC pulse icon, mesh gradient background, marquee ticker
- Device grid with staggered card entrance and hover effects
- Device detail overlay with photo, metadata, and confirm button
- Inactivity warning overlay with live countdown
- Custom cursor with lagged ring follower
- Web Audio API click sounds (no audio files)
- Demo mode with sample data — fully testable without a backend
- API stubs ready to connect to the FastAPI routes (each is one uncomment away)

---

## Understanding the Frontend Files

The frontend is made of three files, each with exactly one job. A common beginner confusion: **JavaScript is not Java** — they are completely different languages that happen to share part of a name. JavaScript runs inside the browser and controls what the page does.

Think of building a house:

```
index.html  →  The structure   (walls, rooms, doors — what exists)
style.css   →  The appearance  (paint, furniture, lighting — what it looks like)
app.js      →  The behaviour   (electricity, plumbing — what it does)
```

None of these files is useful on its own. They only work as a set.

---

### index.html — Structure

HTML is the skeleton of the page. It is a list of **elements** (called tags) that describe what content exists. Every tag has an opening and a closing form:

```html
<div class="auth-card">           ← open a box, give it a name ("auth-card")
  <div class="auth-title">        ← open a smaller box inside it
    Card Not Recognized           ← the actual visible text
  </div>                          ← close the smaller box
</div>                            ← close the outer box
```

The `class="..."` attribute is just a label. On its own it does nothing — it is how `style.css` and `app.js` find and target that element.

The `id="..."` attribute works similarly, but must be **unique** — only one element per page can have a given id. JavaScript uses ids to update specific elements:

```html
<div id="clock-time">00:00</div>    ← JS updates this text every second
```

**In our file**, each screen is a `<div class="screen">` block. At any moment only one is visible — the others are hidden off-screen by CSS. JavaScript decides which one to show:

| Element id | What it is |
|---|---|
| `screen-idle` | "TAP YOUR CARD" screen |
| `screen-auth-failed` | Red error screen |
| `screen-main-menu` | Welcome + Borrow / Return buttons |
| `screen-borrow` | Device grid for borrowing |
| `screen-return` | Device grid for returning |
| `overlay-device-detail` | Full-screen device detail popup |
| `overlay-inactivity` | Countdown warning overlay |

---

### style.css — Appearance

CSS is a list of rules. Each rule says: **"find elements that match this selector, and apply these visual properties."**

```css
/* selector  ↓          property: value; */
.auth-title {
  font-size: 1.9rem;      /* text size */
  color: var(--danger);   /* colour — references a variable */
  text-align: center;
}
```

A **dot** (`.auth-title`) means "find every element whose class includes `auth-title`".
A **hash** (`#clock-time`) means "find the element with that exact id".

**CSS variables** at the top of the file are reusable values. Changing one line updates the whole UI:

```css
:root {
  --accent: #00d4ff;   /* change this one value → every cyan element updates */
  --danger: #ef4444;
}

/* used anywhere like this: */
color: var(--accent);
```

**Animations** describe movement over time:

```css
@keyframes ring-pulse {           /* define the movement */
  0%   { transform: scale(0.78); opacity: 0.7; }   /* starting state */
  100% { transform: scale(1.25); opacity: 0; }     /* ending state */
}

.nfc-ring-outer {
  animation: ring-pulse 2.8s ease-in-out infinite;
  /*                    ↑ duration   ↑ timing  ↑ loops forever */
}
```

**The clip-path trick** — how screen wipe transitions work:

```css
/* Every screen starts hidden — clipped 100% from the top */
.screen {
  clip-path: inset(100% 0 0 0);
}

/* When JS adds the "active" class, CSS animates to fully visible */
.screen.active {
  clip-path: inset(0% 0 0 0);
  transition: clip-path 0.72s cubic-bezier(0.76, 0, 0.24, 1);
}
```

When JavaScript adds the `active` class to a screen, the browser automatically animates from hidden → visible, creating the bottom-to-top wipe effect. JavaScript triggers it; CSS does the animation.

---

### app.js — Behaviour

JavaScript runs in the browser and reacts to events (clicks, timers, API responses). It can read and modify the HTML and CSS in real time.

**Finding an element:**
```js
document.getElementById('clock-time')      // find by id
document.querySelectorAll('.back-btn')     // find all elements with this class
```

**Changing content or style:**
```js
document.getElementById('clock-time').textContent = '14:32';  // change text
element.classList.add('active');      // add a CSS class  → triggers animation
element.classList.remove('active');   // remove a CSS class
```

This is the bridge between JS and CSS: JavaScript adds or removes class names; CSS defines what those class names look like. Every screen transition works this way.

**Reacting to user actions:**
```js
document.getElementById('btn-borrow').addEventListener('click', () => {
  openBorrow();    // this function runs when the button is clicked
});
```

**`async` / `await`** — talking to the server without freezing the page:
```js
async function apiGetDevices() {
  const res = await fetch('/api/devices');   // ask the FastAPI server
  return res.json();                          // convert the response to JS data
}
// "await" means: pause here until the server replies, then continue
```

**The state object `S`** is the memory of the entire app. Every important decision reads or writes it:
```js
const S = {
  screen:   'idle',   // which screen is currently showing
  user:     null,     // who is logged in  { id, name, role }
  devices:  [],       // list of devices loaded from the server
  selected: null,     // which device the user last tapped
  mode:     null,     // 'borrow' or 'return' — set when entering a grid screen
};
```

For example: `navigate('main-menu')` sets `S.screen = 'main-menu'` and updates the DOM. `confirmAction()` checks `S.mode` to know whether to call `apiBorrow` or `apiReturn`.

---

### How the Three Files Connect

```
Browser opens index.html
  │
  ├── <link href="style.css"> — browser loads and applies all CSS rules immediately
  │
  └── <script src="app.js">  — browser runs the JS once the page is ready
        JS on startup:
          - hides the inactivity / detail overlays
          - starts the clock (updates every second)
          - attaches click listeners to all buttons
          - waits for user interaction
```

When a button is tapped, the chain looks like this:

```
User taps "BORROW"
  → app.js listener fires → openBorrow() is called
      → navigate('borrow')       JS adds .active to #screen-borrow
          → CSS animates it in   clip-path transition plays automatically
      → apiGetDevices()          JS fetches /api/devices from FastAPI
          → buildGrid(...)       JS creates <div> cards and inserts them into index.html
              → CSS styles them  .device-card rules apply automatically to new elements
```

---

### Quick Reference — Where to Look to Change Something

| You want to... | File | Search for... |
|---|---|---|
| Change a colour | `style.css` | `:root {` at the very top |
| Change the font | `style.css` | `--font-display` or `--font-body` |
| Change animation speed | `style.css` | `transition:` or `animation:` on that element |
| Change button label text | `index.html` | the button's text content |
| Add a new screen | `index.html` + `app.js` | add a `<div class="screen">` and a navigate case |
| Change the borrow count display | `app.js` | `borrow-badge` |
| Connect to the real API | `app.js` | `apiAuthTap`, `apiGetDevices`, `apiBorrow`, `apiReturn` |
| Change the inactivity timeout | `app.js` | `cdSeconds: 120` in the `S` object |

---

### The `ui/` Folder

`smart_locker/smart_locker/ui/` contains only an empty `__init__.py` — it is a leftover placeholder from early project setup. The entire UI lives in `smart_locker/frontend/`. The `ui/` folder does nothing and can be safely ignored.

---

## Stage 9 — FastAPI REST API Layer (Built)

The REST API is implemented in `smart_locker/api/routes.py` with SSE event stream for NFC bridge.

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/session` | Check current session state |
| `POST` | `/api/session/end` | End the current session |
| `POST` | `/api/session/touch` | Reset the inactivity timer |
| `GET` | `/api/devices` | List all devices with status, borrower, and extended metadata |
| `POST` | `/api/devices/{id}/borrow` | Borrow a device |
| `POST` | `/api/devices/{id}/return` | Return a device (admins can return on behalf) |
| `GET` | `/api/events` | SSE stream — pushes card-tap, auth, and session events to the browser |

### Device response fields

`GET /api/devices` returns an array with these fields per device:

```json
{
  "id": 1,
  "pm_number": "PM-042",
  "name": "PM-042 Keysight DSOX3054T",
  "device_type": "Oscilloscope",
  "serial_number": "MY12345678",
  "manufacturer": "Keysight",
  "model": "DSOX3054T",
  "barcode": "4900123456789",
  "locker_slot": 3,
  "description": "4-channel 500MHz oscilloscope",
  "image_path": null,
  "calibration_due": "2026-09-15",
  "status": "available",
  "borrower_name": null
}
```

---

## Stage 10 — NFC → Frontend Event Bridge (Built)

The SSE event stream is implemented at `GET /api/events`. The NFC reader pushes events to an `asyncio.Queue` shared via the app context, and the SSE endpoint streams them to the browser.

### Event flow

```
NFC card tap
    ↓
Background NFC listener thread detects card → queues event
    ↓
GET /api/events  (SSE stream, browser is subscribed)
    ↓
Frontend receives event → triggers auth flow / session update
```

---

## Stage 11 — Static File Serving (Built)

FastAPI serves the frontend at `http://localhost:8000`. The `create_app()` factory in `smart_locker/api/server.py` mounts static files and serves `index.html` at the root.

---

## Stage 12 — Kiosk Deployment

Once all stages are complete, the system runs as a permanent installation on the locker PC.

### Auto-start on Windows boot

Create a Windows Task Scheduler task that runs on login:

```powershell
# Create a startup script: start_locker.bat
@echo off
cd /d "D:\Python Projects\smart_locker"
call venv\Scripts\activate
start /min python -m smart_locker.app
timeout /t 3
start chrome --kiosk --no-first-run --disable-infobars http://localhost:8000
```

Register it in Task Scheduler to run at logon (or as a Windows service using `pywin32`).

### Chromium kiosk flags

```powershell
chrome --kiosk `
  --no-first-run `
  --disable-infobars `
  --disable-session-crashed-bubble `
  --disable-features=TranslateUI `
  http://localhost:8000
```

- `--kiosk` — fullscreen, no address bar, no window chrome
- `--no-first-run` — skips the Chrome welcome screen
- `--disable-infobars` — suppresses "Chrome is being controlled" banner

### Prevent accidental exit

- Set Windows to auto-login to a dedicated `locker` user account
- Disable Task Manager shortcut (Ctrl+Alt+Del) via Group Policy for the kiosk user
- Set the desktop wallpaper to black so any accidental window close looks intentional

### Touch display calibration

If using a touch display, ensure the Windows touch driver is installed and calibrated:

```powershell
# Launch touch calibration tool
TabletPC.cpl
```

---

## Barcode Scanner Plan

The system stores a `barcode` value per device (imported from the "Barcode" column in the device Excel). This enables a future barcode scanner workflow for shared lockers.

### The Problem

Right now, 20 lockers hold 20 devices — one device per locker. But some device types have multiple units (e.g., 5 current probes). Using 5 lockers for 5 identical probes is wasteful. Instead, one locker can hold all probes, and a barcode scanner identifies which specific probe is being taken or returned.

### Planned Workflow

```
1. User taps NFC card → authenticated
2. User selects "Borrow" on touch display
3. User opens shared locker, picks up a device
4. User scans the device's barcode sticker with the USB scanner
5. System looks up devices.barcode → identifies the exact device
6. Borrow is recorded for that specific device
```

Same flow for returns: scan the barcode to identify which device is being put back.

### Implementation Steps

1. **Hardware**: USB barcode scanner plugged into the kiosk PC. Most scanners emulate a keyboard — they type the barcode digits followed by Enter.
2. **Frontend (`app.js`)**: Add a barcode input listener that detects rapid sequential keystrokes ending in Enter (the scanner's keyboard emulation pattern). Distinguish scanner input from regular keyboard typing by timing threshold (~50ms between characters).
3. **API**: Add a `GET /api/devices/barcode/{barcode}` endpoint that looks up a device by its barcode value.
4. **UI flow**: When a barcode is scanned during an active session in borrow/return mode, auto-open the device detail overlay for that device and prompt to confirm.

### What's Already in Place

- `devices.barcode` column stores barcode values (imported from Excel)
- The API `GET /api/devices` response includes the `barcode` field
- The Excel auto-sync exports barcode values

---

## Future Improvements (Not Yet Planned)
- **Calibration date notifications** — calibration dates are stored; a notification system can alert when devices are due for recalibration
- **Admin web panel** — browser-based interface for managing users, devices, and viewing transaction history
- **MIFARE sector data reading** — APDU commands are already defined in `nfc/apdu.py` but not wired into the auth flow
- **Multi-reader support** — currently only the first matching reader is used
- **Email / webhook notifications** — alert admins when a device is overdue or a borrow limit is hit
- **Device condition reporting** — let users flag damaged equipment on return
