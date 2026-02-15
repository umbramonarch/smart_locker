"""Database initialization script.

Creates all tables. Safe to run multiple times (CREATE IF NOT EXISTS).

Usage:
    python -m scripts.init_db
"""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.logging_config import setup_logging
from smart_locker.database.engine import init_db


def main() -> None:
    setup_logging()
    init_db()
    print("Database initialized successfully.")


if __name__ == "__main__":
    main()
