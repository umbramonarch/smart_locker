"""
File: test_keyboard.py
Description: Contract tests for the kiosk on-screen keyboard (keyboard.js):
             fields hidden by attribute, computed style, or a covering overlay
             are treated as gone — both when focus arrives (a stale .focus()
             timer) and on every key press before the input is mutated.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_keyboard.py -v
       Frontend is vanilla JS; asserted as text like the other
       frontend-text suites.
"""
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[1] / "smart_locker" / "frontend"


def _js() -> str:
    return (FRONTEND / "keyboard.js").read_text(encoding="utf-8")


def _fn(js: str, name: str, next_marker: str) -> str:
    return js.split(f"function {name}", 1)[1].split(next_marker, 1)[0]


class TestFieldGone:
    """fieldGone() covers hidden attributes, computed CSS, and cover."""

    def test_hidden_attribute_and_connectivity_checked(self):
        fn = _fn(_js(), "fieldGone", "new MutationObserver")
        assert "el.isConnected" in fn
        assert "el.hidden" in fn

    def test_computed_visibility_checked(self):
        """checkVisibility catches class/display/visibility rules anywhere."""
        fn = _fn(_js(), "fieldGone", "new MutationObserver")
        assert "checkVisibility" in fn
        assert "checkVisibilityCSS" in fn

    def test_covering_overlay_checked(self):
        """Another visible .overlay over the field's host counts as gone."""
        fn = _fn(_js(), "fieldGone", "new MutationObserver")
        assert "querySelectorAll('.overlay')" in fn
        assert "contains('visible')" in fn
        assert "style.display" in fn

    def test_ancestor_walk_kept(self):
        """The original hidden/screen/overlay ancestor walk stays."""
        fn = _fn(_js(), "fieldGone", "new MutationObserver")
        assert "contains('hidden')" in fn
        assert "contains('screen') && !c.contains('active')" in fn
        assert "contains('overlay') && !c.contains('visible')" in fn


class TestEligibilityGuards:
    """open() and every key press re-check that the field is still eligible."""

    def test_focusin_guards_hidden_fields(self):
        """focusin (incl. a stale programmatic .focus()) opens only if shown."""
        fn = _js()[_js().index("addEventListener('focusin'"):]
        assert "fieldGone(e.target)" in fn

    def test_handle_key_rechecks_target_before_mutation(self):
        """A key press on a since-hidden field closes, never writes."""
        fn = _fn(_js(), "handleKey", "kbd.addEventListener")
        assert "fieldGone(el)" in fn
        assert fn.index("fieldGone(el)") < fn.index("case 'shift'")
        assert fn.index("fieldGone(el)") < fn.index("insertText(el")

    def test_number_inputs_excluded(self):
        """'number' stays out of TEXT_TYPES — .value coercion would corrupt."""
        block = _js().split("TEXT_TYPES", 1)[1].split(";", 1)[0]
        assert "'number'" not in block
