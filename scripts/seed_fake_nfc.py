"""
File: seed_fake_nfc.py
Description: Dev-only seed for the no-hardware fake NFC reader. Enrolls the 3
             fixed work-card UIDs behind the dev-tap preset panel (keys 1/2/3)
             as users, and binds the 3 fixed device-tag UIDs (keys A/S/D) to
             existing locker PMs. Refuses to run unless SMART_LOCKER_FAKE_READER
             is enabled; reruns are idempotent no-ops. Never run on the Pi.
Project: smart_locker/scripts
Notes: Usage:
         SMART_LOCKER_FAKE_READER=1 python -m scripts.seed_fake_nfc
         SMART_LOCKER_FAKE_READER=1 python -m scripts.seed_fake_nfc \\
             --pm-a PM-101 --pm-s PM-102 --pm-d PM-103
       The panel constants live in app.js (initDevTap) and are pinned by
       tests/test_dev_tap_panel.py — change all three together. Enrollment and
       binding reuse scripts/enroll_card and scripts/enroll_device_tag; like
       them, this script never creates a device row and never prints a raw UID.
"""

import argparse
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.logging_config import setup_logging
from scripts import enroll_card, enroll_device_tag
from smart_locker.database.engine import get_session, init_db
from smart_locker.database.repositories import DeviceRepository, UserRepository
from smart_locker.nfc.factory import fake_reader_enabled
from smart_locker.security.hashing import compute_uid_hmac
from smart_locker.security.key_manager import key_manager

# Fixed preset UIDs served by the dev-tap panel in app.js (initDevTap):
# keys 1/2/3 inject FAKE_CARD_UIDS, keys A/S/D inject FAKE_TAG_UIDS.
# Pinned by tests/test_dev_tap_panel.py — change all three together.
FAKE_CARD_UIDS = ["040000A1", "040000A2", "040000A3"]
FAKE_TAG_UIDS = ["050000B1", "050000B2", "050000B3"]

# Panel keycaps in array order, for console output only.
_CARD_KEYS = ["1", "2", "3"]
_TAG_KEYS = ["A", "S", "D"]

DEFAULT_NAMES = ["Fake Admin", "Fake User 2", "Fake User 3"]
DEFAULT_ROLES = ["admin", "user", "user"]
DEFAULT_PMS = ["PM-001", "PM-002", "PM-003"]


def _user_enrolled(uid: str) -> bool:
    """Return whether a user row already exists for a card UID.

    Args:
        uid: Normalised card UID hex string.

    Returns:
        True if the UID's HMAC already belongs to a user.
    """
    uid_hmac = compute_uid_hmac(uid, key_manager.hmac_key)
    with get_session() as session:
        return UserRepository.find_by_uid_hmac(session, uid_hmac) is not None


def _tag_already_bound(uid: str, pm_number: str) -> bool:
    """Return whether a device already carries exactly this sticker UID.

    A missing PM returns False here — the bind step below (reused from
    ``enroll_device_tag``) reports that with its own clear error.

    Args:
        uid: Normalised sticker UID hex string.
        pm_number: Device PM number (business key).

    Returns:
        True if the PM exists and its tag HMAC matches the UID.
    """
    tag_hmac = compute_uid_hmac(uid, key_manager.hmac_key)
    with get_session() as session:
        device = DeviceRepository.find_by_pm(session, pm_number)
        return device is not None and device.tag_hmac == tag_hmac


def seed(names: list[str], roles: list[str], pms: list[str]) -> None:
    """Enroll the 3 preset work cards and bind the 3 preset tags.

    Users are enrolled first (reusing ``enroll_card``), then tags are bound
    (reusing ``enroll_device_tag`` with ``force=False``), so a missing PM
    still leaves the users in place for a later rerun. Rerunning with the
    same values is a no-op success: already-enrolled cards are skipped and
    already-bound tags are left untouched — nothing is ever duplicated.

    Args:
        names: Display names for the 3 card UIDs (keys 1/2/3).
        roles: Roles ("user"/"admin") for the 3 card UIDs.
        pms: Registered locker PMs for the 3 tag UIDs (keys A/S/D).

    Returns:
        None. Progress is printed to stdout (UIDs masked).

    Raises:
        SystemExit: If a PM is not a registered locker device, or a device
            already carries a *different* tag (pass nothing — re-tap with the
            real bind flow or unbind first). No device row is ever created.
    """
    for key, uid, name, role in zip(_CARD_KEYS, FAKE_CARD_UIDS, names, roles):
        if _user_enrolled(uid):
            print(f"Key {key}: already enrolled ({enroll_card._mask_uid(uid)})")
            continue
        enroll_card._enroll(uid, name, role)
    for key, uid, pm_number in zip(_TAG_KEYS, FAKE_TAG_UIDS, pms):
        if _tag_already_bound(uid, pm_number):
            print(
                f"Key {key}: already bound to {pm_number} "
                f"({enroll_card._mask_uid(uid)})"
            )
            continue
        enroll_device_tag._bind(uid, pm_number, force=False)


def main() -> None:
    """Seed the fake-reader preset UIDs into the local database.

    Parses display names, roles, and locker PMs from the command line, refuses
    to run unless ``SMART_LOCKER_FAKE_READER`` is enabled (dev-only footgun
    guard), then enrolls the 3 preset cards and binds the 3 preset tags.

    Returns:
        None. Progress is printed to stdout.
    """
    parser = argparse.ArgumentParser(
        description="Dev-only: enroll the fake-reader preset cards/tags "
                    "(panel keys 1/2/3/A/S/D).",
        epilog="Requires SMART_LOCKER_FAKE_READER=1 (never set on the Pi). "
               "Idempotent: reruns skip what is already seeded. PMs must "
               "already be registered locker devices (Register Device) — "
               "missing PMs abort the bind step with an error.",
    )
    for i in range(3):
        parser.add_argument(
            f"--name-{i + 1}",
            default=DEFAULT_NAMES[i],
            help=f"Display name for card key {_CARD_KEYS[i]} "
                 f"(default: {DEFAULT_NAMES[i]})",
        )
        parser.add_argument(
            f"--role-{i + 1}",
            choices=["user", "admin"],
            default=DEFAULT_ROLES[i],
            help=f"Role for card key {_CARD_KEYS[i]} "
                 f"(default: {DEFAULT_ROLES[i]})",
        )
    for i in range(3):
        parser.add_argument(
            f"--pm-{_TAG_KEYS[i].lower()}",
            default=DEFAULT_PMS[i],
            help=f"Registered locker PM to bind tag key {_TAG_KEYS[i]} to "
                 f"(default: {DEFAULT_PMS[i]}). Must already exist — the "
                 f"script never creates device rows.",
        )
    args = parser.parse_args()

    if not fake_reader_enabled():
        print(
            "ERROR: Refusing to seed — SMART_LOCKER_FAKE_READER is not enabled. "
            "This dev-only script seeds test cards/tags for the simulated "
            "reader; enable the fake reader on a dev machine (never on the Pi)."
        )
        raise SystemExit(1)

    setup_logging()
    init_db()

    seed(
        [args.name_1, args.name_2, args.name_3],
        [args.role_1, args.role_2, args.role_3],
        [args.pm_a, args.pm_s, args.pm_d],
    )


if __name__ == "__main__":
    main()
