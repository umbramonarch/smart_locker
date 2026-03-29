"""
File: init_db.py
Description: Database initialization script. Creates all tables using
             SQLAlchemy's CREATE IF NOT EXISTS, making it safe to run
             multiple times without data loss.
Project: smart_locker/scripts
Notes: Usage: python -m scripts.init_db
"""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.logging_config import setup_logging
from smart_locker.database.engine import init_db


def main() -> None:
    """Initialize the Smart Locker database by creating all tables.

    Calls SQLAlchemy's ``metadata.create_all()`` which uses CREATE TABLE IF
    NOT EXISTS, so this is safe to run repeatedly without data loss.

    Returns:
        None. Result is printed to stdout.
    """
    setup_logging()
    init_db()
    print("Database initialized successfully.")


if __name__ == "__main__":
    main()
