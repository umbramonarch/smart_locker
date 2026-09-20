# ADR 0002: SQLite on the Pi, not on the CIFS share

**Architecture Decision Record** — a short note for a choice that is expensive to undo (protocol, pinout, crate vs vendor HAL, flash layout). **Not** a Rust-only file. Most work never needs one; put tiny decisions in the issue tracker.

Copy to `docs/adr/NNNN-title.md` in the **project**. Number from `0001`.

Date: 2026-08-28
Status: accepted

## Context

The locker file share (`/mnt/locker` on the Pi) already holds Excel, photos, and signed updates. SQLite with WAL needs reliable POSIX locking. CIFS locking over that share is not.

## Options

1. Put `smart_locker.db` on the CIFS mount so a PC can open it directly.
2. Keep SQLite on the Pi SD card; use Excel on the share as the catalog/export surface.
3. Run PostgreSQL (or similar) on the Pi or LAN.

## Decision

We pick **option 2** because WAL on CIFS is unreliable and a share outage must not take the kiosk down. The database stays on the appliance. Excel import/export and Location write-back are the share contract.

## Consequences

- Good: kiosk borrow/return keeps working when the share is down; no remote SQLite corruption from CIFS locks.
- Cost / follow-up: do not “fix” share watchers with watchdog `PollingObserver`. Do not put the DB path on `/mnt/locker`.
- Hardware / recovery if this is firmware: restore from `backups/` on the Pi, not from a copy of the DB on the share.
