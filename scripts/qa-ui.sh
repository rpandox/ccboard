#!/usr/bin/env bash
# qa-ui.sh: drive the ccboard shell in demo mode through the phone, tablet, desktop, installed-desktop and iPad viewports and check it.
#
#   scripts/qa-ui.sh [BASE_URL] [OUT_DIR]
#
#   BASE_URL  the board to test, default http://127.0.0.1:8777 (a local app started with CCBOARD_DEV_BYPASS_USER, see
#             README "Development"); every URL gets ?demo=1 so the pages read app/static/demo/*.json and need no tmux.
#   OUT_DIR   screenshots land here as <route>-<width>.png (<route>-pwa-<width>.png for the installed-desktop pass,
#             <route>-ipad-<width>x<height>.png for the iPad passes), default ${TMPDIR:-/tmp}/ccboard-qa-ui
#             (keep it under /tmp, $TMPDIR or the repo: the gstack browse CLI refuses to write anywhere else)
#
# With the gstack browser CLI ($B, default ~/.claude/skills/gstack/browse/dist/browse) every route at every viewport is
# asserted and screenshotted; without it headless Chrome only takes the screenshots (assertions show "skip").
#
# Passes (VIEWPORTS below; QA_WIDTHS filters them by width):
#   base  390 compact, 768 medium, 1024x768 expanded, 1280 large
#   pwa   1440x900 large with html.pwa injected (the installed-app class main.js sets in standalone mode)
#   ipad  1024x1366 expanded and 834x1194 medium (iPad Pro 12.9 / 11 inch portrait, also the Split View widths)
#   Lighthouse (optional, after the passes): when a Lighthouse is available (LIGHTHOUSE_BIN, else `npx --no-install lighthouse`),
#   performance / pwa / accessibility scores for Home, Agents, Usage, a project page, Settings and the terminal page, each with
#   --preset=desktop and with the mobile default, as median/worst of QA_LIGHTHOUSE_RUNS runs (default 3 in strict mode, else 1), are
#   printed and written to $OUT/lighthouse-summary.tsv. Advisory unless QA_LIGHTHOUSE_STRICT=1, which makes a median under target (performance
#   90, pwa 95 where the category exists, accessibility 95 on desktop; performance 90 on mobile), a run that writes no report and a missing
#   Lighthouse each a FAIL: strict mode never passes without a measurement.
#
# Assertions per route and viewport (gstack path):
#   shell    body[data-shell] is compact / medium / expanded / large (390 / 768 / 834 / 1024 / 1280 / 1440 px)
#   bnav     #bnav is visible at 390 px and hidden everywhere else
#   page     body[data-page] is the route's id and #page is not empty (session peek: dialog#sheet open; #dock belongs to the terminal dock from 1024 px, v0.5.9)
#   console  `console --errors` is empty after the load (CSP violations and failed fetches land there)
#   overflow document.documentElement.scrollWidth <= innerWidth (no horizontal scroll)
#   targets  at 390 px with html.force-coarse every visible button and a.bp5-button is at least 44 px tall
#   topbar   pwa pass only: html.pwa is set and #topbar is at least 48 px tall (the title-bar height under Window Controls Overlay)
#   extra    per-route checks of the project page (v0.5.6): '#/p/phasezero' shows the project name and its code-server link, '?tab=files' mounts
#            a role=tree with treeitems and exactly one tabbable node (roving tabindex), '?tab=tasks' draws the Backlog column
#            with a startable card that has a Start button, an in-progress card of a task handed to a running session with its owner
#            chip (a link to the session, v0.5.14b), and '+ task' opens the task form (Run: Now | Later | Schedule, one pressed, a submit button, every button of the sheet at
#            least 44 px at 390 px); '#/tasks' (v0.5.14a) has the Backlog column with a Start card and a '+ task' button that opens the
#            repo picker (its rows at least 44 px at 390 px); the 10x pass: 3 taps from the project page to a running task, 1 from a backlog card.
#            These two checks click '+ task', so the screenshots of '#/tasks' and '?tab=tasks' show the sheet open over the board, on purpose.
#
# Environment: B (browse binary), CHROME (Chrome binary), QA_TMUX (session for #/s/<tmux>, default: the first session in
# app/static/demo/state.json), QA_HEADER (e.g. 'Tailscale-User-Login: demo@example.com' when the board is not in dev bypass),
# QA_SETTLE (seconds to let a page settle after it mounts, default 0.6), QA_WIDTHS (space-separated widths to run, e.g. "390 1280"; default all),
# QA_LIGHTHOUSE (0 = skip the Lighthouse step; default: run it when lighthouse is available), QA_LIGHTHOUSE_STRICT (1 = a score under target is a FAIL,
# and so is a Lighthouse that is not there), LIGHTHOUSE_BIN (the lighthouse executable, installed outside the repo, e.g.
# `npm i --prefix /tmp/lh lighthouse@<version>` then LIGHTHOUSE_BIN=/tmp/lh/node_modules/.bin/lighthouse), QA_LIGHTHOUSE_RUNS (runs per page and preset),
# QA_LIGHTHOUSE_PAGES ("label|url label|url ...", default home agents usage project settings terminal),
# QA_SHARED_BROWSER (1 = drive the shared per-repo browse server instead of a private one that is stopped at the end),
# QA_MANIFEST_SHOTS (1 = also capture the two manifest screenshots, agents-390.png at 390x844 and agents-1280.png at 1280x800,
# into $QA_SHOTS_DIR, default app/static/screenshots/: viewport-only shots of '#/agents' in demo mode, sizes verified).
# Exit status: 0 all assertions pass (or screenshots only), 1 a FAIL, 2 nothing to drive the page with or board unreachable.
set -u
set -f   # no globbing: the routes carry ? and #

BASE="${1:-http://127.0.0.1:8777}"
OUT="${2:-${TMPDIR:-/tmp}/ccboard-qa-ui}"
BASE="${BASE%/}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_JSON="$HERE/../app/static/demo/state.json"
B="${B:-$HOME/.claude/skills/gstack/browse/dist/browse}"
CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
SETTLE="${QA_SETTLE:-0.6}"

if [ -x "$B" ]; then
  MODE=gstack
elif [ -x "$CHROME" ]; then
  MODE=chrome
else
  echo "qa-ui: neither the gstack browse CLI ($B) nor Chrome ($CHROME) was found" >&2
  exit 2
fi

CURL_H=()
[ -n "${QA_HEADER:-}" ] && CURL_H=(-H "$QA_HEADER")
code="$(curl -s -o /dev/null -w '%{http_code}' ${CURL_H[@]+"${CURL_H[@]}"} "$BASE/" 2>/dev/null || true)"
case "$code" in
  200) ;;
  000|"") echo "qa-ui: $BASE is not reachable (start the board, see README Development)" >&2; exit 2 ;;
  403) echo "qa-ui: $BASE answered 403: start the board with CCBOARD_DEV_BYPASS_USER=<user> or pass QA_HEADER" >&2; exit 2 ;;
  *) echo "qa-ui: warning: GET $BASE/ answered $code" >&2 ;;
esac
# issue #100: a board that could reach the real home is not a QA target. /api/state carries dev.sandboxed under the dev bypass only;
# false or absent (a production board, an old build, an unreachable API) is refused. Start it with the four directories on temp paths and
# scripts/dev/fake_agents first on PATH (README, Development).
sandboxed="$(curl -s ${CURL_H[@]+"${CURL_H[@]}"} "$BASE/api/state" 2>/dev/null | python3 -c 'import json,sys; print("yes" if json.load(sys.stdin).get("dev",{}).get("sandboxed") is True else "no")' 2>/dev/null || true)"
if [ "$sandboxed" != yes ]; then
  echo "qa-ui: $BASE does not report dev.sandboxed=true in /api/state: refusing to run against it. Start the dev board with PROJECTS_DIR, CCBOARD_DATA_DIR, CLAUDE_CONFIG_DIR and CODEX_HOME on temp paths and scripts/dev/fake_agents first on PATH (README, Development)" >&2
  exit 2
fi
mkdir -p "$OUT" || exit 2

# A private browser by default: the gstack browse server is shared per repo, so another agent or a stray tab resizing or
# navigating it mid-run turns assertions into noise. QA_SHARED_BROWSER=1 drives the shared one instead (e.g. to watch it).
if [ "$MODE" = gstack ] && [ "${QA_SHARED_BROWSER:-0}" != 1 ]; then
  mkdir -p "$OUT/.browse" || exit 2
  export BROWSE_STATE_FILE="$OUT/.browse/browse.json"
  trap '"$B" stop >/dev/null 2>&1' EXIT
fi

# the first session of the demo fleet, for the #/s/<tmux> peek
first_tmux() {
  if [ -n "${QA_TMUX:-}" ]; then printf '%s' "$QA_TMUX"; return; fi
  local src
  if [ -f "$STATE_JSON" ]; then src="$(cat "$STATE_JSON")"
  else src="$(curl -s ${CURL_H[@]+"${CURL_H[@]}"} "$BASE/static/demo/state.json")"; fi
  printf '%s' "$src" | python3 -c '
import json, sys
s = json.load(sys.stdin)
for p in s.get("projects", []):
    for r in ([p["root"]] if p.get("root") else []) + p.get("repos", []):
        for x in r.get("sessions", []):
            print(x["tmux"]); sys.exit(0)
    for x in p.get("orphan_sessions", []):
        print(x["tmux"]); sys.exit(0)
' 2>/dev/null || printf '%s' "$src" | grep -o '"tmux": *"[^"]*"' | head -n1 | sed 's/.*: *"\(.*\)"/\1/'
}
TMUX_NAME="$(first_tmux)"
[ -n "$TMUX_NAME" ] || TMUX_NAME="ccboard--ccboard--s1"

# width x height : expected body[data-shell] [: pass]; a pass is pwa (html.pwa injected, topbar checked) or ipad (a tablet size)
VIEWPORTS="390x844:compact 768x1024:medium 1024x768:expanded 1280x800:large 1440x900:large:pwa 1024x1366:expanded:ipad 834x1194:medium:ipad"
# hash route | expected body[data-page]
ROUTES="#/|home #/?f=waiting|home #/inbox|inbox #/agents|agents #/tasks|tasks #/p/phasezero|project #/p/phasezero?tab=files|project #/p/phasezero?tab=tasks|project #/settings|settings #/search?q=auth|search #/s/$TMUX_NAME|session"

slug() {
  local s="${1#\#/}"
  [ -z "$s" ] && s=home
  case "$s" in \?*) s="home$s" ;; esac     # '#/?f=waiting' is Home with a filter: home-f-waiting
  printf '%s' "$s" | tr '/?=&#' '-----'
}

# route-specific assertion: a JS expression that answers 'ok' or what is wrong (the project page, v0.5.6); empty = nothing extra for the route
extra_check() {
  case "$1" in
    "#/p/phasezero")
      printf '%s' "(() => { const p = document.querySelector('#page'); if (!p || p.textContent.indexOf('phasezero') < 0) return 'the project name is not on the page'; return p.querySelector('a[href*=\"folder=\"]') ? 'ok' : 'no code-server folder link'; })()" ;;
    "#/p/phasezero?tab=files")
      printf '%s' "(() => { const t = document.querySelector('#page [role=tree]'); if (!t) return 'no [role=tree]'; const n = t.querySelectorAll('[role=treeitem]').length; if (!n) return 'the tree has no [role=treeitem]'; const tab = t.querySelectorAll('[role=treeitem][tabindex=\"0\"]').length; return tab === 1 ? 'ok' : tab + ' tabbable treeitems (roving tabindex wants exactly 1)'; })()" ;;
    "#/p/phasezero?tab=tasks")
      # a backlog card with Start, the 'in <session>' chip on the session-mode card, then + task opens the form (the sheet is built synchronously)
      printf '%s' "(() => { const p = document.querySelector('#page'); if (!p) return 'no page'; const cols = [...p.querySelectorAll('.col')]; const col = (re) => cols.find((c) => re.test((c.querySelector('h2') || {}).textContent || '')); const bl = col(/^Backlog/); if (!bl) return 'no Backlog column'; const card = [...bl.querySelectorAll('.task')].find((c) => c.getAttribute('data-phase') === 'backlog'); if (!card) return 'no startable card in the Backlog column (the demo has two; a queued chain step has no Start by design)'; if (![...card.querySelectorAll('button')].some((b) => b.textContent.trim() === 'Start')) return 'the backlog card has no Start button'; const ip = col(/^In progress/); const chip = ip && [...ip.querySelectorAll('.task a')].find((a) => a.classList.contains('tk-owner') && /^#\\/s\\//.test(a.getAttribute('href') || '')); if (!chip) return 'no owner chip (a link to the session peek) on the in-progress card of the task handed to a running session'; const add = [...p.querySelectorAll('button')].find((b) => /\\+\\s*task/.test(b.textContent)); if (!add) return 'no + task button'; add.click(); const sh = document.querySelector('#sheet'); if (!sh || !sh.open) return '+ task did not open the sheet'; const run = sh.querySelector('.seg-ctl[aria-label=Run]'); if (!run) return 'the task form has no Run control'; const segs = [...run.querySelectorAll('.seg-btn')].map((b) => b.textContent.trim() + ':' + b.getAttribute('aria-pressed')); if (segs.map((s) => s.split(':')[0]).join('|') !== 'Now|Later|Schedule') return 'the Run control is ' + segs.join(','); if (segs.filter((s) => /:true\$/.test(s)).length !== 1) return 'not exactly one Run mode is pressed: ' + segs.join(','); if (!sh.querySelector('button[type=submit]')) return 'the task form has no submit button'; const small = document.documentElement.classList.contains('force-coarse') ? [...sh.querySelectorAll('button')].filter((b) => b.getClientRects().length > 0 && b.offsetHeight < 44).map((b) => (b.textContent.trim() || b.getAttribute('aria-label') || b.className) + '=' + b.offsetHeight) : []; return small.length ? 'task form targets under 44 px: ' + small.slice(0, 4).join(',') : 'ok'; })()" ;;
    "#/tasks")
      printf '%s' "(() => { const p = document.querySelector('#page'); if (!p) return 'no page'; const bl = [...p.querySelectorAll('.col')].find((c) => /^Backlog/.test((c.querySelector('h2') || {}).textContent || '')); if (!bl) return 'no Backlog column'; const card = [...bl.querySelectorAll('.task')].find((c) => c.getAttribute('data-phase') === 'backlog'); if (!card) return 'no startable card in the Backlog column (the demo has two; a queued chain step has no Start by design)'; if (![...card.querySelectorAll('button')].some((b) => b.textContent.trim() === 'Start')) return 'the backlog card has no Start button'; const add = [...p.querySelectorAll('button')].find((b) => /\\+\\s*task/.test(b.textContent)); if (!add) return 'no + task button in the page head'; add.click(); const sh = document.querySelector('#sheet'); if (!(sh && sh.open && sh.querySelectorAll('.pick-row').length > 0)) return '+ task did not open the repo picker'; const small = document.documentElement.classList.contains('force-coarse') ? [...sh.querySelectorAll('button')].filter((b) => b.getClientRects().length > 0 && b.offsetHeight < 44).map((b) => (b.textContent.trim() || b.getAttribute('aria-label') || b.className) + '=' + b.offsetHeight) : []; return small.length ? 'picker targets under 44 px: ' + small.slice(0, 4).join(',') : 'ok'; })()" ;;
  esac
}

FAILS=0
DETAILS=""
fail() { FAILS=$((FAILS + 1)); DETAILS="${DETAILS}  $1"$'\n'; }

js() { "$B" js "$1" 2>/dev/null | tr -d '\r' | tail -n 1; }

row() { printf '%-36s %-10s %-6s %-6s %-6s %-8s %-9s %-8s %-7s %-6s %s\n' "$@"; }

# Load one route at one size and wait for the shell to be built and the router to have run. The browse server is one shared
# browser: if anything else drives it meanwhile (another agent, a stray tab) the size or the hash is not what was asked for,
# so the load is verified and retried up to three times before the assertions run on whatever is there.
load_route() {   # width height url hash
  local tries=0 got
  while [ "$tries" -lt 3 ]; do
    tries=$((tries + 1))
    "$B" viewport "${1}x${2}" >/dev/null 2>&1
    "$B" console --clear >/dev/null 2>&1
    "$B" goto "$3" >/dev/null 2>&1
    for _ in $(seq 1 20); do                       # up to 5 s for body[data-shell] and body[data-page] to be set
      got="$(js "(document.body && document.body.dataset.shell && document.body.dataset.page) ? 1 : 0")"
      case "$got" in ""|0) ;; *) break ;; esac     # empty = the browse server is still starting
      sleep 0.25
    done
    sleep "$SETTLE"                                # the first state update lands after the mount
    if [ "$(js "window.innerWidth")" = "$1" ] && [ "$(js "location.hash")" = "$4" ]; then return 0; fi
  done
  return 1
}

echo "ccboard qa-ui: $BASE  mode=$MODE  out=$OUT"
echo "session peek route: #/s/$TMUX_NAME"
echo
row ROUTE VIEWPORT SHELL BNAV PAGE CONSOLE OVERFLOW TARGETS TOPBAR EXTRA SHOT
row ---------------------------------- ---------- ------ ------ ------ -------- --------- -------- ------- ------ ----

if [ "$MODE" = gstack ] && [ -n "${QA_HEADER:-}" ]; then "$B" header "$QA_HEADER" >/dev/null 2>&1; fi

if [ "$MODE" = gstack ]; then "$B" goto "$BASE/?demo=1&qa=warmup" >/dev/null 2>&1; sleep 1; fi   # start the browse server before row one

idx=0
for vp in $VIEWPORTS; do
  IFS=: read -r size want_shell vpass <<< "$vp"
  w="${size%%x*}"; h="${size##*x}"
  case " ${QA_WIDTHS:-$w} " in *" $w "*) ;; *) continue ;; esac
  case "${vpass:-base}" in                 # what the viewport column shows, and the screenshot name suffix
    pwa)  vlabel="$w pwa"; vtag="pwa-$w" ;;
    ipad) vlabel="${w}x${h}"; vtag="ipad-${w}x${h}" ;;
    *)    vlabel="$w"; vtag="$w" ;;
  esac
  for rt in $ROUTES; do
    route="${rt%%|*}"; want_page="${rt##*|}"
    idx=$((idx + 1))
    name="$(slug "$route")"
    shot="$OUT/$name-$vtag.png"
    url="$BASE/?demo=1&qa=$idx$route"      # a new query string per load: a real document load every time, not a hashchange

    if [ "$MODE" = chrome ]; then
      "$CHROME" --headless=new --disable-gpu --no-first-run --no-default-browser-check --disable-extensions --hide-scrollbars \
        --window-size="$w,$h" --virtual-time-budget=4000 \
        --screenshot="$shot" "$url" >/dev/null 2>&1
      [ -s "$shot" ] && s=yes || { s=FAIL; fail "$name@$vtag: headless Chrome wrote no screenshot"; }
      row "$route" "$vlabel" skip skip skip skip skip skip skip skip "$s"
      continue
    fi

    load_route "$w" "$h" "$url" "$route"
    [ "$w" = 390 ] && js "(() => { document.documentElement.classList.add('force-coarse'); return 1; })()" >/dev/null
    # the installed-app pass: main.js sets html.pwa in standalone mode, a browser tab never does, so it is injected
    [ "${vpass:-base}" = pwa ] && js "(() => { document.documentElement.classList.add('pwa'); return 1; })()" >/dev/null

    # shell mode
    got="$(js "document.body.dataset.shell || ''")"
    if [ "$got" = "$want_shell" ]; then c_shell=PASS; else c_shell=FAIL; fail "$name@$vtag: body[data-shell] is '$got', expected '$want_shell'"; fi

    # bottom nav: only the phone shell shows it
    got="$(js "(() => { const n = document.querySelector('#bnav'); if (!n) return 'missing'; const cs = getComputedStyle(n); return cs.display !== 'none' && cs.visibility !== 'hidden' && n.getClientRects().length > 0 ? 'visible' : 'hidden'; })()")"
    if [ "$w" = 390 ]; then want_bnav=visible; else want_bnav=hidden; fi
    if [ "$got" = "$want_bnav" ]; then c_bnav=PASS; else c_bnav=FAIL; fail "$name@$vtag: #bnav is $got, expected $want_bnav"; fi

    # the router mounted the right page; the session peek lives in dialog#sheet (in #dock only when the terminal dock is off), never in #page
    if [ "$want_page" = session ]; then
      got="$(js "(() => { const d = document.querySelector('#dock'); const s = document.querySelector('#sheet'); const dock = !!d && !d.classList.contains('hidden') && d.childElementCount > 0; const sheet = !!s && s.open === true && s.childElementCount > 0; return (document.body.dataset.page || '') + ':' + (dock ? 'dock' : sheet ? 'sheet' : 'none'); })()")"
      want_peek=sheet                  # v0.5.9 (3e044b7): from 1024 px the terminal dock owns #dock, so the peek is a sheet at every width (dock only with ccboard:dock:off, peekSurface in session.js)
      if [ "$got" = "session:$want_peek" ]; then c_page=PASS; else c_page=FAIL; fail "$name@$vtag: session peek is '$got', expected 'session:$want_peek'"; fi
    else
      got="$(js "(() => { const p = document.querySelector('#page'); return (document.body.dataset.page || '') + ':' + (p ? p.childElementCount : -1); })()")"
      if [ "${got%%:*}" = "$want_page" ] && [ "${got##*:}" -gt 0 ] 2>/dev/null; then c_page=PASS; else c_page=FAIL; fail "$name@$vtag: page is '$got', expected '$want_page' with children"; fi
    fi

    # console errors since the load
    errs="$("$B" console --errors 2>&1 | grep -E '\[error\]' | head -n 3)"
    if [ -z "$errs" ]; then c_console=PASS; else c_console=FAIL; fail "$name@$vtag: console errors: $(printf '%s' "$errs" | tr '\n' ' ' | cut -c1-300)"; fi

    # no horizontal overflow
    got="$(js "(() => { const d = document.documentElement; return d.scrollWidth <= window.innerWidth ? 'ok' : d.scrollWidth + '>' + window.innerWidth; })()")"
    if [ "$got" = ok ]; then c_over=PASS; else c_over=FAIL; fail "$name@$vtag: horizontal overflow, scrollWidth>innerWidth is $got"; fi

    # 44 px touch targets (phone width, coarse pointer forced above)
    if [ "$w" = 390 ]; then
      got="$(js "(() => { const bad = [...document.querySelectorAll('button, a.bp5-button')].filter((e) => e.getClientRects().length > 0 && e.offsetHeight < 44); return bad.length + '|' + bad.slice(0, 4).map((e) => (e.id ? '#' + e.id : String(e.className || e.tagName).split(' ').slice(0, 2).join('.')) + '=' + e.offsetHeight).join(','); })()")"
      if [ "${got%%|*}" = 0 ]; then c_tgt=PASS; else c_tgt=FAIL; fail "$name@$vtag: ${got%%|*} target(s) under 44 px: ${got#*|}"; fi
    else
      c_tgt=-
    fi

    # installed desktop app: the topbar is the title bar, never thinner than 48 px
    if [ "${vpass:-base}" = pwa ]; then
      got="$(js "(() => { const t = document.querySelector('#topbar'); return (document.documentElement.classList.contains('pwa') ? 'pwa' : 'no-pwa-class') + ':' + (t ? t.offsetHeight : -1); })()")"
      if [ "${got%%:*}" = pwa ] && [ "${got##*:}" -ge 48 ] 2>/dev/null; then c_top=PASS; else c_top=FAIL; fail "$name@$vtag: #topbar under html.pwa is '$got', expected pwa with height >= 48"; fi
    else
      c_top=-
    fi

    # route-specific content (the project page): see extra_check
    xjs="$(extra_check "$route")"
    if [ -n "$xjs" ]; then
      got="$(js "$xjs")"
      if [ "$got" = ok ]; then c_extra=PASS; else c_extra=FAIL; fail "$name@$vtag: ${got:-no answer from the page}"; fi
    else
      c_extra=-
    fi

    shot_out=$("$B" screenshot "$shot" 2>&1 | tail -1)
    [ -s "$shot" ] && s=yes || { s=FAIL; fail "$name@$vtag: no screenshot written to $shot (${shot_out:-no output}; the browse CLI only writes under /tmp, \$TMPDIR or the repo)"; }
    row "$route" "$vlabel" "$c_shell" "$c_bnav" "$c_page" "$c_console" "$c_over" "$c_tgt" "$c_top" "$c_extra" "$s"
  done
done

# ---- manifest screenshots (opt-in: they are committed under app/static/screenshots and named in the manifest)
png_size() { python3 -c 'import struct, sys; print("%dx%d" % struct.unpack(">II", open(sys.argv[1], "rb").read(24)[16:24]))' "$1" 2>/dev/null; }

if [ "${QA_MANIFEST_SHOTS:-0}" = 1 ]; then
  if [ "$MODE" != gstack ]; then
    echo "manifest screenshots: need the gstack browse CLI, skipped"
  else
    SHOTS_DIR="${QA_SHOTS_DIR:-$(cd "$HERE/.." && pwd)/app/static/screenshots}"
    mkdir -p "$SHOTS_DIR"
    for spec in 390x844:agents-390 1280x800:agents-1280; do
      size="${spec%%:*}"; sname="${spec##*:}"
      if ! load_route "${size%%x*}" "${size##*x}" "$BASE/?demo=1&qa=manifest-$sname#/agents" "#/agents"; then
        fail "manifest shot $sname: the browser did not settle on #/agents at $size"
        continue
      fi
      rm -f "$OUT/manifest-$sname.png"          # the browse CLI writes under /tmp or $TMPDIR only (or its cwd): shoot into OUT, then copy
      "$B" screenshot --viewport "$OUT/manifest-$sname.png" >/dev/null 2>&1
      got="$(png_size "$OUT/manifest-$sname.png")"
      if [ "$got" = "$size" ] && cp "$OUT/manifest-$sname.png" "$SHOTS_DIR/$sname.png"; then echo "manifest screenshot: $SHOTS_DIR/$sname.png ($got)"
      else fail "manifest shot $sname: wrote '${got:-nothing}', expected $size"; fi
    done
  fi
fi

# ---- Lighthouse (optional; strict with QA_LIGHTHOUSE_STRICT=1): performance / pwa / accessibility per page, desktop preset and mobile default
# LIGHTHOUSE_BIN points at a Lighthouse installed outside the repo (npm i --prefix /tmp/lh lighthouse@<version>; then LIGHTHOUSE_BIN=/tmp/lh/node_modules/.bin/lighthouse);
# without it `npx --no-install lighthouse` is tried. Pages: QA_LIGHTHOUSE_PAGES="label|url ..." (default Home, Agents, Usage, a project page, Settings, the terminal page).
# Each page and preset runs QA_LIGHTHOUSE_RUNS times (default 3 in strict mode, else 1); the median and the worst are printed and the MEDIAN is held to the target.
# Targets: performance >= 90, pwa >= 95 (where this Lighthouse still has the category), accessibility >= 95 on desktop; performance >= 90 on mobile.
# Strict mode: a score under target is a FAIL, and so is a run that cannot find Lighthouse or that writes no report (it says it did not measure, it never passes silently).
LH_LOW=""
LH_BIN="${LIGHTHOUSE_BIN:-}"
LH_STRICT="${QA_LIGHTHOUSE_STRICT:-0}"
LH_RUNS="${QA_LIGHTHOUSE_RUNS:-}"
[ -n "$LH_RUNS" ] || { [ "$LH_STRICT" = 1 ] && LH_RUNS=3 || LH_RUNS=1; }
LH_PAGES="${QA_LIGHTHOUSE_PAGES:-home|$BASE/?demo=1#/ agents|$BASE/?demo=1#/agents usage|$BASE/?demo=1#/usage project|$BASE/?demo=1#/p/phasezero settings|$BASE/?demo=1#/settings terminal|$BASE/term/$TMUX_NAME?demo=1}"
LH_TABLE="$OUT/lighthouse-summary.tsv"

lh() { if [ -n "$LH_BIN" ]; then "$LH_BIN" "$@"; else npx --no-install lighthouse "$@"; fi; }

lh_available() {   # true when a Lighthouse can be run: LIGHTHOUSE_BIN executable and answering --version, else npx --no-install
  if [ -n "$LH_BIN" ]; then [ -x "$LH_BIN" ] && "$LH_BIN" --version </dev/null >/dev/null 2>&1
  else command -v npx >/dev/null 2>&1 && npx --no-install lighthouse --version </dev/null >/dev/null 2>&1; fi
}

lh_run() {   # url json-path log-path categories [lighthouse flags...]: true when a report was written
  local url="$1" json="$2" log="$3" cats="$4" cp="${CHROME_PATH:-}"; shift 4
  [ -z "$cp" ] && [ -x "$CHROME" ] && cp="$CHROME"
  rm -f "$json"
  CHROME_PATH="$cp" lh "$url" "$@" --only-categories="$cats" --chrome-flags='--headless=new' \
    --output=json --output-path="$json" --quiet ${LH_ARGS[@]+"${LH_ARGS[@]}"} </dev/null >"$log" 2>&1
  [ -s "$json" ]
}

lh_scores() {   # json-path -> "performance pwa accessibility" as 0-100 integers, n/a for a category the report lacks
  python3 -c '
import json, sys
cats = json.load(open(sys.argv[1])).get("categories", {})
def sc(k):
    v = (cats.get(k) or {}).get("score")
    return "n/a" if v is None else str(round(v * 100))
print(sc("performance"), sc("pwa"), sc("accessibility"))' "$1" 2>/dev/null
}

lh_stat() {   # "81 90 n/a 88" -> "median/worst" of the numbers ("n/a" when there are none): the worst is the lowest
  python3 -c '
import statistics, sys
v = [int(x) for x in sys.argv[1:] if x.lstrip("-").isdigit()]
print("n/a" if not v else "%d/%d" % (round(statistics.median(v)), min(v)))' "$@" 2>/dev/null
}

lh_below() {   # label metric median target: remember a median under its target (n/a and no target never count)
  local med="${3%%/*}"
  [ -z "$4" ] || [ "$med" = n/a ] || [ -z "$med" ] && return 0
  [ "$med" -lt "$4" ] 2>/dev/null && LH_LOW="$LH_LOW $1:$2=$med<$4"
  return 0
}

lh_page() {   # label url: every preset, QA_LIGHTHOUSE_RUNS runs each
  local label="$1" url="$2" form json log perf pwa a11y i ok lp lw la
  for form in desktop mobile; do
    local preset=(); [ "$form" = desktop ] && preset=(--preset=desktop)
    lp=""; lw=""; la=""; ok=0
    for i in $(seq 1 "$LH_RUNS"); do
      json="$OUT/lighthouse-$label-$form-$i.json"; log="$OUT/lighthouse-$label-$form-$i.log"
      # Lighthouse 12 dropped the pwa category: if the three-category run is refused, ask for the other two
      if lh_run "$url" "$json" "$log" performance,pwa,accessibility ${preset[@]+"${preset[@]}"} || lh_run "$url" "$json" "$log" performance,accessibility ${preset[@]+"${preset[@]}"}; then
        read -r perf pwa a11y <<< "$(lh_scores "$json")"
        lp="$lp ${perf:-n/a}"; lw="$lw ${pwa:-n/a}"; la="$la ${a11y:-n/a}"; ok=$((ok + 1))
      else
        echo "lighthouse $label $form run $i: no report written (see $log)"
      fi
    done
    if [ "$ok" -eq 0 ]; then
      [ "$LH_STRICT" = 1 ] && fail "lighthouse $label $form: no report was written, nothing was measured"
      continue
    fi
    perf="$(lh_stat $lp)"; pwa="$(lh_stat $lw)"; a11y="$(lh_stat $la)"
    printf 'lighthouse %-9s %-8s performance %-7s pwa %-7s accessibility %-7s (median/worst of %d)\n' "$label" "$form" "$perf" "$pwa" "$a11y" "$ok"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%d\n' "$label" "$form" "$perf" "$pwa" "$a11y" "$url" "$ok" >> "$LH_TABLE"
    if [ "$form" = desktop ]; then lh_below "$label/desktop" performance "$perf" 90; lh_below "$label/desktop" pwa "$pwa" 95; lh_below "$label/desktop" accessibility "$a11y" 95
    else lh_below "$label/mobile" performance "$perf" 90; fi
  done
}

if [ "${QA_LIGHTHOUSE:-auto}" = 0 ]; then
  :
elif lh_available; then
  LH_ARGS=()
  if [ -n "${QA_HEADER:-}" ]; then
    LH_ARGS=(--extra-headers="$(python3 -c 'import json, sys; k, v = sys.argv[1].split(":", 1); print(json.dumps({k.strip(): v.strip()}))' "$QA_HEADER")")
  fi
  printf 'page\tpreset\tperformance (median/worst)\tpwa\taccessibility\turl\truns\n' > "$LH_TABLE"
  echo
  echo "lighthouse $(lh --version </dev/null 2>/dev/null | tail -n 1) (targets: performance >= 90, pwa >= 95, accessibility >= 95 on desktop; performance >= 90 on mobile; $LH_RUNS run(s) per page and preset)"
  for pg in $LH_PAGES; do lh_page "${pg%%|*}" "${pg#*|}"; done
  echo "lighthouse table: $LH_TABLE"
  if [ -n "$LH_LOW" ]; then
    echo "lighthouse below target:$LH_LOW"
    [ "$LH_STRICT" = 1 ] && fail "lighthouse scores under target:$LH_LOW"
  fi
else
  if [ "$LH_STRICT" = 1 ]; then
    echo "lighthouse: NOT FOUND (${LH_BIN:-npx --no-install lighthouse}); strict mode cannot pass without a measurement"
    fail "lighthouse strict mode: Lighthouse was not found (set LIGHTHOUSE_BIN or install it outside the repo); nothing was measured"
  else
    echo "lighthouse: not available (${LH_BIN:-npx --no-install lighthouse} fails), skipped"
  fi
fi

echo
if [ "$FAILS" -gt 0 ]; then
  echo "FAIL: $FAILS assertion(s) failed"
  printf '%s' "$DETAILS"
  echo "screenshots: $OUT"
  exit 1
fi
if [ "$MODE" = chrome ]; then echo "screenshots only (no gstack browse CLI): $OUT"; else echo "PASS: every assertion held; screenshots: $OUT"; fi
