"""
File: test_photo_watcher.py
Description: Tests for the photo watcher module — verifies model-based matching,
             file copying to the serve directory, database image_path updates,
             multi-device model matching, and unrecognised model handling.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_photo_watcher.py -v
       Uses temporary directories to isolate filesystem side-effects.
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from smart_locker.database.repositories import DeviceRepository
from smart_locker.sync.photo_watcher import (
    _is_image,
    process_photo,
    scan_existing_photos,
)


def _create_test_image(path: Path) -> None:
    """Write a minimal 1x1 PNG image to disk for testing.

    Args:
        path: Destination file path.
    """
    # Write a tiny valid PNG manually (1x1 white pixel) to avoid
    # pulling in Pillow as a test dependency.  The photo watcher only
    # copies files — it doesn't parse image contents.
    path.parent.mkdir(parents=True, exist_ok=True)
    # Minimal valid PNG: 8-byte signature + IHDR + IDAT + IEND
    import struct, zlib
    signature = b"\x89PNG\r\n\x1a\n"
    # IHDR: width=1, height=1, bit_depth=8, color_type=2 (RGB)
    ihdr_data = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF
    ihdr = struct.pack(">I", 13) + b"IHDR" + ihdr_data + struct.pack(">I", ihdr_crc)
    # IDAT: single row, filter byte 0, then 3 bytes RGB (white)
    raw = b"\x00\xff\xff\xff"
    compressed = zlib.compress(raw)
    idat_crc = zlib.crc32(b"IDAT" + compressed) & 0xFFFFFFFF
    idat = struct.pack(">I", len(compressed)) + b"IDAT" + compressed + struct.pack(">I", idat_crc)
    # IEND
    iend_crc = zlib.crc32(b"IEND") & 0xFFFFFFFF
    iend = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", iend_crc)
    path.write_bytes(signature + ihdr + idat + iend)


class TestIsImage:
    """Tests for the _is_image helper function."""

    def test_jpg(self):
        """JPG extension is recognised."""
        assert _is_image(Path("photo.jpg")) is True

    def test_jpeg(self):
        """JPEG extension is recognised."""
        assert _is_image(Path("photo.JPEG")) is True

    def test_png(self):
        """PNG extension is recognised."""
        assert _is_image(Path("photo.png")) is True

    def test_webp(self):
        """WEBP extension is recognised."""
        assert _is_image(Path("photo.webp")) is True

    def test_non_image(self):
        """Non-image extensions are rejected."""
        assert _is_image(Path("data.xlsx")) is False
        assert _is_image(Path("readme.txt")) is False


class TestProcessPhoto:
    """Tests for the core process_photo function."""

    def test_matching_model_updates_device(self, db_session):
        """Photo with filename matching a device model updates image_path."""
        DeviceRepository.create(
            db_session, name="Fluke 87V", device_type="Multimeter",
            pm_number="PM-001", model="87V",
        )
        db_session.commit()

        with tempfile.TemporaryDirectory() as tmpdir:
            input_dir = Path(tmpdir) / "input"
            serve_dir = Path(tmpdir) / "serve"
            photo = input_dir / "87V.jpg"
            _create_test_image(photo)

            from smart_locker.database.engine import get_engine
            count = process_photo(photo, serve_dir, get_engine())

            assert count == 1
            # Photo copied to serve directory
            assert (serve_dir / "87V.jpg").exists()
            # Device image_path updated
            device = DeviceRepository.find_by_pm(db_session, "PM-001")
            assert device.image_path == "images/87V.jpg"

    def test_multiple_devices_same_model(self, db_session):
        """One photo updates all devices sharing the same model."""
        for i in range(3):
            DeviceRepository.create(
                db_session, name=f"Unit {i}", device_type="Multimeter",
                pm_number=f"PM-{i:03d}", model="87V",
            )
        db_session.commit()

        with tempfile.TemporaryDirectory() as tmpdir:
            photo = Path(tmpdir) / "87V.png"
            serve_dir = Path(tmpdir) / "serve"
            _create_test_image(photo)

            from smart_locker.database.engine import get_engine
            count = process_photo(photo, serve_dir, get_engine())

            assert count == 3

    def test_no_matching_model(self, db_session):
        """Photo with unrecognised model name updates zero devices."""
        DeviceRepository.create(
            db_session, name="Fluke 87V", device_type="Multimeter",
            pm_number="PM-001", model="87V",
        )
        db_session.commit()

        with tempfile.TemporaryDirectory() as tmpdir:
            photo = Path(tmpdir) / "UNKNOWN_MODEL.jpg"
            serve_dir = Path(tmpdir) / "serve"
            _create_test_image(photo)

            from smart_locker.database.engine import get_engine
            count = process_photo(photo, serve_dir, get_engine())

            assert count == 0
            # File is still copied even if no devices match
            assert (serve_dir / "UNKNOWN_MODEL.jpg").exists()

    def test_case_insensitive_model_match(self, db_session):
        """Model matching is case-insensitive."""
        DeviceRepository.create(
            db_session, name="Fluke 87V", device_type="Multimeter",
            pm_number="PM-001", model="87V",
        )
        db_session.commit()

        with tempfile.TemporaryDirectory() as tmpdir:
            # Filename with different case
            photo = Path(tmpdir) / "87v.jpg"
            serve_dir = Path(tmpdir) / "serve"
            _create_test_image(photo)

            from smart_locker.database.engine import get_engine
            count = process_photo(photo, serve_dir, get_engine())

            assert count == 1

    def test_nonexistent_photo_returns_zero(self, db_session):
        """Missing photo file returns 0 without crashing."""
        count = process_photo(Path("/nonexistent/photo.jpg"), Path("/tmp"), MagicMock())
        assert count == 0


class TestScanExistingPhotos:
    """Tests for the initial scan that processes all photos in the input folder."""

    def test_scan_processes_all_images(self, db_session):
        """Startup scan processes every image file in the input directory."""
        DeviceRepository.create(
            db_session, name="Fluke 87V", device_type="Multimeter",
            pm_number="PM-001", model="87V",
        )
        DeviceRepository.create(
            db_session, name="Rigol DS1054Z", device_type="Oscilloscope",
            pm_number="PM-002", model="DS1054Z",
        )
        db_session.commit()

        with tempfile.TemporaryDirectory() as tmpdir:
            input_dir = Path(tmpdir) / "input"
            serve_dir = Path(tmpdir) / "serve"

            _create_test_image(input_dir / "87V.jpg")
            _create_test_image(input_dir / "DS1054Z.png")
            # Non-image file should be skipped
            (input_dir / "notes.txt").write_text("not an image")

            from smart_locker.database.engine import get_engine
            total = scan_existing_photos(input_dir, serve_dir, get_engine())

            assert total == 2

    def test_scan_nonexistent_dir_returns_zero(self):
        """Scanning a nonexistent directory returns 0 without crashing."""
        total = scan_existing_photos(
            Path("/nonexistent/dir"), Path("/tmp"), MagicMock()
        )
        assert total == 0
