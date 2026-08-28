"""
File: test_dashboard_launcher.py
Description: Tests for the locker-share dashboard launcher. The Pi writes a
             small HTML redirect and a Windows .url shortcut so colleagues
             can double-click a file on the share and land on the live
             /dashboard. Missing config or a down share must not raise.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_dashboard_launcher.py -v
       No NFC hardware. Uses a temp directory, not a real CIFS mount.
"""

from pathlib import Path

from smart_locker.sync.dashboard_launcher import (
    dashboard_public_url,
    write_dashboard_launcher,
)


class TestDashboardPublicUrl:
    """PUBLIC_URL is normalised to the live /dashboard page."""

    def test_appends_dashboard(self):
        """Bare Pi origin becomes the dashboard URL."""
        assert dashboard_public_url("http://192.168.1.10:8000") == (
            "http://192.168.1.10:8000/dashboard"
        )

    def test_keeps_existing_dashboard_path(self):
        """A URL that already ends in /dashboard is not doubled."""
        assert dashboard_public_url("http://192.168.1.10:8000/dashboard") == (
            "http://192.168.1.10:8000/dashboard"
        )

    def test_strips_trailing_slash(self):
        """Trailing slash on the origin is not kept."""
        assert dashboard_public_url("http://192.168.1.10:8000/") == (
            "http://192.168.1.10:8000/dashboard"
        )

    def test_rejects_scheme_less(self):
        """Host:port without http(s) must not be treated as a web origin."""
        import pytest

        with pytest.raises(ValueError, match="http"):
            dashboard_public_url("192.168.1.10:8000")


class TestWriteDashboardLauncher:
    """HTML + .url are written next to each other on the share path."""

    def test_writes_html_and_url(self, tmp_path):
        """A directory path gets dashboard.html and dashboard.url."""
        share = tmp_path / "locker"
        share.mkdir()
        origin = "http://192.168.1.10:8000"

        written = write_dashboard_launcher(share, origin)

        html_path = share / "dashboard.html"
        url_path = share / "dashboard.url"
        assert written is True
        assert html_path.is_file()
        assert url_path.is_file()
        html = html_path.read_text(encoding="utf-8")
        shortcut = url_path.read_text(encoding="utf-8")
        target = "http://192.168.1.10:8000/dashboard"
        assert target in html
        assert "file://" not in html.split("href", 1)[-1][:80]
        assert "[InternetShortcut]" in shortcut
        assert f"URL={target}" in shortcut.replace("\r\n", "\n")

    def test_quotes_in_url_are_html_escaped(self, tmp_path):
        """Attribute interpolation cannot break on quotes in PUBLIC_URL (M5)."""
        share = tmp_path / "locker"
        share.mkdir()
        origin = 'http://192.168.1.10:8000/"onclick=alert(1)'
        write_dashboard_launcher(share, origin)
        html = (share / "dashboard.html").read_text(encoding="utf-8")
        assert 'href="http://192.168.1.10:8000/"onclick' not in html
        assert "&quot;" in html

    def test_unconfigured_is_noop(self, tmp_path):
        """Empty path or URL does not create files and does not raise."""
        share = tmp_path / "locker"
        share.mkdir()
        assert write_dashboard_launcher("", "http://192.168.1.10:8000") is False
        assert write_dashboard_launcher(share, "") is False
        assert write_dashboard_launcher(None, None) is False
        assert list(share.iterdir()) == []

    def test_missing_share_does_not_raise(self, tmp_path):
        """Share down (missing directory) is skipped, not an exception."""
        missing = tmp_path / "not-mounted" / "locker"
        assert write_dashboard_launcher(missing, "http://192.168.1.10:8000") is False
        assert not missing.exists()

    def test_scheme_less_url_is_rejected(self, tmp_path):
        """A host:port PUBLIC_URL must not become a file:// shortcut target."""
        share = tmp_path / "locker"
        share.mkdir()
        assert write_dashboard_launcher(share, "192.168.1.10:8000") is False
        assert list(share.iterdir()) == []

    def test_retries_when_share_becomes_available(self, tmp_path):
        """Share down at first call is skipped; a later call writes once the path exists."""
        share = tmp_path / "locker"
        origin = "http://192.168.1.10:8000"
        assert write_dashboard_launcher(share, origin) is False
        assert not share.exists()
        share.mkdir()
        assert write_dashboard_launcher(share, origin) is True
        assert (share / "dashboard.html").is_file()
        assert (share / "dashboard.url").is_file()
