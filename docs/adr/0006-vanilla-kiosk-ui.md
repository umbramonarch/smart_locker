# ADR 0006: Vanilla kiosk UI (HTML/CSS/JS, no Node)

**Architecture Decision Record** — a short note for a choice that is expensive to undo (protocol, pinout, crate vs vendor HAL, flash layout). **Not** a Rust-only file. Most work never needs one; put tiny decisions in the git Issue.

Copy to `docs/adr/NNNN-title.md` in the **project**. Number from `0001`.

Date: 2026-08-28
Status: accepted

## Context

The kiosk is Chromium fullscreen on a Raspberry Pi 4, served as static files by FastAPI. The dashboard is the same origin at `/dashboard`. House playbook has a TypeScript-React pack. Adding Node, Vite, or React would change the offline Pi install (wheelhouse, no-apt) and the kiosk runtime.

## Options

1. Vanilla HTML/CSS/JS as FastAPI static files. No Node runtime for the product.
2. React (or similar) SPA with a Node build in CI and on the Pi.
3. Separate dashboard app (second service/port).

## Decision

We pick **option 1** because the appliance is already Python + Chromium, tests do not need a JS toolchain, and offline install must stay USB/wheelhouse. Keep the current kiosk and dashboard. TypeScript-React is not used.

## Consequences

- Good: no Node on the Pi; one static tree under `smart_locker/frontend/`; CI stays `pip install -r requirements.txt` + pytest.
- Cost / follow-up: do not add a frontend framework or npm without an explicit session that accepts the install/runtime cost.
- Hardware / recovery if this is firmware: N/A.
