"""
File: helpers.py
Description: Seeding helpers for E2E tests. Creates users, devices, and tag
             binds whose uid_hmac matches a simulated card UID, so a fake tap
             resolves to the row exactly as a real card would.
Project: smart_locker/tests/e2e
"""

from smart_locker.database.repositories import DeviceRepository, UserRepository
from smart_locker.security.hashing import compute_uid_hmac
from smart_locker.security.key_manager import key_manager


def uid_hmac_for(uid: str) -> str:
    """HMAC digest the DB stores for a given tap UID."""
    return compute_uid_hmac(uid, key_manager.hmac_key)


def add_user(harness, uid: str, display_name: str = "E2E User", role: str = "user") -> int:
    """Create a user whose card UID is `uid`; returns the user id."""
    with harness.db() as db:
        user = UserRepository.create(
            db,
            display_name=display_name,
            uid_hmac=uid_hmac_for(uid),
            encrypted_card_uid=f"enc:{uid}",
            role=role,
        )
        db.commit()
        return user.id


def add_device(
    harness,
    name: str = "Camera",
    pm_number: str = "PM-100",
    locker_slot: int = 1,
    tag_uid: str | None = None,
    status: str | None = None,
    model: str | None = None,
    calibration_due=None,
) -> int:
    """Create a locker device, optionally with a bound tag; returns the id."""
    with harness.db() as db:
        device = DeviceRepository.create(
            db,
            name=name,
            device_type="Tool",
            pm_number=pm_number,
            locker_slot=locker_slot,
            status=status,
            model=model,
            calibration_due=calibration_due,
        )
        if tag_uid is not None:
            DeviceRepository.bind_tag(db, device, uid_hmac_for(tag_uid))
        db.commit()
        return device.id


def get_device(harness, device_id: int):
    """Fresh read of a device row (new session, no stale state)."""
    from smart_locker.database.models import Device

    with harness.db() as db:
        return db.get(Device, device_id)


def get_user(harness, user_id: int):
    """Fresh read of a user row."""
    from smart_locker.database.models import User

    with harness.db() as db:
        return db.get(User, user_id)
