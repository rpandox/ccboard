"""The Usage page's static contract (v0.5.17): what has to be on disk, wired in index.html and the service-worker shell, free of the constructs
the CSP forbids, and what the demo fixtures must carry so `?demo=1#/usage` shows every state the page draws.

It reads files; the behaviour is in tests/js/charts.test.mjs, usage.test.mjs and widgets-usage.test.mjs. A file one of the slices has not written yet
fails its test with a message that names the file (never a FileNotFoundError), so the first red line says what to create:

  app/static/charts.js, charts.css, tests/js/charts.test.mjs       slice A (impl:charts)
  app/static/pages/usage.js, index.html wiring, placeholders.js    slice B (impl:usage-page)
  app/static/pages/widgets.js, demo/*.json, this file               slice C (impl:home-wiring)
  the demo mapping line in core.js                                  the orchestrator (see test_core_maps_the_events_path_to_its_own_fixture)

The generic bans (innerHTML, cssText, style attributes, data: URIs in CSS, bp5- outside core.js, remote URLs) are enforced for every app-owned file by
tests/test_static.py; they are repeated here per Usage file so a violation names the file the Usage work owns, and extended with the Usage-only rules.
"""
import datetime
import json
import pathlib
import re
from html.parser import HTMLParser

import pytest

from tests.test_static import JS_BANS, blank_css_comments, blank_js, line_of, snippet, top_level_statements

STATIC = pathlib.Path(__file__).resolve().parent.parent / "app" / "static"
DEMO = STATIC / "demo"
H = {"Tailscale-User-Login": "alice@example.com"}

OWNER = {
    "charts.js": "slice A (impl:charts) writes it", "charts.css": "slice A (impl:charts) writes it",
    "pages/usage.js": "slice B (impl:usage-page) writes it", "index.html": "slice B (impl:usage-page) wires it",
    "pages/placeholders.js": "slice B (impl:usage-page) edits it", "pages/widgets.js": "slice C (impl:home-wiring) edits it",
    "demo/series_events.json": "slice C (impl:home-wiring) writes it", "core.js": "the orchestrator adds the demo mapping line",
}
STATE_CODES = {"idle": 0, "working": 1, "waiting": 2, "done": 3, "errored": 4, "ended": 5}


def need(rel_path):
    """The path under app/static, or a failure that names the missing file and who writes it."""
    path = STATIC / rel_path
    if not path.is_file():
        pytest.fail(f"app/static/{rel_path} is missing: {OWNER.get(rel_path, 'a Usage-page file')}", pytrace=False)
    return path


def text_of(rel_path):
    return need(rel_path).read_text(encoding="utf-8")


def demo(name):
    return json.loads(text_of("demo/" + name))


def live_sessions():
    """tmux name -> session record of every live session in the state fixture (project repos, project root, orphans)."""
    state = demo("state.json")
    out = {}
    for p in state["projects"]:
        groups = [r["sessions"] for r in p["repos"]] + [p["orphan_sessions"]]
        if p.get("root"):
            groups.append(p["root"]["sessions"])
        for g in groups:
            for s in g:
                out[s["tmux"]] = s
    return out


# ---------------------------------------------------------------- vendored uPlot

def test_vendored_uplot_has_the_script_the_stylesheet_and_an_mit_licence():
    base = "vendor/uplot/"
    js, css, lic = (STATIC / (base + n) for n in ("uPlot.iife.min.js", "uPlot.min.css", "LICENSE"))
    for p in (js, css, lic):
        assert p.is_file(), f"app/static/{base}{p.name} is missing (vendor uPlot 1.6.32: iife build, css and its licence)"
    assert js.stat().st_size > 20000 and "uPlot" in js.read_text(encoding="utf-8"), "the iife build must define uPlot"
    assert css.stat().st_size > 500 and ".uplot" in css.read_text(encoding="utf-8"), "uPlot's own stylesheet"
    assert "MIT" in lic.read_text(encoding="utf-8"), "the licence that ships next to the code must be the MIT one"


# ---------------------------------------------------------------- index.html wiring

class _Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts, self.styles = [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script" and a.get("src"):
            self.scripts.append(a["src"])
        elif tag == "link" and "stylesheet" in (a.get("rel") or "").split():
            self.styles.append(a.get("href") or "")


def index_assets():
    p = _Assets()
    p.feed(text_of("index.html"))
    return p


def test_index_links_charts_css_after_pages_css():
    need("charts.css")
    styles = index_assets().styles
    assert styles.count("/static/charts.css") == 1, f"index.html must link /static/charts.css exactly once, found {styles}"
    assert "/static/pages.css" in styles and styles.index("/static/charts.css") > styles.index("/static/pages.css"), \
        "charts.css comes after pages.css (it may refine a page rule, never the other way round)"


def test_index_loads_charts_js_before_shell_js_and_usage_js_before_placeholders_js():
    need("charts.js")
    need("pages/usage.js")
    s = index_assets().scripts
    for name in ("/static/charts.js", "/static/pages/usage.js"):
        assert s.count(name) == 1, f"index.html must load {name} exactly once, found {s}"
    assert s.index("/static/core.js") < s.index("/static/charts.js") < s.index("/static/shell.js"), "charts.js: after core.js, before shell.js"
    assert s.index("/static/router.js") < s.index("/static/pages/usage.js") < s.index("/static/pages/placeholders.js"), \
        "usage.js registers its route with router.js's registerPage, and must beat the placeholder's registration"


def test_index_never_loads_uplot_eagerly():
    """uPlot (51 KB) loads on the Usage page only, through loadAsset: neither a <script> nor a <link> for it in the skeleton."""
    a = index_assets()
    bad = [u for u in a.scripts + a.styles if "uplot" in u.lower()]
    assert not bad, f"index.html must not load uPlot up front (Charts.ready() loads it lazily): {bad}"


# ---------------------------------------------------------------- the route

def test_placeholders_no_longer_register_any_real_page():
    code = blank_js(text_of("pages/placeholders.js"))
    assert not re.search(r"""registerPage\(\s*['"]usage['"]""", code), "pages/placeholders.js must not register 'usage' any more (pages/usage.js does)"
    assert not re.search(r"^\s*usage\s*:", code, re.M), "the usage entry of PLACEHOLDER_INFO is dead once the page is real: remove it"
    assert not re.search(r"""registerPage\(\s*['"]quad['"]""", code), "pages/placeholders.js must not register 'quad' any more (pages/quad.js does, v0.5.9)"
    assert not re.search(r"^\s*quad\s*:", code, re.M), "the quad entry of PLACEHOLDER_INFO is dead once the page is real: remove it"
    assert not re.search(r"""registerPage\(\s*['"]memory['"]""", code), "pages/memory.js registers 'memory' (v0.5.20): the placeholder is dead"
    assert not re.search(r"""registerPage\(\s*['"]onboarding['"]""", code), "pages/onboarding.js registers 'onboarding' (v0.5.19): the placeholder is dead"


def test_usage_js_registers_the_usage_route_once():
    code = blank_js(text_of("pages/usage.js"))
    assert len(re.findall(r"""registerPage\(\s*['"]usage['"]""", code)) == 1, "pages/usage.js registers 'usage' exactly once"


# what may start a column-0 statement of a script that only defines: a declaration, a directive string, a function / literal assigned to a property of a
# declared namespace at any depth (`Charts.geom.stack = function ...`), and the one-line `window.Charts` alias (a plain property write, no DOM, no timer)
DEFINES = re.compile(
    r"(?:const|let|var|function|class|async\s+function)\b|['\"`]"
    r"|[A-Z]\w*(?:\.\w+)+\s*=\s*(?:async\s+)?(?:function\b|\([^()]*\)\s*=>|\w+\s*=>|[{\['\"`\d]|true\b|false\b|null\b)"
    r"|if \(typeof window !== '\s*'\) window\.[A-Z]\w* = [A-Z]\w*;$")


def test_charts_js_only_defines_at_load():
    """`Charts` is a namespace: nothing runs, touches the DOM or starts a timer until a page calls it (charts.js loads on every page)."""
    path = need("charts.js")
    bad = [f"app/static/charts.js:{n}: {t[:100]}" for n, t in top_level_statements(path.read_text(encoding="utf-8")) if not DEFINES.match(t)]
    assert not bad, "top-level statement that is not a declaration (charts.js loads on every page and must not run anything):\n" + "\n".join(bad)


def test_the_define_only_predicate_accepts_declarations_and_flags_calls():
    ok = "const Charts = {\n  a: 1,\n};\nCharts.spark = function (h) {\n  return h;\n};\nCharts.geom.stack = function (d) {\n  return d;\n};\nCharts.N = 4;\nif (typeof window !== 'undefined') window.Charts = Charts;\n"
    assert all(DEFINES.match(t) for _, t in top_level_statements(ok)), top_level_statements(ok)
    for bad in ("setInterval(tick, 1);", "document.title = 'x';", "Charts.timer = setInterval(tick, 1);", "Charts.geom.x = init();", "Charts.start();",
                "if (x) { y(); }", "if (typeof window !== 'undefined') window.addEventListener('x', y);", "window.Charts = boot();"):
        assert not any(DEFINES.match(t) for _, t in top_level_statements(bad)), bad


# ---------------------------------------------------------------- uPlot arrives through loadAsset only

@pytest.mark.parametrize("name", ["charts.js", "pages/usage.js"])
def test_no_script_or_link_element_is_created_for_uplot_by_hand(name):
    code = blank_js(text_of(name))
    bad = [f"app/static/{name}:{line_of(code, m.start())}: {snippet(code, m.start())}"
           for m in re.finditer(r"""createElement\s*\(\s*['"`](?:script|link)['"`]|el\(\s*['"`](?:script|link)['"`]|<\s*script\b""", code)]
    assert not bad, "load uPlot with loadAsset() (core.js), never by building a <script>/<link> here:\n" + "\n".join(bad)


def test_charts_js_loads_the_vendored_uplot_through_loadasset():
    code = blank_js(text_of("charts.js"))
    assert "loadAsset(" in code, "Charts.ready() must use loadAsset (core.js): it dedupes by path and adds the <link>/<script> itself"
    for asset in ("/static/vendor/uplot/uPlot.min.css", "/static/vendor/uplot/uPlot.iife.min.js"):
        assert asset in code, f"charts.js must load {asset}"


def test_usage_js_reaches_uplot_only_through_charts_or_loadasset():
    raw = text_of("pages/usage.js")
    code = blank_js(raw)
    if "vendor/uplot" in code:
        assert "loadAsset(" in code, "usage.js names a uPlot asset: it may only hand it to loadAsset()"
    if re.search(r"\buPlot\b", blank_js(raw, strings=True)):       # the identifier, not a log message that names it
        assert "loadAsset(" in code or re.search(r"\bready\b", code), "usage.js uses uPlot: it must wait for Charts.ready() (which is loadAsset) first"


# ---------------------------------------------------------------- the CSP, per Usage file

EXTRA_JS_BANS = [
    ("data: URI", r"""['"`]\s*data:\s*[\w.+-]+/[\w.+-]+|url\(\s*['"]?\s*data:""", "CSP default-src 'self' blocks data: images; draw with svg() / a file under /static"),
    ("bp5- literal", r"\bbp5-", "Blueprint classes go through el()'s semantic map in core.js"),
    ("absolute URL", r"""\b(?:src|href|url)\s*[:=]\s*['"`]https?://(?!www\.w3\.org/2000/svg)""", "the board only talks to its own origin"),
    ("history.pushState", r"\bhistory\s*\.\s*(?:pushState|replaceState)\b", "navigate() owns the history stack"),
    ("CDN host", r"cdnjs\.cloudflare\.com|cdn\.jsdelivr\.net|unpkg\.com|fonts\.googleapis\.com|fonts\.gstatic\.com|code\.jquery\.com", "vendor it under app/static/vendor"),
]


@pytest.mark.parametrize("name", ["charts.js", "pages/usage.js"])
def test_usage_js_files_carry_no_banned_construct(name):
    raw = text_of(name)
    code = blank_js(raw)
    bad = []
    for label, pattern, advice in JS_BANS + EXTRA_JS_BANS:
        flags = re.I if label == "javascript: URL" else 0
        for m in re.finditer(pattern, code, flags):
            bad.append(f"app/static/{name}:{line_of(code, m.start())}: {label} ({advice}): {snippet(code, m.start())}")
    bare = blank_js(raw, strings=True)
    for m in re.finditer(r"\bstyle\s*:", bare):
        bad.append(f"app/static/{name}:{line_of(bare, m.start())}: style: attribute key (use a class, el.style.setProperty or an SVG attribute): {snippet(code, m.start())}")
    for m in re.finditer(r"""['"`]style['"`]\s*:""", code):
        bad.append(f"app/static/{name}:{line_of(code, m.start())}: 'style' attribute key: {snippet(code, m.start())}")
    assert not bad, "\n".join(bad)


def test_charts_css_is_local_and_inert():
    css = blank_css_comments(text_of("charts.css"))
    bad = []
    for m in re.finditer(r"@import\b", css, re.I):
        bad.append(f"app/static/charts.css:{line_of(css, m.start())}: @import (link the stylesheet from index.html)")
    for m in re.finditer(r"url\(\s*['\"]?\s*data:", css, re.I):
        bad.append(f"app/static/charts.css:{line_of(css, m.start())}: url(data:...) (CSP blocks data: images; use an svg <pattern> or a file under /static)")
    for m in re.finditer(r"url\(\s*['\"]?\s*(?:https?:)?//", css, re.I):
        bad.append(f"app/static/charts.css:{line_of(css, m.start())}: remote url()")
    for m in re.finditer(r"expression\s*\(|behavior\s*:|-moz-binding", css, re.I):
        bad.append(f"app/static/charts.css:{line_of(css, m.start())}: script-capable CSS")
    assert not bad, "\n".join(bad)


# ---------------------------------------------------------------- charts.css contract (tokens, hosts, phone layout)

CHART_TOKENS = ("--st-idle", "--st-working", "--st-waiting", "--st-done", "--st-errored", "--st-ended",
                "--agent-claude", "--agent-codex", "--agent-shell", "--c-unattributed")


def test_charts_css_defines_its_tokens_on_root():
    css = blank_css_comments(text_of("charts.css"))
    roots = "\n".join(re.findall(r":root\s*\{([^}]*)\}", css))
    missing = [t for t in CHART_TOKENS if not re.search(re.escape(t) + r"\s*:", roots)]
    assert not missing, f"app/static/charts.css :root must define {missing} (uPlot reads them with getComputedStyle)"


def test_charts_css_chart_hosts_scroll_vertically_through_the_chart():
    css = blank_css_comments(text_of("charts.css"))
    assert re.search(r"\.chart\b[^{}]*\{[^}]*touch-action\s*:\s*pan-y", css), "`.chart { touch-action: pan-y }`: a swipe over a chart must still scroll the page"
    assert re.search(r"\.chart\.loading\b", css), "the `.chart.loading` skeleton"
    assert re.search(r"max-width\s*:\s*599px", css), "the phone stacking rule (@media (max-width:599px))"


@pytest.mark.parametrize("selector", [".legend", ".seg-unattributed"] + [f".st-{i}" for i in range(6)] + [f".lv{i}" for i in range(5)])
def test_charts_css_styles_the_classes_charts_js_emits(selector):
    css = blank_css_comments(text_of("charts.css"))
    assert re.search(re.escape(selector) + r"(?![\w-])", css), f"app/static/charts.css has no rule for {selector} (charts.js puts it on its nodes)"


# ---------------------------------------------------------------- the service-worker shell

def test_sw_shell_carries_the_usage_assets(lite_client):
    for name in ("charts.css", "charts.js", "pages/usage.js"):
        need(name)
    r = lite_client.get("/sw.js", headers=H)
    assert r.status_code == 200
    m = re.search(r"JSON\.parse\('([^']*)'\)", r.text)
    assert m, "SHELL = JSON.parse('...') not found in the served worker"
    shell = json.loads(m.group(1))
    for must in ("/static/charts.css", "/static/charts.js", "/static/pages/usage.js",
                 "/static/vendor/uplot/uPlot.min.css", "/static/vendor/uplot/uPlot.iife.min.js"):
        assert must in shell, f"{must} is missing from the service-worker SHELL: the Usage page would not open offline"
    assert not any("/demo/" in p for p in shell), "demo fixtures stay out of the shell"


# ---------------------------------------------------------------- demo fixtures

def parse_iso(s):
    return datetime.datetime.fromisoformat(s).timestamp()


def test_demo_series_carries_ctx_and_scost_for_a_live_session():
    """The per-session detail on the Usage page (ctx % and session $ over 24 h) renders in demo mode for a session the state fixture runs."""
    s = demo("series.json")
    live = live_sessions()
    n = len(s["t"])
    assert {"rl_5h:claude", "rl_7d:claude"} <= set(s["series"]), "the limits chart still has its two series"
    scost = sorted(k for k in s["series"] if k.startswith("scost:"))
    assert scost, "series.json needs scost:<tmux> next to ctx:<tmux> (GET /api/series?series=ctx,scost&key=<tmux>&since=24h)"
    for key in scost:
        tmux = key[len("scost:"):]
        assert tmux in live, f"{key}: not a live session of the state fixture"
        assert "ctx:" + tmux in s["series"], f"{key} has no ctx:{tmux} beside it"
        values = s["series"][key]
        assert len(values) == n, key
        known = [v for v in values if v is not None]
        assert known and all(isinstance(v, (int, float)) and v >= 0 for v in known), key
        assert known == sorted(known), f"{key}: a session's cost only grows"
        assert known[-1] == pytest.approx(live[tmux]["stats"]["cost_usd"], abs=1e-6), f"{key} ends at the session's cost_usd in the state fixture"
        assert s["series"]["ctx:" + tmux][-1] == live[tmux]["stats"]["context_pct"]


def test_demo_summary_shows_every_state_the_usage_page_draws():
    fx = demo("usage_summary.json")
    daily = fx["daily"]
    assert len(daily) == 30
    week = daily[-7:]
    # the seven-day view (the default range) already shows the unattributed segment and a hatched zero day
    assert any("(unattributed)" in d["by_project"] for d in week), "'(unattributed)' spend inside the last 7 days of `daily`"
    assert any(d["zero"] and d["total"] == 0 for d in week), "a zero day inside the last 7 days (drawn as a hatched empty bar)"
    assert any(not d["zero"] for d in week)
    # today's window is tied to the state fixture's cost record by tests/test_static.py, which has no '(unattributed)' entry; 7d and 30d carry it
    for win in ("7d", "30d"):
        rows = {p["project"]: p for p in fx["windows"][win]["by_project"]}
        assert "(unattributed)" in rows and rows["(unattributed)"]["total"] > 0, f"windows.{win}.by_project has its own '(unattributed)' row"
        assert rows["(unattributed)"]["hours"] == 0, "no hours: nothing the board launched"
        assert len(rows) >= 4, "enough projects to need the 'top 6 + other' cut on 30d, or at least a real table"
    assert "(unattributed)" in {t["project"] for t in fx["top_sessions"]}, "a top session the board did not start"
    # unpriced sessions: a hatched pattern and the 'N sessions have tokens but no price' note
    assert fx["unpriced"], "at least one unpriced session"
    assert all(u["tokens"] > 0 for u in fx["unpriced"])
    assert not {u["key"] for u in fx["unpriced"]} & {t["key"] for t in fx["top_sessions"]}, "an unpriced session has no total to rank"
    # limit episodes inside the last week, one per kind
    now = parse_iso(fx["generated_at"])
    recent = {e["kind"] for e in fx["episodes"] if 0 <= now - parse_iso(e["at"]) <= 7 * 86400}
    assert {"5h", "7d"} <= recent, f"episodes within the last 7 days must include a 5h and a 7d one, found {sorted(recent)}"
    assert [e["at"] for e in fx["episodes"]] == sorted(e["at"] for e in fx["episodes"])


def test_demo_top_sessions_give_open_buttons_to_live_sessions_only():
    """A row whose uuid is a live session's claude_session_id gets Open -> #/s/<tmux>; the others link to their project."""
    fx = demo("usage_summary.json")
    ids = {s.get("claude_session_id"): tmux for tmux, s in live_sessions().items() if s.get("claude_session_id")}
    rows = fx["top_sessions"]
    live_rows = [t for t in rows if t["key"].split(":", 1)[1] in ids]
    assert len(live_rows) >= 2, "two rows match a live session (an Open button each, one tap to the terminal peek)"
    assert len(rows) - len(live_rows) >= 2, "and the rest do not (a project link instead)"
    series = demo("series.json")["series"]
    detail = [t for t in live_rows if "scost:" + ids[t["key"].split(":", 1)[1]] in series]
    assert detail, "at least one live row has ctx/scost in series.json, so its expanded detail draws a chart"
    assert len({t["key"] for t in rows}) == len(rows)
    assert any(t["models"] and all(m.startswith(("claude-", "gpt-")) for m in t["models"]) for t in rows), \
        "models come as the ccusage full names ('claude-opus-5-5'): the chips shorten them"


def test_demo_series_events_fixture_is_a_state_gantt_of_five_live_sessions():
    body = demo("series_events.json")
    live = live_sessions()
    events = body["events"]
    assert body["truncated"] is False
    assert events == sorted(events, key=lambda e: e["t"]), "oldest first, like the endpoint"
    for e in events:
        assert {"t", "key", "v", "m"} <= set(e) and isinstance(e["t"], int) and e["v"] in (0, 1, 2, 3, 4, 5), e
        assert {"p", "r", "s", "a"} <= set(e["m"]), e
        assert e["key"] in live, f"{e['key']} is not a session of the state fixture"
    keys = {e["key"] for e in events}
    assert len(keys) == 5, f"state events for 5 tmux keys (four Claude sessions and the live Codex one, v0.5.12), found {len(keys)}"
    assert {e["m"]["a"] for e in events} == {"claude", "codex"}, "a teal Codex row among the Claude ones"
    epoch = demo("state.json")["demo"]["epoch"]
    assert body["demo"]["epoch"] == epoch, "the fixture clock rides along (like state.json) so demoApi can shift the events to now"
    assert all(epoch - 24 * 3600 <= e["t"] <= epoch for e in events), "all inside the 24 h before the fixture clock"
    assert {0, 1, 2, 3, 5} <= {e["v"] for e in events}, "a working / waiting / done / idle / ended mix"
    last = {}
    for e in events:
        last[e["key"]] = e["v"]
    assert 5 in last.values() and any(v != 5 for v in last.values()), "an ended row (it fades) among rows that are still running"
    for key, v in last.items():
        assert v == STATE_CODES[live[key]["state"]], f"{key}: the Gantt's last state must be the state fixture's ({live[key]['state']})"
    for e in events:
        s = live[e["key"]]
        assert e["m"]["a"] == (s.get("agent") or "claude") and e["m"]["s"] == s["name"], e


def test_core_maps_the_events_path_to_its_own_fixture():
    """/api/series/events must reach series_events.json: the '/api/series' prefix test in demoApi would otherwise answer it with series.json,
    whose `events` are limit episodes ({key:'5h'}), and the Gantt would draw them as sessions. Add, BEFORE the '/api/series' line in core.js:
        else if (bare.startsWith('/api/series/events')) name = 'series_events';"""
    core = blank_js(text_of("core.js"))
    ev = re.search(r"""bare\.startsWith\(\s*['"]/api/series/events['"]\s*\)\s*\)\s*name\s*=\s*['"]series_events['"]""", core)
    assert ev, "core.js demoApi has no `bare.startsWith('/api/series/events')) name = 'series_events'` line"
    plain = re.search(r"""bare\.startsWith\(\s*['"]/api/series['"]\s*\)\s*\)\s*name\s*=\s*['"]series['"]""", core)
    assert plain and ev.start() < plain.start(), "the /api/series/events line must come BEFORE the /api/series prefix line (first match wins)"


def test_usage_page_classes_never_collide_with_the_legacy_usage_strip():
    """style.css keeps a bare `.usage` rule (the v0.4 top strip: flex row, 12 px, nowrap below 700 px). The page root used that class once
    and squeezed every section into an 82 px column at 390. Every class charts.css styles for the page must not also be a bare selector in
    style.css or pages.css (shared vocabulary such as .actions or .chip is fine: charts.css does not redefine those)."""
    import re
    usage_js = (STATIC / "pages" / "usage.js").read_text()
    assert "class: 'usage'" not in usage_js and "class: 'usage " not in usage_js
    charts_css = (STATIC / "charts.css").read_text()
    own = set(re.findall(r"#page \.([a-z][a-z0-9-]*)", charts_css))
    legacy = (STATIC / "style.css").read_text() + (STATIC / "pages.css").read_text()
    for cls in sorted(own):
        assert not re.search(rf"^\.{re.escape(cls)}(?![a-z0-9-])", legacy, re.M), f"charts.css styles '{cls}' for the Usage page, but style.css/pages.css define a bare .{cls} rule too"
    assert "#page .usage " not in charts_css and "#page .usage{" not in charts_css
    assert "fmt: 'usd'" in usage_js, "the per-session cost detail chart is formatted as dollars, not percent"
