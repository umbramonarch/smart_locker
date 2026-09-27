"""
File: dashboard_launcher.py
Description: Write a Windows .url shortcut onto the locker share so colleagues
             can double-click a file and land on the live /dashboard. The live
             page stays GET /dashboard on the Pi.
Project: smart_locker/sync
Notes: Both SMART_LOCKER_PUBLIC_URL and SMART_LOCKER_DASHBOARD_SHARE_PATH must
       be set. A missing share is logged and skipped — the kiosk stays up.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def dashboard_public_url(public_url: str) -> str:
    """Normalise the Pi origin to the live dashboard URL.

    Args:
        public_url: ``SMART_LOCKER_PUBLIC_URL`` value, with or without
            ``/dashboard``.

    Returns:
        URL ending in ``/dashboard`` with no trailing slash.

    Raises:
        ValueError: If the origin is not ``http://`` or ``https://``.
    """
    base = (public_url or "").strip().rstrip("/")
    lowered = base.lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        raise ValueError("PUBLIC_URL must be an http:// or https:// origin")
    if lowered.endswith("/dashboard"):
        return base
    return f"{base}/dashboard"


def _url_shortcut(url: str) -> str:
    """Return a Windows Internet Shortcut for ``url`` (CRLF)."""
    return f"[InternetShortcut]\r\nURL={url}\r\n"


def _url_target(share_path: Path) -> Path:
    """Resolve the .url path from a file or directory setting."""
    suffix = share_path.suffix.lower()
    if suffix in {".html", ".htm", ".url"}:
        return share_path.with_suffix(".url")
    return share_path / "dashboard.url"


def _atomic_write(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write ``text`` via a temp file in the same directory, then replace."""
    path.parent.mkdir(parents=False, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".dash-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write_dashboard_launcher(
    share_path: str | Path | None,
    public_url: str | None,
) -> bool:
    """Write ``dashboard.url`` onto the locker share.

    Args:
        share_path: Directory (or an ``.html`` / ``.url`` file path) on the share.
        public_url: Origin or full dashboard URL of this Pi.

    Returns:
        True if the shortcut was written. False if unconfigured or the share is
        missing — never raises for those cases.
    """
    if share_path is None or not str(share_path).strip():
        return False
    if public_url is None or not str(public_url).strip():
        return False

    url_path = _url_target(Path(str(share_path).strip()))
    parent = url_path.parent
    if not parent.is_dir():
        logger.info(
            "Dashboard launcher skipped — share path is not available: %s",
            parent,
        )
        return False

    try:
        target = dashboard_public_url(str(public_url))
    except ValueError:
        logger.warning(
            "Dashboard launcher skipped — PUBLIC_URL must be http:// or https://."
        )
        return False
    try:
        if url_path.read_text(encoding="utf-8") == _url_shortcut(target):
            # Already points at this dashboard — the per-tick retry is a
            # no-op, so skip the share write and the log line.
            return True
    except OSError:
        pass

    try:
        _atomic_write(url_path, _url_shortcut(target))
    except OSError:
        logger.exception("Dashboard launcher could not be written to %s.", parent)
        return False

    logger.info("Dashboard launcher written: %s → %s", url_path, target)
    return True
