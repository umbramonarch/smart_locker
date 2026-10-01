# ADR 0008: Dashboard auth model (all endpoints public)

**Architecture Decision Record** — a short note for a choice that is expensive to undo (protocol, pinout, crate vs vendor HAL, flash layout). **Not** a Rust-only file. Most work never needs one; put tiny decisions in the git Issue.

Copy to `docs/adr/NNNN-title.md` in the **project**. Number from `0001`.

Date: 2026-09-30 (supersedes the 2026-08-28 fail-closed-secret decision)
Status: accepted

## Context

`/dashboard` is the LAN page for colleagues: live Excel Inventory and SQLite Locker, owner edit, NFC bind/unbind, user rename, and the users/transactions lists. The earlier revision gated mutations and the gated GETs behind `SMART_LOCKER_DASHBOARD_ADMIN_SECRET` (header `X-Smart-Locker-Admin`, 401 if unset). The product decision changed: the dashboard is trusted on the locker LAN — the shared secret should be removed entirely rather than kept as ceremony.

## Options

1. Public dashboard for all GETs and POSTs (LAN trust).
2. Public GET Inventory and Locker catalog; admin header `X-Smart-Locker-Admin` matching `SMART_LOCKER_DASHBOARD_ADMIN_SECRET`; **401 if the secret is unset** (fail closed).
3. Full login (sessions/passwords) for the dashboard.

## Decision

We pick **option 1** per the owner's request: every `/api/dashboard/*` endpoint is public — catalog GETs, owner POST, bind/unbind, user rename, and the users/transactions/owners reads. The 5-tap clock gesture (8 s window) opens the users/logs/NFC overlay — it is a reveal, not authorization. Role changes are public too: `POST /api/dashboard/users/{id}/role` accepts `admin`/`user` only and refuses to demote the last active admin (409).

This does not change the kiosk boundary. Two different things stay protected:

- **Kiosk session / appliance surface.** Session mutations, registration, SSE (`/api/events`), and power/update still require loopback (`require_session` / `require_loopback`) plus the admin role. A LAN browser cannot ride or forge those.
- **Physical tag operations.** A dashboard bind/unbind *request* is public, but an arm-bind only completes when someone taps the sticker on the locker's own ACR1252U — the NFC window is armed remotely, completed physically. Reader-conflict checks (no arming while a kiosk session or registration is live) still refuse 409.

## Consequences

- Good: no shared secret to distribute or rotate; the overlay opens directly; a missing env var can no longer fail-closed a legitimate dashboard action; LAN staff can fix names/owners without kiosk access.
- Cost / follow-up: any LAN browser can rename users, edit non-locker owners, and arm tag binds — accepted for the appliance's trusted-LAN deployment; containment is the locker LAN itself.
- Hardware / recovery if this is firmware: N/A.
