# deploy/

Container delivery (v0.5.1-docker). GitHub Actions builds `ghcr.io/rpandox/ccboard`; the box pulls it. What each file is:

| File | What it is |
|---|---|
| `../Dockerfile` | The image: Ubuntu 24.04 (same tmux 3.4 as the box), Python venv with the app at `/opt/ccboard`, gh, restic, Node 22 + ccusage, the Tailscale CLI, runs as uid/gid 1000. Claude Code and Codex are **not** in it: the host's `~/.local/bin` binaries run, through the host home mounted at the same path. |
| `../.dockerignore` | Keeps the repo's dev files, caches and this directory out of the build context. |
| `../scripts/docker-entrypoint.sh` | Container start: refuses root, syncs `bin/`, `scripts/` and `tmux.conf` into `$CCBOARD_DATA_DIR/app/` (what the host's hooks, ttyd and MCP run), re-merges the Claude hooks, re-applies `tmux.conf` to the running tmux server, registers the MCP server if missing, then execs uvicorn. `CCBOARD_SHADOW=1` skips every host mutation. |
| `docker-compose.yml` | The production template: `ccboard` (host network, host pid, uid 1000, label-scoped for Watchtower, healthcheck on `/healthz`) and `watchtower` (profile `prod`, only `ccboard`-scoped containers). Installed as `~/.local/share/ccboard/compose/docker-compose.yml` with a `.env` next to it. |
| `docker-compose.shadow.yml` | Override for a side-by-side test run: `ccboard-shadow` on 127.0.0.1:8010 with its own data dir, projects read-only, no hooks, no notifications, no Watchtower. |
| `docker-compose.local.yml` | Not in the repo: written next to the compose file by `scripts/deploy.sh --local-build` (GitHub Actions down). Runs `ccboard-local:<sha>` with `pull_policy: never` and the Watchtower enable label set to `false`; a plain `scripts/deploy.sh <host>` removes it. README, "When GitHub Actions is down". |

## `.env` next to the compose file

| Variable | Default | Meaning |
|---|---|---|
| `CCBOARD_HOME` | `/home/rpandox` | Host home, mounted at the same path inside |
| `CCBOARD_UID`, `CCBOARD_GID` | `1000` | Run as this user; the tmux socket directory is `/tmp/tmux-<uid>` |
| `PROJECTS_DIR` | `/srv/projects` | Mounted at the same path |
| `CCBOARD_IMAGE_TAG` | `latest` | `sha-<7>` or `vX.Y.Z` pins a build |
| `CCBOARD_SHADOW_PORT` | `8010` | Shadow run only |

Every other setting lives in `/etc/ccboard/env` (the compose `env_file`). `docker compose up -d` reads it when it creates the container; Watchtower recreates from the existing container, so after editing the file run `docker compose up -d --force-recreate ccboard`.

## Commands (from `~/.local/share/ccboard/compose/`)

```sh
docker compose --profile prod up -d                      # board + Watchtower
docker compose pull ccboard && docker compose up -d ccboard
docker compose logs -f ccboard
docker compose -f docker-compose.yml -f docker-compose.shadow.yml up -d ccboard    # shadow run
docker compose -f docker-compose.yml -f docker-compose.shadow.yml down
```

## Things to know

- The compose file mounts `/tmp/tmux-<uid>`, `/var/run/tailscale`, `/etc/ccboard` and `~/.docker/config.json` with `create_host_path: false`: a missing source fails the start instead of Docker creating a root-owned directory. Create `~/.docker/config.json` (`echo '{}' > ~/.docker/config.json`) or drop that mount when no registry login is needed.
- `/var/run/tailscale` is mounted as a directory so the container keeps working after `tailscaled` restarts.
- Watchtower watches only containers labelled `com.centurylinklabs.watchtower.enable=true` in scope `ccboard`. It never updates itself or any other container on the box.
- If `docker exec ccboard ss -ltnp` lists no `pid=` for dev servers started in sessions (preview links need it), the AppArmor `docker-default` profile is blocking reads of other processes' `/proc/<pid>/fd`; add `security_opt: ["apparmor=unconfined"]` to the `ccboard` service.
- The image is built only by CI. A local `docker build -t ccboard-local:test .` is fine to check that the Dockerfile builds; do not run it with the host mounts.
