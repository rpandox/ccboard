#!/bin/sh
# ccboard container entrypoint (v0.5.1-docker). POSIX sh (dash on Ubuntu): no bashisms.
#
# The container shares the host's home, tmux socket and tailscale socket (deploy/docker-compose.yml), so before the
# board starts it brings the HOST-side pieces up to date with this image, idempotently:
#   1. sync bin/, scripts/ and tmux.conf into $CCBOARD_DATA_DIR/app/ (host-mounted): the hook scripts, the statusline,
#      the ttyd attach wrapper and the MCP shim that the HOST runs come from there, so a new image updates them with
#      no git checkout on the box;
#   2. re-merge the Claude Code hooks and statusline into ~/.claude/settings.json (scripts/claude_settings.py);
#   3. re-apply tmux.conf to the running tmux server (never starts or restarts it: it owns the sessions);
#   4. register the MCP server with Claude Code when it is missing, with a command the HOST can run.
# Steps 2-4 touch the host and are skipped when CCBOARD_SHADOW=1 (a side-by-side test run). Every step is best effort:
# a failure is a warning, never a reason to keep the board down. Then it execs uvicorn.
set -eu

APP_ROOT=${CCBOARD_APP_ROOT:-/opt/ccboard}   # where the image keeps the app; only tests point it elsewhere
HOST_PYTHON=${CCBOARD_HOST_PYTHON:-/usr/bin/python3}   # claude spawns the MCP shim on the host, so a host-valid python

log()  { printf 'ccboard-entrypoint: %s\n' "$*"; }
warn() { printf 'ccboard-entrypoint: warning: %s\n' "$*" >&2; }

if [ "$(id -u)" -eq 0 ]; then
  printf 'ccboard-entrypoint: refusing to run as root (uid 0); the board runs as the host user (compose: user "1000:1000")\n' >&2
  exit 1
fi

if [ -z "${HOME:-}" ]; then
  HOME=$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6 || true)
  [ -n "$HOME" ] || HOME=/home/rpandox
fi
: "${CCBOARD_PORT:=8000}"
: "${CCBOARD_DATA_DIR:=$HOME/.local/share/ccboard}"
: "${CCBOARD_TMUX_SOCKET:=ccboard}"
: "${CCBOARD_APPROVE_TIMEOUT:=90}"
export HOME CCBOARD_PORT CCBOARD_DATA_DIR

# The host's claude, codex and ccusage first, then the image's venv, then the system.
path_prepend() { case ":$PATH:" in *":$1:"*) ;; *) PATH="$1:$PATH";; esac; }
path_prepend "$APP_ROOT/.venv/bin"
path_prepend "$HOME/.local/bin"
export PATH

SHADOW=0
[ "${CCBOARD_SHADOW:-0}" = 1 ] && SHADOW=1
APP_DST=$CCBOARD_DATA_DIR/app

log "ccboard ${CCBOARD_IMAGE_VERSION:-dev}, runtime ${CCBOARD_RUNTIME:-docker}, uid $(id -u), data $CCBOARD_DATA_DIR, port $CCBOARD_PORT, shadow $SHADOW"

# ---------------------------------------------------------------- 1. sync the host-run files
# Every file is copied beside its twin and renamed over it, so a hook or ttyd attach that starts during the sync
# never meets a half-written script; files the image no longer has are pruned afterwards.
sync_dir() { # src dst
  drc=0
  mkdir -p "$2" || return 1
  for f in "$1"/*; do
    [ -e "$f" ] || continue
    b=${f##*/}
    case "$b" in __pycache__) continue;; esac
    if [ -d "$f" ]; then
      rm -rf "${2:?}/${b:?}" && cp -a "$f" "$2/$b" || drc=1
    else
      { cp -a "$f" "$2/.$b.new" && mv -f "$2/.$b.new" "$2/$b"; } || drc=1
    fi
  done
  for f in "$2"/*; do
    [ -e "$f" ] || continue
    [ -e "$1/${f##*/}" ] || rm -rf "$f" || drc=1
  done
  return "$drc"
}
sync_app() {
  arc=0
  mkdir -p "$APP_DST" || return 1
  sync_dir "$APP_ROOT/bin" "$APP_DST/bin" || arc=1
  sync_dir "$APP_ROOT/scripts" "$APP_DST/scripts" || arc=1
  { cp -a "$APP_ROOT/tmux.conf" "$APP_DST/.tmux.conf.new" && mv -f "$APP_DST/.tmux.conf.new" "$APP_DST/tmux.conf"; } || arc=1
  chmod 755 "$APP_DST"/bin/* || arc=1
  return "$arc"
}
if sync_app; then
  log "synced bin, scripts and tmux.conf into $APP_DST"
else
  warn "could not fully sync bin, scripts and tmux.conf into $APP_DST; the host keeps the copies it already has"
fi

# ---------------------------------------------------------------- 2-4. host mutations
run_timeout() { # seconds cmd...  (DISABLE_AUTOUPDATER: this claude is the host's, let its own sessions update it)
  t=$1; shift
  if command -v timeout >/dev/null 2>&1; then timeout "$t" env DISABLE_AUTOUPDATER=1 "$@"; else env DISABLE_AUTOUPDATER=1 "$@"; fi
}
host_hooks() {
  if [ "${CCBOARD_REMOTE_APPROVE:-1}" = 0 ]; then
    "$HOST_PYTHON" "$APP_DST/scripts/claude_settings.py" install --app-dir "$APP_DST" --no-remote-approve --approve-timeout "$CCBOARD_APPROVE_TIMEOUT"
  else
    "$HOST_PYTHON" "$APP_DST/scripts/claude_settings.py" install --app-dir "$APP_DST" --approve-timeout "$CCBOARD_APPROVE_TIMEOUT"
  fi
}
host_code_server() {
  # cheap-to-open code-server: user settings merged (keys you set win) and, when the config is ccboard's, the flags that skip
  # telemetry, update checks and the workspace-trust prompt (code-server reads config.yaml at start: the next restart or reboot)
  "$HOST_PYTHON" "$APP_DST/scripts/code_server_settings.py" install || log "code-server settings not merged"
  cfg="$HOME/.config/code-server/config.yaml"
  if [ -f "$cfg" ] && [ "$(head -1 "$cfg")" = "# managed by ccboard" ] && ! grep -q '^disable-workspace-trust:' "$cfg"; then
    printf 'disable-telemetry: true\ndisable-update-check: true\ndisable-workspace-trust: true\ndisable-getting-started-override: true\n' >> "$cfg" \
      && log "code-server config: added the no-telemetry / no-update-check / no-workspace-trust flags (applied at its next restart)"
  fi
}
host_tmux() {
  if out=$(tmux -L "$CCBOARD_TMUX_SOCKET" source-file "$APP_DST/tmux.conf" 2>&1); then
    log "tmux.conf applied to the running tmux server (socket $CCBOARD_TMUX_SOCKET)"
  else
    log "tmux.conf not applied: ${out:-tmux failed}. The server reads it when it starts."
  fi
}
host_mcp() {
  if ! command -v claude >/dev/null 2>&1; then
    log "claude is not on PATH ($HOME/.local/bin first): MCP registration skipped"
  elif run_timeout 30 claude mcp get ccboard >/dev/null 2>&1; then
    log "MCP server 'ccboard' is already registered"
  elif run_timeout 60 claude mcp add --scope user ccboard -- "$HOST_PYTHON" "$APP_DST/scripts/ccboard_mcp.py" >/dev/null 2>&1; then
    log "registered the 'ccboard' MCP server with Claude Code"
  else
    return 1
  fi
}
if [ "$SHADOW" = 1 ]; then
  log "CCBOARD_SHADOW=1: Claude hooks, tmux server and MCP registration are left alone"
else
  host_hooks || warn "could not merge the Claude hooks and statusline into settings.json; hooks keep pointing where they pointed"
  host_code_server || true
  host_tmux
  host_mcp || warn "claude mcp add failed; register by hand: claude mcp add --scope user ccboard -- $HOST_PYTHON $APP_DST/scripts/ccboard_mcp.py"
fi

# ---------------------------------------------------------------- the board
cd "$APP_ROOT"
exec "$APP_ROOT/.venv/bin/uvicorn" app.main:app --host 127.0.0.1 --port "$CCBOARD_PORT" --timeout-graceful-shutdown 2
