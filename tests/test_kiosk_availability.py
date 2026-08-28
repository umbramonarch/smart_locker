"""
File: test_kiosk_availability.py
Description: Contract tests for the kiosk locker availability overlay (in/out),
             PM number on borrow/return cards and the device-detail pane, and
             Register name-list layout (scroll the list, pin Cancel/Continue),
             and Register Cancel hiding with the screen (not after the reveal).
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


def _css() -> str:
    return (FRONTEND / "style.css").read_text(encoding="utf-8")


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

    def test_session_touch_is_debounced(self):
        """Pointer/key activity must not POST /api/session/touch on every event (I15)."""
        js = _js()
        assert "sessionTouchInFlight" in js
        assert "onUserActivity" in js
        assert "pointerdown" in js
        assert "['click', 'touchstart', 'keydown'].forEach" not in js

    def test_bind_search_filters_cache(self):
        """Bind-search keystrokes filter S.devices; they do not GET per character (I3)."""
        js = _js()
        assert "populateBindList(false)" in js
        assert "bindSearchTimer" in js

    def test_lite_navigation_skips_forced_reflow(self):
        """Lite mode must not wait 1.3s or force offsetHeight (I3)."""
        js = _js()
        assert "PERF.lite ? 250 : 1200" in js
        assert "if (!PERF.lite)" in js
        assert "1300" not in js.split("function navigate", 1)[1].split(
            "function reportKioskDisplay", 1
        )[0]


class TestAdminHoverIsCheap:
    """ADMIN MODE tile hover must not stall on stagger delay or GPU filters."""

    def test_hover_uses_transform_not_filter(self):
        """Admin :hover lifts with transform; no filter:blur or box-shadow."""
        css = _css()
        hover, _, rest = css.partition(".admin-action-btn:hover {")
        hover_block, _, after = rest.partition("}")
        danger, _, danger_rest = after.partition(".admin-action-btn.danger:hover {")
        danger_block, _, _ = danger_rest.partition("}")
        combined = hover_block + danger_block
        assert "transform: translateY(-2px)" in hover_block
        assert "filter:" not in combined
        assert "box-shadow:" not in combined
        assert "backdrop-filter:" not in combined

    def test_stagger_does_not_delay_hover(self):
        """Entrance stagger is a one-shot animation, not transition-delay on hover."""
        css = _css()
        btn, _, rest = css.partition(".admin-action-btn {")
        block, _, _ = rest.partition(".admin-btn-icon {")
        assert "transition-delay:" not in block
        assert "animation: admin-tile-in" in block
        assert "animation-fill-mode: backwards" in block or "backwards" in block
        assert "admin-spin" not in css

    def test_lite_skips_admin_hover_lift(self):
        """html.lite must not animate or translate admin tiles on hover."""
        css = _css()
        lite, _, rest = css.partition("html.lite .admin-action-btn {")
        lite_block, _, after = rest.partition("html.lite .btn-reveal-img")
        assert "animation: none" in lite_block
        assert "html.lite .admin-action-btn:hover" in after or "html.lite .admin-action-btn:hover" in lite_block
        hover_lite = lite_block + after[:400]
        assert "transform: none" in hover_lite


class TestMainMenuHoverIsCheap:
    """Locker / Return / End Session hover must not stall on stagger or overlays."""

    def test_hover_uses_transform_not_filter(self):
        """Main-menu :hover lifts with transform; no filter or box-shadow."""
        css = _css()
        hover, _, rest = css.partition(".action-btn:hover {")
        hover_block, _, after = rest.partition("}")
        assert "transform: translateY(-2px)" in hover_block
        assert "filter:" not in hover_block
        assert "box-shadow:" not in hover_block
        primary, _, prim_rest = after.partition(".action-btn.primary:hover {")
        primary_block, _, _ = prim_rest.partition("}")
        assert "box-shadow:" not in primary_block
        assert "filter:" not in primary_block

    def test_stagger_does_not_delay_hover(self):
        """Entrance stagger is a one-shot animation, not transition-delay on hover."""
        css = _css()
        _, _, rest = css.partition(".action-btn {")
        block, _, _ = rest.partition(".btn-icon {")
        assert "transition-delay:" not in block
        assert "animation: action-tile-in" in block
        assert "backwards" in block
        img_hover, _, img_rest = css.partition(".action-btn:hover .btn-reveal-img {")
        img_block, _, _ = img_rest.partition("}")
        assert "clip-path:" not in img_block
        assert "transform:" not in img_block

    def test_lite_skips_action_hover_lift(self):
        """html.lite must not animate or translate main-menu tiles on hover."""
        css = _css()
        _, _, rest = css.partition("html.lite .action-btn {")
        lite_block, _, _ = rest.partition("html.lite .device-card")
        assert "animation: none" in lite_block
        assert "html.lite .action-btn:hover" in lite_block
        assert "transform: none" in lite_block

    def test_hidden_overlay_does_not_eat_pointer(self):
        """Closing/hidden overlays must not intercept hover on the menu."""
        css = _css()
        _, _, rest = css.partition(".overlay {")
        overlay_block, _, after = rest.partition(".overlay.visible {")
        assert "pointer-events: none" in overlay_block
        vis, _, vis_rest = after.partition("}")
        assert "pointer-events: auto" in vis
        hide, _, hide_rest = vis_rest.partition(".overlay.hidden-left {")
        hide_block, _, _ = hide_rest.partition(".overlay.hidden-right {")
        assert "pointer-events: none" in hide_block

    def test_magnetic_skips_action_btn(self):
        """Magnetic mousemove must not attach to main-menu .action-btn tiles."""
        js = _js()
        mag = js.split("function initMagneticHover()", 1)[1].split(
            "function initMarquee()", 1
        )[0]
        assert "querySelectorAll('.action-btn')" not in mag
        assert "querySelectorAll('.back-btn, .detail-close, .stay-btn, .confirm-btn')" in mag


class TestLiteDeviceDetailCloseRestoresLocker:
    """html.lite: leaving a locker-grid device must not leave an invisible hit-catcher."""

    def test_lite_closing_overlay_descendants_do_not_eat_pointer(self):
        """Lite fades with opacity only; hidden overlay children must not capture taps."""
        css = _css()
        lite = css.split("html.lite .overlay {", 1)[1].split(
            "html.lite .action-btn {", 1
        )[0]
        assert "pointer-events: none" in lite
        assert "html.lite .overlay.hidden-left *" in lite
        assert "html.lite .overlay.hidden-right *" in lite
        assert "html.lite .overlay:not(.visible) *" in lite

    def test_lite_close_detail_hides_before_full_wipe(self):
        """closeDetail must drop lite hit-testing on the fade, not wait 710ms."""
        js = _js()
        fn = js.split("function closeDetail()", 1)[1].split(
            "async function confirmAction", 1
        )[0]
        assert "PERF.lite ? 220 : 710" in fn
        assert "hidden-right" in fn
        assert "clearTimeout(detailHideTimer)" in fn

    def test_lite_reopen_clears_closing_detail_overlay(self):
        """A stale hide must not display:none a detail pane opened from the locker grid."""
        js = _js()
        nav = js.split("function navigate(toId)", 1)[1].split(
            "function reportKioskDisplay", 1
        )[0]
        assert "classList.remove('hidden-left', 'hidden-right')" in nav
        assert "clearTimeout(detailHideTimer)" in nav
        assert "overlay-device-detail" in nav


class TestOverlaySessionRestore:
    """A leftover 5-tap overlay must not restore as a full admin locker login."""

    def test_check_existing_session_ignores_overlay(self):
        """checkExistingSession stays idle when GET /api/session overlay is true."""
        js = _js()
        fn = js.split("async function checkExistingSession", 1)[1].split(
            "if (USE_DEMO)", 1
        )[0]
        assert "data.overlay" in fn
        assert "!data.overlay" in fn
        assert "navigate('main-menu')" in fn
        overlay_false = fn.split("!data.overlay")[1]
        assert "overlay=false" in overlay_false

    def test_admin_overlay_sets_screen_so_touch_runs(self):
        """Admin / Register Device overlays must not look like idle to onUserActivity."""
        js = _js()
        open_admin = js.split("async function openAdminPanel", 1)[1].split(
            "function closeAdminPanel", 1
        )[0]
        assert "S.screen = 'admin'" in open_admin
        assert "armIdle()" in open_admin
        register = js.split("async function adminRegisterDevice", 1)[1].split(
            "let bindSearchTimer", 1
        )[0]
        assert "S.screen = 'admin'" in register
        assert "armIdle()" in register


class TestRegisterNameListFitsViewport:
    """N registrant names must not shove Cancel/Continue off a 10.1\" kiosk."""

    def test_html_pins_continue_below_scrollable_list(self):
        """Name pick is head / list / foot; search and Continue ids stay put."""
        html = _html()
        assert 'class="register-step register-step-pick"' in html
        pick = html.split('id="register-step-name"', 1)[1].split(
            'id="register-step-name-admin"', 1
        )[0]
        assert 'class="register-step-head"' in pick
        assert 'id="register-search"' in pick
        assert pick.find('id="register-search"') < pick.find('id="register-name-list"')
        assert 'class="register-step-foot"' in pick
        assert pick.find('id="register-name-list"') < pick.find('id="register-next-btn"')
        assert 'id="register-cancel-btn"' not in pick
        assert 'id="register-cancel-btn"' in html

    def test_name_list_scrolls_inside_flex_column(self):
        """List takes leftover height and scrolls; no fixed 340px cap."""
        css = _css()
        _, _, rest = css.partition(".register-name-list {")
        block, _, _ = rest.partition(".register-name-list::-webkit-scrollbar")
        assert "overflow-y: auto" in block
        assert "overflow-x: visible" in block
        assert "min-height: 0" in block
        assert "flex: 1" in block
        assert "max-height: 340px" not in block

    def test_register_heading_is_not_clipped(self):
        """REGISTER title/icon must not sit inside overflow:hidden flex parents."""
        css = _css()
        _, _, rest = css.partition(".register-center {")
        center, _, _ = rest.partition("}")
        assert "overflow: visible" in center
        assert "overflow: hidden" not in center
        _, _, pick_rest = css.partition(".register-step-pick {")
        pick_block, _, _ = pick_rest.partition(".register-step-head {")
        assert "overflow: visible" in pick_block
        assert "overflow: hidden" not in pick_block
        _, _, head_rest = css.partition(".register-step-head {")
        head_block, _, _ = head_rest.partition(".register-step-foot {")
        assert "overflow: visible" in head_block
        assert "padding:" in head_block
        _, _, title_rest = css.partition(".register-title {")
        title_block, _, _ = title_rest.partition(".register-title-sm {")
        assert "line-height: 1.12" in title_block
        assert "overflow: visible" in title_block
        _, _, wrap_rest = css.partition(".register-title .reveal-wrap {")
        wrap_block, _, _ = wrap_rest.partition("}")
        assert "padding:" in wrap_block

    def test_register_head_sits_at_top(self):
        """Name-pick chrome starts at the top so the list gets leftover height."""
        css = _css()
        _, _, rest = css.partition("#screen-register {")
        screen, _, _ = rest.partition(".register-center {")
        assert "justify-content: flex-start" in screen
        _, _, center_rest = css.partition(".register-center {")
        center, _, _ = center_rest.partition("}")
        assert "justify-content: flex-start" in center
        _, _, pick_rest = css.partition(".register-step-pick {")
        pick_block, _, _ = pick_rest.partition(".register-step-head {")
        assert "justify-content: flex-start" in pick_block
        assert "flex: 1" in pick_block
        _, _, head_rest = css.partition(".register-step-head {")
        head_block, _, _ = head_rest.partition("}")
        assert "flex-shrink: 0" in head_block

    def test_register_footer_does_not_shrink(self):
        """Continue / Cancel / name-pick foot stay pinned; screen leaves Cancel room."""
        css = _css()
        _, _, rest = css.partition("#screen-register {")
        screen, _, _ = rest.partition(".register-center {")
        assert "padding:" in screen
        assert "5.75rem" in screen
        assert "justify-content: flex-start" in screen
        _, _, pick_rest = css.partition(".register-step-pick {")
        pick_block, _, _ = pick_rest.partition(".register-step-head {")
        assert "min-height: 0" in pick_block
        foot, _, foot_rest = css.partition(".register-step-foot {")
        foot_block, _, _ = foot_rest.partition(".register-icon-circle {")
        assert "flex-shrink: 0" in foot_block
        _, _, next_rest = css.partition(".register-next-btn {")
        next_block, _, _ = next_rest.partition(".register-next-btn svg")
        assert "flex-shrink: 0" in next_block
        _, _, cancel_rest = css.partition(".register-cancel-btn {")
        cancel_block, _, _ = cancel_rest.partition(".register-cancel-btn svg")
        assert "flex-shrink: 0" in cancel_block
        assert "position: absolute" in cancel_block

    def test_lite_does_not_override_register_list_scroll(self):
        """html.lite must not undo the name-list flex scroll (Pi launches ?lite)."""
        css = _css()
        lite = css.split("html.lite", 1)[1]
        assert "register-name-list" not in lite
        assert "register-step-pick" not in lite
        assert "register-step-foot" not in lite

    def test_name_item_stagger_does_not_delay_hover(self):
        """Entrance stagger is a one-shot animation; JS must not set transitionDelay."""
        js = _js()
        fn = js.split("function populateNameList", 1)[1].split(
            "function selectRegistrantName", 1
        )[0]
        assert "transitionDelay" not in fn
        assert "style.transitionDelay" not in fn
        assert "setProperty('--i'" in fn
        css = _css()
        _, _, rest = css.partition(".name-item {")
        block, _, _ = rest.partition(".name-item:hover {")
        assert "transition-delay:" not in block
        assert "animation: name-item-in" in block
        assert "backwards" in block

    def test_register_cancel_hides_with_screen_exit(self):
        """Cancel must snap off with .exit / not(.active), not after the 1.2s reveal."""
        css = _css()
        assert "#screen-register.exit .register-cancel-btn" in css
        assert "#screen-register:not(.active) .register-cancel-btn" in css
        _, _, rest = css.partition("#screen-register.exit .register-cancel-btn")
        block, _, _ = rest.partition(".nfc-wrapper-sm")
        assert "opacity: 0" in block
        assert "pointer-events: none" in block
        assert "transition: none" in block
        assert "1.2s" not in block
        assert "1200" not in block
        assert "710ms" not in block
        js = _js()
        cancel = js.split("function cancelRegistration", 1)[1].split(
            "function handleRegistrationSuccess", 1
        )[0]
        assert "navigate('idle')" in cancel
        assert "setTimeout" not in cancel


class TestSlotOverlay:
    """Returned devices show the slot overlay from the device_action SSE payload."""

    def test_html_has_overlay_slot(self):
        """Kiosk markup includes the return-slot overlay the overlay JS targets."""
        html = _html()
        assert 'id="overlay-slot"' in html
        assert 'id="slot-return-name"' in html
        assert 'id="slot-return-num"' in html

    def test_js_wires_device_action_to_slot_overlay(self):
        """SSE device_action return shows the overlay; payload includes locker_slot."""
        js = _js()
        assert "function showSlotOverlay" in js
        assert "addEventListener('device_action'" in js
        action = js.split("addEventListener('device_action'", 1)[1].split(
            "addEventListener(", 1
        )[0]
        assert "showSlotOverlay" in action
        assert "data.action === 'return'" in action
        assert "data.locker_slot" in action
        assert "data.device_name" in action
