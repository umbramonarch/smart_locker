"""
File: test_kiosk_availability.py
Description: Contract tests for the kiosk locker availability overlay (in/out)
             and PM number on borrow/return cards and the device-detail pane.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_kiosk_availability.py -v
       Frontend is vanilla HTML/JS; these tests assert the served files.
"""

from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[1] / "smart_locker" / "frontend"


def _html() -> str:
    return (FRONTEND / "index.html").read_text(encoding="utf-8")


def _js() -> str:
    return (FRONTEND / "app.js").read_text(encoding="utf-8")


class TestLockerAvailabilityOverlay:
    """Borrow is a locker availability overlay, not the only way to borrow."""

    def test_main_menu_button_is_locker_not_borrow(self):
        """Main menu opens a Locker overlay; the primary label is not Borrow."""
        html = _html()
        assert '<span class="btn-label">Locker</span>' in html
        assert "what's in" in html.lower()
        assert '<span class="btn-label">Borrow</span>' not in html

    def test_locker_screen_title(self):
        """The overlay title describes locker contents, not a borrow-only grid."""
        html = _html()
        assert "What's in the locker" in html
        assert "Borrow Equipment" not in html

    def test_admin_shortcut_renamed(self):
        """Admin shortcut matches the renamed overlay."""
        html = _html()
        assert '<span class="admin-btn-label">Locker</span>' in html
        assert "Borrow Screen" not in html

    def test_overlay_badge_is_in_out(self):
        """Locker overlay badge counts devices in vs out, not personal borrows."""
        js = _js()
        assert "borrow-badge" in js
        assert " in · " in js
        assert 'statusTxt = \'IN\'' in js
        assert 'statusTxt = \'OUT\'' in js
        assert "statusTxt = 'AVAILABLE'" not in js
        assert "statusTxt = 'IN USE'" not in js

    def test_screen_pick_borrow_still_exists(self):
        """Scan-to-borrow is primary; screen pick remains a fallback."""
        js = _js()
        assert "Confirm Borrow" in js


class TestPmNumberOnCardsAndDetail:
    """Borrow/return cards and the detail pane show the PM number."""

    def test_cards_include_pm_number(self):
        """Grid cards render pm_number next to name / type."""
        js = _js()
        assert "card-pm" in js
        assert "dev.pm_number" in js

    def test_detail_pane_has_pm_field(self):
        """Device detail overlay has a PM row filled from the API payload."""
        html = _html()
        assert 'id="detail-pm"' in html
        assert 'id="detail-pm-label"' in html
        js = _js()
        assert "detail-pm" in js
        assert "dev.pm_number" in js

    def test_cards_do_not_interpolate_catalog_html(self):
        """Kiosk cards use textContent, not innerHTML with catalog fields (I4)."""
        js = _js()
        assert "buildDeviceCardEl" in js
        assert "safeKioskImagePath" in js
        assert "<div class=\"card-name\">${dev.name}</div>" not in js
        assert "SLOT_GRID_MAX" in js
        assert "reportKioskDisplay(S.screen)" in js
