# After update — Pi still on git `308a5aa2`

Short checklist for a Pi whose live tree is still
`308a5aa2` (`pack_release` + HMAC tarball on CIFS). That updater cannot apply
the new payload. Full notes: `GUIDE.md` Section 9.

**What changed:** signed tarball + `.hmac` on `/mnt/locker/locker-updates` is
gone. Windows `python -m scripts.copy_update` writes gitignored `locker-updates/`
and copies it onto the USB stick. The new `update.sh` finds that folder on USB,
copies it to `/home/locker/smart_locker/locker-updates` (then you can unplug),
and applies with backup / health / rollback. Live `.env` and SQLite stay.

CIFS (`/mnt/locker`) is still Excel import/export and photos. Not software
updates.

---

## First time only (chicken-and-egg)

The `update.sh` on this Pi still looks for `smart-locker-*.tar.gz` + HMAC.
Do **not** drag the USB tree onto `/home/locker/smart_locker` in the file
manager (that can smash `.env` / SQLite).

1. On Windows, from this checkout: `python -m scripts.copy_update --dest D:\`
   (omit `--dest` if exactly one removable drive is plugged in).
2. If it **warns** about missing wheels, rebuild `deploy/install/build-wheelhouse.sh`
   and re-run copy_update. A warning still copies; pip failure on the Pi rolls
   back. Rebuild only when you see that warning.
3. Plug the stick into the Pi. Raspberry Pi OS mounts it at
   `/media/<user>/<label>/` (usually `/media/locker/<label>/`).
4. Copy **only** `locker-updates/deploy/install/update.sh` over the live
   `deploy/install/update.sh`. Keep LF — do not open the file in Notepad.

   ```bash
   sudo cp /media/locker/*/locker-updates/deploy/install/update.sh \
     /home/locker/smart_locker/deploy/install/update.sh
   ```

5. Leave the stick plugged in. Idle **clock 5× within 3 s** → **Software
   Update**. If the kiosk is down: `sudo bash /home/locker/smart_locker/deploy/install/update.sh`.

---

## Later updates

Windows `python -m scripts.copy_update --dest D:\` → plug USB → clock 5× →
**Software Update**. No hand-copy of `update.sh`.

---

## Never copy onto the live Pi

`.env`, `smart_locker.db` (`-wal` / `-shm`), `venv/`, `logs/`, `backups/`.

`copy_update` already skips these. Extra wheels go via copy_update only when
it warns.

## After this jump (optional)

`SMART_LOCKER_UPDATE_HMAC_KEY` and `SMART_LOCKER_UPDATE_DIR` are unused. Leave
them in `.env` (harmless) or delete those two lines.
