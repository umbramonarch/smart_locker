"""
File: app.py
Description: Web application entry point for the Smart Locker system. Starts the
             FastAPI + uvicorn server serving the kiosk UI with NFC bridge.
Project: smart_locker
Notes: Run via 'python -m smart_locker.app' (web server on port 8000).
"""

import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.logging_config import setup_logging
from smart_locker.database.engine import init_db

logger = logging.getLogger(__name__)


def run_server() -> None:
    """Start the FastAPI and Uvicorn web server.

    Initializes logging and the database, then creates and runs the FastAPI
    application. Lifecycle-owned synchronization starts after the NFC API
    context is ready; its startup import runs asynchronously.
    """
    import uvicorn

    from config.settings import API_HOST, API_PORT
    from smart_locker.api.server import create_app

    setup_logging()
    init_db()

    app = create_app()
    logger.info("Starting Smart Locker web server on %s:%d", API_HOST, API_PORT)
    uvicorn.run(app, host=API_HOST, port=API_PORT)


if __name__ == "__main__":
    run_server()
