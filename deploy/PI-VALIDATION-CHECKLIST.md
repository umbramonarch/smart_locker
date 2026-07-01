# Raspberry Pi On-Hardware Validation Checklist

The no-hardware simulation (`sim/` — QEMU + Samba + fake NFC reader) rehearses the
install, the systemd service, the kiosk, the source import, and the borrow/return
flow. It deliberately **cannot** confirm two things, which is what this checklist
is for:

| Not covered by QEMU/sim | Why | Confirmed here |
|---|---|---|
| Real ACR1252U card reads | QEMU has no PC/SC passthrough; the sim uses the fake reader | §3 |
| Real kiosk GPU smoothness | QEMU has no VideoCore VI; the FPS probe is best-effort only | §4 |

Run this once on the actual Pi 4 + Riverdi display after provisioning. Tick every
box; record the result in the sign-off table at the end.

---

## 1. Pre-flight (before first boot)

- [ ] SD card flashed with 64-bit Raspberry Pi OS **Full** (see `GUIDE.md` Step 0).
- [ ] Offline install kit built and copied over via USB stick if the Pi has no internet:
      `deploy/wheelhouse/*.whl` + `deploy/system-packages/*.deb` present (`GUIDE.md` Step 0b).
- [ ] Repo at `/home/locker/smart_locker`; `sudo deploy/install/install.sh` ran clean.
- [ ] `import smartcard` works in the venv (confirms `python3-pyscard` +
      `--system-site-packages` — `GUIDE.md` Section 4.3).
- [ ] `.env` has real `SMART_LOCKER_ENC_KEY` + `SMART_LOCKER_HMAC_KEY` (32-byte each).
- [ ] `SMART_LOCKER_FAKE_READER` is **unset / not `1`** (production uses the real reader).
- [ ] Kiosk boots and runs correctly with the M: share **not yet connected** (Sections 4-5
      of `GUIDE.md` are all reachable with zero network).
- [ ] **Last:** `/etc/smart-locker/cifs-credentials` filled; fstab line points at the real
      `//SERVER/share`; `SMART_LOCKER_SOURCE_EXCEL_PATH` points at the workbook on the
      mounted `/mnt/locker` (`GUIDE.md` Section 6).

## 2. Boot & service

- [ ] `sudo systemctl status smart-locker` → `active (running)`, no crash loop.
- [ ] Kill the process (`sudo systemctl kill smart-locker`) → systemd restarts it (Restart=on-failure).
- [ ] `journalctl -u smart-locker -b` shows a clean startup and the startup source import.
- [ ] After a full reboot, the service comes up headless with no login.

## 3. NFC reader — REAL hardware (QEMU cannot test this)

- [ ] `pcscd` running; `pcsc_scan` (or app log) detects `ACR1252U`.
- [ ] Tap an enrolled card → kiosk authenticates and shows the user's main menu.
- [ ] Second tap → session ends (logout).
- [ ] Tap an **unenrolled** card → "not registered" screen (no crash).
- [ ] Unplug the reader mid-session → UI shows "reader disconnected"; replug → recovers.
- [ ] **Security:** `grep -ri <the card's UID> logs/` returns **nothing** — raw UIDs are never logged.

## 4. Kiosk display & performance — REAL GPU (QEMU cannot test this)

- [ ] Chromium launches fullscreen on the Riverdi display; no cursor, no chrome.
- [ ] `<html>` has class `lite` (the launcher forces `?lite`) — confirm via remote DevTools or by the flat UI.
- [ ] Idle screen animation is smooth (no visible stutter) for ≥60 s.
- [ ] Borrow grid scroll is smooth; screen transitions do not drop frames.
- [ ] Touch targets respond on the first tap; no ghost/double taps.
- [ ] If any jank is seen even in lite mode, note it — that is a real-hardware-only finding.

## 5. M: share & source import

- [ ] `mount | grep /mnt/locker` shows the CIFS mount; the workbook is readable.
- [ ] Admin panel → **Sync Source** → preview shows add/update/skip counts → confirm → counts applied.
- [ ] Edit the workbook on M: from another PC → within the poll interval (default 30 s) the change imports (check `journalctl` for `mtime poll`).
- [ ] Reboot with the share **unavailable** → boot still completes (nofail), service starts, kiosk loads.
- [ ] (If `SMART_LOCKER_EXCEL_AUTO_EXPORT=1`) the exported workbook is refreshed on M: after an import.

## 6. Sign-off

| Section | Pass/Fail | Notes |
|---|---|---|
| 1 Pre-flight | | |
| 2 Boot & service | | |
| 3 NFC reader | | |
| 4 Display & perf | | |
| 5 M: share & import | | |

Validated by: _______________  Date: _______________  Pi serial: _______________
