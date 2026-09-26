"""
File: uid_helpers.py
Description: Shared UID parsing, display masking, and NFC reader enrollment.
Project: smart_locker/scripts
Notes: Reader dependencies remain lazy so --uid works without PC/SC.
"""

import time


def normalize_uid(raw: str) -> str:
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


def mask_uid(uid: str) -> str:
    if len(uid) <= 4:
        return "*" * len(uid)
    return uid[:2] + "*" * (len(uid) - 4) + uid[-2:]


def read_uid_from_reader(prompt: str, unreadable: str, timeout: str) -> str | None:
    """Wait up to 30 seconds for an inserted card, without eager PC/SC imports."""
    from smart_locker.nfc.reader import NFCReader
    from smart_locker.nfc.card_observer import CardEvent, CardEventType
    from smart_locker.nfc.reader_observer import ReaderEvent

    reader = NFCReader()
    try:
        print(f"Reader: {reader.start()}")
        print(prompt)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            evt = reader.wait_for_event(timeout=max(deadline - time.monotonic(), 0.1))
            if evt is None:
                break
            if isinstance(evt, ReaderEvent):
                continue
            if isinstance(evt, CardEvent) and evt.event_type == CardEventType.INSERTED:
                if evt.uid is None:
                    print(unreadable)
                    return None
                return evt.uid
        print(timeout)
        return None
    finally:
        reader.stop()
