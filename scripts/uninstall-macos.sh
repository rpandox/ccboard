#!/bin/bash
# Uninstall ccboard's launchd jobs on macOS (issue #117). Stock bash 3.2, no sudo.
#
#   scripts/uninstall-macos.sh [--yes] [--keep-tmux]
#
# Boots out every dev.ccboard.* job and removes its plist from ~/Library/LaunchAgents. The tmux job goes LAST, because it is the tmux
# server that owns every Claude and Codex session: booting it out ends them all. It asks first (--yes answers for you; without a terminal
# --yes is required). --keep-tmux leaves dev.ccboard.tmux and its plist alone, so the sessions survive and only the board, the terminal and
# the optional jobs go.
#
# Kept, and said so at the end: the data folder and the settings file in it (the hub token, the backup password file, the database), the
# logs, your projects, the checkout and its .venv, the Claude and Codex hook and MCP registrations, and the tailscale serve mappings. Each
# line says how to remove that part by hand. Nothing outside your home folder is touched.
#
# The domain follows CCBOARD_LAUNCHD_DOMAIN (gui, or user for the experimental layout); without it, a plist that carries
# LimitLoadToSessionType (the Background layout) is booted out of user/<uid> and every other one of gui/<uid>.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

YES=0; KEEP_TMUX=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --yes|-y) YES=1; shift;;
    --keep-tmux) KEEP_TMUX=1; shift;;
    -h|--help) sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0;;
    *) die "unknown argument '$1' (use --yes or --keep-tmux)";;
  esac
done

[ "$(id -u)" -ne 0 ] || die "run as the user who installed ccboard, not as root"
[ "$(uname -s)" = Darwin ] || die "this script is for macOS (found $(uname -s)); on Ubuntu see the Uninstall section of the README"
UID_N=$(id -u)
HOME_DIR=$HOME
LA_DIR=$HOME_DIR/Library/LaunchAgents
LOG_DIR=$HOME_DIR/Library/Logs/ccboard
DATA_DIR=${CCBOARD_DATA_DIR:-$HOME_DIR/.local/share/ccboard}

domain_of() { # plist -> the launchd domain its job lives in
  case "$(printf '%s' "${CCBOARD_LAUNCHD_DOMAIN:-}" | tr '[:upper:]' '[:lower:]')" in
    user) printf 'user/%s' "$UID_N"; return 0;;
    gui) printf 'gui/%s' "$UID_N"; return 0;;
  esac
  if grep -q '<key>LimitLoadToSessionType</key>' "$1" 2>/dev/null; then printf 'user/%s' "$UID_N"; else printf 'gui/%s' "$UID_N"; fi
}
remove_job() { # plist: boot the job out (when loaded) and remove the file
  local plist=$1 label dom
  label=$(basename "$plist" .plist)
  dom=$(domain_of "$plist")
  if launchctl print "$dom/$label" >/dev/null 2>&1; then
    if launchctl bootout "$dom/$label" >/dev/null 2>&1; then note "stopped $label"; else warn "could not boot out $dom/$label; the plist is removed anyway"; fi
  else
    note "$label was not loaded"
  fi
  rm -f "$plist"
  note "removed $plist"
}

COUNT=0
TMUX_PLIST=""
for p in "$LA_DIR"/dev.ccboard.*.plist; do
  [ -e "$p" ] || continue
  COUNT=$((COUNT + 1))
  if [ "$(basename "$p" .plist)" = dev.ccboard.tmux ]; then TMUX_PLIST=$p; fi
done
if [ "$COUNT" -eq 0 ]; then
  log "nothing to remove"
  note "no dev.ccboard.*.plist in $LA_DIR"
else
  if [ -n "$TMUX_PLIST" ] && [ "$KEEP_TMUX" != 1 ]; then
    warn "booting out dev.ccboard.tmux ends the tmux server and every Claude and Codex session in it. Use --keep-tmux to leave it running."
    if [ "$YES" != 1 ]; then
      [ -t 0 ] || die "no terminal to ask on: rerun with --yes (or --keep-tmux)"
      printf '    Remove everything, ending all sessions? [y/N] '
      read -r ans || ans=n
      case "$ans" in y|Y|yes|YES) ;; *) die "cancelled; nothing was changed";; esac
    fi
  fi
  log "launchd jobs"
  for p in "$LA_DIR"/dev.ccboard.*.plist; do
    [ -e "$p" ] || continue
    [ "$p" != "$TMUX_PLIST" ] || continue
    remove_job "$p"
  done
  if [ -n "$TMUX_PLIST" ]; then
    if [ "$KEEP_TMUX" = 1 ]; then note "kept dev.ccboard.tmux (--keep-tmux): the sessions keep running"; else remove_job "$TMUX_PLIST"; fi
  fi
fi

log "what was kept"
note "settings and data: $DATA_DIR (the env file with the hub token, the database, the restic password file)"
note "logs: $LOG_DIR (rm -r it when you no longer need them)"
note "your projects folder and this checkout ($APP_DIR, with its .venv)"
note "Claude hooks and statusline: python3 $APP_DIR/scripts/claude_settings.py remove"
note "Codex hooks: python3 $APP_DIR/scripts/codex_hooks.py remove"
note "the 'ccboard' MCP server: claude mcp remove --scope user ccboard (and codex mcp remove ccboard)"
note "tailscale serve mappings: python3 $APP_DIR/scripts/tailscale_serve.py off --https <port> --path / --target http://127.0.0.1:<port> (only turns off ccboard's own; there is no reset)"
note "Homebrew tools (tmux, ttyd, ...): brew uninstall <name> if you want them gone"
