# ADR 0003: NFC stickers on devices, not a barcode scanner

**Architecture Decision Record** — a short note for a choice that is expensive to undo (protocol, pinout, crate vs vendor HAL, flash layout). **Not** a Rust-only file. Most work never needs one; put tiny decisions in the issue tracker.

Copy to `docs/adr/NNNN-title.md` in the **project**. Number from `0001`.

Date: 2026-08-28
Status: accepted

## Context

Colleagues need to identify the exact locker unit after a work-card login. An earlier GUIDE sketch used a USB HID barcode scanner (`GET /api/devices/barcode/{barcode}`). That extra device fights Chromium kiosk focus and occupies a USB port. The ACR1252U already returns a UID.

Shipped plan: `docs/planning/nfc-device-tags.md` (merged in MR #3).

## Options

1. USB barcode scanner + `devices.barcode` from Excel.
2. Cheap NFC stickers on the same ACR1252U; store `devices.tag_hmac` (same HMAC key as work cards).
3. On-screen pick only (no per-unit tag).

## Decision

We pick **option 2** because the reader is already in the path, stickers share the HMAC lookup, and Excel barcode is unused leftover (not imported). Auto-intent: after login, a sticker tap borrows if available and returns if the session user holds it. A borrowed sticker can be returned from idle without a work card. Available tags do not borrow from idle. Public JSON may expose `has_tag: bool`, never the digest.

## Consequences

- Good: one reader, no HID focus fights, same security model as work cards.
- Cost / follow-up: one sticker per device; re-bind replaces the HMAC. Do not reuse `devices.barcode`. Keep `docs/planning/nfc-device-tags.md` as history, not open work.
- Hardware / recovery if this is firmware: N/A — USB NFC, not a HAT.
