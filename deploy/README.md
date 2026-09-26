# deploy/ — Raspberry Pi appliance provisioning

Everything needed to turn a stock Raspberry Pi 4 (64-bit Raspberry Pi OS) into a
Smart Locker kiosk: a systemd backend service, a Chromium kiosk autostart, the
locker file-share (CIFS) mount, and an offline Python install.

> **Setup walkthrough: [`../GUIDE.md`](../GUIDE.md).**
> This file is the technical index — what each artifact is and the command sequence.

## What's here

| Path | What it is |
|---|---|
| `.env.pi.example` | Environment template for the Pi — copy to the repo root as `.env`. Paths are pre-filled for the share mount. |
| `install/install.sh` | One-shot, idempotent provisioner. Run as root from the repo: `sudo bash deploy/install/install.sh` (`bash <script>` is intentional — exFAT copies strip the +x bit, see `../GUIDE.md` 3c). |
| `install/sudoers-smart-locker` | Sudoers template: restart own service, launch `update.sh`, `systemctl poweroff`. |
| `install/apply-sudoers.sh` | Renders the template to `/etc/sudoers.d/smart-locker` after `visudo -cf`. |
| `install/update.sh` | Applies gitignored `locker-updates/` from USB into `$APP_DIR/locker-updates` (backup, swap, health, rollback). |
| `install/build-wheelhouse.sh` | Builds the **complete** offline kit in one run: downloads all Python wheels for offline install **and** auto-fetches the `python3-pyscard` `.deb` into `deploy/system-packages/`. Works from an aarch64 host directly, OR from any other machine (e.g. Windows/x86_64) via pip's cross-platform `--platform`/`--python-version`/`--abi` flags — no aarch64 hardware needed to build it. Targets Python 3.13 / cp313 (trixie). |
| `wheelhouse/` | Where those wheels are staged (the `.whl` files are gitignored). |
| `system-packages/` | Holds the `python3-pyscard` `.deb` — `pyscard` has no prebuilt Linux aarch64 wheel on PyPI, so it's installed via `dpkg`/`apt` instead of pip. See `system-packages/README.md`. |
| `systemd/smart-locker.service` | Backend service: starts uvicorn + the NFC listener on boot, restarts on crash. |
| `kiosk/start-kiosk.sh` | Launches Chromium fullscreen at `http://localhost:8000` (auto-detects `chromium`/`chromium-browser`, waits for the backend). |
| `kiosk/smart-locker-kiosk.desktop` | XDG autostart entry that runs `start-kiosk.sh` on graphical login. |
| `mount/fstab.snippet` | The `/etc/fstab` CIFS line for the locker share (`nofail` + automount so a missing share never blocks boot). Connected **last**, after everything else works — see `../GUIDE.md` Section 6. |
| `mount/cifs-credentials.example` | Template for `/etc/smart-locker/cifs-credentials` (root-only, `chmod 600`). |

## Architecture in one breath

```
Pi boot
  ├─ pcscd.service ............ talks to the ACR1252U NFC reader (PC/SC)
  ├─ /mnt/locker (CIFS) ....... the locker file share, mounted via fstab (lazy automount)
  ├─ smart-locker.service ..... uvicorn backend on :8000 + background NFC listener
  └─ graphical login (auto)
        └─ start-kiosk.sh ..... Chromium --kiosk -> http://localhost:8000 on the touch display
```

`.env` (repo root) points `SMART_LOCKER_SOURCE_EXCEL_PATH` and `SMART_LOCKER_EXCEL_PATH`
at `/mnt/locker/...`, so the device list is **imported from** the share. Auto-writing
`smart_locker_data.xlsx` is **off** unless `SMART_LOCKER_EXCEL_AUTO_EXPORT=1`. The SQLite
database stays on the Pi's **local** disk — never on the CIFS share (WAL mode is unreliable
there).

## First-build command sequence

If the Pi will never have internet access at all, build the offline kit **before** the Pi
ever boots at the deployment site — see `../GUIDE.md` Section 3b/4.1 for the full
walkthrough (SD card partitions aren't readable from Windows, so getting the project onto
the Pi needs a USB stick, not a direct copy):

```bash
# 0. On ANY machine with internet (aarch64 not required — cross-platform download):
deploy/install/build-wheelhouse.sh
#    (auto-downloads the python3-pyscard .deb into deploy/system-packages/ — nothing else to fetch)

# On the Pi, with the repo at e.g. /home/locker/smart_locker:
sudo bash deploy/install/install.sh        # packages (incl. pyscard via apt/.deb), venv, pcscd, service, kiosk

cp deploy/.env.pi.example .env        # then edit .env
venv/bin/python -m scripts.generate_key   # paste keys into .env
venv/bin/python -m scripts.init_db
venv/bin/python -m scripts.enroll_card --name "Your Name" --role admin

sudo systemctl start smart-locker     # then reboot to test kiosk autostart

# LAST — connect the locker share, once the kiosk itself is proven working:
sudo nano /etc/smart-locker/cifs-credentials              # real share login
sudo nano /etc/fstab                  # add the line from deploy/mount/fstab.snippet
sudo mount /mnt/locker && ls /mnt/locker                  # verify the share
venv/bin/python -m scripts.sync_source --file "/mnt/locker/<workbook>.xlsx"
```

## Verifying a running Pi

```bash
systemctl status smart-locker                 # active (running)
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/         # 200
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/dashboard # 200
mount | grep cifs                             # locker share is mounted
pcsc_scan                                     # the ACR1252U is detected (Ctrl-C to exit)
journalctl -u smart-locker -n 50 --no-pager   # recent backend logs
```
