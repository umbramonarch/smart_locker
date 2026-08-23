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
       Does not create a device row — Excel/schrank import already did.
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


def _mask_uid(uid: str) -> str:
    """Return a display-safe masked form of a UID hex string.

    Args:
        uid: Raw UID hex string.

    Returns:
        Masked UID (e.g. "AA****DD"). UIDs of 4 chars or fewer are fully masked.
    """
    if len(uid) <= 4:
        return "*" * len(uid)
    return uid[:2] + "*" * (len(uid) - 4) + uid[-2:]


def _normalize_uid(raw: str) -> str:
    """Validate and normalise a UID supplied on the command line.

    Args:
        raw: UID string as typed by the operator (e.g. "AABBCCDD").

    Returns:
        Normalised, contiguous uppercase-hex UID.

    Raises:
        SystemExit: If the value is empty or not valid hexadecimal.
    """
    cleaned = "".join(raw.split()).upper()
    if not cleaned:
        print("ERROR: --uid is empty.")
        raise SystemExit(2)
    try:
        bytes.fromhex(cleaned)
    except ValueError:
        print(
            f"ERROR: --uid '{raw}' is not valid hex "
            "(expected an even number of hex digits, e.g. AABBCCDD)."
        )
        raise SystemExit(2)
    return cleaned


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
    """Wait for a sticker tap on the NFC reader and return its UID hex.

    pyscard and the reader modules are imported here (not at module load) so
    the no-hardware ``--uid`` path never requires a PC/SC stack.

    Returns:
        The UID hex string, or None on timeout or an unreadable tag.
    """
    import time

    from smart_locker.nfc.reader import NFCReader
    from smart_locker.nfc.card_observer import CardEvent, CardEventType
    from smart_locker.nfc.reader_observer import ReaderEvent

    reader = NFCReader()
    try:
        reader_name = reader.start()
        print(f"Reader: {reader_name}")
        print("Place the device sticker on the reader...")

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            evt = reader.wait_for_event(timeout=max(remaining, 0.1))
            if evt is None:
                break
            if isinstance(evt, ReaderEvent):
                continue
            if isinstance(evt, CardEvent) and evt.event_type == CardEventType.INSERTED:
                if evt.uid is None:
                    print("Could not read tag UID. Hold the sticker steady and try again.")
                    return None
                return evt.uid

        print("Timeout — no tag detected. Try again.")
        return None
    finally:
        reader.stop()


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
        return
    print(f"Tag detected (UID: {_mask_uid(uid)})")
    _bind(uid, args.pm, args.force)


if __name__ == "__main__":
    main()
