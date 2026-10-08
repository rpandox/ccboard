#!/usr/bin/env bash
# qa_terminal.sh: drive the mobile terminal page (/term/<session>) through the phone, tablet and desktop viewports and check it.
#
#   scripts/qa_terminal.sh [BASE_URL] [OUT_DIR]
#
#   BASE_URL  a board started with the dev bypass, default http://127.0.0.1:8777 (see README "Development"): with
#             CCBOARD_DEV_BYPASS_USER set the board serves scripts/dev/fake_tty/ at /tty/, a stand-in for ttyd (window.term,
#             .xterm-screen, a wheel counter), so the page runs without ttyd. Without the bypass /tty/ is a 404 and the script stops (exit 2).
#   OUT_DIR   screenshots land here as term-<width>.png, default ${TMPDIR:-/tmp}/ccboard-qa-terminal
#             (keep it under /tmp, $TMPDIR or the repo: the gstack browse CLI refuses to write anywhere else)
#
# Needs the gstack browser CLI ($B, default ~/.claude/skills/gstack/browse/dist/browse), like scripts/qa-ui.sh.
#
# Viewports: 390x844 (phone, single column), 768x1024 (tablet, single column), 1280x800 (desktop, terminal + a side column of 320 to 360 px).
#
# Assertions per viewport (all in the browser, over the page's own DOM and the fake tty inside its iframe):
#   wrap      #ttywrap does not scroll (scrollHeight <= clientHeight, same for width), the page itself does not scroll, the iframe fills
#             #ttywrap, and the terminal keeps at least 40 % of the window height
#   bounce    body and html have overscroll-behavior none, body.term is position:fixed, and TermKit.bind hardened the iframe document
#             (overscroll none on its root, touch-action none on .xterm)
#   overflow  document.documentElement.scrollWidth <= innerWidth, and document.body.scrollWidth <= innerWidth (body.term is position:fixed
#             with overflow hidden, so the root's scrollWidth never shows a chip poking out of the side column: the body's does)
#   keys      every visible key-bar key (.kb-key) is at least 44 px tall and wide, and so is every scroll-rail button. One exception: in the side
#             column (840 px and up) with a mouse (no html.force-coarse, pointer:fine) a key is 32 px tall; its width stays 44 px or more
#   targets   390 px only, with html.force-coarse: every visible button and a.bp5-button is at least 44 px tall
#   composer  #sendtext is visible and inside the window, 16 px (no iOS zoom) under force-coarse at 390
#   layout    below 840 px the key bar and the composer sit under the terminal; from 840 px the composer is in a column of 320 to 360 px to its right
#   touch     a synthetic one-finger swipe on the fake .xterm-screen produces wheel events (window.__wheel grows, __lastDelta set), a swipe
#             starting 10 px from the left edge produces none
#   scroll    pressing PgUp on the scroll rail POSTs /api/sessions/<name>/scroll {dir:'up'} (seen by wrapping fetch in the page; with
#             QA_SERVER_LOG also in the server log) and sends no raw keys (the key bar has no PgUp / PgDn / Top / Bottom: the rail owns them)
#   history   needs the session to exist on the board's tmux and to be a shell pane: after PgUp the History chip shows (GET /pane
#             in_mode), tapping it leaves history; "skip" otherwise
#   compact   390 px only: with the key-bar mode forced to compact only Esc, Tab, Shift+Tab, Ctrl-C/^C, Enter and More are visible, and More
#             reveals the rest
#   tune      the tuning strip (#tune). Fake board only: the page's fetch is wrapped so that GET /api/sessions/<name> answers an idle Claude
#             row (a REALISTIC one: a task, a two-line last prompt and a two-line last message, so the context strip takes the room it takes
#             on a real turn and the 40 % rule measures the real case) and POST /command, /keys and /prompt answer like the server (a read
#             command like /usage carries `screen`), so no session, agent or tmux is needed and nothing is typed anywhere. Checks, in order:
#             the strip is visible with the chips Compact, Clear, Usage, Rename, Context, Status, the Effort segments (ultracode) and the
#             Model chips (opus), none disabled; below 840 px it is ONE scrolling row (the chips share one top, the strip is under 60 px
#             tall, the edge fade `more-r` is set, and the current effort and model chips and the `model` label were scrolled into view);
#             at 390 with html.force-coarse every chip is at least 44x44 and the strip does not squeeze the terminal under 40 % of the
#             window or make the page scroll; from 840 px every #tune button's right edge is inside #tune (the segments wrap inside the
#             side column); document.body.scrollWidth <= innerWidth at every width; the header `tune` button (#headtools) hides and
#             restores the strip and stores ccboard:term:tune (0 / 1); with the row 'working' every chip is disabled with a title that
#             says why and the send button reads "queue"; back to idle, Usage posts /command {cmd:'usage'} and opens `dialog.readout` with
#             the captured text, and the next /command (Compact) is preceded by an Escape through /keys; a 409 answer toasts the reason and
#             leaves no `pending` chip; ONE tap on the ultracode segment posts /command {cmd:'effort', arg:'ultracode on'} (term.js
#             EFFORT_ARG: claude.py V19, `/effort ultracode on`) with no dialog or sheet in between; Clear needs two taps and posts
#             confirm:true. On a real board (QA_REAL_TTYD=1) only the 44 px / squeeze / overflow check runs, on the real row, when the strip
#             is visible (a shell session has none: skip).
#   quick     the quick-reply editor: window.prompt / confirm / alert are replaced by functions that throw and count, the pencil button
#             (#qtools, title "Edit quick replies") opens `dialog.qr-editor` with one input per reply (16 px under force-coarse), Cancel
#             closes it, localStorage ccboard:quick:<name> is untouched by that, and the counter is still 0
#   font      without ccboard:term:font=1 the font spike did nothing: no JetBrains Mono in term.options.fontFamily, no FontFace built
#             (fake tty), TermKit.fontState 'off'. After the table, the spike itself (fake board only, first viewport that ran): a stand-in
#             FontFace (ccboard:fake:font=stub) goes loading -> on with the family set BEFORE a fit(), and the cols x rows stay put over more
#             fits; a rejected load and a missing FontFace end as failed with ttyd's fonts untouched and the page still bound; the browser's
#             own FontFace loads the vendored woff2 for real. QA_FONT=1 with QA_REAL_TTYD=1 runs the on-box version against the real ttyd
#             (cols x rows at 13 and 11 px, off and on, each stable over repeated fits) and prints the numbers
#   kbd       390 px only, fake board only (a headless window has no soft keyboard): localStorage ccboard:fake:vv=520 makes the fake tty shrink
#             the page's visualViewport to 520 px and fire `resize` (scripts/dev/fake_tty/fake_tty.js; ?vv=<height> on the page URL does the
#             same). With the faked row, the strip and the context strip are on screen first; then, under force-coarse, body.kbd is set,
#             --vvh is 520px, #sendform ends at or above 520, #tune, #ctxstrip and #quickrow (the pencil's home) are gone, and the quick-reply
#             editor (opened through quickReplyEditor(), the call the pencil makes) has its dialog, Save and Cancel inside the visual viewport
#             (0 to 520 px) and no input focused (a focused input pops the keyboard up before anything was tapped). A screenshot of the editor lands in OUT as term-390-kbd-editor.png
#   console   `console --errors` is empty after the run (CSP violations, failed fetches); when the session is absent on the board the
#             expected 404/503 resource failures of /api/sessions/<name> are ignored and the table says so
#
# QA_REAL_TTYD=1 aims the script at a real board with a real ttyd (BASE_URL e.g. the tailnet URL of the box, no dev bypass; QA_TMUX must name a
# session that exists THERE, the script cannot create one on a remote tmux). It skips what only the fake tty can answer (the touch counter,
# the faked strip, the font stand-ins) and keeps wrap, bounce, overflow, keys, targets, composer, layout, scroll, history, compact,
# quick, the font-off check and console, which read the page and the real iframe.
#
# The page reads GET /api/sessions/<name> and /pane every 3 s. With a session of that name on the board's tmux server everything is
# strict; without one the page shows "ended" and only the console filter above changes. To get a session:
#   - start one yourself on the board's tmux socket (the name must look like <project>--<repo>--<session>, default qa--terminal--s1), or
#   - QA_TMUX_CREATE=1: the script creates it (a shell with 400 lines of output) on `tmux -L ${CCBOARD_TMUX_SOCKET:-ccboard}` and kills it
#     again at the end. Run it with the same TMUX_TMPDIR / CCBOARD_TMUX_SOCKET as the board, e.g.
#       TMUX_TMPDIR=/tmp CCBOARD_TMUX_SOCKET=ccboard-qa CCBOARD_DEV_BYPASS_USER=dev uvicorn app.main:app --port 8777
#       TMUX_TMPDIR=/tmp CCBOARD_TMUX_SOCKET=ccboard-qa QA_TMUX_CREATE=1 scripts/qa_terminal.sh http://127.0.0.1:8777
#
# Environment: B (browse binary), QA_TMUX (session name), QA_TMUX_CREATE (1 = create and remove it), QA_HEADER (e.g.
# 'Tailscale-User-Login: demo@example.com' when the board is not in dev bypass: /tty/ is then not the fake and the script stops),
# QA_SETTLE (seconds to let a page settle, default 0.6), QA_TICK (seconds between polls of the newer checks, default 0.25), QA_START_WAIT (seconds between the browser-ready probes, default 1), QA_WIDTHS (space-separated widths to run, e.g. "390 1280"; default all),
# QA_SERVER_LOG (the board's log file: the scroll check then also greps it), QA_SHARED_BROWSER (1 = drive the shared per-repo browse
# server instead of a private one that is stopped at the end), QA_REAL_TTYD (1 = a real board and ttyd, see above), QA_FONT (1 = with
# QA_REAL_TTYD, also run the on-box font spike), QA_FONT_W (width of the viewport the font spike runs at, default the first one run).
# Exit status: 0 all assertions pass, 1 a FAIL, 2 nothing to drive the page with, the board is unreachable or /tty/ is not the fake
# (with QA_REAL_TTYD=1: or it IS the fake).
set -u
set -f   # no globbing: the URLs carry ? and &

BASE="${1:-http://127.0.0.1:8777}"
OUT="${2:-${TMPDIR:-/tmp}/ccboard-qa-terminal}"
BASE="${BASE%/}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
B="${B:-$HOME/.claude/skills/gstack/browse/dist/browse}"
SETTLE="${QA_SETTLE:-0.6}"
TICK="${QA_TICK:-0.25}"      # the step of every wait loop of the tune, quick and font checks (tests/test_dev_harness.py runs the script with 0.01)
NAME="${QA_TMUX:-qa--terminal--s1}"
SOCK="${CCBOARD_TMUX_SOCKET:-ccboard}"
REAL="${QA_REAL_TTYD:-0}"

if [ ! -x "$B" ]; then
  echo "qa_terminal: the gstack browse CLI ($B) was not found; set B=/path/to/browse" >&2
  exit 2
fi

CURL_H=()
[ -n "${QA_HEADER:-}" ] && CURL_H=(-H "$QA_HEADER")
http_code() { curl -s -o /dev/null -w '%{http_code}' ${CURL_H[@]+"${CURL_H[@]}"} "$1" 2>/dev/null || true; }

code="$(http_code "$BASE/")"
case "$code" in
  200) ;;
  000|"") echo "qa_terminal: $BASE is not reachable (start the board with CCBOARD_DEV_BYPASS_USER, see README Development)" >&2; exit 2 ;;
  403) echo "qa_terminal: $BASE answered 403: start the board with CCBOARD_DEV_BYPASS_USER=<user> or pass QA_HEADER" >&2; exit 2 ;;
  *) echo "qa_terminal: warning: GET $BASE/ answered $code" >&2 ;;
esac
# issue #100: the fake-tty (dev bypass) mode refuses a board that does not report dev.sandboxed=true in /api/state, so a QA click can never
# start a real agent against the real home. QA_REAL_TTYD=1 drives the box itself (no dev bypass) and is not subject to it.
if [ "$REAL" != 1 ]; then
  sandboxed="$(curl -s ${CURL_H[@]+"${CURL_H[@]}"} "$BASE/api/state" 2>/dev/null | python3 -c 'import json,sys; print("yes" if json.load(sys.stdin).get("dev",{}).get("sandboxed") is True else "no")' 2>/dev/null || true)"
  if [ "$sandboxed" != yes ]; then
    echo "qa_terminal: $BASE does not report dev.sandboxed=true in /api/state: refusing to run against it. Start the dev board with PROJECTS_DIR, CCBOARD_DATA_DIR, CLAUDE_CONFIG_DIR and CODEX_HOME on temp paths and scripts/dev/fake_agents first on PATH (README, Development)" >&2
    exit 2
  fi
fi
tty_body="$(curl -s ${CURL_H[@]+"${CURL_H[@]}"} "$BASE/tty/" 2>/dev/null || true)"
tty_code="$(http_code "$BASE/tty/")"
if [ "$REAL" = 1 ]; then
  # a real ttyd behind tailscale serve: /tty/ must NOT be the fake (that would make every fake-only skip a silent pass)
  if printf '%s' "$tty_body" | grep -q fake_tty; then
    echo "qa_terminal: QA_REAL_TTYD=1 but $BASE/tty/ is the fake tty: unset QA_REAL_TTYD for a dev-bypass board" >&2
    exit 2
  fi
  [ "$tty_code" = 200 ] || echo "qa_terminal: warning: GET $BASE/tty/ answered $tty_code (is tailscale serve mapping /tty to ttyd?)" >&2
  if [ "${QA_TMUX_CREATE:-0}" = 1 ]; then echo "qa_terminal: QA_TMUX_CREATE is ignored with QA_REAL_TTYD=1 (the session must already exist on the board's own tmux)" >&2; fi
elif [ "$tty_code" != 200 ] || ! printf '%s' "$tty_body" | grep -q fake_tty; then
  echo "qa_terminal: $BASE/tty/ is not the fake tty: start the board with CCBOARD_DEV_BYPASS_USER set (scripts/dev/fake_tty is mounted only then), or QA_REAL_TTYD=1 for a real ttyd" >&2
  exit 2
fi
mkdir -p "$OUT" || exit 2

# ---- the session the page watches
CREATED=0
SERVER_STARTED=0
tmux_sock() { tmux -L "$SOCK" "$@"; }
if [ "${QA_TMUX_CREATE:-0}" = 1 ] && [ "$REAL" != 1 ]; then
  if command -v tmux >/dev/null 2>&1 && ! tmux_sock list-sessions >/dev/null 2>&1; then SERVER_STARTED=1; fi   # no server yet: new-session below starts one
  if ! command -v tmux >/dev/null 2>&1; then
    echo "qa_terminal: QA_TMUX_CREATE=1 but tmux is not installed" >&2
  elif tmux_sock has-session -t "=$NAME" 2>/dev/null; then
    echo "qa_terminal: session $NAME already exists on socket $SOCK; using it (not removed at the end)"
  else
    if tmux_sock -f "$HERE/../tmux.conf" new-session -d -s "$NAME" -x 120 -y 30 -c "$HERE/.." 'sh -c "seq 1 400; exec sh"' 2>/dev/null; then
      CREATED=1
    else
      echo "qa_terminal: could not create session $NAME on tmux socket $SOCK" >&2
    fi
  fi
fi

cleanup() {
  # `browse stop` starts a server first when the state file points at a dead one: stop only a browser this run started
  if [ "${QA_SHARED_BROWSER:-0}" != 1 ] && [ -f "${BROWSE_STATE_FILE:-/nonexistent}" ]; then "$B" stop >/dev/null 2>&1; fi
  if [ "$CREATED" = 1 ]; then
    tmux_sock kill-session -t "=$NAME" >/dev/null 2>&1
    # tmux.conf sets exit-empty off, so the server this run started would outlive its only session: stop it, but only when it was ours
    # (it did not exist before) and nothing else has started a session on it since
    if [ "$SERVER_STARTED" = 1 ] && [ -z "$(tmux_sock list-sessions 2>/dev/null)" ]; then tmux_sock kill-server >/dev/null 2>&1; fi
  fi
}
trap cleanup EXIT

# A private browser by default: the gstack browse server is shared per repo, so another agent or a stray tab resizing or
# navigating it mid-run turns assertions into noise. QA_SHARED_BROWSER=1 drives the shared one instead (e.g. to watch it).
if [ "${QA_SHARED_BROWSER:-0}" != 1 ]; then
  mkdir -p "$OUT/.browse" || exit 2
  export BROWSE_STATE_FILE="$OUT/.browse/browse.json"
fi

SESSION=absent
for _ in 1 2 3 4 5 6 7 8; do
  [ "$(http_code "$BASE/api/sessions/$NAME")" = 200 ] && { SESSION=present; break; }
  [ "$CREATED" = 1 ] || break
  sleep 0.5
done
PANE_ALT=unknown
if [ "$SESSION" = present ]; then
  PANE_ALT="$(curl -s ${CURL_H[@]+"${CURL_H[@]}"} "$BASE/api/sessions/$NAME/pane" 2>/dev/null | python3 -c '
import json, sys
try:
    print("app" if json.load(sys.stdin).get("alt") else "shell")
except Exception:
    print("unknown")' 2>/dev/null)"
fi

VIEWPORTS="390x844:single 768x1024:single 1280x800:wide"

FAILS=0
DETAILS=""
fail() { FAILS=$((FAILS + 1)); DETAILS="${DETAILS}  $1"$'\n'; }

js() { "$B" js "$1" 2>/dev/null | tr -d '\r' | tail -n 1; }

row() { printf '%-10s %-5s %-6s %-8s %-5s %-8s %-9s %-7s %-6s %-7s %-8s %-8s %-5s %-5s %-5s %-5s %-8s %s\n' "$@"; }

# Load the terminal page at one size: about:blank first (a real document load every time, no stale iframe), then wait for the page to
# have built its iframe and for TermKit.bind to have run in it (it marks .xterm with touch-action:none through the CSSOM).
load_term() {   # width height url
  local tries=0 got
  while [ "$tries" -lt 3 ]; do
    tries=$((tries + 1))
    "$B" goto about:blank >/dev/null 2>&1
    "$B" viewport "${1}x${2}" >/dev/null 2>&1
    "$B" console --clear >/dev/null 2>&1
    "$B" goto "$3" >/dev/null 2>&1
    for _ in $(seq 1 40); do                       # up to 10 s for the iframe, window.term and the bind
      got="$(js "(() => { const f = document.querySelector('#ttywrap iframe'); if (!f || !document.body.classList.contains('term')) return 0; try { const d = f.contentDocument; const x = d && d.querySelector('.xterm'); return x && f.contentWindow.term && x.style.touchAction === 'none' ? 1 : 0; } catch (e) { return 0; } })()")"
      case "$got" in 1) break ;; esac              # empty = the browse server is still starting
      sleep "$TICK"
    done
    sleep "$SETTLE"
    if [ "$(js "window.innerWidth")" = "$1" ] && [ "$got" = 1 ]; then return 0; fi
  done
  return 1
}

# A press the way a finger delivers it: pointerdown, pointerup, then the click a browser adds for a mouse (the key bar acts on pointerdown
# and ignores that click, a plain button acts on the click): $1 is a JS expression for the element.
PRESS_JS='const press = (n) => { const o = { bubbles: true, cancelable: true, pointerType: "touch", pointerId: 1, isPrimary: true, button: 0 }; n.dispatchEvent(new PointerEvent("pointerdown", o)); n.dispatchEvent(new PointerEvent("pointerup", o)); n.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, detail: 1 })); };'

# Poll a JS expression until it returns 1: wait_js EXPR [tries=24] [delay=$TICK]
wait_js() {
  local n="${2:-24}" d="${3:-$TICK}" i
  for i in $(seq 1 "$n"); do [ "$(js "$1")" = 1 ] && return 0; sleep "$d"; done
  return 1
}

# Make the page poll now instead of in up to 3 s (term.js polls again on visibilitychange).
repoll() { js "(() => { document.dispatchEvent(new Event('visibilitychange')); return 1; })()" >/dev/null; }

# Press a #tune chip by its (lower-case) label: ok | missing | disabled
press_chip() {
  js "(() => { $PRESS_JS const n = [...document.querySelectorAll('#tune button')].find((b) => b.textContent.trim().toLowerCase() === '$1'); if (!n || n.getClientRects().length === 0) return 'missing'; if (n.disabled) return 'disabled'; press(n); return 'ok'; })()"
}

# JS expression: how many POST /command {cmd: $1 [, arg: $2]} the page's fetch stub has seen (the leading slash of cmd is ignored)
fake_cmd_count() {
  local t="window.__qaFake.posts.filter((x) => x.path === 'command' && String(x.body.cmd).replace(/^\\//, '') === '$1'"
  [ -n "${2:-}" ] && t="$t && x.body.arg === '$2'"
  printf '%s' "$t).length"
}

# The page's fetch, wrapped (installed and removed around the tune checks): an idle Claude row, and the three POST routes the strip uses.
# window.__qaFake.row is the row GET /api/sessions/<name> answers, .posts records every POST in order, .next409 makes the next /command a 409.
# No shell_version in the row: the page reloads itself when the version it first saw changes.
qa_fake_js() {
  cat <<'EOF_FAKE'
(() => {
  if (window.__qaFake) return 'installed';
  const NAME = '__NAME__';
  const API = '/api/sessions/' + encodeURIComponent(NAME);
  const orig = window.fetch;
  const prev = orig.bind(window);
  // a realistic row for the 40 % rule: a task (title + phase + the Tasks link), a last prompt and a last message that each take the context
  // strip's two clamped lines at 390 px; the strip, the key bar, the quick row and the composer then take what they take on a real turn
  const prompt = 'Refactor the session poller so that a stale pane capture never overwrites a newer statusline, and keep the 3 s cadence the page relies on.';
  const message = 'Done. I split the poller into a fetch and a merge step and added the missing test. The suite is green: 41 passed, 0 failed. Next up is the review of the merge step.';
  const fake = { orig, posts: [], next409: false, row: { name: NAME, agent: 'claude', state: 'idle', project: 'qa', repo: 'terminal', last_prompt: prompt, last_message: message, task: { id: 7, title: 'Make the tuning strip one row', phase: 'implement' }, pending: [], flags: {}, stats: { model: 'opus', effort: 'high', fast: false, context_pct: 31, session_name: 'qa' }, viewers: 1, win: {} } };
  const reply = (code, body) => Promise.resolve(new Response(JSON.stringify(body), { status: code, headers: { 'Content-Type': 'application/json' } }));
  window.fetch = (u, o) => {
    const url = String((u && u.url) || u).split('?')[0];
    const method = String((o && o.method) || 'GET').toUpperCase();
    let body = {};
    try { body = o && o.body ? JSON.parse(o.body) : {}; } catch (e) { body = {}; }
    if (url === API && method === 'GET') return reply(200, fake.row);
    if (method === 'POST' && url === API + '/keys') { fake.posts.push({ path: 'keys', body }); return reply(200, { ok: true }); }
    if (method === 'POST' && url === API + '/prompt') { fake.posts.push({ path: 'prompt', body }); return reply(200, { ok: true, pasted: true, queued: fake.row.state === 'working' }); }
    if (method === 'POST' && url === API + '/command') {
      fake.posts.push({ path: 'command', body });
      if (fake.next409) { fake.next409 = false; return reply(409, { error: 'the session is busy, try again in 5 s', state: 'working', wait_kind: null, retry: 5 }); }
      const cmd = String(body.cmd || '').replace(/^\//, '');
      const out = { ok: true, sent: ('/' + cmd + ' ' + (body.arg || '')).trim(), verified: false };
      if (['usage', 'context', 'status', 'cost'].indexOf(cmd) >= 0) out.screen = 'QA readout for /' + cmd + ' weekly limit 12 percent used';
      return reply(200, out);
    }
    return prev(u, o);
  };
  window.__qaFake = fake;
  return 'ok';
})()
EOF_FAKE
}

qa_unfake_js() { echo "(() => { if (window.__qaFake) { window.fetch = window.__qaFake.orig; delete window.__qaFake; } return 1; })()"; }

TUNE_BTNS="[...document.querySelectorAll('#tune button')].filter((b) => b.getClientRects().length > 0)"

# The tuning strip against the faked row (fake board). Sets c_tune; every failed step adds a clause to the one detail line.
tune_fake_checks() {
  local t_fail="" got
  if [ "$(js "$(qa_fake_js | sed "s/__NAME__/$NAME/g")")" != ok ]; then c_tune=FAIL; fail "$tag: tune: could not install the fetch stub in the page"; return; fi
  js "(() => { try { localStorage.removeItem('ccboard:term:tune'); } catch (e) { /* ignore */ } window.__qaFake.row.state = 'idle'; return 1; })()" >/dev/null   # no stored choice from an earlier run: the default (shown from 700 px of height)
  repoll
  if ! wait_js "(() => { const s = document.querySelector('#tune'); return s && !s.classList.contains('hidden') && s.getClientRects().length > 0 && $TUNE_BTNS.length > 0 ? 1 : 0; })()" 32; then
    js "$(qa_unfake_js)" >/dev/null
    c_tune=FAIL; fail "$tag: tune: no visible #tune with buttons within 8 s of an idle Claude row (a strip under the context strip, hidden only for shell rows)"
    return
  fi
  # presence, labels, nothing disabled on an idle row
  got="$(js "(() => { const bs = $TUNE_BTNS; const labels = bs.map((b) => b.textContent.trim().toLowerCase()); const bad = [];
    for (const need of ['compact', 'clear', 'usage', 'rename', 'context', 'status', 'opus', 'ultracode']) if (labels.indexOf(need) < 0) bad.push('no \"' + need + '\" chip');
    if (bs.length < 12) bad.push('only ' + bs.length + ' chips (Compact Clear Usage Rename Context Status + 6 effort + 4 model = 16)');
    const off = bs.filter((b) => b.disabled); if (off.length) bad.push(off.length + ' chips disabled on an idle row: ' + off.slice(0, 4).map((b) => b.textContent.trim()).join(','));
    return bad.length ? bad.join('; ') + ' [chips: ' + labels.join(',') + ']' : 'ok'; })()")"
  [ "$got" = ok ] || t_fail="$t_fail chips: $got;"
  # the strip must not squeeze the terminal or make the page scroll; 44 px chips on the phone (coarse forced earlier). Below 840 px it is ONE
  # scrolling row with an edge fade and the current chips in view; from 840 px two segmented controls over a command grid, all inside the column
  got="$(js "(() => { const bad = []; const bs = $TUNE_BTNS; const wrap = document.querySelector('#ttywrap').getBoundingClientRect(); const r = document.documentElement; const tune = document.querySelector('#tune'); const tr = tune.getBoundingClientRect();
    if (wrap.height < innerHeight * 0.4) bad.push('the terminal is only ' + Math.round(wrap.height) + ' px of ' + innerHeight + ' (' + Math.round(wrap.height / innerHeight * 100) + ' %) with the strip, the rule is 40 %');
    if (r.scrollWidth > innerWidth) bad.push('page scrollWidth ' + r.scrollWidth + ' > ' + innerWidth);
    if (document.body.scrollWidth > innerWidth) bad.push('body scrollWidth ' + document.body.scrollWidth + ' > ' + innerWidth + ' (something pokes out of the page: body.term is position:fixed, so only the body sees it)');
    if (r.scrollHeight > innerHeight + 1 || document.body.scrollHeight > innerHeight + 1) bad.push('the page scrolls vertically (' + r.scrollHeight + ' > ' + innerHeight + ')');
    if (innerWidth === 390) { const small = bs.filter((b) => b.offsetHeight < 44 || b.offsetWidth < 44); if (small.length) bad.push(small.length + ' chips under 44x44: ' + small.slice(0, 5).map((b) => b.textContent.trim() + '=' + b.offsetWidth + 'x' + b.offsetHeight).join(',')); }
    if (innerWidth >= 840) {
      const out = bs.filter((b) => b.getBoundingClientRect().right > tr.right + 0.5); if (out.length) bad.push(out.length + ' chips poke out of #tune on the right (' + out.slice(0, 4).map((b) => b.textContent.trim() + ' ' + Math.round(b.getBoundingClientRect().right)).join(', ') + ' > ' + Math.round(tr.right) + '): the segments must wrap inside the side column');
    } else {
      const tops = new Set(bs.map((b) => Math.round(b.getBoundingClientRect().top))); if (tops.size !== 1) bad.push('the chips sit on ' + tops.size + ' rows, expected ONE scrolling row');
      if (tr.height > 60) bad.push('the strip is ' + Math.round(tr.height) + ' px tall, expected about 52 (4 + 44 + 3 + 1)');
      const row = tune.querySelector('.tune-row'); const scrolls = !!row && row.scrollWidth > row.clientWidth + 1;
      if (!row) bad.push('no .tune-row');
      else if (innerWidth === 390 && !scrolls) bad.push('the row does not scroll at 390 (' + row.scrollWidth + ' <= ' + row.clientWidth + '): about 1000 px of chips in a 374 px box');   // 768 is layout-dependent: only the fade is demanded there
      if (scrolls && !tune.classList.contains('more-r') && !tune.classList.contains('more-l')) bad.push('a row that scrolls has no edge fade (more-l / more-r on #tune)');
      const inside = (n) => { const b = n.getBoundingClientRect(); return b.left >= tr.left - 1 && b.right <= tr.right + 1; };
      const lbl = [...tune.querySelectorAll('.tune-lbl')].find((n) => n.textContent.trim() === 'model'); if (!lbl || !inside(lbl)) bad.push('the \"model\" label was not scrolled into view');
      for (const t of ['high', 'opus']) { const n = bs.find((b) => b.textContent.trim() === t); if (!n || !inside(n)) bad.push('the current chip \"' + t + '\" was not scrolled into view'); }
    }
    return bad.length ? bad.join('; ') : 'ok'; })()")"
  [ "$got" = ok ] || t_fail="$t_fail layout: $got;"
  # the header `tune` button hides the strip, brings it back, and remembers the choice (ccboard:term:tune 0 / 1)
  got="$(js "(() => { $PRESS_JS const b = document.querySelector('#headtools .tune-toggle'); if (!b || b.getClientRects().length === 0) return 'no visible tune button in #headtools'; const bad = [];
    if (innerWidth === 390 && (b.offsetHeight < 44 || b.offsetWidth < 44)) bad.push('the tune button is ' + b.offsetWidth + 'x' + b.offsetHeight + ', under 44 px'); press(b); return bad.length ? bad.join('; ') : 'ok'; })()")"
  if [ "$got" != ok ]; then
    t_fail="$t_fail toggle: $got;"
  elif ! wait_js "(() => { const s = document.querySelector('#tune'); return s && s.getClientRects().length === 0 && localStorage.getItem('ccboard:term:tune') === '0' ? 1 : 0; })()" 16; then
    t_fail="$t_fail toggle: the tune button did not hide the strip and store ccboard:term:tune=0;"
  else
    js "(() => { $PRESS_JS press(document.querySelector('#headtools .tune-toggle')); return 1; })()" >/dev/null
    wait_js "(() => { const s = document.querySelector('#tune'); return s && s.getClientRects().length > 0 && localStorage.getItem('ccboard:term:tune') === '1' ? 1 : 0; })()" 16 || t_fail="$t_fail toggle: a second tap did not bring the strip back and store ccboard:term:tune=1;"
  fi
  js "(() => { try { localStorage.removeItem('ccboard:term:tune'); } catch (e) { /* ignore */ } return 1; })()" >/dev/null
  # working: every chip disabled with the reason as its title, and the send button says queue
  js "(() => { window.__qaFake.row.state = 'working'; return 1; })()" >/dev/null
  repoll
  if wait_js "(() => { const bs = $TUNE_BTNS; return bs.length > 0 && bs.every((b) => b.disabled) ? 1 : 0; })()" 32; then
    got="$(js "(() => { const bad = []; const bs = $TUNE_BTNS; const notitled = bs.filter((b) => !/prompt/i.test(b.getAttribute('title') || '')); if (notitled.length) bad.push(notitled.length + ' disabled chips without a title that says why (expected \"available when the session is at its prompt\"): ' + notitled.slice(0, 3).map((b) => b.textContent.trim()).join(','));
      const send = document.querySelector('#sendbtn'); if (!send || send.textContent.trim().toLowerCase() !== 'queue') bad.push('the send button reads \"' + (send ? send.textContent.trim() : 'missing') + '\" on a working row, expected queue');
      return bad.length ? bad.join('; ') : 'ok'; })()")"
    [ "$got" = ok ] || t_fail="$t_fail working: $got;"
  else
    t_fail="$t_fail working: the chips were not all disabled within 8 s of a working row;"
  fi
  # idle again, then Usage: /command {cmd:'usage'} and a readout dialog with the captured text
  js "(() => { window.__qaFake.row.state = 'idle'; window.__qaFake.posts.length = 0; return 1; })()" >/dev/null
  repoll
  wait_js "(() => { const bs = $TUNE_BTNS; return bs.length > 0 && bs.every((b) => !b.disabled) ? 1 : 0; })()" 32 || t_fail="$t_fail idle: the chips stayed disabled after the row went back to idle;"
  got="$(press_chip usage)"
  if [ "$got" != ok ]; then
    t_fail="$t_fail usage: the Usage chip is $got;"
  elif ! wait_js "$(fake_cmd_count usage) === 1 ? 1 : 0" 16; then
    t_fail="$t_fail usage: one tap did not POST /command {cmd:'usage'} exactly once (posts: $(js "JSON.stringify(window.__qaFake.posts)" | cut -c1-160));"
  elif ! wait_js "(() => { const d = document.querySelector('dialog.readout[open]'); const p = d && d.querySelector('pre'); return p && p.textContent.indexOf('QA readout for /usage') >= 0 ? 1 : 0; })()" 20; then
    t_fail="$t_fail usage: no open dialog.readout with a <pre> holding the response's screen text;"
  else
    got="$(js "(() => { $PRESS_JS const d = document.querySelector('dialog.readout[open]'); const b = [...d.querySelectorAll('button')].find((x) => /^close\$/i.test(x.textContent.trim()) || /^close\$/i.test(x.getAttribute('aria-label') || '')); if (!b) return 'no Close button'; press(b); return 'ok'; })()")"
    if [ "$got" != ok ]; then t_fail="$t_fail usage: the readout has $got;"
    elif ! wait_js "document.querySelector('dialog.readout[open]') ? 0 : 1" 12; then t_fail="$t_fail usage: Close did not close the readout;"; fi
  fi
  # the next /command (Compact) must be preceded by an Escape through /keys (a read command leaves Claude's dialog open)
  got="$(press_chip compact)"
  if [ "$got" != ok ]; then t_fail="$t_fail compact: the Compact chip is $got;"
  elif wait_js "$(fake_cmd_count compact) >= 1 ? 1 : 0" 16; then
    got="$(js "(() => { const p = window.__qaFake.posts; const iu = p.findIndex((x) => x.path === 'command' && /^\\/?usage\$/.test(String(x.body.cmd))); const ic = p.findIndex((x, i) => i > iu && x.path === 'command' && /^\\/?compact\$/.test(String(x.body.cmd)));
      if (iu < 0 || ic < 0) return 'missing posts (' + p.map((x) => x.path).join(',') + ')'; return p.slice(iu + 1, ic).some((x) => x.path === 'keys' && JSON.stringify(x.body).indexOf('Escape') >= 0) ? 'ok' : 'no Escape through /keys between the /usage readout and the next /command'; })()")"
    [ "$got" = ok ] || t_fail="$t_fail escape: $got;"
  else
    t_fail="$t_fail compact: a tap on Compact did not POST /command {cmd:'compact'};"
  fi
  # a 409 toasts the reason and leaves that chip not pending (a model chip: the Compact chip above may be pending after its success)
  js "(() => { window.__qaFake.next409 = true; const t = document.querySelector('#toasts'); if (t) t.textContent = ''; return 1; })()" >/dev/null
  got="$(press_chip sonnet)"
  if [ "$got" != ok ]; then t_fail="$t_fail 409: the sonnet model chip is $got;"
  elif wait_js "(() => { const t = document.querySelector('#toasts'); return t && /busy/.test(t.textContent) ? 1 : 0; })()" 16; then
    got="$(js "(() => { const t = document.querySelector('#toasts').textContent; const n = [...document.querySelectorAll('#tune button')].find((b) => b.textContent.trim().toLowerCase() === 'sonnet'); const bad = []; if (!/5/.test(t)) bad.push('the toast does not carry the retry seconds (\"' + t.slice(0, 80) + '\")'); if (n && (n.classList.contains('pending') || n.closest('.pending'))) bad.push('the chip is pending after a 409'); return bad.length ? bad.join('; ') : 'ok'; })()")"
    [ "$got" = ok ] || t_fail="$t_fail 409: $got;"
  else
    t_fail="$t_fail 409: no toast with the server's error after a 409 answer;"
  fi
  # Effort ultracode in ONE tap: terminal -> effort is two taps in all (open the page, tap the segment), never a sheet in between.
  # The argument is 'ultracode on', not 'ultracode': term.js EFFORT_ARG.ultracode maps it (claude.py V19: until `--effort ultracode` is proven on
  # the box the switch is `/effort ultracode on`). The mapping and this assertion retire TOGETHER, when claude.py EFFORTS gains ultracode.
  js "(() => { window.__qaFake.posts.length = 0; return 1; })()" >/dev/null
  got="$(press_chip ultracode)"
  if [ "$got" != ok ]; then t_fail="$t_fail effort: the ultracode segment is $got;"
  elif wait_js "$(fake_cmd_count effort 'ultracode on') === 1 ? 1 : 0" 16; then
    [ "$(js "document.querySelector('dialog[open]') ? 1 : 0")" = 0 ] || t_fail="$t_fail effort: a dialog or sheet is open after tapping ultracode (it must be one tap);"
  else
    t_fail="$t_fail effort: one tap on ultracode did not POST /command {cmd:'effort', arg:'ultracode on'} (posts: $(js "JSON.stringify(window.__qaFake.posts)" | cut -c1-160));"
  fi
  # Clear needs two taps (confirmButton) and then sends confirm:true
  js "(() => { window.__qaFake.posts.length = 0; return 1; })()" >/dev/null
  got="$(press_chip clear)"
  if [ "$got" != ok ]; then
    t_fail="$t_fail clear: the Clear chip is $got;"
  else
    sleep 0.5
    if [ "$(js "$(fake_cmd_count clear)")" != 0 ]; then
      t_fail="$t_fail clear: the first tap already POSTed /command {cmd:'clear'} (it needs two taps);"
    else
      got="$(js "(() => { $PRESS_JS const n = [...document.querySelectorAll('#tune button')].find((b) => b.getClientRects().length > 0 && (b.classList.contains('confirm') || /confirm/i.test(b.textContent))) || [...document.querySelectorAll('#tune button')].find((b) => b.textContent.trim().toLowerCase() === 'clear'); if (!n) return 'missing'; press(n); return 'ok'; })()")"
      if [ "$got" != ok ]; then t_fail="$t_fail clear: no second-tap button after the first tap;"
      elif ! wait_js "$(fake_cmd_count clear) === 1 && window.__qaFake.posts.some((x) => x.path === 'command' && /clear/.test(String(x.body.cmd)) && x.body.confirm === true) ? 1 : 0" 16; then
        t_fail="$t_fail clear: the second tap did not POST /command {cmd:'clear', confirm:true} (posts: $(js "JSON.stringify(window.__qaFake.posts)" | cut -c1-160));"
      fi
    fi
  fi
  js "$(qa_unfake_js)" >/dev/null
  if [ -z "$t_fail" ]; then c_tune=PASS; else c_tune=FAIL; fail "$tag: tune:$t_fail"; fi
}

# The real-board version: whatever row the board has. No stub, nothing sent. A shell session (or an ended one) has no strip: skip.
tune_real_checks() {
  local got
  got="$(js "(() => { const s = document.querySelector('#tune'); if (!s || s.classList.contains('hidden') || s.getClientRects().length === 0) return 'none'; const bs = $TUNE_BTNS; const bad = []; const wrap = document.querySelector('#ttywrap').getBoundingClientRect();
    if (bs.length === 0) bad.push('the strip has no visible buttons'); if (wrap.height < innerHeight * 0.4) bad.push('the terminal is only ' + Math.round(wrap.height) + ' px of ' + innerHeight + ' with the strip');
    if (document.documentElement.scrollWidth > innerWidth) bad.push('page scrollWidth > innerWidth');
    if (document.body.scrollWidth > innerWidth) bad.push('body scrollWidth ' + document.body.scrollWidth + ' > ' + innerWidth);
    if (innerWidth >= 840) { const tr = s.getBoundingClientRect(); const out = bs.filter((b) => b.getBoundingClientRect().right > tr.right + 0.5); if (out.length) bad.push(out.length + ' chips poke out of #tune on the right (the segments must wrap inside the side column)'); }
    if (innerWidth === 390) { const small = bs.filter((b) => b.offsetHeight < 44 || b.offsetWidth < 44); if (small.length) bad.push(small.length + ' chips under 44x44: ' + small.slice(0, 5).map((b) => b.textContent.trim() + '=' + b.offsetWidth + 'x' + b.offsetHeight).join(',')); }
    return bad.length ? bad.join('; ') : 'ok'; })()")"
  case "$got" in
    none) c_tune=skip ;;
    ok) c_tune=PASS ;;
    *) c_tune=FAIL; fail "$tag: tune (real row): $got" ;;
  esac
}

# The quick-reply editor: window.prompt must never be called, the pencil opens a dialog, Cancel leaves storage as it was.
quick_checks() {
  local q_fail="" got before
  js "(() => { window.__qaPrompt = 0; const boom = () => { window.__qaPrompt += 1; throw new Error('window.prompt/confirm/alert must not be used'); }; window.prompt = boom; window.confirm = boom; window.alert = boom; return 1; })()" >/dev/null
  before="$(js "(() => { try { return String(localStorage.getItem('ccboard:quick:$NAME')); } catch (e) { return 'unavailable'; } })()")"
  got="$(js "(() => { $PRESS_JS const n = document.querySelector('#qtools button[title=\"Edit quick replies\"], #qtools button[aria-label=\"Edit quick replies\"]'); if (!n || n.getClientRects().length === 0) return 'missing'; press(n); return 'ok'; })()")"
  if [ "$got" != ok ]; then
    q_fail="$q_fail the pencil button (#qtools, title \"Edit quick replies\") is $got;"
  elif wait_js "document.querySelector('dialog.qr-editor[open]') ? 1 : 0" 20; then
    got="$(js "(() => { const d = document.querySelector('dialog.qr-editor[open]'); const ins = [...d.querySelectorAll('input')]; const bad = [];
      if (ins.filter((i) => i.value.trim()).length < 3) bad.push('fewer than 3 inputs holding a reply (' + ins.length + ' inputs: ' + ins.map((i) => i.value).join('|').slice(0, 60) + ')');
      if (innerWidth === 390) { const small = ins.filter((i) => parseFloat(getComputedStyle(i).fontSize) < 16); if (small.length) bad.push(small.length + ' inputs under 16 px (iOS zooms on focus)'); }
      if (!d.querySelector('button')) bad.push('no buttons (Save, Cancel)'); return bad.length ? bad.join('; ') : 'ok'; })()")"
    [ "$got" = ok ] || q_fail="$q_fail editor: $got;"
    got="$(js "(() => { $PRESS_JS const d = document.querySelector('dialog.qr-editor[open]'); const b = [...d.querySelectorAll('button')].find((x) => /^cancel\$/i.test(x.textContent.trim())); if (!b) return 'no Cancel button'; press(b); return 'ok'; })()")"
    if [ "$got" != ok ]; then q_fail="$q_fail editor: $got;"
    elif ! wait_js "document.querySelector('dialog.qr-editor[open]') ? 0 : 1" 12; then q_fail="$q_fail editor: Cancel did not close the dialog;"
    else
      got="$(js "(() => { try { return String(localStorage.getItem('ccboard:quick:$NAME')); } catch (e) { return 'unavailable'; } })()")"
      [ "$got" = "$before" ] || q_fail="$q_fail editor: Cancel changed ccboard:quick:$NAME from '$before' to '$got';"
    fi
  else
    q_fail="$q_fail the pencil did not open a dialog.qr-editor;"
  fi
  got="$(js "String(window.__qaPrompt)")"
  [ "$got" = 0 ] || q_fail="$q_fail window.prompt/confirm/alert was called $got time(s);"
  js "(() => { document.querySelectorAll('dialog[open]').forEach((d) => { try { d.close(); } catch (e) { /* ignore */ } }); return 1; })()" >/dev/null
  if [ -z "$q_fail" ]; then c_quick=PASS; else c_quick=FAIL; fail "$tag: quick:$q_fail"; fi
}

# The soft keyboard (390 px, fake board). A headless window has none, so the fake tty shrinks the page's visualViewport to KBD_VV px and fires
# `resize` on it when localStorage ccboard:fake:vv is set (scripts/dev/fake_tty/fake_tty.js; ?vv=<height> on the page URL does the same): what a
# phone does when the keyboard opens. Order: load with the shim, the faked row on screen (strip and context strip visible), THEN force-coarse and
# the resize that makes term.js read the viewport again. Expect body.kbd, the composer inside the visual viewport, the strip and the context strip
# out of the way, and the quick-reply editor (a dialog) fully inside the visual viewport with no input focused.
KBD_VV=520
kbd_cleanup() {
  js "(() => { try { localStorage.removeItem('ccboard:fake:vv'); } catch (e) { /* ignore */ } if (window.__qaFake) { window.fetch = window.__qaFake.orig; delete window.__qaFake; } document.querySelectorAll('dialog[open]').forEach((d) => { try { d.close(); } catch (e) { /* ignore */ } }); return 1; })()" >/dev/null
}
kbd_checks() {
  local k_fail="" got
  js "(() => { try { localStorage.setItem('ccboard:fake:vv', '$KBD_VV'); localStorage.removeItem('ccboard:term:tune'); } catch (e) { return 0; } return 1; })()" >/dev/null
  if ! load_term "$w" "$h" "$BASE/term/$NAME?qa=kbd"; then
    kbd_cleanup; c_kbd=FAIL; fail "$tag: kbd: the page did not settle with ccboard:fake:vv=$KBD_VV"; return
  fi
  if [ "$(js "$(qa_fake_js | sed "s/__NAME__/$NAME/g")")" != ok ]; then kbd_cleanup; c_kbd=FAIL; fail "$tag: kbd: could not install the fetch stub in the page"; return; fi
  js "(() => { window.__qaFake.row.state = 'idle'; return 1; })()" >/dev/null
  repoll
  # before the keyboard: the strip and the context strip are on screen (so "gone" below means something)
  if ! wait_js "(() => { const s = document.querySelector('#tune'); const c = document.querySelector('#ctxstrip'); return s && c && s.getClientRects().length > 0 && c.getClientRects().length > 0 ? 1 : 0; })()" 32; then
    kbd_cleanup; c_kbd=FAIL; fail "$tag: kbd: #tune and #ctxstrip were not both on screen before the keyboard came up (the faked row has a task, a prompt and a message)"; return
  fi
  js "(() => { document.documentElement.classList.add('force-coarse'); const v = window.visualViewport; if (v) v.dispatchEvent(new Event('resize')); window.dispatchEvent(new Event('resize')); return 1; })()" >/dev/null
  if ! wait_js "document.body.classList.contains('kbd') ? 1 : 0" 24; then
    got="$(js "(() => 'visualViewport.height ' + (window.visualViewport ? window.visualViewport.height : 'none') + ', innerHeight ' + innerHeight + ', iframe __vv ' + (() => { try { return JSON.stringify(document.querySelector('#ttywrap iframe').contentWindow.__vv || null); } catch (e) { return 'unreadable'; } })())()")"
    kbd_cleanup; c_kbd=FAIL; fail "$tag: kbd: body.kbd was not set with the visual viewport at $KBD_VV px ($got): did the fake tty install ccboard:fake:vv?"; return
  fi
  got="$(js "(() => { const bad = []; const vv = $KBD_VV;
    const root = getComputedStyle(document.documentElement).getPropertyValue('--vvh').trim(); if (root !== vv + 'px') bad.push('--vvh is ' + (root || 'unset') + ', expected ' + vv + 'px');
    const f = document.querySelector('#sendform').getBoundingClientRect(); if (f.bottom > vv + 1) bad.push('#sendform ends at ' + Math.round(f.bottom) + ', below the visual viewport (' + vv + ')'); if (f.top < 0) bad.push('#sendform starts above the window (' + Math.round(f.top) + ')');
    for (const id of ['#tune', '#ctxstrip', '#quickrow']) { const n = document.querySelector(id); if (n && n.getClientRects().length > 0) bad.push(id + ' is still on screen with the keyboard up'); }
    if (document.querySelector('#tune').classList.contains('hidden') || document.querySelector('#ctxstrip').classList.contains('hidden')) bad.push('the strips were hidden by the page, not by the keyboard rules (the check proves nothing)');
    return bad.length ? bad.join('; ') : 'ok'; })()")"
  [ "$got" = ok ] || k_fail="$k_fail layout: $got;"
  # the quick-reply editor must stay inside the visual viewport, Save and Cancel included, and must not focus an input on open
  # (the pencil sits in #quickrow, which steps aside under the keyboard on purpose: the layout check above asserts that. The dialog can still be open when
  # the keyboard comes up, e.g. a tap in one of its rows, so it is opened the way the pencil opens it: components.js quickReplyEditor over the stored list)
  got="$(js "(() => { try { quickReplyEditor({ items: quickLoad('$NAME'), defaults: QUICK_DEFAULTS, onSave() {} }); return 'ok'; } catch (e) { return 'error: ' + e.message; } })()")"
  if [ "$got" != ok ]; then
    k_fail="$k_fail editor: quickReplyEditor() did not open with the keyboard up ($got);"
  elif wait_js "document.querySelector('dialog.qr-editor[open]') ? 1 : 0" 20; then
    sleep "$SETTLE"
    "$B" screenshot "$OUT/term-$tag-kbd-editor.png" >/dev/null 2>&1
    got="$(js "(() => { const d = document.querySelector('dialog.qr-editor[open]'); const vv = $KBD_VV; const bad = []; const r = (n) => n.getBoundingClientRect(); const dr = r(d);
      if (dr.top < 0 || dr.bottom > vv + 1) bad.push('the dialog spans y ' + Math.round(dr.top) + ' to ' + Math.round(dr.bottom) + ', outside the visual viewport (0 to ' + vv + ')');
      for (const sel of ['.qr-save', '.qr-cancel']) { const n = d.querySelector(sel); if (!n) { bad.push('no ' + sel + ' button'); continue; } const b = r(n); if (b.top < 0 || b.bottom > vv + 1) bad.push(sel + ' is at y ' + Math.round(b.top) + ' to ' + Math.round(b.bottom) + ', outside the visual viewport (0 to ' + vv + ')'); }
      const a = document.activeElement; if (a && /^(INPUT|TEXTAREA)\$/.test(a.tagName)) bad.push('an input is focused when the dialog opens (' + String(a.className).slice(0, 30) + '): the soft keyboard would pop up before anything was tapped');
      return bad.length ? bad.join('; ') : 'ok'; })()")"
    [ "$got" = ok ] || k_fail="$k_fail editor: $got;"
    js "(() => { $PRESS_JS const d = document.querySelector('dialog.qr-editor[open]'); const b = d && d.querySelector('.qr-cancel'); if (b) press(b); return 1; })()" >/dev/null
  else
    k_fail="$k_fail editor: quickReplyEditor() did not open a dialog.qr-editor with the keyboard up;"
  fi
  kbd_cleanup
  if [ -z "$k_fail" ]; then c_kbd=PASS; else c_kbd=FAIL; fail "$tag: kbd:$k_fail"; fi
}

# Without the flag the font spike did nothing (the fake tty also counts FontFace objects; a real ttyd has no counter).
font_default_checks() {
  local got
  got="$(js "(() => { try { const w = document.querySelector('#ttywrap iframe').contentWindow; const fam = String((w.term.options || {}).fontFamily || ''); const bad = [];
    if (fam.indexOf('JetBrains') >= 0) bad.push('term.options.fontFamily names JetBrains Mono without ccboard:term:font=1: ' + fam.slice(0, 60));
    if (w.__font && w.__font.made > 0) bad.push(w.__font.made + ' FontFace object(s) were built without the flag');
    if (typeof TermKit !== 'undefined' && TermKit.fontState !== 'off') bad.push('TermKit.fontState is ' + TermKit.fontState + ', expected off');
    return bad.length ? bad.join('; ') : 'ok'; } catch (e) { return 'iframe not readable: ' + e.message; } })()")"
  if [ "$got" = ok ]; then c_font=PASS; else c_font=FAIL; fail "$tag: font: $got"; fi
}

# ---- the font spike (after the table): FW x FH is the viewport it runs at (the first one run, or QA_FONT_W)
FW=""; FH=""
FONT_LINES=""
FONT_STATE_JS="(() => { try { const w = document.querySelector('#ttywrap iframe').contentWindow; let s = typeof TermKit !== 'undefined' ? TermKit.fontState : ''; if (!s) s = String(w.term.options.fontFamily).indexOf('JetBrains Mono') >= 0 ? 'on' : 'unknown'; return s; } catch (e) { return 'unreadable'; } })()"
# every size the on-box check reads: fit twice at each, cols x rows must not move between the two (xterm resizes synchronously)
FONT_SIZES_JS="(() => { try { const t = document.querySelector('#ttywrap iframe').contentWindow.term; const out = []; let stable = true;
  for (const s of [13, 11]) { t.options.fontSize = s; t.fit(); const a = t.cols + 'x' + t.rows; t.fit(); const b = t.cols + 'x' + t.rows; if (a !== b) stable = false; out.push(s + 'px ' + b); }
  return (stable ? 'stable ' : 'UNSTABLE ') + out.join(' '); } catch (e) { return 'error: ' + e.message; } })()"

# font_load FLAG FAKEMODE: set (or clear, FLAG=0) ccboard:term:font and ccboard:fake:font in the board's storage, then load the page afresh
font_load() {
  js "(() => { try { if ('$1' === '1') localStorage.setItem('ccboard:term:font', '1'); else localStorage.removeItem('ccboard:term:font'); if ('$2' === '') localStorage.removeItem('ccboard:fake:font'); else localStorage.setItem('ccboard:fake:font', '$2'); } catch (e) { return 0; } return 1; })()" >/dev/null
  load_term "$FW" "$FH" "$BASE/term/$NAME?qa=font$1$2"
}

# font_wait WANT [tries]: poll the spike's state until it is WANT (on | failed | off)
font_wait() {
  local i
  for i in $(seq 1 "${2:-24}"); do [ "$(js "$FONT_STATE_JS")" = "$1" ] && return 0; sleep "$TICK"; done
  return 1
}

font_clear() { js "(() => { try { localStorage.removeItem('ccboard:term:font'); localStorage.removeItem('ccboard:fake:font'); } catch (e) { /* ignore */ } return 1; })()" >/dev/null; }

# The spike on the fake board: stand-in on, rejected, no FontFace at all, then the browser's own FontFace and the vendored woff2.
font_spike_fake() {
  local tag="$FW(font spike)" got base mode label
  font_load 0 "" || { fail "$tag: the page did not settle for the baseline load"; return; }
  base="$(js "$FONT_SIZES_JS")"
  FONT_LINES="${FONT_LINES}  baseline (flag off): $base"$'\n'
  for mode in stub fail missing ''; do
    label="${mode:-native}"
    if ! font_load 1 "$mode"; then fail "$tag: $label: the page did not settle with the spike on (a font step must never stop the terminal from binding)"; continue; fi
    case "$mode" in
      stub|'')
        if ! font_wait on 48; then fail "$tag: $label: TermKit.fontState is '$(js "$FONT_STATE_JS")' after 12 s, expected on"; FONT_LINES="${FONT_LINES}  $label: FAIL"$'\n'; continue; fi
        got="$(js "(() => { try { const w = document.querySelector('#ttywrap iframe').contentWindow; const t = w.term; const f = w.__font || {}; const bad = []; const fam = String(t.options.fontFamily);
          if (fam.indexOf(\"'JetBrains Mono'\") !== 0) bad.push('fontFamily is ' + fam.slice(0, 50));
          if (f.added !== 1) bad.push('document.fonts.add ran ' + f.added + ' times, expected 1'); if (f.made !== 1) bad.push(f.made + ' FontFace objects, expected 1');
          if (!(w.__fits > w.__fitsAtFamily)) bad.push('no fit() after the family changed (fits ' + w.__fits + ', at family ' + w.__fitsAtFamily + ')');
          if ('$mode' === '' && !w.document.fonts.check(\"13px 'JetBrains Mono'\")) bad.push('document.fonts.check says JetBrains Mono is not loaded (the woff2 did not come from the board?)');
          if ('$mode' === '' && f.loaded !== 1) bad.push('the native FontFace loaded ' + f.loaded + ' times, expected 1');
          return bad.length ? bad.join('; ') : 'ok'; } catch (e) { return 'error: ' + e.message; } })()")"
        if [ "$got" != ok ]; then fail "$tag: $label: $got"; FONT_LINES="${FONT_LINES}  $label: FAIL"$'\n'; continue; fi
        got="$(js "$FONT_SIZES_JS")"
        case "$got" in stable*) FONT_LINES="${FONT_LINES}  $label: on, $got"$'\n' ;; *) fail "$tag: $label: cols x rows are not stable over repeated fits: $got"; FONT_LINES="${FONT_LINES}  $label: FAIL ($got)"$'\n' ;; esac
        ;;
      fail|missing)
        if ! font_wait failed 48; then fail "$tag: $label: TermKit.fontState is '$(js "$FONT_STATE_JS")' after 12 s, expected failed"; FONT_LINES="${FONT_LINES}  $label: FAIL"$'\n'; continue; fi
        got="$(js "(() => { try { const w = document.querySelector('#ttywrap iframe').contentWindow; const bad = []; if (String(w.term.options.fontFamily).indexOf('JetBrains') >= 0) bad.push('the family changed after a failed load: ' + w.term.options.fontFamily);
          if ('$mode' === 'missing' && (typeof w.FontFace !== 'undefined' || w.document.fonts)) bad.push('the fake did not remove FontFace / document.fonts');
          const x = w.document.querySelector('.xterm'); if (!x || x.style.touchAction !== 'none') bad.push('the rest of bind() did not run (.xterm touch-action ' + (x ? x.style.touchAction || 'unset' : 'missing') + ')');
          return bad.length ? bad.join('; ') : 'ok'; } catch (e) { return 'error: ' + e.message; } })()")"
        if [ "$got" = ok ]; then FONT_LINES="${FONT_LINES}  $label: failed as expected, terminal untouched"$'\n'; else fail "$tag: $label: $got"; FONT_LINES="${FONT_LINES}  $label: FAIL"$'\n'; fi
        ;;
    esac
    errs="$("$B" console --errors 2>&1 | grep -E '\[error\]' | grep -vE "/api/sessions/$NAME|status of (404|503)" | head -n 2)"
    [ -z "$errs" ] || fail "$tag: $label: console errors: $(printf '%s' "$errs" | tr '\n' ' ' | cut -c1-200)"
  done
  font_clear
}

# The spike against a real ttyd (QA_FONT=1): the numbers the verdict needs. Cursor and box-drawing alignment, iOS Safari PWA and Android
# Chrome stay by-eye checks on the device (README, "Font spike").
font_spike_real() {
  local tag="$FW(font spike, real ttyd)" got base
  font_load 0 "" || { fail "$tag: the page did not settle for the baseline load"; return; }
  base="$(js "$FONT_SIZES_JS")"
  FONT_LINES="${FONT_LINES}  real ttyd, flag off: $base"$'\n'
  if ! font_load 1 ""; then fail "$tag: the page did not settle with ccboard:term:font=1"; font_clear; return; fi
  if ! font_wait on 80; then
    fail "$tag: TermKit.fontState is '$(js "$FONT_STATE_JS")' after 20 s, expected on (is /static/vendor/fonts/jetbrains-mono-latin-wght-normal.woff2 reachable from the iframe, and is ttyd's page same-origin?)"
    font_clear; return
  fi
  got="$(js "(() => { try { const w = document.querySelector('#ttywrap iframe').contentWindow; const bad = []; if (String(w.term.options.fontFamily).indexOf(\"'JetBrains Mono'\") !== 0) bad.push('fontFamily is ' + w.term.options.fontFamily);
    if (!w.document.fonts.check(\"13px 'JetBrains Mono'\")) bad.push('document.fonts.check says JetBrains Mono is not loaded'); return bad.length ? bad.join('; ') : 'ok'; } catch (e) { return 'error: ' + e.message; } })()")"
  [ "$got" = ok ] || fail "$tag: $got"
  got="$(js "$FONT_SIZES_JS")"
  case "$got" in stable*) FONT_LINES="${FONT_LINES}  real ttyd, flag on:  $got"$'\n' ;; *) fail "$tag: cols x rows are not stable over repeated fits with the font on: $got"; FONT_LINES="${FONT_LINES}  real ttyd, flag on:  FAIL ($got)"$'\n' ;; esac
  font_clear
}

if [ "$REAL" = 1 ]; then MODE=real-ttyd; else MODE=fake-tty; fi
echo "ccboard qa_terminal: $BASE  session=$NAME ($SESSION, pane: $PANE_ALT)  mode=$MODE  out=$OUT"
[ "$SESSION" = absent ] && echo "note: no such session on the board's tmux: the page shows 'ended', the History check is skipped and /api/sessions 404/503 console lines are ignored (see QA_TMUX_CREATE in the script header)"
echo
row VIEWPORT WRAP BOUNCE OVERFLOW KEYS TARGETS COMPOSER LAYOUT TOUCH SCROLL HISTORY COMPACT TUNE QUICK FONT KBD CONSOLE SHOT
row ---------- ----- ------ -------- ----- -------- --------- ------- ------ ------- -------- -------- ---- ----- ---- --- -------- ----

# Start the browse server before row one and wait until it answers three times in a row: a cold start can take longer than the CLI waits
# ("Server failed to start within 8s"), after which it starts a second one that takes over a moment later and drops the page mid-run.
ok=0
for _ in $(seq 1 60); do
  "$B" goto about:blank >/dev/null 2>&1
  if [ "$(js "1 + 1")" = 2 ]; then ok=$((ok + 1)); [ "$ok" -ge 3 ] && break; else ok=0; fi
  sleep "${QA_START_WAIT:-1}"
done
if [ "$ok" -lt 3 ]; then echo "qa_terminal: the gstack browser did not start (browse CLI: $B)" >&2; exit 2; fi

if [ -n "${QA_HEADER:-}" ]; then "$B" header "$QA_HEADER" >/dev/null 2>&1; fi

idx=0
for vp in $VIEWPORTS; do
  IFS=: read -r size kind <<< "$vp"
  w="${size%%x*}"; h="${size##*x}"
  case " ${QA_WIDTHS:-$w} " in *" $w "*) ;; *) continue ;; esac
  idx=$((idx + 1))
  tag="$w"
  if [ -z "$FW" ] && { [ -z "${QA_FONT_W:-}" ] || [ "${QA_FONT_W:-}" = "$w" ]; }; then FW="$w"; FH="$h"; fi
  shot="$OUT/term-$tag.png"
  url="$BASE/term/$NAME?qa=$idx"                     # a new query string per load

  if ! load_term "$w" "$h" "$url"; then
    fail "$tag: the page did not settle (no #ttywrap iframe with a bound fake tty within 10 s, or the window is not ${w} wide)"
    row "$w" FAIL - - - - - - - - - - - - - - - no
    continue
  fi
  [ "$w" = 390 ] && js "(() => { document.documentElement.classList.add('force-coarse'); return 1; })()" >/dev/null

  # --- wrap: the terminal box is a clipped box, the page never scrolls, the iframe fills it
  got="$(js "(() => { const w = document.querySelector('#ttywrap'); if (!w) return 'no #ttywrap'; const bad = []; const r = document.documentElement;
    if (w.scrollHeight > w.clientHeight) bad.push('ttywrap scrollHeight ' + w.scrollHeight + ' > clientHeight ' + w.clientHeight);
    if (w.scrollWidth > w.clientWidth) bad.push('ttywrap scrollWidth ' + w.scrollWidth + ' > clientWidth ' + w.clientWidth);
    if (r.scrollHeight > innerHeight + 1) bad.push('page scrollHeight ' + r.scrollHeight + ' > ' + innerHeight);
    if (document.body.scrollHeight > innerHeight + 1) bad.push('body scrollHeight ' + document.body.scrollHeight + ' > ' + innerHeight);
    const f = w.querySelector('iframe'); const a = w.getBoundingClientRect(); const b = f ? f.getBoundingClientRect() : null;
    if (!b) bad.push('no iframe in #ttywrap'); else if (Math.abs(a.width - b.width) > 1 || Math.abs(a.height - b.height) > 1) bad.push('iframe ' + Math.round(b.width) + 'x' + Math.round(b.height) + ' does not fill #ttywrap ' + Math.round(a.width) + 'x' + Math.round(a.height));
    if (a.height < innerHeight * 0.4) bad.push('terminal is only ' + Math.round(a.height) + ' px of ' + innerHeight);
    return bad.length ? bad.join('; ') : 'ok'; })()")"
  if [ "$got" = ok ]; then c_wrap=PASS; else c_wrap=FAIL; fail "$tag: wrap: $got"; fi

  # --- bounce: no pull-to-refresh, no rubber band, the iframe document hardened too
  got="$(js "(() => { const bad = []; const cs = (n) => getComputedStyle(n);
    for (const [label, n] of [['html', document.documentElement], ['body', document.body]]) for (const ax of ['x', 'y']) { const v = cs(n).getPropertyValue('overscroll-behavior-' + ax); if (v !== 'none') bad.push(label + ' overscroll-behavior-' + ax + ' is ' + (v || 'unset')); }
    if (cs(document.body).position !== 'fixed') bad.push('body position is ' + cs(document.body).position + ', expected fixed');
    try { const d = document.querySelector('#ttywrap iframe').contentDocument; if (d.documentElement.style.overscrollBehavior !== 'none') bad.push('fake tty root overscroll-behavior is ' + (d.documentElement.style.overscrollBehavior || 'unset')); const x = d.querySelector('.xterm'); if (!x || x.style.touchAction !== 'none') bad.push('.xterm touch-action is ' + (x ? x.style.touchAction || 'unset' : 'missing')); } catch (e) { bad.push('iframe document not readable: ' + e.message); }
    return bad.length ? bad.join('; ') : 'ok'; })()")"
  if [ "$got" = ok ]; then c_bounce=PASS; else c_bounce=FAIL; fail "$tag: bounce: $got"; fi

  # --- no horizontal overflow
  # body too: body.term is position:fixed with overflow hidden, so documentElement.scrollWidth stays at innerWidth however far a child pokes out
  got="$(js "(() => { const d = document.documentElement; const b = document.body; if (d.scrollWidth > window.innerWidth) return 'documentElement ' + d.scrollWidth + '>' + window.innerWidth; if (b.scrollWidth > window.innerWidth) return 'body ' + b.scrollWidth + '>' + window.innerWidth; return 'ok'; })()")"
  if [ "$got" = ok ]; then c_over=PASS; else c_over=FAIL; fail "$tag: horizontal overflow, scrollWidth>innerWidth is $got"; fi

  # --- keys: 44 px key-bar keys and scroll-rail buttons at every width (a mouse in the side column: keys 32 px tall, still 44 px wide)
  got="$(js "(() => { const vis = (e) => e.getClientRects().length > 0; const all = [...document.querySelectorAll('#keyhost .kb-key, #rail .rail-btn')].filter(vis);
    if (all.length < 15) return 'only ' + all.length + ' visible keys';
    const dense = innerWidth >= 840 && !document.documentElement.classList.contains('force-coarse') && matchMedia('(pointer:fine)').matches;
    const bad = all.filter((e) => e.offsetWidth < 44 || e.offsetHeight < (dense && e.classList.contains('kb-key') ? 32 : 44));
    return bad.length ? bad.length + ' under 44 px: ' + bad.slice(0, 4).map((e) => (e.getAttribute('data-key') || e.getAttribute('aria-label') || e.className) + '=' + e.offsetWidth + 'x' + e.offsetHeight).join(',') : 'ok'; })()")"
  if [ "$got" = ok ]; then c_keys=PASS; else c_keys=FAIL; fail "$tag: keys: $got"; fi

  # --- targets (phone, coarse pointer forced above): every visible button
  if [ "$w" = 390 ]; then
    got="$(js "(() => { const bad = [...document.querySelectorAll('button, a.bp5-button')].filter((e) => e.getClientRects().length > 0 && e.offsetHeight < 44); return bad.length ? bad.length + ' under 44 px: ' + bad.slice(0, 5).map((e) => (e.id ? '#' + e.id : String(e.className || e.tagName).split(' ').slice(0, 2).join('.')) + '=' + e.offsetHeight).join(',') : 'ok'; })()")"
    if [ "$got" = ok ]; then c_tgt=PASS; else c_tgt=FAIL; fail "$tag: targets: $got"; fi
  else
    c_tgt=-
  fi

  # --- composer: visible and inside the window; 16 px on the phone so iOS does not zoom
  got="$(js "(() => { const t = document.querySelector('#sendtext'); if (!t) return 'no #sendtext'; const r = t.getBoundingClientRect(); const cs = getComputedStyle(t); const bad = [];
    if (cs.display === 'none' || cs.visibility === 'hidden' || r.width < 40 || r.height < 20) bad.push('not visible (' + Math.round(r.width) + 'x' + Math.round(r.height) + ')');
    if (r.top < 0 || r.left < 0 || r.bottom > innerHeight + 1 || r.right > innerWidth + 1) bad.push('outside the window: top ' + Math.round(r.top) + ' bottom ' + Math.round(r.bottom) + ' of ' + innerHeight + ', right ' + Math.round(r.right) + ' of ' + innerWidth);
    if (innerWidth === 390 && parseFloat(cs.fontSize) < 16) bad.push('font-size ' + cs.fontSize + ' < 16px (iOS zooms on focus)');
    const s = document.querySelector('#sendbtn'); if (!s || s.getClientRects().length === 0) bad.push('no visible Send button');
    return bad.length ? bad.join('; ') : 'ok'; })()")"
  if [ "$got" = ok ]; then c_comp=PASS; else c_comp=FAIL; fail "$tag: composer: $got"; fi

  # --- layout: one column under the terminal below 840 px, a side column of 320 to 360 px from 840 px
  got="$(js "(() => { const g = (s) => document.querySelector(s).getBoundingClientRect(); const wrap = g('#ttywrap'); const send = g('#sendform'); const keys = g('#keyhost'); const bad = [];
    if (innerWidth >= 840) { if (send.left < wrap.right - 1) bad.push('composer at x ' + Math.round(send.left) + ' is not right of the terminal (ends ' + Math.round(wrap.right) + ')'); if (keys.left < wrap.right - 1) bad.push('key bar is not in the side column'); if (send.width < 318 || send.width > 362) bad.push('side column is ' + Math.round(send.width) + ' px, expected 320 to 360'); }
    else { if (send.top < wrap.bottom - 1) bad.push('composer (top ' + Math.round(send.top) + ') is not under the terminal (bottom ' + Math.round(wrap.bottom) + ')'); if (keys.top < wrap.bottom - 1) bad.push('key bar is not under the terminal'); if (send.width < innerWidth - 2) bad.push('composer is ' + Math.round(send.width) + ' px wide in a ' + innerWidth + ' px window'); }
    return bad.length ? bad.join('; ') : 'ok'; })()")"
  if [ "$got" = ok ]; then c_layout=PASS; else c_layout=FAIL; fail "$tag: layout: $got"; fi

  # --- touch: a synthetic swipe on the fake screen becomes wheel events; a swipe from the left edge does not
  swipe_js() {   # start x as $1: dispatches touchstart, touchmoves and touchend (falling back to pointer events) and returns before>after|delta
    cat <<EOF
(() => { const f = document.querySelector('#ttywrap iframe'); let w, d; try { w = f.contentWindow; d = w.document; } catch (e) { return 'iframe not readable'; }
  const s = d.querySelector('.xterm-screen'); if (!s) return 'no .xterm-screen'; const r = s.getBoundingClientRect();
  const x = $1 < 0 ? Math.round(r.left + r.width / 2) : $1, y0 = Math.round(r.top + r.height * 0.8), y1 = Math.round(r.top + r.height * 0.2);
  const before = w.__wheel || 0; let how = 'touch';
  try {
    const mk = (type, y) => { const t = new w.Touch({ identifier: 7, target: s, clientX: x, clientY: y, pageX: x, pageY: y, screenX: x, screenY: y }); const list = type === 'touchend' ? [] : [t];
      return new w.TouchEvent(type, { bubbles: true, cancelable: true, touches: list, targetTouches: list, changedTouches: [t] }); };
    s.dispatchEvent(mk('touchstart', y0)); for (let y = y0; y > y1; y -= 12) s.dispatchEvent(mk('touchmove', y)); s.dispatchEvent(mk('touchend', y1));
  } catch (e) { how = 'touch-unsupported'; }
  if ((w.__wheel || 0) === before && how === 'touch-unsupported') { how = 'pointer';
    const pe = (type, y) => new w.PointerEvent(type, { bubbles: true, cancelable: true, pointerType: 'touch', pointerId: 7, isPrimary: true, clientX: x, clientY: y });
    s.dispatchEvent(pe('pointerdown', y0)); for (let y = y0; y > y1; y -= 12) s.dispatchEvent(pe('pointermove', y)); s.dispatchEvent(pe('pointerup', y1)); }
  return how + ':' + before + ':' + (w.__wheel || 0) + ':' + (w.__lastDelta || 0); })()
EOF
  }
  if [ "$REAL" = 1 ]; then
    c_touch=skip                                      # a real ttyd has no wheel counter (window.__wheel is the fake's)
  else
    res="$(js "$(swipe_js -1)")"
    IFS=: read -r how before after last <<< "$res"
    if [ "${how:-}" = touch ] && [ "${after:-0}" -ge $(( ${before:-0} + 3 )) ] 2>/dev/null && [ "${last:-0}" != 0 ]; then
      edge="$(js "$(swipe_js 10)")"
      IFS=: read -r _ ebefore eafter _ <<< "$edge"
      if [ "${eafter:-x}" = "${ebefore:-y}" ]; then c_touch=PASS; else c_touch=FAIL; fail "$tag: touch: a swipe starting 10 px from the left edge made wheel events (${ebefore:-?} -> ${eafter:-?}): the iOS back gesture must be left alone"; fi
    else
      c_touch=FAIL; fail "$tag: touch: a synthetic swipe on .xterm-screen gave '$res' (expected touch:<n>:<n+3 or more>:<non-zero deltaY>; wheel events must reach window.__wheel)"
    fi
  fi

  # --- scroll: the rail's PgUp POSTs /scroll {dir:'up'} and sends no raw keys (the key bar has no scroll keys since v0.5.6d: the rail owns scrolling)
  js "(() => { window.__qaCalls = []; if (!window.__qaWrapped) { const f = window.fetch.bind(window); window.fetch = (u, o) => { try { window.__qaCalls.push({ url: String((u && u.url) || u), method: (o && o.method) || 'GET', body: (o && o.body) || '' }); } catch (e) { /* never break the page */ } return f(u, o); }; window.__qaWrapped = 1; } return 1; })()" >/dev/null
  scroll_calls() { js "(() => JSON.stringify(window.__qaCalls.filter((c) => c.method === 'POST')))()"; }
  press_js() {   # $1 selector
    js "(() => { $PRESS_JS const n = document.querySelector('$1'); if (!n || n.getClientRects().length === 0) return 'missing'; press(n); return 'ok'; })()"
  }
  s_fail=""
  for target in "#rail .rail-btn:first-child|rail"; do
    sel="${target%%|*}"; label="${target##*|}"
    js "(() => { window.__qaCalls.length = 0; return 1; })()" >/dev/null
    pressed="$(press_js "$sel")"
    if [ "$pressed" != ok ]; then s_fail="$s_fail $label PgUp button not found/visible ($sel);"; continue; fi
    sleep 0.5
    calls="$(scroll_calls)"
    if ! printf '%s' "$calls" | python3 -c '
import json, sys
name = sys.argv[1]
calls = json.load(sys.stdin)
scroll = [c for c in calls if c["url"].split("?")[0].endswith("/api/sessions/" + name + "/scroll")]
raw = [c for c in calls if c["url"].split("?")[0].endswith("/keys")]
ok = scroll and all(json.loads(c["body"] or "{}").get("dir") == "up" for c in scroll) and not raw
sys.exit(0 if ok else 1)' "$NAME" 2>/dev/null; then
      s_fail="$s_fail $label PgUp made these POSTs: $(printf '%s' "$calls" | cut -c1-200);"
    fi
    if [ "$SESSION" = present ] && [ "$PANE_ALT" = shell ]; then      # leave copy-mode so the next press and the next viewport start live
      sleep 0.3; js "(() => { $PRESS_JS const c = document.querySelector('#histchip'); if (c && c.getClientRects().length) press(c); return 1; })()" >/dev/null
    fi
  done
  if [ -n "${QA_SERVER_LOG:-}" ] && [ -z "$s_fail" ]; then
    sleep 0.3
    if ! grep -aq "POST /api/sessions/$NAME/scroll" "$QA_SERVER_LOG" 2>/dev/null; then s_fail="$s_fail no 'POST /api/sessions/$NAME/scroll' line in $QA_SERVER_LOG;"; fi
  fi
  if [ -z "$s_fail" ]; then c_scroll=PASS; else c_scroll=FAIL; fail "$tag: scroll:$s_fail"; fi

  # --- history: the chip follows tmux copy-mode (needs a shell pane on the board's tmux)
  if [ "$SESSION" = present ] && [ "$PANE_ALT" = shell ]; then
    press_js "#rail .rail-btn:first-child" >/dev/null
    shown=no
    for _ in $(seq 1 24); do                                          # the page polls /pane every 3 s, and sooner right after a scroll
      [ "$(js "(() => { const c = document.querySelector('#histchip'); return c && c.getClientRects().length > 0 && !c.classList.contains('hidden') ? 1 : 0; })()")" = 1 ] && { shown=yes; break; }
      sleep 0.25
    done
    if [ "$shown" = yes ]; then
      js "(() => { $PRESS_JS press(document.querySelector('#histchip')); return 1; })()" >/dev/null
      gone=no
      for _ in $(seq 1 24); do
        [ "$(js "(() => { const c = document.querySelector('#histchip'); return c && c.getClientRects().length > 0 ? 1 : 0; })()")" = 0 ] && { gone=yes; break; }
        sleep 0.25
      done
      if [ "$gone" = yes ]; then c_hist=PASS; else c_hist=FAIL; fail "$tag: history: the History chip stayed after tapping it (copy-mode was not left)"; fi
    else
      c_hist=FAIL; fail "$tag: history: no History chip within 6 s of PgUp on a shell pane (POST /scroll -> tmux copy-mode -> GET /pane in_mode)"
    fi
  else
    c_hist=skip
  fi

  # --- compact: the soft-keyboard key bar (forced through the persisted override, a headless window has no soft keyboard)
  if [ "$w" = 390 ]; then
    VISKEYS="[...document.querySelectorAll('#keyhost .kb-key')].filter((e) => e.getClientRects().length > 0)"
    got="$(js "(() => { try { localStorage.setItem('ccboard:term:keys', 'compact'); } catch (e) { return 'localStorage unavailable'; } window.dispatchEvent(new Event('resize'));
      const v = $VISKEYS; return v.filter((e) => !e.classList.contains('kb-more')).map((e) => e.getAttribute('data-key')).join(',') + '|' + v.some((e) => e.classList.contains('kb-more')) + '|' + v.length; })()")"
    js "(() => { $PRESS_JS const m = document.querySelector('#keyhost .kb-more'); if (m && m.getClientRects().length) press(m); return 1; })()" >/dev/null
    opened="$(js "(() => $VISKEYS.length)()")"
    js "(() => { try { localStorage.removeItem('ccboard:term:keys'); } catch (e) { /* ignore */ } window.dispatchEvent(new Event('resize')); return 1; })()" >/dev/null
    full="$(js "(() => $VISKEYS.length)()")"
    if [ "$got" = "Escape,Tab,BTab,C-c,Enter|true|6" ] && [ "${opened:-0}" -ge 12 ] 2>/dev/null && [ "${full:-0}" -ge 11 ] 2>/dev/null; then c_compact=PASS
    else c_compact=FAIL; fail "$tag: compact: with the mode forced to compact the visible keys are '$got' (expected 'Escape,Tab,BTab,C-c,Enter|true|6': five keys, the More toggle, six in all); after More ${opened:-?} keys (expected at least 12: the two rows and Less), back to the full bar ${full:-?} (expected at least 11)"; fi
  else
    c_compact=-
  fi

  # --- tune: the strip under the context strip. Fake board: against a faked row (the page's fetch is wrapped for the checks and
  # restored); real board: only the layout and 44 px checks on whatever row the board has.
  if [ "$REAL" = 1 ]; then tune_real_checks; else tune_fake_checks; fi

  # --- quick: the dialog editor replaces window.prompt
  quick_checks

  # --- font: the spike is off unless ccboard:term:font=1
  font_default_checks

  # --- console: CSP violations and failed fetches land here
  errs="$("$B" console --errors 2>&1 | grep -E '\[error\]' | head -n 40)"
  if [ "$SESSION" = absent ]; then
    ignore="/api/sessions/$NAME|status of (404|503)"
    [ "$REAL" = 1 ] && ignore="$ignore|WebSocket"                  # ttyd's socket to a session that is not there
    errs="$(printf '%s\n' "$errs" | grep -vE "$ignore" )"
  fi
  errs="$(printf '%s' "$errs" | sed '/^$/d' | head -n 3)"
  if [ -z "$errs" ]; then c_console=PASS; else c_console=FAIL; fail "$tag: console errors: $(printf '%s' "$errs" | tr '\n' ' ' | cut -c1-300)"; fi

  shot_out=$("$B" screenshot "$shot" 2>&1 | tail -1)
  [ -s "$shot" ] && s=yes || { s=FAIL; fail "$tag: no screenshot written to $shot (${shot_out:-no output}; the browse CLI only writes under /tmp, \$TMPDIR or the repo)"; }

  # --- kbd: last, because it reloads the page (the screenshot and the console check above are of the page as it was)
  if [ "$w" != 390 ]; then c_kbd=-
  elif [ "$REAL" = 1 ]; then c_kbd=skip                  # the soft-keyboard shim is the fake tty's
  else kbd_checks; fi
  row "$w" "$c_wrap" "$c_bounce" "$c_over" "$c_keys" "$c_tgt" "$c_comp" "$c_layout" "$c_touch" "$c_scroll" "$c_hist" "$c_compact" "$c_tune" "$c_quick" "$c_font" "$c_kbd" "$c_console" "$s"
done

# the font spike itself: fake board always, a real ttyd on request (QA_FONT=1); one viewport, a few reloads
if [ -n "$FW" ]; then
  if [ "$REAL" != 1 ]; then font_spike_fake
  elif [ "${QA_FONT:-0}" = 1 ]; then font_spike_real
  fi
fi

"$B" goto about:blank >/dev/null 2>&1

echo
if [ -n "$FONT_LINES" ]; then echo "font spike at ${FW}px wide (cols x rows after fit at 13 and 11 px; the cursor / box-drawing alignment and the phones are by-eye checks, see README):"; printf '%s' "$FONT_LINES"; echo; fi
if [ "$idx" -eq 0 ]; then echo "qa_terminal: no viewport matched QA_WIDTHS='${QA_WIDTHS:-}'" >&2; exit 2; fi
if [ "$FAILS" -gt 0 ]; then
  echo "FAIL: $FAILS assertion(s) failed"
  printf '%s' "$DETAILS"
  echo "screenshots: $OUT"
  exit 1
fi
echo "PASS: every assertion held; screenshots: $OUT"
