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

# --- Excel sync ---
# Auto-export DB state to Excel on every change (Devices, Transactions, Users sheets)
EXCEL_SYNC_PATH = os.getenv(
    "SMART_LOCKER_EXCEL_PATH", str(BASE_DIR / "smart_locker_data.xlsx")
)

# --- Source Excel ---
# Company device master list (OneDrive path) — leave empty to disable automatic import
SOURCE_EXCEL_PATH = os.getenv("SMART_LOCKER_SOURCE_EXCEL_PATH", "")

# Daily source import schedule in 24-hour format (default: 06:00)
SOURCE_SYNC_HOUR = int(os.getenv("SMART_LOCKER_SOURCE_SYNC_HOUR", "6"))
SOURCE_SYNC_MINUTE = int(os.getenv("SMART_LOCKER_SOURCE_SYNC_MINUTE", "0"))

# --- Security ---
# Environment variable names for the two cryptographic keys (actual keys loaded by key_manager)
ENC_KEY_ENV_VAR = "SMART_LOCKER_ENC_KEY"
HMAC_KEY_ENV_VAR = "SMART_LOCKER_HMAC_KEY"
