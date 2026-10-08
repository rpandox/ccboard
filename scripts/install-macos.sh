#!/bin/bash
# ccboard installer for macOS (issue #117). Run it as ./install.sh (which hands over to this script on a Mac) or directly.
# Idempotent: a rerun changes nothing it should not. Runs under the stock bash 3.2 of macOS and needs no GNU tool; it never runs sudo.
#
#   ./install.sh                       the board, tmux and ttyd as launchd jobs of your login session (the default, "gui" domain)
#   ./install.sh --runtime launchd     the same; an alias for CCBOARD_RUNTIME=launchd. docker and systemd are refused on a Mac.
#   CCBOARD_HTTPS_PORT=8443 CODE_HTTPS_PORT=10000 ./install.sh
#
# Every setting is an environment variable; previous values are kept in <data dir>/env (mode 0600, read by the board itself because launchd
# has no EnvironmentFile). Settings are the keys `python -m app.config keys` lists; the README has the table. Installer-only switches,
# none of them remembered:
#   CCBOARD_LAUNCHD_DOMAIN=user    the experimental Background-session layout (jobs in user/<uid> instead of gui/<uid>)
#   CCBOARD_KEEP_AWAKE=1           also run dev.ccboard.awake (caffeinate: no idle sleep)
#   CCBOARD_MEM_SERVICE=1          also run dev.ccboard.mem (the claude-mem worker from a clean environment)
#   CCBOARD_BACKUP=0               no nightly backup job (otherwise dev.ccboard.backup runs at CCBOARD_BACKUP_ONCALENDAR)
#   CCBOARD_MACOS_OPTIONAL=...     optional Homebrew tools, default code-server,restic,bun; none = skip them
#   CCBOARD_MACOS_INSTALL_TOOLS=1  install missing Homebrew tools without asking (0 = never; unset = ask, and refuse without a terminal)
#   CCBOARD_TMUX_TMPDIR, CCBOARD_TMUX_SOCKET   where tmux puts its socket (default /tmp and ccboard); the same in every job
#   CCBOARD_REPLACE_SERVE=1        replace another tailscale serve handler on one of our ports (never 443 unless it is the board's)
#   CCBOARD_REPLACE_CODE_SERVER_CONFIG=1   back up and replace a code-server config.yaml that ccboard did not write
# Test seams (the tests run this script against fakes on a temporary PATH and a temporary HOME):
#   CCBOARD_MACOS_VENV_READY=1     $APP_DIR/.venv is already built: only check that .venv/bin/python runs; never create it, never run pip
#   CCBOARD_MACOS_HEALTH_TRIES=N   seconds to wait for /healthz after the jobs start (default 30)
# Every external tool is found on PATH: uname, sw_vers, id, brew, launchctl, tailscale, tmux, ttyd, curl, claude, codex, python3.
#
# What it never does: sudo; `tailscale serve reset`; touch port 443 unless it is the board's own; boot out or kickstart dev.ccboard.tmux once
# it is loaded (that job owns every session); write outside your home folder and the Homebrew prefix.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# The container runtime cannot work on a Mac. The text is install.sh's docker_host_refusal for "macOS", word for word
# (tests/test_install_macos.py runs both and compares them).
docker_host_refusal() {
  die "CCBOARD_RUNTIME=docker is not supported with macOS: the board's container reaches the host's tmux socket, tailscale socket, process list and claude and codex programs, and none of those cross the virtual machine that Docker Desktop runs containers in. Instead: use the launchd runtime"
}

# ---------------------------------------------------------------- arguments and runtime
while [ "$#" -gt 0 ]; do
  case "$1" in
    --runtime) [ "$#" -ge 2 ] || die "--runtime wants a value (launchd)"; CCBOARD_RUNTIME=$2; shift 2;;
    --runtime=*) CCBOARD_RUNTIME=${1#--runtime=}; shift;;
    -h|--help) sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0;;
    *) die "unknown argument '$1' (this installer takes --runtime launchd only; settings are environment variables)";;
  esac
done
CCBOARD_RUNTIME=${CCBOARD_RUNTIME:-launchd}
case "$CCBOARD_RUNTIME" in
  launchd) ;;
  docker) docker_host_refusal;;
  systemd) die "CCBOARD_RUNTIME=systemd is not supported on macOS: systemd is Linux only. Use the launchd runtime (the default here)";;
  *) die "CCBOARD_RUNTIME must be launchd on a Mac (got '$CCBOARD_RUNTIME')";;
esac

# ---------------------------------------------------------------- guards
[ "$(id -u)" -ne 0 ] || die "run as the user who will own the sessions, not as root (nothing here needs root)"
[ "$(uname -s)" = Darwin ] || die "this installer is for macOS (found $(uname -s)); on Ubuntu run install.sh"
MACOS_VERSION=$(sw_vers -productVersion 2>/dev/null || true)
case "${MACOS_VERSION%%.*}" in ''|*[!0-9]*) die "could not read the macOS version from sw_vers (got '$MACOS_VERSION')";; esac
[ "${MACOS_VERSION%%.*}" -ge 13 ] || die "macOS 13 or newer is required (found $MACOS_VERSION)"
ARCH=$(uname -m)
BREW=$(command -v brew 2>/dev/null || true)
if [ -z "$BREW" ]; then
  for c in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    if [ -x "$c" ]; then BREW=$c; break; fi
  done
fi
[ -n "$BREW" ] || die "Homebrew is not installed (https://brew.sh); the tools ccboard needs come from it"
BREW_PREFIX=$("$BREW" --prefix 2>/dev/null || true)
[ -n "$BREW_PREFIX" ] || die "brew --prefix printed nothing; is Homebrew working? Try: brew doctor"
PY3=$(command -v python3 2>/dev/null || true)
[ -n "$PY3" ] || die "python3 is required (brew install python@3.12)"
UID_N=$(id -u)
HOME_DIR=$HOME
[ -n "$HOME_DIR" ] && [ -d "$HOME_DIR" ] || die "HOME is not a directory"
LA_DIR=$HOME_DIR/Library/LaunchAgents
LOG_DIR=$HOME_DIR/Library/Logs/ccboard
cd "$APP_DIR"

TMPD=$(mktemp -d)
trap 'rm -rf "$TMPD"' EXIT

# What a previous run decided and the plists still say, so a rerun without the same switches finds its own settings file and its own domain
# (the settings file lives in the data dir, and Linux's fixed /etc/ccboard/env has no such problem).
prev_board() { # key -> EnvironmentVariables.CCBOARD_ENV_FILE ("env") or LimitLoadToSessionType ("session") of the installed board plist, or nothing
  [ -f "$LA_DIR/dev.ccboard.board.plist" ] || return 0
  "$PY3" -c '
import plistlib, sys
try:
    with open(sys.argv[1], "rb") as f:
        d = plistlib.load(f)
except Exception:
    sys.exit(0)
print(d.get("LimitLoadToSessionType", "") if sys.argv[2] == "session" else (d.get("EnvironmentVariables") or {}).get("CCBOARD_ENV_FILE", ""))
' "$LA_DIR/dev.ccboard.board.plist" "$1" 2>/dev/null || true
}
ENV_DIR=${CCBOARD_DATA_DIR:-}
if [ -z "$ENV_DIR" ]; then
  prev_env=$(prev_board env)
  case "$prev_env" in /*/env) ENV_DIR=${prev_env%/env}; note "using the settings folder of the previous install: $ENV_DIR";; esac
fi
ENV_DIR=${ENV_DIR:-$HOME_DIR/.local/share/ccboard}
case "$ENV_DIR" in /*) ;; *) die "CCBOARD_DATA_DIR must be an absolute path (got '$ENV_DIR')";; esac
ENV_FILE=$ENV_DIR/env
# Run a command with the environment the app's own modules want, in a clean one (env -i): a setting you passed with a typo must reach this
# script's own checks below, not crash an import, and importing them neither reads the settings file nor creates the real data folder.
pyapp() {
  env -i PATH="$PATH" HOME="$HOME_DIR" CCBOARD_DATA_DIR="$TMPD/data" CCBOARD_ENV_FILE= CCBOARD_ALLOWED_USERS=installer CCBOARD_RUNTIME=launchd \
    CCBOARD_REPLACE_SERVE="${CCBOARD_REPLACE_SERVE:-}" "$@"
}

# ---------------------------------------------------------------- config
# Explicit environment beats the previous settings file, which beats the defaults (the same order as install.sh).
KEYS=$(cd "$APP_DIR" && pyapp "$PY3" -m app.config keys 2>"$TMPD/keys.err") || KEYS=
[ -n "$KEYS" ] || die "could not list the settings: $PY3 -m app.config keys failed in $APP_DIR: $(tail -n 3 "$TMPD/keys.err")"
is_key() { case "
$KEYS
" in *"
$1
"*) return 0;; esac; return 1; }
for k in $KEYS; do
  case "$k" in ''|[0-9]*|*[!A-Za-z0-9_]*) die "unexpected settings key '$k'";; esac
  eval "CALLER_$k=\${$k:-}; $k=\${$k:-}"          # $k is a checked identifier; this binds every key (set -u) and remembers what the caller gave
done
EXTRA_ENV=""
if [ -L "$ENV_FILE" ]; then die "$ENV_FILE is a symbolic link; the board refuses such a settings file. Remove it and rerun"; fi
if [ -f "$ENV_FILE" ]; then
  # Never sourced: values are data. Keys of this list are read; other KEY=value lines you added by hand are kept as they are.
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in *=*) ;; *) continue;; esac
    k=${line%%=*}; v=${line#*=}
    case "$k" in ''|[0-9]*|*[!A-Za-z0-9_]*) continue;; esac
    if is_key "$k"; then printf -v "$k" '%s' "$v"; else EXTRA_ENV="$EXTRA_ENV$line
"; fi
  done < "$ENV_FILE"
fi
for k in $KEYS; do
  eval "c=\${CALLER_$k}"
  if [ -n "$c" ]; then printf -v "$k" '%s' "$c"; fi
done
CCBOARD_DATA_DIR=$ENV_DIR                       # the settings file lives in the data dir, so that is where the data dir is
CCBOARD_RUNTIME=launchd
: "${PROJECTS_DIR:=$HOME_DIR/projects}"
: "${CCBOARD_PORT:=8000}"
: "${TTYD_PORT:=7681}"
: "${CODE_SERVER_PORT:=8080}"
: "${CCBOARD_HTTPS_PORT:=443}"
: "${CODE_HTTPS_PORT:=8443}"
: "${CODE_SERVER_VERSION:=4.139.1}"
: "${NTFY_PORT:=2586}"
: "${NTFY_HTTPS_PORT:=8444}"
: "${NTFY_TOPIC:=ccboard}"
: "${CCBOARD_APPROVE_TIMEOUT:=90}"
: "${PREVIEW_HTTPS_BASE:=9100}"
: "${CCBOARD_NODE_NAME:=$(hostname -s)}"
if [ -z "$CCBOARD_HUB_TOKEN" ]; then CCBOARD_HUB_TOKEN=$("$PY3" -c 'import secrets; print(secrets.token_hex(24))'); note "generated CCBOARD_HUB_TOKEN (copy it to the other boxes to form a fleet)"; fi
: "${CCBOARD_BACKUP_PUSH:=1}"
: "${CCBOARD_BACKUP_ONCALENDAR:=*-*-* 02:30:00}"
: "${CCBOARD_AUTO_CONTINUE:=1}"
: "${CCBOARD_CLAUDE_MEM:=1}"
[ "$CCBOARD_MEM_HTTPS_PORT" != off ] || CCBOARD_MEM_HTTPS_PORT=
: "${CCBOARD_MEM_SERVICE:=0}"
: "${CCBOARD_CODEX_HOOK_TRUST:=review}"
: "${CCBOARD_MCP_REMOTE:=0}"
: "${CCBOARD_AUTOCLOSE_GRACE:=45}"
: "${CCBOARD_CODEX_HOOKS_ASYNC:=1}"

[[ "$CCBOARD_NODE_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,40}$ ]] || die "CCBOARD_NODE_NAME must be letters, digits, - or _"
[[ "$PREVIEW_HTTPS_BASE" =~ ^[0-9]{1,5}$ ]] || die "PREVIEW_HTTPS_BASE must be a port number"
[[ "$CCBOARD_APPROVE_TIMEOUT" =~ ^[0-9]{1,4}$ ]] || die "CCBOARD_APPROVE_TIMEOUT must be seconds"
for k in CCBOARD_PORT TTYD_PORT CODE_SERVER_PORT CCBOARD_HTTPS_PORT CODE_HTTPS_PORT NTFY_PORT NTFY_HTTPS_PORT; do
  eval "v=\${$k}"
  [[ "$v" =~ ^[0-9]{1,5}$ ]] || die "$k must be a port number (got '$v')"
done
[ "$CCBOARD_HTTPS_PORT" != "$CODE_HTTPS_PORT" ] || die "CCBOARD_HTTPS_PORT and CODE_HTTPS_PORT must differ"
[ "$CODE_HTTPS_PORT" != 443 ] || die "CODE_HTTPS_PORT must not be 443: another service may use it, and only the board's own HTTPS port may be 443"
[ "$CCBOARD_PORT" != "$TTYD_PORT" ] && [ "$CCBOARD_PORT" != "$CODE_SERVER_PORT" ] && [ "$TTYD_PORT" != "$CODE_SERVER_PORT" ] \
  || die "CCBOARD_PORT, TTYD_PORT and CODE_SERVER_PORT must all differ"
[[ "$NTFY_TOPIC" =~ ^[A-Za-z0-9_-]{1,64}$ ]] || die "NTFY_TOPIC must be letters, digits, - or _"
[[ "$PROJECTS_DIR" = /* ]] || die "PROJECTS_DIR must be an absolute path"
case "$CCBOARD_CLAUDE_MEM" in 0|1) ;; *) die "CCBOARD_CLAUDE_MEM must be 0 or 1 (got '$CCBOARD_CLAUDE_MEM')";; esac
case "$CCBOARD_MEM_SERVICE" in 0|1) ;; *) die "CCBOARD_MEM_SERVICE must be 0 or 1 (got '$CCBOARD_MEM_SERVICE')";; esac
case "${CCBOARD_KEEP_AWAKE:-0}" in 0|1) ;; *) die "CCBOARD_KEEP_AWAKE must be 0 or 1 (got '${CCBOARD_KEEP_AWAKE:-}')";; esac
case "${CCBOARD_BACKUP:-1}" in 0|1) ;; *) die "CCBOARD_BACKUP must be 0 or 1 (got '${CCBOARD_BACKUP:-}')";; esac
[ -z "$CCBOARD_MEM_PORT" ] || [[ "$CCBOARD_MEM_PORT" =~ ^[0-9]{1,5}$ ]] || die "CCBOARD_MEM_PORT must be empty or a port number (got '$CCBOARD_MEM_PORT')"
[[ "$CCBOARD_AUTOCLOSE_GRACE" =~ ^[0-9]+(\.[0-9]+)?$ ]] || die "CCBOARD_AUTOCLOSE_GRACE must be a number of seconds (got '$CCBOARD_AUTOCLOSE_GRACE')"
case "$CCBOARD_CODEX_HOOKS_ASYNC" in 0|1) ;; *) die "CCBOARD_CODEX_HOOKS_ASYNC must be 0 or 1 (got '$CCBOARD_CODEX_HOOKS_ASYNC')";; esac
[[ -z "$CCBOARD_SUBAGENT_MODEL" || "$CCBOARD_SUBAGENT_MODEL" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}(\[1m\])?$ ]] || die "CCBOARD_SUBAGENT_MODEL must be empty, inherit, haiku, sonnet, opus or a full model id (got '$CCBOARD_SUBAGENT_MODEL')"
[[ -z "$CCBOARD_HEADLESS_FABLE_CAP" || "$CCBOARD_HEADLESS_FABLE_CAP" =~ ^[0-9]+(\.[0-9]+)?$ ]] || die "CCBOARD_HEADLESS_FABLE_CAP must be a number of dollars (got '$CCBOARD_HEADLESS_FABLE_CAP')"
case "$CCBOARD_CLAUDE_ULTRACODE_FLAG" in ''|0|1) ;; *) die "CCBOARD_CLAUDE_ULTRACODE_FLAG must be empty, 0 or 1 (got '$CCBOARD_CLAUDE_ULTRACODE_FLAG')";; esac
case "$CCBOARD_CODEX_HOOK_TRUST" in review|bypass) ;; *) die "CCBOARD_CODEX_HOOK_TRUST must be review or bypass (got '$CCBOARD_CODEX_HOOK_TRUST')";; esac
[ -z "$CCBOARD_PRICE_TABLE" ] || [[ "$CCBOARD_PRICE_TABLE" = /* ]] || die "CCBOARD_PRICE_TABLE must be empty or an absolute path (got '$CCBOARD_PRICE_TABLE')"
[ -z "$CODEX_HOME" ] || [[ "$CODEX_HOME" = /* ]] || die "CODEX_HOME must be empty or an absolute path (got '$CODEX_HOME')"
[ "$CCBOARD_CODEX_HOOK_TRUST" != bypass ] || warn "CCBOARD_CODEX_HOOK_TRUST=bypass: every Codex session the board starts skips the hook review, including a .codex/hooks.json inside a repository you cloned (the same risk class as bypassPermissions)"
CCBOARD_ALLOWED_USERS=$(printf '%s' "$CCBOARD_ALLOWED_USERS" | tr -d '[:space:]')
for k in $KEYS; do   # a newline would split a KEY=value line of the settings file; the plist renderer refuses control characters too
  eval "v=\${$k}"
  case "$v" in *[[:cntrl:]]*) die "$k must not contain a control character";; esac
done
[ -z "$CODEX_HOME" ] || export CODEX_HOME          # codex_hooks.py and the codex CLI below read it
export CCBOARD_CODEX_HOOKS_ASYNC                   # codex_hooks.py reads it: the hooks written now match what the board is remembered to use
for v in ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN CLAUDE_CODE_OAUTH_TOKEN; do
  eval "c=\${$v:-}"
  [ -z "$c" ] || warn "$v is set in your environment; it outranks the Claude login. It is NOT written to $ENV_FILE."
done
[ -z "$CCBOARD_MEM_HTTPS_PORT" ] || warn "CCBOARD_MEM_HTTPS_PORT is remembered, but the macOS installer does not map the claude-mem viewer on the tailnet yet"

# The backup time, before anything changes: scripts/macos_tools.py backup-schedule accepts `*-*-* HH:MM[:SS]` and `HH:MM`.
if ! BACKUP_TIME=$("$PY3" "$APP_DIR/scripts/macos_tools.py" backup-schedule "$CCBOARD_BACKUP_ONCALENDAR" 2>"$TMPD/backup-schedule.err"); then
  die "CCBOARD_BACKUP_ONCALENDAR: $(cat "$TMPD/backup-schedule.err")"
fi
BACKUP_HOUR=${BACKUP_TIME%% *}
BACKUP_MINUTE=${BACKUP_TIME##* }

# The launchd domain.
DOMAIN_KIND=$(printf '%s' "${CCBOARD_LAUNCHD_DOMAIN:-}" | tr '[:upper:]' '[:lower:]')
if [ -z "$DOMAIN_KIND" ] && [ "$(prev_board session)" = Background ]; then DOMAIN_KIND=user; note "keeping the user domain the previous install chose"; fi
DOMAIN_KIND=${DOMAIN_KIND:-gui}
SESSION_ARGS=""
case "$DOMAIN_KIND" in
  gui|'') DOMAIN="gui/$UID_N";;
  user) DOMAIN="user/$UID_N"; SESSION_ARGS="--session-type Background"
        warn "CCBOARD_LAUNCHD_DOMAIN=user is experimental: whether a Background job outlives a logout, and whether it can reach your Keychain, is not verified";;
  *) die "CCBOARD_LAUNCHD_DOMAIN must be gui or user (got '$CCBOARD_LAUNCHD_DOMAIN')";;
esac
TMUX_TMPDIR_V=${CCBOARD_TMUX_TMPDIR:-/tmp}
TMUX_SOCKET_V=${CCBOARD_TMUX_SOCKET:-ccboard}
case "$TMUX_TMPDIR_V" in /*) ;; *) die "CCBOARD_TMUX_TMPDIR must be an absolute path (got '$TMUX_TMPDIR_V')";; esac
case "$TMUX_SOCKET_V" in ''|*/*) die "CCBOARD_TMUX_SOCKET must be a socket name, not a path (got '$TMUX_SOCKET_V')";; esac

# Which optional tools are wanted.
OPTIONAL=$(printf '%s' "${CCBOARD_MACOS_OPTIONAL:-code-server,restic,bun}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')
case "$OPTIONAL" in none) OPTIONAL="";; esac
want_optional() { case ",$OPTIONAL," in *",$1,"*) return 0;; esac; return 1; }
if [ -n "$OPTIONAL" ] && ! "$PY3" "$APP_DIR/scripts/macos_tools.py" optional-bundle --pick "$OPTIONAL" >"$TMPD/Brewfile.optional" 2>"$TMPD/optional.err"; then
  die "CCBOARD_MACOS_OPTIONAL: $(cat "$TMPD/optional.err")"
fi

# ---------------------------------------------------------------- tailscale (before touching anything)
log "tailscale"
ts_get() { "$PY3" -c 'import json,sys
d=json.loads(sys.argv[1]); v=d.get(sys.argv[2])
print("" if v is None else v)' "$TS_JSON" "$1"; }
TS_JSON=$(pyapp "$PY3" "$APP_DIR/scripts/tailscale_serve.py" status 2>/dev/null) || true
[ -n "$TS_JSON" ] || die "scripts/tailscale_serve.py status printed nothing; is tailscale installed? https://tailscale.com/download"
if [ "$(ts_get ok)" != True ]; then die "$(ts_get reason)"; fi
TS_FQDN=$(ts_get fqdn)
TS_LOGIN=$(ts_get login)
if [ -z "$CCBOARD_ALLOWED_USERS" ]; then
  [ -n "$TS_LOGIN" ] || die "could not derive your tailnet login; set CCBOARD_ALLOWED_USERS=you@provider"
  CCBOARD_ALLOWED_USERS=$TS_LOGIN
fi
# shellcheck disable=SC2034    # one of the settings KEYS: it is written to the settings file by name below
CCBOARD_PUBLIC_URL="https://$TS_FQDN:$CCBOARD_HTTPS_PORT"
note "tailnet node $TS_FQDN ($(ts_get variant) variant), allowed users: $CCBOARD_ALLOWED_USERS"

ts_run() { # verb https path target
  pyapp "$PY3" "$APP_DIR/scripts/tailscale_serve.py" "$1" --https "$2" --path "$3" --target "$4" --board-https "$CCBOARD_HTTPS_PORT" --fqdn "$TS_FQDN"
}
serve_pre() { # https path target: refuse before any change what apply would refuse later
  local st rc=0
  st=$(ts_run check "$1" "$2" "$3" 2>/dev/null) || rc=$?
  case "$st" in
    ours|missing|stale) ;;
    funnel|foreign:*)
      [ "$1" != 443 ] || die "tailscale serve port 443 carries something else ($st); the installer never replaces it, even with CCBOARD_REPLACE_SERVE=1. Pick another port (e.g. CCBOARD_HTTPS_PORT=8443)"
      [ "${CCBOARD_REPLACE_SERVE:-}" = 1 ] || die "tailscale serve port $1 is already used by something else ($st). Pick another port (e.g. CCBOARD_HTTPS_PORT=8443 CODE_HTTPS_PORT=10000) or rerun with CCBOARD_REPLACE_SERVE=1 to replace it.";;
    *) die "could not read the tailscale serve configuration (state '${st:-none}', exit $rc); is Tailscale running, signed in and allowed to run serve?";;
  esac
}
SERVE_BOARD="http://127.0.0.1:$CCBOARD_PORT"
SERVE_TTYD="http://127.0.0.1:$TTYD_PORT"
SERVE_CODE="http://127.0.0.1:$CODE_SERVER_PORT"
serve_pre "$CCBOARD_HTTPS_PORT" / "$SERVE_BOARD"
serve_pre "$CCBOARD_HTTPS_PORT" /tty "$SERVE_TTYD"
if want_optional code-server; then serve_pre "$CODE_HTTPS_PORT" / "$SERVE_CODE"; fi

# ---------------------------------------------------------------- tools (Homebrew)
log "tools (Homebrew)"
brew_names() { sed -n 's/^brew "\([^"]*\)".*/\1/p' "$1"; }
missing_formulae() { # Brewfile -> the formulae that are not installed, one per line
  local f
  for f in $(brew_names "$1"); do
    "$BREW" list --formula "${f##*/}" >/dev/null 2>&1 || printf '%s\n' "$f"
  done
}
NEED_REQ=""; NEED_OPT=""
if ! "$BREW" bundle check --file="$APP_DIR/scripts/macos/Brewfile" >/dev/null 2>&1; then NEED_REQ=$(missing_formulae "$APP_DIR/scripts/macos/Brewfile" | tr '\n' ' '); fi
if [ -n "$OPTIONAL" ] && ! "$BREW" bundle check --file="$TMPD/Brewfile.optional" >/dev/null 2>&1; then NEED_OPT=$(missing_formulae "$TMPD/Brewfile.optional" | tr '\n' ' '); fi
if [ -z "$NEED_REQ$NEED_OPT" ]; then
  note "all present"
else
  [ -z "$NEED_REQ" ] || note "missing (required): $NEED_REQ"
  [ -z "$NEED_OPT" ] || note "missing (optional): $NEED_OPT"
  for f in $NEED_REQ; do   # say beforehand which ones compile (tmux and ttyd have no Intel bottle) instead of leaving a silent wait
    out=$("$BREW" info --json=v2 "${f##*/}" 2>/dev/null | "$PY3" "$APP_DIR/scripts/macos_tools.py" bottle --formula "${f##*/}" --macos "$MACOS_VERSION" --arch "$ARCH" --explain 2>&1 || true)
    printf '%s\n' "$out" | while IFS= read -r l; do note "${f##*/}: $l"; done
  done
  go=0
  case "${CCBOARD_MACOS_INSTALL_TOOLS:-}" in
    1) go=1;;
    0) go=0;;
    '') if [ -t 0 ]; then
          printf '    Install the missing tools with Homebrew (brew bundle)? [y/N] '
          read -r ans || ans=n
          case "$ans" in y|Y|yes|YES) go=1;; esac
        fi;;
    *) die "CCBOARD_MACOS_INSTALL_TOOLS must be 1 or 0 (got '$CCBOARD_MACOS_INSTALL_TOOLS')";;
  esac
  if [ "$go" != 1 ]; then
    [ -z "$NEED_REQ" ] || die "tools are missing: $NEED_REQ. Install them with: brew bundle --file=$APP_DIR/scripts/macos/Brewfile   (or rerun with CCBOARD_MACOS_INSTALL_TOOLS=1)"
    warn "optional tools are missing ($NEED_OPT); continuing without them. Install later with: brew bundle --file=$APP_DIR/scripts/macos/Brewfile.optional"
  else
    if [ -n "$NEED_REQ" ]; then
      "$BREW" bundle --no-upgrade --file="$APP_DIR/scripts/macos/Brewfile" || die "brew bundle failed for the required tools; fix the error above and rerun"
    fi
    if [ -n "$NEED_OPT" ]; then
      "$BREW" bundle --no-upgrade --file="$TMPD/Brewfile.optional" || warn "brew bundle failed for some optional tools; the rest of the install goes on"
    fi
  fi
fi
find_tool() { # name -> its path, or nothing
  local p
  p=$(command -v "$1" 2>/dev/null || true)
  if [ -z "$p" ] && [ -x "$BREW_PREFIX/bin/$1" ]; then p="$BREW_PREFIX/bin/$1"; fi
  printf '%s' "$p"
}
TMUX_BIN=$(find_tool tmux)
TTYD_BIN=$(find_tool ttyd)
[ -n "$TMUX_BIN" ] || die "tmux is not installed: brew install tmux"
[ -n "$TTYD_BIN" ] || die "ttyd is not installed: brew install ttyd"
tmux_ver=$("$TMUX_BIN" -V 2>/dev/null | sed -n 's/^tmux \([0-9][0-9]*\)\.\([0-9][0-9]*\).*/\1 \2/p' || true)
if [ -n "$tmux_ver" ]; then
  if [ "${tmux_ver% *}" -lt 3 ] || { [ "${tmux_ver% *}" -eq 3 ] && [ "${tmux_ver#* }" -lt 2 ]; }; then die "tmux 3.2 or newer is needed (found $("$TMUX_BIN" -V)): brew upgrade tmux"; fi
fi
if ! ttyd_problems=$("$PY3" "$APP_DIR/scripts/macos_tools.py" ttyd --bin "$TTYD_BIN" 2>&1); then
  die "$ttyd_problems
  Fix: brew upgrade ttyd"
fi
note "tmux $TMUX_BIN, ttyd $TTYD_BIN"
CODE_SERVER_BIN=""
if want_optional code-server; then
  CODE_SERVER_BIN=$(find_tool code-server)
  [ -n "$CODE_SERVER_BIN" ] || warn "code-server is not installed: no editor job. brew install code-server, then rerun"
fi
RESTIC_BIN=$(find_tool restic)

# ---------------------------------------------------------------- python venv
log "python venv"
VENV_PY=$APP_DIR/.venv/bin/python
VENV_CHANGED=0
if [ "${CCBOARD_MACOS_VENV_READY:-0}" = 1 ]; then
  "$VENV_PY" -c 'pass' >/dev/null 2>&1 || die "CCBOARD_MACOS_VENV_READY=1 but $VENV_PY does not run"
  note "using the virtualenv that is already built"
else
  PYBIN=""
  for c in "$BREW_PREFIX/opt/python@3.12/bin/python3.12" "$BREW_PREFIX/bin/python3.12" "$(command -v python3.12 2>/dev/null || true)" "$PY3"; do
    if [ -n "$c" ] && [ -x "$c" ] && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then PYBIN=$c; break; fi
  done
  [ -n "$PYBIN" ] || die "Python 3.12 or newer is required: brew install python@3.12"
  req_sha=$("$PY3" -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$APP_DIR/requirements.txt")
  stamp="$req_sha $("$PYBIN" -V 2>&1)"
  if [ -d .venv ] && ! "$VENV_PY" -V >/dev/null 2>&1; then rm -rf .venv; fi
  if [ ! -f .venv/.ccboard-stamp ] || [ "$(cat .venv/.ccboard-stamp)" != "$stamp" ] || ! "$VENV_PY" -c 'import fastapi, uvicorn' 2>/dev/null; then
    [ -d .venv ] || "$PYBIN" -m venv .venv
    .venv/bin/pip install -q --disable-pip-version-check -r requirements.txt
    printf '%s' "$stamp" > .venv/.ccboard-stamp
    VENV_CHANGED=1
    note "installed requirements"
  else
    note "up to date"
  fi
fi

# ---------------------------------------------------------------- folders and the settings file
log "folders and settings file"
mkdir -p "$PROJECTS_DIR" "$ENV_DIR" "$LA_DIR" "$LOG_DIR"
chmod 0755 "$APP_DIR/bin/ccboard-attach"
env_body=""
for k in $KEYS; do eval "v=\${$k}"; env_body="$env_body$k=$v
"; done
env_body="$env_body$EXTRA_ENV"
ENV_CHANGED=0
(umask 077; printf '%s' "$env_body" > "$TMPD/env.new")
if [ ! -f "$ENV_FILE" ] || ! cmp -s "$TMPD/env.new" "$ENV_FILE"; then
  (umask 077; cp "$TMPD/env.new" "$ENV_FILE.tmp.$$" && mv "$ENV_FILE.tmp.$$" "$ENV_FILE")
  ENV_CHANGED=1; note "wrote $ENV_FILE"
else
  note "$ENV_FILE unchanged"
fi
chmod 0600 "$ENV_FILE"
if ! problem=$(pyapp "$VENV_PY" -m app.config check "$ENV_FILE"); then die "$problem"; fi
CS_CHANGED=0
if [ -n "$CODE_SERVER_BIN" ]; then
  cs_cfg=$HOME_DIR/.config/code-server/config.yaml
  "$PY3" "$APP_DIR/scripts/macos_tools.py" code-server-config --port "$CODE_SERVER_PORT" > "$TMPD/cs.yaml"
  if [ -f "$cs_cfg" ] && [ "$(head -1 "$cs_cfg")" != "# managed by ccboard" ]; then
    if [ "${CCBOARD_REPLACE_CODE_SERVER_CONFIG:-}" = 1 ]; then
      cp "$cs_cfg" "$cs_cfg.ccboard-bak-$(date +%s)"; warn "backed up your existing $cs_cfg"
    else
      die "$cs_cfg exists and is not managed by ccboard. ccboard needs 'auth: none' on 127.0.0.1:$CODE_SERVER_PORT. Rerun with CCBOARD_REPLACE_CODE_SERVER_CONFIG=1 to back it up and replace it."
    fi
  fi
  if [ ! -f "$cs_cfg" ] || ! cmp -s "$TMPD/cs.yaml" "$cs_cfg"; then
    mkdir -p "$(dirname "$cs_cfg")"; cp "$TMPD/cs.yaml" "$cs_cfg"; CS_CHANGED=1; note "wrote $cs_cfg"
  fi
  "$VENV_PY" "$APP_DIR/scripts/code_server_settings.py" install || warn "code-server settings not merged"
fi

# ---------------------------------------------------------------- backup (restic password file; CCBOARD_BACKUP=0 leaves the job off)
log "backup"
if [ "${CCBOARD_BACKUP:-1}" = 0 ]; then
  note "no backup job (CCBOARD_BACKUP=0)"
elif [ "$CCBOARD_RESTIC_REPO" = off ]; then
  note "restic off (CCBOARD_RESTIC_REPO=off); nightly backup branches on origin only"
else
  if [ -z "$RESTIC_BIN" ]; then warn "restic is not installed; backups will report 'restic is not installed'. brew install restic"; fi
  pwfile="${CCBOARD_RESTIC_PASSWORD_FILE:-$CCBOARD_DATA_DIR/restic-password}"
  if [ ! -s "$pwfile" ]; then
    mkdir -p "$(dirname "$pwfile")"
    (umask 077; "$PY3" -c 'import secrets; print(secrets.token_urlsafe(32))' > "$pwfile")
    note "generated $pwfile — copy it somewhere safe; without it the backups cannot be read"
  fi
  chmod 0600 "$pwfile"
  note "restic repo: ${CCBOARD_RESTIC_REPO:-$CCBOARD_DATA_DIR/restic (same disk: set CCBOARD_RESTIC_REPO to an sftp:/rclone:/s3: repo for real safety)}"
fi

# ---------------------------------------------------------------- launchd jobs: render, then write, then load
log "launchd jobs"
ALL_JOBS="tmux ttyd board awake mem code-server backup"
JOBS="tmux ttyd board"
if [ "${CCBOARD_KEEP_AWAKE:-0}" = 1 ]; then JOBS="$JOBS awake"; fi
if [ "$CCBOARD_CLAUDE_MEM" = 1 ] && [ "$CCBOARD_MEM_SERVICE" = 1 ]; then JOBS="$JOBS mem"; fi
if [ -n "$CODE_SERVER_BIN" ]; then JOBS="$JOBS code-server"; fi
if [ "${CCBOARD_BACKUP:-1}" != 0 ]; then JOBS="$JOBS backup"; fi
in_list() { case " $2 " in *" $1 "*) return 0;; esac; return 1; }

STAGE=$TMPD/stage; mkdir -p "$STAGE"
render_job() { # job: STAGE/<label>.plist, from launchd/<label>.plist.in
  local label="dev.ccboard.$1" extra=""
  if [ "$1" = backup ]; then extra="--set BACKUP_HOUR=$BACKUP_HOUR --set BACKUP_MINUTE=$BACKUP_MINUTE"; fi
  # shellcheck disable=SC2086
  "$PY3" "$APP_DIR/scripts/launchd_render.py" --template "$APP_DIR/launchd/$label.plist.in" --out "$STAGE/$label.plist" $SESSION_ARGS $extra \
    --set "HOME=$HOME_DIR" --set "APP_DIR=$APP_DIR" --set "SHELL=$LOGIN_SHELL" --set "CCBOARD_PORT=$CCBOARD_PORT" --set "TTYD_PORT=$TTYD_PORT" \
    --set "ENV_FILE=$ENV_FILE" --set "TMUX_BIN=$TMUX_BIN" --set "TTYD_BIN=$TTYD_BIN" --set "CODE_SERVER_BIN=$CODE_SERVER_BIN" \
    --set "BREW_PREFIX=$BREW_PREFIX" --set "LOG_DIR=$LOG_DIR" --set "TMUX_TMPDIR=$TMUX_TMPDIR_V" --set "TMUX_SOCKET=$TMUX_SOCKET_V" --set "UID=$UID_N"
}
LOGIN_SHELL=$("$PY3" -c 'import os, pwd; print(pwd.getpwuid(os.getuid()).pw_shell)' 2>/dev/null || true)
case "$LOGIN_SHELL" in /*) ;; *) LOGIN_SHELL=${SHELL:-/bin/zsh};; esac
case "$LOGIN_SHELL" in /*) ;; *) LOGIN_SHELL=/bin/zsh;; esac
for job in $JOBS; do render_job "$job" || die "could not render the $job job"; done

CHANGED_JOBS=""
for job in $JOBS; do
  label=dev.ccboard.$job
  if [ ! -f "$LA_DIR/$label.plist" ] || ! cmp -s "$STAGE/$label.plist" "$LA_DIR/$label.plist"; then
    cp "$STAGE/$label.plist" "$LA_DIR/$label.plist.tmp.$$" && mv "$LA_DIR/$label.plist.tmp.$$" "$LA_DIR/$label.plist"
    chmod 0644 "$LA_DIR/$label.plist"
    CHANGED_JOBS="$CHANGED_JOBS $job"; note "wrote $LA_DIR/$label.plist"
  fi
done

is_loaded() { launchctl print "$DOMAIN/dev.ccboard.$1" >/dev/null 2>&1; }
bootstrap_job() { # job: loads the plist; a bootout that is still finishing makes the first tries fail, so it retries
  local i=0 plist="$LA_DIR/dev.ccboard.$1.plist"
  while ! launchctl bootstrap "$DOMAIN" "$plist" 2>"$TMPD/bootstrap.err"; do
    i=$((i + 1))
    [ "$i" -lt 5 ] || die "launchctl bootstrap $DOMAIN $plist failed: $(cat "$TMPD/bootstrap.err"). Log: $LOG_DIR/$1.log"
    sleep 1
  done
  note "loaded dev.ccboard.$1"
}

# Jobs that are off now: boot out and remove what a previous run installed (never the tmux job: it is always wanted).
for job in $ALL_JOBS; do
  in_list "$job" "$JOBS" && continue
  label=dev.ccboard.$job
  if is_loaded "$job"; then launchctl bootout "$DOMAIN/$label" >/dev/null 2>&1 || warn "could not boot out $label"; note "stopped $label (no longer wanted)"; fi
  if [ -f "$LA_DIR/$label.plist" ]; then rm -f "$LA_DIR/$label.plist"; note "removed $LA_DIR/$label.plist"; fi
done

# The board restarts (kickstart -k) when the code, the requirements or the settings file changed since the last run. A changed plist is
# re-read only by a bootout and a bootstrap, which the board, ttyd and the optional jobs may have; tmux never gets either once it runs.
CODE_STAMP_FILE=$ENV_DIR/.macos-code-stamp
CODE_STAMP=$("$PY3" - "$APP_DIR" <<'PY'
import hashlib, os, sys
root = sys.argv[1]
h = hashlib.sha256()
for top in ("app", "bin", "scripts", "launchd"):
    for d, dirs, files in os.walk(os.path.join(root, top)):
        dirs[:] = sorted(x for x in dirs if x != "__pycache__")
        for f in sorted(files):
            if f.endswith(".pyc"):
                continue
            p = os.path.join(d, f)
            h.update(os.path.relpath(p, root).encode() + b"\0")
            try:
                with open(p, "rb") as fh:
                    h.update(fh.read())
            except OSError:
                pass
for f in ("tmux.conf", "requirements.txt"):
    try:
        with open(os.path.join(root, f), "rb") as fh:
            h.update(fh.read())
    except OSError:
        pass
print(h.hexdigest())
PY
)
KICK_JOBS=""
if [ "$ENV_CHANGED" = 1 ] || [ "$VENV_CHANGED" = 1 ] || [ "$(cat "$CODE_STAMP_FILE" 2>/dev/null || true)" != "$CODE_STAMP" ]; then KICK_JOBS="board"; fi
if [ "$CS_CHANGED" = 1 ]; then KICK_JOBS="$KICK_JOBS code-server"; fi

RESTARTED=""
for job in $JOBS; do
  label=dev.ccboard.$job
  if ! is_loaded "$job"; then
    bootstrap_job "$job"; RESTARTED="$RESTARTED $job"
  elif in_list "$job" "$CHANGED_JOBS"; then
    if [ "$job" = tmux ]; then
      warn "dev.ccboard.tmux changed but is running; reloading it would end every session. The new definition loads the next time the job starts (after a logout and login). To load it now: launchctl bootout $DOMAIN/$label, when no session matters"
    else
      launchctl bootout "$DOMAIN/$label" >/dev/null 2>&1 || warn "could not boot out $label before reloading it"
      bootstrap_job "$job"; RESTARTED="$RESTARTED $job"
    fi
  elif in_list "$job" "$KICK_JOBS" && [ "$job" != tmux ]; then
    launchctl kickstart -k "$DOMAIN/$label" >/dev/null 2>&1 || warn "launchctl kickstart -k $DOMAIN/$label failed"
    note "restarted $label"; RESTARTED="$RESTARTED $job"
  fi
done
[ -n "$RESTARTED" ] || note "nothing to load or restart"
if in_list mem "$JOBS"; then
  if out=$(sh "$APP_DIR/bin/ccboard-mem-run" --print 2>&1); then note "claude-mem worker runtime: $(printf '%s\n' "$out" | sed -n 's/^bun=//p')"
  else warn "the claude-mem worker cannot be launched yet: ${out#ccboard-mem-run: }"; fi
fi

# tmux.conf, live: the server read it at its start, so a changed file needs source-file. Only when the server is up (-N: never start one);
# a failure only warns. Sessions are untouched.
log "tmux.conf (live)"
if TMUX_TMPDIR="$TMUX_TMPDIR_V" "$TMUX_BIN" -N -L "$TMUX_SOCKET_V" show-options -s >/dev/null 2>&1; then
  if out=$(TMUX_TMPDIR="$TMUX_TMPDIR_V" "$TMUX_BIN" -N -L "$TMUX_SOCKET_V" source-file "$APP_DIR/tmux.conf" 2>&1); then
    note "applied to the running tmux server (no restart; sessions untouched)"
  else
    warn "tmux.conf was not applied to the running tmux server (${out:-tmux failed}); the server reads it at its next start"
  fi
else
  note "the tmux server is not running yet; it reads tmux.conf when dev.ccboard.tmux starts it"
fi

# ---------------------------------------------------------------- Claude Code: hooks, statusline, MCP
log "Claude Code hooks"
if [ "${CCBOARD_REMOTE_APPROVE:-1}" = 0 ]; then
  "$VENV_PY" "$APP_DIR/scripts/claude_settings.py" install --app-dir "$APP_DIR" --no-remote-approve || warn "could not merge the Claude hooks; Claude sessions will not report to the board"
else
  "$VENV_PY" "$APP_DIR/scripts/claude_settings.py" install --app-dir "$APP_DIR" --approve-timeout "$CCBOARD_APPROVE_TIMEOUT" || warn "could not merge the Claude hooks; Claude sessions will not report to the board"
fi

log "MCP server registration"
MCP_SCRIPT=$APP_DIR/scripts/ccboard_mcp.py
CLAUDE_BIN=$(find_tool claude)
if [ -z "$CLAUDE_BIN" ] && [ -x "$HOME_DIR/.local/bin/claude" ]; then CLAUDE_BIN=$HOME_DIR/.local/bin/claude; fi
if [ -z "$CLAUDE_BIN" ]; then
  note "claude is not installed: MCP registration skipped. Install Claude Code (curl -fsSL https://claude.ai/install.sh | bash), then rerun"
else
  # the board's own venv python, never /usr/bin/python3 (a stub until the Command Line Tools are installed); the shim imports only the standard library
  if cur=$("$CLAUDE_BIN" mcp get ccboard 2>/dev/null); then
    if printf '%s' "$cur" | grep -qF -- "$VENV_PY" && printf '%s' "$cur" | grep -qF -- "$MCP_SCRIPT"; then note "present"; cur=ok
    else
      note "re-pointing the 'ccboard' MCP server at $VENV_PY $MCP_SCRIPT"
      "$CLAUDE_BIN" mcp remove --scope user ccboard >/dev/null 2>&1 || warn "claude mcp remove ccboard failed"
      cur=""
    fi
  else
    cur=""
  fi
  if [ "$cur" != ok ]; then
    if "$CLAUDE_BIN" mcp add --scope user ccboard -- "$VENV_PY" "$MCP_SCRIPT" >/dev/null 2>&1; then
      note "registered 'ccboard' (tools: list_projects, create_task, list_tasks, get_task_status)"
    else
      warn "claude mcp add failed; register manually: claude mcp add --scope user ccboard -- $VENV_PY $MCP_SCRIPT"
    fi
  fi
fi

# ---------------------------------------------------------------- Codex: hooks.json + the MCP server (only when codex is installed)
codex_mcp_register() { # codex-binary host-python script board-url: add the 'ccboard' MCP server to Codex, re-pointing a stale one
  local bin=$1 py=$2 script=$3 url=$4 cur="" stale=0
  cur=$("$bin" mcp get ccboard --json 2>/dev/null) || cur=$("$bin" mcp get ccboard 2>/dev/null) || cur=""
  if [ -n "$cur" ]; then
    case "$cur" in *"$script"*) ;; *) stale=1;; esac
    case "$cur" in *'"transport"'*) case "$cur" in *"\"$url\""*) ;; *) stale=1;; esac;; esac   # only the --json form shows env values
    if [ "$stale" = 0 ]; then note "present"; return 0; fi
    note "re-pointing the 'ccboard' MCP server at $script ($url)"
    "$bin" mcp remove ccboard >/dev/null 2>&1 || { warn "codex mcp remove ccboard failed"; return 1; }
  fi
  if "$bin" mcp add ccboard --env "CCBOARD_URL=$url" -- "$py" "$script" >/dev/null 2>&1; then
    note "registered 'ccboard' with Codex (tools: list_projects, create_task, list_tasks, get_task_status)"
  else
    warn "codex mcp add failed; register manually: codex mcp add ccboard --env CCBOARD_URL=$url -- $py $script"
    return 1
  fi
}
log "Codex hooks and MCP server"
CODEX_BIN=$(find_tool codex)
if [ -z "$CODEX_BIN" ] && [ -x "$HOME_DIR/.local/bin/codex" ]; then CODEX_BIN=$HOME_DIR/.local/bin/codex; fi
if [ -z "$CODEX_BIN" ]; then
  note "codex is not installed: Codex hooks and MCP skipped (install Codex, then rerun the installer)"
else
  if [ "${CCBOARD_REMOTE_APPROVE:-1}" = 0 ]; then
    "$VENV_PY" "$APP_DIR/scripts/codex_hooks.py" install --app-dir "$APP_DIR" --no-remote-approve \
      || warn "could not merge the Codex hooks into ${CODEX_HOME:-$HOME_DIR/.codex}/hooks.json; Codex sessions will not report to the board"
  else
    "$VENV_PY" "$APP_DIR/scripts/codex_hooks.py" install --app-dir "$APP_DIR" --approve-timeout "$CCBOARD_APPROVE_TIMEOUT" \
      || warn "could not merge the Codex hooks into ${CODEX_HOME:-$HOME_DIR/.codex}/hooks.json; Codex sessions will not report to the board"
  fi
  codex_mcp_register "$CODEX_BIN" "$VENV_PY" "$MCP_SCRIPT" "http://127.0.0.1:$CCBOARD_PORT" || true
fi

# ---------------------------------------------------------------- small extras
if have gh && gh auth status >/dev/null 2>&1; then
  gh auth setup-git >/dev/null 2>&1 && note "gh credential helper configured for git (private https clones)" || true
fi
log "ccusage"
if have ccusage || [ -x "$HOME_DIR/.local/bin/ccusage" ]; then
  note "present"
elif have npm; then
  if npm install -g --prefix "$HOME_DIR/.local" ccusage >/dev/null 2>&1; then note "installed to $HOME_DIR/.local/bin/ccusage"
  else warn "npm install ccusage failed; the usage strip will lack burn rate"; fi
else
  warn "node/npm not found: ccusage skipped (the usage strip still shows the 5h/weekly limits from Claude's statusline)"
fi
if [ "$CCBOARD_CLAUDE_MEM" = 1 ]; then
  note "the claude-mem plugin is not installed by this installer; if you want it: claude plugin marketplace add thedotmack/claude-mem, then claude plugin install claude-mem@thedotmack"
fi

# ---------------------------------------------------------------- tailscale serve
log "tailscale serve"
serve_apply() { # https path target
  ts_run apply "$1" "$2" "$3" || die "tailscale serve did not apply https $1 $2 -> $3 (see above). Is HTTPS enabled for the tailnet?"
}
serve_apply "$CCBOARD_HTTPS_PORT" / "$SERVE_BOARD"
serve_apply "$CCBOARD_HTTPS_PORT" /tty "$SERVE_TTYD"
if [ -n "$CODE_SERVER_BIN" ]; then serve_apply "$CODE_HTTPS_PORT" / "$SERVE_CODE"; fi

# ---------------------------------------------------------------- health
log "health"
ok=0
i=0
while [ "$i" -lt "${CCBOARD_MACOS_HEALTH_TRIES:-30}" ]; do
  if curl -fsS "http://127.0.0.1:$CCBOARD_PORT/healthz" >/dev/null 2>&1; then ok=1; break; fi
  i=$((i + 1)); sleep 1
done
if [ "$ok" != 1 ]; then
  tail -n 20 "$LOG_DIR/board.log" 2>/dev/null || true
  die "ccboard is not answering on 127.0.0.1:$CCBOARD_PORT. Look at $LOG_DIR/board.log and run: launchctl print $DOMAIN/dev.ccboard.board"
fi
printf '%s\n' "$CODE_STAMP" > "$CODE_STAMP_FILE"
note "restarted or loaded:${RESTARTED:- nothing}"

printf '\n\033[1;32mccboard is installed.\033[0m\n'
printf '  Dashboard:   https://%s:%s/\n' "$TS_FQDN" "$CCBOARD_HTTPS_PORT"
if [ -n "$CODE_SERVER_BIN" ]; then printf '  code-server: https://%s:%s/\n' "$TS_FQDN" "$CODE_HTTPS_PORT"; fi
printf '  Open them from another device on your tailnet (requests from this Mac carry no Tailscale identity).\n'
printf '  Then click "Log in" on the dashboard to sign in to Claude Code.\n'
if [ -n "$CODEX_BIN" ]; then printf '  Codex:       trust the board'"'"'s hooks once so Codex sessions report state: run codex, review them (or type /hooks); details: python3 %s/scripts/codex_hooks.py trust-help\n' "$APP_DIR"; fi
if [ -n "$CCBOARD_NODES" ]; then printf '  Fleet:       polling %s (same CCBOARD_HUB_TOKEN on every box)\n' "$CCBOARD_NODES"; fi
if in_list backup "$JOBS"; then printf '  Backup:      nightly at %02d:%02d (dev.ccboard.backup) restic + unpushed work to ccboard-backup/<node>/ branches; run one now: launchctl kickstart %s/dev.ccboard.backup\n' "$BACKUP_HOUR" "$BACKUP_MINUTE" "$DOMAIN"; fi
printf '  Restart the board (sessions and the terminal stay):  launchctl kickstart -k %s/dev.ccboard.board\n' "$DOMAIN"
printf '  Logs:        %s/<job>.log  (board, tmux, ttyd)\n' "$LOG_DIR"
printf '  Update:      cd %s && git pull && ./install.sh\n' "$APP_DIR"
printf '  Uninstall:   %s/scripts/uninstall-macos.sh   (keeps %s)\n' "$APP_DIR" "$ENV_DIR"
printf '  Note: after a logout or a reboot nothing runs until you log in again, and the tmux sessions are gone.\n'
