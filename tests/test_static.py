"""Static-asset guard rails over every app-owned file under app/static (everything except vendor/ and demo/).

The CSP is `default-src 'self'`, so nothing here may inject markup, carry inline code or style, or reach another origin.
Each rule is its own test and names file:line, so a failure says exactly what to change. Comments are blanked before the JS
and CSS rules run (a comment may say "never innerHTML"), and line numbers are preserved.
"""
import pathlib
import re
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
