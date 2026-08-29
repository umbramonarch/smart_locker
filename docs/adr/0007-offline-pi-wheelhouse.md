# ADR 0007: Offline Pi wheelhouse and signed pack_release

**Architecture Decision Record** — a short note for a choice that is expensive to undo (protocol, pinout, crate vs vendor HAL, flash layout). **Not** a Rust-only file. Most work never needs one; put tiny decisions in the git Issue.

Copy to `docs/adr/NNNN-title.md` in the **project**. Number from `0001`.

Date: 2026-08-28
Status: accepted

## Context

Production is Raspberry Pi OS trixie: **Python 3.13 / cp313**. The appliance may have no internet except the locker share. `pyscard` has no aarch64 PyPI wheel; it comes from `.deb`s. Field updates must not be `git pull` of an unsigned tree.

Dev/Windows is Python **3.11+**. CI may run 3.11 and 3.13. Do not treat 3.12 as the product pin.

## Options

1. `apt` / PyPI at install time on the Pi.
2. USB offline kit: wheelhouse built for cp313 + system `.deb`s; `install.sh` / `update.sh` with no network assumption.
3. Docker image pulled at site.

## Decision

We pick **option 2** because the site can be air-gapped. In-field updates are an unpacked repo tree from a USB stick (Windows git checkout; `update.sh` copies it off the stick, then stop / backup / rsync-preserve / pip from the existing Pi wheelhouse / migrate / health / rollback). HMAC is not used on that path. A signed tarball from `python -m scripts.pack_release` (`.tar.gz` + `.hmac` on `/mnt/locker/locker-updates`) remains fallback only; `SMART_LOCKER_UPDATE_HMAC_KEY` is the env **string** for openssl, not decoded 32 bytes. Git reset is not a full update. Keep `requirements.txt` (no `pyproject.toml` for this product).

## Consequences

- Good: install and update work without PyPI; bad updates can roll back; Python pin matches the Pi.
- Cost / follow-up: rebuild the wheelhouse when dependencies or the Pi Python ABI change. Do not enable `SMART_LOCKER_FAKE_READER` in production.
- Hardware / recovery if this is firmware: re-run `install.sh` from USB + restore `backups/`; do not flash random OS images over the SD without a backup.
