#!/usr/bin/env bash
# Push main and update the box, then fail loudly if the board does not come back healthy. The mode is detected on the box:
#  - systemd (default): the box runs ccboard from a git clone: pull, refresh the venv if requirements changed, restart the
#    stateless ccboard service (sudoers rule from install.sh).
#  - docker (a container named ccboard is running, see CCBOARD_RUNTIME=docker in install.sh): GitHub Actions builds
#    ghcr.io/rpandox/ccboard from the push; wait for its sha tag, then compose pull + up -d (Watchtower would do the same
#    within 5 minutes). The clone is pulled too, so the systemd rollback path stays current.
# Usage: scripts/deploy.sh <ssh-host> [remote-dir]     e.g. scripts/deploy.sh ubu2
set -euo pipefail
host=${1:?usage: deploy.sh <ssh-host> [remote-dir]}
dir=${2:-ccboard}
image=ghcr.io/rpandox/ccboard
actions=https://github.com/rpandox/ccboard/actions
cd "$(dirname "${BASH_SOURCE[0]}")/.."
git push -q origin main
sha=$(git rev-parse origin/main); sha=${sha:0:7}      # CI tags the image sha-<7 chars>
# shellcheck disable=SC2029  # the arguments are quoted for the remote shell on purpose
ssh -o BatchMode=yes "$host" "bash -s -- $(printf '%q %q %q %q' "$dir" "$sha" "$image" "$actions")" <<'REMOTE'
set -euo pipefail
dir=$1; sha=$2; image=$3; actions=$4
cd "$dir"
old=$(git rev-parse HEAD); git pull -q; new=$(git rev-parse HEAD)
changed=$(git diff --name-only "$old" "$new" || true)
if grep -q '^requirements.txt$' <<<"$changed"; then
  .venv/bin/pip install -q --disable-pip-version-check -r requirements.txt && echo 'venv updated' || { echo 'FAILED: pip install' >&2; exit 1; }
fi
names=$(docker ps -a --format '{{.Names}}' 2>/dev/null || true)
compose=$HOME/.local/share/ccboard/compose/docker-compose.yml
if grep -qx ccboard <<<"$names"; then mode=docker; else mode=systemd; fi
if [ "$mode" = docker ]; then
  [ -f "$compose" ] || { echo "FAILED: $compose is missing; run CCBOARD_RUNTIME=docker ./install.sh on the box" >&2; exit 1; }
  ref=$image:sha-$sha
  echo "docker mode: waiting for $ref (build: $actions)"
  ok=0
  for i in $(seq 1 30); do
    if out=$(docker manifest inspect "$ref" 2>&1); then ok=1; break; fi
    if grep -qiE 'unauthorized|denied|authentication required' <<<"$out"; then
      echo "FAILED: ghcr.io refuses $ref (private package?). Make the package public in its GitHub settings, or on the box: gh auth refresh -s read:packages && gh auth token | docker login ghcr.io -u <github-user> --password-stdin" >&2; exit 1
    fi
    echo "  not published yet ($i/30), checking again in 20 s"
    sleep 20
  done
  [ "$ok" = 1 ] || { echo "FAILED: $ref was not published within 10 minutes; see $actions (the board keeps running its current image)" >&2; exit 1; }
  docker compose -f "$compose" --profile prod pull
  docker compose -f "$compose" --profile prod up -d
elif sudo -n systemctl restart ccboard.service 2>/dev/null; then echo 'restarted ccboard'
else echo 'NOTE: could not restart without a password; rerun ./install.sh once to add the sudoers rule'; fi
port=$(grep -E '^CCBOARD_PORT=' /etc/ccboard/env 2>/dev/null | cut -d= -f2 || true); port=${port:-8000}
ok=0; for i in $(seq 1 40); do curl -fsS "http://127.0.0.1:$port/healthz" >/dev/null 2>&1 && { ok=1; break; }; sleep 1; done
if [ "$ok" != 1 ]; then
  echo 'FAILED: ccboard did not come back healthy' >&2
  if [ "$mode" = docker ]; then docker logs --tail 15 ccboard 2>&1 | tail -15; else systemctl status ccboard --no-pager 2>&1 | tail -5; fi
  exit 1
fi
echo "deployed $(git log --oneline -1)"
pattern='^(install.sh|systemd/|tmux.conf|scripts/claude_settings.py)'
if [ "$mode" = docker ]; then
  docker ps --filter 'name=^ccboard$' --format 'running {{.Image}} ({{.Status}})'
  pattern='^(install.sh|systemd/|deploy/|tmux.conf|scripts/claude_settings.py)'
fi
if grep -qE "$pattern" <<<"$changed"; then
  echo 'NOTE: install.sh, units, tmux.conf or the Claude settings changed: rerun ./install.sh on the box'
fi
REMOTE
