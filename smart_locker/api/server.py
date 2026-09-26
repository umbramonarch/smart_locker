"""
File: server.py
Description: FastAPI application factory and lifespan management. Builds the
             FastAPI app, mounts API routes and static frontend files, and
             manages NFC reader startup/shutdown via the async lifespan context.
Project: smart_locker/api
Notes: The frontend is served from smart_locker/frontend/ as static files with
       index.html at the root. API routes take priority over static file paths.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import smart_locker.api.app_context as ctx
from config.settings import (
    DASHBOARD_SHARE_PATH,
    PHOTO_INPUT_PATH,
    PHOTO_SERVE_DIR,
    PUBLIC_URL,
    SOURCE_SYNC_INTERVAL_HOURS,
)
from smart_locker.api.app_context import AppContext
from smart_locker.database.engine import get_engine
from smart_locker.api.routes import router
from smart_locker.sync.workbook_adapter import configured_workbook

logger = logging.getLogger(__name__)

# Absolute path to the static frontend directory (index.html, style.css, app.js)
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


def _start_background_sync() -> None:
    """Start non-critical source and photo synchronization services.

    Scheduler setup queues the potentially slow workbook import in its own
    worker thread. Therefore this lifecycle work finishes before Uvicorn begins
    serving without waiting for that import.
    """
    workbook = configured_workbook()
    if workbook is not None:
        try:
            from smart_locker.sync.scheduler import start_scheduler

            start_scheduler(
                get_engine(), workbook.path, SOURCE_SYNC_INTERVAL_HOURS
            )
        except Exception:
            logger.exception(
                "Source-import scheduler failed to start — continuing without it. "
                "The kiosk stays up; run the admin 'Sync source' once the share is back."
            )

    if PHOTO_INPUT_PATH:
        try:
            from smart_locker.sync.photo_watcher import start_photo_watcher

            start_photo_watcher(get_engine(), PHOTO_INPUT_PATH, PHOTO_SERVE_DIR)
        except Exception:
            logger.exception(
                "Photo watcher failed to start — continuing without it. "
                "Photos can be applied later via 'python -m scripts.update_device --auto'."
            )

    try:
        from smart_locker.sync.dashboard_launcher import write_dashboard_launcher

        write_dashboard_launcher(DASHBOARD_SHARE_PATH, PUBLIC_URL)
    except Exception:
        logger.exception(
            "Dashboard launcher failed to write — continuing without it. "
            "Set SMART_LOCKER_PUBLIC_URL and SMART_LOCKER_DASHBOARD_SHARE_PATH "
            "once the share is back."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle — start NFC reader on startup, stop on shutdown.

    Creates the shared ``AppContext`` singleton, starts the NFC bridge loop and
    sync services, then yields control to uvicorn. The source scheduler queues
    its startup workbook import in the background, so health is available while
    that import runs. On shutdown, stops the NFC reader and scheduler.

    Args:
        app: The FastAPI application instance (provided by the framework).

    Yields:
        None. Control is held by uvicorn between startup and shutdown.
    """
    ctx.context = AppContext()
    await ctx.context.start()
    _start_background_sync()
    logger.info("Smart Locker API started.")
    yield
    await ctx.context.stop()
    from smart_locker.sync.scheduler import stop_scheduler
    stop_scheduler()
    logger.info("Smart Locker API stopped.")


def create_app() -> FastAPI:
    """Build and configure the FastAPI application.

    Registers the API router (``/api/*``) and mounts the static frontend
    directory at ``/`` with ``html=True`` so that ``index.html`` is served
    at the root path.

    Returns:
        FastAPI: The configured application instance ready for uvicorn.
    """
    app = FastAPI(title="Smart Locker", lifespan=lifespan)

    # API routes first (so /api/* takes priority over static files)
    app.include_router(router)

    # Serve frontend static files (html=True serves index.html for /)
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

    return app
