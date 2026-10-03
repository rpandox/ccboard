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
# Viewports: 390x844 (phone, single column), 768x1024 (tablet, single column), 1280x800 (desktop, terminal + 300 px side column).
#
# Assertions per viewport (all in the browser, over the page's own DOM and the fake tty inside its iframe):
#   wrap      #ttywrap does not scroll (scrollHeight <= clientHeight, same for width), the page itself does not scroll, the iframe fills
#             #ttywrap, and the terminal keeps at least 40 % of the window height
#   bounce    body and html have overscroll-behavior none, body.term is position:fixed, and TermKit.bind hardened the iframe document
#             (overscroll none on its root, touch-action none on .xterm)
#   overflow  document.documentElement.scrollWidth <= innerWidth
#   keys      every visible key-bar key (.kb-key) is at least 44 px tall and wide, and so is every scroll-rail button
#   targets   390 px only, with html.force-coarse: every visible button and a.bp5-button is at least 44 px tall
#   composer  #sendtext is visible and inside the window, 16 px (no iOS zoom) under force-coarse at 390
#   layout    below 840 px the key bar and the composer sit under the terminal; from 840 px the composer is in a 300 px column to its right
#   touch     a synthetic one-finger swipe on the fake .xterm-screen produces wheel events (window.__wheel grows, __lastDelta set), a swipe
#             starting 10 px from the left edge produces none
#   scroll    pressing PgUp on the scroll rail and on the key bar each POST /api/sessions/<name>/scroll {dir:'up'} (seen by wrapping
#             fetch in the page; with QA_SERVER_LOG also in the server log), and the key bar sends no raw keys for it
#   history   needs the session to exist on the board's tmux and to be a shell pane: after PgUp the History chip shows (GET /pane
#             in_mode), tapping it leaves history; "skip" otherwise
#   compact   390 px only: with the key-bar mode forced to compact only Esc, Tab, Shift+Tab, Ctrl-C/^C, Enter and More are visible, and More
#             reveals the rest
#   console   `console --errors` is empty after the run (CSP violations, failed fetches); when the session is absent on the board the
#             expected 404/503 resource failures of /api/sessions/<name> are ignored and the table says so
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
# QA_SETTLE (seconds to let a page settle, default 0.6), QA_WIDTHS (space-separated widths to run, e.g. "390 1280"; default all),
# QA_SERVER_LOG (the board's log file: the scroll check then also greps it), QA_SHARED_BROWSER (1 = drive the shared per-repo browse
# server instead of a private one that is stopped at the end).
# Exit status: 0 all assertions pass, 1 a FAIL, 2 nothing to drive the page with, the board is unreachable or /tty/ is not the fake.
set -u
set -f   # no globbing: the URLs carry ? and &

BASE="${1:-http://127.0.0.1:8777}"
OUT="${2:-${TMPDIR:-/tmp}/ccboard-qa-terminal}"
BASE="${BASE%/}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
B="${B:-$HOME/.claude/skills/gstack/browse/dist/browse}"
SETTLE="${QA_SETTLE:-0.6}"
NAME="${QA_TMUX:-qa--terminal--s1}"
SOCK="${CCBOARD_TMUX_SOCKET:-ccboard}"

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
tty_body="$(curl -s ${CURL_H[@]+"${CURL_H[@]}"} "$BASE/tty/" 2>/dev/null || true)"
if [ "$(http_code "$BASE/tty/")" != 200 ] || ! printf '%s' "$tty_body" | grep -q fake_tty; then
  echo "qa_terminal: $BASE/tty/ is not the fake tty: start the board with CCBOARD_DEV_BYPASS_USER set (scripts/dev/fake_tty is mounted only then)" >&2
  exit 2
fi
mkdir -p "$OUT" || exit 2

# ---- the session the page watches
CREATED=0
SERVER_STARTED=0
tmux_sock() { tmux -L "$SOCK" "$@"; }
if [ "${QA_TMUX_CREATE:-0}" = 1 ]; then
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

row() { printf '%-10s %-5s %-6s %-8s %-5s %-8s %-9s %-7s %-6s %-7s %-8s %-8s %-8s %s\n' "$@"; }

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
      sleep 0.25
    done
    sleep "$SETTLE"
    if [ "$(js "window.innerWidth")" = "$1" ] && [ "$got" = 1 ]; then return 0; fi
  done
  return 1
}

# A press the way a finger delivers it: pointerdown, pointerup, then the click a browser adds for a mouse (the key bar acts on pointerdown
# and ignores that click, a plain button acts on the click): $1 is a JS expression for the element.
PRESS_JS='const press = (n) => { const o = { bubbles: true, cancelable: true, pointerType: "touch", pointerId: 1, isPrimary: true, button: 0 }; n.dispatchEvent(new PointerEvent("pointerdown", o)); n.dispatchEvent(new PointerEvent("pointerup", o)); n.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, detail: 1 })); };'

echo "ccboard qa_terminal: $BASE  session=$NAME ($SESSION, pane: $PANE_ALT)  out=$OUT"
[ "$SESSION" = absent ] && echo "note: no such session on the board's tmux: the page shows 'ended', the History check is skipped and /api/sessions 404/503 console lines are ignored (see QA_TMUX_CREATE in the script header)"
echo
row VIEWPORT WRAP BOUNCE OVERFLOW KEYS TARGETS COMPOSER LAYOUT TOUCH SCROLL HISTORY COMPACT CONSOLE SHOT
row ---------- ----- ------ -------- ----- -------- --------- ------- ------ ------- -------- -------- -------- ----

# Start the browse server before row one and wait until it answers three times in a row: a cold start can take longer than the CLI waits
# ("Server failed to start within 8s"), after which it starts a second one that takes over a moment later and drops the page mid-run.
ok=0
for _ in $(seq 1 60); do
  "$B" goto about:blank >/dev/null 2>&1
  if [ "$(js "1 + 1")" = 2 ]; then ok=$((ok + 1)); [ "$ok" -ge 3 ] && break; else ok=0; fi
  sleep 1
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
  shot="$OUT/term-$tag.png"
  url="$BASE/term/$NAME?qa=$idx"                     # a new query string per load

  if ! load_term "$w" "$h" "$url"; then
    fail "$tag: the page did not settle (no #ttywrap iframe with a bound fake tty within 10 s, or the window is not ${w} wide)"
    row "$w" FAIL - - - - - - - - - - - no
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
  got="$(js "(() => { const d = document.documentElement; return d.scrollWidth <= window.innerWidth ? 'ok' : d.scrollWidth + '>' + window.innerWidth; })()")"
  if [ "$got" = ok ]; then c_over=PASS; else c_over=FAIL; fail "$tag: horizontal overflow, scrollWidth>innerWidth is $got"; fi

  # --- keys: 44 px key-bar keys and scroll-rail buttons at every width
  got="$(js "(() => { const vis = (e) => e.getClientRects().length > 0; const all = [...document.querySelectorAll('#keyhost .kb-key, #rail .rail-btn')].filter(vis);
    if (all.length < 15) return 'only ' + all.length + ' visible keys';
    const bad = all.filter((e) => e.offsetHeight < 44 || e.offsetWidth < 44);
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

  # --- layout: one column under the terminal below 840 px, a 300 px side column from 840 px
  got="$(js "(() => { const g = (s) => document.querySelector(s).getBoundingClientRect(); const wrap = g('#ttywrap'); const send = g('#sendform'); const keys = g('#keyhost'); const bad = [];
    if (innerWidth >= 840) { if (send.left < wrap.right - 1) bad.push('composer at x ' + Math.round(send.left) + ' is not right of the terminal (ends ' + Math.round(wrap.right) + ')'); if (keys.left < wrap.right - 1) bad.push('key bar is not in the side column'); if (Math.abs(send.width - 300) > 2) bad.push('side column is ' + Math.round(send.width) + ' px, expected 300'); }
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
  res="$(js "$(swipe_js -1)")"
  IFS=: read -r how before after last <<< "$res"
  if [ "${how:-}" = touch ] && [ "${after:-0}" -ge $(( ${before:-0} + 3 )) ] 2>/dev/null && [ "${last:-0}" != 0 ]; then
    edge="$(js "$(swipe_js 10)")"
    IFS=: read -r _ ebefore eafter _ <<< "$edge"
    if [ "${eafter:-x}" = "${ebefore:-y}" ]; then c_touch=PASS; else c_touch=FAIL; fail "$tag: touch: a swipe starting 10 px from the left edge made wheel events (${ebefore:-?} -> ${eafter:-?}): the iOS back gesture must be left alone"; fi
  else
    c_touch=FAIL; fail "$tag: touch: a synthetic swipe on .xterm-screen gave '$res' (expected touch:<n>:<n+3 or more>:<non-zero deltaY>; wheel events must reach window.__wheel)"
  fi

  # --- scroll: the rail's and the key bar's PgUp POST /scroll {dir:'up'}, the key bar sends no raw keys
  js "(() => { window.__qaCalls = []; if (!window.__qaWrapped) { const f = window.fetch.bind(window); window.fetch = (u, o) => { try { window.__qaCalls.push({ url: String((u && u.url) || u), method: (o && o.method) || 'GET', body: (o && o.body) || '' }); } catch (e) { /* never break the page */ } return f(u, o); }; window.__qaWrapped = 1; } return 1; })()" >/dev/null
  scroll_calls() { js "(() => JSON.stringify(window.__qaCalls.filter((c) => c.method === 'POST')))()"; }
  press_js() {   # $1 selector
    js "(() => { $PRESS_JS const n = document.querySelector('$1'); if (!n || n.getClientRects().length === 0) return 'missing'; press(n); return 'ok'; })()"
  }
  s_fail=""
  for target in "#rail .rail-btn:first-child|rail" "#keyhost .kb-key[data-key=up]|key bar"; do
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
    if [ "$got" = "Escape,Tab,BTab,C-c,Enter|true|6" ] && [ "${opened:-0}" -ge 15 ] 2>/dev/null && [ "${full:-0}" -ge 15 ] 2>/dev/null; then c_compact=PASS
    else c_compact=FAIL; fail "$tag: compact: with the mode forced to compact the visible keys are '$got' (expected 'Escape,Tab,BTab,C-c,Enter|true|6': five keys, the More toggle, six in all); after More ${opened:-?} keys (expected at least 15), back to the full bar ${full:-?} (expected at least 15)"; fi
  else
    c_compact=-
  fi

  # --- console: CSP violations and failed fetches land here
  errs="$("$B" console --errors 2>&1 | grep -E '\[error\]' | head -n 40)"
  if [ "$SESSION" = absent ]; then errs="$(printf '%s\n' "$errs" | grep -vE "/api/sessions/$NAME|status of (404|503)" )"; fi
  errs="$(printf '%s' "$errs" | sed '/^$/d' | head -n 3)"
  if [ -z "$errs" ]; then c_console=PASS; else c_console=FAIL; fail "$tag: console errors: $(printf '%s' "$errs" | tr '\n' ' ' | cut -c1-300)"; fi

  shot_out=$("$B" screenshot "$shot" 2>&1 | tail -1)
  [ -s "$shot" ] && s=yes || { s=FAIL; fail "$tag: no screenshot written to $shot (${shot_out:-no output}; the browse CLI only writes under /tmp, \$TMPDIR or the repo)"; }
  row "$w" "$c_wrap" "$c_bounce" "$c_over" "$c_keys" "$c_tgt" "$c_comp" "$c_layout" "$c_touch" "$c_scroll" "$c_hist" "$c_compact" "$c_console" "$s"
done

"$B" goto about:blank >/dev/null 2>&1

echo
if [ "$idx" -eq 0 ]; then echo "qa_terminal: no viewport matched QA_WIDTHS='${QA_WIDTHS:-}'" >&2; exit 2; fi
if [ "$FAILS" -gt 0 ]; then
  echo "FAIL: $FAILS assertion(s) failed"
  printf '%s' "$DETAILS"
  echo "screenshots: $OUT"
  exit 1
fi
echo "PASS: every assertion held; screenshots: $OUT"
