"""
File: test_register_device.py
Description: Contract tests for admin Register Device: add by PM from Excel,
             pick a free slot, then tap NFC. Existing locker rows keep Bind
             / Replace tag / Unbind / change-slot.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_register_device.py -v
       Frontend is vanilla HTML/JS; asserted as text, plus a node --check
       syntax gate (skipped when node is absent). Static text cannot prove
       runtime behavior (see the MR report for missing coverage).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "smart_locker" / "frontend"


def _html() -> str:
    return (FRONTEND / "index.html").read_text(encoding="utf-8")


def _js() -> str:
    return (FRONTEND / "app.js").read_text(encoding="utf-8")


def _css() -> str:
    return (FRONTEND / "style.css").read_text(encoding="utf-8")


def _fn_body(js: str, start: str, end: str) -> str:
    """Slice one JS function body out of app.js source text."""
    return js.split(start, 1)[1].split(end, 1)[0]


class TestFrontendSyntaxGate:
    """app.js must at least parse; without a JS runner this is the ceiling."""

    def test_app_js_parses_with_node(self):
        """node --check passes on app.js (skipped when node is absent)."""
        node = shutil.which("node")
        if node is None:
            pytest.skip("node not installed")
        proc = subprocess.run(
            [node, "--check", str(FRONTEND / "app.js")],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr

    def test_dashboard_js_parses_with_node(self):
        """node --check passes on dashboard.js (skipped when node is absent)."""
        node = shutil.which("node")
        if node is None:
            pytest.skip("node not installed")
        proc = subprocess.run(
            [node, "--check", str(FRONTEND / "dashboard.js")],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr


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


class TestAdminDevicesFeed:
    """Admin panel reads /api/admin/devices (untagged rows included)."""

    def test_bind_list_uses_admin_feed(self):
        """populateBindList fetches and filters the admin device list."""
        js = _js()
        body = js.split("function populateBindList", 1)[1].split(
            "function startDeviceTagBind", 1
        )[0]
        assert "apiGetAdminDevices" in body
        assert "S.adminDevices" in body
        assert "apiGetDevices" not in body

    def test_occupied_slots_use_admin_feed(self):
        """occupiedSlots counts slots from S.adminDevices, not the grid list."""
        js = _js()
        body = js.split("function occupiedSlots", 1)[1].split(
            "function renderSlotGrid", 1
        )[0]
        assert "S.adminDevices" in body
        assert "S.devices" not in body

    def test_admin_devices_endpoint_referenced(self):
        """app.js fetches /api/admin/devices for the admin list."""
        js = _js()
        assert "/api/admin/devices" in js
        assert "apiGetAdminDevices" in js


class TestAdminFeedFailureBranches:
    """H1/M1: a failed admin fetch must not read as an empty locker."""

    def test_admin_fetch_throws_typed_failure(self):
        """apiGetAdminDevices throws an Error carrying .status, not []."""
        js = _js()
        body = _fn_body(js, "async function apiGetAdminDevices", "async function apiBorrow")
        assert "throw err" in body
        assert "err.status" in body
        assert "return []" not in body

    def test_bind_list_catches_and_keeps_prior_rows(self):
        """populateBindList try/catches, tracks freshness, shows a retry note."""
        js = _js()
        body = _fn_body(js, "async function populateBindList", "async function startDeviceTagBind")
        assert "try {" in body
        assert "catch" in body
        assert "adminDevicesOk" in body
        assert "bind-list-note" in body
        assert "Retry" in body

    def test_bind_list_note_markup_present(self):
        """The list step carries the failure-note line the JS targets."""
        html = _html()
        assert 'id="bind-list-note"' in html

    def test_slot_grid_not_painted_from_failed_fetch(self):
        """Add and change-slot steps bail before renderSlotGrid when stale."""
        js = _js()
        add = _fn_body(js, "function openAddFromExcel", "async function submitRegisterDevice")
        assert "adminDevicesOk" in add
        assert add.index("adminDevicesOk") < add.index("renderSlotGrid")
        slot = _fn_body(js, "function openChangeSlot", "async function submitChangeSlot")
        assert "adminDevicesOk" in slot
        assert slot.index("adminDevicesOk") < slot.index("renderSlotGrid")


class TestCatalogFailureBranches:
    """M2/L1: catalog errors branch by status; payload shape is validated."""

    def test_auth_failures_get_session_note(self):
        """401/403 show a sign-in note, not the type-the-PM Excel note."""
        js = _js()
        body = _fn_body(js, "async function populateCatalogList", "function openAddFromExcel")
        assert "status === 401" in body
        assert "status === 403" in body
        assert "sign in again as admin" in body
        assert "Excel list unavailable" in body

    def test_catalog_guards_note_and_validates_rows(self):
        """populateCatalogList guards note and rejects non-array payloads."""
        js = _js()
        body = _fn_body(js, "async function populateCatalogList", "function openAddFromExcel")
        assert "!note" in body
        assert "Array.isArray(rows)" in body
        assert "for (const r of valid)" in body

    def test_empty_note_names_no_location_value(self):
        """L7: the empty note stays value-agnostic (token is configurable)."""
        js = _js()
        assert "Location = Locker" not in js
        assert "this locker's Location" in js


class TestDemoExercisesNewBehavior:
    """M3: ?demo mirrors the tagged-only filter and renders stub catalog rows."""

    def test_demo_devices_mirror_tagged_only_filter(self):
        """Demo apiGetDevices filters has_tag like GET /api/devices."""
        js = _js()
        body = _fn_body(js, "async function apiGetDevices", "async function apiGetAdminDevices")
        assert "DEMO_DEVICES.filter" in body
        assert "has_tag" in body

    def test_demo_has_untagged_row(self):
        """At least one demo device is untagged so bind/No-tag renders."""
        js = _js()
        assert "has_tag:false" in js
        assert "has_tag:true" in js

    def test_demo_renders_stub_catalog_rows(self):
        """populateCatalogList serves DEMO_CATALOG stubs under USE_DEMO."""
        js = _js()
        assert "DEMO_CATALOG" in js
        body = _fn_body(js, "async function populateCatalogList", "function openAddFromExcel")
        assert "DEMO_CATALOG" in body

    def test_demo_opens_add_step_but_submit_stays_pi_only(self):
        """Add step is explorable in demo; only the POST keeps the Pi gate."""
        js = _js()
        add = _fn_body(js, "function openAddFromExcel", "async function submitRegisterDevice")
        assert "Pi only" not in add
        submit = _fn_body(js, "async function submitRegisterDevice", "function openChangeSlot")
        assert "Add from Excel is Pi only" in submit


class TestAddStepFitsViewport:
    """M4: the panel scrolls and the slot grid is capped."""

    def test_bind_panel_scrolls(self):
        """bind-panel keeps its 90vh cap and scrolls past it."""
        css = _css()
        panel = css.split(".bind-panel", 1)[1].split(".bind-step", 1)[0]
        assert "max-height: 90vh" in panel
        assert "overflow-y: auto" in panel

    def test_slot_grid_capped_and_scrollable(self):
        """Slot picker cannot grow the Add step past the viewport."""
        css = _css()
        grid = css.split(".bind-slot-grid", 1)[1].split(".bind-slot-btn", 1)[0]
        assert "max-height" in grid
        assert "overflow-y: auto" in grid


class TestKioskGridRefreshAfterAdminMutation:
    """L9: bind/unbind/register refresh S.devices; dashboard polls itself."""

    def test_unbind_refreshes_kiosk_feed(self):
        """unbindDeviceTag refetches the kiosk grid list on success."""
        js = _js()
        body = _fn_body(js, "async function unbindDeviceTag", "function handleTagBindSuccess")
        assert "refreshKioskDevices" in body

    def test_bind_success_refreshes_kiosk_feed(self):
        """tag_bind_success (bind + register flows) refetches the grid list."""
        js = _js()
        body = _fn_body(js, "function handleTagBindSuccess", "function handleTagBindFailed")
        assert "refreshKioskDevices" in body

    def test_refresh_helper_refetches_devices(self):
        """The helper re-reads apiGetDevices into S.devices, tolerating failure."""
        js = _js()
        body = _fn_body(js, "async function refreshKioskDevices", "async function refreshAfterDeviceAction")
        assert "apiGetDevices" in body
        assert "S.devices" in body

    def test_dashboard_polls_without_push(self):
        """Dashboard refetches on a timer, so no kiosk-to-dashboard push exists."""
        dashboard = (FRONTEND / "dashboard.js").read_text(encoding="utf-8")
        assert "setInterval(fetchTables" in dashboard


class TestMaintenanceToggle:
    """Register Device rows toggle maintenance (out for calibration)."""

    def test_bind_list_has_maintenance_buttons(self):
        """Available rows get To maintenance; maintenance rows Back in service."""
        js = _js()
        assert "To maintenance" in js
        assert "Back in service" in js
        assert "data-maint-id" in js or "maintId" in js
        assert 'data.maintOn' in js or "maint-on" in js or "maintOn" in js

    def test_js_posts_maintenance(self):
        """adminSetMaintenance POSTs /api/admin/devices/{id}/maintenance."""
        js = _js()
        assert "adminSetMaintenance" in js
        assert "/maintenance" in js


class TestUsersOverlayMarkup:
    """Users overlay + Register as admin toggle exist in kiosk markup/JS."""

    def test_admin_users_button_and_overlay_exist(self):
        """index.html carries the Users button and the Users overlay."""
        html = _html()
        assert 'id="admin-users"' in html
        assert 'id="overlay-users"' in html
        assert 'id="users-close"' in html
        assert 'id="users-list"' in html
        assert 'id="users-countdown"' in html

    def test_register_admin_role_toggle_exists(self):
        """Manual register step has the Register as admin switch."""
        html = _html()
        assert 'id="register-admin-role"' in html
        assert "Register as admin" in html

    def test_register_admin_role_defaults_unchecked(self):
        """The Register as admin switch is off by default (no checked attr)."""
        html = _html()
        start = html.index('id="register-admin-role"')
        tag_start = html.rindex("<input", 0, start)
        tag_end = html.index(">", start)
        tag = html[tag_start:tag_end]
        assert "checked" not in tag

    def test_js_posts_role_to_admin_register(self):
        """app.js sends {name, role} to /api/admin/register."""
        js = _js()
        assert "apiStartAdminRegistration" in js
        assert "JSON.stringify({ name, role })" in js
        assert "usersReplacePending" in js
        assert "api/admin/users" in js


class TestReplaceRaceGuards:
    """Replace-card late-response guards in app.js (text contracts)."""

    def test_hide_bumps_replace_epoch(self):
        """Hiding the overlay invalidates any arm POST still in flight."""
        js = _js()
        body = _fn_body(js, "function hideUsersOverlay() {", "\n}\n")
        assert "usersReplaceEpoch++" in body

    def test_late_arm_after_close_cancels_window(self):
        """An arm response from a closed overlay releases the orphaned window."""
        js = _js()
        body = _fn_body(js, "async function usersReplaceCard(id) {", "\n}\n")
        assert "const epoch = usersReplaceEpoch" in body
        # Last occurrence: the success-path stale branch (error branches guard first).
        epoch_branch = body.rsplit("epoch !== usersReplaceEpoch", 1)[1]
        assert "apiCancelRegistration()" in epoch_branch.split("return;", 1)[0]

    def test_consumed_window_keeps_result_step(self):
        """A fast tap's SSE result is not overwritten by the arm response."""
        js = _js()
        body = _fn_body(js, "async function usersReplaceCard(id) {", "\n}\n")
        tail = body.split("apiCancelRegistration();", 1)[1]
        assert "if (!S.usersReplacePending)" in tail
        assert tail.index("if (!S.usersReplacePending)") < tail.index(
            "showUsersStep('users-step-tap')"
        )

    def test_stale_arm_cancel_gated_on_no_live_replace(self):
        """A stale arm response cancels only when no fresh replace is live."""
        js = _js()
        body = _fn_body(js, "async function usersReplaceCard(id) {", "\n}\n")
        branch = body.rsplit("epoch !== usersReplaceEpoch", 1)[1]
        gate = branch.split("apiCancelRegistration()", 1)[0]
        assert "if (" in gate
        gate_if = gate.rsplit("if (", 1)[1]
        assert "!S.usersReplacePending" in gate_if
        assert "!isUsersTapStepShowing()" in gate_if
        assert "!usersReplaceArming" in gate_if

    def test_http_error_branch_guards_stale_epoch(self):
        """A stale arm POST's HTTP failure stays silent on a fresh overlay."""
        js = _js()
        body = _fn_body(js, "async function usersReplaceCard(id) {", "\n}\n")
        err_branch = body.split("if (!res.ok) {", 1)[1].split("} catch", 1)[0]
        assert "epoch !== usersReplaceEpoch" in err_branch
        assert err_branch.index("epoch !== usersReplaceEpoch") < err_branch.index(
            "res.status === 409"
        )
        stale = err_branch.index("epoch !== usersReplaceEpoch")
        stale_return = err_branch.index("return;", stale)
        assert stale_return < err_branch.index("S.usersReplacePending = false")
        assert stale_return < err_branch.index("errEl.textContent")

    def test_catch_branch_guards_stale_epoch(self):
        """A stale arm POST's network failure stays silent on a fresh overlay."""
        js = _js()
        body = _fn_body(js, "async function usersReplaceCard(id) {", "\n}\n")
        catch_branch = body.split("} catch (_) {", 1)[1].split("\n  if (epoch", 1)[0]
        assert "epoch !== usersReplaceEpoch" in catch_branch
        stale = catch_branch.index("epoch !== usersReplaceEpoch")
        stale_return = catch_branch.index("return;", stale)
        assert stale_return < catch_branch.index("S.usersReplacePending = false")
        assert stale_return < catch_branch.index("errEl.textContent")

    def test_disconnect_resets_session_ui(self):
        """reader_disconnected ends the session UI; nothing waits on a dead reader."""
        js = _js()
        assert "source.addEventListener('reader_disconnected'" in js
        branch = js.split("source.addEventListener('reader_disconnected'", 1)[1]
        branch = branch.split("});", 1)[0]
        assert "endSession(true, true)" in branch
