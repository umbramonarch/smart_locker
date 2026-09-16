"""
File: settings.py
Description: Central configuration and path constants for the Smart Locker system.
             Loads environment variables via python-dotenv, sets defaults, and
             exposes typed settings for all modules.
Project: config
Notes: Requires a .env file with SMART_LOCKER_ENC_KEY and SMART_LOCKER_HMAC_KEY
       at minimum. See .env.example for the full list of variables.
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Load .env file so all os.getenv() calls below pick up user-defined overrides
load_dotenv()

# Project root directory — used as the base for relative file paths
BASE_DIR = Path(__file__).resolve().parent.parent

# --- Database ---
# Absolute path to the SQLite database file
DB_PATH = os.getenv("SMART_LOCKER_DB_PATH", str(BASE_DIR / "smart_locker.db"))
# SQLAlchemy connection string derived from DB_PATH
DATABASE_URL = f"sqlite:///{DB_PATH}"

# --- NFC Reader ---
# Substring matched against connected reader names to auto-select the correct device
READER_NAME_FILTER = os.getenv("SMART_LOCKER_READER_NAME", "ACR1252")
# pyscard CardMonitor polling interval in milliseconds (500 ms balances responsiveness and CPU)
CARD_POLL_INTERVAL_MS = 500


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    """Parse an integer env var; fall back to ``default`` on a bad value.

    Args:
        name: Environment variable name.
        default: Used when unset, empty, or not an integer.
        minimum: If set, clamp the parsed value up to this floor.

    Returns:
        Parsed integer, or ``default`` (then clamped).
    """
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        value = default
    else:
        try:
            value = int(str(raw).strip())
        except ValueError:
            logger.warning(
                "Invalid %s=%r — using %s.", name, raw, default
            )
            value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


# --- Session ---
# Idle timeout in seconds — session is silently ended after this period of inactivity
SESSION_TIMEOUT_SECONDS = _env_int("SMART_LOCKER_SESSION_TIMEOUT", 120)

# --- Borrow limit ---
# Maximum number of devices a single user may borrow concurrently
MAX_BORROWS = _env_int("SMART_LOCKER_MAX_BORROWS", 5)

# --- Calibration alerts ---
# Days ahead of the Excel calibration date to flag a device as "due soon" (0 = only today)
CALIBRATION_WARN_DAYS = _env_int("SMART_LOCKER_CALIBRATION_WARN_DAYS", 14, minimum=0)

# Cabinet slot picker and Register Device / change-slot API upper bound.
MAX_LOCKER_SLOT = 48

# --- Web server ---
API_HOST = os.getenv("SMART_LOCKER_API_HOST", "0.0.0.0")
API_PORT = _env_int("SMART_LOCKER_API_PORT", 8000)

# --- Excel export ---
# Path for the exported workbook (Devices / Transactions / Users sheets). On the
# Raspberry Pi this points at the locker share (e.g.
# /mnt/locker/smart_locker_data.xlsx) so the export lands where colleagues read it.
EXCEL_SYNC_PATH = os.getenv("SMART_LOCKER_EXCEL_PATH") or str(
    BASE_DIR / "smart_locker_data.xlsx"
)

# Auto-write the export to EXCEL_SYNC_PATH after each source import and photo change.
# Off by default (export stays on-demand via admin Export Excel). Set to 1 only if
# you still want smart_locker_data.xlsx refreshed on the locker share automatically.
EXCEL_AUTO_EXPORT = os.getenv("SMART_LOCKER_EXCEL_AUTO_EXPORT", "").strip().lower() in {
    "1", "true", "yes", "on",
}

# --- Source Excel ---
# Catalog spreadsheet on the locker share (SMB/CIFS mount) — on the Pi this
# is the mounted path, e.g. /mnt/locker/device-list.xlsx. Empty disables auto-import.
SOURCE_EXCEL_PATH = os.getenv("SMART_LOCKER_SOURCE_EXCEL_PATH", "")

# Hours between automatic source imports (startup import + admin Sync still run).
# Minimum 1. Values below 1 are raised to 1 so a zero env cannot spin the importer.
SOURCE_SYNC_INTERVAL_HOURS = _env_int(
    "SMART_LOCKER_SOURCE_SYNC_INTERVAL_HOURS", 6, minimum=1
)

# Site overlay: display name and extra Excel header aliases. Storage/API stay
# pm_number. Extra headers are merged with the built-in English lists.
# Comma-separated, case-insensitive. Read on each use so tests can setenv.


def parse_csv_aliases(raw: str | None) -> list[str]:
    """Split a comma-separated header list into stripped lowercase names.

    Args:
        raw: Env value such as ``"Inventory No, Asset Tag"``.

    Returns:
        Lowercased aliases, empty strings dropped.
    """
    return [part.strip().lower() for part in (raw or "").split(",") if part.strip()]


def asset_label() -> str:
    """Kiosk/dashboard noun for the locker join key.

    Returns:
        Label from ``SMART_LOCKER_ASSET_LABEL``, or ``PM number``.
    """
    return (os.getenv("SMART_LOCKER_ASSET_LABEL") or "PM number").strip() or "PM number"


def in_locker_token() -> str:
    """Excel Location cell written when a locker device is not borrowed.

    Returns:
        Token from ``SMART_LOCKER_IN_LOCKER_TOKEN``, or ``Locker``.
    """
    return (os.getenv("SMART_LOCKER_IN_LOCKER_TOKEN") or "Locker").strip() or "Locker"


def maintenance_token() -> str:
    """Excel Location cell written while a locker device is in maintenance.

    Returns:
        Token from ``SMART_LOCKER_MAINTENANCE_TOKEN``, or ``Maintenance``.
    """
    return (
        os.getenv("SMART_LOCKER_MAINTENANCE_TOKEN") or "Maintenance"
    ).strip() or "Maintenance"


def id_header_extras() -> list[str]:
    """Extra Excel header aliases for the join key.

    Returns:
        Aliases from ``SMART_LOCKER_ID_HEADERS``.
    """
    return parse_csv_aliases(os.getenv("SMART_LOCKER_ID_HEADERS", ""))


def location_header_extras() -> list[str]:
    """Extra Excel header aliases for the Location column.

    Returns:
        Aliases from ``SMART_LOCKER_LOCATION_HEADERS``.
    """
    return parse_csv_aliases(os.getenv("SMART_LOCKER_LOCATION_HEADERS", ""))

# --- Dashboard share launcher ---
# Origin of this Pi as colleagues see it (e.g. http://192.168.1.10:8000).
# Combined with DASHBOARD_SHARE_PATH, startup writes dashboard.url on the locker
# share so a double-click opens the live /dashboard.
PUBLIC_URL = os.getenv("SMART_LOCKER_PUBLIC_URL", "").strip()
DASHBOARD_SHARE_PATH = os.getenv("SMART_LOCKER_DASHBOARD_SHARE_PATH", "").strip()

# --- Photo import ---
# Input folder for device photos — filenames must match the device model
# (e.g., "87V.jpg" applies to all devices with model "87V"). Leave empty to disable.
PHOTO_INPUT_PATH = os.getenv("SMART_LOCKER_PHOTO_INPUT_PATH", "")

# Destination directory inside the frontend static files where photos are served from
PHOTO_SERVE_DIR = BASE_DIR / "smart_locker" / "frontend" / "images"

# --- Security ---
# Environment variable names for the two cryptographic keys (actual keys loaded by key_manager)
ENC_KEY_ENV_VAR = "SMART_LOCKER_ENC_KEY"
HMAC_KEY_ENV_VAR = "SMART_LOCKER_HMAC_KEY"

# Header LAN browsers send for dashboard mutations (bind/unbind, later owner).
DASHBOARD_ADMIN_HEADER = "X-Smart-Locker-Admin"


def dashboard_admin_secret() -> str:
    """Shared secret for dashboard admin mutations.

    Read on each call so tests can setenv. Empty means fail closed: mutating
    dashboard routes must 401 rather than treating an admin SQLite row as auth.

    Returns:
        Stripped ``SMART_LOCKER_DASHBOARD_ADMIN_SECRET``, or ``""``.
    """
    return (os.getenv("SMART_LOCKER_DASHBOARD_ADMIN_SECRET") or "").strip()
