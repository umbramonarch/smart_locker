"""
File: test_register_device.py
Description: Contract tests for admin Register Device: add by PM from Excel,
             pick a free slot, then tap NFC. Existing locker rows keep Bind
             / Replace tag / Unbind / change-slot.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_register_device.py -v
       Frontend is vanilla HTML/JS; asserted as text.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "smart_locker" / "frontend"


def _html() -> str:
    return (FRONTEND / "index.html").read_text(encoding="utf-8")


def _js() -> str:
    return (FRONTEND / "app.js").read_text(encoding="utf-8")


class TestRegisterDeviceAddFromExcel:
    """Register Device is PM + slot + NFC, not bind-only on existing rows."""

    def test_add_from_excel_controls_present(self):
        """Overlay has an add step with PM input and a slot grid."""
        html = _html()
        assert 'id="bind-add-open"' in html
        assert 'id="bind-step-add"' in html
        assert 'id="bind-pm-input"' in html
        assert 'id="bind-slot-grid"' in html
        assert 'id="bind-add-submit"' in html

    def test_change_slot_step_present(self):
        """Existing locker rows can pick a different free slot."""
        html = _html()
        assert 'id="bind-step-slot"' in html
        assert 'id="bind-slot-submit"' in html

    def test_admin_button_describes_pm_slot_nfc(self):
        """Admin tile copy is no longer bind-only."""
        html = _html()
        assert '<span class="admin-btn-label">Register Device</span>' in html
        desc = html[html.index('id="admin-register-device"'):html.index('id="admin-export-excel"')]
        assert "PM" in desc and "slot" in desc.lower()

    def test_js_posts_register_then_waits_for_sticker(self):
        """Add flow POSTs /api/admin/devices/register then shows the tap step."""
        js = _js()
        assert "/api/admin/devices/register" in js
        assert "bind-step-tap" in js
        assert "bind-pm-input" in js

    def test_js_posts_slot_change(self):
        """Change-slot flow POSTs /api/admin/devices/{id}/slot."""
        js = _js()
        assert "/slot" in js
        assert "bind-step-slot" in js

    def test_demo_mode_does_not_post_register(self):
        """?demo must not create a locker row against a missing workbook."""
        js = _js()
        assert "submitRegisterDevice" in js
        assert "Add from Excel is Pi only" in js

    def test_js_loads_asset_label_from_config(self):
        """Kiosk copy for the join-key label comes from GET /api/config."""
        js = _js()
        assert "/api/config" in js
        assert "asset_label" in js
        html = _html()
        assert 'id="detail-pm-label"' in html
        assert 'id="bind-pm-input"' in html

    def test_dashboard_loads_asset_label_from_config(self):
        """Dashboard column title comes from GET /api/config."""
        js = (FRONTEND / "dashboard.js").read_text(encoding="utf-8")
        html = (FRONTEND / "dashboard.html").read_text(encoding="utf-8")
        assert "/api/config" in js
        assert 'id="col-asset-label"' in html


class TestReplaceTagCopy:
    """Same device, new sticker: the bind button is labelled Replace tag."""

    def test_tagged_row_says_replace_tag(self):
        """Register Device uses Replace tag when the row already has a sticker."""
        js = _js()
        assert "Replace tag" in js
        assert "bindBtn.textContent" in js


class TestCatalogLockerPickList:
    """Add from Excel offers a tap-to-pick list of unregistered locker rows."""

    def test_catalog_list_markup_present(self):
        """The add step carries the pick list and its note line."""
        html = _html()
        assert 'id="bind-catalog-list"' in html
        assert 'id="bind-catalog-note"' in html

    def test_js_fetches_catalog_locker(self):
        """app.js fetches the catalog pick list on the add step."""
        js = _js()
        assert "/api/admin/devices/catalog-locker" in js
        assert "populateCatalogList" in js
