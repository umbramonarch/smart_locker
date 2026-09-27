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

# Cabinet slot picker and Register Device / change-slot API upper bound.
MAX_LOCKER_SLOT = 48

# --- Web server ---
API_HOST = os.getenv("SMART_LOCKER_API_HOST", "0.0.0.0")
API_PORT = _env_int("SMART_LOCKER_API_PORT", 8000)

# --- Catalog mirror ---
# The workbook the Pi writes. The SQLite devices table is the catalog; the
# .xlsx is a hidden mirror of it (catalog columns + Location), regenerated
# whenever the file can be written. SMART_LOCKER_MIRROR_PATH wins;
# SMART_LOCKER_SOURCE_EXCEL_PATH is kept as the legacy name so a preserved
# Pi .env still points the mirror at the share file it used to import.
# With neither set, the mirror lives next to the database on the Pi.
SOURCE_EXCEL_PATH = os.getenv("SMART_LOCKER_SOURCE_EXCEL_PATH", "")


def mirror_path() -> Path | None:
    """Path of the Pi-written catalog mirror workbook, or None when disabled.

    ``SMART_LOCKER_MIRROR_PATH`` wins, then the legacy
    ``SMART_LOCKER_SOURCE_EXCEL_PATH``, then a default file next to the
    database. An in-memory or unset database path yields None (tests and
    dev harnesses opt in by configuring a path).
    """
    override = (os.getenv("SMART_LOCKER_MIRROR_PATH") or "").strip()
    if override:
        return Path(override)
    legacy = (SOURCE_EXCEL_PATH or "").strip()
    if legacy:
        return Path(legacy)
    if not DB_PATH or DB_PATH == ":memory:":
        return None
    return Path(DB_PATH).with_name("smart_locker_catalog.xlsx")


# Seconds between mirror ticks (pending-write flush + external-edit check).
MIRROR_SYNC_SECONDS = _env_int(
    "SMART_LOCKER_MIRROR_SYNC_SECONDS", 60, minimum=5
)

# Env override for the mirror-state JSON path (tests point it at a temp
# path). Default: ``mirror_state.json`` next to the database.
MIRROR_STATE_PATH_ENV_VAR = "SMART_LOCKER_MIRROR_STATE_PATH"


def mirror_state_path() -> Path:
    """JSON file holding the mirror writer's persisted state."""
    override = (os.getenv(MIRROR_STATE_PATH_ENV_VAR) or "").strip()
    if override:
        return Path(override)
    if not DB_PATH or DB_PATH == ":memory:":
        return BASE_DIR / "logs" / "mirror_state.json"
    return Path(DB_PATH).with_name("mirror_state.json")

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
    """Location cell written for a device that is in maintenance.

    Returns:
        Token from ``SMART_LOCKER_MAINTENANCE_TOKEN``, or ``Maintenance``.
    """
    return (
        os.getenv("SMART_LOCKER_MAINTENANCE_TOKEN") or "Maintenance"
    ).strip() or "Maintenance"


def calibration_warn_days() -> int:
    """Days before the calibration date that the due-soon badge appears.

    Borrow is refused on the due date itself regardless of this window.
    Read on each call so tests can setenv.

    Returns:
        ``SMART_LOCKER_CALIBRATION_WARN_DAYS`` clamped at 0, or 14.
    """
    return _env_int("SMART_LOCKER_CALIBRATION_WARN_DAYS", 14, minimum=0)


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

# Environment variable that stores the dashboard admin password.
DASHBOARD_ADMIN_SECRET_ENV_VAR = "SMART_LOCKER_DASHBOARD_ADMIN_SECRET"

# Env override for the Setup-written secret file location (tests point it at a
# temp path). Default: ``dashboard.secret`` next to .env at the app root.
DASHBOARD_SECRET_PATH_ENV_VAR = "SMART_LOCKER_DASHBOARD_SECRET_PATH"


def dashboard_secret_path() -> Path:
    """File that holds the Setup-typed dashboard admin password.

    The password CANNOT go in .env: on the installed appliance ``.env`` is
    root-owned and the app directory is sticky — the service must not be able
    to rewrite the file that update.sh parses as root. A dedicated
    service-owned file is the only durable write the kiosk needs.
    """
    override = (os.getenv(DASHBOARD_SECRET_PATH_ENV_VAR) or "").strip()
    return Path(override) if override else BASE_DIR / "dashboard.secret"


def dashboard_admin_secret() -> str:
    """Shared secret for dashboard admin mutations.

    ``SMART_LOCKER_DASHBOARD_ADMIN_SECRET`` (env/.env) wins when set — that is
    the operator-configured path. Otherwise the first-boot Setup file written
    by ``setup_service.write_dashboard_secret`` supplies it. Read on each call
    so tests can setenv. Empty means fail closed: mutating dashboard routes
    must 401 rather than treating an admin SQLite row as auth.

    Returns:
        Stripped dashboard admin secret, or ``""``.
    """
    env = (os.getenv(DASHBOARD_ADMIN_SECRET_ENV_VAR) or "").strip()
    if env:
        return env
    try:
        return dashboard_secret_path().read_text(encoding="utf-8").strip()
    except OSError:
        return ""
