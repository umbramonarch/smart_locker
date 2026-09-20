# ADR 0001: UID HMAC lookup and AES-GCM storage

**Architecture Decision Record** — a short note for a choice that is expensive to undo (protocol, pinout, crate vs vendor HAL, flash layout). **Not** a Rust-only file. Most work never needs one; put tiny decisions in the issue tracker.

Copy to `docs/adr/NNNN-title.md` in the **project**. Number from `0001`.

Date: 2026-08-28
Status: accepted

## Context

Work cards (and later device stickers) present a UID. We need indexed lookup in SQLite and at-rest protection, without writing the raw UID to logs or to public APIs.

## Options

1. Store the raw UID (or a reversible encoding) and look up by equality.
2. One key for both encryption and hashing.
3. Two keys: HMAC-SHA256 digest for lookup (`uid_hmac` / `tag_hmac`), AES-256-GCM ciphertext for admin-only recovery of work-card UIDs.

## Decision

We pick **option 3** because HMAC is deterministic and indexable (O(1) lookup) while AES-GCM uses a random nonce so the same UID is never stored as the same ciphertext twice. `SMART_LOCKER_HMAC_KEY` and `SMART_LOCKER_ENC_KEY` are separate 32-byte keys. Device stickers store HMAC only (no encrypted UID). Card events log "Card inserted on \<reader\>" / "Device tag on \<reader\>" — never a raw UID.

## Consequences

- Good: stolen DB rows do not yield UIDs without the AES key; logs stay clean; lookups stay indexed.
- Cost / follow-up: rotating the HMAC key invalidates every stored digest (re-enroll). Do not log UID even in enrollment (mask it).
- Hardware / recovery if this is firmware: N/A — host software on the Pi.
