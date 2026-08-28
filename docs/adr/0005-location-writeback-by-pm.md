# ADR 0005: Pi owns the Excel Location cell by PM

**Architecture Decision Record** — a short note for a choice that is expensive to undo (protocol, pinout, crate vs vendor HAL, flash layout). **Not** a Rust-only file. Most work never needs one; put tiny decisions in the git Issue.

Copy to `docs/adr/NNNN-title.md` in the **project**. Number from `0001`.

Date: 2026-08-28
Status: accepted

## Context

Site colleagues already look at `device-list.xlsx` for who has a unit. After kiosk borrow/return, that Location cell must match SQLite or the spreadsheet lies. Editing Location by hand on locker PMs fights the kiosk.

## Options

1. Humans edit Location in Excel (or on the dashboard) for locker devices; the Pi only reads.
2. The Pi writes **only** the Location column, keyed by PM: available → `SMART_LOCKER_IN_LOCKER_TOKEN` (default `Locker`); borrowed → borrower name. Other columns and sheets stay.
3. Full workbook rewrite on every transaction.

## Decision

We pick **option 2** because the kiosk is the occupancy source. Write-back runs after borrow/return, Register Device, and each source import (`location_writeback.py`). A locked or missing workbook is logged and retried — never a kiosk crash. Dashboard Inventory owner edit is for **non-locker** PMs only. Do not edit Location in Excel for locker devices; the Pi overwrites that cell.

## Consequences

- Good: one owner for locker Location; catalog columns survive; share-down does not crash the kiosk.
- Cost / follow-up: operators who type into Location on a locker PM will lose that edit on the next write-back.
- Hardware / recovery if this is firmware: N/A.
