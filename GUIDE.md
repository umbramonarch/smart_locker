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

| Device Name       | Serial Number | Type        | Locker | Description             | Image          |
|-------------------|---------------|-------------|--------|-------------------------|----------------|
| Multimeter        | SN-001        | measurement | 1      | Fluke 87V digital meter | multimeter.jpg |
| Oscilloscope      | SN-002        | measurement | 2      | Rigol DS1054Z 50MHz     | oscilloscope.jpg |
| Soldering Station | SN-003        | tool        | 3      | Hakko FX-888D           | soldering.jpg  |

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

This starts the NFC reader listener and the main event loop. Tap your enrolled card to authenticate, tap again to log out.

Expected terminal output:
```
Smart Locker starting...
NFC reader ready: ACS ACR1252 Dual Reader PICC 0
Smart Locker ready. Tap your card to begin.
Press Ctrl+C to exit.
```

> **Note:** The FastAPI REST API and browser UI serving are not yet wired up. The terminal is the current interface for testing. Once the API layer is built (Stage 9), the browser kiosk UI will be served at `http://localhost:8000`.

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
# Run all 52 tests
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

## What's Built vs. What's Next

### ✅ Built — Backend Core

- NFC card UID reading (any card type with a UID)
- Card enrollment with AES-256-GCM encrypted storage
- User authentication via HMAC-SHA256 card fingerprint lookup
- Session management with inactivity timeout
- Device tracking (available / borrowed / maintenance states)
- Transaction logging (borrow / return, with admin-return attribution)
- Admin vs. regular user roles with permission enforcement
- Borrow limit enforcement (default 5 devices per user, configurable)
- UID masking in logs and on screen (never displayed in full)
- Reader connect/disconnect detection and retry logic
- Bulk device import from Excel (name, serial, type, slot, description, image)
- 52 unit tests — all passing, no NFC hardware required

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

## Stage 9 — FastAPI REST API Layer (Next to Build)

The backend services are fully implemented. What's missing is the HTTP layer that lets the frontend call them.

**Files to create:** `smart_locker/api/routes.py` · `smart_locker/api/schemas.py`

### Endpoints to implement

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/auth/tap` | Receive `uid_hmac` from NFC listener, authenticate, start session. Returns `{ user: { id, name, role } }` |
| `GET` | `/api/devices` | Return all devices with status and current borrower name. Returns array of device objects |
| `POST` | `/api/borrow` | Borrow a device for the current session user. Body: `{ device_id }` |
| `POST` | `/api/return` | Return a device. Body: `{ device_id }`. Admins can return any device |
| `POST` | `/api/session/end` | End the current session, clear session state |
| `GET` | `/api/session` | Return current session info: user, borrow count, time remaining |
| `GET` | `/api/events` | SSE stream — pushes `card-tap` events to the frontend when NFC detects a card |

### How to connect app.js to the real API

In `smart_locker/frontend/app.js`, each API function has the real `fetch()` call commented out directly above the demo code. Once the routes exist:

1. Open `app.js`
2. Find the function (e.g. `apiGetDevices`)
3. Uncomment the `fetch()` block
4. Delete the `await sleep(...)` and demo return below it

Example — `apiGetDevices` before and after:

```js
// BEFORE (demo mode):
async function apiGetDevices() {
  await sleep(280);
  return DEMO_DEVICES;
}

// AFTER (real API):
async function apiGetDevices() {
  const res = await fetch('/api/devices');
  return res.json();
}
```

### Session storage

The FastAPI API needs to hold the active session in memory between requests. The existing `UserSession` and `SessionManager` classes in `smart_locker/auth/session_manager.py` handle this — the API routes just need to call into them.

---

## Stage 10 — NFC → Frontend Event Bridge

The NFC reader runs as a background thread. When a card is tapped, the frontend needs to know immediately so it can trigger the auth flow. The recommended approach is **Server-Sent Events (SSE)**.

### How it works

```
NFC card tap
    ↓
Background NFC listener thread detects card
    ↓
Computes uid_hmac from card UID
    ↓
Pushes event to an internal asyncio queue
    ↓
GET /api/events  (SSE stream, browser is subscribed)
    ↓
Frontend receives { type: "card-tap", uid_hmac: "..." }
    ↓
app.js calls apiAuthTap(uid_hmac) → POST /api/auth/tap
    ↓
Navigate to main-menu screen
```

### Frontend change needed in app.js

Replace the debug-button-only tap handler with an SSE subscriber:

```js
// Connect to the NFC event stream on page load
const eventSource = new EventSource('/api/events');
eventSource.addEventListener('card-tap', e => {
  const { uid_hmac } = JSON.parse(e.data);
  handleTap(uid_hmac);   // existing function, just pass the real hmac
});
```

The `handleTap` function in `app.js` already accepts `uid_hmac` as an argument and calls `apiAuthTap(uid_hmac)` — no other changes needed in the frontend.

---

## Stage 11 — Static File Serving (FastAPI)

The frontend files need to be served by FastAPI so the browser can load them at `http://localhost:8000`.

Add to `smart_locker/app.py`:

```python
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# Serve frontend files
app.mount("/static", StaticFiles(directory="smart_locker/static"), name="static")

@app.get("/")
def serve_frontend():
    return FileResponse("smart_locker/frontend/index.html")

# Serve style.css and app.js from the frontend folder
app.mount("/", StaticFiles(directory="smart_locker/frontend"), name="frontend")
```

After this, opening `http://localhost:8000` in the browser loads the kiosk UI automatically.

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

## Future Improvements (Not Yet Planned)

- **Admin web panel** — browser-based interface for managing users, devices, and viewing transaction history without database access
- **MIFARE sector data reading** — APDU commands are already defined in `nfc/apdu.py` but not wired into the auth flow
- **Multi-reader support** — currently only the first matching reader is used
- **Email / webhook notifications** — alert admins when a device is overdue or a borrow limit is hit
- **Device condition reporting** — let users flag damaged equipment on return
