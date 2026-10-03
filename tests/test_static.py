"""Static-asset guard rails over every app-owned file under app/static (everything except vendor/ and demo/).

The CSP is `default-src 'self'`, so nothing here may inject markup, carry inline code or style, or reach another origin.
Each rule is its own test and names file:line, so a failure says exactly what to change. Comments are blanked before the JS
and CSS rules run (a comment may say "never innerHTML"), and line numbers are preserved.
"""
import pathlib
import re
import struct
from html.parser import HTMLParser

import pytest

STATIC = pathlib.Path(__file__).resolve().parent.parent / "app" / "static"
EXCLUDED_TOP = {"vendor", "demo"}
TEXT_SUFFIXES = {".js", ".css", ".html", ".webmanifest", ".svg"}


def app_files(*suffixes):
    out = []
    for p in sorted(STATIC.rglob("*")):
        rel = p.relative_to(STATIC)
        if not p.is_file() or rel.parts[0] in EXCLUDED_TOP or "__pycache__" in rel.parts:
            continue
        if not suffixes or p.suffix in suffixes:
            out.append(p)
    return out


def rel(path):
    return "app/static/" + path.relative_to(STATIC).as_posix()


def line_of(text, pos):
    return text.count("\n", 0, pos) + 1


def snippet(text, pos):
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    return text[start:end if end >= 0 else len(text)].strip()[:120]


def blank(s):
    return re.sub(r"[^\n]", " ", s)


# ---------- comment (and optionally string) blanking, so rules read code, not prose ----------

_WORD = re.compile(r"[A-Za-z0-9_$]+")
_REGEX_AFTER_CHARS = set("(,=:[!&|?{};+-*%<>~^")
_REGEX_AFTER_WORDS = {"return", "typeof", "case", "do", "else", "in", "of", "delete", "void", "throw", "yield", "await"}


def blank_js(src, strings=False):
    """Blank JS comments (and, with strings=True, the text of string, template and regex literals), keeping every newline.

    A small scanner, not a parser: a '/' starts a regex literal when the previous token cannot end an operand. Quoted strings and
    regex literals end at a newline, so a misjudgement cannot swallow more than the rest of its line.
    """
    out = []
    n = len(src)
    i = 0
    last = ""                 # previous significant token: a character, or an identifier / number
    tpl = []                  # brace depth inside each open `${ ... }` of a template literal

    def literal(text):        # text of a string/regex literal, blanked between its delimiters when strings=True
        if not strings or len(text) <= 2:
            return text
        return text[0] + blank(text[1:-1]) + text[-1]

    def template(j):          # scan template text from j to the closing backtick or the next `${`; returns (end, opened_expr)
        k = j
        while k < n and src[k] != "`" and src[k:k + 2] != "${":
            k += 2 if src[k] == "\\" else 1
        k = min(k, n)
        if src[k:k + 2] == "${":
            out.append(blank(src[j:k]) if strings else src[j:k])
            out.append("${")
            return k + 2, True
        end = min(k + 1, n)
        out.append((blank(src[j:k]) + src[k:end]) if strings else src[j:end])
        return end, False

    while i < n:
        c = src[i]
        two = src[i:i + 2]
        if two == "//":
            j = src.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif two == "/*":
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(blank(src[i:j]))
            i = j
        elif c in "'\"":
            j = i + 1
            while j < n and src[j] != c and src[j] != "\n":
                j += 2 if src[j] == "\\" else 1
            j = min(j + 1, n)
            out.append(literal(src[i:j]))
            i, last = j, "x"
        elif c == "`":
            out.append("`")
            i, opened = template(i + 1)
            if opened:
                tpl.append(0)
                last = "("
            else:
                last = "x"
        elif c == "/" and (last == "" or last in _REGEX_AFTER_CHARS or last in _REGEX_AFTER_WORDS):
            j, in_class = i + 1, False
            while j < n and src[j] != "\n" and (in_class or src[j] != "/"):
                if src[j] == "\\":
                    j += 1
                elif src[j] == "[":
                    in_class = True
                elif src[j] == "]":
                    in_class = False
                j += 1
            if j < n and src[j] == "/":
                j += 1
                while j < n and src[j].isalpha():
                    j += 1
                out.append(literal(src[i:j]))
                i, last = j, "x"
            else:                                   # not a regex after all: a stray division
                out.append(c)
                i, last = i + 1, c
        elif c == "{" and tpl:
            tpl[-1] += 1
            out.append(c)
            i, last = i + 1, c
        elif c == "}" and tpl:
            if tpl[-1] == 0:
                tpl.pop()
                out.append("}")
                i, opened = template(i + 1)
                if opened:
                    tpl.append(0)
                    last = "("
                else:
                    last = "x"
            else:
                tpl[-1] -= 1
                out.append(c)
                i, last = i + 1, c
        elif c.isspace():
            out.append(c)
            i += 1
        else:
            m = _WORD.match(src, i)
            if m:
                out.append(m.group())
                i, last = m.end(), m.group()
            else:
                out.append(c)
                i, last = i + 1, c
    return "".join(out)


def blank_css_comments(css):
    return re.sub(r"/\*.*?\*/", lambda m: blank(m.group()), css, flags=re.S)


def test_js_comment_blanker():
    src = "a // x.innerHTML\nb /* y\n .innerHTML */ c = 'http://z'; /[/'\"]/g.test(d) // tail\n`q ${ e('//') } //r`; f / g // end\n"
    got = blank_js(src)
    assert got.count("\n") == src.count("\n")
    assert "innerHTML" not in got and "tail" not in got and "end" not in got
    assert "'http://z'" in got and "/[/'\"]/g.test(d)" in got and "`q ${ e('//') } //r`" in got and "f / g" in got
    only_code = blank_js(src, strings=True)
    assert "http" not in only_code and "e(" in only_code and "g.test(d)" in only_code
    assert "style" not in blank_js("const t = 'style: x'; /* style: y */", strings=True)


# ---------- the scan itself finds the files it should ----------

def test_scan_covers_the_app_owned_files():
    names = {rel(p) for p in app_files()}
    for must in ("app/static/index.html", "app/static/term.html", "app/static/sw.js", "app/static/core.js", "app/static/style.css"):
        assert must in names, f"{must} is missing from the static scan (path or exclusion bug in tests/test_static.py)"
    assert not any("/vendor/" in n or "/demo/" in n for n in names)


# ---------- PNG assets: icons and the manifest's store screenshots ----------

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PNG_COLOR_TYPES_WITH_ALPHA = (4, 6)
SCREENSHOT_MAX_BYTES = 1_000_000          # every static png is precached by the service worker: keep the offline shell light


def png_header(path):
    """(width, height, bit_depth, color_type) from the IHDR chunk; fails the test when the file is not a PNG."""
    data = pathlib.Path(path).read_bytes()[:33]
    assert data[:8] == PNG_MAGIC and data[12:16] == b"IHDR", f"{path} is not a PNG file"
    width, height, depth, color_type = struct.unpack(">IIBB", data[16:26])
    return width, height, depth, color_type


def test_screenshots_dir_holds_only_valid_pngs():
    """app/static/screenshots/ (manifest `screenshots`, captured by scripts/qa-ui.sh with QA_MANIFEST_SHOTS=1) is app-owned, so it is
    scanned like the rest; the only thing it may contain is PNGs of a sane size."""
    shots = STATIC / "screenshots"
    files = sorted(p for p in shots.rglob("*") if p.is_file()) if shots.is_dir() else []
    if not files:
        pytest.skip("no screenshots yet (the manifest test in tests/test_shell.py asserts the ones the manifest names)")
    bad = [rel(p) for p in files if p.suffix != ".png"]
    assert not bad, f"only .png files belong in app/static/screenshots/: {bad}"
    scanned = {rel(p) for p in app_files()}
    for p in files:
        assert rel(p) in scanned, f"{rel(p)} is missing from the static scan"
        width, height, _, _ = png_header(p)
        assert width >= 320 and height >= 320, f"{rel(p)} is {width}x{height}: Chrome ignores install screenshots under 320 px"
        assert p.stat().st_size <= SCREENSHOT_MAX_BYTES, f"{rel(p)} is {p.stat().st_size} bytes (limit {SCREENSHOT_MAX_BYTES}): it ships in the offline shell"


# ---------- JavaScript ----------

JS_BANS = [
    ("innerHTML", r"\.\s*innerHTML\b|\binnerHTML\s*[=+]", "build nodes with el()/textContent, never innerHTML"),
    ("insertAdjacentHTML", r"\binsertAdjacentHTML\b", "build nodes with el()/textContent"),
    ("outerHTML", r"\bouterHTML\b", "build nodes with el()/textContent"),
    ("document.write", r"\bdocument\s*\.\s*write(?:ln)?\s*\(", "no document.write"),
    ("eval", r"\beval\s*\(", "no eval (CSP forbids it, and so do we)"),
    ("new Function", r"\bnew\s+Function\s*\(", "no new Function (same as eval)"),
    ("cssText", r"\bcssText\b", "set el.style.<prop> instead of style.cssText"),
    ("setAttribute('style')", r"""\bsetAttribute\s*\(\s*['"`]style['"`]""", "set el.style.<prop>; style attributes break the CSP"),
    ("createElement('style')", r"""\bcreateElement\s*\(\s*['"`]style['"`]""", "put the rule in a .css file"),
    ("javascript: URL", r"javascript\s*:", "no javascript: URLs"),
]


JS_BAD_SAMPLES = {
    "innerHTML": "node.innerHTML = x;",
    "insertAdjacentHTML": "node.insertAdjacentHTML('beforeend', x)",
    "outerHTML": "const h = node.outerHTML;",
    "document.write": "document.write(x)",
    "eval": "eval(code)",
    "new Function": "new Function('return 1')",
    "cssText": "node.style.cssText = 'a:b'",
    "setAttribute('style')": "node.setAttribute('style', 'a:b')",
    "createElement('style')": "document.createElement('style')",
    "javascript: URL": "a.href = 'JavaScript:void(0)'",
}


@pytest.mark.parametrize("name,pattern,advice", JS_BANS, ids=[b[0] for b in JS_BANS])
def test_js_ban_patterns_catch_their_sample(name, pattern, advice):
    """A rule that never matches protects nothing: each ban must fire on a known-bad line and ignore the same text in a comment."""
    sample = JS_BAD_SAMPLES[name]
    assert re.search(pattern, blank_js(sample), re.I if name == "javascript: URL" else 0), f"{name} pattern misses {sample!r}"
    assert not re.search(pattern, blank_js("// " + sample + "\n/* " + sample + " */"), re.I)


@pytest.mark.parametrize("name,pattern,advice", JS_BANS, ids=[b[0] for b in JS_BANS])
def test_js_banned_construct(name, pattern, advice):
    bad = []
    for p in app_files(".js"):
        code = blank_js(p.read_text(encoding="utf-8"))
        for m in re.finditer(pattern, code, re.I if name == "javascript: URL" else 0):
            bad.append(f"{rel(p)}:{line_of(code, m.start())}: {snippet(code, m.start())}")
    assert not bad, f"{name} is banned ({advice}):\n" + "\n".join(bad)


def test_js_no_style_key_in_el_attrs():
    """el('div', { style: '...' }) would end up as a style attribute, which the CSP blocks. Use el.style.<prop> or a class."""
    bad = []
    for p in app_files(".js"):
        raw = p.read_text(encoding="utf-8")
        code = blank_js(raw)
        bare = blank_js(raw, strings=True)
        for m in re.finditer(r"\bstyle\s*:", bare):
            bad.append(f"{rel(p)}:{line_of(bare, m.start())}: {snippet(code, m.start())}")
        for m in re.finditer(r"""['"`]style['"`]\s*:""", code):
            bad.append(f"{rel(p)}:{line_of(code, m.start())}: {snippet(code, m.start())}")
    assert not bad, "a `style:` attribute key is banned (use a class or el.style.<prop> after creating the node):\n" + "\n".join(bad)


def test_js_history_state_only_in_router():
    """Only router.js may touch the history stack, so the back button and the legacy-hash rewrite have one owner."""
    bad = []
    for p in app_files(".js"):
        if p.name == "router.js" and p.parent == STATIC:
            continue
        code = blank_js(p.read_text(encoding="utf-8"))
        for m in re.finditer(r"\bhistory\s*\.\s*(?:pushState|replaceState)\b", code):
            bad.append(f"{rel(p)}:{line_of(code, m.start())}: {snippet(code, m.start())}")
    assert not bad, "history.pushState/replaceState outside app/static/router.js (call navigate() or rewriteLegacyHash()):\n" + "\n".join(bad)


def test_bp5_literals_only_in_core_and_html_skeletons():
    """Blueprint class names live in core.js (el() and blueprint()) and in the HTML skeletons; everything else goes through them."""
    bad = []
    for p in app_files(".js"):
        if p.name == "core.js" and p.parent == STATIC:
            continue
        code = blank_js(p.read_text(encoding="utf-8"))
        for m in re.finditer(r"\bbp5-", code):
            bad.append(f"{rel(p)}:{line_of(code, m.start())}: {snippet(code, m.start())}")
    assert not bad, "bp5-* literal outside app/static/core.js (add a variant to el()/blueprint() there instead):\n" + "\n".join(bad)


# ---------- external URLs (CSP default-src 'self'; the box is tailnet-only and offline-capable) ----------

CDN_HOSTS = ("cdnjs.cloudflare.com", "cdn.jsdelivr.net", "unpkg.com", "fonts.googleapis.com", "fonts.gstatic.com", "code.jquery.com")

# Absolute URLs that may follow src/href/url in JS: the SVG namespace, the code-server link built from location.hostname
# (codeServerUrl), and the ntfy docs link in the phone-setup text. The node-URL regex /^https:\/\// never matches the rules below.
ALLOWED_URL_STARTS = ("http://www.w3.org/2000/svg", "https://${location.hostname}", "https://ntfy.sh")


def test_js_no_absolute_url_for_src_href_url():
    bad = []
    for p in app_files(".js"):
        code = blank_js(p.read_text(encoding="utf-8"))
        for m in re.finditer(r"""\b(?:src|href|url)\s*[:=]\s*['"`](https?://[^'"`\s]*)""", code):
            if m.group(1).startswith(ALLOWED_URL_STARTS):
                continue
            bad.append(f"{rel(p)}:{line_of(code, m.start())}: {snippet(code, m.start())}")
    assert not bad, "absolute http(s) URL assigned to src/href/url (vendor the asset under /static, or extend ALLOWED_URL_STARTS here):\n" + "\n".join(bad)


@pytest.mark.parametrize("name,pattern", [
    ("importScripts", r"\bimportScripts\s*\("),
    ("new Worker(http", r"""\bnew\s+Worker\s*\(\s*['"`](?:https?:)?//"""),
    ("fetch(http", r"""\bfetch\s*\(\s*['"`](?:https?:)?//"""),
], ids=["importScripts", "worker-url", "fetch-url"])
def test_js_no_remote_code_or_calls(name, pattern):
    bad = []
    for p in app_files(".js"):
        code = blank_js(p.read_text(encoding="utf-8"))
        for m in re.finditer(pattern, code):
            bad.append(f"{rel(p)}:{line_of(code, m.start())}: {snippet(code, m.start())}")
    assert not bad, f"{name} is banned (the board only talks to its own origin):\n" + "\n".join(bad)


def test_no_cdn_hosts_anywhere():
    bad = []
    for p in app_files(*TEXT_SUFFIXES):
        text = p.read_text(encoding="utf-8")
        for host in CDN_HOSTS:
            for m in re.finditer(re.escape(host), text):
                bad.append(f"{rel(p)}:{line_of(text, m.start())}: {host}")
    assert not bad, "CDN host referenced (vendor it under app/static/vendor):\n" + "\n".join(bad)


# ---------- CSS ----------

CSS_URL = re.compile(r"""url\(\s*(['"]?)([^'")]*)\1\s*\)""", re.I)


def test_css_no_import():
    bad = []
    for p in app_files(".css"):
        css = blank_css_comments(p.read_text(encoding="utf-8"))
        for m in re.finditer(r"@import\b", css, re.I):
            bad.append(f"{rel(p)}:{line_of(css, m.start())}: {snippet(css, m.start())}")
    assert not bad, "@import is banned (link the stylesheet from the HTML skeleton so the SW shell sees it):\n" + "\n".join(bad)


def test_css_urls_are_local():
    bad = []
    for p in app_files(".css"):
        css = blank_css_comments(p.read_text(encoding="utf-8"))
        for m in CSS_URL.finditer(css):
            target = m.group(2).strip()
            if target.lower().startswith("data:"):
                continue                                    # its own test below
            local = target.startswith("/static/") or target.startswith("#") or not (target.startswith("/") or re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", target))
            if not local:
                bad.append(f"{rel(p)}:{line_of(css, m.start())}: url({target})")
    assert not bad, "url() must point under /static or be relative:\n" + "\n".join(bad)


def test_css_no_data_uris():
    bad = []
    for p in app_files(".css"):
        css = blank_css_comments(p.read_text(encoding="utf-8"))
        for m in re.finditer(r"url\(\s*['\"]?\s*data:", css, re.I):
            bad.append(f"{rel(p)}:{line_of(css, m.start())}: {snippet(css, m.start())}")
    assert not bad, "url(data:...) is banned (CSP default-src 'self' blocks data: images; ship a file under /static):\n" + "\n".join(bad)


# ---------- HTML skeletons ----------

class _Tags(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []          # (tag, {attr: value}, line)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, {k: (v or "") for k, v in attrs}, self.getpos()[0]))


def html_tags(path):
    parser = _Tags()
    parser.feed(path.read_text(encoding="utf-8"))
    parser.close()
    return parser.tags


def test_html_no_inline_script():
    bad = [f"{rel(p)}:{line}: <script> without src" for p in app_files(".html") for tag, attrs, line in html_tags(p)
           if tag == "script" and not attrs.get("src")]
    assert not bad, "every script must be external (CSP default-src 'self'):\n" + "\n".join(bad)


def test_html_no_style_tag_or_attribute():
    bad = []
    for p in app_files(".html"):
        for tag, attrs, line in html_tags(p):
            if tag == "style":
                bad.append(f"{rel(p)}:{line}: <style>")
            if "style" in attrs:
                bad.append(f"{rel(p)}:{line}: <{tag} style=...>")
    assert not bad, "inline styles are blocked by the CSP; use a class in a .css file:\n" + "\n".join(bad)


def test_html_no_event_handler_attributes():
    bad = [f"{rel(p)}:{line}: <{tag} {name}=...>" for p in app_files(".html") for tag, attrs, line in html_tags(p)
           for name in attrs if re.fullmatch(r"on[a-z]+", name)]
    assert not bad, "inline event handlers are blocked by the CSP; attach listeners in a script:\n" + "\n".join(bad)


def test_html_src_and_href_are_local():
    bad = []
    for p in app_files(".html"):
        for tag, attrs, line in html_tags(p):
            for name in ("src", "href"):
                v = attrs.get(name)
                if v is not None and not re.match(r"(#|/(?!/))", v):
                    bad.append(f"{rel(p)}:{line}: <{tag} {name}=\"{v}\">")
    assert not bad, "src/href must start with /static, / or # (never another origin, never //host):\n" + "\n".join(bad)


def test_svg_files_are_inert():
    bad = []
    for p in app_files(".svg"):
        for tag, attrs, line in html_tags(p):
            if tag in ("script", "foreignobject", "style"):
                bad.append(f"{rel(p)}:{line}: <{tag}>")
            for name, v in attrs.items():
                if re.fullmatch(r"on[a-z]+", name) or name == "style":
                    bad.append(f"{rel(p)}:{line}: {name}=...")
                if name in ("href", "xlink:href", "src") and not re.match(r"(#|/(?!/))", v):
                    bad.append(f"{rel(p)}:{line}: {name}=\"{v}\"")
    assert not bad, "SVG files must carry no script, style, handlers or remote references:\n" + "\n".join(bad)


# ---------- visual system: vendored fonts, tokens.css, the skeletons' head (v0.5.2) ----------

FONTS_DIR = STATIC / "vendor" / "fonts"
FONT_FILES = ("jetbrains-mono-latin-wght-normal.woff2", "inter-latin-wght-normal.woff2")
FONT_LICENCES = ("OFL-JetBrainsMono.txt", "OFL-Inter.txt")
THEME_COLOR = "#14181c"


def _tokens_css():
    path = STATIC / "tokens.css"
    assert path.is_file(), "app/static/tokens.css is missing (the palette, font-face and size tokens live there)"
    return blank_css_comments(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", FONT_FILES)
def test_vendored_font_is_a_woff2(name):
    path = FONTS_DIR / name
    assert path.is_file(), f"{rel(path) if path.exists() else 'app/static/vendor/fonts/' + name} is missing"
    head = path.read_bytes()[:4]
    assert head == b"wOF2", f"{name} is not a WOFF2 file (starts with {head!r})"
    assert path.stat().st_size > 10_000, f"{name} is suspiciously small"


@pytest.mark.parametrize("name", FONT_LICENCES)
def test_font_licence_ships_next_to_the_fonts(name):
    path = FONTS_DIR / name
    assert path.is_file(), f"app/static/vendor/fonts/{name} is missing: the OFL requires the licence to travel with the font"
    assert "open font license" in path.read_text(encoding="utf-8").lower(), f"{name} does not look like the SIL Open Font License"


def test_tokens_css_references_both_fonts_through_static_urls():
    css = _tokens_css()
    targets = [m.group(2).strip() for m in CSS_URL.finditer(css)]
    font_targets = [t for t in targets if t.endswith(".woff2")]
    for name in FONT_FILES:
        assert f"/static/vendor/fonts/{name}" in font_targets, f"tokens.css has no url(/static/vendor/fonts/{name})"
    for t in font_targets:
        assert t.startswith("/static/vendor/fonts/"), f"font url outside /static/vendor/fonts: {t}"
        assert (STATIC / t[len("/static/"):]).is_file(), f"tokens.css points at a missing file: {t}"
    assert len(re.findall(r"@font-face\b", css)) >= 2, "expected an @font-face for JetBrains Mono and one for Inter"


def test_tokens_css_has_no_data_uris_or_remote_urls():
    css = _tokens_css()
    assert "data:" not in css.lower(), "tokens.css must not embed data: URIs (CSP default-src 'self')"
    assert not re.search(r"https?:|//[A-Za-z0-9.-]+\.[a-z]{2,}", css, re.I), "tokens.css must not reference another origin"


@pytest.mark.parametrize("page", ["index.html", "term.html"])
def test_html_head_carries_tokens_theme_color_and_font_preloads(page):
    tags = html_tags(STATIC / page)
    sheets = [a["href"] for t, a, _ in tags if t == "link" and a.get("rel") == "stylesheet"]
    assert "/static/tokens.css" in sheets and "/static/style.css" in sheets, f"{page}: tokens.css and style.css must both be linked ({sheets})"
    assert sheets.index("/static/tokens.css") < sheets.index("/static/style.css"), f"{page}: tokens.css must come before style.css ({sheets})"
    themes = [a.get("content", "").lower() for t, a, _ in tags if t == "meta" and a.get("name") == "theme-color"]
    assert themes == [THEME_COLOR], f"{page}: theme-color must be {THEME_COLOR} (found {themes})"
    preloads = {a.get("href"): a for t, a, _ in tags if t == "link" and a.get("rel") == "preload"}
    for name in FONT_FILES:
        href = f"/static/vendor/fonts/{name}"
        assert href in preloads, f"{page}: missing <link rel=preload> for {href}"
        a = preloads[href]
        assert a.get("as") == "font" and a.get("type") == "font/woff2", f"{page}: preload for {name} needs as=font type=font/woff2"
        assert "crossorigin" in a, f"{page}: font preload for {name} needs the crossorigin attribute (fonts are fetched in CORS mode)"


# ---------- v0.5.3 shell: the index.html skeleton, the script and stylesheet order, demo fixtures ----------
# Written against the v0.5.3 contract (plan "index.html skeleton and script order"): a test that fails because a slice has not
# landed yet names the missing piece in its assertion message.

import datetime  # noqa: E402
import json  # noqa: E402
import subprocess  # noqa: E402
from collections import Counter  # noqa: E402

INDEX = STATIC / "index.html"
SKELETON_IDS = ("topbar", "sidebar", "main", "banner", "page", "dock", "bnav", "drawer", "sheet", "helpdlg", "modal", "toasts")
SCRIPT_ORDER = ["/static/" + n for n in (            # v0.5.3 contract plus keymap.js and palette.js (v0.5.3b)
    "core.js", "components.js", "keymap.js", "live.js", "launcher.js", "palette.js", "shell.js", "router.js",
    "pages/home.js", "pages/inbox.js", "pages/tasks.js", "pages/agents.js", "pages/settings.js", "pages/search.js",
    "pages/session.js", "pages/placeholders.js", "main.js")]
STYLE_ORDER = ["/static/vendor/blueprint/blueprint.css", "/static/vendor/blueprint/blueprint-icons.css", "/static/tokens.css",
               "/static/style.css", "/static/shell.css", "/static/pages.css"]
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class _Node:
    def __init__(self, tag, attrs, line):
        self.tag, self.attrs, self.line = tag, attrs, line
        self.children = []
        self.text = ""

    @property
    def classes(self):
        return self.attrs.get("class", "").split()

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()


class _TreeBuilder(HTMLParser):
    """A minimal DOM: enough to check nesting, order and emptiness of the skeleton (void tags never open a level)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {}, 0)
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, {k: (v or "") for k, v in attrs}, self.getpos()[0])
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].text += data


def html_tree_from(text):
    builder = _TreeBuilder()
    builder.feed(text)
    builder.close()
    return builder.root


def html_tree(path):
    return html_tree_from(path.read_text(encoding="utf-8"))


def _by_tag(node, tag):
    return [n for n in node.walk() if n.tag == tag]


def test_html_tree_builder_nests_and_skips_void_tags():
    root = html_tree_from("<html><head><meta charset=utf-8><link rel=x></head><body><div id=a><dialog id=d> </dialog><p>t</p></div></body></html>")
    body = _by_tag(root, "body")[0]
    div = body.children[0]
    assert [c.tag for c in div.children] == ["dialog", "p"] and div.children[0].text.strip() == ""
    assert [c.tag for c in _by_tag(root, "head")[0].children] == ["meta", "link"]


def test_index_skeleton_ids_are_present_exactly_once():
    ids = [a["id"] for _, a, _ in html_tags(INDEX) if "id" in a]
    for want in SKELETON_IDS:
        assert ids.count(want) == 1, f"app/static/index.html must carry exactly one #{want} (found {ids.count(want)})"
    assert len(ids) == len(set(ids)), f"duplicate ids in app/static/index.html: {[i for i, n in Counter(ids).items() if n > 1]}"


def test_index_skeleton_nesting_matches_the_contract():
    """body.bp5-dark[data-shell][data-page] > a.skip + #app{header#topbar, aside#sidebar, main#main{#banner,#page}, aside#dock.hidden}
    + nav#bnav + dialog#drawer + dialog#sheet + dialog#helpdlg + div#modal.hidden + div#toasts; scripts after all of it."""
    body = _by_tag(html_tree(INDEX), "body")[0]
    assert "bp5-dark" in body.classes and "data-shell" in body.attrs and "data-page" in body.attrs, \
        "<body> needs class bp5-dark and the data-shell / data-page attributes (the shell and router fill them in)"
    kids = [n for n in body.children if n.tag not in ("script", "noscript")]
    assert [(n.tag, n.attrs.get("id")) for n in kids] == [
        ("a", None), ("div", "app"), ("nav", "bnav"), ("dialog", "drawer"), ("dialog", "sheet"), ("dialog", "helpdlg"),
        ("div", "modal"), ("div", "toasts")], "body children are not the contract skeleton"
    skip, app, bnav, _drawer, _sheet, _help, modal, toasts = kids
    assert "skip" in skip.classes and skip.attrs.get("href") == "#main", "the first body child is the a.skip link to #main"
    assert [(n.tag, n.attrs.get("id")) for n in app.children] == [
        ("header", "topbar"), ("aside", "sidebar"), ("main", "main"), ("aside", "dock")], "#app children"
    topbar, sidebar, main, dock = app.children
    assert sidebar.attrs.get("aria-label") == "Projects", "aside#sidebar needs aria-label=Projects"
    assert main.attrs.get("tabindex") == "-1", "main#main needs tabindex=-1 (the router focuses it after a route change)"
    assert [(n.tag, n.attrs.get("id")) for n in main.children] == [("div", "banner"), ("div", "page")], "#main children"
    assert "hidden" in dock.classes, "aside#dock starts hidden"
    assert "hidden" in modal.classes, "the legacy div#modal starts hidden"
    assert toasts.attrs.get("role") == "status" and toasts.attrs.get("aria-live") == "polite", "div#toasts is a polite live region"
    assert bnav.attrs.get("aria-label"), "nav#bnav needs an aria-label"
    last_non_script = max(i for i, n in enumerate(body.children) if n.tag not in ("script", "noscript"))
    first_script = min(i for i, n in enumerate(body.children) if n.tag == "script")
    assert first_script > last_non_script, "scripts belong at the end of <body>, after the skeleton"


def test_index_scripts_follow_the_contract_order_exactly():
    scripts = [a.get("src") for t, a, _ in html_tags(INDEX) if t == "script"]
    assert scripts == SCRIPT_ORDER, "index.html script order differs from the contract (v0.5.3 plus v0.5.3b):\n  got      " + "\n  ".join(map(str, scripts)) \
        + "\n  expected " + "\n  ".join(SCRIPT_ORDER)
    for t, a, line in html_tags(INDEX):
        if t == "script":
            assert "async" not in a and a.get("type", "text/javascript") in ("", "text/javascript"), \
                f"app/static/index.html:{line}: classic scripts only (no async, no type=module)"


def test_index_stylesheets_follow_the_contract_order_exactly():
    sheets = [a["href"] for t, a, _ in html_tags(INDEX) if t == "link" and a.get("rel") == "stylesheet"]
    assert sheets == STYLE_ORDER, f"index.html stylesheet order differs from the v0.5.3 contract: {sheets}"


def test_index_scripts_and_stylesheets_exist_on_disk():
    missing = [p for p in SCRIPT_ORDER + STYLE_ORDER if not (STATIC / p[len("/static/"):]).is_file()]
    assert not missing, f"index.html references files that do not exist yet: {missing}"


def test_index_dialogs_are_empty_in_the_html():
    """<dialog>s are filled by el() at runtime (drawer, sheet and help); static markup inside one would bypass the CSP-safe builders."""
    dialogs = _by_tag(html_tree(INDEX), "dialog")
    assert {d.attrs.get("id") for d in dialogs} >= {"drawer", "sheet", "helpdlg"}, "expected dialog#drawer, #sheet and #helpdlg"
    bad = [f"app/static/index.html:{d.line}: <dialog id={d.attrs.get('id')}> has content" for d in dialogs
           if d.children or d.text.strip()]
    assert not bad, "every <dialog> in index.html must be empty (build its content with el()):\n" + "\n".join(bad)


# ---------- demo fixtures (app/static/demo/*.json, read by api() when ?demo=1 or ccboard:demo=1) ----------

DEMO_DIR = STATIC / "demo"
DEMO_FILES = ("state.json", "search.json", "tree.json", "series.json", "memory.json")
SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+--[A-Za-z0-9_-]+--[A-Za-z0-9_-]+$")
KANBAN = ("in_progress", "needs_you", "done", "pr", "merged")
DEMO_HEADERS = {"Tailscale-User-Login": "alice@example.com"}


def demo_json(name):
    return json.loads((DEMO_DIR / name).read_text(encoding="utf-8"))


def fleet_sessions(state):
    """Every live session in a state payload with the (project, repo-key) block it sits in; repo-key is 'root' for the project folder."""
    out = []
    for p in state["projects"]:
        for r in p["repos"]:
            out += [(p["name"], r["name"], s) for s in r["sessions"]]
        if p.get("root"):
            out += [(p["name"], "root", s) for s in p["root"]["sessions"]]
        out += [(p["name"], s["repo"], s) for s in p["orphan_sessions"]]
    return out


def demo_epoch(state):
    return state["demo"]["epoch"]


def parse_iso(s):
    return datetime.datetime.fromisoformat(s).timestamp()


@pytest.mark.parametrize("name", DEMO_FILES)
def test_demo_fixture_exists_and_parses(name):
    path = DEMO_DIR / name
    assert path.is_file(), f"app/static/demo/{name} is missing (api() reads it in demo mode)"
    assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict)


def test_demo_fixtures_carry_no_host_specific_data():
    bad = []
    for name in DEMO_FILES:
        text = (DEMO_DIR / name).read_text(encoding="utf-8")
        for needle in ("/Users/", "/tmp/", "/private/", "gmail.com", "rpandox", "ccb-fix"):
            if needle in text:
                bad.append(f"app/static/demo/{name}: contains {needle!r}")
    assert not bad, "fixtures must be anonymous:\n" + "\n".join(bad)


def test_demo_state_has_the_seven_sessions_in_the_stated_states():
    state = demo_json("state.json")
    sessions = fleet_sessions(state)
    assert len(sessions) == 7, f"expected 7 live sessions, found {len(sessions)}"
    assert Counter(s["state"] for _, _, s in sessions) == Counter(
        {"waiting": 1, "working": 2, "idle": 1, "done": 1, "errored": 1, "ended": 1})
    names = [s["tmux"] for _, _, s in sessions]
    assert len(set(names)) == 7 and all(SESSION_NAME_RE.match(n) for n in names), names
    for project, repo, s in sessions:
        assert s["tmux"] == f"{project}--{repo}--{s['name']}", f"{s['tmux']} sits in {project}/{repo} with name {s['name']}"
    attn = {s["state"] for _, _, s in sessions if s["needs_attention"]}
    assert attn == {"waiting", "done", "errored"} and sum(1 for _, _, s in sessions if s["needs_attention"]) == 3
    codex = [s for _, _, s in sessions if s["agent"] == "codex"]
    assert len(codex) == 1 and codex[0]["launcher"] == "codex" and codex[0]["state"] == "working"
    assert codex[0]["stats"]["model"] == "gpt-5.5"
    for _, _, s in sessions:
        assert {"model", "model_id", "context_pct", "cost_usd"} <= set(s["stats"]), s["tmux"]
        assert s["last_prompt"] and s["last_message"] and s["created"] and s["state_at"], s["tmux"]
    assert {s["stats"]["context_pct"] for _, _, s in sessions} >= {23, 61, 88}
    assert any(s["stats"]["model"] == "Opus 5" for _, _, s in sessions)


def test_demo_state_session_ages_spread_from_hours_to_nine_days():
    state = demo_json("state.json")
    now = demo_epoch(state)
    ages = sorted((now - s["created"]) / 86400 for _, _, s in fleet_sessions(state))
    assert round(ages[0], 1) == 0.4 and round(ages[-1], 1) == 8.8, ages
    assert len({round(a) for a in ages}) >= 5, f"ages should spread over the range: {ages}"
    for _, _, s in fleet_sessions(state):
        assert parse_iso(s["state_at"]) <= now + 1, f"{s['tmux']}: state_at is after the fixture's clock"


def test_demo_state_projects_three_active_and_six_older():
    state = demo_json("state.json")
    by_name = {p["name"]: p for p in state["projects"]}
    assert len(by_name) == 9
    active = {"phasezero": {"website", "NestJs-Ecommerce-Backend"}, "ccboard": {"ccboard"}, "petroit": {"api"}}
    for name, repos in active.items():
        assert {r["name"] for r in by_name[name]["repos"]} == repos, name
    with_sessions = {p for p, _, _ in fleet_sessions(state)}
    assert with_sessions == set(active), f"sessions belong to {with_sessions}"
    older = set(by_name) - set(active)
    assert len(older) == 6 and all(by_name[n]["repos"] for n in older), older
    assert any(by_name["phasezero"]["root"]["sessions"]), "one session lives in the project folder (repo 'root')"


def test_demo_state_pending_permission_belongs_to_the_waiting_session():
    state = demo_json("state.json")
    waiting = [s for _, _, s in fleet_sessions(state) if s["state"] == "waiting"]
    perms = state["pending_permissions"]
    assert len(waiting) == 1 and len(perms) == 1
    assert perms[0]["tmux_name"] == waiting[0]["tmux"] and perms[0]["tool_name"] == "Bash" and "npm test" in perms[0]["summary"]
    assert {"id", "created_at"} <= set(perms[0])


def test_demo_state_tasks_fill_every_kanban_column_consistently():
    from app import tasks as tasks_mod
    state = demo_json("state.json")
    live = {s["tmux"]: s for _, _, s in fleet_sessions(state)}
    listed = state["tasks"]
    assert Counter(t["column"] for t in listed) == Counter({c: 1 for c in KANBAN}), [t["column"] for t in listed]
    assert [t["id"] for t in listed] == sorted((t["id"] for t in listed), reverse=True), "db.tasks() lists newest id first"
    for t in listed:
        sess = live.get(t["tmux"])
        row = {"pr_state": t["pr_state"], "pr_url": t["pr_url"], "status": "open", "archived_at": None}
        assert tasks_mod.derive_status(row, sess) == t["column"], f"task {t['id']} ({t['slug']}): column disagrees with derive_status"
        if t["session"] is not None:
            assert sess is not None and t["session"]["state"] == sess["state"], f"task {t['id']}: session sub-object disagrees"
        assert t["tmux"] == f"{t['project']}--{t['repo']}--t-{t['slug']}"
    by_col = {t["column"]: t for t in listed}
    assert by_col["in_progress"]["session"]["state"] == "working" and by_col["needs_you"]["session"]["needs_attention"] is True
    pr, merged = by_col["pr"], by_col["merged"]
    assert pr["pr_url"] and pr["pr_number"] and pr["pr_state"] == "OPEN" and pr["ci"]["bucket"] in ("pass", "fail", "pending", "none")
    assert pr["pr"]["review"] and pr["ci"]["checks"]
    assert merged["pr_state"] == "MERGED" and merged["pr_number"] and merged["ci"]["bucket"] == "pass"
    assert any(t["overlap"] for t in listed), "one task shows the overlap warning"


def test_demo_state_jobs_runs_and_the_rest_of_the_fleet_view():
    state = demo_json("state.json")
    now = demo_epoch(state)
    assert len(state["jobs"]) == 2 and len(state["runs"]) == 3
    assert {r["job_id"] for r in state["runs"]} <= {j["id"] for j in state["jobs"]}
    assert any(j["enabled"] and j["cron"] for j in state["jobs"]) and any(not j["enabled"] for j in state["jobs"])
    rl = state["usage"]["value"]
    assert rl["five_hour"]["used_percentage"] == 42 and rl["seven_day"]["used_percentage"] == 71
    for key in ("five_hour", "seven_day"):
        assert now < rl[key]["resets_at"] < now + 8 * 86400, f"{key}.resets_at must lie ahead of the fixture clock"
    assert "spend_limit" not in rl, "a Max subscription has no spend limit pill"
    blk = state["block"]["value"]
    assert blk["available"] and blk["active"] and blk["remaining_minutes"] > 0
    assert {"phasezero", "ccboard", "petroit"} <= set(state["cost"]["value"]["projects"])
    health = state["health"]
    assert health["host"] and health["disk"]["pct"] and health["mem"]["pct"] and health["uptime_s"]
    assert state["backup"]["status"] == "ok" and state["backup"]["at"]
    nodes = state["nodes"]["value"]
    assert len(nodes) == 1 and nodes[0]["online"] is True and nodes[0]["name"]
    assert state["rate_limited"] is None and state["version"] == "demo" and state["tmux_down"] is False
    c = state["claude"]
    assert c["installed"] is True and c["loggedIn"] is True and c["email"] and c["subscriptionType"] == "max"
    assert state["usage_codex"]["value"]["primary"]["window_minutes"] == 10080
    assert state["agents"]["codex"]["installed"] is True
    assert state["login"]["running"] is False and state["scheduler"]["known"] is True


def _missing_keys(real, fixture, path="state"):
    """Keys the app's real payload carries that the fixture lacks (dicts recurse; lists compare their first element)."""
    out = []
    if isinstance(real, dict) and isinstance(fixture, dict):
        for k, v in real.items():
            if k not in fixture:
                out.append(f"{path}.{k}")
            else:
                out += _missing_keys(v, fixture[k], f"{path}.{k}")
    elif isinstance(real, list) and isinstance(fixture, list) and real and fixture:
        out += _missing_keys(real[0], fixture[0], path + "[0]")
    return out


def test_demo_state_covers_every_key_the_live_state_endpoint_returns(lite_client, projects_dir, fake_tmux):
    """The fixture is a superset of a real GET /api/state, rows included, so demo mode cannot render with a hole where the app
    would have data. When /api/state grows a key, this test says which one to add to app/static/demo/state.json."""
    from app import hooks, main
    repo = projects_dir / "shop" / "api"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(repo), check=True, capture_output=True)
    name = "shop--api--s1"
    fake_tmux["sessions"][name] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%1", "command": "claude",
                                   "path": str(repo), "pid": 1, "env": {}}
    main.db.add_session(tmux_name=name, project="shop", repo="api", name="s1", launcher="claude", cmd="claude",
                        claude_session_id="6f1d2c9e-3b7a-4c1e-9a52-1d0e5b7c8a01", add_dirs=[])
    hooks.apply(main.db, name, "statusline", {"model": {"display_name": "Opus 5", "id": "claude-opus-5"},
                                              "context_window": {"used_percentage": 12, "context_window_size": 200000},
                                              "cost": {"total_cost_usd": 1.0, "total_lines_added": 1, "total_lines_removed": 0},
                                              "version": "2.1.288"})
    hooks.apply(main.db, name, "UserPromptSubmit", {"session_id": "6f1d2c9e-3b7a-4c1e-9a52-1d0e5b7c8a01", "prompt": "hi"})
    main.db.task_add(project="shop", repo="api", slug="t", title="t", prompt="p", branch="worktree-t", base="main",
                     worktree=str(repo / ".claude" / "worktrees" / "t"), tmux_name="shop--api--t-t", claude_session_id=None)
    jid = main.db.job_add(project="shop", repo="api", name="j", prompt="p", cron=None, permission_mode="acceptEdits", max_turns=5,
                          max_budget_usd=None, args=None, timeout_s=None, enabled=1, batch_id=None, next_run_at=None)
    main.db.run_finish(main.db.run_start(jid), status="ok", result="r")
    main._invalidate_scan()
    real = lite_client.get("/api/state", headers=DEMO_HEADERS).json()
    assert [s for _, _, s in fleet_sessions(real)], "the probe session did not show up in the real state"
    missing = _missing_keys(real, demo_json("state.json"))
    assert not missing, "app/static/demo/state.json lacks keys the live /api/state returns:\n  " + "\n  ".join(missing)


def test_demo_search_fixture_matches_api_search():
    search = demo_json("search.json")
    assert search["q"] == "auth" and len(search["results"]) == 3
    for hit in search["results"]:
        assert {"session_id", "cwd", "ts", "kind", "file", "line", "snippet", "project", "repo", "tmux"} <= set(hit)
        assert hit["kind"] in ("user", "assistant", "subagent") and "[auth]" in hit["snippet"]
    assert any(h["tmux"] for h in search["results"]) and any(h["tmux"] is None for h in search["results"])


def test_demo_tree_fixture_matches_the_tree_endpoint_shape():
    tree = demo_json("tree.json")
    assert tree["path"] == "" and tree["project"] and tree["repo"] and isinstance(tree["entries"], list) and tree["entries"]
    for e in tree["entries"]:
        assert {"name", "type", "status", "dirty", "has_children", "ignored"} <= set(e), e
        assert e["type"] in ("dir", "file") and e["status"] in (None, "M", "?") and isinstance(e["has_children"], bool)
        assert e["dirty"] == (e["status"] is not None)
    assert {e["status"] for e in tree["entries"]} == {None, "M", "?"}
    assert any(e["type"] == "dir" and e["has_children"] for e in tree["entries"])
    assert any(e["type"] == "file" and not e["has_children"] for e in tree["entries"])


def test_demo_series_fixture_matches_the_series_endpoint_shape():
    s = demo_json("series.json")
    assert s["step"] == 300 and s["meta"] == {} and s["since"] and s["until"]
    assert len(s["t"]) == 48 and all(b - a == 300 for a, b in zip(s["t"], s["t"][1:]))
    assert set(s["series"]) == {"rl_5h:claude", "rl_7d:claude"}
    for key, values in s["series"].items():
        assert len(values) == 48, key
        assert all(v is None or 0 <= v <= 100 for v in values), key
    assert max(v for v in s["series"]["rl_5h:claude"][:20] if v is not None) == 100, "the 5h series shows one limit-hit episode"
    assert s["series"]["rl_5h:claude"][-1] == 42 and s["series"]["rl_7d:claude"][-1] == 71, "the last point matches the state's pills"


def test_demo_memory_fixture_matches_the_memory_envelope():
    m = demo_json("memory.json")
    assert m["state"] == "up" and len(m["items"]) == 5
    for o in m["items"]:
        assert {"id", "type", "title", "subtitle", "created_at_epoch", "concepts", "files_read"} <= set(o), o
        assert isinstance(o["concepts"], list) and o["concepts"] and isinstance(o["files_read"], list)
        assert isinstance(o["created_at_epoch"], int)
    assert len({o["type"] for o in m["items"]}) >= 4 and len({o["id"] for o in m["items"]}) == 5


@pytest.mark.parametrize("name", DEMO_FILES)
def test_demo_files_are_served_with_the_static_headers(lite_client, name):
    r = lite_client.get(f"/static/demo/{name}", headers=DEMO_HEADERS)
    assert r.status_code == 200, f"/static/demo/{name} is not served"
    assert r.json() == demo_json(name)
    assert r.headers["Content-Security-Policy"] == "default-src 'self'; frame-ancestors 'none'"
    assert lite_client.get(f"/static/demo/{name}").status_code == 403       # no Tailscale identity: nothing is served


def test_demo_files_are_absent_from_the_sw_shell_and_the_asset_version(lite_client):
    from app import main
    assert not any("demo" in p.relative_to(main.STATIC_DIR).parts for p in main.static_files())
    assert not any("/demo/" in p for p in main.shell_paths())
    sw = lite_client.get("/sw.js", headers=DEMO_HEADERS).text
    m = re.search(r"JSON\.parse\('([^']*)'\)", sw)
    assert m and not any("demo" in p for p in json.loads(m.group(1))), "the served worker precaches a demo fixture"
