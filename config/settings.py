"""Central configuration for the Smart Locker system."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# Database
DB_PATH = os.getenv("SMART_LOCKER_DB_PATH", str(BASE_DIR / "smart_locker.db"))
DATABASE_URL = f"sqlite:///{DB_PATH}"

# NFC Reader
READER_NAME_FILTER = os.getenv("SMART_LOCKER_READER_NAME", "ACR1252")
CARD_POLL_INTERVAL_MS = 500

# Session
SESSION_TIMEOUT_SECONDS = int(os.getenv("SMART_LOCKER_SESSION_TIMEOUT", "120"))

# Borrow limit
MAX_BORROWS = int(os.getenv("SMART_LOCKER_MAX_BORROWS", "5"))

# Security — keys loaded via key_manager, not directly here
ENC_KEY_ENV_VAR = "SMART_LOCKER_ENC_KEY"
HMAC_KEY_ENV_VAR = "SMART_LOCKER_HMAC_KEY"
