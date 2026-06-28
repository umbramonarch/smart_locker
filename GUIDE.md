# Smart Locker — Setup & Usage Guide (Raspberry Pi)

This guide explains, in plain English, what the Smart Locker is, how it runs on a
Raspberry Pi 4, and exactly how to set it up from a blank SD card to a working kiosk.

---

## 1. What it is and how it runs

The Smart Locker is a small appliance for borrowing and returning equipment. A colleague
taps their NFC work card on a reader, then borrows or returns devices by touching a screen.
Everything is tracked in a local database; nobody needs a login or the internet.

It runs on a **Raspberry Pi 4** as a self-contained kiosk:

```
Raspberry Pi 4 (Raspberry Pi OS, 64-bit) — no internet needed at runtime
  ├─ pcscd .................. the Linux service that talks to the ACR1252U NFC reader
  ├─ /mnt/locker ............ the company M: drive, mounted over the network (CIFS/SMB)
  ├─ smart-locker service ... the Python backend: a small web server on port 8000
  │                           plus a background listener for card taps
  └─ Chromium (kiosk mode) .. a fullscreen browser on the touch display, showing
                              http://localhost:8000 — this is what users see and touch
```

Three things are worth understanding up front:

- **No internet at runtime.** The Pi only needs the company network to reach the **M:
  drive** (a normal Windows file share). Everything else runs locally. Installation pulls
  software from the SD card, not the web. (OneDrive is no longer involved — the M: drive
  is now the single place the device list lives.)
- **The M: drive is both source and destination.** The Pi *imports* the company device
  master list from M:, and *writes* an up-to-date Excel workbook back to M: so colleagues
  can read the current borrow/return state.
- **The database stays on the Pi.** The SQLite database lives on the Pi's local SD card,
  never on M: (network shares don't handle SQLite's locking reliably).

---

## 2. Hardware you need

1. **Raspberry Pi 4** (2 GB RAM or more) with a **64-bit Raspberry Pi OS** SD card.
2. **ACR1252U NFC reader** (USB).
3. The **Riverdi RVT101HVHNWC00** 10.1" capacitive touch display (HDMI for video + USB for
   touch). Capacitive touch works out of the box on Linux — no calibration step needed.
4. NFC work cards (MIFARE Classic, Ultralight, NTAG, DESFire — any card with a UID).
5. Network access to the company **M:** share (wired Ethernet is most reliable).

---

## 3. Two ways to install

**Fast path (recommended for production):** copy the project onto the Pi and run one script
that sets up everything. See **Section 3a**.

**Manual path (recommended the first time, to understand each piece):** do each step by
hand. See **Section 4**.

Either way, you finish by filling in a few secrets (encryption keys, the M: share login)
and enrolling your first card. But **first the Pi needs an operating system** — Step 0.

### Step 0 — Prepare the SD card (install Raspberry Pi OS)

A Raspberry Pi 4 ships with **no operating system** — you write one onto the SD card
yourself. Do this on any PC (including your work laptop) with the free **Raspberry Pi
Imager** (https://www.raspberrypi.com/software/); the Pi doesn't need to be present yet.

In the Imager:

1. **Device:** Raspberry Pi 4.
2. **Operating System:** **Raspberry Pi OS (64-bit)** — the standard **Desktop** edition.
   - *64-bit* matches what this project is built for (the Python packages have prebuilt
     64-bit wheels; 32-bit would force slow on-device compiles).
   - *Desktop*, **not** *Lite* — the kiosk runs Chromium in a graphical session, which the
     Lite (no-GUI) edition does not have.
3. **Storage:** a 32 GB or larger SD card.
4. Open the **⚙ settings** ("Edit Settings", the gear icon) **before** writing, and set:
   - a **hostname** (e.g. `smartlocker`),
   - **enable SSH** (lets you finish setup from another computer),
   - a **username and password** — it can be `locker`, but any name works (`install.sh`
     auto-detects whichever user owns the project folder),
   - **WiFi and locale**, if you will use WiFi.
5. Write the card, insert it into the Pi, and power on.

**About the internet:** the "no internet" rule is only for *running* in the company. During
this **one-time setup** you will want to give the Pi internet (a home or test network) so it
can install its system packages and build the Python environment. After setup it runs fully
offline.

**Installing onto several Pis (the "SD card" model):** set up **one** Pi completely and
confirm it works, then **clone its SD card to an image** and write that image onto the other
units' cards. That golden image already contains the OS, the app, the Python environment, and
your settings — so the others need no internet at all.

> Tip: current Raspberry Pi OS may run the desktop under Wayland. If the kiosk autostart
> misbehaves, switch to X11 with `sudo raspi-config` → *Advanced Options* → *Wayland* → *X11*,
> then reboot.

### 3a. Fast path — the install script

Put the project on the Pi (for example at `/home/locker/smart_locker`) and run:

```bash
sudo deploy/install/install.sh
```

This is safe to re-run. It installs the system packages, builds the Python environment,
enables the NFC service, installs the auto-start service and the kiosk browser, and
scaffolds the M: mount. When it finishes it prints the few manual steps that remain
(filling `.env`, the share login, enrolling a card). Those are covered below.

> **Offline note:** the company Pi has no internet. Run
> `deploy/install/build-wheelhouse.sh` **once on a Pi (or aarch64 machine) that does have
> internet** to download all Python packages into `deploy/wheelhouse/`, then bake that into
> the SD image. `install.sh` installs from there automatically when offline. See
> `deploy/README.md`.

Then jump to **Section 4.4** (keys & `.env`), **4.5** (mount M:), **4.6**–**4.8** (database,
admin card, devices) and **Section 5** (kiosk autostart).

---

## 4. Step-by-step setup (manual)

These steps assume a terminal on the Pi and the project at `~/smart_locker`.

### 4.1 Get the code onto the Pi

Copy the project folder to the Pi (USB stick, `scp`, or the SD image already contains it).
Open a terminal in the project folder:

```bash
cd ~/smart_locker
```

### 4.2 System packages and the NFC reader

The NFC reader talks to Linux through the **PC/SC daemon** (`pcscd`) plus the CCID driver.
You also need the CIFS tools (for the M: mount) and Chromium (for the kiosk display):

```bash
sudo apt update
sudo apt install -y pcscd pcsc-tools libccid cifs-utils chromium unclutter curl python3-venv
sudo systemctl enable --now pcscd
```

Plug in the ACR1252U and confirm Linux sees it:

```bash
pcsc_scan          # should list "ACS ACR1252..."; press Ctrl-C to stop
```

If it isn't listed, see **Section 8 (Troubleshooting)**.

### 4.3 Python environment

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Verify the reader is reachable from Python:

```bash
python -c "from smartcard.System import readers; print(readers())"
# Expected (reader plugged in):
# ['ACS ACR1252 Dual Reader PICC 0', 'ACS ACR1252 Dual Reader SAM 0']
```

An empty list `[]` means the reader isn't plugged in or `pcscd` isn't running.

### 4.4 Encryption keys and the `.env` file

The system encrypts every card UID. Generate the two keys:

```bash
python -m scripts.generate_key
```

Create your `.env` from the Pi template, then paste the keys into it:

```bash
cp deploy/.env.pi.example .env
nano .env          # paste SMART_LOCKER_ENC_KEY and SMART_LOCKER_HMAC_KEY
```

The template already points the Excel paths at the M: mount (`/mnt/locker/...`) and keeps
the database local. Adjust the `SMART_LOCKER_SOURCE_EXCEL_PATH` filename to match your real
workbook. **Keep `.env` secret** — it holds the encryption keys (it is already gitignored).

### 4.5 Mount the M: network share (CIFS)

The Pi reaches the M: drive as a CIFS (SMB) network mount at `/mnt/locker`.

1. Create the mount point and a root-only credentials file:

   ```bash
   sudo mkdir -p /mnt/locker
   sudo install -d -m 700 /etc/smart-locker
   sudo cp deploy/mount/cifs-credentials.example /etc/smart-locker/cifs-credentials
   sudo nano /etc/smart-locker/cifs-credentials      # real username / password / domain
   sudo chmod 600 /etc/smart-locker/cifs-credentials
   ```

2. Add the mount line to `/etc/fstab`. Copy the line from `deploy/mount/fstab.snippet` and
   replace `//SERVER/share` with the real share path and the `uid`/`gid` with the locker
   user's ids (`id <user>`):

   ```bash
   sudo nano /etc/fstab        # paste & edit the line from deploy/mount/fstab.snippet
   sudo systemctl daemon-reload
   sudo mount /mnt/locker
   ls /mnt/locker              # you should see the company files
   ```

The line uses `nofail` and `x-systemd.automount`, so the Pi still boots and the kiosk still
works even if the share is temporarily unreachable — it just can't import/export until the
share comes back.

### 4.6 Initialize the database

```bash
python -m scripts.init_db
# Expected: Database initialized successfully.
```

This creates `smart_locker.db` with four tables: `users`, `registrants`, `devices`,
`transaction_logs`.

### 4.7 Enroll your first (admin) card

With the reader plugged in:

```bash
python -m scripts.enroll_card --name "Your Name" --role admin
```

When you see `Place card on reader...`, tap your card and hold it steady for 1–2 seconds.
The card UID is masked in the output (e.g. `A1****D4`) and stored encrypted — only admins
can ever decrypt it. Enroll regular users the same way with `--role user`.

### 4.8 Load devices from the M: Excel list

The company device master list lives on M:. Import it (it filters to the locker/"schrank"
rows and auto-numbers slots 1…N). German and English column headers are auto-detected.

```bash
# Preview without writing anything:
python -m scripts.import_devices --file "/mnt/locker/Messmittelliste.xlsx" --dry-run

# Import for real:
python -m scripts.import_devices --file "/mnt/locker/Messmittelliste.xlsx"
```

| Excel column | German | Maps to | Required? |
|---|---|---|---|
| Equipment | Equipment | `pm_number` | **Yes** — the device identifier |
| Category | Kategorie | `device_type` | No |
| Description | Beschreibung | `description` | No |
| Manufacturer | Hersteller | `manufacturer` | No |
| Type designation | Typbezeichnung | `model` | No |
| Serial number | Hersteller-serialnummer | `serial_number` | No |
| Barcode | Barcode | `barcode` | No |
| Locker placement | Platz Messmittelschrank | `locker_slot` | No |
| Calibration date | Datum der nächsten Kalibrierung | `calibration_due` | No |

If auto-detection picks the wrong column, override it, e.g.
`--pm-col "Equipment" --type-col "Kategorie"`. Re-importing is safe — devices are matched by
PM number, and a re-import **never** overwrites `locker_slot`, `image_path`, `description`,
`status`, or `borrower`.

Once running as a service, this same import also happens **automatically**: once on startup,
once a day at 06:00, and on demand from the hidden admin panel. (See Section 7 for why the
live "watch the file" mode is off for network shares.)

### 4.9 Add device photos

Photos make the touch UI easier to use. Two ways to attach them — both copy the image into
`smart_locker/frontend/images/` and link it to the matching device(s):

```bash
# By PM number — list devices, then assign:
python -m scripts.update_device --list
python -m scripts.update_device --auto          # auto-match PM-001.jpg, PM-002.png, ...
python -m scripts.update_device --pm PM-042 --image scope.jpg --description "4-ch 500MHz scope"
```

Or drop images into the **photo folder** set by `SMART_LOCKER_PHOTO_INPUT_PATH`, named after
the device **model** (e.g. `87V.jpg` applies to every "87V" device). If that folder is on
the M: share, photos present at startup are applied automatically; photos added later are
picked up on the next restart or by re-running `update_device --auto`.

### 4.10 Run it (test before making it permanent)

```bash
python -m smart_locker.app
```

You'll see the backend start, the NFC reader come up, and the web server bind to port 8000.
Open `http://localhost:8000` in a browser on the Pi to see the kiosk UI. Press `Ctrl+C` to
stop. (Use `python -m smart_locker.app --cli` for a console-only NFC loop with no web UI.)

When that works, make it permanent — Section 5.

---

## 5. Run as a kiosk appliance (autostart on boot)

In production the Pi should boot straight into the kiosk with no keyboard. Two pieces do
this, and `deploy/install/install.sh` sets up both:

1. **The backend service** (`smart-locker.service`) — starts the web server + NFC listener
   on boot and restarts it automatically if it ever crashes.
2. **The kiosk browser** (`start-kiosk.sh`, launched by an autostart entry) — opens Chromium
   fullscreen at `http://localhost:8000` on the touch display once the desktop logs in.

If you used the fast path, both are already installed. Start the backend and reboot to test
the full cold-boot experience:

```bash
sudo systemctl start smart-locker
sudo reboot
```

After the reboot the Pi should come up directly into the fullscreen kiosk. Useful commands:

```bash
systemctl status smart-locker        # should say: active (running)
sudo systemctl restart smart-locker  # restart the backend
journalctl -u smart-locker -f        # follow the backend log live
```

**Boot to the desktop automatically:** make sure Raspberry Pi OS auto-logs into the desktop
session for your kiosk user (`sudo raspi-config` → *System Options* → *Boot / Auto Login* →
*Desktop Autologin*). The kiosk autostart entry runs in that session.

**Display orientation (Riverdi):** the capacitive touch works out of the box. If the picture
is rotated, set the display rotation in `/boot/firmware/config.txt` (e.g. `display_rotate=`
or a `video=` line) and reboot.

---

## 6. Day-to-day: how the kiosk is used

### Session flow (tap-and-go)

The card is **tapped and removed** — it is not left on the reader. Its only job is to
authenticate. After that, everything happens on the touch display.

1. **Tap your card** → the reader reads the UID → the system authenticates you → the welcome
   screen appears.
2. **Use the touch display** → borrow devices, return devices, view device info.
3. **The session ends** via the **End Session** button, a **second card tap**, or the
   **inactivity timeout** (120 seconds of no touch — a silent security backstop).

### The screens

- **Idle** — animated NFC ring, "Tap your card", live clock, a "Register your card" entry.
- **Register (self-service)** — search and pick your approved name, then tap your card to
  enrol it under that name.
- **Authentication failed** — red "Card Not Recognized", auto-returns to idle.
- **Main menu** — "Welcome, [Name]!" with **Borrow**, **Return**, **End Session**.
- **Borrow** — a grid of devices by locker slot; available ones are tappable, borrowed ones
  show who has them.
- **Return** — the same grid, with your own borrowed items highlighted.
- **Device detail** (overlay) — photo, specs, and a confirm button.
- **Inactivity warning** (overlay) — a countdown with a "Stay Active" button.
- **Hidden admin panel** (overlay) — opened by tapping the idle clock 5 times. Shortcuts for
  Borrow, Return, **Sync source**, Register user, **Export to Excel**, End Session.

### The rules

- **Borrow limit:** each user can hold up to `SMART_LOCKER_MAX_BORROWS` devices (default 5).
- **Returns:** only the borrower can return their own device; an admin can return any device
  on anyone's behalf (the log records both people).
- **Open-access locker:** there is no physical lock — the system tracks *who has what*.

---

## 7. Excel, the M: share, and the dashboard

There are two ways to see live data — a web dashboard and the Excel workbook on M:.

**Web dashboard** — open `http://<pi-address>:8000/dashboard` from any browser on the
network (no login). It shows three tables, auto-refreshing every 30 seconds: **Devices**
(slot, PM number, status, borrower, calibration due — filterable/sortable), **Transactions**
(last 500), and **Users**.

**Excel on M:** the device list is **imported from** M:, and an up-to-date workbook
(Devices + Transactions + Users) is **written back to** M: at `SMART_LOCKER_EXCEL_PATH`.
With `SMART_LOCKER_EXCEL_AUTO_EXPORT=1` (set in the Pi template), that workbook is refreshed
automatically after every source import — on startup, at the daily 06:00 import, and
whenever an admin uses **Sync source**. You can also download a snapshot any time from the
admin panel's **Export to Excel**.

**Why the import is scheduled, not instant:** the Pi can't reliably get a "file changed"
notification for a file that lives on a network share (the Linux mechanism for this,
*inotify*, doesn't see edits made by other computers on a CIFS/SMB mount). So instead of a
live file-watch, the system imports on startup and once a day at 06:00. To pull changes in
immediately, use **Sync source** in the admin panel, or run
`python -m scripts.sync_source`.

---

## 8. Logs and troubleshooting

**Logs** are in two places:

```bash
journalctl -u smart-locker -n 100 --no-pager   # the service's output (systemd journal)
tail -f logs/smart_locker.log                  # the app's own rotating log file
```

**The NFC reader isn't detected (`pcsc_scan` shows nothing):**
- Confirm `pcscd` is running: `sudo systemctl status pcscd`.
- Re-seat the USB cable; try a different USB port.
- A kernel NFC module can grab the reader. If so, blacklist it:
  `echo -e "blacklist pn533\nblacklist pn533_usb\nblacklist nfc" | sudo tee /etc/modprobe.d/blacklist-nfc.conf` then reboot.
- The app's error message will tell you the Linux fix: `sudo systemctl start pcscd`.

**The M: share won't mount:**
- `sudo mount /mnt/locker` prints the error. A "permission denied" usually means the
  credentials file is wrong; a "host is down"/timeout means the network or server path is
  wrong.
- Try a different SMB version in the fstab line: `vers=3.0` → `vers=2.1` → `vers=1.0`.
- Check the server path with another machine first (`\\SERVER\share` in Windows Explorer).

**The kiosk screen doesn't appear after boot:**
- Confirm the desktop auto-login is on (Section 5) and the autostart entry exists:
  `~/.config/autostart/smart-locker-kiosk.desktop`.
- Run the launcher by hand to see errors: `deploy/kiosk/start-kiosk.sh`.
- Confirm the backend is up first: `curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/` should print `200`.

**The backend won't start:** `journalctl -u smart-locker -n 50 --no-pager`. The most common
cause is a missing or malformed `.env` (encryption keys not pasted in).

---

## 9. Tests

The test suite needs no NFC hardware (it uses in-memory SQLite and mock data):

```bash
python -m pytest tests/ -v          # all tests
python -m pytest tests/test_security.py -v
```

---

## 10. Configuration reference

All settings live in `.env` (loaded by `config/settings.py`). The Pi template
`deploy/.env.pi.example` pre-fills sensible values.

| Variable | Default | Description |
|---|---|---|
| `SMART_LOCKER_ENC_KEY` | (required) | AES-256-GCM key, base64 — from `generate_key` |
| `SMART_LOCKER_HMAC_KEY` | (required) | HMAC-SHA256 key, base64 — from `generate_key` |
| `SMART_LOCKER_DB_PATH` | `smart_locker.db` | SQLite path — keep on the Pi's local disk |
| `SMART_LOCKER_READER_NAME` | `ACR1252` | Substring filter for the NFC reader name |
| `SMART_LOCKER_SESSION_TIMEOUT` | `120` | Idle session timeout (seconds) |
| `SMART_LOCKER_MAX_BORROWS` | `5` | Max devices a user can hold at once |
| `SMART_LOCKER_API_HOST` | `0.0.0.0` | Web server bind address |
| `SMART_LOCKER_API_PORT` | `8000` | Web server port |
| `SMART_LOCKER_SOURCE_EXCEL_PATH` | (empty) | Device master list on M: to import; empty disables auto-import |
| `SMART_LOCKER_EXCEL_PATH` | `smart_locker_data.xlsx` | Where the exported workbook is written (the M: path on the Pi) |
| `SMART_LOCKER_EXCEL_AUTO_EXPORT` | (off) | `1` = auto-refresh the exported workbook after each import/photo change |
| `SMART_LOCKER_SOURCE_SYNC_HOUR` / `_MINUTE` | `6` / `0` | Daily source-import time (24h) |
| `SMART_LOCKER_PHOTO_INPUT_PATH` | (empty) | Folder watched for device photos; empty disables |

---

## 11. What's built vs. what's next

**Built:** NFC enrollment & authentication (AES-256-GCM + HMAC), single-user sessions with
timeout, device tracking with the full schema, borrow/return with admin overrides and
per-user limits, self-service registration, Excel import (schrank filter, DE/EN headers) and
on-demand/auto export, photo assignment, the read-only `/dashboard`, the FastAPI REST API +
SSE bridge, the 6-screen kiosk UI, **Raspberry Pi appliance deployment** (systemd service,
CIFS mount, Chromium kiosk, offline install), and 132 hardware-free tests.

**Next:** barcode scanner for shared lockers (Section 14), calibration-due notifications, a
full admin web panel, MIFARE sector reading, and multi-reader support.

---

## 12. Understanding the frontend files

The frontend is three files, each with one job. A common beginner confusion: **JavaScript is
not Java** — they are different languages that happen to share part of a name. JavaScript
runs inside the browser and controls what the page does.

Think of building a house:

```
index.html  →  The structure   (walls, rooms, doors — what exists)
style.css   →  The appearance  (paint, furniture, lighting — what it looks like)
app.js      →  The behaviour   (electricity, plumbing — what it does)
```

None of these files is useful on its own. They only work as a set.

### index.html — structure

HTML is the skeleton of the page: a list of **elements** (tags) describing what content
exists. Every tag has an opening and a closing form:

```html
<div class="auth-card">           <!-- open a box, give it a name ("auth-card") -->
  <div class="auth-title">        <!-- a smaller box inside it -->
    Card Not Recognized           <!-- the visible text -->
  </div>
</div>
```

The `class="..."` attribute is just a label — it does nothing by itself; it's how
`style.css` and `app.js` find that element. The `id="..."` attribute is similar but must be
**unique** (one element per page). Each screen is a `<div class="screen">`; only one is
visible at a time, and JavaScript decides which:

| Element id | What it is |
|---|---|
| `screen-idle` | "Tap your card" screen |
| `screen-register` | Self-service registration |
| `screen-auth-failed` | Red error screen |
| `screen-main-menu` | Welcome + Borrow / Return |
| `screen-borrow` | Device grid for borrowing |
| `screen-return` | Device grid for returning |
| `overlay-device-detail` | Device detail popup |
| `overlay-inactivity` | Countdown warning |
| `overlay-admin` | Hidden admin panel (5× clock tap) |

### style.css — appearance

CSS is a list of rules: *"find elements that match this selector, apply these visual
properties."* A **dot** (`.auth-title`) matches a class; a **hash** (`#clock-time`) matches
an id. **CSS variables** at the top let you change the whole look in one line:

```css
:root {
  --accent: #009641;   /* Phoenix Contact green — change once, the whole UI follows */
  --danger: #ef4444;
}
color: var(--accent);
```

Screen transitions use a `clip-path` trick: every screen starts clipped (hidden); when
JavaScript adds the `active` class, CSS animates it into view (a bottom-to-top wipe).
JavaScript triggers it; CSS does the animation.

### app.js — behaviour

JavaScript reacts to events (clicks, timers, server replies) and can read/modify the HTML
and CSS live. It finds elements (`document.getElementById(...)`), changes them
(`element.classList.add('active')`), and talks to the server without freezing the page using
`async`/`await`:

```js
async function apiGetDevices() {
  const res = await fetch('/api/devices');   // ask the backend
  return res.json();                          // turn the reply into JS data
}
```

The state object `S` is the app's memory — `{ screen, user, devices, selected, mode }`. Every
important decision reads or writes it.

### How they connect

```
User taps "BORROW"
  → app.js listener → openBorrow()
      → navigate('borrow')   JS adds .active to #screen-borrow → CSS wipes it in
      → apiGetDevices()      JS fetches /api/devices → builds the grid of cards
```

### Where to look to change something

| You want to... | File | Search for... |
|---|---|---|
| Change a colour | `style.css` | `:root {` at the top |
| Change the font | `style.css` | `--font-display` / `--font-body` |
| Change a button label | `index.html` | the button's text |
| Change the borrow count display | `app.js` | `borrow-badge` |
| Change the inactivity timeout (UI) | `app.js` | `cdSeconds` in the `S` object |

---

## 13. The REST API

The backend API is in `smart_locker/api/routes.py`, with a Server-Sent Events (SSE) stream
that bridges card taps to the browser.

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/session` | Current session state |
| `POST` | `/api/session/end` | End the session |
| `POST` | `/api/session/touch` | Reset the inactivity timer |
| `GET` | `/api/devices` | All devices with status, borrower, metadata |
| `POST` | `/api/devices/{id}/borrow` | Borrow a device |
| `POST` | `/api/devices/{id}/return` | Return a device (admins on behalf) |
| `POST` | `/api/register` | Start self-registration (validates the name) |
| `POST` | `/api/register/cancel` | Cancel a pending self-registration |
| `GET` | `/api/registrants` | Approved names for self-registration |
| `POST` | `/api/admin/session` | Start the hidden admin-panel session |
| `POST` | `/api/admin/register` | Admin manual enrolment (skips name check) |
| `POST` | `/api/admin/sync-source` | Trigger the source Excel import now |
| `GET` | `/api/admin/export-excel` | Download the full database as `.xlsx` |
| `GET` | `/api/dashboard/devices` | Public device inventory (no auth) |
| `GET` | `/api/dashboard/transactions` | Public transaction history, last 500 |
| `GET` | `/api/dashboard/users` | Public registered-users list |
| `GET` | `/api/events` | SSE stream — card-tap, auth, and session events |

`GET /api/devices` returns per device: `id`, `pm_number`, `name`, `device_type`,
`serial_number`, `manufacturer`, `model`, `barcode`, `locker_slot`, `description`,
`image_path`, `calibration_due`, `status`, `borrower_name`.

**The NFC → browser bridge:** the background NFC listener detects a tap and puts an event on
a queue; `GET /api/events` streams it to the browser, which then runs the auth/registration
flow. FastAPI serves the kiosk UI (`index.html`) and the dashboard as static files from
`smart_locker/api/server.py`.

---

## 14. Barcode scanner plan (not yet built)

Each device stores a `barcode` value (imported from the Excel "Barcode" column). The planned
use: a **shared locker** holds several identical devices (e.g. 5 current probes) instead of
one per slot, and a USB barcode scanner identifies the specific unit being taken or returned.

The flow would be: tap NFC → choose Borrow/Return → scan the device barcode → the system
matches `devices.barcode` → the transaction is recorded for that exact unit. Implementation:
a barcode listener in `app.js` (USB scanners type the digits then Enter) plus a
`GET /api/devices/barcode/{barcode}` endpoint. The barcode field is already imported and
included in the API and the export.

---

## 15. Future improvements

- **Calibration-due notifications** — calibration dates are stored; a reminder system is not.
- **Full admin web panel** — edit users/devices from the browser (today: read-only dashboard
  + the kiosk's hidden admin panel).
- **MIFARE sector reading** — APDU commands exist in `nfc/apdu.py` but aren't wired in.
- **Multi-reader support** — currently the first matching reader is used.
- **Email / webhook alerts** — overdue devices, borrow-limit hits.
- **Device condition reporting** — let users flag damaged equipment on return.
