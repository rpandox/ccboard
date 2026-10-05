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
    for must in ("app/static/index.html", "app/static/term.html", "app/static/sw.js", "app/static/core.js", "app/static/style.css",
                 "app/static/termkit.js", "app/static/term.js", "app/static/term.css"):
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
SKELETON_IDS = ("topbar", "sidebar", "main", "banner", "page", "dock", "bnav", "drawer", "sheet", "helpdlg", "toasts")
SCRIPT_ORDER = ["/static/" + n for n in (            # v0.5.3 contract plus keymap.js and palette.js (v0.5.3b), pages/widgets.js (v0.5.5), tree.js and pages/project.js (v0.5.6), termkit.js (v0.5.9: the dock), pages/quad.js (v0.5.9)
    "core.js", "components.js", "keymap.js", "live.js", "termkit.js", "launcher.js", "tree.js", "charts.js", "dnd.js", "palette.js", "shell.js", "router.js",
    "pages/home.js", "pages/inbox.js", "pages/widgets.js", "pages/tasks.js", "pages/project.js", "pages/agents.js", "pages/settings.js", "pages/search.js",
    "pages/session.js", "pages/usage.js", "pages/quad.js", "pages/placeholders.js", "main.js")]
STYLE_ORDER = ["/static/vendor/blueprint/blueprint.css", "/static/vendor/blueprint/blueprint-icons.css", "/static/tokens.css",
               "/static/style.css", "/static/shell.css", "/static/pages.css", "/static/charts.css", "/static/termkit.css"]
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
    + nav#bnav + dialog#drawer + dialog#sheet + dialog#helpdlg + div#toasts; scripts after all of it. (v0.5.13: the legacy div#modal is gone: the diff viewer is a sheet.)"""
    body = _by_tag(html_tree(INDEX), "body")[0]
    assert "bp5-dark" in body.classes and "data-shell" in body.attrs and "data-page" in body.attrs, \
        "<body> needs class bp5-dark and the data-shell / data-page attributes (the shell and router fill them in)"
    kids = [n for n in body.children if n.tag not in ("script", "noscript")]
    assert [(n.tag, n.attrs.get("id")) for n in kids] == [
        ("a", None), ("div", "app"), ("nav", "bnav"), ("dialog", "drawer"), ("dialog", "sheet"), ("dialog", "helpdlg"),
        ("div", "toasts")], "body children are not the contract skeleton"
    skip, app, bnav, _drawer, _sheet, _help, toasts = kids
    assert "skip" in skip.classes and skip.attrs.get("href") == "#main", "the first body child is the a.skip link to #main"
    assert [(n.tag, n.attrs.get("id")) for n in app.children] == [
        ("header", "topbar"), ("aside", "sidebar"), ("main", "main"), ("aside", "dock")], "#app children"
    topbar, sidebar, main, dock = app.children
    assert sidebar.attrs.get("aria-label") == "Projects", "aside#sidebar needs aria-label=Projects"
    assert main.attrs.get("tabindex") == "-1", "main#main needs tabindex=-1 (the router focuses it after a route change)"
    assert [(n.tag, n.attrs.get("id")) for n in main.children] == [("div", "banner"), ("div", "page")], "#main children"
    assert "hidden" in dock.classes, "aside#dock starts hidden"
    assert toasts.attrs.get("role") == "status" and toasts.attrs.get("aria-live") == "polite", "div#toasts is a polite live region"
    assert bnav.attrs.get("aria-label"), "nav#bnav needs an aria-label"
    last_non_script = max(i for i, n in enumerate(body.children) if n.tag not in ("script", "noscript"))
    first_script = min(i for i, n in enumerate(body.children) if n.tag == "script")
    assert first_script > last_non_script, "scripts belong at the end of <body>, after the skeleton"


def test_the_legacy_modal_is_gone_from_the_page_the_styles_and_the_scripts():
    """v0.5.13: the diff / PR viewer, the import and batch forms are <dialog> sheets built with el(); nothing draws into a div#modal, no rule styles it, no script opens it."""
    assert 'id="modal"' not in INDEX.read_text(encoding="utf-8"), "app/static/index.html still has a div#modal"
    for name in ("style.css", "shell.css", "pages.css", "term.css", "charts.css"):
        css = (STATIC / name).read_text(encoding="utf-8")
        assert not re.search(r"#modal\s*[{,.]|\.modal-box", css), f"app/static/{name} still styles the legacy #modal"
    legacy = re.compile(r"""['"]#modal['"]|getElementById\(['"]modal['"]\)|\bcloseModal\b|\bmodal-box\b""")
    for js in sorted(STATIC.glob("*.js")) + sorted((STATIC / "pages").glob("*.js")):
        assert not legacy.search(js.read_text(encoding="utf-8")), f"{js.relative_to(STATIC)} still reaches for the legacy #modal"


def test_index_scripts_follow_the_contract_order_exactly():
    scripts = [a.get("src") for t, a, _ in html_tags(INDEX) if t == "script"]
    assert scripts == SCRIPT_ORDER, "index.html script order differs from the contract (v0.5.3 plus v0.5.3b, v0.5.5 and v0.5.6):\n  got      " + "\n  ".join(map(str, scripts)) \
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
DEMO_FILES = ("state.json", "search.json", "tree.json", "file.json", "series.json", "series_events.json", "usage_summary.json", "memory.json",
              "agents.json")
SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+--[A-Za-z0-9_-]+--[A-Za-z0-9_-]+$")
KANBAN = ("backlog", "in_progress", "needs_you", "done", "pr", "merged")
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


def test_demo_state_has_the_eight_sessions_in_the_stated_states():
    """The five-state fleet of v0.5.3 plus petroit--api--s3 (v0.5.5): a second session that waits, with a question and no pending permission."""
    state = demo_json("state.json")
    sessions = fleet_sessions(state)
    assert len(sessions) == 8, f"expected 8 live sessions, found {len(sessions)}"
    assert Counter(s["state"] for _, _, s in sessions) == Counter(
        {"waiting": 2, "working": 2, "idle": 1, "done": 1, "errored": 1, "ended": 1})
    names = [s["tmux"] for _, _, s in sessions]
    assert len(set(names)) == 8 and all(SESSION_NAME_RE.match(n) for n in names), names
    assert len({s["row_id"] for _, _, s in sessions}) == 8 and len({s["pane_id"] for _, _, s in sessions}) == 8, "row ids and panes are unique"
    for project, repo, s in sessions:
        assert s["tmux"] == f"{project}--{repo}--{s['name']}", f"{s['tmux']} sits in {project}/{repo} with name {s['name']}"
    attn = {s["state"] for _, _, s in sessions if s["needs_attention"]}
    assert attn == {"waiting", "done", "errored"} and sum(1 for _, _, s in sessions if s["needs_attention"]) == 4
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


def test_demo_state_pending_permission_belongs_to_one_of_the_two_waiting_sessions():
    state = demo_json("state.json")
    waiting = {s["tmux"]: s for _, _, s in fleet_sessions(state) if s["state"] == "waiting"}
    perms = state["pending_permissions"]
    assert len(waiting) == 2 and len(perms) == 1
    assert perms[0]["tmux_name"] in waiting and perms[0]["tool_name"] == "Bash" and "npm test" in perms[0]["summary"]
    assert {"id", "created_at"} <= set(perms[0])
    question = waiting[next(n for n in waiting if n != perms[0]["tmux_name"])]
    assert question["last_message"].rstrip().endswith("?"), "the other waiting session asks a question (the inbox card kind 'question')"
    assert question["needs_attention"] is True


def test_demo_state_carries_the_home_page_cases():
    """v0.5.5: what the Home rows, the inbox cards and the limit banner draw, so ?demo=1 shows every chip and card kind."""
    state = demo_json("state.json")
    now = demo_epoch(state)
    sessions = {s["tmux"]: s for _, _, s in fleet_sessions(state)}
    costs = sorted(s["stats"]["cost_usd"] for s in sessions.values() if s["stats"].get("cost_usd") is not None)
    assert 12.5 in costs and 140.0 in costs, f"a cost-warn and a cost-bad session: {costs}"
    assert any(10 <= c < 100 for c in costs) and any(c >= 100 for c in costs) and any(c < 10 for c in costs)
    ctx = {s["stats"]["context_pct"] for s in sessions.values()}
    assert any(60 <= c < 85 for c in ctx) and any(c >= 85 for c in ctx) and any(c < 60 for c in ctx), "ctx-hi, ctx-crit and a plain meter"
    assert any("/.claude/worktrees/" in s["path"] for s in sessions.values()), "a worktree badge"
    blocked = [s for s in sessions.values() if ((s.get("flags") or {}).get("registry") or {}).get("job")]
    assert len(blocked) == 1 and blocked[0]["flags"]["subagents"] == 2
    job = blocked[0]["flags"]["registry"]["job"]
    assert job == {"state": "blocked", "tempo": "blocked", "needs": "approve the plan", "suggested_reply": "approve"}
    rl = state["rate_limited"]
    assert rl["at"] and rl["value"]["kind"] == "5h" and rl["value"]["session"] in sessions, "an active 5h episode on one session"
    assert rl["value"]["resets_at"] > now and len(rl["value"]["message"]) <= 200
    assert rl["value"]["resets_at"] == state["usage"]["value"]["five_hour"]["resets_at"], "the banner and the topbar pill agree on the reset"
    jobs = [j for j in state["jobs"] if j["enabled"] and j["next_run_at"]]
    assert len(jobs) >= 3, "the schedules strip shows the next three runs"
    assert all(parse_iso(j["next_run_at"]) > now for j in jobs)


def test_demo_state_tasks_fill_every_kanban_column_consistently():
    from app import tasks as tasks_mod
    state = demo_json("state.json")
    live = {s["tmux"]: s for _, _, s in fleet_sessions(state)}
    rows = {s["row_id"]: s for _, _, s in fleet_sessions(state)}
    listed = state["tasks"]
    cols = Counter(t["column"] for t in listed)
    assert set(cols) == set(KANBAN), [t["column"] for t in listed]
    assert cols == Counter({"backlog": 4, "in_progress": 2, "needs_you": 3, "done": 4, "pr": 1, "merged": 1}), cols     # v0.5.15 added a chain of three, a failed task, a held question and a pending close; v0.5.16 a Codex run's task card
    assert [t["id"] for t in listed] == sorted((t["id"] for t in listed), reverse=True), "db.tasks() lists newest id first"
    for t in listed:
        started = t["phase"] not in ("backlog", "queued")
        sess = rows.get(t["session_row"]) if t["session_row"] is not None else live.get(t["tmux"])
        row = {"pr_state": t["pr_state"], "pr_url": t["pr_url"], "status": "open", "archived_at": None, "phase": t["phase"]}
        assert tasks_mod.derive_status(row, sess if started else None) == t["column"], f"task {t['id']} ({t['slug']}): column disagrees with derive_status"
        if t["session"] is not None:
            assert sess is not None and t["session"]["state"] == sess["state"], f"task {t['id']}: session sub-object disagrees"
        if t["mode"] == "worktree" and started:
            kind = "j" if t["title"].startswith("[") else "t"           # a scheduled run's card is named <project>--<repo>--j-<slug> (scheduler.run_job)
            assert t["tmux"] == f"{t['project']}--{t['repo']}--{kind}-{t['slug']}"
    by_id = {t["id"]: t for t in listed}
    assert by_id[5]["session"]["state"] == "working" and by_id[4]["session"]["needs_attention"] is True
    by_col = {t["column"]: t for t in listed if t["mode"] == "worktree"}
    pr, merged = by_col["pr"], by_col["merged"]
    assert pr["pr_url"] and pr["pr_number"] and pr["pr_state"] == "OPEN" and pr["ci"]["bucket"] in ("pass", "fail", "pending", "none")
    assert pr["pr"]["review"] and pr["ci"]["checks"]
    assert merged["pr_state"] == "MERGED" and merged["pr_number"] and merged["ci"]["bucket"] == "pass"
    assert any(t["overlap"] for t in listed), "one task shows the overlap warning"


def test_demo_state_has_a_backlog_task_and_a_task_handed_to_a_running_session():
    """v0.5.14a: a task before any session (Backlog column, Start / Send to session) and a task that runs in an existing session (the 'in <session>' chip)."""
    state = demo_json("state.json")
    rows = {s["row_id"]: s for _, _, s in fleet_sessions(state)}
    owner = {s["tmux"]: (p, r) for p, r, s in fleet_sessions(state)}
    by_id = {t["id"]: t for t in state["tasks"]}
    backlog = [t for t in state["tasks"] if t["phase"] == "backlog"]
    assert len(backlog) == 2, "two cards in the Backlog column: one in a repo with no ready session, one in a repo whose only ready session gets the one-tap button"
    repos = {(p["name"], r["name"]) for p in state["projects"] for r in p["repos"]}
    ready = {(p, r) for p, r, s in fleet_sessions(state) if s["state"] in ("idle", "done", "waiting") and s["agent"] == "claude" and s["tmux"] not in {x["tmux_name"] for x in state["pending_permissions"]}}
    for b in backlog:
        assert (b["tmux"], b["branch"], b["worktree"], b["session"], b["session_row"]) == ("", "", "", None, None)
        assert b["mode"] == "worktree" and b["column"] == "backlog" and b["agent"] == "claude"
        assert (b["project"], b["repo"]) in repos, "a backlog task targets a real repo (the project folder is not a git repo in the demo)"
        assert b["created_at"] and b["title"] and b["slug"]
        assert b["prompt"] and len(b["prompt"]) <= 600 and b["prompt_len"] == len(b["prompt"]), "a backlog row carries the head of its prompt (<= 600 chars) and the full length"
    assert {(b["project"], b["repo"]) for b in backlog} == {("phasezero", "website"), ("ccboard", "ccboard")}
    assert ("ccboard", "ccboard") in ready and ("phasezero", "website") not in ready, "the demo shows both a one-tap send and a sheet of sessions that are all busy"
    assert all(t["prompt"] is None and t["prompt_len"] > 0 for t in state["tasks"] if t["phase"] not in ("backlog", "queued")), "started rows carry no prompt, only its length"
    handed = [t for t in state["tasks"] if t["mode"] == "session" and t["phase"] == "running"]          # v0.5.15 also hands two finished tasks to s1 / s3 (a pending close, a question)
    assert len(handed) == 1
    s = handed[0]
    assert s["phase"] == "running" and s["column"] == "in_progress" and s["session"]["state"] == "working"
    assert s["session_row"] in rows and rows[s["session_row"]]["tmux"] == s["tmux"], "bound to a live session by its row id"
    assert owner[s["tmux"]] == (s["project"], s["repo"]), "handed to a session of its own repo (no 'work in <path>' prefix)"
    assert (s["branch"], s["worktree"]) == ("", ""), "a session-mode task never creates a worktree"
    assert by_id[5]["session_row"] == s["session_row"], "a session may carry several tasks"


def test_demo_state_jobs_runs_and_the_rest_of_the_fleet_view():
    state = demo_json("state.json")
    now = demo_epoch(state)
    assert len(state["jobs"]) == 5 and len(state["runs"]) == 4
    assert [j["id"] for j in state["jobs"]] == [5, 4, 3, 2, 1], "db.jobs() lists newest id first"
    assert {r["job_id"] for r in state["runs"]} <= {j["id"] for j in state["jobs"]}
    codex_job = state["jobs"][0]                                   # v0.5.16: a Codex schedule, its run and the task card the run left, in the demo
    assert (codex_job["agent"], codex_job["opts"]) == ("codex", {"model": "gpt-5.5", "reasoning_effort": "high"}) and codex_job["max_budget_usd"] is None
    assert all(j["agent"] == "claude" and j["opts"] is None for j in state["jobs"][1:])
    crun = state["runs"][0]
    task = next(t for t in state["tasks"] if t["id"] == crun["task_id"])
    assert (crun["agent"], crun["job_id"], crun["cost_usd"], task["agent"], task["branch"]) == ("codex", 5, None, "codex", crun["branch"])
    assert task["worktree"] == crun["worktree"] and ".ccboard/worktrees/" in task["worktree"] and all(r["agent"] == "claude" for r in state["runs"][1:])
    assert state["scheduler"]["codex"]["known"] is True and state["scheduler"]["known"] is True
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
    assert state["rate_limited"]["value"]["session"] and state["version"] == "demo" and state["tmux_down"] is False
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


TREE_KEYS = {"project", "repo", "path", "git", "branch", "ahead", "behind", "entries", "truncated", "total", "hidden", "ignored", "status_stale", "etag"}
TREE_ENTRY_KEYS = {"name", "type", "status", "dirty", "has_children", "ignored", "size"}


def tree_fixture():
    """app/static/demo/tree.json: {'<repo>|<path>': the payload GET /api/projects/<p>/repos/<repo>/tree?path=<path> answers}; repo 'root' is the
    project folder, path '' its top level. core.js's demoApi resolves a request by that key (the query's hidden/ignored/repos flags are ignored)."""
    return demo_json("tree.json")


def file_fixture():
    """app/static/demo/file.json: {'<repo>|<path>': {status: 200, body: <the /file payload>} | {status: 415|403, error, reveal?: {status: 200, body}}}.
    A key that is absent answers 404."""
    return demo_json("file.json")


def sort_key(e):
    return (e["type"] == "file", e["name"].lower())


def test_demo_tree_fixture_matches_the_tree_endpoint_shape():
    trees = tree_fixture()
    state = demo_json("state.json")
    owner = {r["name"]: p["name"] for p in state["projects"] for r in p["repos"]}
    assert trees and all("|" in k for k in trees)
    for key, t in trees.items():
        repo, _, path = key.partition("|")
        assert set(t) == TREE_KEYS, f"{key}: payload keys {sorted(set(t) ^ TREE_KEYS)} differ from the endpoint's"
        assert (t["repo"], t["path"]) == (repo, path), f"{key}: repo/path inside the payload must match the key"
        assert t["project"] == owner.get(repo, "phasezero" if repo == "root" else None), f"{key}: project {t['project']!r} does not own repo {repo!r} in state.json"
        assert isinstance(t["git"], bool)
        if t["git"]:
            assert isinstance(t["ahead"], int) and isinstance(t["behind"], int)
        else:
            assert t["branch"] is None and t["ahead"] is None and t["behind"] is None, f"{key}: a plain directory has no branch, ahead or behind"
        assert t["hidden"] is False and t["ignored"] is False and t["status_stale"] is False
        assert isinstance(t["etag"], str) and t["etag"]
        assert isinstance(t["entries"], list) and t["entries"], key
        assert t["truncated"] == (t["total"] > len(t["entries"])), f"{key}: truncated must say whether the level was cut"
        assert t["total"] >= len(t["entries"])
        if t["git"]:
            assert t["branch"], f"{key}: a git level carries its branch"
        for e in t["entries"]:
            assert set(e) == TREE_ENTRY_KEYS, (key, e)
            assert e["type"] in ("dir", "file", "repo", "symlink") and e["status"] in (None, "M", "A", "?", "D", "U")
            assert isinstance(e["has_children"], bool) and isinstance(e["dirty"], bool) and isinstance(e["ignored"], bool)
            assert e["size"] is None or (isinstance(e["size"], int) and e["size"] >= 0)
            assert (e["type"] == "file") == (e["size"] is not None or e["status"] == "D"), (key, e)
            assert e["type"] not in ("dir", "repo") or e["has_children"], (key, e)
            assert e["type"] != "file" or not e["has_children"]
            if e["type"] == "file":
                assert e["dirty"] == (e["status"] is not None), (key, e)
            if e["status"] is not None:
                assert e["dirty"], (key, e)
        names = [e["name"] for e in t["entries"]]
        assert len(set(names)) == len(names), f"{key}: duplicate names"
        if not t["truncated"]:
            assert [sort_key(e) for e in t["entries"]] == sorted(sort_key(e) for e in t["entries"]), f"{key}: dirs first, then files, both case-insensitive"
            assert t["total"] == len(t["entries"])


def test_demo_tree_fixture_covers_the_cases_the_page_draws():
    trees = tree_fixture()
    entries = [(k, e) for k, t in trees.items() for e in t["entries"]]
    letters = {e["status"] for _, e in entries}
    assert {None, "M", "A", "?", "D"} <= letters, f"status letters in the demo: {letters}"
    assert any(e["type"] == "dir" and e["dirty"] and e["status"] is None for _, e in entries), "a directory that is dirty only because of its subtree"
    assert any(e["type"] == "dir" and e["status"] == "?" for _, e in entries), "a collapsed untracked directory"
    assert any(e["type"] == "repo" for _, e in entries), "a nested repo in the project folder"
    assert any(t["truncated"] for t in trees.values()), "a truncated level"
    root = trees["root|"]
    assert root["git"] is False and root["branch"] is None, "the project folder is not a git repo"
    assert any(e["type"] == "dir" and e["name"] == "assets" for e in root["entries"]), "the project folder holds a plain (non-git) directory"
    assert {"website|", "NestJs-Ecommerce-Backend|", "website|src", "website|src/components"} <= set(trees)
    assert max(len(t["entries"]) for t in trees.values()) <= 1500, "no level is longer than the endpoint's MAX_ENTRIES"


def test_demo_tree_every_directory_and_repo_can_be_opened():
    """A directory with children must have a listing under its own key and a nested repo must have its top level: no dead click in the demo."""
    trees = tree_fixture()
    missing = []
    for key, t in trees.items():
        repo, _, path = key.partition("|")
        for e in t["entries"]:
            if e["type"] == "dir":
                child = f"{repo}|{path + '/' if path else ''}{e['name']}"
            elif e["type"] == "repo":
                child = f"{e['name']}|"
            else:
                continue
            if child not in trees:
                missing.append(f"{key}: {e['name']} -> {child}")
    assert not missing, "tree.json has no listing for:\n  " + "\n  ".join(missing)
    for key in trees:                       # and nothing is unreachable from its repo's top level
        repo, _, path = key.partition("|")
        if path:
            parent = path.rpartition("/")[0]
            assert f"{repo}|{parent}" in trees, f"{key} has no parent listing"


def test_demo_tree_state_repos_all_have_a_top_level():
    state = demo_json("state.json")
    trees = tree_fixture()
    for p in state["projects"]:
        for r in p["repos"]:
            assert f"{r['name']}|" in trees, f"project {p['name']}: repo {r['name']} has no '<repo>|' listing in app/static/demo/tree.json"
    assert "root|" in trees and trees["root|"]["project"] == "phasezero"


def test_demo_file_fixture_matches_the_file_endpoint_shape():
    files = file_fixture()
    trees = tree_fixture()
    assert files and all("|" in k for k in files)
    ok = [(k, v) for k, v in files.items() if v["status"] == 200]
    assert len(ok) >= 2, "at least two text files"
    for key, v in files.items():
        repo, _, path = key.partition("|")
        assert v["status"] in (200, 403, 415), key
        bodies = [v["body"]] if v["status"] == 200 else [v["reveal"]["body"]] if v["status"] == 403 else []
        if v["status"] != 200:
            assert isinstance(v["error"], str) and v["error"], f"{key}: an error status carries its message"
        if v["status"] == 403:
            assert v["reveal"]["status"] == 200, f"{key}: reveal=1 answers the content"
        else:
            assert "reveal" not in v
        for b in bodies:
            assert set(b) == {"path", "size", "mtime", "truncated", "lines", "text"}, key
            assert b["path"] == path and b["truncated"] is False
            assert b["size"] == len(b["text"].encode("utf-8")) and b["lines"] == len(b["text"].splitlines()) and b["lines"] > 0
            assert isinstance(b["mtime"], int) and b["size"] <= 200 * 1024
        assert f"{repo}|{path.rpartition('/')[0]}" in trees, f"{key}: its directory has no listing"
        parent = trees[f"{repo}|{path.rpartition('/')[0]}"]
        assert any(e["name"] == path.rpartition("/")[2] and e["type"] == "file" for e in parent["entries"]), f"{key}: no such file in its listing"
    assert any(v["status"] == 415 for v in files.values()), "a binary file (415)"
    forbidden = [k for k, v in files.items() if v["status"] == 403]
    secret = re.compile(r"^\.env|\.pem$|^id_rsa|credentials|\.key$|secret", re.I)       # the endpoint's 403 rule
    assert forbidden and all(secret.search(k.rpartition("/")[2]) for k in forbidden), "a secret-looking name (.env*, *.pem, id_rsa*, *credentials*, *.key, *secret*)"


def test_demo_every_listed_file_has_a_preview_unless_it_is_deleted_or_in_a_truncated_level():
    files = file_fixture()
    missing = []
    for key, t in tree_fixture().items():
        if t["truncated"]:
            continue
        repo, _, path = key.partition("|")
        for e in t["entries"]:
            if e["type"] == "file" and e["status"] != "D" and f"{repo}|{path + '/' if path else ''}{e['name']}" not in files:
                missing.append(f"{key}: {e['name']}")
    assert not missing, "file.json has no entry for:\n  " + "\n  ".join(missing)


def test_demo_file_sizes_agree_with_the_listing():
    files = file_fixture()
    for key, t in tree_fixture().items():
        repo, _, path = key.partition("|")
        for e in t["entries"]:
            k = f"{repo}|{path + '/' if path else ''}{e['name']}"
            if e["type"] == "file" and k in files and files[k]["status"] == 200:
                assert e["size"] == files[k]["body"]["size"], f"{k}: the listing says {e['size']} bytes, the preview {files[k]['body']['size']}"


def _git(path, *args):
    import subprocess
    subprocess.run(["git", "-C", str(path), "-c", "user.name=demo", "-c", "user.email=demo@example.invalid", "-c", "commit.gpgsign=false", *args],
                   check=True, capture_output=True)


def test_demo_tree_fixture_is_what_list_dir_answers_for_the_same_files(projects_dir):
    """The fixture is a recording of the endpoint: rebuild phasezero/website (and the project folder) from the fixtures' own file contents with the
    same git state, ask app.tree.list_dir for every level that is not truncated and compare entry for entry (name, type, status, dirty,
    has_children, ignored, size) and the payload's keys."""
    from app import tree
    trees, files = tree_fixture(), file_fixture()
    proj = projects_dir / "phasezero"
    repo = proj / "website"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    deleted = "tests/legacy-cart.test.ts"
    untracked = {"notes.txt", "src/components/CartDrawer.tsx"}
    staged = {"src/components/ProductCard.tsx"}
    def write(rel, text=None, data=None):
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data if data is not None else text.encode("utf-8"))
    for key, fx in files.items():
        r, _, rel = key.partition("|")
        if r == "website":
            body = fx["body"] if fx["status"] == 200 else fx["reveal"]["body"] if fx["status"] == 403 else None
            if body is not None:
                write(rel, body["text"])
    write("public/favicon.ico", data=b"\0" * 15086)
    write(deleted, "test('legacy', () => {});\n")
    write("public/icons/icon-000.svg", "<svg/>\n")                # the truncated level's directory exists; only its first level is compared elsewhere
    _git(repo, "add", "-A")
    for rel in sorted(untracked | staged):
        _git(repo, "rm", "-q", "--cached", rel)
    _git(repo, "commit", "-qm", "base")
    (repo / deleted).unlink()
    for rel in staged:
        _git(repo, "add", rel)
    for rel in ("next.config.js", "src/middleware.ts", "src/components/Header.tsx", "src/app/checkout/Step2.tsx"):
        (repo / rel).write_text((repo / rel).read_text() + "\n// changed\n")
    (proj / "NestJs-Ecommerce-Backend").mkdir()
    _git(proj / "NestJs-Ecommerce-Backend", "init", "-q", "-b", "develop")
    for key, fx in files.items():
        r, _, rel = key.partition("|")
        if r == "root":
            f = proj / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"\0" * 482113 if fx["status"] == 415 else fx["body"]["text"].encode("utf-8"))
    compared = 0
    for key, fx in trees.items():
        r, _, rel = key.partition("|")
        if r not in ("website", "root") or fx["truncated"]:
            continue
        got = tree.list_dir("phasezero", r, rel)
        assert set(got) == set(fx), f"{key}: payload keys differ: {sorted(set(got) ^ set(fx))}"
        for k in ("project", "repo", "path", "git", "truncated", "total", "hidden", "ignored", "status_stale"):
            assert got[k] == fx[k], f"{key}: {k} is {got[k]!r} in the endpoint, {fx[k]!r} in the fixture"
        assert (got["branch"], got["ahead"] is None) == (fx["branch"], fx["ahead"] is None), key
        assert [e["name"] for e in got["entries"]] == [e["name"] for e in fx["entries"]], f"{key}: entries differ"
        for g, f in zip(got["entries"], fx["entries"]):
            assert set(g) == set(f), (key, g)
            for field in ("type", "status", "dirty", "has_children", "ignored"):
                assert g[field] == f[field], f"{key}: {f['name']}.{field} is {g[field]!r} in the endpoint, {f[field]!r} in the fixture"
            if f["type"] == "file" and f["status"] != "M":
                assert g["size"] == f["size"], f"{key}: {f['name']} is {g['size']} bytes on disk, {f['size']} in the fixture"
        compared += 1
    assert compared >= 8, f"only {compared} levels were compared"


def test_demo_series_fixture_matches_the_series_endpoint_shape():
    """24 h in 30-minute buckets: GET /api/series?series=rl_5h,rl_7d,ctx&key=*&since=24h&points=48. The lim events that
    /api/series/events answers ride in the same file (demo mode maps both paths to it)."""
    s = demo_json("series.json")
    state = demo_json("state.json")
    assert s["since"] and s["until"] and s["step"] == 1800 and len(s["t"]) == 48 and s["t"][0] % s["step"] == 0
    assert all(b - a == s["step"] for a, b in zip(s["t"], s["t"][1:]))
    assert parse_iso(s["until"]) == demo_epoch(state), "the series ends at the fixture clock"
    assert parse_iso(s["until"]) - parse_iso(s["since"]) >= 23 * 3600
    assert {"rl_5h:claude", "rl_7d:claude"} <= set(s["series"])
    ctx = sorted(k for k in s["series"] if k.startswith("ctx:"))
    assert len(ctx) == 2, "ctx for two sessions"
    for key, values in s["series"].items():
        assert len(values) == 48, key
        assert all(v is None or 0 <= v <= 100 for v in values), key
    five = s["series"]["rl_5h:claude"]
    top = [i for i, v in enumerate(five) if v is not None and v >= 99]
    assert top and top == list(range(top[0], top[-1] + 1)), "the 5h series shows one limit-hit plateau"
    assert top[-1] < len(five) - 1 and min(v for v in five[top[-1] + 1:] if v is not None) < 50, "and then the window resets"
    assert five[-1] == 42 and s["series"]["rl_7d:claude"][-1] == 71, "the last point matches the state's pills"
    pct = {x["tmux"]: x["stats"]["context_pct"] for _, _, x in fleet_sessions(state) if x.get("stats")}
    for key in ctx:
        assert key[len("ctx:"):] in pct and s["series"][key][-1] == pct[key[len("ctx:"):]], f"{key} ends at the session's context %"
        assert s["meta"][key]["window"] == 200000 and s["meta"][key]["model"]
    assert s["meta"]["rl_5h:claude"]["resets_at"] == state["usage"]["value"]["five_hour"]["resets_at"]
    assert s["truncated"] is False and s["events"], "a limit episode"
    sessions = {x["tmux"] for _, _, x in fleet_sessions(state)}
    for e in s["events"]:
        assert {"t", "key", "v", "m"} <= set(e) and e["key"] in ("5h", "7d", "other") and e["m"]["session"] in sessions
        assert s["t"][0] <= e["t"] <= parse_iso(s["until"])
    assert (s["events"][0]["t"] - s["t"][0]) // s["step"] in top, "the episode sits on the plateau"


def test_demo_usage_summary_fixture_matches_build_and_agrees_with_the_state_fixture(tmp_path):
    from datetime import datetime, timezone
    from app import usage_summary
    from app.db import DB
    state = demo_json("state.json")
    now = datetime.fromtimestamp(demo_epoch(state), timezone.utc)
    fx = demo_json("usage_summary.json")
    empty = usage_summary.build(DB(tmp_path / "empty.db"), days=30, tz_min=345, now=now)
    missing = _missing_keys(empty, fx, "usage_summary")
    assert not missing, "app/static/demo/usage_summary.json lacks keys usage_summary.build returns:\n  " + "\n  ".join(missing)
    assert set(fx) - {"demo"} == set(empty), "and nothing the builder does not return (the fixture clock `demo` is the one extra: demoApi rebases the summary with it)"
    assert fx["demo"]["epoch"] == demo_epoch(state), "the same fixture clock as state.json: demoApi shifts the reset times and stamps by it"
    assert fx["source"] == "samples" and fx["tz_min"] == 345 and fx["generated_at"] == empty["generated_at"]
    # daily: 30 local days ending on the fixture clock's day (Kathmandu), zero days present and flagged
    assert [d["day"] for d in fx["daily"]] == [d["day"] for d in empty["daily"]]
    assert all(set(d) == set(empty["daily"][0]) for d in fx["daily"])
    assert any(d["zero"] for d in fx["daily"]) and any(not d["zero"] for d in fx["daily"])
    assert all(d["zero"] == (d["total"] == 0) and d["total"] >= 0 and d["tokens"] >= 0 for d in fx["daily"])
    # windows are sums of the daily bars
    w = fx["windows"]
    assert w["today"]["total"] == round(fx["daily"][-1]["total"], 4)
    assert w["7d"]["total"] == pytest.approx(sum(d["total"] for d in fx["daily"][-7:]), abs=1e-3)
    assert w["30d"]["total"] == pytest.approx(sum(d["total"] for d in fx["daily"]), abs=1e-3)
    for win in w.values():
        assert win["total"] == pytest.approx(sum(a["total"] for a in win["by_agent"].values()), abs=1e-3)
        assert set(win["by_agent"]) >= {"claude"} and all({"total", "tokens"} <= set(a) for a in win["by_agent"].values())
        assert all({"project", "total", "hours"} <= set(p) for p in win["by_project"])
        assert [p["total"] for p in win["by_project"]] == sorted((p["total"] for p in win["by_project"]), reverse=True)
    # the same projects and per-project cost-today as the state fixture's cost record
    cost = state["cost"]["value"]["projects"]
    today = {p["project"]: p["total"] for p in w["today"]["by_project"]}
    assert today == {name: c["today"] for name, c in cost.items() if c["today"]}
    assert {p["project"] for p in w["30d"]["by_project"]} <= {p["name"] for p in state["projects"]} | {"(unattributed)"}
    # hour of day and heatmap
    assert len(fx["hourly_profile"]) == 24 and len(fx["heatmap"]) == 7 and all(len(r) == 24 for r in fx["heatmap"])
    assert sum(fx["hourly_profile"]) == sum(map(sum, fx["heatmap"])) > 0
    morning, evening = sum(fx["hourly_profile"][9:14]), sum(fx["hourly_profile"][19:24])
    assert morning > 0 and evening > morning, "both peaks of the user's day show, the evening one higher"
    # top sessions, active hours, rate limits, episodes, unpriced
    top = fx["top_sessions"]
    assert 1 < len(top) <= 10 and [t["total"] for t in top] == sorted((t["total"] for t in top), reverse=True)
    assert all({"key", "project", "repo", "agent", "total", "hours"} <= set(t) and ":" in t["key"] for t in top)
    assert set(fx["active_hours"]) <= {p["name"] for p in state["projects"]} and all(h > 0 for h in fx["active_hours"].values())
    rl = fx["rate_limits"]["claude"]
    assert rl["rl_5h"]["value"] == 42 and rl["rl_7d"]["value"] == 71
    assert rl["rl_5h"]["meta"]["resets_at"] == state["usage"]["value"]["five_hour"]["resets_at"]
    assert fx["episodes"] and all({"kind", "at", "resets_at", "session"} <= set(e) and e["kind"] in ("5h", "7d", "other") for e in fx["episodes"])
    assert [e["at"] for e in fx["episodes"]] == sorted(e["at"] for e in fx["episodes"])
    assert fx["unpriced"] and all({"key", "agent", "project", "repo", "tokens", "models"} <= set(u) for u in fx["unpriced"])


@pytest.mark.parametrize("name", DEMO_FILES)
def test_demo_api_maps_a_path_to_every_fixture(name):
    """api() in demo mode reads /static/demo/<name>.json: the table in core.js must name every fixture or the file is dead weight."""
    assert f"name = '{name[:-len('.json')]}';" in (STATIC / "core.js").read_text(encoding="utf-8")


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


# ---------- terminal page on mobile: definition-only kit, no ES modules, the term.css scroll-fix set, the dev tty fake ----------

REPO = STATIC.parent.parent
DEFINE_ONLY = ("core.js", "components.js", "termkit.js", "pages/widgets.js", "tree.js", "dnd.js")
# a declaration, a string (a directive), or a function / literal assigned to a property of a declared namespace (`Widgets.usageCard = function ...`):
# none of them runs anything at load. A call, an `if`, a bare `document.x = ...` or `Name.start();` is not in the list.
DEFINE_ONLY_START = re.compile(
    r"(?:const|let|var|function|class|async\s+function)\b|['\"`]"
    r"|[A-Z]\w*\.\w+\s*=\s*(?:async\s+)?(?:function\b|\([^()]*\)\s*=>|\w+\s*=>|[{\['\"`\d]|true\b|false\b|null\b)")


def top_level_statements(src):
    """(line, text) for each line that starts in column 0 at bracket depth 0 of the comment- and string-blanked source."""
    out = []
    depth = 0
    for n, line in enumerate(blank_js(src, strings=True).splitlines(), 1):
        if depth == 0 and line.strip() and not line[0].isspace():
            out.append((n, line.strip()))
        for ch in line:
            if ch in "({[":
                depth += 1
            elif ch in ")}]":
                depth -= 1
    return out


def test_top_level_scanner_flags_calls_and_accepts_declarations():
    ok = ("'use strict';\nconst A = {\n  b: 1,\n};\nfunction f() {\n  g();\n}\nasync function h() {}\nclass K {\n  m() { x(); }\n}\nlet t = `${a} ${b}`;\n"
          "Widgets.usageCard = function (host) {\n  return host;\n};\nWidgets.limitBanner = (st) => {\n  return st;\n};\nWidgets.N = 12;\nWidgets.live = async (x) => x;\n")
    assert all(DEFINE_ONLY_START.match(text) for _, text in top_level_statements(ok)), top_level_statements(ok)
    for bad in ("setInterval(tick, 1000);", "document.title = 'x';", "(function () { go(); })();", "TermKit.boot();", "window.addEventListener('x', y);",
                "Widgets.timer = setInterval(tick, 1000);", "Widgets.go = init();", "Widgets.x = a.b();", "Widgets.start();", "if (x) { y(); }"):
        assert not any(DEFINE_ONLY_START.match(text) for _, text in top_level_statements(bad)), bad


@pytest.mark.parametrize("name", DEFINE_ONLY)
def test_definition_only_scripts_declare_and_never_run_at_the_top_level(name):
    """core.js, components.js and termkit.js only define: a column-0 statement that is not a declaration would run at load, in every
    page that includes the file (tests/js/define-only.test.mjs proves the same at run time with a hostile world)."""
    path = STATIC / name
    assert path.is_file(), f"app/static/{name} is missing"
    bad = [f"{rel(path)}:{n}: {text[:100]}" for n, text in top_level_statements(path.read_text(encoding="utf-8"))
           if not DEFINE_ONLY_START.match(text)]
    assert not bad, "top-level statement that is not a declaration (only term.js / main.js start things):\n" + "\n".join(bad)


def test_js_no_es_modules():
    """Classic scripts only: no import/export, no dynamic import(), no type=module (the app is loaded script by script, in order)."""
    bad = []
    pattern = re.compile(r"(?<![\w.$])import\s*\(|^\s*import\s+[\w{*'\"]|\bimport\.meta\b|^\s*export\s+(?:default\b|const\b|let\b|var\b|function\b|class\b|async\b|\{|\*)", re.M)
    for p in app_files(".js"):
        code = blank_js(p.read_text(encoding="utf-8"))
        for m in pattern.finditer(code):
            bad.append(f"{rel(p)}:{line_of(code, m.start())}: {snippet(code, m.start())}")
    for p in app_files(".html"):
        for tag, attrs, line in html_tags(p):
            if tag == "script" and attrs.get("type", "").lower() == "module":
                bad.append(f"{rel(p)}:{line}: <script type=module>")
    assert not bad, "ES modules are banned (classic scripts share one global scope, loaded in the order the HTML lists):\n" + "\n".join(bad)


# ---------- tree.js (v0.5.6): the WAI-ARIA tree pattern, read at grep level (tests/js/tree.test.mjs proves the behaviour) ----------

def tree_js_code():
    path = STATIC / "tree.js"
    assert path.is_file(), "app/static/tree.js is missing"
    return blank_js(path.read_text(encoding="utf-8"))


def test_tree_js_sets_role_tree_once_and_treeitem_on_the_nodes():
    code = tree_js_code()
    roles = re.findall(r"""['"]?role['"]?\s*[:,]\s*['"](\w+)['"]""", code)
    assert roles.count("tree") == 1, f"role=tree must be set exactly once (the list), found {roles.count('tree')}"
    assert roles.count("treeitem") >= 1, "every node is a role=treeitem"
    assert "group" in roles, "a directory's children sit in a role=group"
    assert not re.search(r"role['\"]?\s*[:,]\s*['\"](?:menu|listbox|list|option)['\"]", code), "a tree is not a list or a menu"


def test_tree_js_aria_expanded_is_only_ever_written_for_directories():
    """Files are leaves: an aria-expanded on one would be announced as a collapsed group. Every use of the attribute in tree.js sits within
    four lines of a directory test (the exact behaviour is asserted at run time in tests/js/tree.test.mjs)."""
    code = tree_js_code().splitlines()
    uses = [i for i, line in enumerate(code) if "aria-expanded" in line]
    assert uses, "tree.js never sets aria-expanded: directories must expose their state"
    for i in uses:
        window = "\n".join(code[max(0, i - 4):i + 1])
        assert re.search(r"\b(?:isDir|dir|directory|isdir)\b|'dir'|\"dir\"|has_children|hasChildren|expandable|isParent", window, re.I), \
            f"app/static/tree.js:{i + 1}: aria-expanded without a directory test in the preceding lines: {code[i].strip()[:100]}"


def test_tree_js_has_the_aria_level_selected_and_roving_tabindex_attributes():
    code = tree_js_code()
    for attr in ("aria-level", "aria-selected", "tabindex"):
        assert attr in code, f"tree.js never sets {attr}"
    for name in ("Tree.mount", "Tree.previewFile"):
        assert re.search(re.escape(name) + r"\s*=", code), f"tree.js does not define {name}"


def test_tree_js_makes_its_requests_cancellable_and_uses_semantic_classes():
    code = tree_js_code()
    assert "AbortController" in code, "an expand that is cancelled by a collapse aborts its request"
    assert "If-None-Match" in code or "etag" in code.lower(), "open nodes revalidate with the ETag"
    assert not re.search(r"bp5-", code), "Blueprint classes live in core.js (SEMANTIC): tree.js uses the semantic names"
    for sem in ("tree", "treenode", "treecontent", "treecaret", "treelabel", "treesecondary"):
        assert re.search(rf"\b{sem}\b", code), f"tree.js never uses the semantic class '{sem}'"


def test_core_js_maps_the_tree_semantic_classes_to_blueprint():
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    block = core[core.index("const SEMANTIC"):core.index("function blueprint")]
    for sem, bp in (("tree", "bp5-tree"), ("treenode", "bp5-tree-node"), ("treecontent", "bp5-tree-node-content"), ("treecaret", "bp5-tree-node-caret"),
                    ("treelabel", "bp5-tree-node-label"), ("treesecondary", "bp5-tree-node-secondary-label")):
        assert re.search(rf"\b{sem}:\s*'[^']*\b{bp}\b", block), f"SEMANTIC lacks {sem}: '{bp}'"


def test_tree_js_and_project_js_use_no_style_attributes_or_history_api():
    for name in ("tree.js", "pages/project.js"):
        path = STATIC / name
        assert path.is_file(), f"app/static/{name} is missing"
        code = blank_js(path.read_text(encoding="utf-8"), strings=False)
        assert not re.search(r"""['"]style['"]\s*[:,]|setAttribute\(\s*['"]style|\bstyle\s*:\s*['"]""", code), f"{name}: no style attributes (the CSP forbids them)"
        assert not re.search(r"\bhistory\s*\.\s*(?:pushState|replaceState)\b", code), f"{name}: the history API belongs to router.js"


def test_term_scripts_carry_the_persisted_keys_and_the_back_rule():
    """v0.5.8 contract: font size, context strip and key-bar mode survive a reload; the back chevron returns to where the user came
    from (history.back() for a same-origin referrer) and otherwise lands on the Agents roster."""
    term = (STATIC / "term.js").read_text(encoding="utf-8")
    both = term + (STATIC / "termkit.js").read_text(encoding="utf-8")
    for key in ("ccboard:term:fs", "ccboard:term:ctx", "ccboard:term:keys"):
        assert key in both, f"{key} is not used by term.js or termkit.js"
    code = blank_js(term)
    assert re.search(r"\bhistory\s*\.\s*back\s*\(", code), "term.js: the back chevron uses history.back() for a same-origin referrer"
    assert re.search(r"\blocation\s*\.\s*assign\s*\(", code) and "/#/agents" in term, "term.js: otherwise location.assign('/#/agents')"
    assert re.search(r"\breferrer\b", code), "term.js: the same-origin test reads document.referrer"


# -- term.css: the iOS scroll fix set (d) and the layout rules, read as parsed rules so spacing and ordering do not matter --

_CSS_CONTAINERS = ("@media", "@supports", "@keyframes", "@-webkit-keyframes", "@layer", "@container")


def css_decls(body):
    out = {}
    for part in body.split(";"):
        prop, sep, value = part.partition(":")
        if sep and prop.strip():
            out[prop.strip().lower()] = re.sub(r"\s+", " ", re.sub(r"!important", "", value, flags=re.I)).strip()
    return out


def css_rules(css):
    """[(enclosing at-rules joined by space, selector text, {prop: value})] for blanked CSS; containers (media, supports) nest."""
    css = blank_css_comments(css)
    rules, stack, start, i = [], [], 0, 0
    while i < len(css):
        c = css[i]
        if c == "{":
            prelude = re.sub(r"\s+", " ", css[start:i]).strip()
            if prelude.startswith(_CSS_CONTAINERS):
                stack.append(prelude)
                i += 1
            else:
                j = css.find("}", i)
                j = len(css) if j < 0 else j
                rules.append((" ".join(stack), prelude, css_decls(css[i + 1:j])))
                i = j + 1
            start = i
        elif c == "}":
            if stack:
                stack.pop()
            i += 1
            start = i
        else:
            i += 1
    return rules


def test_css_rule_parser_handles_media_lists_and_important():
    css = "/* c */ html, body.term { a: 1 !important; b:  x   y }\n@media (min-width: 840px) { .a > .b { c: d; e: f } }\n@font-face { src: url(/static/x.woff2) }\nz { k: v }"
    got = css_rules(css)
    assert got[0] == ("", "html, body.term", {"a": "1", "b": "x y"})
    assert got[1] == ("@media (min-width: 840px)", ".a > .b", {"c": "d", "e": "f"})
    assert got[2][1] == "@font-face" and got[3] == ("", "z", {"k": "v"})


def term_css():
    path = STATIC / "term.css"
    assert path.is_file(), "app/static/term.css is missing"
    return path.read_text(encoding="utf-8")


def term_style(selector, in_media=False):
    """The declarations the given selector (one entry of a rule's selector list) gets, later rules winning; top-level rules by default."""
    merged = {}
    for media, sel, decls in css_rules(term_css()):
        if selector in [s.strip() for s in sel.split(",")] and (bool(media) == in_media):
            merged.update(decls)
    return merged


def _zero_inset(d):
    return d.get("inset") in ("0", "0px") or all(d.get(k) in ("0", "0px") for k in ("top", "right", "bottom", "left"))


def test_term_css_body_is_a_fixed_visual_viewport_box_that_never_bounces():
    body = term_style("body.term")
    assert body.get("position") == "fixed" and _zero_inset(body), body
    assert "var(--vvh" in body.get("height", ""), "body.term height comes from --vvh (TermKit.viewportFit), with a 100dvh fallback"
    assert "100dvh" in body.get("height", "")
    assert body.get("overflow") == "hidden", body
    assert body.get("overscroll-behavior", body.get("overscroll-behavior-y")) == "none", body
    html = term_style("html")
    assert html.get("overscroll-behavior", html.get("overscroll-behavior-y")) == "none", "html needs overscroll-behavior:none too (pull-to-refresh bounce)"
    assert re.search(r"-webkit-tap-highlight-color\s*:\s*transparent", term_css()), "no grey tap flash on the keys"


def test_term_css_ttywrap_clips_and_the_iframe_fills_it():
    wrap = term_style("#ttywrap")
    assert wrap.get("position") == "relative" and wrap.get("overflow") == "hidden", wrap
    frame = {}
    for media, sel, decls in css_rules(term_css()):
        if not media and any(re.fullmatch(r"#ttywrap\s*>?\s*iframe(?:#tty)?|iframe#tty|#tty", s.strip()) for s in sel.split(",")):
            frame.update(decls)
    assert frame.get("position") == "absolute" and _zero_inset(frame), f"the iframe is absolute inset 0 inside #ttywrap, got {frame}"
    assert frame.get("width") in (None, "100%") and frame.get("height") in (None, "100%")


def test_term_css_safe_area_on_all_four_sides():
    css = term_css()
    for side in ("top", "right", "bottom", "left"):
        assert f"safe-area-inset-{side}" in css, f"term.css never reads env(safe-area-inset-{side})"


def test_term_css_two_columns_from_840px_with_a_side_column_of_320_to_360px():
    media = [(m, sel, d) for m, sel, d in css_rules(term_css()) if re.search(r"min-width\s*:\s*840px", m)]
    assert media, "no @media (min-width: 840px) block in term.css"
    assert any("clamp(320px, 27vw, 360px)" in v for _, _, d in media for v in d.values()), "the side column is 320 to 360 px wide at 840 px and up"
    assert not any(re.search(r"max-width\s*:\s*(?:[0-7]\d\d|8[0-3]\d)px", m) and "grid-template-columns" in d for m, _, d in css_rules(term_css())), \
        "below 840 px the terminal page stays a single column"


def test_term_css_inputs_are_16px_so_ios_does_not_zoom_on_focus():
    ok = [(sel, d) for m, sel, d in css_rules(term_css())
          if re.search(r"composer|textarea|input|sendtext", sel) and d.get("font-size") == "16px"]
    assert ok, "the composer (and any input) needs font-size:16px on coarse pointers"


def _px(value):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)px", (value or "").strip())
    return float(m.group(1)) if m else None


def _rules_for(fragment):
    """Declarations of every top-level rule one of whose selectors contains `fragment`, merged in file order."""
    merged = {}
    for media, sel, decls in css_rules(term_css()):
        if not media and any(fragment in part for part in sel.split(",")):
            merged.update(decls)
    return merged


def test_term_css_key_bar_keys_and_scroll_rail_buttons_are_at_least_44px():
    """The key bar's whole point on a phone: a thumb-sized target for every key and for the scroll rail (WCAG 2.5.5, Apple HIG 44 pt)."""
    for fragment in (".kb-key", ".rail-btn"):
        d = _rules_for(fragment)
        assert d, f"term.css has no rule for {fragment}"
        for prop in ("min-height", "min-width"):
            v = d.get(prop)
            assert v is not None and (v == "var(--tap)" or (_px(v) or 0) >= 44), f"{fragment} {prop} is {v!r}, expected at least 44px"
    chip = _rules_for(".chip-history")
    assert chip.get("min-height") in ("var(--tap)", None) or (_px(chip.get("min-height")) or 0) >= 44, chip


def test_term_css_compact_key_bar_keeps_row_one_and_the_more_toggle():
    """Soft keyboard up (.kb.compact): Esc ^C Tab Shift+Tab Enter + More stay, rows 2 and 3 fold away until More is tapped."""
    rules = css_rules(term_css())
    hidden = [sel for media, sel, d in rules if not media and d.get("display") == "none" and ".kb.compact" in sel]
    joined = ",".join(hidden)
    assert ".kb-r2" in joined and ".kb-r3" in joined and ":not(.more)" in joined, f"rows 2 and 3 must fold away in .kb.compact:not(.more), got {hidden}"
    assert ".kb-r1" not in joined, "row 1 (the five compact keys) must stay visible"
    assert term_style(".kb-more").get("display") == "none", "More is hidden outside compact mode"
    shown = [d for media, sel, d in rules if not media and ".kb.compact .kb-more" in sel and ".kb-more" in sel and d.get("display") not in (None, "none")]
    assert shown, "More is shown in compact mode"


def test_term_css_has_no_remote_or_data_urls_and_no_100vh_without_a_dynamic_fallback():
    css = blank_css_comments(term_css())
    assert not re.search(r"url\(\s*['\"]?(?:data:|https?:|//)", css, re.I)
    for m in re.finditer(r"height\s*:\s*100vh\b", css):
        assert "100dvh" in css[m.end():m.end() + 80], f"term.css:{line_of(css, m.start())}: 100vh must be followed by a 100dvh (or --vvh) declaration"


# -- scripts/dev/fake_tty: the dev harness page is outside app/static (so no app rule scans it) but must still reach nothing --

FAKE_TTY = REPO / "scripts" / "dev" / "fake_tty"
FAKE_TTY_ALLOWED_URLS = ("http://www.w3.org/2000/svg",)


def test_fake_tty_is_outside_the_app_scan_and_the_service_worker_shell():
    assert STATIC not in FAKE_TTY.parents
    assert not any(FAKE_TTY in p.parents or p == FAKE_TTY for p in app_files())


@pytest.mark.parametrize("name", ["index.html", "fake_tty.js"])
def test_fake_tty_has_no_external_url_and_no_network_call(name):
    path = FAKE_TTY / name
    if not path.is_file():
        pytest.skip(f"{path.relative_to(REPO)} does not exist yet (tests/test_dev_harness.py asserts it)")
    text = path.read_text(encoding="utf-8")
    code = blank_js(text) if name.endswith(".js") else text
    urls = [m.group() for m in re.finditer(r"https?://[^\s'\"<>)]*", code) if not m.group().startswith(FAKE_TTY_ALLOWED_URLS)]
    urls += [m.group() for m in re.finditer(r"""\b(?:src|href|action)\s*=\s*['"]//[^'"]*""", code)]
    urls += [host for host in CDN_HOSTS if host in text]
    assert not urls, f"{path.relative_to(REPO)} reaches out to {urls}: the fake tty is offline by construction"
    if name.endswith(".js"):
        for what in ("fetch", "XMLHttpRequest", "WebSocket", "EventSource", "sendBeacon", "importScripts", "Worker", "innerHTML", "eval"):
            assert not re.search(rf"\b{what}\b", code), f"{name}: {what} (the fake tty is a static page with a counter)"
