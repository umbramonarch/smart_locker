"""
File: test_dev_tap_panel.py
Description: Contract tests for the fake-reader preset tap panel in app.js
             (initDevTap): fixed preset UID constants enrollable via
             scripts/enroll_card.py, the editable-focus key guard, F2, and the
             fake_reader gate (zero footprint when the harness is inactive).
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_dev_tap_panel.py -v
       Frontend is vanilla HTML/JS; asserted as text like the other
       frontend-text suites. The expected UID lists below are the contract a
       later seed script enrolls verbatim — change both together.
"""

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[1] / "smart_locker" / "frontend"

EXPECTED_CARD_UIDS = ["040000A1", "040000A2", "040000A3"]
EXPECTED_TAG_UIDS = ["050000B1", "050000B2", "050000B3"]


def _js() -> str:
    return (FRONTEND / "app.js").read_text(encoding="utf-8")


def _dev_block(js: str) -> str:
    """Slice the initDevTap IIFE out of app.js source text."""
    return js.split("(function initDevTap()", 1)[1]


def _const_uids(block: str, name: str) -> list:
    """Extract the string values of a `const NAME = [...]` UID array."""
    match = re.search(r"const %s = \[(.*?)\];" % name, block, re.DOTALL)
    assert match, f"{name} constant missing from the dev-tap block"
    return re.findall(r"'([0-9A-Fa-f]+)'", match.group(1))


class TestPresetUidConstants:
    """The panel's fixed UIDs exist and survive enrollment normalization."""

    def test_card_and_tag_constants_exist_with_three_uids_each(self):
        """FAKE_CARD_UIDS / FAKE_TAG_UIDS each hold exactly the 3 seeded UIDs."""
        block = _dev_block(_js())
        assert _const_uids(block, "FAKE_CARD_UIDS") == EXPECTED_CARD_UIDS
        assert _const_uids(block, "FAKE_TAG_UIDS") == EXPECTED_TAG_UIDS

    def test_uids_match_enroll_card_normalization(self):
        """Every preset UID passes _normalize_uid's contract: contiguous
        uppercase hex, even digit count, accepted by bytes.fromhex — so a
        seed script enrolling these exact strings HMACs to the same digest
        as a later tap. All six are distinct; cards/tags stay in their own
        families."""
        block = _dev_block(_js())
        uids = _const_uids(block, "FAKE_CARD_UIDS") + _const_uids(block, "FAKE_TAG_UIDS")
        assert len(set(uids)) == 6
        for uid in uids:
            assert re.fullmatch(r"[0-9A-F]+", uid), uid
            assert len(uid) % 2 == 0, uid
            bytes.fromhex(uid)
        cards = _const_uids(block, "FAKE_CARD_UIDS")
        tags = _const_uids(block, "FAKE_TAG_UIDS")
        assert all(u.startswith("04") for u in cards)
        assert all(u.startswith("05") for u in tags)


class TestPresetPanel:
    """Panel buttons POST their UID; F2 keeps the server-default path."""

    def test_card_and_tag_buttons_post_their_uid(self):
        """Rows wire keycaps 1/2/3 and A/S/D to the card/tag UID arrays via
        the shared simulateTap POST logic."""
        block = _dev_block(_js())
        assert "presetRow('Card', ['1', '2', '3'], FAKE_CARD_UIDS, 'card')" in block
        assert "presetRow('Tag', ['A', 'S', 'D'], FAKE_TAG_UIDS, 'tag')" in block
        assert "simulateTap(uid)" in block
        assert "fetch('/api/dev/tap'" in block

    def test_f2_shortcut_still_injects_default_tap(self):
        """F2 bypasses the preset keys and POSTs with no UID (server default
        or the prompt fallback)."""
        block = _dev_block(_js())
        assert "e.key === 'F2'" in block
        assert "id = 'dev-tap-btn'" in block

    def test_panel_uses_kiosk_palette_inline_styles(self):
        """Dev-only floating control reuses the inline-style approach and the
        kiosk palette — no new design language."""
        block = _dev_block(_js())
        assert "id = 'dev-tap-panel'" in block
        assert "#181d24" in block
        assert "#009641" in block


class TestPresetKeyGuard:
    """Preset keys never fire from editable focus or modified shortcuts."""

    def test_keydown_ignores_editable_focus(self):
        """1/2/3/a/s/d are dropped when focus is in an input, textarea,
        select, or contentEditable element."""
        block = _dev_block(_js())
        handler = block.split("addEventListener('keydown'", 1)[1]
        assert "tag === 'INPUT'" in handler
        assert "tag === 'TEXTAREA'" in handler
        assert "tag === 'SELECT'" in handler
        assert "isContentEditable" in handler

    def test_keydown_ignores_modified_shortcuts(self):
        """Ctrl/Alt/Meta combinations (e.g. Ctrl+S) must not inject taps."""
        block = _dev_block(_js())
        handler = block.split("addEventListener('keydown'", 1)[1]
        assert "e.ctrlKey" in handler
        assert "e.metaKey" in handler
        assert "e.altKey" in handler

    def test_keydown_ignores_key_auto_repeat(self):
        """A held key (e.repeat) must not spray repeated taps — F2 included."""
        block = _dev_block(_js())
        handler = block.split("addEventListener('keydown'", 1)[1]
        assert "e.repeat" in handler
        assert handler.find("e.repeat") < handler.find("e.key === 'F2'")

    def test_preset_mapping_is_case_insensitive(self):
        """Lower/upper-case A/S/D reach the same tag UID lookup."""
        block = _dev_block(_js())
        handler = block.split("addEventListener('keydown'", 1)[1]
        assert ".toLowerCase()" in handler
        assert "'123'.indexOf(key)" in handler
        assert "'asd'.indexOf(key)" in handler


class TestFakeReaderGate:
    """Nothing renders or listens unless the backend reports the fake reader."""

    def test_panel_and_keys_stay_behind_fake_reader_gate(self):
        """initDevTap returns early on fake_reader:false before any DOM or
        keydown wiring, so production has zero footprint."""
        block = _dev_block(_js())
        gate = "if (!status || !status.fake_reader) return;"
        assert gate in block
        gated = block.split(gate, 1)[1]
        assert "dev-tap-panel" in gated
        assert "addEventListener('keydown'" in gated
        assert "FAKE_CARD_UIDS" in gated
        assert "FAKE_TAG_UIDS" in gated
