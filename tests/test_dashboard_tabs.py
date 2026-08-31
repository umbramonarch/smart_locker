"""
File: test_dashboard_tabs.py
Description: Contract tests for the public dashboard tabs: Inventory (Excel),
             Locker (SQLite), Display (kiosk view-only). Kiosk colors, desktop
             cursor and scroll. Users and transaction logs stay behind the
             5-tap clock overlay. Owner change is Inventory only, and not for
             locker PMs. Locker shows Tagged / No tag. Kiosk Register Device
             uses Replace tag for an already-bound sticker.
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

    def test_users_and_logs_not_on_public_tabs(self):
        """Registered users and transaction history are not public tab content."""
        html = _html()
        assert 'id="tab-inventory"' in html
        assert 'id="tab-users"' not in html
        assert 'id="tab-transactions"' not in html
        public = html.split('id="admin-overlay"', 1)[0]
        assert "Transaction History" not in public
        assert "Registered Users" not in public

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
        js = _js()
        assert 'id="display-screen"' in html
        assert 'id="display-user"' in html
        assert "data.occupied" in js
        assert "In use" in js
        fetch = js.split("async function fetchDisplay", 1)[1].split(
            "async function ", 1
        )[0]
        assert "user_name" not in fetch


class TestDashboardFiveTapAdmin:
    """Same 5-tap clock as the kiosk; overlay has users, logs, unbind / arm-bind."""

    def test_clock_and_overlay_present(self):
        """Header clock is the 5-tap target; overlay is hidden until then."""
        html = _html()
        js = _js()
        assert 'id="dash-clock"' in html
        assert 'id="admin-overlay"' in html
        assert 'id="admin-secret-dialog"' in html
        assert 'id="admin-secret-input"' in html
        assert 'id="admin-secret-confirm"' in html
        assert "ADMIN_TAP_COUNT" in js or "adminTaps" in js
        assert "3000" in js
        assert "openAdminOverlay" in js or "checkAdminTapSequence" in js
        assert "ensureDashboardAdminSecret" in js

    def test_overlay_has_users_logs_and_tag_actions(self):
        """5-tap overlay lists users, last transactions, and NFC unbind / arm-bind."""
        html = _html()
        js = _js()
        assert 'id="admin-users-tbody"' in html
        assert 'id="admin-tx-tbody"' in html
        assert 'id="admin-tags-tbody"' in html
        assert "Registered Users" in html
        assert "Transaction History" in html
        assert "/api/dashboard/users" in js
        assert "/api/dashboard/transactions" in js
        assert "/api/dashboard/bind-tag" in js
        assert "/api/dashboard/unbind-tag" in js
        assert "X-Smart-Locker-Admin" in js
        assert "Replace tag" in js
        assert "/api/admin/session" not in js

    def test_users_and_logs_not_fetched_on_public_load(self):
        """Public DOMContentLoaded must not pull users/logs; overlay open does."""
        js = _js()
        # The overlay fetchers exist; the public init block must not call them
        # until the 5-tap overlay opens.
        assert "fetchAdminTables" in js or "fetchUsers" in js
        init = js.split("DOMContentLoaded", 1)[-1]
        assert "/api/dashboard/users" not in init
        assert "/api/dashboard/transactions" not in init


class TestDashboardHasTag:
    """Locker tab shows Tagged / No tag; never the HMAC digest."""

    def test_locker_has_tag_column(self):
        """Public Locker table has a Tag column driven by has_tag."""
        html = _html()
        js = _js()
        assert "data-sort=\"has_tag\"" in html or "data-sort='has_tag'" in html
        assert "has_tag" in js
        assert "Tagged" in js
        assert "No tag" in js
        assert "tag_hmac" not in js


class TestDashboardOwnerEdit:
    """Owner change is Inventory only; locker devices are not editable there."""

    def test_inventory_has_owner_controls_locker_does_not(self):
        """Inventory can open the owner dialog; Locker rows stay plain text."""
        html = _html()
        js = _js()
        assert 'id="owner-dialog"' in html
        assert 'id="owner-confirm"' in html
        assert 'id="owner-input"' in html
        assert 'id="owner-names"' in html
        assert "/api/dashboard/owner" in js
        assert "/api/dashboard/owners" in js
        assert "in_locker" in js
        assert "ownerCell(d.pm_number, d.location)" in js
        assert "ownerCell(d.pm_number, d.borrower_name" not in js

    def test_confirm_before_write(self):
        """Owner change requires an explicit Confirm control."""
        html = _html()
        assert 'id="owner-confirm"' in html
        assert "Confirm" in html

    def test_location_click_does_not_prompt_secret(self):
        """Opening the owner dialog must not ask for the admin secret."""
        js = _js()
        fn = js.split("function openOwnerDialog(", 1)[1].split(
            "function ", 1
        )[0]
        assert "ensureDashboardAdminSecret" not in fn
        assert "window.prompt" not in fn

    def test_window_prompt_not_used(self):
        """Dashboard.js must not use browser window.prompt for any flow."""
        js = _js()
        assert "window.prompt" not in js

    def test_stored_secret_is_revalidated_before_overlay(self):
        """A cached secret is checked, and cleared when the API rejects it."""
        js = _js()
        fn = js.split("async function ensureDashboardAdminSecret(", 1)[1].split(
            "\n/**", 1
        )[0]
        assert "validateAdminSecret(stored)" in fn
        assert "sessionStorage.removeItem(ADMIN_SECRET_KEY)" in fn
        assert "openAdminSecretDialog(" in fn

    def test_validation_separates_rejection_from_outage(self):
        """Only a 401/403 means wrong secret; other failures say try again."""
        js = _js()
        validate = js.split("async function validateAdminSecret(", 1)[1].split(
            "\n/**", 1
        )[0]
        assert "res.status === 401" in validate
        assert "'unavailable'" in validate
        submit = js.split("async function submitAdminSecret(", 1)[1].split(
            "\n/**", 1
        )[0]
        assert "result === 'invalid'" in submit
        assert "ADMIN_SECRET_UNAVAILABLE_MSG" in submit


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


class TestDashboardPollAndAdminFetch:
    """I3 / I16: skip hidden polls, debounce search, do not reread Inventory on 5-tap."""

    def test_poll_skips_when_document_hidden(self):
        """30s Inventory poll must not run while the tab is in the background."""
        js = _js()
        assert "document.hidden" in js
        assert "tablesInFlight" in js
        assert "Promise.all" in js
        assert "SEARCH_DEBOUNCE_MS" in js
        assert "visibilitychange" in js

    def test_fetch_admin_does_not_copy_inventory_excel(self):
        """Opening 5-tap must not await fetchTables / GET inventory."""
        js = _js()
        fn = js.split("async function fetchAdminTables", 1)[1].split(
            "async function ", 1
        )[0]
        assert "fetchTables()" not in fn
        assert "/api/dashboard/inventory" not in fn
        assert "/api/dashboard/users" in fn
        assert "/api/dashboard/transactions" in fn
        assert "visibilitychange" in js
        assert "sessionStorage.removeItem" in js
        assert "ADMIN_SECRET_KEY" in js
        unbind = js.split("async function unbindTag", 1)[1].split(
            "async function ", 1
        )[0]
        assert "fetchAdminTables" in unbind


class TestDashboardEsc:
    """esc() must be safe for HTML attributes (data-pm, data-owner)."""

    def test_esc_encodes_quotes(self):
        """Attribute interpolation encodes quotes, not only &<>."""
        js = _js()
        fn = js.split("function esc(str)", 1)[1].split("function ", 1)[0]
        assert "&quot;" in fn
        assert "&#39;" in fn
        assert "&amp;" in fn
        assert "&lt;" in fn
        assert "&gt;" in fn
        assert "data-pm=\"${esc(pm)}\"" in js or 'data-pm="${esc(pm)}"' in js
        assert "data-owner=\"${esc(owner" in js or 'data-owner="${esc(owner' in js
