"""
File: test_dashboard_tabs.py
Description: Contract tests for the public dashboard tabs: Inventory (Excel),
             Locker (SQLite), Display (kiosk view-only). Kiosk colors, desktop
             cursor and scroll. Users and transaction logs stay off the public
             page.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_dashboard_tabs.py -v
       Frontend is vanilla HTML/JS/CSS; asserted as text.
"""

from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[1] / "smart_locker" / "frontend"


def _html() -> str:
    return (FRONTEND / "dashboard.html").read_text(encoding="utf-8")


def _js() -> str:
    return (FRONTEND / "dashboard.js").read_text(encoding="utf-8")


def _css() -> str:
    return (FRONTEND / "dashboard.css").read_text(encoding="utf-8")


def _kiosk_js() -> str:
    return (FRONTEND / "app.js").read_text(encoding="utf-8")


class TestDashboardPublicTabs:
    """Public page is three tabs, not devices + transactions + users."""

    def test_three_tabs_present(self):
        """Inventory, Locker, and Display tabs exist."""
        html = _html()
        assert 'id="tab-inventory"' in html
        assert 'id="tab-locker"' in html
        assert 'id="tab-display"' in html
        assert "Inventory" in html
        assert "Locker" in html
        assert "Display" in html

    def test_users_and_logs_not_on_public_page(self):
        """Registered users and transaction history are not public tab content."""
        html = _html()
        assert "Transaction History" not in html
        assert "Registered Users" not in html
        js = _js()
        assert "/api/dashboard/transactions" not in js
        assert "/api/dashboard/users" not in js

    def test_inventory_fetches_excel_endpoint(self):
        """Inventory tab reads the live Excel API, not SQLite devices."""
        js = _js()
        assert "/api/dashboard/inventory" in js
        html = _html()
        assert 'id="inventory-search"' in html
        assert 'id="col-asset-label"' in html

    def test_locker_fetches_sqlite_devices(self):
        """Locker tab still uses the SQLite dashboard devices endpoint."""
        js = _js()
        assert "/api/dashboard/devices" in js
        html = _html()
        assert 'id="devices-table"' in html

    def test_display_polls_kiosk_screen(self):
        """Display tab polls the kiosk screen snapshot. No remote-control posts."""
        js = _js()
        assert "/api/dashboard/display" in js
        assert "/api/kiosk/display" not in js
        html = _html()
        assert 'id="display-screen"' in html
        assert 'id="display-user"' in html


class TestDashboardDesktopTheme:
    """Kiosk tokens, but a normal desktop page (cursor, select, scroll)."""

    def test_uses_kiosk_colors(self):
        """Dashboard reuses charcoal and green from the kiosk."""
        css = _css()
        assert "#181d24" in css
        assert "#009641" in css

    def test_does_not_hide_cursor(self):
        """Desktop viewers keep a real cursor."""
        css = _css()
        assert "cursor: none" not in css
        assert "cursor:none" not in css


class TestKioskDisplayHeartbeat:
    """The kiosk reports its current screen so Display can poll it."""

    def test_kiosk_posts_screen_id(self):
        """navigate() (or equivalent) POSTs the screen id to the Pi."""
        js = _kiosk_js()
        assert "/api/kiosk/display" in js
        assert "reportKioskDisplay" in js or "kiosk/display" in js
