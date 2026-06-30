"""
File: enroll_card.py
Description: Card enrollment utility. Enrolls a new user with encrypted UID
             storage and an HMAC fingerprint for authentication lookups. The
             UID can be read live from the NFC reader, or supplied directly
             with --uid HEX for no-hardware setups (the simulation harness or
             remote provisioning, where no reader/PC/SC service is present).
Project: smart_locker/scripts
Notes: Usage:
         python -m scripts.enroll_card --name "John Doe" [--role admin]
         python -m scripts.enroll_card --name "Sim Admin" --role admin --uid AABBCCDD
       The reader path requires a physical ACR1252U + PC/SC service; the --uid
       path requires neither (pyscard is imported lazily, only when reading a
       card). The raw UID is masked in console output and never logged.
"""

import argparse
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.logging_config import setup_logging
from smart_locker.database.engine import get_session, init_db
from smart_locker.security.key_manager import key_manager
from smart_locker.services.user_service import UserService


def _mask_uid(uid: str) -> str:
    """Return a display-safe masked form of a card UID hex string.

    Shows only the first two and last two hex characters; the middle is
    replaced with asterisks so the raw UID never appears in console output
    (the same masking the reader path uses).

    Args:
        uid: Raw card UID hex string.

    Returns:
        Masked UID (e.g. "AA****DD"). UIDs of 4 chars or fewer are fully masked.
    """
    if len(uid) <= 4:
        return "*" * len(uid)
    return uid[:2] + "*" * (len(uid) - 4) + uid[-2:]


def _normalize_uid(raw: str) -> str:
    """Validate and normalise a UID supplied on the command line.

    Removes all whitespace and upper-cases the value so an enrolled card and a
    later tap produce the same HMAC digest — the NFC reader reports a contiguous
    uppercase-hex UID (``APDUResponse.uid_hex``), and the sample
    ``SMART_LOCKER_FAKE_DEFAULT_UID`` is uppercase, so enrollment must match
    exactly. (``bytes.fromhex`` tolerates spaces between byte pairs, so stripping
    only the ends would let "AA BB CC DD" through and silently mismatch a tap.)
    The result is verified to be valid hexadecimal.

    Args:
        raw: UID string as typed by the operator (e.g. "AABBCCDD").

    Returns:
        Normalised, contiguous uppercase-hex UID.

    Raises:
        SystemExit: If the value is empty or not valid hexadecimal (this includes
            an odd number of hex digits, which ``bytes.fromhex`` rejects).
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


def _enroll(uid: str, name: str, role: str) -> None:
    """Encrypt the UID, store the user, and print a masked confirmation.

    Args:
        uid: Normalised card UID hex string.
        name: User display name.
        role: "user" or "admin".

    Returns:
        None.
    """
    user_svc = UserService(
        enc_key=key_manager.enc_key,
        hmac_key=key_manager.hmac_key,
    )
    with get_session() as session:
        user = user_svc.enroll_user(
            session,
            display_name=name,
            card_uid_hex=uid,
            role=role,
        )
        print(
            f"Enrolled: {user.display_name} "
            f"(id={user.id}, role={role}, UID={_mask_uid(uid)})"
        )


def _read_uid_from_reader() -> str | None:
    """Wait for a card tap on the NFC reader and return its UID hex.

    pyscard and the reader modules are imported here (not at module load) so
    the no-hardware ``--uid`` path never requires a PC/SC stack.

    Returns:
        The card UID hex string, or None on timeout or an unreadable card.
    """
    import time

    from smart_locker.nfc.reader import NFCReader
    from smart_locker.nfc.card_observer import CardEvent, CardEventType
    from smart_locker.nfc.reader_observer import ReaderEvent

    reader = NFCReader()
    try:
        reader_name = reader.start()
        print(f"Reader: {reader_name}")
        print("Place card on reader...")

        # Wait for a CardEvent INSERTED, skipping ReaderEvents
        # (ReaderMonitor fires CONNECTED immediately on start).
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            evt = reader.wait_for_event(timeout=max(remaining, 0.1))
            if evt is None:
                break
            if isinstance(evt, ReaderEvent):
                continue  # Skip reader connect/disconnect events
            if isinstance(evt, CardEvent) and evt.event_type == CardEventType.INSERTED:
                if evt.uid is None:
                    print("Could not read card UID. Hold card steady and try again.")
                    return None
                return evt.uid

        print("Timeout — no card detected. Try again.")
        return None
    finally:
        reader.stop()


def main() -> None:
    """Enroll a new NFC card user, by reader tap or an explicit ``--uid``.

    Parses CLI arguments for name, role, and an optional UID. With ``--uid`` the
    card UID is supplied directly and no reader / PC/SC service is needed (the
    no-hardware path used by the simulation harness and remote provisioning).
    Without ``--uid``, the utility waits up to 30 seconds for a card tap. Either
    way the UID is encrypted (AES-256-GCM) and stored alongside its HMAC
    fingerprint, and is masked in console output.

    Returns:
        None. Enrollment result is printed to stdout.
    """
    parser = argparse.ArgumentParser(description="Enroll a new NFC card user.")
    parser.add_argument("--name", required=True, help="User display name")
    parser.add_argument(
        "--role", choices=["user", "admin"], default="user", help="User role"
    )
    parser.add_argument(
        "--uid",
        default=None,
        help="Card UID hex (e.g. AABBCCDD). When supplied, the user is enrolled "
             "directly with NO NFC reader (the no-hardware path). Omit to read "
             "the UID from a card tap on the reader.",
    )
    args = parser.parse_args()

    setup_logging()
    init_db()

    # --- No-hardware path: UID supplied on the command line ---------------
    if args.uid is not None:
        uid = _normalize_uid(args.uid)
        print(f"Enrolling without reader (UID supplied: {_mask_uid(uid)})")
        _enroll(uid, args.name, args.role)
        return

    # --- Reader path: read the UID from a physical card tap ---------------
    uid = _read_uid_from_reader()
    if uid is None:
        return
    print(f"Card detected (UID: {_mask_uid(uid)})")
    _enroll(uid, args.name, args.role)


if __name__ == "__main__":
    main()
