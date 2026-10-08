#!/usr/bin/env bash
# Push main and update the box, then fail loudly if the board does not come back healthy. The mode is detected on the box:
#  - systemd (default): the box runs ccboard from a git clone: pull, refresh the venv if requirements changed, restart the
#    stateless ccboard service (sudoers rule from install.sh).
#  - docker (a container named ccboard is running, see CCBOARD_RUNTIME=docker in install.sh): GitHub Actions builds
#    ghcr.io/rpandox/ccboard from the push; wait for its sha tag, then compose pull + up -d (Watchtower would do the same
#    within 5 minutes). The clone is pulled too, so the systemd rollback path stays current.
#  - docker + --local-build (GitHub Actions is down): still push main, but build the image on the box from the pulled
#    checkout (ccboard-local:<sha>) and run it through a compose override that turns Watchtower off for the container.
#    The open-terminal deploy gate is asked first (exit 75 = hold; --force goes on anyway). Running this script again
#    WITHOUT the flag removes the override and returns to the registry image (README, "When GitHub Actions is down").
# Usage: scripts/deploy.sh [--local-build] [--force] <ssh-host> [remote-dir]     e.g. scripts/deploy.sh ubu2
set -euo pipefail
localbuild=0; force=0; pos=()
for a in "$@"; do
  case "$a" in
    --local-build) localbuild=1 ;;
    --force) force=1 ;;
    -*) echo "unknown option $a; usage: deploy.sh [--local-build] [--force] <ssh-host> [remote-dir]" >&2; exit 2 ;;
    *) pos+=("$a") ;;
  esac
done
if [ "$force" = 1 ] && [ "$localbuild" != 1 ]; then echo '--force only applies to --local-build' >&2; exit 2; fi
host=${pos[0]:?usage: deploy.sh [--local-build] [--force] <ssh-host> [remote-dir]}
dir=${pos[1]:-ccboard}
image=ghcr.io/rpandox/ccboard
actions=https://github.com/rpandox/ccboard/actions
cd "$(dirname "${BASH_SOURCE[0]}")/.."
git push -q origin main
full=$(git rev-parse origin/main); sha=${full:0:7}      # CI tags the image sha-<7 chars>
# shellcheck disable=SC2029  # the arguments are quoted for the remote shell on purpose
ssh -o BatchMode=yes "$host" "bash -s -- $(printf '%q %q %q %q %q %q %q' "$dir" "$sha" "$image" "$actions" "$localbuild" "$force" "$full")" <<'REMOTE'
set -euo pipefail
dir=$1; sha=$2; image=$3; actions=$4; localbuild=$5; force=$6; full=$7
cd "$dir"
old=$(git rev-parse HEAD); git pull -q; new=$(git rev-parse HEAD)
changed=$(git diff --name-only "$old" "$new" || true)
if grep -q '^requirements.txt$' <<<"$changed"; then
  .venv/bin/pip install -q --disable-pip-version-check -r requirements.txt && echo 'venv updated' || { echo 'FAILED: pip install' >&2; exit 1; }
fi
names=$(docker ps -a --format '{{.Names}}' 2>/dev/null || true)
compose=$HOME/.local/share/ccboard/compose/docker-compose.yml
override=$(dirname "$compose")/docker-compose.local.yml
modefile=$(dirname "$(dirname "$compose")")/deploy-mode      # <data dir>/deploy-mode: the line Doctor can read later
if grep -qx ccboard <<<"$names"; then mode=docker; else mode=systemd; fi
if [ "$localbuild" = 1 ] && [ "$mode" != docker ]; then
  echo 'FAILED: --local-build is for the docker mode only (no container named ccboard on the box)' >&2; exit 1
fi
runmode=
if [ "$mode" = docker ] && [ "$localbuild" = 1 ]; then
  [ -f "$compose" ] || { echo "FAILED: $compose is missing; run CCBOARD_RUNTIME=docker ./install.sh on the box" >&2; exit 1; }
  head=$(git rev-parse HEAD)
  case "$head" in "$sha"*) ;; *) echo "FAILED: the checkout on the box is at ${head:0:7}, not the pushed $sha; nothing was built" >&2; exit 1 ;; esac
  # 1. The deploy gate first: exit 75 = someone is at a terminal, a permission is pending or a clone runs.
  gate=0; docker exec ccboard /opt/ccboard/scripts/ccboard-deploy-gate || gate=$?
  if [ "$gate" = 75 ] && [ "$force" != 1 ]; then
    echo 'HOLD: the deploy gate says not now (an open terminal, a pending permission or a clone). Nothing was built; the board keeps running. Rerun with --force to go on anyway.' >&2; exit 1
  fi
  [ "$gate" = 75 ] && echo 'NOTE: the deploy gate holds, going on because of --force'
  [ "$gate" = 0 ] || [ "$gate" = 75 ] || echo "NOTE: the deploy gate answered $gate (is the container running?); going on"
  # 2. Build under nice/ionice. A failed build leaves the current container and compose files untouched.
  echo "local build ccboard-local:$sha: this can take many minutes on a loaded box (Ctrl-C is safe, nothing is swapped yet)"
  wrap=(); command -v nice >/dev/null 2>&1 && wrap+=(nice -n 19); command -v ionice >/dev/null 2>&1 && wrap+=(ionice -c3)
  SECONDS=0
  ${wrap[@]+"${wrap[@]}"} docker build --build-arg "CCBOARD_VERSION=local-$sha" --build-arg "CCBOARD_REVISION=$head" -t "ccboard-local:$sha" . \
    || { echo "FAILED: docker build (the board keeps running its current image)" >&2; exit 1; }
  echo "built ccboard-local:$sha in ${SECONDS}s"
  # 3. Run it through an override: Watchtower leaves a container whose enable label is false, so it cannot pull `latest` over it.
  cat > "$override" <<OVERRIDE
# written by scripts/deploy.sh --local-build; removed by a plain scripts/deploy.sh (back to the registry image)
services:
  ccboard:
    image: ccboard-local:$sha
    pull_policy: never
    labels:
      com.centurylinklabs.watchtower.enable: "false"
OVERRIDE
  docker compose -f "$compose" -f "$override" --profile prod up -d ccboard
  runmode="running local build $sha"
elif [ "$mode" = docker ]; then
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
  if [ "$ok" != 1 ]; then
    echo "FAILED: $ref was not published within 10 minutes; see $actions (the board keeps running its current image)" >&2
    [ -f "$override" ] && echo "NOTE: the box is still running its local build; $override is kept. Use scripts/deploy.sh --local-build to ship this commit anyway." >&2
    exit 1
  fi
  rm -f "$override"      # back to the registry image: the override goes only once the tag exists, so a missing tag leaves the local build intact
  docker compose -f "$compose" --profile prod pull
  docker compose -f "$compose" --profile prod up -d
  runmode="running $ref"
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
if [ -n "$runmode" ]; then
  echo "$runmode"
  printf '%s\n' "$runmode" > "$modefile" || echo "NOTE: could not write $modefile" >&2
fi
pattern='^(install.sh|systemd/|tmux.conf|scripts/claude_settings.py)'
if [ "$mode" = docker ]; then
  docker ps --filter 'name=^ccboard$' --format 'running {{.Image}} ({{.Status}})'
  pattern='^(install.sh|systemd/|deploy/|tmux.conf|scripts/claude_settings.py)'
fi
if grep -qE "$pattern" <<<"$changed"; then
  echo 'NOTE: install.sh, units, tmux.conf or the Claude settings changed: rerun ./install.sh on the box'
fi
REMOTE
