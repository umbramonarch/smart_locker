# ADR 0009: Single uvicorn worker

**Architecture Decision Record** — a short note for a choice that is expensive to undo (protocol, pinout, crate vs vendor HAL, flash layout). **Not** a Rust-only file. Most work never needs one; put tiny decisions in the git Issue.

Copy to `docs/adr/NNNN-title.md` in the **project**. Number from `0001`.

Date: 2026-08-28
Status: accepted

## Context

The kiosk process holds an in-memory session, an SSE subscriber queue, and the NFC listener (pyscard threads bridged into asyncio). Uvicorn `--workers N` (or multiple systemd processes) would fork that state.

## Options

1. Multiple uvicorn workers for throughput.
2. One worker / one process: in-memory session + SSE + NFC stay coherent.
3. External session store (Redis, SQLite session table) so workers can share.

## Decision

We pick **option 2** because there is one Riverdi kiosk and one ACR1252U. Extra workers would split sessions and duplicate NFC ownership. Do not run uvicorn with multiple workers. Throughput is not the constraint.

## Consequences

- Good: one session, one SSE stream, one reader; systemd unit stays simple.
- Cost / follow-up: do not add gunicorn worker counts or a second `smart-locker` process without a session/NFC redesign.
- Hardware / recovery if this is firmware: N/A.
