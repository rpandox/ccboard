#!/usr/bin/env bash
# Push main and update a box that runs ccboard from a git clone: pull, refresh the venv if
# requirements changed, restart the stateless ccboard service (sudoers rule from install.sh).
# Usage: scripts/deploy.sh <ssh-host> [remote-dir]     e.g. scripts/deploy.sh ubu2
set -euo pipefail
host=${1:?usage: deploy.sh <ssh-host> [remote-dir]}
dir=${2:-ccboard}
cd "$(dirname "${BASH_SOURCE[0]}")/.."
git push -q origin main
ssh -o BatchMode=yes "$host" "set -e; cd '$dir'; old=\$(git rev-parse HEAD); git pull -q; new=\$(git rev-parse HEAD);
  changed=\$(git diff --name-only \"\$old\" \"\$new\");
  echo \"\$changed\" | grep -q '^requirements.txt$' && .venv/bin/pip install -q --disable-pip-version-check -r requirements.txt && echo 'venv updated' || true;
  if sudo -n systemctl restart ccboard.service 2>/dev/null; then echo 'restarted ccboard'; else echo 'NOTE: could not restart without a password; rerun ./install.sh once to add the sudoers rule'; fi;
  for i in 1 2 3 4 5 6 7 8 9 10; do curl -fsS http://127.0.0.1:\${CCBOARD_PORT:-8000}/healthz >/dev/null 2>&1 && break; sleep 1; done;
  echo \"deployed \$(git log --oneline -1)\";
  if echo \"\$changed\" | grep -qE '^(install.sh|systemd/|tmux.conf|scripts/claude_settings.py)'; then echo 'NOTE: install.sh, units, tmux.conf or the Claude settings changed: rerun ./install.sh on the box'; fi"
