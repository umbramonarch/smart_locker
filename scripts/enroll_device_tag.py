"""
File: enroll_device_tag.py
Description: Bind an NFC sticker UID to an existing locker device (HMAC only,
             never stores the raw UID). The UID can be read live from the NFC
             reader, or supplied directly with --uid HEX for no-hardware setups.
Project: smart_locker/scripts
Notes: Usage:
         python -m scripts.enroll_device_tag --pm PM-001
         python -m scripts.enroll_device_tag --pm PM-001 --uid AABBCCDD
         python -m scripts.enroll_device_tag --pm PM-001 --force
       Does not create a device row — Register Device (PM + slot + NFC) does.
       The raw UID is masked in console output and never logged.
"""

import argparse
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.logging_config import setup_logging
from smart_locker.auth.tap_router import bind_uid_to_device
from smart_locker.database.engine import get_session, init_db
from smart_locker.database.repositories import DeviceRepository
from smart_locker.security.key_manager import key_manager
from scripts.uid_helpers import (
    mask_uid, normalize_uid as _shared_normalize_uid, read_uid_from_reader,
)


def _mask_uid(uid: str) -> str:
    """Return a display-safe masked form of a UID hex string.

    Args:
        uid: Raw UID hex string.

    Returns:
        Masked UID (e.g. "AA****DD"). UIDs of 4 chars or fewer are fully masked.
    """
    return mask_uid(uid)


def _normalize_uid(raw: str) -> str:
    """Validate and normalise a UID supplied on the command line.

    Args:
        raw: UID string as typed by the operator (e.g. "AABBCCDD").

    Returns:
        Normalised, contiguous uppercase-hex UID.

    Raises:
        SystemExit: If the value is empty or not valid hexadecimal.
    """
    return _shared_normalize_uid(raw)


def _bind(uid: str, pm_number: str, force: bool) -> None:
    """Look up the device by PM and bind the sticker HMAC.

    Args:
        uid: Normalised sticker UID hex string.
        pm_number: Device PM number (business key).
        force: Replace an existing bind on this device.

    Returns:
        None.
    """
    with get_session() as session:
        device = DeviceRepository.find_by_pm(session, pm_number)
        if device is None:
            print(f"ERROR: No device with PM '{pm_number}'.")
            raise SystemExit(1)
        if device.tag_hmac is not None and not force:
            print(
                f"ERROR: {device.name} ({device.pm_number}) already has a tag. "
                "Pass --force to replace it."
            )
            raise SystemExit(1)
        try:
            bind_uid_to_device(session, device, uid, key_manager.hmac_key)
        except ValueError as e:
            print(f"ERROR: {e}")
            raise SystemExit(1)
        print(
            f"Bound tag to {device.name} "
            f"(pm={device.pm_number}, UID={_mask_uid(uid)})"
        )


def _read_uid_from_reader() -> str | None:
    """Wait for a sticker tap using the shared bench reader loop."""
    return read_uid_from_reader(
        "Place the device sticker on the reader...",
        "Could not read tag UID. Hold the sticker steady and try again.",
        "Timeout — no tag detected. Try again.",
    )


def main() -> None:
    """Bind an NFC sticker to an existing locker device.

    Parses ``--pm`` (required), optional ``--uid``, and ``--force``. With
    ``--uid`` no reader is needed. The UID is stored only as HMAC-SHA256.

    Returns:
        None. Result is printed to stdout.
    """
    parser = argparse.ArgumentParser(
        description="Bind an NFC sticker to an existing locker device."
    )
    parser.add_argument("--pm", required=True, help="Device PM number (e.g. PM-001)")
    parser.add_argument(
        "--uid",
        default=None,
        help="Sticker UID hex (e.g. AABBCCDD). When supplied, bind with NO "
             "NFC reader. Omit to read the UID from a tap on the reader.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing tag bind on this device.",
    )
    args = parser.parse_args()

    setup_logging()
    init_db()

    if args.uid is not None:
        uid = _normalize_uid(args.uid)
        print(f"Binding without reader (UID supplied: {_mask_uid(uid)})")
        _bind(uid, args.pm, args.force)
        return

    uid = _read_uid_from_reader()
    if uid is None:
        raise SystemExit(1)
    print(f"Tag detected (UID: {_mask_uid(uid)})")
    _bind(uid, args.pm, args.force)


if __name__ == "__main__":
    main()
