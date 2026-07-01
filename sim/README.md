# Smart Locker — Simulation Harness

A developer with **no hardware** can follow this guide from zero to:

- Boot Raspberry Pi OS arm64 in QEMU (Path A — full OS rehearsal)
- OR run the app directly on a dev box (Path B — fast native iteration)
- Inject simulated card taps via HTTP / keyboard / browser button
- Mount a Samba "M:" stand-in and trigger a source-Excel import
- Confirm logs and dashboard — **with no card UID in any log line**

All real project files are reused as-is. This harness adds only the `sim/`
directory; nothing outside it is touched.

---

## What this harness does and does NOT validate

| Validated by the harness | NOT validated — requires real hardware |
|---|---|
| FastAPI app boots and serves UI | ACR1252U NFC reader (PC/SC) |
| NFC bridge, authenticator, session manager | Actual RFID/NFC card reads |
| Borrow / return flows end-to-end | Chromium kiosk rendering / GPU smoothness |
| `deploy/install/install.sh` steps on Pi OS | Pi GPIO or USB peripherals |
| systemd service install + lifecycle | CIFS performance over a real network share |
| Source-Excel import from a Samba share | Production key management |
| Photo matching by model filename | Multi-user concurrent hardware access |
| inotify-dead-on-CIFS-remote-writes behaviour | |
| Security invariant: no UID in logs | |

> **QEMU limitations (state plainly):**
> QEMU does **not** emulate the ACR1252U reader. Card taps are injected via
> `POST /api/dev/tap`, the **F2 key**, or the **bottom-right corner button**
> that appears in the browser when `SMART_LOCKER_FAKE_READER=1`.
> QEMU also does **not** validate GPU smoothness — confirm kiosk performance
> only on real Pi 4 hardware.

> **Path A status — not run on the current dev box:** the full QEMU-boot +
> Samba-mount rehearsal (Path A) has **not** been executed in this repo's
> history; the dev machine used for this work has no `qemu-system-aarch64`
> system emulator and no Docker/host `smbd` for the Samba stand-in. All
> lifecycle validation to date (borrow/return flow, source-Excel import,
> no-UID-in-logs check) was done via **Path B (native run)** against a local
> filesystem path standing in for the CIFS mount. Path A remains available
> for anyone with the tooling installed; do not read the native-run results
> as having exercised the CIFS/QEMU boot path.

---

## Files in `sim/`

```
sim/
├── README.md                       This walkthrough
├── .env.sim.example                Sample env (copy to repo root as .env)
├── data/
│   ├── make_sample_data.py         Generates the workbook + photo placeholders
│   ├── Messmittelliste.sample.xlsx Sample device master list (9 schrank imported, 2 skipped)
│   └── photos/                     Filenames == device model exactly (see make_sample_data.py)
│       ├── 87V.jpg                 Model "87V" — matches BOTH 87V units (shared-model rule)
│       ├── 287.jpg                 Model "287"
│       ├── 376 FC.jpg              Model "376 FC" (space in the name is part of the match)
│       ├── 175T1.jpg              Model "175T1"
│       └── 1587 FC.jpg             Model "1587 FC" (a borrowed device — photo still attaches)
├── native/
│   ├── run-native.sh               Fast native run (Linux / macOS / Git Bash)
│   └── run-native.ps1              Fast native run (Windows PowerShell)
├── qemu/
│   └── run-qemu.sh                 Boot Pi OS arm64 in QEMU
└── samba/
    ├── docker-compose.yml          Samba M: stand-in via Docker
    ├── smb.conf                    Alternative: host-run smbd config
    └── share-data/                 Drop workbook + photos here (created by you)
```

Cross-reference the real deploy docs: `deploy/README.md` and `GUIDE.md` for
the authoritative production walkthrough. This file covers only the sim path.

---

## Prerequisites

### Common to both paths

- Python 3.11+ with a working `venv` at `<repo>/venv/`
- `<repo>/.env` filled in (see Step 0 below)

### Path A (QEMU) additionally requires

- QEMU >= 6.2 (`qemu-system-aarch64` with the `raspi4b` machine type)
- A Raspberry Pi OS arm64 image (manual download — Step A1)
- Docker or host smbd for the Samba stand-in (Step A3)

### Path B (native) requires nothing beyond the common prerequisites.

---

## Step 0 — One-time setup (both paths)

```bash
# 1. Create and activate the Python venv
python3 -m venv venv
source venv/bin/activate              # Linux / macOS / Git Bash
# .\venv\Scripts\Activate            # Windows PowerShell

# 2. Install dependencies
pip install -r requirements.txt

# 3. Generate the two cryptographic keys
python -m scripts.generate_key
#  -> outputs two base64 strings; paste them into .env (step 4)

# 4. Copy the sim env template and fill in the generated keys
cp sim/.env.sim.example .env
#    Edit .env and paste the ENC_KEY and HMAC_KEY values.
#    All other values in the template are pre-configured for the sim.

# 5. Initialise the database
python -m scripts.init_db

# 6. Enroll the sample admin card
#    The UID must match SMART_LOCKER_FAKE_DEFAULT_UID in .env (default: AABBCCDD)
python -m scripts.enroll_card \
  --name "Sim Admin" \
  --role admin \
  --uid AABBCCDD

# 7. Generate the sample workbook and photo placeholders
python sim/data/make_sample_data.py
#  -> writes sim/data/Messmittelliste.sample.xlsx
#  -> writes sim/data/photos/87V.jpg and 175T1.jpg
```

---

## Path B — Fast native run (daily iteration, no QEMU)

This is the recommended path for day-to-day development. The app runs on
your host machine with the fake reader; no QEMU, no Samba, no pcscd.

```bash
# Linux / macOS / Git Bash
bash sim/native/run-native.sh

# Windows PowerShell
.\sim\native\run-native.ps1
```

The script exports `SMART_LOCKER_FAKE_READER=1` and points the source path at
`sim/data/Messmittelliste.sample.xlsx` before launching the app.

Once running:
- Kiosk UI: **http://localhost:8000**
- Admin dashboard: **http://localhost:8000/admin**
- Simulate a tap: see [Injecting a simulated card tap](#injecting-a-simulated-card-tap) below

---

## Path A — Full Pi OS rehearsal in QEMU

This path runs the **real** `deploy/install/install.sh` installer inside an
emulated Raspberry Pi 4 running Pi OS arm64. Use it to rehearse the production
install before touching real hardware.

### Step A1 — Obtain the Pi OS arm64 image (manual download)

> This step is intentionally manual. Automated downloads of OS images are out
> of scope for this harness.

1. Go to: <https://downloads.raspberrypi.com/raspios_lite_arm64/images/>
2. Download the latest `.img.xz` (Pi OS Lite arm64 is sufficient).
3. Decompress: `xz -d *.img.xz`
4. You now have a raw `.img` file (≈ 2-4 GB).

### Step A2 — Extract the kernel and DTB from the image

```bash
# Find the byte offset of the FAT32 boot partition (partition 1):
OFFSET=$(fdisk -l raspios-bookworm-arm64-lite.img \
          | awk '/FAT32/{print $2 * 512}' | head -1)

sudo mkdir -p /mnt/pios-boot
sudo mount -o loop,offset=$OFFSET raspios-bookworm-arm64-lite.img /mnt/pios-boot
cp /mnt/pios-boot/kernel8.img        sim/qemu/
cp /mnt/pios-boot/bcm2711-rpi-4-b.dtb sim/qemu/
sudo umount /mnt/pios-boot
```

On macOS, use `hdiutil attach -imagekey diskimage-class=CRawDiskImage` to
mount the image and copy from the FAT volume.

### Step A3 — Start the Samba M: stand-in

The real Pi imports devices from a company M: drive (SMB/CIFS). This step
provides an equivalent share using Docker Compose.

```bash
# Put the sample workbook where the share can serve it:
mkdir -p sim/samba/share-data/photos
cp sim/data/Messmittelliste.sample.xlsx sim/samba/share-data/Messmittelliste.xlsx
cp sim/data/photos/*.jpg                sim/samba/share-data/photos/

# Start the Samba container (Docker must be running):
cd sim/samba
docker compose up -d
cd -

# Verify the share is accessible from the host:
smbclient //localhost/locker -U simuser%simpass -c "ls"
```

**Without Docker** — edit `sim/samba/smb.conf`, update the path, then:

```bash
sudo cp sim/samba/smb.conf /etc/samba/smb.conf
sudo smbpasswd -a simuser    # set password to: simpass
sudo systemctl restart smbd
```

### Step A4 — Boot the QEMU guest

```bash
cd sim/qemu
bash run-qemu.sh                   # uses kernel8.img and bcm2711-rpi-4-b.dtb in sim/qemu/
# OR specify files explicitly:
bash run-qemu.sh /path/to/raspios.img /path/to/kernel8.img /path/to/bcm2711-rpi-4-b.dtb
```

The script forwards:
- Guest port **22** → host port **2222** (SSH)
- Guest port **8000** → host port **8000** (kiosk)

Default credentials for Pi OS: `pi` / `raspberry`. Enable SSH on first boot
via `sudo systemctl enable --now ssh` inside the serial console.

From the host:
```bash
ssh -p 2222 pi@localhost
```

### Step A5 — Clone and install inside the guest

Inside the QEMU guest (via SSH or the serial console):

```bash
# Clone the repo (or copy the tarball)
git clone <your-repo-url> ~/smart_locker
cd ~/smart_locker

# Run the installer (mirrors the production flow exactly)
sudo deploy/install/install.sh
# This runs as root, creates the locker user, installs the venv, installs
# the systemd service, and scaffolds /mnt/locker. See deploy/README.md.
```

### Step A6 — Configure and mount the share inside the guest

```bash
# Copy and fill in the env file
cp deploy/.env.pi.example .env
# Edit .env:
#   SMART_LOCKER_ENC_KEY  / SMART_LOCKER_HMAC_KEY  <- generate_key output
#   SMART_LOCKER_FAKE_READER=1                      <- enable fake NFC reader
#   SMART_LOCKER_FAKE_DEFAULT_UID=AABBCCDD          <- default tap UID
#   SMART_LOCKER_SOURCE_EXCEL_PATH=/mnt/locker/Messmittelliste.xlsx

python -m scripts.generate_key     # paste keys into .env

# Set up CIFS credentials (the Samba user defined in docker-compose.yml):
sudo tee /etc/smart-locker/cifs-credentials <<EOF
username=simuser
password=simpass
domain=WORKGROUP
EOF
sudo chmod 600 /etc/smart-locker/cifs-credentials

# Add the fstab line (host is 10.0.2.2 = QEMU user-net gateway):
echo "//10.0.2.2/locker /mnt/locker cifs credentials=/etc/smart-locker/cifs-credentials,uid=locker,gid=locker,file_mode=0664,dir_mode=0775,iocharset=utf8,vers=3.0,nofail 0 0" \
  | sudo tee -a /etc/fstab
sudo mount /mnt/locker
ls /mnt/locker        # should show Messmittelliste.xlsx and photos/

# Initialise DB and enroll the sample admin card
venv/bin/python -m scripts.init_db
venv/bin/python -m scripts.enroll_card \
  --name "Sim Admin" --role admin --uid AABBCCDD

# Start the service
sudo systemctl start smart-locker
sudo journalctl -u smart-locker -f
```

From the **host** browser: **http://localhost:8000**

---

## Injecting a simulated card tap

The fake NFC reader (`smart_locker/nfc/fake_reader.py`) is activated by
`SMART_LOCKER_FAKE_READER=1` (set automatically by `run-native.sh`; add to
`.env` for the QEMU path). It exposes three ways to inject a tap:

### 1. Browser button / F2 key

When `GET /api/dev/status` returns `{"fake_reader": true, ...}`, the kiosk
frontend (`smart_locker/frontend/app.js`, `initDevTap()`) renders a floating
dark-red button labelled **"⊙ Simulate tap (F2)"** in the bottom-right corner.
Clicking it — or pressing **F2** — calls `POST /api/dev/tap` with no body,
which falls back to `SMART_LOCKER_FAKE_DEFAULT_UID`.

### 2. HTTP (curl / REST client)

```bash
# Tap with the default UID (from SMART_LOCKER_FAKE_DEFAULT_UID):
curl -s -X POST http://localhost:8000/api/dev/tap \
     -H "Content-Type: application/json" \
     -d '{}'

# Tap with a specific UID:
curl -s -X POST http://localhost:8000/api/dev/tap \
     -H "Content-Type: application/json" \
     -d '{"uid": "AABBCCDD"}'

# Confirm the fake reader is active:
curl -s http://localhost:8000/api/dev/status
# -> {"fake_reader": true, "default_uid_set": true}
```

Returns `{"ok": true}` on success; `404` if the fake reader is not active;
`400` if no UID is available (set `SMART_LOCKER_FAKE_DEFAULT_UID`).

### 3. POST /api/dev/tap from the QEMU host

When using Path A, the kiosk is also reachable at **http://localhost:8000**
from the host (via port-forward). The `curl` commands above work from the host
unchanged.

### Security invariant

Tap injection goes through `FakeNFCReader.simulate_tap(uid)` which enqueues
a `CardEvent(INSERTED)` — the same event the real reader posts. The UID is
**never logged**; log lines read:
```
Simulated card tap on FAKE-ACR1252U (simulated).
```
This is identical to the real reader's invariant ("Card inserted on <reader>").

---

## Triggering a source-Excel import

```bash
# From the repo root (app must be running in another terminal for SSE events):
python -m scripts.sync_source

# Or via the admin API endpoint:
curl -s -X POST http://localhost:8000/api/admin/sync-source

# The daily cron fires at 06:00 by default (SMART_LOCKER_SOURCE_SYNC_HOUR=6).
```

The importer (`smart_locker/sync/source_import.py`) reads
`SMART_LOCKER_SOURCE_EXCEL_PATH`, filters rows whose
**Platz Messmittelschrank** column starts with "Schrank" (case-insensitive),
and interprets **Aktueller Einsatzort**:
- Contains "schrank" → device is in the locker (**AVAILABLE**)
- Non-empty, no "schrank" → borrower name (**BORROWED**)
- Empty → no change to current status

The sample workbook (`sim/data/Messmittelliste.sample.xlsx`) produces:
- 9 imported schrank rows — **7 AVAILABLE** (Schrank A1–A7) + **2 BORROWED**
  (1587 FC → Max Mustermann, 1736 → Anna Schmidt)
- 2 rows skipped — non-schrank slot ("Regal B1") or empty slot
- 3 registrant names harvested from "Aktueller Einsatzort" — Max Mustermann,
  Anna Schmidt, **and Lukas Weber** (whose device is on the *skipped* non-schrank
  row, showing that names are read from every row, not just imported ones)

The first source import (into an empty DB) reports **9 new**; a re-import with no
workbook change reports **0 new / 0 updated / 9 unchanged**. Editing a row (e.g. a
borrower returning a device → its "Aktueller Einsatzort" flips back to "Schrank …")
makes the next import report it as **1 updated**.

**inotify / network-path note:** The CIFS mount point (`/mnt/locker`) is
detected as a network path by `sync/fs_utils.is_network_path()`. The file
watcher intentionally skips live monitoring on network paths (inotify never
fires for remote writes on a CIFS client). Import is triggered by the cron
and manually — **do not swap in PollingObserver** to work around this; it doesn't fix
the underlying CIFS/inotify incompatibility.

---

## Verifying the security invariant

After a tap and borrow/return cycle, confirm no UID appears in the log:

```bash
grep -i "uid\|AABBCCDD\|aabb\|ccdd" logs/smart_locker.log && echo "FAIL: UID leaked" || echo "PASS: no UID in log"
```

Expected log lines:
```
INFO  Fake NFC reader started (simulation mode — no hardware).
INFO  Simulated card tap on FAKE-ACR1252U (simulated).
INFO  Card inserted on FAKE-ACR1252U (simulated).
```

---

## Regenerating the sample data

```bash
python sim/data/make_sample_data.py
```

Re-run whenever you modify `ROWS` or `PHOTO_MODELS` in that script. The xlsx
and jpg files in `sim/data/` are checked into the repo as convenience copies.
