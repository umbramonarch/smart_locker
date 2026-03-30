"""
File: photo_watcher.py
Description: Watches a designated input folder for device photos and
             automatically assigns them to matching devices by model number
             (Typbezeichnung). When a photo is added or modified, the watcher
             copies it to the frontend images directory and updates the
             ``image_path`` column on every device whose model matches the
             filename stem. Because many locker devices share the same model,
             a single photo covers all units of that type.
Project: smart_locker/sync
Notes: The filename must match the device model exactly (case-insensitive,
       without extension). For example, "87V.jpg" matches all devices with
       model "87V". Supported extensions: .jpg, .jpeg, .png, .webp, .gif.
       Requires watchdog. Disabled when SMART_LOCKER_PHOTO_INPUT_PATH is empty.
"""

import logging
import shutil
import threading
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)

# File extensions recognised as device photos
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

# Debounce window in seconds — a single save may trigger several filesystem
# events; this collapses them into one import per file.
_DEBOUNCE_SECONDS = 2.0

# Module-level singleton — set by start_photo_watcher(), cleared by stop()
_observer: Observer | None = None


def _is_image(path: Path) -> bool:
    """Check whether a file path has a recognised image extension.

    Args:
        path: File path to check.

    Returns:
        True if the suffix (lowercased) is in the supported set.
    """
    return path.suffix.lower() in _IMAGE_EXTENSIONS


def process_photo(photo_path: Path, serve_dir: Path, engine) -> int:
    """Copy a photo to the serve directory and update matching devices.

    Extracts the model name from the filename stem, copies the image to
    ``serve_dir``, then sets ``image_path`` on every device whose model
    matches (case-insensitive).

    Args:
        photo_path: Absolute path to the source photo file.
        serve_dir: Destination directory (``frontend/images/``).
        engine: SQLAlchemy engine for database access.

    Returns:
        Number of devices updated.
    """
    if not photo_path.exists():
        logger.warning("Photo file not found: %s", photo_path)
        return 0

    # The filename stem IS the model string (e.g. "87V" from "87V.jpg")
    model = photo_path.stem

    # Copy photo into the frontend images directory, preserving extension
    serve_dir.mkdir(parents=True, exist_ok=True)
    dest = serve_dir / photo_path.name
    try:
        shutil.copy2(photo_path, dest)
    except PermissionError:
        logger.warning("Cannot copy photo %s — source file locked.", photo_path)
        return 0

    # Relative path used by the frontend (from the images/ directory root)
    relative_path = f"images/{photo_path.name}"

    # Update all devices with a matching model
    from smart_locker.database.engine import get_session
    from smart_locker.database.repositories import DeviceRepository

    updated = 0
    with get_session() as session:
        devices = DeviceRepository.find_by_model(session, model)
        if not devices:
            logger.info(
                "Photo '%s' does not match any device model — file copied "
                "but no devices updated.", photo_path.name,
            )
            return 0

        for device in devices:
            if device.image_path != relative_path:
                device.image_path = relative_path
                updated += 1
        if updated:
            session.flush()

    if updated:
        logger.info(
            "Photo '%s' applied to %d device(s) with model '%s'.",
            photo_path.name, updated, model,
        )

        # Trigger Excel export so the image_path change is reflected
        try:
            from smart_locker.sync.excel_sync import export_to_excel
            export_to_excel(engine)
        except Exception as e:
            logger.warning("Excel sync after photo import failed: %s", e)

    return updated


def scan_existing_photos(input_dir: Path, serve_dir: Path, engine) -> int:
    """Process all existing photos in the input folder on startup.

    Iterates over every image file in ``input_dir`` and runs
    ``process_photo`` for each, so the database is up-to-date before
    the first user interaction.

    Args:
        input_dir: Photo input folder to scan.
        serve_dir: Destination directory (``frontend/images/``).
        engine: SQLAlchemy engine for database access.

    Returns:
        Total number of devices updated across all photos.
    """
    total = 0
    if not input_dir.exists():
        logger.warning("Photo input directory does not exist: %s", input_dir)
        return total

    for photo in sorted(input_dir.iterdir()):
        if photo.is_file() and _is_image(photo):
            total += process_photo(photo, serve_dir, engine)
    return total


class _PhotoHandler(FileSystemEventHandler):
    """Watchdog handler that processes new or modified photos.

    Monitors the input folder for image files. Uses per-file debounce
    timers so that bursts of filesystem events from a single save
    produce only one ``process_photo`` call.

    Attributes:
        _engine: SQLAlchemy Engine for database operations.
        _serve_dir: Frontend images directory to copy photos into.
        _timers: Dict of per-file debounce timers keyed by lowercase filename.
        _lock: Thread lock protecting the timers dict.
    """

    def __init__(self, engine, serve_dir: Path) -> None:
        """Initialize the photo handler.

        Args:
            engine: SQLAlchemy Engine for database operations.
            serve_dir: Destination directory (``frontend/images/``).
        """
        super().__init__()
        self._engine = engine
        self._serve_dir = serve_dir
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def on_created(self, event) -> None:
        """Handle file creation events for new photos.

        Args:
            event: Watchdog file system event.

        Returns:
            None.
        """
        self._handle(event)

    def on_modified(self, event) -> None:
        """Handle file modification events for updated photos.

        Args:
            event: Watchdog file system event.

        Returns:
            None.
        """
        self._handle(event)

    def _handle(self, event) -> None:
        """Filter to image files and schedule a debounced import.

        Args:
            event: Watchdog file system event.

        Returns:
            None.
        """
        if event.is_directory:
            return

        path = Path(event.src_path)
        if not _is_image(path):
            return

        # Skip temporary files that editors/OS create during saves
        if path.name.startswith(".") or path.name.startswith("~"):
            return

        self._schedule_debounced(path)

    def _schedule_debounced(self, photo_path: Path) -> None:
        """Reset the per-file debounce timer and schedule processing.

        Args:
            photo_path: Path to the photo that triggered the event.

        Returns:
            None.
        """
        key = photo_path.name.lower()
        with self._lock:
            existing = self._timers.get(key)
            if existing is not None:
                existing.cancel()
            timer = threading.Timer(
                _DEBOUNCE_SECONDS,
                self._process,
                args=[photo_path],
            )
            timer.daemon = True
            timer.start()
            self._timers[key] = timer

    def _process(self, photo_path: Path) -> None:
        """Run process_photo (called by the debounce timer).

        Args:
            photo_path: Path to the photo to process.

        Returns:
            None.
        """
        process_photo(photo_path, self._serve_dir, self._engine)


def start_photo_watcher(engine, input_dir: str | Path, serve_dir: str | Path) -> None:
    """Start the watchdog observer on the photo input folder.

    Performs an initial scan of existing photos, then begins monitoring
    for new or changed image files.

    Args:
        engine: SQLAlchemy engine.
        input_dir: Path to the photo input folder.
        serve_dir: Path to the frontend images directory.

    Returns:
        None.
    """
    global _observer

    input_path = Path(input_dir).resolve()
    serve_path = Path(serve_dir).resolve()

    if not input_path.exists():
        logger.warning(
            "Photo input directory %s does not exist — creating it.",
            input_path,
        )
        input_path.mkdir(parents=True, exist_ok=True)

    # Initial scan — apply existing photos before starting the watcher
    count = scan_existing_photos(input_path, serve_path, engine)
    if count:
        logger.info("Initial photo scan: %d device(s) updated.", count)

    # Start watcher
    handler = _PhotoHandler(engine, serve_path)
    _observer = Observer()
    _observer.schedule(handler, str(input_path), recursive=False)
    _observer.daemon = True
    _observer.start()
    logger.info("Photo watcher started: monitoring %s", input_path)


def stop_photo_watcher() -> None:
    """Stop the photo watcher gracefully.

    Stops the watchdog ``Observer`` and clears the module-level singleton.

    Returns:
        None.
    """
    global _observer
    if _observer is not None:
        _observer.stop()
        _observer.join(timeout=5)
        _observer = None
        logger.info("Photo watcher stopped.")
