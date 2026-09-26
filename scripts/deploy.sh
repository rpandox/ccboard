#!/usr/bin/env bash
# Push main and update a box that runs ccboard from a git clone: pull, refresh the venv if
# requirements changed, restart the stateless ccboard service (sudoers rule from install.sh),
# and fail loudly if the board does not come back healthy.
# Usage: scripts/deploy.sh <ssh-host> [remote-dir]     e.g. scripts/deploy.sh ubu2
set -euo pipefail
host=${1:?usage: deploy.sh <ssh-host> [remote-dir]}
dir=${2:-ccboard}
cd "$(dirname "${BASH_SOURCE[0]}")/.."
git push -q origin main
ssh -o BatchMode=yes "$host" "bash -s" <<REMOTE
set -euo pipefail
cd '$dir'
old=\$(git rev-parse HEAD); git pull -q; new=\$(git rev-parse HEAD)
changed=\$(git diff --name-only "\$old" "\$new" || true)
if echo "\$changed" | grep -q '^requirements.txt\$'; then
  .venv/bin/pip install -q --disable-pip-version-check -r requirements.txt && echo 'venv updated' || { echo 'FAILED: pip install' >&2; exit 1; }
fi
if sudo -n systemctl restart ccboard.service 2>/dev/null; then echo 'restarted ccboard'
else echo 'NOTE: could not restart without a password; rerun ./install.sh once to add the sudoers rule'; fi
port=\$(grep -E '^CCBOARD_PORT=' /etc/ccboard/env 2>/dev/null | cut -d= -f2); port=\${port:-8000}
ok=0; for i in 1 2 3 4 5 6 7 8 9 10; do curl -fsS "http://127.0.0.1:\$port/healthz" >/dev/null 2>&1 && { ok=1; break; }; sleep 1; done
[ "\$ok" = 1 ] || { echo 'FAILED: ccboard did not come back healthy' >&2; systemctl status ccboard --no-pager 2>&1 | tail -5; exit 1; }
echo "deployed \$(git log --oneline -1)"
if echo "\$changed" | grep -qE '^(install.sh|systemd/|tmux.conf|scripts/claude_settings.py)'; then
  echo 'NOTE: install.sh, units, tmux.conf or the Claude settings changed: rerun ./install.sh on the box'
fi
REMOTE
