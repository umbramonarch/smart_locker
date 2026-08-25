# Plan: NFC device tags (replace barcode scanner)

**Branch:** `feature/nfc-device-tags` (cut from `main` at MR #2 / `467de11`; deleted after merge)
**Status:** implemented — merged to `main` in [MR #3](https://git.example.invalid/anas.alshaer/smart_locker/pull/3) (`63f0cf9`)
**Hardware:** same ACR1252U as work cards. No USB barcode scanner.

Shipped as specified. Historical plan — do not treat this file as open work.

---

## 1. Goal

A colleague taps their **work card** to log in, then either:

- taps an **NFC sticker on the device** on the same reader, or
- picks the device on the touch UI (existing Borrow / Return grids).

The system records who has that exact unit. Auto-intent: a device tap borrows if the unit is available, and returns it if the logged-in user already has it.

Device rows still come from Excel (**schrank** rows only). **Register Device** in the hidden admin panel binds a sticker to an existing locker device. The list must show **name and PM number**, because several units can share a name and differ only by PM.

---

## 2. Locked decisions

| Topic | Decision |
|---|---|
| Intent | **Auto-intent.** After login, a device-tag tap borrows or returns from device status. No extra “Borrow or Return?” step. |
| Post-login UI | **Change the main menu** to scan-first. Keep Borrow / Return as “or pick on screen.” Do not skip the menu. Do not merge the two grids into one inventory on login. |
| Register Device | Admin bind only. Does **not** create a device. Excel/schrank import already created the row. |
| List identity | Show **name + PM** (and slot if present). Same name, different PM → two rows. |
| Tag storage | `devices.tag_hmac` (HMAC-SHA256 of UID, same key as `users.uid_hmac`). **Never** store or log the raw UID. No encrypted UID on devices. |
| One sticker | One tag per device. Re-bind replaces the HMAC on that row. |
| Excel | Re-import must **not** overwrite `tag_hmac` (same idea as `locker_slot`). Do **not** reuse `devices.barcode`. |
| Lookup | Classify UID by HMAC in users, then devices. Not by card type, NDEF, or which screen is showing. |
| Second tap | A **work card** tap still logs out. A **device tag** does not. |
| Session | Stays open after a tag borrow/return so several devices can be tagged in one login. |
| Return-of-others | Unchanged: owner, or admin on behalf. |
| Barcode scanner | **Not built.** GUIDE §15 / README barcode plan is replaced by this flow when we update those docs. |

---

## 3. Why this instead of a barcode scanner

GUIDE §15 (not built) was: tap card → Borrow/Return → USB scanner types digits + Enter → `GET /api/devices/barcode/{barcode}`.

That extra HID device fights the Chromium kiosk (focus, “types into the wrong field”) and uses another USB port. The ACR1252U already returns a UID for NTAG/Ultralight/Classic via `GET_UID` (`FF CA 00 00 00`). Cheap stickers are another UID on the same pipeline.

`devices.barcode` stays an Excel string. Re-import **does** overwrite it. Tag bindings are locker-local, like slot.

---

## 4. What is already true in the code

- Idle → work-card HMAC lookup → session → main menu (`Borrow` / `Return` / `End Session`).
- NFC bridge (`smart_locker/api/app_context.py`): **any** insert while a session is active **logs out before lookup**. A device sticker would currently end the session. That is the main state-machine change.
- `LockerService` already enforces `MAX_BORROWS`, available-only borrow, owner-or-admin return, and `touch()` on success.
- Frontend `S.mode` (`borrow` / `return`) is **UI-only**. The backend has no current screen. Auto-intent must live in the backend from device status, not from a new “mode” API.
- Fake reader `simulate_tap(uid)` already injects any UID. No second fake-reader class.
- Admin panel: idle clock 5× within 3 s. **Register User** already: pick/type name → 60 s tap window.
- Import: schrank rows only; `DeviceRepository.update_metadata` ALLOWED set does not include slot/image/description. `tag_hmac` must stay off that set.

---

## 5. Data model

Add one nullable unique column:

```text
devices.tag_hmac  VARCHAR(64)  NULL  UNIQUE  INDEX ix_devices_tag_hmac
```

- Same `compute_uid_hmac()` + `SMART_LOCKER_HMAC_KEY` as work cards.
- SQLite UNIQUE allows many NULLs (unbound devices).
- New DBs: SQLAlchemy `create_all` in `init_db`.
- Existing Pi DBs: `scripts/migrate_db.py` ALTER + unique index (`update.sh` already runs migrate).

Do **not** put `tag_hmac` on Excel export, public dashboard JSON, or kiosk `GET /api/devices` as a digest. Kiosk/admin list may expose `has_tag: bool`.

Cross-table uniqueness is application-enforced:

- Bind tag: reject if HMAC is already a work card, or already another device’s tag.
- Enroll user (script, self-register, admin register): reject if HMAC is already a device tag.

---

## 6. State machine (one reader, one UID at a time)

Work card is tap-and-go (session survives removal). The device sticker must not be presented while the work card is still on the reader.

### Classify

```text
uid → hmac = compute_uid_hmac(uid, hmac_key)
    → users.uid_hmac     → work card
    → devices.tag_hmac   → device tag
    → else               → unknown
```

Pending intercepts run **before** classify (same pattern as `pending_registration`):

1. `pending_registration` → enroll user (fail if UID is a device tag).
2. `pending_tag_bind` → bind to the chosen device (fail if UID is a work card or another device’s tag).
3. Else classify.

### Idle (no session)

| Tap | Result |
|---|---|
| Active work card | `auth_success` → scan-first main menu |
| Unknown / inactive card | `auth_failed` → “Card Not Recognized” (unchanged) |
| Bound device tag | **Not** auth-failed. Short idle message: tap your work card first. No session. |

### Logged in (main menu, borrow, return, detail)

| Tap | Result |
|---|---|
| Any enrolled work card | Logout (`session_ended` / `card_tap`). Do **not** start the new user on the same tap (same as today). |
| Bound tag, device `available` | Borrow if under `MAX_BORROWS`; session stays. |
| Bound tag, borrowed by self | Return; session stays. |
| Bound tag, borrowed by someone else, normal user | Fail; session stays. |
| Bound tag, borrowed by someone else, admin | Return on behalf (existing rule); session stays. |
| Bound tag, `maintenance` | Fail; session stays. |
| Unknown UID | Toast; session stays. **Change:** today this tap would log out. |

Successful **and** failed tag taps `touch()` the session so fumbling does not burn the 120 s idle timer.

Device-tag taps are accepted for the **whole remaining session**, same window as UI pick. No extra shorter window.

### Admin overlay

The admin panel starts a session as the first enrolled admin **without a card**. Without `pending_tag_bind`, a sticker tap would check the tool out as that admin.

- While “tap the sticker” is waiting: next insert **binds**, does not borrow.
- While the admin panel is open but **not** in bind mode: do **not** auto-borrow/return on a device tag.

---

## 7. Post-login UI (scan-first)

Keep `#screen-main-menu`. Change the message so the reader is the primary path.

- Welcome + name + role stay.
- Smaller NFC ring / copy: **Tap the device** — to borrow or return.
- **Borrow** and **Return** stay, demoted: *or pick on screen* (tag missing, metal chassis, browsing).
- **End Session** stays.
- Optional one-liner: `N / 5 borrowed`.

Happy path: tap work card → tap sticker → toast (“Borrowed Fluke 87V”) → stay on this screen for the next device.

If the user opened Borrow or Return, a sticker tap still uses auto-intent. On `device_action`, refresh the open grid (or detail) and show the same toast.

Idle copy can mention that after login they can tap a device. Keep it one line. Self-service **Register your card** stays for **people**, not devices.

---

## 8. Admin: Register Device (bind, not create)

Excel is the master list. Import already inserted schrank devices into SQLite. Operators:

1. **Sync Source** if the workbook changed.
2. **Register Device** (new admin button, next to Register User).
3. See locker devices as **distinct rows**: display **name**, **PM**, slot if any, and whether a tag is already bound.
4. Search/filter by name or PM (same names must still all be visible).
5. Pick **one row** (the PM identifies the unit).
6. 60 s **Tap the sticker** step (reuse the user-registration waiting UI).
7. Success / fail, then back to the list so the next unit can be bound.

Unbound rows first; already-tagged rows remain so an operator can **re-bind** (lost sticker). Unbind is allowed (clear `tag_hmac`).

Do **not** add a “new device” form. If it is not a schrank row in the DB, it is not a locker device.

---

## 9. API and SSE

No public “borrow by tag” URL. The NFC bridge calls `LockerService` and pushes SSE.

Admin (require admin session, same as Register User):

- `POST /api/admin/devices/{id}/bind-tag` — set `pending_tag_bind` (60 s).
- `POST /api/admin/devices/{id}/unbind-tag` — clear `tag_hmac`.
- `GET /api/devices` — add `has_tag: bool` (no digest). Admin list uses name, `pm_number`, `locker_slot`, `has_tag`.

SSE (extend `connectSSE()` in `app.js`):

| Event | When |
|---|---|
| `device_action` | `{success, action: "borrow"\|"return", message, device_id, device_name}` |
| `device_tag_idle` | Bound tag at idle |
| `unknown_tag` | Unknown UID while logged in |
| `tag_bind_success` / `tag_bind_failed` | Admin bind window |

Logs: `"Card inserted on <reader>"` / `"Device tag on <reader>"` — **never** the raw UID.

---

## 10. Shared tap router

New `smart_locker/auth/tap_router.py`, used by the web NFC bridge **and** CLI `SmartLockerApp._on_card_inserted` (today CLI also logs out on any second tap).

- `classify_uid(...)` → work card / device tag / unknown
- `handle_insert(...)` → result object with SSE payload + CLI message, never the raw UID

Registration and tag-bind stay as pending intercepts **above** the router.

---

## 11. Script (tests + first Pi)

`scripts/enroll_device_tag.py`, same shape as `scripts/enroll_card.py`:

- `--pm PM-001` then tap, or `--uid HEX` with no hardware
- Mask UID in stdout
- `--force` to replace an existing bind on that device

Admin UI is required in this slice (binding tens of stickers over SSH is not acceptable).

---

## 12. Excel import

`source_import.py` must not pass `tag_hmac` into `update_metadata`. Add an explicit test: bind a tag, re-import a row that changes barcode, assert `tag_hmac` unchanged.

Re-import never overwrites `status` or `current_borrower_id`. New PMs still take Aktueller Einsatzort on first insert.

---

## 13. Hardware (no code change)

- ACR1252U + pyscard + `pcscd` already read NTAG213/215 UIDs. `_read_uid` accepts ≥ 4 bytes (7-byte NTAG UIDs are fine).
- No NDEF, no writing `pm_number` onto the sticker, no MIFARE sector, no second reader, no pcscd/libccid/pyscard change.
- Metal bodies need **on-metal / ferrite** tags. Software does not care; a plain sticker on a scope chassis often will not read.
- Do **not** enable `SMART_LOCKER_FAKE_READER` on the Pi.
- Pi checklist: one line — work card login, then NTAG on a device → borrow.

---

## 14. Implementation order

Tests from the acceptance in this file only. No drive-by refactors. No new dependencies.

1. **Schema** — `Device.tag_hmac`; `find_by_tag_hmac` / `bind_tag` / `unbind_tag`; migrate + unique index. Do not add `tag_hmac` to `update_metadata` ALLOWED.
2. **Collision on user enroll** — `UserService.enroll_user` and registration tap reject a UID that is already a device tag.
3. **Tap router** — classify + borrow/return/logout; pytest, in-memory DB, no hardware.
4. **Wire bridge + CLI** — replace “second tap → logout”; add `pending_tag_bind`; ignore device-tag actions while admin overlay is open and not binding.
5. **Admin bind API + script** — bind/unbind, `has_tag`, `enroll_device_tag.py`.
6. **Excel test** — re-import leaves `tag_hmac` alone.
7. **Kiosk UI** — scan-first main menu; SSE handlers; admin **Register Device** list with **name + PM** (duplicates stay distinct).
8. **Docs** — GUIDE §15 and README barcode paragraphs become this flow; session text: second tap means **work card**.

### Tests that must exist

- Bind, lookup, NULL uniqueness, re-bind (`tests/test_database.py`).
- Idle work card / idle device tag / idle unknown.
- Session + available tag → borrow + transaction log.
- Session + own borrowed tag → return.
- Session + someone else’s tag as user → fail, session remains.
- Session + someone else’s tag as admin → return-on-behalf.
- Session + work card → logout, no new session.
- Session + unknown → stay logged in.
- Maintenance / borrow limit.
- HMAC collision enroll/bind.
- Registration + bind API: 401 without session, 403 non-admin.
- Source re-import does not wipe `tag_hmac`.
- Two devices with the same `name` and different `pm_number` both appear in the admin bind list payload (name and PM present on each row).

---

## 15. Files

| Path | Change |
|---|---|
| `smart_locker/database/models.py` | `tag_hmac` |
| `smart_locker/database/repositories.py` | lookup / bind / unbind |
| `scripts/migrate_db.py` | column + unique index |
| `smart_locker/services/user_service.py` | reject device-tag UID on enroll |
| `smart_locker/auth/tap_router.py` | **new** |
| `smart_locker/api/app_context.py` | classify before logout; `pending_tag_bind` |
| `smart_locker/app.py` | CLI uses router |
| `smart_locker/api/routes.py` | bind/unbind; `has_tag` |
| `scripts/enroll_device_tag.py` | **new** |
| `smart_locker/sync/source_import.py` | do not touch `tag_hmac` (verify + test) |
| `smart_locker/frontend/index.html` | main-menu copy; admin Register Device |
| `smart_locker/frontend/app.js` | SSE; admin list (name + PM); scan-first menu |
| `smart_locker/frontend/style.css` | scan-first + bind list |
| `GUIDE.md` / `README.md` | replace barcode plan |
| `deploy/PI-VALIDATION-CHECKLIST.md` | one NFC device-tag line |
| `tests/test_tap_router.py` | **new** |
| `tests/test_database.py`, `test_api.py`, `test_services.py`, `test_source_import.py` | extend |

---

## 16. Out of scope

- USB barcode scanner, HID listener in `app.js`, `GET /api/devices/barcode/{barcode}`
- Writing NDEF / device id onto tags
- Encrypting device-tag UIDs
- Exporting `tag_hmac` to Excel or the public dashboard
- Multi-tag per device, second physical reader, multi-reader
- Creating devices from the admin panel
- Changing `MAX_BORROWS`, session timeout, or the existing grid/confirm dialogs
- Fake-reader production flag
- Calibration alerts, condition reporting, full admin web panel
- Plugin split of `smart_locker/`
