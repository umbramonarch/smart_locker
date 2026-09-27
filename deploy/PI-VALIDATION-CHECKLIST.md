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
      `//SERVER/share`; `SMART_LOCKER_MIRROR_PATH` points at the workbook on the
      mounted `/mnt/locker` (`GUIDE.md` Section 6).

## 2. Boot & service

- [ ] `sudo systemctl status smart-locker` → `active (running)`, no crash loop.
- [ ] Kill the process (`sudo systemctl kill smart-locker`) → systemd restarts it (Restart=always).
- [ ] `journalctl -u smart-locker -b` shows a clean startup and the startup mirror tick
      (adopts an existing catalog sheet on first sight, otherwise writes pending changes).
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

## 5. locker share & catalog mirror

- [ ] `mount | grep /mnt/locker` shows the CIFS mount; the mirror workbook is writable.
- [ ] Admin panel → **Sync Sheet** runs a mirror tick; the sheet on the share is
      rewritten from the SQLite catalog. Check `journalctl` for the mirror write.
- [ ] The **first** mirror write against an adopted sheet rewrites the whole sheet
      to the catalog columns — extra/foreign columns are removed. Keep a copy of
      the old sheet first if it carried notes.
- [ ] Hand edits in the sheet are **held for admin review** on the dashboard
      (Apply sheet edits / Keep database) — they are never silently merged.
- [ ] Borrow or return on the kiosk updates **Location** in
      `device-list.xlsx` for that PM (`Locker` when in the locker, borrower name
      when out, `Maintenance` for a maintenance unit). If the file is open
      elsewhere, the kiosk still works; the write lands on the next tick.
- [ ] Edit a catalog field in the workbook from another PC → the next tick
      flags a hand edit; the dashboard banner and Admin overlay show the diff.
      **Apply** writes the sheet edits into SQLite; **Keep database** dismisses
      them and the next write overwrites the sheet.
- [ ] Reboot with the share **unavailable** → boot still completes (nofail), service starts, kiosk loads.
      Admin last-sync line is not "never" if a previous tick was recorded.
- [ ] Software update from USB `locker-updates/` (`python -m scripts.copy_update` on Windows): admin **Software Update** overlay, or SSH `sudo bash deploy/install/update.sh`. Do not overlay the stick onto `/home/locker/smart_locker` in the file manager.
- [ ] Admin **Register Device**: pick a registerable catalog row (or type its PM) + free slot → the row gets the slot and waits for the sticker; unknown PM is refused. Existing rows Bind / **Replace tag** / Unbind / Slot still work.
- [ ] On a **blank** database: `GET /api/setup` reports `needed:true`; kiosk 5× clock tap opens **First Admin Setup** (name + required dashboard password → tap card within 60 s → admin enrolled, `needed:false`). The typed password lands in `dashboard.secret` (mode `640`, owned by `locker` — **not** `.env`, which stays root-owned). **Software Update** works from that screen with no admin enrolled; from a LAN browser `POST /api/admin/update` is refused until the secret exists. A `POST /api/setup` from the LAN returns 403.
- [ ] Chromium starts without a browser keyring/password prompt (`--password-store=basic` in `start-kiosk.sh`).
- [ ] Admin **Stop system** (confirm) closes Chromium, stops any in-flight `smart-locker-update` unit, then stops `smart-locker` (`systemctl status smart-locker` → `inactive`, still `enabled`). Dashboard on `:8000` stops answering; the Pi stays powered on. The next boot starts the service and kiosk normally.
- [ ] Admin **Shut down** (confirm) powers the Pi off. After the first update of this feature, if the button errors, SSH `sudo bash deploy/install/apply-sudoers.sh` once.
- [ ] From another PC, open `http://<pi>:8000/dashboard`. **Inventory** lists the full SQLite catalog (share down does **not** break the tab). Owner click works for PMs that are **not** in the locker (public); locker PMs are not editable. **Locker** lists SQLite locker devices (Tagged / No tag; no owner edit). **Display** follows the kiosk screen (idle / main menu / locker / return / admin) and shows the signed-in user. The **Admin** button in the header opens the overlay: catalog editor, mirror diffs, users, logs, Unbind / Replace tag. Cursor and scroll work; the page is kiosk colours, not a second kiosk. If `SMART_LOCKER_PUBLIC_URL` and `SMART_LOCKER_DASHBOARD_SHARE_PATH` are set, `dashboard.url` on the share opens the live page.

## 6. Sign-off

| Section | Pass/Fail | Notes |
|---|---|---|
| 1 Pre-flight | | |
| 2 Boot & service | | |
| 3 NFC reader | | |
| 4 Display & perf | | |
| 5 locker share & import | | |

Validated by: _______________  Date: _______________  Pi serial: _______________
