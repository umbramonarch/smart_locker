"""
File: seed_fake_nfc.py
Description: Dev-only seed for the no-hardware fake NFC reader. Enrolls the
             preset work-card UIDs behind the kiosk dev panel (keys 1/2/3) as
             users, and binds the preset device-tag UIDs (keys A/S/D) to
             locker units so a dev box can drive tap -> borrow -> return with
             no NFC hardware. Refuses to run unless SMART_LOCKER_FAKE_READER
             is enabled; reruns are idempotent. Never run on the Pi.
Project: smart_locker/scripts
Notes: Usage:
         SMART_LOCKER_FAKE_READER=1 python -m scripts.seed_fake_nfc
         SMART_LOCKER_FAKE_READER=1 python -m scripts.seed_fake_nfc \\
             --pm PM-101 --pm PM-102 --pm PM-103
       Without --pm, the still-unbound preset tags bind to the first
       untagged locker units in slot order; a unit already carrying a
       different sticker is never overwritten. Preset UIDs live in
       smart_locker/nfc/fake_reader.py and the dev-status feed — change
       all three together. Raw UIDs are masked in output and never
       logged.
"""

import argparse
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.logging_config import setup_logging
from scripts.uid_helpers import mask_uid
from smart_locker.auth.tap_router import bind_uid_to_device
from smart_locker.database.engine import get_session, init_db
from smart_locker.database.repositories import DeviceRepository, UserRepository
from smart_locker.nfc.factory import fake_reader_enabled
from smart_locker.nfc.fake_reader import FAKE_CARD_UIDS, FAKE_TAG_UIDS
from smart_locker.security.hashing import compute_uid_hmac
from smart_locker.security.key_manager import key_manager
from smart_locker.services.user_service import UserService

# Names/roles for the preset cards, in FAKE_CARD_UIDS order (keys 1/2/3).
PRESET_PEOPLE = [
    ("Fake Admin", "admin"),
    ("Fake User 2", "user"),
    ("Fake User 3", "user"),
]


def _enroll_cards(session, user_svc) -> tuple[int, list[str]]:
    """Enroll each preset card UID as a user; skip ones already present.

    A digest miss on a name that already exists means a stale row left by
    an old HMAC key — it can never match a card. The new row is still
    enrolled (the old digest is dead) but the collision is noted.

    Returns:
        Tuple of (newly enrolled count, error strings).
    """
    enrolled = 0
    errors: list[str] = []
    existing_names = {u.display_name for u in UserRepository.list_all(session)}
    for (name, role), uid in zip(PRESET_PEOPLE, FAKE_CARD_UIDS):
        uid_hmac = compute_uid_hmac(uid, key_manager.hmac_key)
        if UserRepository.find_by_uid_hmac(session, uid_hmac) is not None:
            print(f"  SKIP  {name} (card {mask_uid(uid)} already enrolled)")
            continue
        try:
            user_svc.enroll_user(session, display_name=name, card_uid_hex=uid, role=role)
        except ValueError as e:
            errors.append(f"{name}: {e}")
            continue
        enrolled += 1
        print(f"  OK    {name} ({role}) - card {mask_uid(uid)}")
        if name in existing_names:
            print(
                f"  NOTE  {name}: a stale row with this name exists "
                "(old HMAC key?) — it will never match a card"
            )
    return enrolled, errors


def _bind_tags(session, pms: list[str]) -> tuple[int, list[str]]:
    """Bind each preset tag UID to a locker unit.

    With ``--pm`` each UID binds to that PM in order; without it, the
    preset UIDs not bound anywhere bind to the first untagged locker
    units in slot order. A unit already carrying a different sticker is
    never clobbered — that needs an explicit unbind first.

    Returns:
        Tuple of (newly bound count, error strings).
    """
    bound = 0
    errors: list[str] = []
    if pms:
        # Positional pairing is operator intent: the nth --pm flag takes
        # the nth preset UID, and bad flags leave no hole.
        uids = FAKE_TAG_UIDS
        targets = []
        for pm in pms:
            device = DeviceRepository.find_by_pm(session, pm)
            if device is None:
                errors.append(f"No device with PM '{pm}'.")
            elif device.locker_slot is None:
                errors.append(f"{pm} is not a locker unit — register it first.")
            else:
                targets.append(device)
    else:
        # A rerun must not re-pair a live binding onto a newly registered
        # unit — only preset UIDs with no current owner may take a target.
        uids = []
        for uid in FAKE_TAG_UIDS:
            owner = DeviceRepository.find_by_tag_hmac(
                session, compute_uid_hmac(uid, key_manager.hmac_key)
            )
            if owner is not None:
                print(
                    f"  SKIP  tag {mask_uid(uid)} already bound to "
                    f"{owner.name} ({owner.pm_number})"
                )
            else:
                uids.append(uid)
        targets = [
            d for d in DeviceRepository.list_by_slot(session)
            if d.tag_hmac is None
        ]
        if not targets:
            # Not a failure — the catalog may simply be unregistered yet;
            # the user enrollment above still stands on its own.
            print(
                "  NOTE  no untagged locker units - register devices first "
                "(or pass --pm to target specific rows)."
            )
        unbound = len(uids) - len(targets)
        if unbound > 0:
            print(
                f"  NOTE  {unbound} preset sticker(s) stay unbound - "
                "their dev keys inject an UNKNOWN tag until more locker "
                "units register (or pass --pm)."
            )
    for uid, device in zip(uids, targets):
        tag_hmac = compute_uid_hmac(uid, key_manager.hmac_key)
        if device.tag_hmac == tag_hmac:
            print(f"  SKIP  {device.pm_number} (tag {mask_uid(uid)} already bound)")
            continue
        if device.tag_hmac is not None:
            errors.append(
                f"{device.pm_number} already has a different tag bound — "
                "unbind it first"
            )
            continue
        try:
            bind_uid_to_device(session, device, uid, key_manager.hmac_key)
        except ValueError as e:
            errors.append(f"{device.pm_number}: {e}")
            continue
        bound += 1
        print(f"  OK    tag {mask_uid(uid)} -> {device.name} ({device.pm_number})")
    return bound, errors


def main() -> None:
    """Seed preset fake-reader UIDs (dev boxes only).

    Returns:
        None. Progress and a summary are printed to stdout.
    """
    parser = argparse.ArgumentParser(
        description="Seed preset users/stickers for the simulated NFC reader."
    )
    parser.add_argument(
        "--pm",
        action="append",
        default=[],
        metavar="PM",
        help="Locker PM to bind a preset tag to (repeat, max %d). "
             "Default: first untagged locker units in slot order."
             % len(FAKE_TAG_UIDS),
    )
    args = parser.parse_args()

    setup_logging()
    if not fake_reader_enabled():
        print("ERROR: simulation harness only - set SMART_LOCKER_FAKE_READER=1 "
              "(dev machines; never on the Pi).")
        raise SystemExit(1)
    if len(args.pm) > len(FAKE_TAG_UIDS):
        print(f"ERROR: at most {len(FAKE_TAG_UIDS)} --pm flags.")
        raise SystemExit(1)

    init_db()
    try:
        user_svc = UserService(
            enc_key=key_manager.enc_key, hmac_key=key_manager.hmac_key
        )
    except (EnvironmentError, ValueError) as e:
        print(f"ERROR: {e}")
        raise SystemExit(1)

    print("Seeding fake-reader presets...")
    with get_session() as session:
        enrolled, card_errors = _enroll_cards(session, user_svc)
        bound, tag_errors = _bind_tags(session, args.pm)

    for line in card_errors + tag_errors:
        print(f"  ERROR {line}")
    print(
        f"Done: {enrolled} user(s) enrolled, {bound} tag(s) bound. "
        "Dev panel keys 1-3 tap the preset cards, A/S/D the preset stickers."
    )
    if card_errors or tag_errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
