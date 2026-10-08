#!/bin/sh
# swap-probe.sh: how long is the board down during a container swap? (#39). POSIX sh, needs only curl.
#
#   scripts/swap-probe.sh [seconds] [url]        default 300 s against http://127.0.0.1:${CCBOARD_PORT:-8000}/healthz
#
# Every PROBE_INTERVAL seconds (default 0.5) it prints one line: the time (date +%s.%N, whole seconds where %N is not supported),
# the HTTP code (000 = no answer within 1 s) and curl's time for the request. At the end it prints the longest gap between two
# 200 answers, which is the outage a person at the board saw. Run it on the box in a second shell just before a push you were
# going to make anyway, and join its output with `docker events --filter container=ccboard` and Watchtower's log for the same
# window (README, "Run the board as a container"). It only reads: one GET per tick, nothing is written.
#
# PROBE_COUNT=n stops after n requests instead of after `seconds` (the tests use it).
set -u

SECS=${1:-300}
URL=${2:-http://127.0.0.1:${CCBOARD_PORT:-8000}/healthz}
INTERVAL=${PROBE_INTERVAL:-0.5}
COUNT=${PROBE_COUNT:-}

now() {
  t=$(date +%s.%N 2>/dev/null)
  case "$t" in *N|'') t=$(date +%s);; esac
  printf '%s' "$t"
}

start=$(now)
n=0
while :; do
  if [ -n "$COUNT" ]; then
    [ "$n" -lt "$COUNT" ] || break
  else
    awk -v s="$start" -v e="$(now)" -v d="$SECS" 'BEGIN { exit !(e - s >= d) }' && break
  fi
  t=$(now)
  out=$(curl -m 1 -s -o /dev/null -w '%{http_code} %{time_total}' "$URL" 2>/dev/null)
  set -- $out
  code=${1:-000}
  took=${2:-0}
  printf '%s %s %s\n' "$t" "$code" "$took"
  n=$((n + 1))
  sleep "$INTERVAL"
done | awk '
  { print; fflush() }
  $2 == "200" {
    if (have) { gap = $1 - last; if (gap > best) { best = gap; from = last; to = $1 } }
    last = $1; have = 1; ok++
  }
  $2 != "200" { bad++ }
  END {
    printf "samples %d, 200 answers %d, other %d\n", NR, ok, bad
    if (best > 0) printf "longest gap between two 200 answers: %.1f s (from %s to %s)\n", best, from, to
    else print "longest gap between two 200 answers: none (fewer than two 200 answers, or no gap)"
  }'
