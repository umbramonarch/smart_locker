# ADR 0004: Excel catalog refresh vs locker rows

**Architecture Decision Record** — a short note for a choice that is expensive to undo (protocol, pinout, crate vs vendor HAL, flash layout). **Not** a Rust-only file. Most work never needs one; put tiny decisions in the git Issue.

Copy to `docs/adr/NNNN-title.md` in the **project**. Number from `0001`.

Date: 2026-08-28
Status: accepted

## Context

`device-list.xlsx` on the locker share is the site catalog (PM, name, type, serial, manufacturer, model, calibration, Location as people names). Locker occupancy lives in SQLite: slot, NFC tag, status, current borrower. A naive import that upserts every Excel row would create locker devices the kiosk never registered and could overwrite occupancy.

## Options

1. Treat Excel as source of truth: insert missing PMs into SQLite and overwrite locker fields on each sync.
2. Catalog-only refresh: update name/type/serial/manufacturer/model/calibration for PMs **already** in SQLite; never insert locker rows; never overwrite `locker_slot` / `image_path` / `description` / `tag_hmac` / `status` / `current_borrower_id`.
3. Bidirectional merge of every column.

## Decision

We pick **option 2** because a locker row exists only after admin Register Device (PM + free slot + NFC). Sync (`source_import.py`) is a catalog refresh. Person names in Location still replace the self-register list (names that leave Excel are removed). Scheduler: startup + every 5 minutes + admin Sync.

## Consequences

- Good: Excel cannot invent occupancy or wipe a tag/slot by omitting a column.
- Cost / follow-up: PMs that exist only in Excel stay off the kiosk until Register Device. Extra header aliases: `SMART_LOCKER_ID_HEADERS` / `SMART_LOCKER_LOCATION_HEADERS`.
- Hardware / recovery if this is firmware: N/A.
