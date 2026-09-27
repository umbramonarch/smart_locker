"""
File: test_photo_sync.py
Description: End-to-end coverage of the photo sync pipeline and the dashboard
             share launcher — the sync-layer pieces the API suite never
             touches. process_photo() and the watchdog photo watcher run
             against the shared in-memory StaticPool DB wired up by the
             ``e2e`` harness, so image_path written by the debounce/watchdog
             threads is visible through h.db(). The dashboard launcher tests
             exercise real file writes into a tmp share directory.
Project: smart_locker/tests/e2e
Notes: The photo watcher keeps a module-level Observer singleton — every
       watcher test stops it in a finally so no thread leaks into the next
       test.
"""

import time

from openpyxl import load_workbook

import scripts.update_device as update_script
from smart_locker.database.engine import get_engine
from smart_locker.sync.dashboard_launcher import write_dashboard_launcher
from smart_locker.sync.photo_watcher import (
    process_photo,
    start_photo_watcher,
    stop_photo_watcher,
)
from tests.e2e.helpers import add_device, get_device

# Minimal magic-byte payloads — process_photo only copies bytes, it never
# decodes image content.
JPEG_BYTES = b"\xff\xd8\xff\xe0" + bytes(64)
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + bytes(64)


def _input_photo(tmp_path, name: str, content: bytes = JPEG_BYTES):
    """Create the input folder under tmp_path and drop one photo into it."""
    input_dir = tmp_path / "photos-in"
    input_dir.mkdir(parents=True, exist_ok=True)
    photo = input_dir / name
    photo.write_bytes(content)
    return input_dir, photo


def test_process_photo_updates_every_matching_model(e2e, tmp_path, monkeypatch):
    """A photo named after a model covers ALL units of that model — filename
    stem matching is case-insensitive against the stored model string."""
    h = e2e()
    cam_a = add_device(
        h, name="Cam A", pm_number="PM-300", locker_slot=11, model="CamX"
    )
    cam_b = add_device(
        h, name="Cam B", pm_number="PM-301", locker_slot=12, model="camx"
    )
    other = add_device(
        h, name="Other", pm_number="PM-302", locker_slot=13, model="Other"
    )
    _, photo = _input_photo(tmp_path, "CAMX.jpg")  # stem case differs on purpose
    serve_dir = tmp_path / "serve"

    updated = process_photo(photo, serve_dir, get_engine())

    assert updated == 2
    assert (serve_dir / "CAMX.jpg").read_bytes() == photo.read_bytes()
    assert get_device(h, cam_a).image_path == "images/CAMX.jpg"
    assert get_device(h, cam_b).image_path == "images/CAMX.jpg"
    assert get_device(h, other).image_path is None


def test_auto_photo_command_matches_model_and_mirrors(e2e, tmp_path, monkeypatch):
    """The bench --auto command updates every model match, and the mirror
    catches up on the same call — the sheet shows the matched PM rows."""
    h = e2e()
    first = add_device(h, name="First", pm_number="PM-340", locker_slot=31, model="CamX")
    second = add_device(h, name="Second", pm_number="PM-341", locker_slot=32, model="camx")
    other = add_device(h, name="Other", pm_number="PM-342", locker_slot=33, model="Different")
    images = tmp_path / "smart_locker" / "frontend" / "images"
    images.mkdir(parents=True)
    (images / "CAMX.jpg").write_bytes(JPEG_BYTES)
    mirror_path = tmp_path / "mirror.xlsx"
    monkeypatch.setattr(update_script, "__file__", str(tmp_path / "scripts" / "update_device.py"))
    monkeypatch.setenv("SMART_LOCKER_MIRROR_PATH", str(mirror_path))

    update_script.auto_match_images()

    assert get_device(h, first).image_path == "images/CAMX.jpg"
    assert get_device(h, second).image_path == "images/CAMX.jpg"
    assert get_device(h, other).image_path is None
    workbook = load_workbook(mirror_path, read_only=True)
    try:
        rows = list(workbook.active.values)
    finally:
        workbook.close()
    assert {(row[0], row[4]) for row in rows[1:]} >= {
        ("PM-340", "CamX"), ("PM-341", "camx"),
    }


def test_process_photo_without_matching_model_touches_no_device(
    e2e, tmp_path, monkeypatch
):
    """A filename that matches no model is still copied to the serve dir but
    leaves every device row unchanged."""
    h = e2e()
    device_id = add_device(
        h, name="Cam", pm_number="PM-303", locker_slot=14, model="CamX"
    )
    _, photo = _input_photo(tmp_path, "UnknownModel.png", PNG_BYTES)
    serve_dir = tmp_path / "serve"

    assert process_photo(photo, serve_dir, get_engine()) == 0
    assert (serve_dir / "UnknownModel.png").exists()
    assert get_device(h, device_id).image_path is None


def test_process_photo_missing_file_returns_zero(e2e, tmp_path):
    """A photo that vanished between event and processing is skipped safely."""
    h = e2e()
    device_id = add_device(
        h, name="Cam", pm_number="PM-304", locker_slot=15, model="CamX"
    )

    ghost = tmp_path / "photos-in" / "CamX.jpg"  # never created
    assert process_photo(ghost, tmp_path / "serve", get_engine()) == 0
    assert get_device(h, device_id).image_path is None


def test_photo_watcher_scans_existing_photo_on_start(e2e, tmp_path, monkeypatch):
    """The startup scan applies photos already in the input folder — no
    filesystem event or debounce wait needed (deterministic)."""
    h = e2e()
    device_id = add_device(
        h, name="Scanner Cam", pm_number="PM-310", locker_slot=16, model="ScanCam"
    )
    input_dir, _ = _input_photo(tmp_path, "ScanCam.jpg")
    serve_dir = tmp_path / "watch-serve"

    start_photo_watcher(get_engine(), input_dir, serve_dir)
    try:
        # scan_existing_photos ran synchronously inside start_photo_watcher.
        assert get_device(h, device_id).image_path == "images/ScanCam.jpg"
        assert (serve_dir / "ScanCam.jpg").exists()
    finally:
        stop_photo_watcher()


def test_photo_watcher_applies_dropped_photo(e2e, tmp_path, monkeypatch):
    """Live watch: a file dropped into the input folder fires the watchdog,
    waits out the 2s debounce, then copies and stamps matching devices."""
    h = e2e()
    device_id = add_device(
        h, name="Drop Cam", pm_number="PM-311", locker_slot=17, model="DropCam"
    )
    input_dir = tmp_path / "watch-in"
    input_dir.mkdir()
    serve_dir = tmp_path / "watch-serve"

    start_photo_watcher(get_engine(), input_dir, serve_dir)
    try:
        (input_dir / "DropCam.png").write_bytes(PNG_BYTES)

        # 2s debounce + observer latency — poll rather than sleep a fixed time.
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if get_device(h, device_id).image_path == "images/DropCam.png":
                break
            time.sleep(0.25)
        assert get_device(h, device_id).image_path == "images/DropCam.png"
        assert (serve_dir / "DropCam.png").exists()
    finally:
        stop_photo_watcher()


def test_dashboard_launcher_writes_url_shortcut(tmp_path):
    """A configured share dir gets dashboard.url pointing at .../dashboard."""
    assert write_dashboard_launcher(tmp_path, "http://192.168.1.10:8000") is True
    text = (tmp_path / "dashboard.url").read_text(encoding="utf-8")
    assert text.startswith("[InternetShortcut]")
    assert "URL=http://192.168.1.10:8000/dashboard" in text


def test_dashboard_launcher_url_and_file_target_forms(tmp_path):
    """A public_url already ending in /dashboard is not doubled, and a file
    share path is normalised to a .url sibling in the same folder."""
    assert write_dashboard_launcher(tmp_path, "http://pi.local/dashboard") is True
    text = (tmp_path / "dashboard.url").read_text(encoding="utf-8")
    assert "URL=http://pi.local/dashboard" in text
    assert "dashboard/dashboard" not in text

    # File-shaped share setting -> sibling .url next to it.
    html_target = tmp_path / "locker-share.html"
    assert write_dashboard_launcher(html_target, "http://pi.local") is True
    shortcut = tmp_path / "locker-share.url"
    assert shortcut.exists()
    assert "URL=http://pi.local/dashboard" in shortcut.read_text(encoding="utf-8")


def test_dashboard_launcher_unconfigured_returns_false(tmp_path):
    """Empty settings, a bad URL scheme, or a missing share dir return False
    without raising — the kiosk must stay up when the share is down."""
    assert write_dashboard_launcher(None, "http://pi.local") is False
    assert write_dashboard_launcher("", "http://pi.local") is False
    assert write_dashboard_launcher(tmp_path, None) is False
    assert write_dashboard_launcher(tmp_path, "") is False
    assert write_dashboard_launcher(tmp_path, "ftp://nope") is False
    assert write_dashboard_launcher(tmp_path / "missing", "http://pi.local") is False
    assert not (tmp_path / "dashboard.url").exists()
