# Smart Locker — Setup & Usage Guide (Raspberry Pi)

This guide is the setup and operating notes for the Smart Locker on a Raspberry Pi 4,
including a fully offline install.

---

## 1. What it is and how it runs

The Smart Locker is a small appliance for borrowing and returning equipment. A colleague
taps their NFC work card on a reader, then either taps an NFC sticker on the device (same
reader) or picks the unit on the touch screen. Everything is tracked in a local database;
nobody needs a login or the internet.

It runs on a **Raspberry Pi 4** as a self-contained kiosk:

```
Raspberry Pi 4 (Raspberry Pi OS, 64-bit) — no internet needed at runtime
  ├─ pcscd .................. the Linux service that talks to the ACR1252U NFC reader
  ├─ /mnt/locker ............ locker file share, mounted over the network (CIFS/SMB)
  ├─ smart-locker service ... the Python backend: a small web server on port 8000
  │                           plus a background listener for card taps
  └─ Chromium (kiosk mode) .. a fullscreen browser on the touch display, showing
                              http://localhost:8000 — this is what users see and touch
```

Three things are worth understanding up front:

- **No internet at runtime.** The Pi only needs the company LAN to reach the locker file
  share. Everything else runs locally. Installation comes from the USB stick, not the web.
- **The share is both source and destination.** The Pi imports catalog fields from
  `device-list.xlsx` and writes locker **Aktueller Einsatzort** back into that same file.
  Download Excel (`smart_locker_data.xlsx`) is an on-demand snapshot.
- **The database stays on the Pi.** SQLite lives on the SD card. Do not put it on the
  network share — locking over CIFS is unreliable.

If your Pi will genuinely **never** touch a network except the locker share — not
even briefly, not even at your own desk — read Section 3 carefully before you do anything
else. Every "just apt install X" or "just pip install Y" instinct needs a different answer
in that scenario, and this guide is written for it.

---

## 2. Hardware you need

1. **Raspberry Pi 4** (2 GB RAM or more) with a **64-bit Raspberry Pi OS** SD card.
2. **The official Raspberry Pi 4 power supply (5V/3A, 15W, USB-C).** This matters more
   than it sounds like it should: the Pi 4 does **not** do USB-C Power Delivery
   negotiation — it needs a source that presents a plain, high-current 5V by default. Most
   phone/laptop chargers either cap around 1–2A or only unlock more current through a PD
   handshake the Pi never initiates. That's fine for the bare board with nothing plugged
   in, but once a USB hub, an NFC reader, and a touch display's touch controller are all
   drawing through the Pi's own USB rail, an under-specified supply is a real cause of the
   under-voltage warning icon, random reboots, or SD card corruption — especially at boot,
   when everything initializes at once. Don't substitute a phone charger once peripherals
   are attached, even temporarily.
3. **ACR1252U NFC reader** (USB).
4. The **Riverdi RVT101HVHNWCA0** 10.1" capacitive touch display (HDMI for video, USB-C
   for touch only). Capacitive touch works out of the box on Linux — no calibration step
   needed. **This display needs its own separate power supply** (a 7–14V DC barrel-jack
   input) — the video/touch connections do *not* power the panel or its backlight. Make
   sure you have that power adapter; it's easy to miss since it's not mentioned on the box
   as prominently as the HDMI/USB connections.
5. If your reader and display's USB-C touch connector need more USB-A ports than the Pi
   has free, a small **USB hub** works fine — the Pi's combined USB budget across all
   ports (1200 mA) comfortably covers a reader (~200 mA rated) plus a touch controller
   (well under 100 mA for this class of device) with a lot of headroom to spare. A
   bus-powered (no separate power adapter) hub is fine current-wise for this combination;
   just don't add other high-draw devices to the same hub. A **Delock 64272** (4-port,
   bus-powered) is a known-good example if you want a specific model to buy rather than
   picking one yourself.
6. NFC work cards (MIFARE Classic, Ultralight, NTAG, DESFire — any card with a UID).
   Cheap NTAG stickers on devices use the same reader (on-metal / ferrite tags on metal
   chassis).
7. Network access to the company locker share (wired Ethernet is most reliable) —
   **connected last**, after everything else is working. See Section 6.

**A note on the OS account password:** whatever you choose for the Pi's login, treat it
like any other credential on a device that will sit in a shared company location — avoid
anything that's a simple variation on the username or product name.

---

## 3. Two ways to install

**Fast path (recommended for production):** copy the project onto the Pi and run one script
that sets up everything. See **Section 3c**.

**Manual path (recommended the first time, to understand each piece):** do each step by
hand. See **Section 4**.

Either way, you finish by filling in a few secrets (encryption keys, the share login)
and enrolling your first card. But **first the Pi needs an operating system and an offline
install kit** — Steps 0 and 0b, below.

### The physical sequence, start to finish (read this first)

It's very easy to mix up "the SD card" and "a USB stick" — they are **two different
physical objects with two completely different jobs**, used at different times. Here's the
whole flow before the detailed steps, specifically to avoid that mix-up:

**Part A — entirely on your Windows PC. The Pi is not touched yet.**

1. Flash the SD card **once**, using Raspberry Pi Imager (Step 0, below). This SD card now
   contains the Pi's entire operating system. **You do not format it or touch it again**
   after this — it goes straight from the Imager into the Pi.
2. Still on your PC, with that SD card already set aside: build the offline wheelhouse and
   download the `pyscard` `.deb` (Step 0b, below).
3. Format **one ordinary USB flash drive** (just one — not the SD card, a completely
   different physical object) as exFAT, and copy the whole project folder plus the
   wheelhouse and the `.deb` onto it (also Step 0b). You only ever need this single stick;
   it plugs into any regular USB-A port on the Pi's side, not any special slot.

At the end of Part A you're holding two separate physical items: the flashed SD card, and a
USB stick full of files. Neither has touched the Pi yet.

**Part B — now, for the first time, the Pi gets involved.**

4. Put the flashed SD card into the Pi. Connect the display (with its **own** power
   adapter — not from the Pi), a temporary keyboard/mouse, and the Pi's official power
   supply. Power it on — this is the Pi's first-ever boot, straight to a normal desktop
   (Section 4.1).
5. Once you're looking at the Pi's desktop, plug the USB stick from step 3 into the Pi, and
   copy the project folder **from the USB stick onto the Pi's own disk** (also Section 4.1).
   This copy happens on the Pi itself, in its own file manager or terminal — not from
   Windows.
6. From this point on, every remaining command runs **on the Pi**, in a terminal there:
   installing packages, building the Python environment, filling in `.env`, running the
   app, and setting up kiosk autostart (Sections 4–5). The locker share is connected **last**
   (Section 6), once all of that is already working.

So: the SD card is written to exactly once, on your PC, before anything else happens. The
USB stick is created afterward, also on your PC, and its only job is handing files to the
Pi once the Pi already exists and is running its own OS from the SD card.

### Step 0 — Prepare the SD card (install Raspberry Pi OS)

A Raspberry Pi 4 ships with **no operating system** — you write one onto the SD card
yourself. Do this on any PC (including your work laptop) with the free **Raspberry Pi
Imager** (https://www.raspberrypi.com/software/); the Pi doesn't need to be present yet.

1. Download and install **Raspberry Pi Imager** from the link above (it's a normal
   Windows installer — download, run it, accept the defaults).
2. Insert your SD card into your PC (a card reader slot or a USB adapter) and open
   Raspberry Pi Imager.
3. **Choose Device:** Raspberry Pi 4.
4. **Choose OS → Raspberry Pi OS (other) → Raspberry Pi OS (64-bit) Full.**
   - *64-bit* matches what this project is built for (the Python packages have prebuilt
     64-bit wheels; 32-bit would force slow on-device compiles).
   - **Full, not the plain Desktop or Lite image** — this is the important call for a Pi
     that will *never* be online again after setup. Full bundles a broader set of
     software up front; Lite has no graphical desktop at all (the kiosk needs one to run
     Chromium), and once this Pi is offline for good, you lose the ability to
     `apt install` anything you forgot. Full's extra size is a one-time SD-card cost in
     exchange for that safety margin.
5. **Choose Storage:** your SD card (32 GB or larger).
6. Click the **⚙ gear icon ("Edit Settings")** before writing, and set:
   - a **hostname** (e.g. `smartlocker`),
   - **enable SSH** (useful for troubleshooting later even without internet — you can
     still SSH in over a direct Ethernet cable or the eventual company LAN),
   - a **username and password** (`install.sh` auto-detects whichever user owns the
     project folder, so any username works),
   - skip WiFi entirely if this Pi will truly never be online.
7. Click **Save**, then **Write**, and wait for it to finish (writes + verifies).

**Do not boot the Pi yet.** Go to Step 0b first — everything the Pi needs (packages,
project files) has to be staged *before* the Pi's first real boot at the deployment site,
since it will never be able to fetch anything itself afterward.

> Tip: current Raspberry Pi OS may run the desktop under Wayland. If the kiosk autostart
> misbehaves, switch to X11 with `sudo raspi-config` → *Advanced Options* → *Wayland* →
> *X11*, then reboot.

### Step 0b — Build your offline install kit (on your PC, before the Pi ever boots there)

Since this Pi will never reach the internet itself, every Python package and OS-level
dependency has to be fetched **once, on a machine that does have internet** (your Windows
PC is fine — you don't need Linux or matching hardware, see why below), then carried over
physically.

**1. Build the wheelhouse** (pre-downloaded Python packages):

```bash
# From the project root, in Git Bash (comes with Git for Windows):
deploy/install/build-wheelhouse.sh
```

This downloads every package in `requirements.txt` — except `pyscard`, see step 2 — as
prebuilt Linux/ARM64 wheels for **Python 3.13** (the version Raspberry Pi OS "trixie"
ships), using `pip`'s `--platform` cross-download flags. **This works even though your
PC is Windows/x86_64**: a `.whl` file is just a compiled, ready-to-install archive tagged
for a target platform — pip can fetch the right one for a *different* machine than the
one running pip, without compiling anything locally. Every package in `requirements.txt`
(including the C/Rust-extension ones — `cryptography`, `pydantic-core`, `uvloop`,
`httptools`, `websockets`, `watchfiles`, `SQLAlchemy`, `PyYAML`) publishes a
`manylinux`+`aarch64`+`cp313` wheel on PyPI, so this is verified, not a guess. The wheels
land in `deploy/wheelhouse/`.

> **Don't reuse a wheelhouse built before the move to Python 3.13.** A wheelhouse built
> for cp311 (older Pi OS images) will let pip partially install some packages and then
> fail mid-run on a trixie/Python-3.13 Pi — exactly the failure `install.sh`'s preflight
> guard is designed to catch loud. If you're unsure, delete `deploy/wheelhouse/*.whl` and
> re-run `build-wheelhouse.sh`.

**2. The `pyscard` `.deb` is downloaded automatically** — it's the one exception. `pyscard`
has **no** prebuilt Linux aarch64 wheel on PyPI at all (only Windows/macOS), so it can't
go through the wheelhouse. `build-wheelhouse.sh` downloads the **pinned** trixie package
(`python3-pyscard_2.2.2-1_arm64.deb`) into `deploy/system-packages/` for you; if that
exact file 404s on a future pool refresh, it falls back to scraping the pool for the
latest arm64 build. Pinning (not "latest") matters — the pool also carries `2.3.x`
builds for sid/forky that are NOT what trixie ships and could mismatch deps. If you
staged it by hand and want to confirm the version matches, full explanation is in
`deploy/system-packages/README.md`.

**3. Copy the whole project onto a USB stick:**

1. Plug the USB stick into your PC.
2. Open **File Explorer** (Windows key + `E`) and find the stick under **This PC** — it
   shows up as a drive letter, e.g. `E:`.
3. Right-click that drive → **Format...**
4. In the Format dialog, open the **File system** dropdown and pick **exFAT**. (If you want
   to check what it currently is first, right-click the drive → **Properties** shows the
   current file system.)
5. Leave **Allocation unit size** on its default. A **Volume label** is optional (e.g.
   `SMARTLOCKER` — makes it easier to recognize later, purely cosmetic).
6. Click **Start**. It'll warn you this erases everything already on the stick — confirm,
   and wait for it to finish (a progress bar, usually quick for a normal-sized stick).
7. Open the now-formatted drive and copy the entire `smart_locker` project folder onto it —
   drag-and-drop or copy/paste both work — **including** the `deploy/wheelhouse/*.whl` files
   from step 1 above and the `.deb` from step 2 above (they should already be sitting inside
   your local `smart_locker` project folder at those paths, so copying the whole folder
   picks them up automatically — no separate copy step needed for them).

*(Why a USB stick and not just the SD card directly: Raspberry Pi Imager writes two
partitions — a small `bootfs` that Windows can read/write, and the main Linux filesystem,
which Windows cannot see at all. Getting files onto that second partition has to happen
from the Pi's own side, once it's booted — a USB stick is the simplest way to hand files to
a machine you can't network yet.)*

**What if you boot the Pi before the USB stick is ready?** Nothing bad happens — the SD card
(with the OS) and the USB stick (with the project files) are completely independent. The SD
card alone is enough for the Pi to boot all the way to a normal desktop; the USB stick is
only needed *after* that, for Section 4.1's file copy. If you boot first and prepare the
stick later, you'll just be looking at a bare Raspberry Pi OS desktop with no project on it
yet — plug the stick in and pick up at Section 4.1 whenever it's ready. (You may see a
one-time first-boot setup wizard asking about locale/updates/WiFi — since you already set
hostname and user via the Imager's settings gear, you can click through it, skipping WiFi
and "check for updates" since this Pi stays offline.)

Now you're ready to boot the Pi for the first time — Section 4.

---

### 3a. Understanding the `deploy/` files

Before running anything, here's what each file in `deploy/` actually does and when it gets
used — this trips people up because most of them are only ever invoked *indirectly*, by
`install.sh`.

| File | What it is | When it's used |
|---|---|---|
| `.env.pi.example` | Environment template pre-filled for the Pi (share paths, etc.) | You manually copy it to `.env` and fill in your keys — Section 4.4 |
| `install/install.sh` | The one-shot provisioner — packages, venv, service, mount scaffolding, kiosk autostart | You run it once, as root, on the Pi — Section 3c |
| `install/build-wheelhouse.sh` | Downloads Python packages as offline wheels **and** auto-downloads the `python3-pyscard` `.deb` | You run it **once, on your PC**, before ever touching the Pi — Step 0b |
| `wheelhouse/` | Where those downloaded `.whl` files sit | Read automatically by `install.sh`/`update.sh` — you never touch it directly |
| `system-packages/` | Holds the `python3-pyscard` `.deb` (auto-filled by `build-wheelhouse.sh`, see Step 0b) | Read automatically by `install.sh` if offline; installed via plain `apt` if online |
| `systemd/smart-locker.service` | Defines the backend as a systemd service (auto-restart, boot-start) | Installed by `install.sh`; started manually the first time — Section 5 |
| `install/sudoers-smart-locker` | Grants the app account passwordless sudo for *only* restarting its own service, running updates, and `systemctl poweroff` | Installed by `apply-sudoers.sh`; powers **Software Update** and **Shut down** — Section 9 |
| `install/apply-sudoers.sh` | Renders the sudoers template to `/etc/sudoers.d/smart-locker` after `visudo -cf` | Called by `install.sh` and `update.sh`; SSH once on an existing Pi if Shut down 500s |
| `install/update.sh` | Applies a signed release tarball with backup + health-check + auto-rollback | Runs later, whenever you ship an update — Section 9 |
| `kiosk/start-kiosk.sh` | Launches Chromium fullscreen once the backend is up | Installed by `install.sh`; runs automatically at every graphical login — Section 5 |
| `kiosk/smart-locker-kiosk.desktop` | The autostart entry that triggers `start-kiosk.sh` | Installed by `install.sh` into the app user's autostart folder |
| `mount/fstab.snippet` | The `/etc/fstab` line template for the CIFS mount | You copy/edit it by hand — Section 6 (done **last**) |
| `mount/cifs-credentials.example` | Template for the locker share's login, stored root-only | You copy/edit it by hand — Section 6 |
| `PI-VALIDATION-CHECKLIST.md` | A sign-off checklist for things a no-hardware simulation can't test (real reader, real GPU, real network share) | Run through once, after the Pi is fully set up |
| `README.md` | A short technical index of this table, for quick reference without opening this guide | Reference only |

None of these files need to be understood in isolation before you start — `install.sh`
wires almost all of them together automatically. This table is here for when you want to
know *why* something exists or *where* a given piece of behavior comes from. A few of them
deserve more explanation than a one-line table cell:

**`install/install.sh`** is the single script that does almost everything in Section 4 for
you. Read top to bottom, it: (1) installs OS packages if online, or checks they're already
present if offline; (2) installs `python3-pyscard` — via `apt` if online, or by finding and
`dpkg -i`-ing the `.deb` you staged in `system-packages/` if offline; (3) creates the venv
with `--system-site-packages` and installs everything else from `wheelhouse/`; (4) enables
`pcscd`; (5) copies `systemd/smart-locker.service` into place with your actual username/
paths substituted in, and enables it (but doesn't start it yet — that needs `.env` filled
in first); (6) installs the sudoers rule that lets the app restart itself for updates
and power off from the admin panel; (7)
creates `/mnt/locker` and the credentials-file skeleton (but does **not** mount it — that's
a manual step, done last, in Section 6); (8) installs the kiosk autostart entry. It's
idempotent — re-running it after you've already done some steps by hand won't break
anything, it just skips what's already in place.

**`install/build-wheelhouse.sh`** is the one script in this list you run somewhere other
than the Pi — on your Windows PC, in Step 0b, before the Pi even boots. It has nothing to
do with the Pi's own filesystem; it just downloads files to `deploy/wheelhouse/` inside
your local copy of the project, which you then carry over via the USB stick.

**`install/update.sh`** is what actually runs, weeks or months later, when you ship a new
version. You never run this by hand during initial setup — it's what the admin panel's
**Software Update** button calls (via the sudoers rule from `install.sh`), and what
`SMART_LOCKER_UPDATE_DIR`/`SMART_LOCKER_UPDATE_HMAC_KEY` in `.env` configure. Pack the
release on Windows with `python -m scripts.pack_release` (Section 9).

**Why is `PI-VALIDATION-CHECKLIST.md` a separate file instead of being folded into this
guide?** Two reasons: (1) it's a different *kind* of document — a fill-in-the-blanks
sign-off sheet with Pass/Fail boxes, a signature line, and a date, meant to be checked off
once per physical Pi you deploy, not read once like a tutorial. If you build five of these
kiosks, you'd fill out five checklists against the same one `GUIDE.md`. (2) It specifically
covers the handful of things that *can't* be verified any other way than physically testing
the real hardware (a real card tap, real screen smoothness, a real network share) — folding
it into the guide would bury that "these specific things still need a human to physically
check" signal inside hundreds of lines of setup instructions instead of giving it its own
clearly-scoped, physically-holdable page.

---

### 3b. What files actually need to go on the Pi

Not everything in this repository belongs on the appliance. If you're copying the project
folder by hand (rather than a clean `git archive`/release tarball), here's the real split:

| Goes on the Pi | Stays off (dev-only, or created fresh) |
|---|---|
| `smart_locker/`, `config/`, `scripts/`, `deploy/` | `venv/` — not portable, see the note below |
| `requirements.txt`, `GUIDE.md`, `README.md`, `PROJECT-NOTES.md` | `.env` — the Pi gets its own from `deploy/.env.pi.example` |
| | `smart_locker.db` / `.db-wal` / `.db-shm` — the Pi creates its own via `scripts.init_db` |
| | `tests/` — optional, only needed if you want to run the test suite somewhere |
| | Anything gitignored: `logs/`, `.pytest_cache/`, `venv/` — never part of the shipped app |

**Why you can't just copy `venv/` instead of building the wheelhouse:** a venv isn't
portable code — it's a thin wrapper tied to the *exact* OS, CPU architecture, and Python
build it was created against, including any compiled extensions (like `cryptography`'s C
code) installed into it. A venv built on your Windows PC contains **Windows** binaries,
which cannot run on Linux at all, let alone the Pi's ARM64 chip specifically — copying it
over wouldn't fail loudly, individual imports would just be the wrong binary format. A
wheel file, by contrast, genuinely *is* portable to any machine matching its platform tag —
that's its whole design purpose. `install.sh` creates the venv **once**, the first time it
runs (and always gets the absolute paths right), then installs *into* it from the
wheelhouse instead of the internet — so you get "no waiting on package installation"
without the portability problem. Re-running `install.sh` later reuses that same venv
rather than rebuilding it from scratch — it only recreates the venv if it's missing, or if
it predates the `--system-site-packages` flag pyscard needs (detected automatically).

### 3c. Fast path — the install script

Get the project onto the Pi first — see Section 4.1 for exactly how (USB stick, since the
Pi's own filesystem isn't reachable from Windows). Once it's at e.g.
`/home/locker/smart_locker`, run:

```bash
sudo bash deploy/install/install.sh
```

> **ALWAYS use `sudo bash deploy/install/install.sh`.**
> Never `sudo deploy/install/install.sh` and never `sudo ./deploy/install/install.sh`
> until after the first successful bash run. Copying via exFAT from Windows strips the
> executable (`+x`) bit; without it, sudo prints:
> `sudo: deploy/install/install.sh: command not found`.
> `bash <script>` ignores the +x bit. After a successful install the script re-chmods
> itself, so a later `sudo ./deploy/install/install.sh` also works.

This is safe to re-run. It installs the system packages (including `python3-pyscard`
directly via apt if online, or via the `.deb` you staged in `deploy/system-packages/` if
not — see Section 3a), builds the Python environment (with `--system-site-packages` so it
can see the apt-installed `pyscard`, and from the offline wheelhouse if present), enables
the NFC service, installs the auto-start service and the kiosk browser, and scaffolds the
share mount (but does **not** connect it — that's Section 6, done last). When it finishes it
prints the few manual steps that remain (filling `.env`, enrolling a card). Those are
covered below.

Then jump to **Section 4.4** (keys & `.env`), **4.5**–**4.6** (database, admin card),
**4.7** (test run), and **Section 5** (kiosk autostart). Section 6 (locker share, real device
data) comes **last**, once everything else is verified working.

---

## 4. Step-by-step setup (manual)

These steps assume a terminal on the Pi and the project at `~/smart_locker`. **Notice what's
*not* here:** connecting the locker share and importing real device data — that's Section 6,
deliberately done last, after the kiosk itself is proven working. This matches how the
share typically gets provisioned in practice: IT connects it once everything else is ready,
not before.

**Manual (this section) vs. fast path (Section 3c) — pick ONE, not both.** `install.sh`
(the fast path) does everything in 4.2–4.3 and part of Section 5 for you, automatically, in
one script. Section 4 is the *same work*, broken into individual commands you type yourself
— useful the first time, so you understand what's actually happening and can fix any one
piece without re-running the whole thing. If you're following Section 4 manually, you never
need to run `install.sh` at all — it's a shortcut for later re-installs or additional units,
not a required prerequisite.

**If you make a mistake partway through:** almost everything below is safe to just re-run —
`apt`/`dpkg` installs, `python3 -m venv`, `pip install`, and `scripts.init_db` are all
harmless to repeat. The one genuinely destructive step is re-running `scripts.init_db` on a
database that already has real data in it (Section 4.5 explicitly calls this out). If
something feels badly broken and you're not sure what state you're in, the true reset button
is cheap: re-flash the SD card from Raspberry Pi Imager (Step 0) and start over — you lose
nothing except retyping these commands, since the offline install kit on your USB stick is
untouched and reusable as-is.

### 4.1 Get the code onto the Pi

Boot the Pi for the first time (with a temporary keyboard/mouse and the display connected —
the display needs its own power supply plugged in too; see Section 2). Once you're at the
desktop:

1. **Open a terminal.** On Raspberry Pi OS's default desktop, look for a black terminal-
   screen icon in the taskbar (top or bottom of the screen), or open the application menu
   (the Raspberry Pi icon, top-left) → **Accessories** → **Terminal**. A window opens with a
   command prompt — this is bash, the same shell used throughout this guide.
2. Plug in the USB stick you prepared in Step 0b. Give it a couple of seconds to auto-mount.
3. Confirm you can see it and find its exact path:
   ```bash
   ls /media/*/*
   ```
   This lists what's inside your home folder's auto-mounted drives — you should see your USB
   stick's name as one of the folders, and the `smart_locker` project folder inside it.
4. Copy the whole project folder into your home directory (adjust the path below to match
   exactly what step 3 showed you — the `*` wildcards usually work as-is if this is the only
   USB stick plugged in):
   ```bash
   cp -r /media/*/*/smart_locker ~/smart_locker
   ```
5. Move into the project folder — **every command in the rest of this guide assumes you're
   sitting in this directory**:
   ```bash
   cd ~/smart_locker
   ```
   Confirm you're in the right place and can see the project files:
   ```bash
   pwd    # should print /home/<your-username>/smart_locker
   ls     # should list smart_locker/, config/, scripts/, deploy/, requirements.txt, ...
   ```

(Files app drag-and-drop works identically to steps 2–4 if you prefer a GUI over typing —
just end up with the project at `~/smart_locker` either way.)

### 4.2 System packages and the NFC reader

The NFC reader talks to Linux through the **PC/SC daemon** (`pcscd`) plus the CCID driver.
You also need the CIFS tools (for the share mount, connected later), Chromium (for the kiosk
display), and `python3-pyscard` (the reader's Python bindings — see Section 3a for why this
comes from a `.deb`, not pip). Run these from the terminal, still inside `~/smart_locker`:

```bash
sudo dpkg -i deploy/system-packages/python3-pyscard_*.deb
sudo apt install -y pcscd pcsc-tools libccid cifs-utils chromium unclutter curl python3-venv
sudo systemctl enable --now pcscd
```

Since this Pi has no internet, that `apt install` line will fail unless these packages were
already present on the Full image (they usually are — Chromium and the CIFS/PCSC tools are
common enough to ship on Full) or you've separately staged their `.deb`s the same way as
`pyscard`. If something's missing, `install.sh` prints a clear warning naming exactly what,
rather than failing silently.

Plug in the ACR1252U and confirm Linux sees it:

```bash
pcsc_scan          # should list "ACS ACR1252..."; press Ctrl-C to stop
```

If it isn't listed, see **Section 9 (Troubleshooting)**.

### 4.3 Python environment

```bash
python3 -m venv --system-site-packages venv
source venv/bin/activate
pip install --no-index --find-links deploy/wheelhouse -r <(grep -vi '^pyscard' requirements.txt)
```

`--system-site-packages` is what lets this venv see the `python3-pyscard` you installed at
the system level in 4.2 — a plain `python3 -m venv venv` would be fully isolated and
wouldn't see it. `--no-index --find-links deploy/wheelhouse` tells pip to install from the
wheels you staged in Step 0b instead of reaching out to PyPI (which it can't reach anyway).
This creates the venv as a new `venv/` folder *inside* `~/smart_locker` — you should now see
it if you run `ls`.

**Important: `source venv/bin/activate` only applies to the current terminal window.** If
you close this terminal (or reboot) and open a new one later to keep working through the
rest of this guide, `python`/`pip` will silently go back to meaning the *system* Python,
which doesn't have any of these packages — commands will fail or behave strangely rather
than clearly erroring. Every time you open a fresh terminal to run a `python -m scripts...`
or `python -m smart_locker.app` command from this guide, first run:
```bash
cd ~/smart_locker && source venv/bin/activate
```
(You'll know it worked because your prompt gets a `(venv)` prefix.)

Verify the reader is reachable from Python:

```bash
python -c "from smartcard.System import readers; print(readers())"
# Expected (reader plugged in):
# ['ACS ACR1252 Dual Reader PICC 0', 'ACS ACR1252 Dual Reader SAM 0']
```

An empty list `[]` means the reader isn't plugged in or `pcscd` isn't running.

### 4.4 Encryption keys and the `.env` file

The system encrypts every card UID. Generate the three keys:

```bash
python -m scripts.generate_key
```

Create your `.env` from the Pi template, then paste the keys into it:

```bash
cp deploy/.env.pi.example .env
nano .env          # paste SMART_LOCKER_ENC_KEY, SMART_LOCKER_HMAC_KEY, and SMART_LOCKER_UPDATE_HMAC_KEY
```

The template already points the Excel paths at /mnt/locker (`/mnt/locker/...`, connected
later in Section 6) and keeps the database local. Adjust the
`SMART_LOCKER_SOURCE_EXCEL_PATH` filename to match your real workbook. **Keep `.env`
secret** — it holds the encryption keys (it is already gitignored).

**Every line in `.env`, explained** (the full authoritative reference, with every default,
is Section 11 — this is the same information, walked through in the order it appears in
`deploy/.env.pi.example`):

- `SMART_LOCKER_ENC_KEY` — the AES-256-GCM key. Every card UID is encrypted with this
  before it touches the database. You just generated it above; paste it in as-is.
- `SMART_LOCKER_HMAC_KEY` — a *separate* key used to compute a one-way fingerprint of each
  card UID, so the app can look up "have I seen this card before?" by comparing
  fingerprints, without ever decrypting every stored UID to check. Also just generated.
- `SMART_LOCKER_UPDATE_HMAC_KEY` — a *third*, unrelated key that has nothing to do with
  cards. `python -m scripts.pack_release` uses it to HMAC the tarball; `update.sh` refuses
  anything that doesn't match this exact key (Section 9). Also generated by the same
  command; paste in the third value.
- `SMART_LOCKER_DB_PATH` — where the SQLite database file lives. Leave this pointing at the
  Pi's local disk (the template already does) — never move it onto the locker share.
- `SMART_LOCKER_READER_NAME` — a text filter used to pick the right reader if more than one
  PC/SC device is plugged in. `ACR1252` (the default) matches the ACR1252U; you shouldn't
  need to touch this unless you use a different reader model.
- `SMART_LOCKER_SESSION_TIMEOUT` — seconds of no touch before a session auto-ends (120 by
  default = 2 minutes). Purely a UX/security tradeoff; change it if 2 minutes feels wrong
  for how people actually use the kiosk.
- `SMART_LOCKER_MAX_BORROWS` — how many devices one person can have checked out at once
  before Borrow refuses further items (5 by default).
- `SMART_LOCKER_API_HOST` / `SMART_LOCKER_API_PORT` — what address/port the backend listens
  on. `0.0.0.0:8000` (the default) means "every network interface, port 8000" — this is
  what lets you reach `http://<pi-address>:8000/dashboard` from another computer on the
  same network. You almost never need to change this.
- `SMART_LOCKER_SOURCE_EXCEL_PATH` — the company device master list to **import from** the share.
  Empty disables automatic import entirely. Point this at the real filename once you know
  it (Section 6.2) — until then it can stay as the template's placeholder.
- `SMART_LOCKER_EXCEL_PATH` — the workbook the app **writes back to** the share (devices,
  transactions, users). Different from the line above — one is read-from, this one is
  written-to.
- `SMART_LOCKER_EXCEL_AUTO_EXPORT` — `1` means "refresh that exported workbook
  automatically after every import/photo change." Off in the Pi template (status
  lives on the dashboard and admin **Export Excel**). Existing Pi `.env` files
  keep their old value across `update.sh` — set this to `0` by hand if it is still `1`.
- `SMART_LOCKER_SOURCE_SYNC_INTERVAL_HOURS` — hours between automatic re-imports from
  the share (default `6`). Startup import and admin **Sync Source** still run. Older
  `SMART_LOCKER_SOURCE_SYNC_HOUR` / `_MINUTE` / `_POLL_SECONDS` keys are ignored.
- `SMART_LOCKER_LAST_SYNC_PATH` — JSON snapshot for the admin "Last sync" line. Empty
  stores `last_sync.json` next to the SQLite database (local disk, not the share).
  `update.sh` keeps that file across code swaps.
- `SMART_LOCKER_PHOTO_INPUT_PATH` — a folder (can be on the share or local) the app scans for
  device photos, matched by filename to the device model. Empty disables photo import
  entirely.
- `SMART_LOCKER_UPDATE_DIR` — the share folder where you drop the signed pair from
  `python -m scripts.pack_release` (`.tar.gz` + `.hmac`). `update.sh` picks it up from here.
  Change this one value if you want updates from a different share folder — no script edits
  needed anywhere else.
- `SMART_LOCKER_KEEP_BACKUPS` — how many old code+database backup pairs `update.sh` keeps
  under `./backups` before deleting the oldest. `5` by default.

*(This is a different file from the repo-root `.env.example` you may have used for
development — that one has empty/local defaults meant for a Windows dev machine with no
locker share and no update mechanism. Always use `deploy/.env.pi.example` on the Pi.)*

### 4.5 Initialize the database

```bash
python -m scripts.init_db
# Expected: Database initialized successfully.
```

This creates `smart_locker.db` with four tables: `users`, `registrants`, `devices`,
`transaction_logs`.

### 4.6 Enroll your first (admin) card

With the reader plugged in:

```bash
python -m scripts.enroll_card --name "Your Name" --role admin
```

When you see `Place card on reader...`, tap your card and hold it steady for 1–2 seconds.
The card UID is masked in the output (e.g. `A1****D4`) and stored encrypted — only admins
can ever decrypt it. Enroll regular users the same way with `--role user`.

To bind a sticker to a locker device after Register Device (optional CLI; the admin panel
**Register Device** is the usual path):

```bash
python -m scripts.enroll_device_tag --pm PM-001
# or without a reader:
python -m scripts.enroll_device_tag --pm PM-001 --uid AABBCCDD
```

### 4.7 Run it (test before making it permanent)

```bash
python -m smart_locker.app
```

You'll see the backend start, the NFC reader come up, and the web server bind to port 8000.
Open `http://localhost:8000` in a browser on the Pi to see the kiosk UI. Press `Ctrl+C` to
stop. (Use `python -m smart_locker.app --cli` for a console-only NFC loop with no web UI.)
At this point there's no real device data yet — that's expected, it comes in Section 6.

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

At this point the kiosk is fully working end-to-end, just with no real device inventory yet
(the empty database from Section 4.5). That's intentional — **connect the locker share now**,
Section 6, typically once IT is ready to provision it.

---

## 6. Connect the locker share and load your real data (do this last)

Everything up to here works with **zero** network access. This section is the one place the
Pi needs the company network — and it's deliberately the *last* thing you set up, matching
how the share is usually actually provisioned (IT connects it once the appliance itself is
proven working, not before). The mount is a **soft dependency**: if the share is ever down
after this point, borrow/return keeps working from the local database — only import/export
pause until it's back (see Section 5's systemd unit comments, and Section 9).

### 6.1 Mount the locker share (CIFS)

The Pi mounts the locker file share at `/mnt/locker` (CIFS/SMB). On a Windows PC that is
whatever drive letter or UNC path IT mapped for this locker. Keep the Excel file, photos,
and updates in the **root of that share**, not inside a git working copy.

```
/mnt/locker/                    (same folder your PC sees as the locker share)
  device-list.xlsx          import — device master list
  photos/                       optional; filename = model, e.g. 87V.jpg
  locker-updates/               signed smart-locker-<id>.tar.gz + .hmac (from pack_release)
  smart_locker_data.xlsx        written by the Pi; open it, don't edit it
```

1. Create the mount point and a root-only credentials file (skip if `install.sh` already
   scaffolded these):

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

### 6.2 Load devices from the Excel list

The company device master list lives on the share. **Sync does not put devices in the
locker.** It only refreshes catalog fields (name, type, serial, manufacturer, model,
calibration) for PMs that are **already** locker rows. Platz/Schrank is unused.

A device enters the locker when an admin uses **Register Device**: enter the **PM**
number, pick a **free slot**, tap the NFC sticker. The Pi looks up that PM in
`device-list.xlsx` and copies name / type / manufacturer / model / serial / cal.
Unknown PM or share down → error, no ghost row.

```bash
# Preview catalog updates without writing:
python -m scripts.import_devices --file "/mnt/locker/device-list.xlsx" --dry-run

# Apply catalog updates for PMs already in the locker:
python -m scripts.import_devices --file "/mnt/locker/device-list.xlsx"
```

| Excel column | German | Maps to | Required? |
|---|---|---|---|
| Equipment | Equipment | `pm_number` | **Yes** — the device identifier |
| Category | Kategorie | `device_type` | No |
| Description | Beschreibung | `description` | No |
| Manufacturer | Hersteller | `manufacturer` | No |
| Type designation | Typbezeichnung | `model` | No |
| Serial number | Hersteller-serialnummer | `serial_number` | No |
| Calibration date | Datum der nächsten Kalibrierung | `calibration_due` | No |

If auto-detection picks the wrong column, override it, e.g.
`--pm-col "Equipment" --type-col "Kategorie"`. Re-importing is safe — devices are matched by
PM number. A re-import **never** inserts a locker row and **never** overwrites `locker_slot`,
`image_path`, `description`, `status`, or the current borrower. Catalog fields (name, type,
serial, manufacturer, model, calibration) still update. Barcode is not imported.

Once running as a service, this same catalog refresh also happens **automatically**: once on
startup, every 6 hours (configurable), and on demand from the hidden admin panel. (See
Section 8 for why the live "watch the file" mode is off for network shares.)

### 6.3 Add device photos

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
the locker share, photos present at startup are applied automatically; photos added later are
picked up on the next restart or by re-running `update_device --auto`.

---

## 7. Day-to-day: how the kiosk is used

### Session flow (tap-and-go)

The **work card** is **tapped and removed** — it is not left on the reader. After login,
tap a **device sticker** on the same reader to borrow or return, or pick the unit on
the touch display. Do not present the sticker while the work card is still on the reader.

**Return without logging in:** on the idle screen, tap the NFC sticker of a **borrowed**
device. No work card is required. The kiosk shows a large overlay with the device name
and **Put in slot N**. An available (or unknown) sticker at idle does **not** borrow —
it still asks for a work card first.

1. **Tap your work card** → the reader reads the UID → the system authenticates you → the
   scan-first main menu appears.
2. **Tap the NFC sticker on the device** (auto borrow/return) **or** open **Locker**
   (what's in / what's out) or **Return** on screen. After a return, the same slot
   overlay appears. The session stays open so several devices can be tagged in one login.
3. **The session ends** via the **End Session** button, a **work-card tap** (a device
   sticker does not log you out), or the **inactivity timeout** (120 seconds of no touch —
   a silent security backstop).

### The screens

- **Idle** — animated NFC ring, "Tap your card", live clock, a "Register your card" entry.
  A borrowed device sticker returns it here (slot overlay). An available sticker asks
  for a work card first.
- **Register (self-service)** — search and pick your approved name, then tap your card to
  enrol it under that name.
- **Authentication failed** — red "Card Not Recognized", auto-returns to idle. A bound
  **available** device sticker at idle is **not** this screen — it asks you to tap your
  work card first. A **borrowed** sticker at idle returns the device.
- **Main menu** — welcome + name; **Tap the device** to borrow or return; **Locker**
  (what's in · what's out) and **Return** (*or pick on screen*); **End Session**.
- **Locker** — availability overlay: every locker device by slot, tagged **IN** / **OUT**
  / **YOURS** / **MAINT**, with **PM number** on the card. Screen-pick borrow still
  works for units without a sticker. A sticker tap still auto-intents and refreshes
  this grid.
- **Return** — the same grid (PM on each card), with your own borrowed items highlighted.
  Confirming a return shows the slot overlay.
- **Device detail** (overlay) — photo, PM, type, serial, and a confirm button.
- **Return slot** (overlay) — after any successful return: device name and **Put in slot N**.
- **Inactivity warning** (overlay) — a countdown with a "Stay Active" button.
- **Hidden admin panel** (overlay) — opened by tapping the idle clock 5 times. Shortcuts for
  Locker, Return, **Sync source**, Register user, **Register Device**, **Export to Excel**,
  **Software Update**, **Exit kiosk**, **Shut down**, End Session.

### The rules

- **Borrow limit:** each user can hold up to `SMART_LOCKER_MAX_BORROWS` devices (default 5).
- **Returns:** on the idle screen, anyone can return a borrowed device by tapping its
  sticker (the log keeps the original borrower). While logged in, only the borrower can
  return their own device; an admin can return any device on anyone's behalf (the log
  records both people). After every successful return the kiosk shows where to put it.
- **Open-access locker:** there is no physical lock — the system tracks *who has what*.

### Hidden operator access (do not put this on a user-facing poster)

These are unpublished on purpose. Anyone who can touch the kiosk screen or reach the
Pi on the LAN can use them — the lock is **physical access**, not a password.

**Admin panel on the kiosk**

1. Be on the idle screen — the one that says **TAP YOUR CARD**, with the live clock.
2. Tap the **clock** (the time/date at the top) **five times within three seconds**.
3. The dark admin overlay slides in. The kiosk signs in as the **first enrolled admin**
   in the database — no card tap. If no admin has been enrolled yet, the panel cannot
   open (`POST /api/admin/session` returns "no admin").
4. What the buttons do:
   - **Locker / Return Screen** — jump into the availability overlay or return grid as that admin.
   - **Sync Source** — first tap *previews* Excel changes from the share; second tap *applies* them.
   - **Register User** — type any name, then tap a card (skips the approved-name list).
     After success, timeout, or cancel the kiosk returns to idle; the next
     work-card tap logs that user in (a leftover admin session must not
     treat the tap as logout).
   - **Register Device** — add a locker unit: **PM + free slot + NFC tap** (catalog
     comes from Excel). Existing rows can bind / unbind / change slot. The list shows
     **name + PM** (and slot).
   - **Export to Excel** — download a snapshot of devices / transactions / users.
   - **Software Update** — apply the newest signed tarball from `locker-updates` on the
     share. Full-screen updating overlay, then the kiosk reloads.
   - **Exit kiosk** — close Chromium; the locker service stays up. Chromium does not
     come back until the next graphical login or reboot. Confirm first.
   - **Shut down** — `systemctl poweroff` the Pi. Confirm first. Needs the sudoers
     drop-in (see Section 9 if the button errors after a first update).
   - **End Session** or **X** — close the panel and return to idle. Both end
     the admin session so a leftover overlay session cannot check a tool out.
5. Tap the clock five times again to toggle the panel if it is still on the idle screen.

**Dashboard and health (any PC on the same network, no login)**

| What | URL |
|---|---|
| Live inventory (devices, last 500 transactions, users) | `http://<pi-address>:8000/dashboard` |
| Is the appliance alive? | `http://<pi-address>:8000/api/health` |
| Kiosk UI (only needed if Chromium is not already fullscreen) | `http://localhost:8000/?lite` on the Pi |

`<pi-address>` is the Pi's LAN IP (`hostname -I` on the Pi, or the address IT assigned).

---

## 8. Excel, the locker share, and the dashboard

There are two ways to see live data — a web dashboard and the Excel workbook on the share.

### How the locker reads Excel

The Pi does not keep Excel open. On each import it copies the `.xlsx` to a temp file, then
reads that copy (`openpyxl`). If someone has the workbook open on a PC, the copy still
usually succeeds.

1. `.env` names the file:

   `SMART_LOCKER_SOURCE_EXCEL_PATH=/mnt/locker/device-list.xlsx`

   That is the master list sitting in the locker share root. Change the filename in `.env`
   if yours is different.

2. Import runs when the service starts, every 6 hours (`SMART_LOCKER_SOURCE_SYNC_INTERVAL_HOURS`),
   and when you use **Sync Source** in the admin panel. Linux cannot see "file changed"
   events for a file another computer wrote on a CIFS share, so there is no 30-second poll.

3. Sync **never inserts** locker devices. It updates catalog fields for PMs already
   in SQLite. A device enters the locker only via admin **Register Device**
   (PM + free slot + NFC). Platz/Schrank is unused.

4. After that import, the Pi writes **Aktueller Einsatzort** for locker PMs
   back into the same workbook (available → `Schrank`, borrowed → the borrower's
   name). Other columns and sheets are left alone. If someone has the file open
   in Excel, the write is skipped and retried on the next borrow/return or
   sync — the kiosk keeps running. **Do not edit Aktueller Einsatzort in Excel**
   for locker devices; the Pi owns that cell. Add new PMs and fix catalog
   columns (name, type, manufacturer, model, serial, calibration) in Excel as usual.

5. Values in **Aktueller Einsatzort** that are not schrank locations are treated as person
   names and added to the self-register list.

6. If `SMART_LOCKER_EXCEL_AUTO_EXPORT=1`, after a real import the Pi writes
   `smart_locker_data.xlsx` next to the source file (Devices, Transactions, Users). Don't
   edit that file by hand.

Re-import matches devices by PM number. It leaves `locker_slot`, `image_path`,
`description`, `status`, and the current borrower alone. Catalog fields still update.
Excel never inserts a locker row. After import, the Pi writes locker Einsatzort
back into the sheet.

**Web dashboard** — open `http://<pi-address>:8000/dashboard` from any browser on the
network (no login). It shows three tables, auto-refreshing every 30 seconds: **Devices**
(slot, PM number, status, borrower, calibration due — filterable/sortable), **Transactions**
(last 500), and **Users**.

**Status workbook on the share:** the Pi can write `smart_locker_data.xlsx`
at `SMART_LOCKER_EXCEL_PATH` (Devices + Transactions + Users) when
`SMART_LOCKER_EXCEL_AUTO_EXPORT=1`. The Pi template leaves this **off** — live status is
the dashboard, and you can download a snapshot any time from the admin panel's
**Export Excel**. After `update.sh`, set `SMART_LOCKER_EXCEL_AUTO_EXPORT=0` in the live
`.env` if it is still `1` (the tarball does not overwrite `.env`).

**Why the import is scheduled, not instant:** the Pi can't reliably get a "file changed"
notification for a file that lives on a network share (the Linux mechanism for this,
*inotify*, doesn't see edits made by other computers on a CIFS/SMB mount). So instead of a
live file-watch, the system imports on startup and every 6 hours. To pull changes in
immediately, use **Sync source** in the admin panel, or run
`python -m scripts.sync_source`.

---

## 9. Operations — logs, troubleshooting, self-healing & updates

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
- If `import smartcard` fails in the venv, confirm `python3-pyscard` is actually installed
  (`dpkg -l python3-pyscard`) and that the venv was created with `--system-site-packages` —
  see Section 3a/4.3.

**`SCardEstablishContext: Access denied (0x8010006A)` (pcscd is running, socket is fine):**
- On Raspberry Pi OS **trixie**, this is a **polkit** denial — not a socket/permission
  problem. Even with a world-writable `/run/pcscd/pcscd.comm` and the user in the `pcscd`
  group, non-console sessions (SSH, systemd services) get denied by default.
- `install.sh` installs the fix automatically at
  `/etc/polkit-1/rules.d/50-smart-locker-pcsc.rules` (grants `access_pcsc` /
  `access_card` to members of `pcscd` or `plugdev`). Re-run
  `sudo bash deploy/install/install.sh` if that file is missing, then
  `sudo systemctl try-restart polkit && sudo systemctl restart pcscd`.
- Confirm: `pcsc_scan` should then list the ACR1252U without the Access denied error.

**`sudo: deploy/install/install.sh: command not found`:**
- The executable (`+x`) bit was stripped when files were copied from Windows via
  exFAT (exFAT has no POSIX permissions). Run
  `sudo bash deploy/install/install.sh` instead — `bash <script>` does not need
  the +x bit. Do **not** use `sudo deploy/install/install.sh` (no `bash`).

**`pip` fails with `No matching distribution found for SQLAlchemy` (or similar) during install:**
- The wheelhouse was built for a different Python ABI (e.g. cp311 wheels on a
  cp313 / trixie Pi). `install.sh` now fails fast with a clear message before pip
  partially installs. Fix: re-run `deploy/install/build-wheelhouse.sh` on a machine
  with internet (it targets Python 3.13 / cp313), recopy `deploy/wheelhouse/` and
  `deploy/system-packages/` onto the Pi, re-run the installer.

**The locker share won't mount:**
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

### Running unattended — what recovers on its own

The Pi lives in the locker, far from you, so it is built to heal itself:

- **Crashes restart automatically.** The systemd service uses `Restart=always` with
  `StartLimitIntervalSec=0`, so if the app ever dies it comes back within a few seconds and keeps
  retrying *forever* — a transient fault clears itself with no one on site. It also starts on boot
  and after a power cut.
- **A down locker share doesn't stop the kiosk.** The share is a *soft* dependency: borrow/return keep
  working from the local database; only import/export pause until the share returns.
- **Sync never crashes the app.** If the Excel file is left open/locked, or the share drops, the
  import/export is logged and skipped — the kiosk stays up and the next scheduled or manual sync
  retries.

### Is it alive? Check from any browser — no SSH, no Linux

- **Health:** open `http://<pi-address>:8000/api/health`. It returns a small JSON you can bookmark:
  `status` (`ok`/`degraded`), `uptime_seconds`, `database`, `nfc_reader`, and the last sync result.
- **Dashboard:** open `http://<pi-address>:8000/dashboard` for the live device inventory.
- If `/api/health` doesn't load at all, the Pi is off or off the network (power / cable / Wi-Fi) —
  the one situation that needs someone physically there.

### Updating the software (no internet)

There is one update path: pack a signed tarball on Windows, copy **both** files onto the
locker share, then apply it on the Pi. Do not copy files into the live app folder while
`smart-locker.service` is running. Do not `git reset` on the appliance — that is not a
full update.

**1. Pack on Windows**

From a git checkout of this repo, with `SMART_LOCKER_UPDATE_HMAC_KEY` in `.env` (the same
value as the Pi):

```bash
python -m scripts.pack_release
```

Optional arguments: `python -m scripts.pack_release <ref> <output-dir>`. Default ref is
`HEAD`; default output directory is the current working directory.

That packs a snapshot of the files git is tracking (the committed project, not your
`.env`, `venv`, or database) as `smart-locker-<id>.tar.gz`, then writes
`smart-locker-<id>.tar.gz.hmac` (HMAC-SHA256 via `SMART_LOCKER_UPDATE_HMAC_KEY`).

**2. Copy onto the share**

Copy **both** files into `/mnt/locker/locker-updates/` (`SMART_LOCKER_UPDATE_DIR`). Wait
until the copy has fully finished before applying — this is CIFS; a truncated tarball fails
the HMAC check (fail closed). The share is writable by more people than should be able to
update the Pi, which is why HMAC is required.

**3. Apply on the Pi**

**First apply of this `update.sh`:** copy only `deploy/install/update.sh` onto the Pi
(keep LF — do not open it in Notepad), then SSH:

`sudo bash /home/locker/smart_locker/deploy/install/update.sh`

The old script on the Pi does not preserve `deploy/system-packages/*.deb`. Leave only the
new `.tar.gz` + `.hmac` pair in `locker-updates/` (the newest file by date is applied).

Later applies: admin panel → **Software Update** (full-screen overlay, then the kiosk
reloads), or the same SSH command. If the app is not running, the button is unavailable —
use SSH. The button relies on the sudoers drop-in that `apply-sudoers.sh` writes to
`/etc/sudoers.d/smart-locker`.

**First apply of Exit kiosk / Shut down:** the *old* `update.sh` on the Pi does not
refresh sudoers. After this release is on disk, SSH once:

`sudo bash /home/locker/smart_locker/deploy/install/apply-sudoers.sh`

Without that, **Shut down** returns an error (sudoers still has only the update rule).
**Exit kiosk** does not need sudo — it only stops Chromium. Later `update.sh` applies
refresh sudoers themselves.

`update.sh` still: stops the service, snapshots code + database, rsyncs the new tree
(`--delete`, with preserve), installs wheels from the **existing** Pi wheelhouse, runs
`scripts.migrate_db`, starts the service, and checks `/api/health`. If the new version does
not come up, it restores the snapshot. HMAC is required; unsigned or mismatched releases
are refused.

The backend restarts for a few seconds. The kiosk stays on the updating overlay,
then reloads. Progress is in `logs/update.log` and `logs/update-status.json`.

**Leave these on the Pi forever (never copy them from a Windows tree)**

- `.env`
- `smart_locker.db` (plus `-wal` / `-shm`)
- `venv/`
- `deploy/wheelhouse/`
- `deploy/system-packages/`
- `logs/`
- `backups/`
- `.git/`
- device photos under `smart_locker/frontend/images/` (rsync without `--delete`, so
  `hero_bg.jpg` from a release can update)

---

## 10. Tests

The test suite needs no NFC hardware (it uses in-memory SQLite and mock data):

```bash
python -m pytest tests/ -v          # all tests
python -m pytest tests/test_security.py -v
```

---

## 11. Configuration reference

All settings live in `.env` (loaded by `config/settings.py`). The Pi template
`deploy/.env.pi.example` pre-fills sensible values.

| Variable | Default | Description |
|---|---|---|
| `SMART_LOCKER_ENC_KEY` | (required) | AES-256-GCM key, base64 — from `generate_key` |
| `SMART_LOCKER_HMAC_KEY` | (required) | HMAC-SHA256 key, base64 — from `generate_key` |
| `SMART_LOCKER_UPDATE_HMAC_KEY` | (required on the Pi) | HMAC-SHA256 key, base64 — from `generate_key`; same value `pack_release` uses to sign tarballs, see "Updating the software" in Section 9 |
| `SMART_LOCKER_DB_PATH` | `smart_locker.db` | SQLite path — keep on the Pi's local disk |
| `SMART_LOCKER_READER_NAME` | `ACR1252` | Substring filter for the NFC reader name |
| `SMART_LOCKER_SESSION_TIMEOUT` | `120` | Idle session timeout (seconds) |
| `SMART_LOCKER_MAX_BORROWS` | `5` | Max devices a user can hold at once |
| `SMART_LOCKER_API_HOST` | `0.0.0.0` | Web server bind address |
| `SMART_LOCKER_API_PORT` | `8000` | Web server port |
| `SMART_LOCKER_SOURCE_EXCEL_PATH` | (empty) | Device master list on the share to import; empty disables auto-import |
| `SMART_LOCKER_EXCEL_PATH` | `smart_locker_data.xlsx` | Where the exported workbook is written (the share path on the Pi) |
| `SMART_LOCKER_EXCEL_AUTO_EXPORT` | (off) | `1` = auto-refresh the exported workbook after each import/photo change |
| `SMART_LOCKER_SOURCE_SYNC_INTERVAL_HOURS` | `6` | Hours between automatic source imports (startup + admin Sync still run) |
| `SMART_LOCKER_LAST_SYNC_PATH` | `last_sync.json` next to the DB | Admin last-sync snapshot; keep on the Pi's local disk |
| `SMART_LOCKER_PHOTO_INPUT_PATH` | (empty) | Folder watched for device photos; empty disables |
| `SMART_LOCKER_UPDATE_DIR` | `/mnt/locker/locker-updates` | share folder for the signed pair from `pack_release` (`.tar.gz` + `.hmac`); `update.sh` picks it up — see "Updating the software" in Section 9 |
| `SMART_LOCKER_KEEP_BACKUPS` | `5` | How many old code+DB backup pairs `update.sh` keeps under `./backups` before pruning |

---

## 12. What's built vs. what's next

**Built:** NFC enrollment & authentication (AES-256-GCM + HMAC), single-user sessions with
timeout, device tracking with the full schema, NFC **device tags** (same ACR1252U; auto
borrow/return after login), borrow/return with admin overrides and per-user limits,
self-service registration, Excel catalog refresh (no locker insert), Einsatzort
write-back into `device-list.xlsx`, on-demand/auto export, photo assignment, the
read-only `/dashboard`, the FastAPI REST API + SSE bridge, the
6-screen kiosk UI, **Raspberry Pi appliance deployment** (systemd service, CIFS mount,
Chromium kiosk, fully offline install including the no-PyPI-wheel `pyscard` case), and a
hardware-free pytest suite.

**Next:** calibration-due notifications, a full admin web panel, MIFARE sector reading, and
multi-reader support.

---

## 13. Understanding the frontend files

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
| `screen-main-menu` | Welcome + tap-the-device + Locker / Return |
| `screen-borrow` | Locker availability overlay (in / out) |
| `screen-return` | Device grid for returning |
| `overlay-device-detail` | Device detail popup |
| `overlay-inactivity` | Countdown warning |
| `overlay-slot` | After return: put in slot N |
| `overlay-admin` | Hidden admin panel (5× clock tap) |

### style.css — appearance

CSS is a list of rules: *"find elements that match this selector, apply these visual
properties."* A **dot** (`.auth-title`) matches a class; a **hash** (`#clock-time`) matches
an id. **CSS variables** at the top let you change the whole look in one line:

```css
:root {
  --accent: #009641;   /* kiosk green — change once, the whole UI follows */
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
User taps "LOCKER"
  → app.js listener → openBorrow()
      → navigate('borrow')   JS adds .active to #screen-borrow → CSS wipes it in
      → apiGetDevices()      JS fetches /api/devices → builds the in/out card grid
```

### Where to look to change something

| You want to... | File | Search for... |
|---|---|---|
| Change a colour | `style.css` | `:root {` at the top |
| Change the font | `style.css` | `--font-display` / `--font-body` |
| Change a button label | `index.html` | the button's text |
| Change the locker in/out badge | `app.js` | `borrow-badge` |
| Change the inactivity timeout (UI) | `app.js` | `cdSeconds` in the `S` object |

---

## 14. The REST API

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
| `POST` | `/api/admin/devices/{id}/bind-tag` | 60s window to bind the next sticker to that device |
| `POST` | `/api/admin/devices/{id}/unbind-tag` | Clear the sticker HMAC on that device |
| `POST` | `/api/admin/sync-source` | Trigger the source Excel import now |
| `GET` | `/api/admin/export-excel` | Download the full database as `.xlsx` |
| `GET` | `/api/dashboard/devices` | Public device inventory (no auth) |
| `GET` | `/api/dashboard/transactions` | Public transaction history, last 500 |
| `GET` | `/api/dashboard/users` | Public registered-users list |
| `GET` | `/api/events` | SSE stream — card-tap, auth, and session events |

`GET /api/devices` returns per device: `id`, `pm_number`, `name`, `device_type`,
`serial_number`, `manufacturer`, `model`, `locker_slot`, `description`,
`image_path`, `calibration_due`, `status`, `borrower_name`, `has_tag` (bool — no HMAC
digest). Device-tag HMAC is never on this payload, the public dashboard, or Excel export.

**The NFC → browser bridge:** the background NFC listener detects a tap and puts an event on
a queue; `GET /api/events` streams it to the browser, which then runs the auth/registration
flow. FastAPI serves the kiosk UI (`index.html`) and the dashboard as static files from
`smart_locker/api/server.py`.

---

## 15. NFC device tags

Each locker device can have a cheap NFC sticker (NTAG213/215, same ACR1252U as work cards).
The sticker UID is stored only as `devices.tag_hmac` (HMAC-SHA256, same key as work cards).
The raw UID is never stored or logged. `devices.barcode` is an unused leftover column
(not imported, not exported, not on the API).

**Flow:** tap work card → tap the sticker (or pick on screen). Auto-intent: available →
borrow; borrowed by you → return; borrowed by someone else → fail for a normal user, or
admin return-on-behalf; maintenance → fail. The session stays open. A **work-card** tap
still logs out; a device tag does not. An unknown UID while logged in stays logged in.

**Register Device** (hidden admin panel): enter **PM**, pick a **free slot**, tap the
sticker. Catalog (name, type, manufacturer, model, serial, cal) is copied from Excel.
Unknown PM or share down fails with no ghost row. Existing rows can bind / unbind /
change slot. The list shows **name + PM**. CLI bind-only:
`python -m scripts.enroll_device_tag --pm PM-001` (or `--uid HEX`, `--force` to replace).
There is no USB barcode scanner and no `GET /api/devices/barcode/{barcode}`.

---

## 16. Future improvements

- **Calibration-due notifications** — calibration dates are stored; a reminder system is not.
- **Full admin web panel** — edit users/devices from the browser (today: read-only dashboard
  + the kiosk's hidden admin panel).
- **MIFARE sector reading** — APDU commands exist in `nfc/apdu.py` but aren't wired in.
- **Multi-reader support** — currently the first matching reader is used.
- **Email / webhook alerts** — overdue devices, borrow-limit hits.
- **Device condition reporting** — let users flag damaged equipment on return.
