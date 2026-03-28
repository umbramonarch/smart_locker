"""FastAPI application factory and lifespan management."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import smart_locker.api.app_context as ctx
from smart_locker.api.app_context import AppContext
from smart_locker.api.routes import router

logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start NFC reader + bridge on startup, stop on shutdown."""
    ctx.context = AppContext()
    await ctx.context.start()
    logger.info("Smart Locker API started.")
    yield
    await ctx.context.stop()
    from smart_locker.sync.scheduler import stop_scheduler
    stop_scheduler()
    logger.info("Smart Locker API stopped.")


def create_app() -> FastAPI:
    """Build the FastAPI application."""
    app = FastAPI(title="Smart Locker", lifespan=lifespan)

    # API routes first (so /api/* takes priority over static files)
    app.include_router(router)

    # Serve frontend static files (html=True serves index.html for /)
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

    return app
