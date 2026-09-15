# ADR 0008: Dashboard auth model (public GET, fail-closed admin)

**Architecture Decision Record** — a short note for a choice that is expensive to undo (protocol, pinout, crate vs vendor HAL, flash layout). **Not** a Rust-only file. Most work never needs one; put tiny decisions in the git Issue.

Copy to `docs/adr/NNNN-title.md` in the **project**. Number from `0001`.

Date: 2026-08-28
Status: accepted (amended 2026-09-14)

## Context

`/dashboard` is a LAN page for colleagues: live Excel Inventory and SQLite Locker. Mutations (owner POST, bind/unbind) and gated users/transactions/owners GETs must not be open. The kiosk already uses a 5-tap clock gesture to reveal admin UI. That gesture is visible in the client; it is not a secret.

## Options

1. Public dashboard for all GETs and POSTs (LAN trust).
2. Public GET Inventory and Locker catalog; admin header `X-Smart-Locker-Admin` matching `SMART_LOCKER_DASHBOARD_ADMIN_SECRET`; **401 if the secret is unset** (fail closed).
3. Full login (sessions/passwords) for the dashboard.

## Decision

We pick **option 2** because catalog visibility is the LAN job, and fail-closed avoids a missing-env open admin API. The same 5-tap on the dashboard clock reveals users, last 500 transactions, and unbind/arm-bind **in the client** — it is not authorization (`overlay=true` is not auth). Kiosk loopback keeps Riverdi NFC/session off LAN browsers.

## Consequences

- Good: unset secret cannot accidentally expose bind/unbind; public catalog still works; 5-tap stays a convenience overlay.
- Cost / follow-up: operators must set `SMART_LOCKER_DASHBOARD_ADMIN_SECRET` on the Pi. Share launcher (`SMART_LOCKER_PUBLIC_URL` + `SMART_LOCKER_DASHBOARD_SHARE_PATH`) only writes shortcuts to the live page.
- Hardware / recovery if this is firmware: N/A.

## Amendment 2026-09-14

Owner change on Inventory (`POST /api/dashboard/owner`, `GET /api/dashboard/owners`) is public — anyone who can open the dashboard can change Location for non-locker PMs. Locker PMs are still refused. Bind/unbind and the users/transactions GETs remain behind `SMART_LOCKER_DASHBOARD_ADMIN_SECRET`. Reason: changing the holder of a non-locker device is an everyday task, and the Location column is already public on the same page. The public owners list returns only the in-locker token and Excel-derived registrant names (already public via Inventory and `/api/registrants`), never the kiosk user table.
