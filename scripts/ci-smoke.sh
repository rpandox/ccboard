#!/usr/bin/env bash
# Smoke-test a built ccboard image before CI pushes it (the `latest` tag is what the box follows).
# Usage: scripts/ci-smoke.sh [image]        default image: ccboard:ci  (docker build -t ccboard:ci .)
#
# Starts the image as the non-root board user (1000:1000) on a high port, with temp directories standing in for the
# host's home, data dir, projects dir and tmux socket dir. No real credentials, no real home, no port 443. It proves the
# image starts through its entrypoint, serves /healthz, imports the app and carries the CLIs the board shells out to.
# It does not exercise real tmux sessions, Tailscale, sign-in or the box's mounts.
# The container and the temp directories are removed on every exit path; the container's logs are printed on failure.
set -euo pipefail

image=${1:-ccboard:ci}
port=${CCBOARD_SMOKE_PORT:-18000}
wait_s=${CCBOARD_SMOKE_WAIT:-60}
name=ccboard-smoke
work=$(mktemp -d)
rc=1

cleanup() {
  trap - EXIT
  if [ "$rc" != 0 ]; then
    echo "--- container logs (the container holds no real data) ---"
    docker logs --tail 80 "$name" 2>&1 || true
  fi
  docker rm -f "$name" >/dev/null 2>&1 || true
  # Files written by uid 1000 inside the container may not belong to the runner user: retry as root inside a throwaway container.
  rm -rf "$work" 2>/dev/null || true
  if [ -e "$work" ]; then
    docker run --rm --user 0 --entrypoint rm -v "$work:/w" "$image" -rf /w/home /w/data /w/projects /w/tmux >/dev/null 2>&1 || true
    rm -rf "$work" 2>/dev/null || true
  fi
  exit "$rc"
}
trap cleanup EXIT

docker rm -f "$name" >/dev/null 2>&1 || true
mkdir -p "$work/home" "$work/data" "$work/projects" "$work/tmux"
chmod 0777 "$work" "$work/home" "$work/data" "$work/projects" "$work/tmux"

echo "smoke: starting $image as 1000:1000 on 127.0.0.1:$port"
docker run -d --name "$name" --network host --user 1000:1000 \
  -e "CCBOARD_PORT=$port" -e CCBOARD_ALLOWED_USERS=ci \
  -e HOME=/work/home -e CCBOARD_DATA_DIR=/work/data -e PROJECTS_DIR=/work/projects -e CCBOARD_RECOVER=0 \
  -e NTFY_URL= -e NTFY_PUBLIC_URL= -e CCBOARD_NODES= \
  -v "$work/home:/work/home" -v "$work/data:/work/data" -v "$work/projects:/work/projects" \
  -v "$work/tmux:/tmp/tmux-1000" \
  "$image" >/dev/null

ok=0
for i in $(seq 1 "$wait_s"); do
  if curl -fs "http://127.0.0.1:$port/healthz" >/dev/null 2>&1; then ok=1; break; fi
  if [ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null || echo false)" != true ]; then
    echo "smoke: FAILED: the container exited before answering /healthz" >&2
    exit_code=$(docker inspect -f '{{.State.ExitCode}}' "$name" 2>/dev/null || echo '?')
    echo "smoke: exit code $exit_code" >&2
    exit 1
  fi
  sleep 1
done
if [ "$ok" != 1 ]; then
  echo "smoke: FAILED: /healthz did not answer within ${wait_s}s" >&2
  exit 1
fi
echo "smoke: /healthz answered after ~${i}s"

check() { # description, command...
  desc=$1; shift
  echo "smoke: docker exec $desc"
  docker exec "$name" "$@" || { echo "smoke: FAILED: $desc" >&2; exit 1; }
}
check "import app.main" python -c 'import app.main'
check "ccusage --version" ccusage --version
check "tmux -V" tmux -V
check "gh --version" gh --version

rc=0
echo "smoke: ok"
