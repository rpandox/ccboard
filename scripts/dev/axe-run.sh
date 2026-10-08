#!/usr/bin/env bash
# axe-run.sh <url> [WxH]: open a page of a running board (demo mode: http://127.0.0.1:8777/?demo=1#/agents) in a headless browser, inject the vendored
# axe-core (scripts/dev/axe/axe.min.js, MPL-2.0, version in scripts/dev/axe/VERSION), run axe.run() and print one line per violation:
#   <impact> <rule id> <selector of the first node> (<n> nodes in all)
# Exit 0 = no serious or critical violation, 1 = at least one, 2 = nothing was measured (no browser, or axe did not run).
# axe is injected by the browser tool, which evaluates outside the page's CSP: the strict CSP is not relaxed and nothing under app/static/ holds axe.
# BROWSE=<the gstack `browse` binary> (default ~/.claude/skills/gstack/browse/dist/browse). Size: the second argument or AXE_SIZE (default 1280x800;
# 390x844 for a phone). AXE_COARSE=1 adds the force-coarse class first, the touch layout (44 px targets).
set -u
URL="${1:-}"
[ -n "$URL" ] || { echo "usage: scripts/dev/axe-run.sh <url> [WxH]" >&2; exit 2; }
SIZE="${2:-${AXE_SIZE:-1280x800}}"
HERE="$(cd "$(dirname "$0")" && pwd)"
BROWSE="${BROWSE:-$HOME/.claude/skills/gstack/browse/dist/browse}"
[ -x "$BROWSE" ] || { echo "axe-run: no browser at $BROWSE (set BROWSE=...); nothing was measured" >&2; exit 2; }
[ -f "$HERE/axe/axe.min.js" ] || { echo "axe-run: scripts/dev/axe/axe.min.js is missing; nothing was measured" >&2; exit 2; }
"$BROWSE" viewport "$SIZE" >/dev/null 2>&1
"$BROWSE" goto "$URL" >/dev/null 2>&1 || { echo "axe-run: could not open $URL; nothing was measured" >&2; exit 2; }
"$BROWSE" wait --networkidle >/dev/null 2>&1
if [ "${AXE_COARSE:-0}" = 1 ]; then "$BROWSE" js "document.documentElement.classList.add('force-coarse')" >/dev/null 2>&1; fi
"$BROWSE" eval "$HERE/axe/axe.min.js" >/dev/null 2>&1
OUT="$("$BROWSE" js "axe.run(document, {resultTypes: ['violations']}).then(r => JSON.stringify(r.violations.map(v => ({impact: v.impact, id: v.id, n: v.nodes.length, target: ((v.nodes[0] && v.nodes[0].target) || []).join(' ')}))))" 2>/dev/null)"
case "$OUT" in
  \[*) ;;
  *) echo "axe-run: axe did not return a result for $URL; nothing was measured" >&2; exit 2 ;;
esac
printf '%s' "$OUT" | python3 -c '
import json, sys
rows = json.loads(sys.stdin.read())
order = ["critical", "serious", "moderate", "minor"]
bad = 0
for r in sorted(rows, key=lambda r: order.index(r["impact"]) if r["impact"] in order else 9):
    print("%-9s %s %s (%d nodes in all)" % (r["impact"], r["id"], r["target"], r["n"]))
    bad += r["impact"] in ("serious", "critical")
print("axe: %d violation rule(s), %d serious or critical" % (len(rows), bad))
sys.exit(1 if bad else 0)
'
