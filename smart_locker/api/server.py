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
from smart_locker.api.app_context import AppContext
from smart_locker.api.routes import router

logger = logging.getLogger(__name__)

# Absolute path to the static frontend directory (index.html, style.css, app.js)
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle — start NFC reader on startup, stop on shutdown.

    Creates the shared ``AppContext`` singleton, starts the NFC bridge loop,
    and yields control to uvicorn. On shutdown, stops the NFC reader and
    the APScheduler source-sync scheduler.

    Args:
        app: The FastAPI application instance (provided by the framework).

    Yields:
        None. Control is held by uvicorn between startup and shutdown.
    """
    ctx.context = AppContext()
    await ctx.context.start()
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
