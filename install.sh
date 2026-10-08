#!/usr/bin/env bash
# ccboard installer. Idempotent. Ubuntu 22.04 / 24.04, amd64 / arm64.
# Run as the user who will own the Claude sessions (it calls sudo where root is needed).
#
#   CCBOARD_HTTPS_PORT=8443 CODE_HTTPS_PORT=10000 CODE_SERVER_PORT=8081 ./install.sh
#
# CCBOARD_RUNTIME=docker runs the board as a container (ghcr.io/rpandox/ccboard) instead of ccboard.service; see the README.
#
# Every setting is an environment variable; previous values are kept in /etc/ccboard/env.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE=/etc/ccboard/env
ENV_KEYS=(PROJECTS_DIR CCBOARD_PORT TTYD_PORT CODE_SERVER_PORT CCBOARD_HTTPS_PORT CODE_HTTPS_PORT CCBOARD_ALLOWED_USERS CCBOARD_DATA_DIR CODE_SERVER_VERSION CCBOARD_PUBLIC_URL NTFY_URL NTFY_TOPIC NTFY_PUBLIC_URL NTFY_HTTPS_PORT NTFY_PORT CCBOARD_APPROVE_TIMEOUT PREVIEW_HTTPS_BASE CCBOARD_NODE_NAME CCBOARD_HUB_TOKEN CCBOARD_NODES CCBOARD_RESTIC_REPO CCBOARD_RESTIC_PASSWORD_FILE CCBOARD_BACKUP_PUSH CCBOARD_BACKUP_ONCALENDAR CCBOARD_BACKUP_EXTRA CCBOARD_RUNTIME CCBOARD_AUTO_CONTINUE CCBOARD_CLAUDE_MEM CCBOARD_MEM_PORT CCBOARD_MEM_HTTPS_PORT CCBOARD_MEM_SERVICE CCBOARD_CODEX_HOOK_TRUST CCBOARD_CLONE_ALLOWED_HOSTS CODEX_HOME)
TTYD_VERSION=1.7.7
TTYD_SHA_amd64=8a217c968aba172e0dbf3f34447218dc015bc4d5e59bf51db2f2cd12b7be4f55
TTYD_SHA_arm64=b38acadd89d1d396a0f5649aa52c539edbad07f4bc7348b27b4f4b7219dd4165
TTYD_BIN=/usr/local/bin/ttyd

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
ver_ge() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" = "$2" ]; }

# ---------------------------------------------------------------- guards
[ "$(id -u)" -ne 0 ] || die "run as the user who will own the sessions, not as root (sudo is used where needed)"
[ -t 0 ] || die "run interactively (e.g. ssh -t host 'cd ccboard && ./install.sh'): sudo needs a terminal"
have sudo || die "sudo is required"
. /etc/os-release
[ "${ID:-}" = ubuntu ] || die "Ubuntu 22.04 or 24.04 required (found ${PRETTY_NAME:-unknown})"
case "${VERSION_ID:-}" in 22.04|24.04) ;; *) die "Ubuntu 22.04 or 24.04 required (found $VERSION_ID)";; esac
ARCH=$(dpkg --print-architecture)
case "$ARCH" in amd64|arm64) ;; *) die "unsupported architecture $ARCH";; esac
USER_NAME=$(id -un)
HOME_DIR=$HOME
SHELL_PATH=$(getent passwd "$USER_NAME" | cut -d: -f7)
[ -n "$SHELL_PATH" ] || SHELL_PATH=/bin/bash
have python3 || die "python3 is required (apt-get install python3)"

log "sudo access (one password prompt, kept alive for the run)"
sudo -v
( while true; do sleep 50; sudo -n true 2>/dev/null || exit; done ) &
SUDO_KEEPALIVE=$!
trap 'kill "$SUDO_KEEPALIVE" 2>/dev/null || true' EXIT

# ---------------------------------------------------------------- config
# Explicit environment beats the previous env file, which beats the defaults.
declare -A CALLER
for k in "${ENV_KEYS[@]}"; do CALLER[$k]="${!k:-}"; done
EXTRA_ENV=()
if [ -f "$ENV_FILE" ]; then
  # Never source it: values are data, not shell. Whitelisted keys are read; other KEY=value lines you added by hand
  # (e.g. AWS_* for an s3: restic repo) are kept as they are and written back.
  while IFS='=' read -r k v; do
    case "$k" in ''|'#'*) continue;; esac
    case " ${ENV_KEYS[*]} " in
      *" $k "*) printf -v "$k" '%s' "$v";;
      *) [[ "$k" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] && EXTRA_ENV+=("$k=$v");;
    esac
  done < "$ENV_FILE"
fi
for k in "${ENV_KEYS[@]}"; do [ -n "${CALLER[$k]}" ] && printf -v "$k" '%s' "${CALLER[$k]}"; done
: "${PROJECTS_DIR:=/srv/projects}"
: "${CCBOARD_PORT:=8000}"
: "${TTYD_PORT:=7681}"
: "${CODE_SERVER_PORT:=8080}"
: "${CCBOARD_HTTPS_PORT:=443}"
: "${CODE_HTTPS_PORT:=8443}"
: "${CCBOARD_ALLOWED_USERS:=}"
: "${CCBOARD_DATA_DIR:=$HOME_DIR/.local/share/ccboard}"
: "${CODE_SERVER_VERSION:=4.139.1}"
: "${NTFY_PORT:=2586}"
: "${NTFY_HTTPS_PORT:=8444}"
: "${NTFY_TOPIC:=ccboard}"
: "${NTFY_URL:=}"
: "${NTFY_PUBLIC_URL:=}"
: "${CCBOARD_APPROVE_TIMEOUT:=90}"
: "${PREVIEW_HTTPS_BASE:=9100}"
: "${CCBOARD_NODE_NAME:=$(hostname -s)}"
: "${CCBOARD_NODES:=}"
if [ -z "${CCBOARD_HUB_TOKEN:-}" ]; then CCBOARD_HUB_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(24))'); note "generated CCBOARD_HUB_TOKEN (copy it to the other boxes to form a fleet)"; fi
[[ "$CCBOARD_NODE_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,40}$ ]] || die "CCBOARD_NODE_NAME must be letters, digits, - or _"
: "${CCBOARD_RESTIC_REPO:=}"          # empty = local repo under CCBOARD_DATA_DIR; 'off' disables restic
: "${CCBOARD_RESTIC_PASSWORD_FILE:=}" # empty = $CCBOARD_DATA_DIR/restic-password
: "${CCBOARD_BACKUP_EXTRA:=}"         # colon-separated extra paths to include in the snapshot
: "${CCBOARD_BACKUP_PUSH:=1}"
: "${CCBOARD_BACKUP_ONCALENDAR:=*-*-* 02:30:00}"
: "${CCBOARD_RUNTIME:=systemd}"        # systemd = the board is ccboard.service; docker = the board is a container
: "${CCBOARD_AUTO_CONTINUE:=1}"         # the app reads an empty value as 1; the env file below needs every ENV_KEYS name bound (set -u)
: "${CCBOARD_CLAUDE_MEM:=1}"           # 1 = verify/install the claude-mem plugin and let the board watch its worker; 0 = neither
: "${CCBOARD_MEM_PORT:=}"              # empty = the board finds the worker's port itself (worker.pid, then claude-mem's settings)
: "${CCBOARD_MEM_HTTPS_PORT:=}"        # empty = no claude-mem viewer link (issue #9); only the board's state.config.mem_viewer_url reads it, no tailscale serve mapping is made yet
: "${CCBOARD_MEM_SERVICE:=0}"          # 1 = run the worker as ccboard-mem.service from a clean environment (off until verified on the box)
: "${CCBOARD_CODEX_HOOK_TRUST:=review}"  # review = trust ccboard's Codex hooks once in Codex (/hooks); bypass = start Codex with --dangerously-bypass-hook-trust
: "${CCBOARD_CLONE_ALLOWED_HOSTS:=}"      # empty = clones may only name public hosts; a private git server (gitea.lan, 192.168.1.5) is listed here, comma separated
: "${CODEX_HOME:=}"                    # empty = Codex's own default (~/.codex); the hooks.json and the MCP entry go where Codex reads them
systemd-analyze calendar "$CCBOARD_BACKUP_ONCALENDAR" >/dev/null 2>&1 || die "CCBOARD_BACKUP_ONCALENDAR is not a systemd calendar spec: $CCBOARD_BACKUP_ONCALENDAR"
[[ "$PREVIEW_HTTPS_BASE" =~ ^[0-9]{1,5}$ ]] || die "PREVIEW_HTTPS_BASE must be a port number"
[[ "$CCBOARD_APPROVE_TIMEOUT" =~ ^[0-9]{1,4}$ ]] || die "CCBOARD_APPROVE_TIMEOUT must be seconds"
for k in CCBOARD_PORT TTYD_PORT CODE_SERVER_PORT CCBOARD_HTTPS_PORT CODE_HTTPS_PORT NTFY_PORT NTFY_HTTPS_PORT; do
  [[ "${!k}" =~ ^[0-9]{1,5}$ ]] || die "$k must be a port number (got '${!k}')"
done
[ "$CCBOARD_HTTPS_PORT" != "$CODE_HTTPS_PORT" ] && [ "$NTFY_HTTPS_PORT" != "$CCBOARD_HTTPS_PORT" ] && [ "$NTFY_HTTPS_PORT" != "$CODE_HTTPS_PORT" ] \
  || die "CCBOARD_HTTPS_PORT, CODE_HTTPS_PORT and NTFY_HTTPS_PORT must all differ"
[[ "$NTFY_TOPIC" =~ ^[A-Za-z0-9_-]{1,64}$ ]] || die "NTFY_TOPIC must be letters, digits, - or _"
[ "$CCBOARD_PORT" != "$TTYD_PORT" ] && [ "$CCBOARD_PORT" != "$CODE_SERVER_PORT" ] && [ "$TTYD_PORT" != "$CODE_SERVER_PORT" ] \
  || die "CCBOARD_PORT, TTYD_PORT and CODE_SERVER_PORT must all differ"
[[ "$PROJECTS_DIR" = /* ]] || die "PROJECTS_DIR must be an absolute path"
case "$CCBOARD_RUNTIME" in systemd|docker) ;; *) die "CCBOARD_RUNTIME must be systemd or docker (got '$CCBOARD_RUNTIME')";; esac
case "$CCBOARD_CLAUDE_MEM" in 0|1) ;; *) die "CCBOARD_CLAUDE_MEM must be 0 or 1 (got '$CCBOARD_CLAUDE_MEM')";; esac
case "$CCBOARD_MEM_SERVICE" in 0|1) ;; *) die "CCBOARD_MEM_SERVICE must be 0 or 1 (got '$CCBOARD_MEM_SERVICE')";; esac
if [ -n "$CCBOARD_MEM_HTTPS_PORT" ]; then   # the viewer port: never 443 (another service's Funnel), never one the board already serves; refused before any change
  [[ "$CCBOARD_MEM_HTTPS_PORT" =~ ^[0-9]{1,5}$ ]] && [ "$CCBOARD_MEM_HTTPS_PORT" -ge 1 ] && [ "$CCBOARD_MEM_HTTPS_PORT" -le 65535 ] || die "CCBOARD_MEM_HTTPS_PORT must be empty or a port from 1 to 65535 (got '$CCBOARD_MEM_HTTPS_PORT')"
  case "$CCBOARD_MEM_HTTPS_PORT" in 443|"$CCBOARD_HTTPS_PORT"|"$CODE_HTTPS_PORT"|"$NTFY_HTTPS_PORT") die "CCBOARD_MEM_HTTPS_PORT $CCBOARD_MEM_HTTPS_PORT is taken: it must not be 443 or the board's, code-server's or ntfy's HTTPS port";; esac
fi
[ -z "$CCBOARD_MEM_PORT" ] || [[ "$CCBOARD_MEM_PORT" =~ ^[0-9]{1,5}$ ]] || die "CCBOARD_MEM_PORT must be empty or a port number (got '$CCBOARD_MEM_PORT')"
case "$CCBOARD_CODEX_HOOK_TRUST" in review|bypass) ;; *) die "CCBOARD_CODEX_HOOK_TRUST must be review or bypass (got '$CCBOARD_CODEX_HOOK_TRUST')";; esac
[ -z "$CODEX_HOME" ] || [[ "$CODEX_HOME" = /* ]] || die "CODEX_HOME must be empty or an absolute path (got '$CODEX_HOME')"
[ "$CCBOARD_CODEX_HOOK_TRUST" != bypass ] || warn "CCBOARD_CODEX_HOOK_TRUST=bypass: every Codex session the board starts skips the hook review, including a .codex/hooks.json inside a repository you cloned (the same risk class as bypassPermissions)"
CCBOARD_ALLOWED_USERS=$(printf '%s' "$CCBOARD_ALLOWED_USERS" | tr -d '[:space:]')
for k in PROJECTS_DIR CCBOARD_DATA_DIR CODE_SERVER_VERSION CCBOARD_ALLOWED_USERS CODEX_HOME; do
  case "${!k}" in *[[:space:]\"\$\\]*) die "$k must not contain whitespace, quotes, \$ or backslashes (got '${!k}')";; esac
done
[ -z "$CODEX_HOME" ] || export CODEX_HOME   # codex_hooks.py and the codex CLI below read it
for v in ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN CLAUDE_CODE_OAUTH_TOKEN; do
  [ -z "${!v:-}" ] || warn "$v is set in your environment; it outranks the Claude login. It is NOT written to $ENV_FILE."
done

# ---------------------------------------------------------------- tailscale checks (before touching anything)
have tailscale || die "tailscale is not installed (https://tailscale.com/download)"
TS_STATUS=$(tailscale status --json 2>/dev/null || true)
[ -n "$TS_STATUS" ] || die "tailscale status failed; is tailscaled running and logged in?"
read -r TS_STATE TS_FQDN TS_LOGIN TS_CERTS < <(printf '%s' "$TS_STATUS" | python3 -c '
import json,sys
d=json.load(sys.stdin); s=d.get("Self") or {}
login=((d.get("User") or {}).get(str(s.get("UserID"))) or {}).get("LoginName","")
print(d.get("BackendState","") or "-", (s.get("DNSName") or "").rstrip(".") or "-", login or "-", len(d.get("CertDomains") or []))')
[ "$TS_STATE" = Running ] || die "tailscale is not running/logged in (BackendState=$TS_STATE)"
[ "$TS_FQDN" != - ] || die "this node has no MagicDNS name; enable MagicDNS in the admin console"
[ "$TS_CERTS" != 0 ] || die "HTTPS certificates are not enabled for this tailnet: turn on 'HTTPS Certificates' at https://login.tailscale.com/admin/dns and rerun"
tailscale serve --help 2>&1 | grep -q -- '--set-path' || die "tailscale is too old for 'serve --set-path' (need 1.54+)"
if [ -z "$CCBOARD_ALLOWED_USERS" ]; then
  [ "$TS_LOGIN" != "-" ] || die "could not derive your tailnet login; set CCBOARD_ALLOWED_USERS=you@provider"
  CCBOARD_ALLOWED_USERS=$TS_LOGIN
fi
CCBOARD_PUBLIC_URL="https://$TS_FQDN:$CCBOARD_HTTPS_PORT"
note "tailnet node $TS_FQDN, allowed users: $CCBOARD_ALLOWED_USERS"

# ---------------------------------------------------------------- docker runtime (CCBOARD_RUNTIME=docker)
# Function definitions only; nothing here runs in systemd mode. The board becomes a container (ghcr.io/rpandox/ccboard,
# built by GitHub Actions) with your home, the tmux socket and the tailscale socket mounted. tmux, ttyd, code-server,
# tailscale serve, ntfy and the backup timer stay on the host. docker_prepare runs before anything is touched (the image
# is pulled first); docker_switch runs after the env file and the units are written.
COMPOSE_DIR="$CCBOARD_DATA_DIR/compose"
COMPOSE_FILE="$COMPOSE_DIR/docker-compose.yml"
DOCKER_APP="$CCBOARD_DATA_DIR/app"   # bin/, scripts/ and tmux.conf the host side (ttyd, hooks, MCP) runs; the entrypoint refreshes it
DOCKER_BIN=/usr/bin/docker
IMAGE_TAG=latest
dc() { docker compose -f "$COMPOSE_FILE" "$@"; }   # the .env next to the compose file supplies the variables

docker_container_running() { # the board's container, not just any process on the port
  local names; names=$(docker ps --format '{{.Names}}' 2>/dev/null || true)
  printf '%s\n' "$names" | grep -qx ccboard
}

docker_preflight() {
  have docker || die "docker is not installed. Install Docker Engine and the compose plugin (https://docs.docker.com/engine/install/ubuntu/), then rerun"
  case "$(command -v docker)" in /*) DOCKER_BIN=$(command -v docker);; esac
  docker compose version >/dev/null 2>&1 || die "the 'docker compose' plugin is missing: sudo apt-get install docker-compose-plugin (from Docker's apt repository), then rerun"
  # id with a name reads the group database, so this also catches a user added since this shell started
  id -nG "$USER_NAME" | tr ' ' '\n' | grep -qx docker \
    || die "$USER_NAME is not in the docker group. Run: sudo usermod -aG docker $USER_NAME, log out and back in, then rerun"
  docker info >/dev/null 2>&1 \
    || die "docker is not usable from this shell. If $USER_NAME was just added to the docker group, log out and back in (or run 'newgrp docker'); otherwise start the daemon: sudo systemctl start docker"
  case "$CCBOARD_DATA_DIR/" in "$HOME_DIR"/*) ;; *) die "docker mode mounts your home directory only: CCBOARD_DATA_DIR must be under $HOME_DIR (got $CCBOARD_DATA_DIR)";; esac
  [ -f "$APP_DIR/deploy/docker-compose.yml" ] || die "$APP_DIR/deploy/docker-compose.yml is missing: update this checkout (git pull) or use a newer ccboard.tar.gz"
  # A bind-mount source that does not exist is created by the docker daemon as a root-owned directory: never let that happen.
  [ -S /var/run/tailscale/tailscaled.sock ] || die "no tailscaled socket at /var/run/tailscale/tailscaled.sock (the container mounts it for previews); is tailscaled running?"
  [ ! -d "$HOME_DIR/.docker/config.json" ] || die "$HOME_DIR/.docker/config.json is a directory (docker created it for a missing mount source). Remove it (rmdir) and rerun"
  if [ ! -e "$HOME_DIR/.docker/config.json" ]; then   # Watchtower mounts it read-only for registry credentials
    mkdir -p "$HOME_DIR/.docker"; (umask 077; printf '{}\n' > "$HOME_DIR/.docker/config.json"); note "created an empty $HOME_DIR/.docker/config.json"
  fi
  [ "${CCBOARD_DEVCONTAINER:-0}" != 1 ] || warn "devcontainer sessions need the docker CLI and socket inside the container (not mounted); they stay a systemd-mode feature"
  note "docker $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo '?'), $(docker compose version --short 2>/dev/null || echo 'compose ?')"
}

docker_write_compose() { # the template is variable-driven; the .env carries this box's values
  local want
  mkdir -p "$COMPOSE_DIR"
  if ! cmp -s "$APP_DIR/deploy/docker-compose.yml" "$COMPOSE_FILE"; then
    install -m 0644 "$APP_DIR/deploy/docker-compose.yml" "$COMPOSE_FILE"; note "wrote $COMPOSE_FILE"
  fi
  # the shadow override (rollout step B) travels with it; compose never loads it unless named with -f
  if [ -f "$APP_DIR/deploy/docker-compose.shadow.yml" ] && ! cmp -s "$APP_DIR/deploy/docker-compose.shadow.yml" "$COMPOSE_DIR/docker-compose.shadow.yml"; then
    install -m 0644 "$APP_DIR/deploy/docker-compose.shadow.yml" "$COMPOSE_DIR/docker-compose.shadow.yml"
  fi
  # an explicit CCBOARD_IMAGE_TAG wins, then the tag already in .env (a pinned sha-<7> or vX.Y.Z survives reruns), then latest
  IMAGE_TAG=${CCBOARD_IMAGE_TAG:-}
  [ -n "$IMAGE_TAG" ] || IMAGE_TAG=$(sed -n 's/^CCBOARD_IMAGE_TAG=//p' "$COMPOSE_DIR/.env" 2>/dev/null | head -1 || true)
  : "${IMAGE_TAG:=latest}"
  [[ "$IMAGE_TAG" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] || die "CCBOARD_IMAGE_TAG is not a valid image tag: $IMAGE_TAG"
  want=$(printf 'CCBOARD_HOME=%s\nCCBOARD_UID=%s\nCCBOARD_GID=%s\nPROJECTS_DIR=%s\nCCBOARD_IMAGE_TAG=%s' \
    "$HOME_DIR" "$(id -u)" "$(id -g)" "$PROJECTS_DIR" "$IMAGE_TAG")
  if [ ! -f "$COMPOSE_DIR/.env" ] || [ "$(cat "$COMPOSE_DIR/.env")" != "$want" ]; then
    printf '%s\n' "$want" > "$COMPOSE_DIR/.env"; note "wrote $COMPOSE_DIR/.env"
  fi
}

docker_seed_app() { # once: ttyd and the Claude hooks need valid targets before the first container start
  mkdir -p "$DOCKER_APP/bin" "$DOCKER_APP/scripts"
  if [ -x "$DOCKER_APP/bin/ccboard-attach" ] && [ -f "$DOCKER_APP/scripts/claude_settings.py" ]; then
    note "$DOCKER_APP present (the container refreshes it on every start)"
    return 0
  fi
  local f
  for f in "$APP_DIR"/bin/*; do
    if [ -f "$f" ]; then install -m 0755 "$f" "$DOCKER_APP/bin/"; fi
  done
  for f in "$APP_DIR"/scripts/*; do
    if [ -f "$f" ]; then install -m 0755 "$f" "$DOCKER_APP/scripts/"; fi
  done
  install -m 0644 "$APP_DIR/tmux.conf" "$DOCKER_APP/tmux.conf"
  note "seeded $DOCKER_APP from this checkout (bin, scripts, tmux.conf)"
}

docker_pull() { # a failure here dies before any host change
  log "docker image"
  note "pulling ghcr.io/rpandox/ccboard:$IMAGE_TAG and the Watchtower image (can take a few minutes)"
  dc --profile prod pull || die "docker compose pull failed; nothing running on this box was changed.
  If the error above says denied, unauthorized or not found, the ghcr.io package is private or not built yet. Either:
    1. make it public: GitHub -> your profile -> Packages -> ccboard -> Package settings -> Change visibility -> Public
    2. log this box in to ghcr.io (the same login lets Watchtower pull):
         gh auth refresh -s read:packages && gh auth token | docker login ghcr.io -u <github-user> --password-stdin
  Otherwise check that the GitHub Actions run for the commit you installed has finished: https://github.com/rpandox/ccboard/actions"
}

# The entrypoint reads CCBOARD_REMOTE_APPROVE from /etc/ccboard/env on every start; install.sh keeps it there
# (as a hand-added line, like AWS_* keys) so the hooks it merges match the ones merged here.
docker_remote_approve() {
  local line v=${CCBOARD_REMOTE_APPROVE:-} keep=()
  for line in "${EXTRA_ENV[@]:-}"; do
    case "$line" in
      CCBOARD_REMOTE_APPROVE=*) [ -n "$v" ] || v=${line#*=};;
      *) keep+=("$line");;
    esac
  done
  EXTRA_ENV=("${keep[@]:-}")
  CCBOARD_REMOTE_APPROVE=${v:-1}
  if [ "$CCBOARD_REMOTE_APPROVE" = 0 ]; then EXTRA_ENV+=("CCBOARD_REMOTE_APPROVE=0"); fi
}

docker_prepare() {
  log "docker runtime: preflight, compose files, image pull"
  docker_preflight
  docker_write_compose
  docker_seed_app
  docker_remote_approve
  docker_pull
}

docker_register_mcp() { # claude-binary. Claude spawns the MCP server on the HOST, so the command must be host-valid
  local want="$DOCKER_APP/scripts/ccboard_mcp.py" cur
  if cur=$("$1" mcp get ccboard 2>/dev/null); then
    if printf '%s' "$cur" | grep -qF -- "$want"; then note "present"; return 0; fi
    note "re-pointing the 'ccboard' MCP server at $want"
    "$1" mcp remove --scope user ccboard >/dev/null 2>&1 || warn "claude mcp remove ccboard failed"
  fi
  if "$1" mcp add --scope user ccboard -- /usr/bin/python3 "$want" >/dev/null 2>&1; then
    note "registered 'ccboard' (tools: list_projects, create_task, list_tasks, get_task_status)"
  else
    warn "claude mcp add failed; register manually: claude mcp add --scope user ccboard -- /usr/bin/python3 $want"
  fi
}

docker_rollback_hint() {
  printf '\n\033[1mRollback to the systemd board\033[0m (running Claude sessions are never touched):\n'
  printf '  docker compose -f %s --profile prod down\n' "$COMPOSE_FILE"
  printf '  CCBOARD_RUNTIME=systemd ./install.sh      # also points the ttyd and backup units, hooks and sudoers back at this checkout (the MCP registration keeps working from %s)\n' "$DOCKER_APP"
  printf '  # without the installer (the backup unit keeps targeting the container until you rerun it):\n'
  printf '  sudo sed -i %s %s && sudo systemctl enable --now ccboard.service\n' "'s/^CCBOARD_RUNTIME=.*/CCBOARD_RUNTIME=systemd/'" "$ENV_FILE"
}

docker_auto_rollback() { # the switch failed: bring the systemd board back so the box is never left without a board
  warn "rolling back: stopping the container and re-enabling ccboard.service"
  dc --profile prod down >/dev/null 2>&1 || true
  sudo systemctl enable --now ccboard.service >/dev/null 2>&1 || warn "could not re-enable ccboard.service; run: sudo systemctl enable --now ccboard.service"
}

docker_switch() { # replaces the restart of ccboard.service: stop it, start the container, wait for /healthz
  log "docker runtime: switching the board to the container"
  # the container mounts this directory; if it were missing, docker would create it root-owned and tmux could not use it
  [ -d "/tmp/tmux-$(id -u)" ] || die "/tmp/tmux-$(id -u) does not exist: ccboard-tmux.service is not running (sudo systemctl start ccboard-tmux)"
  sudo tailscale set --operator="$USER_NAME" || die "tailscale set --operator=$USER_NAME failed (previews need it without sudo)"
  note "tailscale operator: $USER_NAME"
  # After a reboot /tmp/tmux-<uid> and /var/run/tailscale exist only once ccboard-tmux and tailscaled are up, and docker
  # does not retry a container whose bind source was missing at daemon start: order the docker daemon after them.
  sudo install -d /etc/systemd/system/docker.service.d
  printf '[Unit]\nAfter=ccboard-tmux.service tailscaled.service\nWants=ccboard-tmux.service\n' | sudo tee /etc/systemd/system/docker.service.d/ccboard.conf >/dev/null
  sudo systemctl daemon-reload
  # Only now, after the pull succeeded: the unit and the container cannot both bind 127.0.0.1:$CCBOARD_PORT.
  sudo systemctl disable --now ccboard.service >/dev/null 2>&1 || warn "could not disable ccboard.service; the container will fail to bind the port if it is still running"
  local up_rc=0
  if printf '%s\n' "${changed_units[@]:-}" | grep -qx ccboard.service; then
    dc --profile prod up -d --force-recreate || up_rc=$?   # the env file or a unit changed: make the container see it
  else
    dc --profile prod up -d || up_rc=$?
  fi
  if [ "$up_rc" != 0 ]; then
    docker_auto_rollback
    docker_rollback_hint >&2
    die "docker compose up failed (exit $up_rc); rolled back to ccboard.service"
  fi
  local i ok=0
  for ((i = 0; i < 60; i++)); do
    if curl -fsS "http://127.0.0.1:$CCBOARD_PORT/healthz" >/dev/null 2>&1; then ok=1; break; fi
    sleep 1
  done
  if [ "$ok" != 1 ]; then
    dc --profile prod ps 2>&1 | tail -8 || true
    docker logs --tail 30 ccboard 2>&1 || true
    docker_auto_rollback
    docker_rollback_hint >&2
    die "the ccboard container is not answering on 127.0.0.1:$CCBOARD_PORT after 60 s; rolled back to ccboard.service"
  fi
  note "ccboard container healthy on 127.0.0.1:$CCBOARD_PORT"
}

if [ "$CCBOARD_RUNTIME" = docker ]; then docker_prepare; fi

# ---------------------------------------------------------------- apt ttyd unit (would hold 7681 as root)
if [ -f /lib/systemd/system/ttyd.service ] || [ -f /usr/lib/systemd/system/ttyd.service ]; then
  if systemctl is-enabled --quiet ttyd.service 2>/dev/null || systemctl is-active --quiet ttyd.service 2>/dev/null; then
    log "disabling the apt package's ttyd.service (ccboard runs its own ttyd)"
    sudo systemctl disable --now ttyd.service || true
  fi
fi

# ---------------------------------------------------------------- port collisions
port_busy() { ss -ltnH 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$1\$"; }
check_port() { # port unit
  if port_busy "$1" && ! systemctl is-active --quiet "$2"; then
    warn "port $1 is in use and $2 is not running:"; sudo ss -ltnp 2>/dev/null | grep -E "[:.]$1 " || true
    die "choose another port (e.g. CODE_SERVER_PORT=8081) or stop that process"
  fi
}
if [ "$CCBOARD_RUNTIME" = docker ] && docker_container_running; then note "port $CCBOARD_PORT is held by the ccboard container"
else check_port "$CCBOARD_PORT" ccboard.service; fi
check_port "$TTYD_PORT" ccboard-ttyd.service
check_port "$CODE_SERVER_PORT" "code-server@$USER_NAME.service"

# ---------------------------------------------------------------- apt
log "apt packages"
missing=()
for p in git tmux curl ca-certificates python3 python3-venv; do
  dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q 'install ok installed' || missing+=("$p")
done
if [ "${#missing[@]}" -gt 0 ]; then
  note "installing: ${missing[*]}"
  sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get update -q
  sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get install -y -q --no-install-recommends "${missing[@]}"
else
  note "all present"
fi

# ---------------------------------------------------------------- ttyd (upstream static binary)
log "ttyd"
need_ttyd=1
if [ -x "$TTYD_BIN" ]; then
  cur=$("$TTYD_BIN" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)
  if [ -n "$cur" ] && ver_ge "$cur" 1.7.4; then need_ttyd=0; note "present: $cur"; fi
fi
if [ "$need_ttyd" = 1 ]; then
  case "$ARCH" in amd64) asset=ttyd.x86_64; sha=$TTYD_SHA_amd64;; arm64) asset=ttyd.aarch64; sha=$TTYD_SHA_arm64;; esac
  tmp=$(mktemp)
  curl -fsSL -o "$tmp" "https://github.com/tsl0922/ttyd/releases/download/$TTYD_VERSION/$asset"
  echo "$sha  $tmp" | sha256sum -c --quiet - || die "ttyd checksum mismatch"
  sudo install -m 0755 "$tmp" "$TTYD_BIN"; rm -f "$tmp"
  note "installed ttyd $TTYD_VERSION to $TTYD_BIN"
fi

# ---------------------------------------------------------------- code-server
log "code-server"
cs_changed=0
if ! have code-server; then
  note "installing code-server $CODE_SERVER_VERSION"
  curl -fsSL https://code-server.dev/install.sh | sh -s -- --version "$CODE_SERVER_VERSION"
  cs_changed=1
else
  note "present: $(code-server --version 2>/dev/null | head -1) (pinned $CODE_SERVER_VERSION; not changed)"
fi
cs_cfg="$HOME_DIR/.config/code-server/config.yaml"
# telemetry, update checks and the workspace-trust prompt all cost time on first open; none of them has a job on a tailnet-only box
cs_want=$(printf '# managed by ccboard\nbind-addr: 127.0.0.1:%s\nauth: none\ncert: false\ndisable-telemetry: true\ndisable-update-check: true\ndisable-workspace-trust: true\ndisable-getting-started-override: true\n' "$CODE_SERVER_PORT")
if [ -f "$cs_cfg" ] && [ "$(head -1 "$cs_cfg")" != "# managed by ccboard" ]; then
  if [ "${CCBOARD_REPLACE_CODE_SERVER_CONFIG:-}" = 1 ]; then
    cp "$cs_cfg" "$cs_cfg.ccboard-bak-$(date +%s)"; warn "backed up your existing $cs_cfg"
  else
    die "$cs_cfg exists and is not managed by ccboard. ccboard needs 'auth: none' on 127.0.0.1:$CODE_SERVER_PORT. Rerun with CCBOARD_REPLACE_CODE_SERVER_CONFIG=1 to back it up and replace it."
  fi
fi
if [ ! -f "$cs_cfg" ] || [ "$(cat "$cs_cfg")" != "$cs_want" ]; then
  mkdir -p "$(dirname "$cs_cfg")"; printf '%s\n' "$cs_want" > "$cs_cfg"; cs_changed=1; note "wrote $cs_cfg"
fi
cs_dropin_dir="/etc/systemd/system/code-server@$USER_NAME.service.d"
# the editor is interactive: it goes ahead of batch jobs for CPU and disk, and the OOM killer takes it last
cs_dropin=$(printf '[Service]\nEnvironment=PATH=%s/.local/bin:/usr/local/bin:/usr/bin:/bin\nNice=-5\nIOSchedulingClass=best-effort\nIOSchedulingPriority=1\nOOMScoreAdjust=-500\n' "$HOME_DIR")
if [ ! -f "$cs_dropin_dir/ccboard.conf" ] || [ "$(cat "$cs_dropin_dir/ccboard.conf")" != "$cs_dropin" ]; then
  printf '%s\n' "$cs_dropin" | sudo install -D -m 0644 /dev/stdin "$cs_dropin_dir/ccboard.conf"
  sudo systemctl daemon-reload; cs_changed=1
fi
sudo systemctl enable --now "code-server@$USER_NAME" >/dev/null
[ "$cs_changed" = 0 ] || sudo systemctl restart "code-server@$USER_NAME"
# user settings that keep a window cheap: no watchers or searches over node_modules / venvs / build output / worktrees, no
# telemetry, update or experiment traffic, no automatic type acquisition. Only keys you have not set; `remove` takes them back.
python3 "$APP_DIR/scripts/code_server_settings.py" install || warn "code-server settings not merged"

# ---------------------------------------------------------------- Claude Code
log "Claude Code"
if have claude || [ -x "$HOME_DIR/.local/bin/claude" ]; then
  note "present: $("$HOME_DIR/.local/bin/claude" --version 2>/dev/null || claude --version 2>/dev/null || echo unknown)"
else
  note "installing with the native installer"
  curl -fsSL https://claude.ai/install.sh | bash
fi

# ---------------------------------------------------------------- claude-mem (the plugin; the clean-environment worker unit is set up with the other units)
# CCBOARD_CLAUDE_MEM=0 skips all of it. The plugin is a Claude Code plugin (claude-mem@thedotmack) that runs one worker per box;
# the board only reads it. Never installed here: bun (the plugin looks for it and installs nothing itself) and anything for Codex.
# Nothing in this block may fail the install: both entry points return 0 on a miss and their call sites end in `|| warn`.
# The functions between the two marker lines are exercised on their own by tests/test_install_mem.py (stubbed sudo and systemctl).
# >>> claude-mem helpers
MEM_PLUGIN_KEY="claude-mem@thedotmack"
MEM_UNIT_DIR=/etc/systemd/system
MEM_SETTLE=3

mem_timeout() { if have timeout; then timeout "$@"; else shift; "$@"; fi; }

mem_plugin_state() { # "<version> enabled|disabled", or "missing" when installed_plugins.json has no claude-mem@thedotmack entry
  python3 - "${CLAUDE_CONFIG_DIR:-$HOME_DIR/.claude}" "$MEM_PLUGIN_KEY" <<'PY' 2>/dev/null || echo missing
import json, os, sys
cfg, key = sys.argv[1:3]
def load(name):
    try:
        with open(os.path.join(cfg, name)) as f:
            return json.load(f)
    except Exception:
        return {}
entries = (load("plugins/installed_plugins.json").get("plugins") or {}).get(key) or []
if not entries:
    print("missing")
else:
    off = (load("settings.json").get("enabledPlugins") or {}).get(key) is False
    print("%s %s" % (entries[0].get("version") or "?", "disabled" if off else "enabled"))
PY
}

mem_plugin_step() {
  local st claude="" ver en out
  if [ "$CCBOARD_CLAUDE_MEM" = 0 ]; then note "skipped (CCBOARD_CLAUDE_MEM=0)"; return 0; fi
  st=$(mem_plugin_state)
  if [ "$st" = missing ]; then
    if have claude; then claude=$(command -v claude); elif [ -x "$HOME_DIR/.local/bin/claude" ]; then claude="$HOME_DIR/.local/bin/claude"; fi
    note "the claude-mem plugin is not installed; installing it with:"
    note "  claude plugin marketplace add thedotmack/claude-mem"
    note "  claude plugin install $MEM_PLUGIN_KEY"
    if [ -z "$claude" ]; then warn "claude is not available; run the two commands above by hand"; return 0; fi
    # not chained with &&: a marketplace that is already added may answer with an error, and the install is what counts
    mem_timeout 300 "$claude" plugin marketplace add thedotmack/claude-mem || note "marketplace add reported an error (continuing with the install)"
    mem_timeout 300 "$claude" plugin install "$MEM_PLUGIN_KEY" || warn "claude plugin install $MEM_PLUGIN_KEY failed (it needs github.com); run it by hand"
    st=$(mem_plugin_state)
    if [ "$st" = missing ]; then
      warn "claude-mem is still not in installed_plugins.json; the board's doctor will say so"
      return 0
    fi
    read -r ver en <<<"$st"
    note "installed claude-mem $ver; Claude sessions that are already open load its hooks when they next start"
  else
    read -r ver en <<<"$st"
    note "present: claude-mem $ver ($en)"
  fi
  if [ "$en" = disabled ]; then warn "claude-mem is disabled for Claude; enable it with: claude plugin enable $MEM_PLUGIN_KEY"; fi
  # the worker needs bun, which the plugin finds (or does not) by itself; say so now rather than in a silent memory gap
  if out=$(sh "$APP_DIR/bin/ccboard-mem-run" --print 2>&1); then
    note "worker runtime: $(printf '%s\n' "$out" | sed -n 's/^bun=//p')"
  else
    warn "the claude-mem worker cannot be launched on this box yet: ${out#ccboard-mem-run: }"
  fi
  return 0
}

mem_autostart_hint() { # $1 unit, $2 bun, $3 worker script, $4 data dir. Printed only: ccboard never writes ~/.claude-mem/settings.json
  note "a plugin update can undo this: the first hook after it sees a worker of the old version, kills it and starts one of its own"
  note "(with that hook's environment, so the leak is back). Recover the same way: $2 $3 stop, then sudo systemctl start $1"
  note "Optional, your decision: \"CLAUDE_MEM_WORKER_AUTOSTART\": \"false\" in $4/settings.json (plugin 13.29.0) keeps hooks from"
  note "starting or replacing a worker. Then only $1 starts one, and the doctor's memory-worker check is what tells you when it is down."
}

mem_service_setup() { # after the other units are enabled: ccboard-mem.service on (CCBOARD_MEM_SERVICE=1) or off
  local u=ccboard-mem.service unit out bun worker data wpid main changed=0 tmp
  unit="$MEM_UNIT_DIR/$u"
  if [ "$CCBOARD_CLAUDE_MEM" != 1 ] || [ "$CCBOARD_MEM_SERVICE" != 1 ]; then
    if [ -f "$unit" ] && { systemctl is-enabled --quiet "$u" 2>/dev/null || systemctl is-active --quiet "$u" 2>/dev/null; }; then
      sudo systemctl disable --now "$u" >/dev/null 2>&1 || warn "could not disable $u"
      note "$u disabled: the claude-mem worker is started by Claude's hooks again (the unit file stays)"
    elif [ "$CCBOARD_CLAUDE_MEM" = 1 ]; then
      note "$u is off (CCBOARD_MEM_SERVICE=1 runs the worker from a clean environment; see the README)"
    fi
    return 0
  fi
  if ! out=$(sh "$APP_DIR/bin/ccboard-mem-run" --print 2>&1); then
    warn "$u not installed: ${out#ccboard-mem-run: }"
    return 0
  fi
  bun=$(printf '%s\n' "$out" | sed -n 's/^bun=//p'); worker=$(printf '%s\n' "$out" | sed -n 's/^worker=//p')
  # the unit runs the launcher from APP_BIN: the checkout's bin/ in systemd mode, the data dir's copy in docker mode
  if [ "$APP_BIN" != "$APP_DIR/bin" ]; then install -m 0755 "$APP_DIR/bin/ccboard-mem-run" "$APP_BIN/ccboard-mem-run"
  else chmod 0755 "$APP_BIN/ccboard-mem-run"; fi
  tmp=$(mktemp); render_unit "$u" > "$tmp"
  if ! cmp -s "$tmp" "$unit"; then sudo install -m 0644 "$tmp" "$unit"; changed=1; note "wrote $unit"; fi
  rm -f "$tmp"
  sudo systemctl daemon-reload
  sudo systemctl enable "$u" >/dev/null
  # Two workers must never race on worker.pid. A worker a hook started (it carries that session's environment, which is what
  # this unit is for) keeps running and makes a second one exit at once, so it is handed over by hand, not killed here.
  data=${CLAUDE_MEM_DATA_DIR:-$HOME_DIR/.claude-mem}
  wpid=$(python3 -c 'import json,sys; print(int(json.load(open(sys.argv[1])).get("pid") or 0))' "$data/worker.pid" 2>/dev/null || echo 0)
  main=$(systemctl show -p MainPID --value "$u" 2>/dev/null || echo 0)
  if [ "${wpid:-0}" -gt 0 ] && kill -0 "$wpid" 2>/dev/null && [ "$wpid" != "${main:-0}" ]; then
    warn "a claude-mem worker started by a hook is running (pid $wpid): $u is enabled for the next boot but not started"
    note "hand over when no session is mid-turn:"
    note "  $bun $worker stop"
    note "  sudo systemctl start $u"
    note "  tr '\\0' '\\n' < /proc/\$(systemctl show -p MainPID --value $u)/environ | grep -c '^CCBOARD_SESSION='    # expect 0"
    mem_autostart_hint "$u" "$bun" "$worker" "$data"
    return 0
  fi
  if [ "$changed" = 1 ] && systemctl is-active --quiet "$u" 2>/dev/null; then sudo systemctl restart "$u"; else sudo systemctl start "$u"; fi
  sleep "$MEM_SETTLE"
  if systemctl is-active --quiet "$u" 2>/dev/null; then
    note "$u running (worker pid $(systemctl show -p MainPID --value "$u" 2>/dev/null || echo '?'))"
    mem_autostart_hint "$u" "$bun" "$worker" "$data"
  else
    warn "$u is not running: journalctl -u ccboard-mem -n 30. A worker a hook started in the meantime would be the cause (see the README, claude-mem)"
  fi
  return 0
}
# <<< claude-mem helpers

log "claude-mem"
mem_plugin_step || warn "the claude-mem step failed; the rest of the install goes on"

# ---------------------------------------------------------------- ntfy (push notifications on the tailnet)
log "ntfy"
if [ "${CCBOARD_NTFY:-1}" = 0 ]; then
  note "skipped (CCBOARD_NTFY=0)"; NTFY_URL=""; NTFY_PUBLIC_URL=""
else
  if ! have ntfy; then
    note "installing ntfy from archive.ntfy.sh"
    sudo mkdir -p /etc/apt/keyrings
    sudo curl -fsSL -o /etc/apt/keyrings/ntfy.gpg https://archive.ntfy.sh/apt/keyring.gpg
    echo "deb [arch=$ARCH signed-by=/etc/apt/keyrings/ntfy.gpg] https://archive.ntfy.sh/apt stable main" | sudo tee /etc/apt/sources.list.d/ntfy.list >/dev/null
    sudo DEBIAN_FRONTEND=noninteractive apt-get update -q
    sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get install -y -q --no-install-recommends ntfy
  fi
  NTFY_URL="http://127.0.0.1:$NTFY_PORT"
  NTFY_PUBLIC_URL="https://$TS_FQDN:$NTFY_HTTPS_PORT"
  ntfy_cfg=/etc/ntfy/server.yml
  ntfy_want=$(printf '# managed by ccboard\nlisten-http: "127.0.0.1:%s"\nbase-url: "%s"\nbehind-proxy: true\nupstream-base-url: "https://ntfy.sh"\ncache-file: "/var/cache/ntfy/cache.db"\nattachment-cache-dir: "/var/cache/ntfy/attachments"\n' "$NTFY_PORT" "$NTFY_PUBLIC_URL")
  ntfy_changed=0
  if [ -f "$ntfy_cfg" ] && [ "$(sudo head -1 "$ntfy_cfg")" != "# managed by ccboard" ] && sudo grep -qE '^[a-z]' "$ntfy_cfg"; then
    if [ "${CCBOARD_REPLACE_NTFY_CONFIG:-}" = 1 ]; then sudo cp "$ntfy_cfg" "$ntfy_cfg.ccboard-bak-$(date +%s)"; warn "backed up your $ntfy_cfg"
    else die "$ntfy_cfg has your own settings. Rerun with CCBOARD_REPLACE_NTFY_CONFIG=1 to replace it, or CCBOARD_NTFY=0 to skip ntfy."; fi
  fi
  if [ ! -f "$ntfy_cfg" ] || [ "$(sudo cat "$ntfy_cfg")" != "$ntfy_want" ]; then
    printf '%s\n' "$ntfy_want" | sudo install -D -m 0644 /dev/stdin "$ntfy_cfg"; ntfy_changed=1; note "wrote $ntfy_cfg"
  fi
  sudo systemctl enable --now ntfy >/dev/null 2>&1 || true
  [ "$ntfy_changed" = 0 ] || sudo systemctl restart ntfy
  note "ntfy on $NTFY_URL, topic $NTFY_TOPIC, phone URL $NTFY_PUBLIC_URL/$NTFY_TOPIC"
fi

# ---------------------------------------------------------------- devcontainer CLI (optional: CCBOARD_DEVCONTAINER=1)
if [ "${CCBOARD_DEVCONTAINER:-0}" = 1 ]; then
  log "devcontainer CLI"
  if ! have docker; then warn "docker is not installed; devcontainer sessions will fail (install docker and add $USER_NAME to the docker group)"; fi
  if have devcontainer || [ -x "$HOME_DIR/.local/bin/devcontainer" ]; then note "present"
  elif have npm; then npm install -g --prefix "$HOME_DIR/.local" @devcontainers/cli >/dev/null 2>&1 && note "installed to $HOME_DIR/.local/bin/devcontainer" || warn "npm install @devcontainers/cli failed"
  else warn "npm not found: devcontainer CLI skipped"; fi
fi

# ---------------------------------------------------------------- restic (nightly backup; CCBOARD_BACKUP=0 leaves the timer off)
log "backup"
if [ "${CCBOARD_BACKUP:-1}" = 0 ]; then
  note "timer will be disabled (CCBOARD_BACKUP=0)"
elif [ "$CCBOARD_RESTIC_REPO" = off ]; then
  note "restic off (CCBOARD_RESTIC_REPO=off); nightly backup branches on origin only"
else
  if ! have restic; then
    note "installing restic (apt)"
    sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get install -y -q --no-install-recommends restic >/dev/null || warn "apt-get install restic failed; backups will report 'restic is not installed'"
  fi
  mkdir -p "$CCBOARD_DATA_DIR"
  pwfile="${CCBOARD_RESTIC_PASSWORD_FILE:-$CCBOARD_DATA_DIR/restic-password}"
  if [ ! -s "$pwfile" ]; then
    mkdir -p "$(dirname "$pwfile")"
    (umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$pwfile")
    note "generated $pwfile — copy it somewhere safe; without it the backups cannot be read"
  fi
  chmod 0600 "$pwfile"
  note "restic repo: ${CCBOARD_RESTIC_REPO:-$CCBOARD_DATA_DIR/restic (same disk: set CCBOARD_RESTIC_REPO to an sftp:/rclone:/s3: repo for real safety)}"
fi

# ---------------------------------------------------------------- gh: let git use gh's credentials for https clones (bulk import)
if have gh && gh auth status >/dev/null 2>&1; then
  gh auth setup-git >/dev/null 2>&1 && note "gh credential helper configured for git (private https clones)" || true
fi

# ---------------------------------------------------------------- ccusage (burn rate for the usage strip; optional)
log "ccusage"
if have ccusage || [ -x "$HOME_DIR/.local/bin/ccusage" ]; then
  note "present"
elif have npm; then
  npm install -g --prefix "$HOME_DIR/.local" ccusage >/dev/null 2>&1 && note "installed to $HOME_DIR/.local/bin/ccusage" || warn "npm install ccusage failed; the usage strip will lack burn rate"
else
  warn "node/npm not found: ccusage skipped (the usage strip still shows the 5h/weekly limits from Claude's statusline)"
fi

# ---------------------------------------------------------------- directories, venv
log "directories"
sudo mkdir -p "$PROJECTS_DIR"; sudo chown "$USER_NAME:$(id -gn)" "$PROJECTS_DIR"
mkdir -p "$CCBOARD_DATA_DIR"
chmod 0755 "$APP_DIR/bin/ccboard-attach"

log "python venv"
cd "$APP_DIR"
stamp="$(sha256sum requirements.txt | cut -d' ' -f1) $(python3 -V 2>&1)"
if [ -d .venv ] && ! .venv/bin/python -V >/dev/null 2>&1; then rm -rf .venv; fi
if [ ! -f .venv/.ccboard-stamp ] || [ "$(cat .venv/.ccboard-stamp)" != "$stamp" ] || ! .venv/bin/python -c 'import fastapi, uvicorn' 2>/dev/null; then
  [ -d .venv ] || python3 -m venv .venv
  .venv/bin/pip install -q --disable-pip-version-check -r requirements.txt
  printf '%s' "$stamp" > .venv/.ccboard-stamp
  note "installed requirements"
else
  note "up to date"
fi

# ---------------------------------------------------------------- Claude Code hooks + statusline (no sudo)
log "Claude Code hooks"
if [ "$CCBOARD_RUNTIME" = docker ]; then   # hooks, statusline and ttyd run from the data dir the container keeps current
  if [ "$CCBOARD_REMOTE_APPROVE" = 0 ]; then
    python3 "$APP_DIR/scripts/claude_settings.py" install --app-dir "$DOCKER_APP" --no-remote-approve
  else
    python3 "$APP_DIR/scripts/claude_settings.py" install --app-dir "$DOCKER_APP" --approve-timeout "${CCBOARD_APPROVE_TIMEOUT:-90}"
  fi
  # watchdog: on a busy box Watchtower can create the new container without its start taking (seen on ubu2: state 'Created',
  # a 502 until someone ran docker start). Every 2 minutes from the user's crontab (no sudo): start it when it is not running,
  # restart it when it stays unhealthy. Marked so a rerun replaces the line.
  if have crontab; then
    wd_line=$(printf '*/2 * * * * CCBOARD_DATA_DIR=%s %s/scripts/ccboard-watchdog.sh >/dev/null 2>&1 # ccboard-watchdog' "$CCBOARD_DATA_DIR" "$DOCKER_APP")
    { crontab -l 2>/dev/null | grep -v '# ccboard-watchdog$'; printf '%s\n' "$wd_line"; } | crontab - && note "watchdog: crontab line installed (every 2 min)"
  else
    warn "crontab not found: the container watchdog (scripts/ccboard-watchdog.sh) is not scheduled"
  fi
elif [ "${CCBOARD_REMOTE_APPROVE:-1}" = 0 ]; then
  python3 "$APP_DIR/scripts/claude_settings.py" install --app-dir "$APP_DIR" --no-remote-approve
else
  python3 "$APP_DIR/scripts/claude_settings.py" install --app-dir "$APP_DIR" --approve-timeout "${CCBOARD_APPROVE_TIMEOUT:-90}"
fi

# ---------------------------------------------------------------- MCP: register the board's tools with Claude Code (user scope)
log "MCP server registration"
if have claude || [ -x "$HOME_DIR/.local/bin/claude" ]; then
  CLAUDE_BIN=$(command -v claude || echo "$HOME_DIR/.local/bin/claude")
  if [ "$CCBOARD_RUNTIME" = docker ]; then
    docker_register_mcp "$CLAUDE_BIN"
  elif "$CLAUDE_BIN" mcp get ccboard >/dev/null 2>&1; then
    note "present"
  else
    "$CLAUDE_BIN" mcp add --scope user ccboard -- "$APP_DIR/.venv/bin/python" "$APP_DIR/scripts/ccboard_mcp.py" >/dev/null 2>&1 \
      && note "registered 'ccboard' (tools: list_projects, create_task, list_tasks, get_task_status)" || warn "claude mcp add failed; register manually: claude mcp add --scope user ccboard -- $APP_DIR/.venv/bin/python $APP_DIR/scripts/ccboard_mcp.py"
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
CODEX_BIN=""
if have codex; then CODEX_BIN=$(command -v codex); elif [ -x "$HOME_DIR/.local/bin/codex" ]; then CODEX_BIN="$HOME_DIR/.local/bin/codex"; fi
if [ -z "$CODEX_BIN" ]; then
  note "codex is not installed: Codex hooks and MCP skipped (install Codex, then rerun ./install.sh)"
else
  if [ "$CCBOARD_RUNTIME" = docker ]; then CODEX_APP="$DOCKER_APP"; CODEX_PY=/usr/bin/python3; else CODEX_APP="$APP_DIR"; CODEX_PY="$APP_DIR/.venv/bin/python"; fi
  if [ "${CCBOARD_REMOTE_APPROVE:-1}" = 0 ]; then
    python3 "$APP_DIR/scripts/codex_hooks.py" install --app-dir "$CODEX_APP" --no-remote-approve \
      || warn "could not merge the Codex hooks into ${CODEX_HOME:-$HOME_DIR/.codex}/hooks.json; Codex sessions will not report to the board"
  else
    python3 "$APP_DIR/scripts/codex_hooks.py" install --app-dir "$CODEX_APP" --approve-timeout "${CCBOARD_APPROVE_TIMEOUT:-90}" \
      || warn "could not merge the Codex hooks into ${CODEX_HOME:-$HOME_DIR/.codex}/hooks.json; Codex sessions will not report to the board"
  fi
  codex_mcp_register "$CODEX_BIN" "$CODEX_PY" "$CODEX_APP/scripts/ccboard_mcp.py" "http://127.0.0.1:$CCBOARD_PORT" || true
fi

# ---------------------------------------------------------------- sudoers: let the user restart the stateless units (deploys)
log "sudoers rule for restarts"
if [ "$CCBOARD_RUNTIME" = docker ]; then
  # the board is a container: only ttyd is restarted through systemd; previews use the tailscale operator, serve stays as a fallback
  if [ "${CCBOARD_PREVIEWS:-1}" = 0 ]; then
    sudoers_want=$(printf '%s ALL=(root) NOPASSWD: /usr/bin/systemctl restart ccboard-ttyd.service\n' "$USER_NAME")
  else
    sudoers_want=$(printf '%s ALL=(root) NOPASSWD: /usr/bin/systemctl restart ccboard-ttyd.service, /usr/bin/tailscale serve *\n' "$USER_NAME")
  fi
elif [ "${CCBOARD_PREVIEWS:-1}" = 0 ]; then
  sudoers_want=$(printf '%s ALL=(root) NOPASSWD: /usr/bin/systemctl restart ccboard.service, /usr/bin/systemctl restart ccboard-ttyd.service, /usr/bin/systemctl try-restart ccboard.service, /usr/bin/systemctl start --no-block ccboard-backup.service\n' "$USER_NAME")
else
  # `tailscale serve` (tailnet-only) is allowed so the board can expose per-worktree preview ports; `tailscale funnel` is not.
  sudoers_want=$(printf '%s ALL=(root) NOPASSWD: /usr/bin/systemctl restart ccboard.service, /usr/bin/systemctl restart ccboard-ttyd.service, /usr/bin/systemctl try-restart ccboard.service, /usr/bin/systemctl start --no-block ccboard-backup.service, /usr/bin/tailscale serve *\n' "$USER_NAME")
fi
if [ ! -f /etc/sudoers.d/ccboard ] || [ "$(sudo cat /etc/sudoers.d/ccboard)" != "$sudoers_want" ]; then
  tmp=$(mktemp); printf '%s\n' "$sudoers_want" > "$tmp"
  sudo visudo -cf "$tmp" >/dev/null && sudo install -m 0440 -o root -g root "$tmp" /etc/sudoers.d/ccboard && note "wrote /etc/sudoers.d/ccboard"
  rm -f "$tmp"
else
  note "present"
fi

# ---------------------------------------------------------------- env file + units
log "config and systemd units"
env_body=""
for k in "${ENV_KEYS[@]}"; do env_body+="$k=${!k}"$'\n'; done
for line in "${EXTRA_ENV[@]:-}"; do [ -n "$line" ] && env_body+="$line"$'\n'; done
changed_units=()
if [ ! -f "$ENV_FILE" ] || [ "$(cat "$ENV_FILE")" != "${env_body%$'\n'}" ]; then
  printf '%s' "$env_body" | sudo install -D -m 0640 -o root -g "$(id -gn)" /dev/stdin "$ENV_FILE"; note "wrote $ENV_FILE"
  changed_units+=(ccboard.service ccboard-ttyd.service)
fi
# The file holds CCBOARD_HUB_TOKEN: readable by root (systemd) and your group (reruns of this script), nobody else.
sudo chmod 0640 "$ENV_FILE"; sudo chgrp "$(id -gn)" "$ENV_FILE"
esc() { printf '%s' "$1" | sed -e 's/[&|\\]/\\&/g'; }
# Values that differ per runtime. The systemd-mode values are what the unit templates used to hardcode.
if [ "$CCBOARD_RUNTIME" = docker ]; then
  APP_BIN="$DOCKER_APP/bin"
  BACKUP_EXEC="$DOCKER_BIN exec ccboard /opt/ccboard/.venv/bin/python -m app.backup"   # DOCKER_BIN = command -v docker (/usr/bin/docker on Ubuntu)
else
  APP_BIN="$APP_DIR/bin"
  BACKUP_EXEC="$APP_DIR/.venv/bin/python -m app.backup"
fi
render_unit() { # name
  sed -e "s|__USER__|$(esc "$USER_NAME")|g" -e "s|__HOME__|$(esc "$HOME_DIR")|g" -e "s|__APP_DIR__|$(esc "$APP_DIR")|g" \
      -e "s|__SHELL__|$(esc "$SHELL_PATH")|g" -e "s|__TTYD_BIN__|$(esc "$TTYD_BIN")|g" \
      -e "s|__APP_BIN__|$(esc "$APP_BIN")|g" -e "s|__BACKUP_EXEC__|$(esc "$BACKUP_EXEC")|g" \
      -e "s|__BACKUP_ONCALENDAR__|$(esc "$CCBOARD_BACKUP_ONCALENDAR")|g" "$APP_DIR/systemd/$1.in"
}
for u in ccboard-tmux.service ccboard-ttyd.service ccboard.service ccboard-backup.service ccboard-backup.timer; do
  tmp=$(mktemp); render_unit "$u" > "$tmp"
  if ! cmp -s "$tmp" "/etc/systemd/system/$u"; then
    sudo install -m 0644 "$tmp" "/etc/systemd/system/$u"; changed_units+=("$u"); note "wrote /etc/systemd/system/$u"
  fi
  rm -f "$tmp"
done
[ "$need_ttyd" = 0 ] || changed_units+=(ccboard-ttyd.service)
sudo systemctl daemon-reload
if [ "$CCBOARD_RUNTIME" = docker ]; then   # ccboard.service stays installed (rollback) but is not enabled: the container owns the port
  sudo systemctl enable --now ccboard-tmux.service ccboard-ttyd.service >/dev/null
else
  sudo systemctl enable --now ccboard-tmux.service ccboard-ttyd.service ccboard.service >/dev/null
  if [ -f /etc/systemd/system/docker.service.d/ccboard.conf ]; then sudo rm -f /etc/systemd/system/docker.service.d/ccboard.conf; sudo systemctl daemon-reload; fi
fi
if [ "${CCBOARD_BACKUP:-1}" = 0 ]; then
  sudo systemctl disable --now ccboard-backup.timer >/dev/null 2>&1 || true
else
  sudo systemctl enable --now ccboard-backup.timer >/dev/null
fi
# claude-mem worker as a unit of its own, from a clean environment (CCBOARD_MEM_SERVICE=1; both runtimes: it is a host process)
mem_service_setup || warn "the ccboard-mem.service step failed; the rest of the install goes on"
# tmux.conf, live: the server read it at its start, so a changed file needs source-file to take effect. Only when the server is up
# (-N: never start one; ccboard-tmux.service owns it and is never restarted here), and a failure only warns: the options are
# idempotent and the next start of the server reads the file anyway. Sessions are untouched.
log "tmux.conf (live)"
if tmux -N -L ccboard show-options -s >/dev/null 2>&1; then
  if out=$(tmux -N -L ccboard source-file "$APP_DIR/tmux.conf" 2>&1); then
    note "applied to the running tmux server (no restart; sessions untouched)"
  else
    warn "tmux.conf was not applied to the running tmux server (${out:-tmux failed}); the server reads it at its next start"
  fi
else
  note "tmux server is not running; it reads tmux.conf when ccboard-tmux.service starts it"
fi
restarted=()
for u in ccboard-tmux.service ccboard-ttyd.service; do
  if printf '%s\n' "${changed_units[@]:-}" | grep -qx "$u"; then
    if [ "$u" = ccboard-tmux.service ] && systemctl is-active --quiet "$u"; then
      warn "ccboard-tmux.service changed but is running; restarting it would kill every Claude session. Restart it yourself when convenient: sudo systemctl restart ccboard-tmux"
    else
      sudo systemctl restart "$u"; restarted+=("$u")
    fi
  fi
done
if [ "$CCBOARD_RUNTIME" = docker ]; then
  docker_switch; restarted+=(ccboard-container)
else
  sudo systemctl restart ccboard.service; restarted+=(ccboard.service)
fi

# ---------------------------------------------------------------- tailscale serve
log "tailscale serve"
serve_check() { # port path target -> prints ours|missing|foreign:<why>|funnel
  tailscale serve status --json 2>/dev/null | python3 -c '
import json,sys
raw=sys.stdin.read().strip(); d=json.loads(raw) if raw and raw!="null" else {}
fqdn,port,path,target=sys.argv[1:5]; key=f"{fqdn}:{port}"
if (d.get("AllowFunnel") or {}).get(key): print("funnel"); sys.exit()
tcp=(d.get("TCP") or {}).get(port) or {}
if tcp and not tcp.get("HTTPS"): print("foreign:port is used for TCP forwarding"); sys.exit()
handlers=(((d.get("Web") or {}).get(key) or {}).get("Handlers") or {})
ours={"/":None,"/tty":None} if port==sys.argv[5] else {"/":None}
for p,h in handlers.items():
    if p not in ours: print(f"foreign:handler {p} -> {h}"); sys.exit()
h=handlers.get(path)
if h is None: print("missing")
elif h.get("Proxy")==target: print("ours")
elif str(h.get("Proxy","")).startswith("http://127.0.0.1:"): print("stale")   # ccboard handler with an old backend port
else: print(f"foreign:handler {path} -> {h}")
' "$TS_FQDN" "$1" "$2" "$3" "$CCBOARD_HTTPS_PORT"
}
serve_apply() { # port path target
  st=$(serve_check "$1" "$2" "$3")
  case "$st" in
    ours) note "https://$TS_FQDN:$1$2 -> $3 (already set)";;
    missing|stale)
      if [ "$st" = stale ]; then
        note "replacing the old ccboard handler at https://$TS_FQDN:$1$2"
        sudo tailscale serve --https="$1" --set-path "$2" off >/dev/null 2>&1 || true
      fi
      if [ "$2" = / ]; then sudo timeout 60 tailscale serve --bg --https="$1" "$3" >/dev/null
      else sudo timeout 60 tailscale serve --bg --https="$1" --set-path "$2" "$3" >/dev/null; fi
      note "https://$TS_FQDN:$1$2 -> $3";;
    funnel|foreign:*)
      if [ "${CCBOARD_REPLACE_SERVE:-}" = 1 ]; then
        warn "port $1 has other handlers ($st); replacing because CCBOARD_REPLACE_SERVE=1"
        sudo tailscale serve --https="$1" --yes off >/dev/null 2>&1 || true
        serve_apply "$1" "$2" "$3"
      else
        die "tailscale serve port $1 is already used by something else ($st). Pick another port (e.g. CCBOARD_HTTPS_PORT=8443 CODE_HTTPS_PORT=10000) or rerun with CCBOARD_REPLACE_SERVE=1 to replace it."
      fi;;
    *) die "unexpected serve state '$st'";;
  esac
}
serve_apply "$CCBOARD_HTTPS_PORT" / "http://127.0.0.1:$CCBOARD_PORT"
serve_apply "$CCBOARD_HTTPS_PORT" /tty "http://127.0.0.1:$TTYD_PORT"
serve_apply "$CODE_HTTPS_PORT" / "http://127.0.0.1:$CODE_SERVER_PORT"
[ -z "$NTFY_URL" ] || serve_apply "$NTFY_HTTPS_PORT" / "$NTFY_URL"
for spec in "$CCBOARD_HTTPS_PORT / http://127.0.0.1:$CCBOARD_PORT" "$CCBOARD_HTTPS_PORT /tty http://127.0.0.1:$TTYD_PORT" "$CODE_HTTPS_PORT / http://127.0.0.1:$CODE_SERVER_PORT"; do
  # shellcheck disable=SC2086
  [ "$(serve_check $spec)" = ours ] || die "tailscale serve did not apply ($spec). Is HTTPS enabled for the tailnet?"
done

# ---------------------------------------------------------------- health
log "health"
ok=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS "http://127.0.0.1:$CCBOARD_PORT/healthz" >/dev/null 2>&1; then ok=1; break; fi; sleep 1
done
if [ "$ok" != 1 ]; then
  if [ "$CCBOARD_RUNTIME" = docker ]; then docker logs --tail 30 ccboard 2>&1 || true; else sudo systemctl status ccboard.service --no-pager 2>&1 | tail -20 || true; fi
  die "ccboard is not answering on 127.0.0.1:$CCBOARD_PORT"
fi
note "restarted: ${restarted[*]}"
printf '\n\033[1;32mccboard is installed.\033[0m\n'
printf '  Dashboard:   https://%s:%s/\n' "$TS_FQDN" "$CCBOARD_HTTPS_PORT"
printf '  code-server: https://%s:%s/\n' "$TS_FQDN" "$CODE_HTTPS_PORT"
[ -z "$NTFY_URL" ] || printf '  ntfy topic:  %s/%s   (subscribe in the ntfy app; iOS needs the app to reach ntfy.sh for wake-ups)\n' "$NTFY_PUBLIC_URL" "$NTFY_TOPIC"
printf '  Open them from another device on your tailnet (requests from this box carry no Tailscale identity).\n'
printf '  Then click "Log in" on the dashboard to sign in to Claude Code.\n'
[ -z "$CODEX_BIN" ] || printf '  Codex:       trust the board'"'"'s hooks once so Codex sessions report state: run codex on this box, review them (or type /hooks); details: python3 %s/scripts/codex_hooks.py trust-help\n' "$APP_DIR"
[ -z "$CCBOARD_NODES" ] || printf '  Fleet:       polling %s (same CCBOARD_HUB_TOKEN on every box)\n' "$CCBOARD_NODES"
[ "${CCBOARD_BACKUP:-1}" = 0 ] || printf '  Backup:      nightly (%s) restic + unpushed work to ccboard-backup/<node>/ branches; run one now: sudo systemctl start ccboard-backup; log: journalctl -u ccboard-backup\n' "$CCBOARD_BACKUP_ONCALENDAR"
if [ "$CCBOARD_RUNTIME" = docker ]; then
  printf '  Runtime:     container ghcr.io/rpandox/ccboard:%s; Watchtower (scope ccboard) pulls new images every 5 minutes\n' "$IMAGE_TAG"
  printf '  Container:   docker compose -f %s --profile prod ps     logs: docker logs -f ccboard\n' "$COMPOSE_FILE"
  docker_rollback_hint
fi
