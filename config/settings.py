"""
File: settings.py
Description: Central configuration and path constants for the Smart Locker system.
             Loads environment variables via python-dotenv, sets defaults, and
             exposes typed settings for all modules.
Project: config
Notes: Requires a .env file with SMART_LOCKER_ENC_KEY and SMART_LOCKER_HMAC_KEY
       at minimum. See .env.example for the full list of variables.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

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

# --- Session ---
# Idle timeout in seconds — session is silently ended after this period of inactivity
SESSION_TIMEOUT_SECONDS = int(os.getenv("SMART_LOCKER_SESSION_TIMEOUT", "120"))

# --- Borrow limit ---
# Maximum number of devices a single user may borrow concurrently
MAX_BORROWS = int(os.getenv("SMART_LOCKER_MAX_BORROWS", "5"))

# --- Web server ---
API_HOST = os.getenv("SMART_LOCKER_API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("SMART_LOCKER_API_PORT", "8000"))

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
# Company device master list on the locker share (SMB/CIFS mount) — on the Pi this
# is the mounted path, e.g. /mnt/locker/device-list.xlsx. Empty disables auto-import.
SOURCE_EXCEL_PATH = os.getenv("SMART_LOCKER_SOURCE_EXCEL_PATH", "")

# Hours between automatic source imports (startup import + admin Sync still run).
# Minimum 1. Values below 1 are raised to 1 so a zero env cannot spin the importer.
SOURCE_SYNC_INTERVAL_HOURS = max(1, int(os.getenv("SMART_LOCKER_SOURCE_SYNC_INTERVAL_HOURS", "6")))

# --- Photo import ---
# Input folder for device photos — filenames must match the device model/Typbezeichnung
# (e.g., "87V.jpg" applies to all devices with model "87V"). Leave empty to disable.
PHOTO_INPUT_PATH = os.getenv("SMART_LOCKER_PHOTO_INPUT_PATH", "")

# Destination directory inside the frontend static files where photos are served from
PHOTO_SERVE_DIR = BASE_DIR / "smart_locker" / "frontend" / "images"

# --- Security ---
# Environment variable names for the two cryptographic keys (actual keys loaded by key_manager)
ENC_KEY_ENV_VAR = "SMART_LOCKER_ENC_KEY"
HMAC_KEY_ENV_VAR = "SMART_LOCKER_HMAC_KEY"
