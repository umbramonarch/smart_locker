"""
File: device_catalog.py
Description: Shared locker device representation and tag unbinding for kiosk and dashboard.
Project: smart_locker/services
Notes: Client-specific fields are selected at the API boundary; tag digests never leave SQLite.
"""

from smart_locker.database.models import Device, DeviceStatus
from smart_locker.database.repositories import DeviceRepository


def device_record(device: Device, *, current_user_id: int | None = None) -> dict:
    """Build the common, display-safe fields for a locker device."""
    borrower_name = None
    if device.status == DeviceStatus.BORROWED and device.current_borrower_id is not None:
        if device.current_borrower_id == current_user_id:
            borrower_name = "You"
        elif device.current_borrower is not None:
            borrower_name = device.current_borrower.display_name
    return {
        "pm_number": device.pm_number,
        "name": device.name,
        "device_type": device.device_type,
        "serial_number": device.serial_number,
        "manufacturer": device.manufacturer,
        "model": device.model,
        "locker_slot": device.locker_slot,
        "description": device.description,
        "calibration_due": device.calibration_due.isoformat() if device.calibration_due else None,
        "status": device.status.value,
        "borrower_name": borrower_name,
        "has_tag": device.tag_hmac is not None,
    }


def unbind_device_tag(session, device: Device) -> None:
    """Clear a tag through the same service for either authorized client."""
    DeviceRepository.unbind_tag(session, device)
