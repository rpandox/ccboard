#!/usr/bin/env bash
# ccboard installer. Idempotent. Ubuntu 22.04 / 24.04, amd64 / arm64.
# Run as the user who will own the Claude sessions (it calls sudo where root is needed).
#
#   CCBOARD_HTTPS_PORT=8443 CODE_HTTPS_PORT=10000 CODE_SERVER_PORT=8081 ./install.sh
#
# Every setting is an environment variable; previous values are kept in /etc/ccboard/env.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE=/etc/ccboard/env
ENV_KEYS=(PROJECTS_DIR CCBOARD_PORT TTYD_PORT CODE_SERVER_PORT CCBOARD_HTTPS_PORT CODE_HTTPS_PORT CCBOARD_ALLOWED_USERS CCBOARD_DATA_DIR CODE_SERVER_VERSION CCBOARD_PUBLIC_URL NTFY_URL NTFY_TOPIC NTFY_PUBLIC_URL NTFY_HTTPS_PORT NTFY_PORT CCBOARD_APPROVE_TIMEOUT)
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
if [ -f "$ENV_FILE" ]; then
  # Never source it: values are data, not shell. Only whitelisted keys are read.
  while IFS='=' read -r k v; do
    case " ${ENV_KEYS[*]} " in *" $k "*) printf -v "$k" '%s' "$v";; esac
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
CCBOARD_ALLOWED_USERS=$(printf '%s' "$CCBOARD_ALLOWED_USERS" | tr -d '[:space:]')
for k in PROJECTS_DIR CCBOARD_DATA_DIR CODE_SERVER_VERSION CCBOARD_ALLOWED_USERS; do
  case "${!k}" in *[[:space:]\"\$\\]*) die "$k must not contain whitespace, quotes, \$ or backslashes (got '${!k}')";; esac
done
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
check_port "$CCBOARD_PORT" ccboard.service
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
cs_want=$(printf '# managed by ccboard\nbind-addr: 127.0.0.1:%s\nauth: none\ncert: false\n' "$CODE_SERVER_PORT")
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
cs_dropin=$(printf '[Service]\nEnvironment=PATH=%s/.local/bin:/usr/local/bin:/usr/bin:/bin\n' "$HOME_DIR")
if [ ! -f "$cs_dropin_dir/ccboard.conf" ] || [ "$(cat "$cs_dropin_dir/ccboard.conf")" != "$cs_dropin" ]; then
  printf '%s\n' "$cs_dropin" | sudo install -D -m 0644 /dev/stdin "$cs_dropin_dir/ccboard.conf"
  sudo systemctl daemon-reload; cs_changed=1
fi
sudo systemctl enable --now "code-server@$USER_NAME" >/dev/null
[ "$cs_changed" = 0 ] || sudo systemctl restart "code-server@$USER_NAME"

# ---------------------------------------------------------------- Claude Code
log "Claude Code"
if have claude || [ -x "$HOME_DIR/.local/bin/claude" ]; then
  note "present: $("$HOME_DIR/.local/bin/claude" --version 2>/dev/null || claude --version 2>/dev/null || echo unknown)"
else
  note "installing with the native installer"
  curl -fsSL https://claude.ai/install.sh | bash
fi

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
if [ "${CCBOARD_REMOTE_APPROVE:-1}" = 0 ]; then
  python3 "$APP_DIR/scripts/claude_settings.py" install --app-dir "$APP_DIR" --no-remote-approve
else
  python3 "$APP_DIR/scripts/claude_settings.py" install --app-dir "$APP_DIR" --approve-timeout "${CCBOARD_APPROVE_TIMEOUT:-90}"
fi

# ---------------------------------------------------------------- sudoers: let the user restart the stateless units (deploys)
log "sudoers rule for restarts"
sudoers_want=$(printf '%s ALL=(root) NOPASSWD: /usr/bin/systemctl restart ccboard.service, /usr/bin/systemctl restart ccboard-ttyd.service, /usr/bin/systemctl try-restart ccboard.service\n' "$USER_NAME")
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
changed_units=()
if [ ! -f "$ENV_FILE" ] || [ "$(cat "$ENV_FILE")" != "${env_body%$'\n'}" ]; then
  printf '%s' "$env_body" | sudo install -D -m 0644 /dev/stdin "$ENV_FILE"; note "wrote $ENV_FILE"
  changed_units+=(ccboard.service ccboard-ttyd.service)
fi
esc() { printf '%s' "$1" | sed -e 's/[&|\\]/\\&/g'; }
render_unit() { # name
  sed -e "s|__USER__|$(esc "$USER_NAME")|g" -e "s|__HOME__|$(esc "$HOME_DIR")|g" -e "s|__APP_DIR__|$(esc "$APP_DIR")|g" \
      -e "s|__SHELL__|$(esc "$SHELL_PATH")|g" -e "s|__TTYD_BIN__|$(esc "$TTYD_BIN")|g" "$APP_DIR/systemd/$1.in"
}
for u in ccboard-tmux.service ccboard-ttyd.service ccboard.service; do
  tmp=$(mktemp); render_unit "$u" > "$tmp"
  if ! cmp -s "$tmp" "/etc/systemd/system/$u"; then
    sudo install -m 0644 "$tmp" "/etc/systemd/system/$u"; changed_units+=("$u"); note "wrote /etc/systemd/system/$u"
  fi
  rm -f "$tmp"
done
[ "$need_ttyd" = 0 ] || changed_units+=(ccboard-ttyd.service)
sudo systemctl daemon-reload
sudo systemctl enable --now ccboard-tmux.service ccboard-ttyd.service ccboard.service >/dev/null
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
sudo systemctl restart ccboard.service; restarted+=(ccboard.service)

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
[ "$ok" = 1 ] || { sudo systemctl status ccboard.service --no-pager 2>&1 | tail -20 || true; die "ccboard is not answering on 127.0.0.1:$CCBOARD_PORT"; }
note "restarted: ${restarted[*]}"
printf '\n\033[1;32mccboard is installed.\033[0m\n'
printf '  Dashboard:   https://%s:%s/\n' "$TS_FQDN" "$CCBOARD_HTTPS_PORT"
printf '  code-server: https://%s:%s/\n' "$TS_FQDN" "$CODE_HTTPS_PORT"
[ -z "$NTFY_URL" ] || printf '  ntfy topic:  %s/%s   (subscribe in the ntfy app; iOS needs the app to reach ntfy.sh for wake-ups)\n' "$NTFY_PUBLIC_URL" "$NTFY_TOPIC"
printf '  Open them from another device on your tailnet (requests from this box carry no Tailscale identity).\n'
printf '  Then click "Log in" on the dashboard to sign in to Claude Code.\n'
