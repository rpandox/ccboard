"""Pins for the page-load fixes of issue #103 (Lighthouse): scripts that do not block the first paint, the icon font fetched early, and a frame that already has
its sidebar column before shell.js runs. The text-compression half is tests/test_static_gzip.py."""
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, {k: (v if v is not None else "") for k, v in attrs}))


def tags(page):
    p = _Tags()
    p.feed((STATIC / page).read_text(encoding="utf-8"))
    return p.tags


@pytest.mark.parametrize("page", ["index.html", "term.html"])
def test_every_script_is_deferred_classic_and_external(page):
    """defer keeps the order (the scripts share one global scope) and runs them before DOMContentLoaded, but the parser and the first paint no longer wait for them."""
    scripts = [a for t, a in tags(page) if t == "script"]
    assert scripts
    for a in scripts:
        assert a.get("src", "").startswith("/static/") and "defer" in a, (page, a)
        assert "async" not in a and a.get("type", "text/javascript") in ("", "text/javascript"), (page, a)


@pytest.mark.parametrize("page", ["index.html", "term.html"])
def test_the_icon_font_is_preloaded_beside_the_text_fonts(page):
    """Blueprint's icons are glyphs of blueprint-icons-20.woff2: asked for only when JS builds the first icon, the font arrived after the first paint and every row swapped
    its icon width (a layout shift on each page). The preload needs as=font, type and crossorigin, like the text fonts."""
    pre = {a.get("href"): a for t, a in tags(page) if t == "link" and a.get("rel") == "preload"}
    a = pre.get("/static/vendor/blueprint/blueprint-icons-20.woff2")
    assert a is not None, f"{page}: no preload for the icon font"
    assert a.get("as") == "font" and a.get("type") == "font/woff2" and "crossorigin" in a
    assert (STATIC / "vendor" / "blueprint" / "blueprint-icons-20.woff2").is_file()
    assert "blueprint-icons-20" in (STATIC / "vendor" / "blueprint" / "blueprint-icons.css").read_text(encoding="utf-8")


def test_the_frame_has_its_sidebar_column_before_the_shell_is_built():
    """index.html ships body[data-shell=""] (no data-sb) until Shell.applyMode runs; without these rules #main sat at the left edge and then moved 260 px (CLS 0.18 on every
    desktop page). The widths are applyMode's own defaults: the rail from 600 px, the full sidebar from 840 px."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert re.search(r'<body[^>]*\bdata-shell=""', html), "the pre-shell state these rules key on"
    css = (STATIC / "shell.css").read_text(encoding="utf-8")
    assert re.search(r'@media \(min-width:600px\)\s*\{\s*body\[data-shell=""\] #app\s*\{\s*--sb-w:var\(--sb-rail\);', css)
    assert re.search(r'@media \(min-width:840px\)\s*\{\s*body\[data-shell=""\] #app\s*\{\s*--sb-w:var\(--sb-full\);', css)
    js = (STATIC / "shell.js").read_text(encoding="utf-8")
    assert "mode === 'compact' ? 'none'" in js and "'rail' : 'full'" in js, "applyMode still picks none / rail / full the way the CSS defaults assume"
    assert re.search(r"--sb-rail:\s*48px", css) and re.search(r"--sb-full:\s*260px", css)
