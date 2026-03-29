"""
File: enroll_card.py
Description: Card enrollment utility. Reads a card UID from the NFC reader
             and enrolls a new user with encrypted UID storage and HMAC
             fingerprint for future authentication lookups.
Project: smart_locker/scripts
Notes: Usage: python -m scripts.enroll_card --name "John Doe" [--role admin]
       Requires physical NFC reader. Card UID is masked in output.
"""

import argparse
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.logging_config import setup_logging
from smart_locker.database.engine import get_session, init_db
from smart_locker.nfc.reader import NFCReader
from smart_locker.nfc.card_observer import CardEvent, CardEventType
from smart_locker.nfc.reader_observer import ReaderEvent
from smart_locker.security.key_manager import key_manager
from smart_locker.services.user_service import UserService


def main() -> None:
    """Enroll a new NFC card by reading its UID and creating a user record.

    Parses CLI arguments for user name and role, waits up to 30 seconds for
    a card tap on the NFC reader, then encrypts the card UID (AES-256-GCM)
    and stores its HMAC fingerprint for future authentication lookups.
    The raw UID is masked in console output for security.

    Returns:
        None. Enrollment result is printed to stdout.
    """
    parser = argparse.ArgumentParser(description="Enroll a new NFC card user.")
    parser.add_argument("--name", required=True, help="User display name")
    parser.add_argument(
        "--role", choices=["user", "admin"], default="user", help="User role"
    )
    args = parser.parse_args()

    setup_logging()
    init_db()

    user_svc = UserService(
        enc_key=key_manager.enc_key,
        hmac_key=key_manager.hmac_key,
    )

    reader = NFCReader()
    try:
        reader_name = reader.start()
        print(f"Reader: {reader_name}")
        print("Place card on reader...")

        # Wait for a CardEvent INSERTED, skipping ReaderEvents
        # (ReaderMonitor fires CONNECTED immediately on start)
        event = None
        import time
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            evt = reader.wait_for_event(timeout=max(remaining, 0.1))
            if evt is None:
                break
            if isinstance(evt, ReaderEvent):
                continue  # Skip reader connect/disconnect events
            if isinstance(evt, CardEvent) and evt.event_type == CardEventType.INSERTED:
                event = evt
                break

        if event is None:
            print("Timeout — no card detected. Try again.")
            return

        if event.uid is None:
            print("Could not read card UID. Hold card steady and try again.")
            return

        # Mask UID for console display — show only first 2 and last 2 hex chars
        masked = event.uid[:2] + "*" * (len(event.uid) - 4) + event.uid[-2:]
        print(f"Card detected (UID: {masked})")

        with get_session() as session:
            user = user_svc.enroll_user(
                session,
                display_name=args.name,
                card_uid_hex=event.uid,
                role=args.role,
            )
            print(f"Enrolled: {user.display_name} (id={user.id}, role={args.role})")

    finally:
        reader.stop()


if __name__ == "__main__":
    main()
