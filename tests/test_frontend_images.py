"""
File: test_frontend_images.py
Description: Contract tests for missing-image hardening: with
             smart_locker/frontend/images/ emptied, kiosk + dashboard degrade
             to designed placeholders (no dead menu art, inline favicons, img
             onerror fallbacks, CSS gradient backdrops). Honest scope: <img>
             photo paths never render a broken-image icon, but the two CSS hero
             backgrounds still request images/hero_bg.jpg (a 404 when emptied)
             — the layered gradient covers the visual, it does not avoid the
             request. That hero URL is the only images/ reference in any CSS.
Project: smart_locker/tests
Notes: Run with: python -m pytest tests/test_frontend_images.py -v
       Frontend is vanilla HTML/JS; asserted as text like the other
       frontend-text suites.
"""

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[1] / "smart_locker" / "frontend"

IMAGE_FILE_RE = re.compile(r"images/[A-Za-z0-9_.\-]+\.(jpg|jpeg|png|svg|gif|webp)")


def _kiosk_html() -> str:
    return (FRONTEND / "index.html").read_text(encoding="utf-8")


def _dash_html() -> str:
    return (FRONTEND / "dashboard.html").read_text(encoding="utf-8")


def _js() -> str:
    return (FRONTEND / "app.js").read_text(encoding="utf-8")


def _dash_js() -> str:
    return (FRONTEND / "dashboard.js").read_text(encoding="utf-8")


def _css() -> str:
    return (FRONTEND / "style.css").read_text(encoding="utf-8")


def _dash_css() -> str:
    return (FRONTEND / "dashboard.css").read_text(encoding="utf-8")


def _head(html: str) -> str:
    """Slice the <head> block out of a served HTML file."""
    return html.split("</head>", 1)[0]


class TestNoDeadImageReferences:
    """Markup and inline JS reference no image files except device photos."""

    def test_kiosk_markup_has_no_image_file_references(self):
        """index.html (markup + inline scripts) names no images/* file."""
        assert IMAGE_FILE_RE.search(_kiosk_html()) is None
        assert "images/camera.jpg" not in _kiosk_html()
        assert "images/drone.jpg" not in _kiosk_html()

    def test_dashboard_markup_has_no_image_file_references(self):
        """dashboard.html names no images/* file."""
        assert IMAGE_FILE_RE.search(_dash_html()) is None
        assert "images/" not in _dash_html()

    def test_kiosk_js_names_no_image_files(self):
        """app.js reaches device photos only through safeKioskImagePath."""
        js = _js()
        assert IMAGE_FILE_RE.search(js) is None
        assert "safeKioskImagePath" in js
        assert "startsWith('images/')" in js

    def test_dashboard_js_names_no_image_files(self):
        """dashboard.js references no images/ path at all."""
        assert IMAGE_FILE_RE.search(_dash_js()) is None
        assert "images/" not in _dash_js()


class TestInlineFavicons:
    """Both heads carry an inline SVG favicon so /favicon.ico never 404s."""

    def test_kiosk_head_has_data_uri_icon(self):
        """index.html head links a data-URI icon in locker green."""
        head = _head(_kiosk_html())
        assert '<link rel="icon" href="data:image/svg+xml,' in head
        assert "%23009641" in head

    def test_dashboard_head_has_data_uri_icon(self):
        """dashboard.html head links a data-URI icon in locker green."""
        head = _head(_dash_html())
        assert '<link rel="icon" href="data:image/svg+xml,' in head
        assert "%23009641" in head


class TestDevicePhotoFallbacks:
    """Grid cards and the detail pane degrade to placeholders on img error."""

    def test_grid_card_wires_onerror_to_placeholder(self):
        """buildDeviceCardEl swaps a failed photo for the shared placeholder
        and drops the hover reveal (which would show the same dead file)."""
        fn = _js().split("function buildDeviceCardEl", 1)[1].split(
            "function openDetail", 1
        )[0]
        assert "img.onerror" in fn
        assert "reveal.remove()" in fn
        assert "buildCardImgPlaceholder(slotLabel)" in fn
        assert fn.find("img.onerror") < fn.find("img.src = imgPath")

    def test_detail_pane_wires_onerror_to_placeholder(self):
        """openDetail unhides #detail-img-placeholder when the photo 404s."""
        fn = _js().split("function openDetail(dev, mode)", 1)[1].split(
            "function closeDetail()", 1
        )[0]
        assert "img.onerror" in fn
        assert "placeholder.classList.remove('hidden')" in fn
        assert fn.find("img.onerror") < fn.find("img.src = imgPath")

    def test_detail_pane_ignores_stale_photo_errors(self):
        """openDetail tags each photo load with a generation so a slow error
        from a previously viewed device cannot flip the current placeholder."""
        js = _js()
        assert "let detailImgGen = 0;" in js
        fn = js.split("function openDetail(dev, mode)", 1)[1].split(
            "function closeDetail()", 1
        )[0]
        assert "++detailImgGen" in fn
        assert "imgGen !== detailImgGen" in fn
        assert fn.find("++detailImgGen") < fn.find("img.onerror")


class TestHeroCssFallback:
    """Backdrop art layers a gradient under the photo so emptied images/
    leaves a designed background instead of a flat void."""

    def _block(self, css: str, selector: str) -> str:
        _, _, rest = css.partition(selector + " {")
        block, _, _ = rest.partition("}")
        assert block, f"{selector} rule missing from style.css"
        return block

    def test_hero_bg_layers_gradient_under_photo(self):
        """The .hero-bg rule keeps url() on top of a gradient fallback."""
        block = self._block(_css(), ".hero-bg")
        assert "url(" in block
        assert "linear-gradient(" in block
        assert block.find("url(") < block.find("linear-gradient(")

    def test_nfc_texture_layers_gradient_under_photo(self):
        """The .nfc-texture-reveal rule keeps url() on top of a gradient."""
        block = self._block(_css(), ".nfc-texture-reveal")
        assert "url(" in block
        assert "linear-gradient(" in block
        assert block.find("url(") < block.find("linear-gradient(")

    def test_css_image_urls_are_hero_only(self):
        """style.css names images/hero_bg.jpg twice (the two hero rules) and
        no other image file; dashboard.css names none. This pins the honest
        contract: the hero 404-when-emptied is the only CSS image request."""
        css = _css()
        assert css.count("images/hero_bg.jpg") == 2
        assert len(IMAGE_FILE_RE.findall(css)) == 2
        assert IMAGE_FILE_RE.search(_dash_css()) is None
        assert "images/" not in _dash_css()
