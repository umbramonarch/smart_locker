"""
File: update_device.py
Description: Update device fields (image, description, or any column) by PM
             number. Supports single-device updates, batch updates from a text
             file, auto-matching photos by PM-numbered filenames, and listing
             all devices with their current status.
Project: smart_locker/scripts
Notes: Usage: python -m scripts.update_device --list | --auto |
       --pm PM-042 --image photo.jpg --description "..." |
       --batch updates.txt
       Changes are automatically synced to the output Excel file.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.logging_config import setup_logging
from smart_locker.database.engine import get_session, init_db
from smart_locker.database.models import Device
from sqlalchemy import select


# Device columns that can be modified via this script (excludes status, borrower, etc.)
UPDATABLE_FIELDS = {
    "name", "device_type", "serial_number", "manufacturer", "model",
    "barcode", "locker_slot", "description", "image_path", "calibration_due",
}


def list_devices() -> None:
    """Print a formatted table of all devices with key fields.

    Queries all devices ordered by locker slot and name, then prints a
    table showing slot, PM number, name, image path, and description.

    Returns:
        None. Device table is printed to stdout.
    """
    init_db()
    with get_session() as session:
        devices = session.execute(
            select(Device).order_by(Device.locker_slot, Device.name)
        ).scalars().all()

        if not devices:
            print("No devices in database.")
            return

        print(f"\n{'Slot':<6} {'PM':<15} {'Name':<35} {'Image':<20} {'Description'}")
        print(f"{'---':<6} {'---':<15} {'---':<35} {'---':<20} {'---'}")
        for d in devices:
            slot = str(d.locker_slot) if d.locker_slot is not None else "-"
            img = d.image_path or "-"
            desc = (d.description[:40] + "...") if d.description and len(d.description) > 40 else (d.description or "-")
            print(f"{slot:<6} {d.pm_number:<15} {d.name[:34]:<35} {img[:19]:<20} {desc}")
        print(f"\n{len(devices)} device(s) total.")


def update_device(pm_number: str, updates: dict) -> None:
    """Update a single device's fields by PM number.

    Looks up the device, applies each field update (skipping non-updatable
    fields), flushes to the database, and triggers an Excel sync export.

    Args:
        pm_number: The PM number identifying the device (e.g. ``"PM-042"``).
        updates: Mapping of field names to new values
                 (e.g. ``{"image_path": "images/osc.jpg"}``).

    Returns:
        None. Progress is printed to stdout.
    """
    init_db()
    with get_session() as session:
        device = session.execute(
            select(Device).where(Device.pm_number == pm_number)
        ).scalar_one_or_none()

        if device is None:
            print(f"ERROR: No device found with PM number '{pm_number}'.")
            return

        for field, value in updates.items():
            if field not in UPDATABLE_FIELDS:
                print(f"  SKIP: '{field}' is not an updatable field. Valid: {sorted(UPDATABLE_FIELDS)}")
                continue

            if field == "locker_slot":
                # locker_slot is an INTEGER column — cast string input to int
                value = int(value) if value else None

            old_val = getattr(device, field)
            setattr(device, field, value)
            print(f"  {field}: '{old_val}' -> '{value}'")

        session.flush()
        print(f"Updated: {device.pm_number} ({device.name})")

    # Trigger Excel sync
    from config.settings import EXCEL_SYNC_PATH
    from smart_locker.database.engine import get_engine
    from smart_locker.sync.excel_sync import export_to_excel
    export_to_excel(get_engine(), EXCEL_SYNC_PATH)
    print(f"Excel sync: {EXCEL_SYNC_PATH}")


def batch_update(batch_file: str) -> None:
    """Read device updates from a text file and apply them in bulk.

    Each non-empty, non-comment line must follow the format
    ``PM_NUMBER field_name value``. Lines are grouped by PM number so
    multiple fields for the same device are applied in a single update.

    Example file content::

        PM-001 image_path oscilloscope.jpg
        PM-001 description 4-channel 500MHz digital oscilloscope
        PM-002 image_path power_supply.jpg

    Args:
        batch_file: Path to the text file containing update lines.

    Returns:
        None. Progress is printed to stdout per device.
    """
    file_path = Path(batch_file)
    if not file_path.exists():
        print(f"ERROR: File not found: {file_path}")
        return

    lines = file_path.read_text(encoding="utf-8").strip().splitlines()
    updates_by_pm: dict[str, dict] = {}

    for line_num, line in enumerate(lines, start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            print(f"  SKIP line {line_num}: need 'PM field value', got: {line}")
            continue
        pm, field, value = parts
        updates_by_pm.setdefault(pm, {})[field] = value

    for pm, updates in updates_by_pm.items():
        print(f"\n--- {pm} ---")
        update_device(pm, updates)


def auto_match_images() -> None:
    """Scan frontend/images/ for files named by PM number and link them automatically.

    Iterates over image files in the frontend images directory, matches each
    filename stem (case-insensitive) against device PM numbers in the database,
    and sets ``image_path`` for every match. Supports .jpg, .jpeg, .png, .webp.

    Returns:
        None. Matched/unmatched counts and per-device updates are printed to stdout.
    """
    images_dir = Path(__file__).resolve().parent.parent / "smart_locker" / "frontend" / "images"
    if not images_dir.exists():
        print(f"ERROR: Images directory not found: {images_dir}")
        return

    image_extensions = {".jpg", ".jpeg", ".png", ".webp"}
    image_files = [
        f for f in images_dir.iterdir()
        if f.is_file() and f.suffix.lower() in image_extensions
    ]

    if not image_files:
        print(f"No image files found in {images_dir}")
        return

    init_db()
    with get_session() as session:
        devices = session.execute(select(Device)).scalars().all()
        pm_to_device = {d.pm_number.lower(): d for d in devices if d.pm_number}

    # Match files to PM numbers
    matched = []
    unmatched = []
    for img_file in sorted(image_files):
        stem = img_file.stem.lower()  # e.g. "pm-001"
        if stem in pm_to_device:
            matched.append((pm_to_device[stem].pm_number, img_file.name))
        else:
            unmatched.append(img_file.name)

    if not matched:
        print("No images matched any PM numbers.")
        print(f"  Found {len(image_files)} image(s), but none matched device PM numbers.")
        print("  Tip: name photos by PM number, e.g. PM-001.jpg, PM-002.png")
        return

    print(f"\nFound {len(matched)} match(es):\n")
    for pm, filename in matched:
        print(f"  {pm} <- {filename}")

    if unmatched:
        print(f"\n  {len(unmatched)} unmatched file(s): {', '.join(unmatched)}")

    print()
    for pm, filename in matched:
        update_device(pm, {"image_path": f"images/{filename}"})

    print(f"\nDone. {len(matched)} device(s) updated.")


def main() -> None:
    """Parse CLI arguments and dispatch to the appropriate update action.

    Supports four modes: ``--list`` (show all devices), ``--batch`` (bulk
    update from file), ``--auto`` (auto-match images by PM number), or
    single-device update via ``--pm`` with ``--image``/``--description``/
    ``--field``+``--value``.

    Returns:
        None.
    """
    parser = argparse.ArgumentParser(description="Update device fields by PM number.")
    parser.add_argument("--pm", help="PM number of the device to update")
    parser.add_argument("--image", help="Set image_path (filename in frontend/images/)")
    parser.add_argument("--description", "--desc", help="Set description text")
    parser.add_argument("--field", help="Field name to update (for arbitrary fields)")
    parser.add_argument("--value", help="Value to set (used with --field)")
    parser.add_argument("--list", action="store_true", help="List all devices")
    parser.add_argument("--batch", help="Batch update from a text file (PM field value per line)")
    parser.add_argument("--auto", action="store_true", help="Auto-match images named by PM number (e.g. PM-001.jpg)")
    args = parser.parse_args()

    setup_logging()

    if args.list:
        list_devices()
        return

    if args.batch:
        batch_update(args.batch)
        return

    if args.auto:
        auto_match_images()
        return

    if not args.pm:
        print("ERROR: --pm is required (or use --list / --batch).")
        parser.print_help()
        return

    updates = {}
    if args.image:
        updates["image_path"] = f"images/{args.image}" if "/" not in args.image else args.image
    if args.description:
        updates["description"] = args.description
    if args.field and args.value is not None:
        updates[args.field] = args.value

    if not updates:
        print("ERROR: Nothing to update. Use --image, --description, or --field/--value.")
        return

    update_device(args.pm, updates)


if __name__ == "__main__":
    main()
