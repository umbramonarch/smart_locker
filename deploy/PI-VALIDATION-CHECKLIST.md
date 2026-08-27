# Raspberry Pi On-Hardware Validation Checklist

This checklist is the human sign-off on the **real** Pi 4 + ACR1252U + Riverdi display
+ locker share. Pytest on a PC cannot confirm a card tap, GPU smoothness, or CIFS.

Run it once after provisioning. Tick every box; record the result in the sign-off
table at the end.

---

## 1. Pre-flight (before first boot)

- [ ] SD card flashed with 64-bit Raspberry Pi OS **Full** (see `GUIDE.md` Step 0).
- [ ] Offline install kit built and copied over via USB stick if the Pi has no internet:
      `deploy/wheelhouse/*.whl` + `deploy/system-packages/*.deb` present (`GUIDE.md` Step 0b).
- [ ] Repo at `/home/locker/smart_locker`; `sudo bash deploy/install/install.sh` ran clean.
- [ ] `import smartcard` works in the venv (confirms `python3-pyscard` +
      `--system-site-packages` — `GUIDE.md` Section 4.3).
- [ ] `.env` has real `SMART_LOCKER_ENC_KEY` + `SMART_LOCKER_HMAC_KEY` (32-byte each).
- [ ] `SMART_LOCKER_FAKE_READER` is **unset / not `1`** (production uses the real reader).
- [ ] Kiosk boots and runs correctly with the locker share **not yet connected** (Sections 4-5
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
- [ ] Tap an enrolled **work card** → kiosk authenticates and shows the scan-first main menu.
- [ ] Second **work-card** tap → session ends (logout). A device sticker does not log out.
- [ ] After login, tap an NTAG/sticker bound to a locker device → borrow (or return if already yours); session stays open.
- [ ] Tap an **unenrolled** card → "not registered" screen (no crash). Bound **available** device tag at idle → short "tap your work card first" (not auth-failed, not borrowed). Bound **borrowed** device tag at idle → returned without a work card; large "Put in slot N" overlay; next work-card tap still logs in.
- [ ] Admin **Register User** → enroll a new card → kiosk returns to idle (welcome copy, then idle). That new card's **next** tap logs in (`auth_success`), not logout. An expired 60s register window must not steal the next work-card tap.
- [ ] Unplug the reader mid-session → UI shows "reader disconnected"; replug → recovers.
- [ ] **Security:** `grep -ri <the card's UID> logs/` returns **nothing** — raw UIDs are never logged.

## 4. Kiosk display & performance — REAL GPU (QEMU cannot test this)

- [ ] Chromium launches fullscreen on the Riverdi display; no cursor, no chrome.
- [ ] `<html>` has class `lite` (the launcher forces `?lite`) — confirm via remote DevTools or by the flat UI.
- [ ] Idle screen animation is smooth (no visible stutter) for ≥60 s.
- [ ] After login, main menu is scan-first. **Locker** opens the in/out availability overlay; cards and device detail show the **PM number**; screen-pick **Confirm Borrow** still works for untagged units.
- [ ] Locker overlay scroll is smooth; screen transitions do not drop frames.
- [ ] Touch targets respond on the first tap; no ghost/double taps.
- [ ] If any jank is seen even in lite mode, note it — that is a real-hardware-only finding.

## 5. locker share & source import

- [ ] `mount | grep /mnt/locker` shows the CIFS mount; the workbook is readable.
- [ ] Admin panel → **Sync Source** → preview shows add/update/skip counts → confirm → counts applied.
- [ ] Edit the workbook on the share from another PC → **Sync Source** (or wait for the
      6-hour interval) imports the catalog change (check `journalctl` for source import).
- [ ] Reboot with the share **unavailable** → boot still completes (nofail), service starts, kiosk loads.
      Admin last-sync line is not "never" if a previous import was recorded.
- [ ] Auto-export of `smart_locker_data.xlsx` is off unless `SMART_LOCKER_EXCEL_AUTO_EXPORT=1`.
      Admin **Export Excel** still downloads a snapshot.
- [ ] Software update from a signed tarball (`pack_release` → `/mnt/locker/locker-updates/`): admin **Software Update** overlay, or SSH `sudo bash deploy/install/update.sh`.

## 6. Sign-off

| Section | Pass/Fail | Notes |
|---|---|---|
| 1 Pre-flight | | |
| 2 Boot & service | | |
| 3 NFC reader | | |
| 4 Display & perf | | |
| 5 locker share & import | | |

Validated by: _______________  Date: _______________  Pi serial: _______________
