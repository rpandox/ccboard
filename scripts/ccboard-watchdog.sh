#!/bin/sh
# ccboard-watchdog: keep the board container running on a box that may be too busy for Watchtower's restart to take.
# Cron runs it every 2 minutes (install.sh, docker mode: a line in the user's crontab, no sudo; the user is in the docker group).
#   container not running (created / exited / dead) -> docker start
#   unhealthy on two consecutive runs               -> docker restart
# Nothing to do when the container does not exist (systemd mode) or docker is missing. Log: $CCBOARD_DATA_DIR/watchdog.log.
set -u
NAME=${CCBOARD_CONTAINER:-ccboard}
STATE_DIR=${CCBOARD_DATA_DIR:-$HOME/.local/share/ccboard}
LOG=$STATE_DIR/watchdog.log
FLAG=$STATE_DIR/.watchdog-unhealthy
log() { mkdir -p "$STATE_DIR" 2>/dev/null; printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >> "$LOG"; }
command -v docker >/dev/null 2>&1 || exit 0
status=$(docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$NAME" 2>/dev/null) || exit 0
set -- $status
run=${1:-}; health=${2:-none}
mkdir -p "$STATE_DIR" 2>/dev/null
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG" | tr -d ' ')" -gt 1048576 ]; then tail -c 262144 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"; fi
case "$run" in
  running)
    if [ "$health" = unhealthy ]; then
      if [ -f "$FLAG" ]; then
        rm -f "$FLAG"; log "unhealthy on two runs: docker restart $NAME"
        docker restart -t 15 "$NAME" >/dev/null 2>&1 || log "docker restart $NAME failed"
      else
        touch "$FLAG"
      fi
    else
      rm -f "$FLAG"
    fi ;;
  created|exited|dead)
    log "container is $run: docker start $NAME"
    docker start "$NAME" >/dev/null 2>&1 || log "docker start $NAME failed" ;;
  *) ;;   # restarting, paused, removing: docker or the person is on it
esac
exit 0
