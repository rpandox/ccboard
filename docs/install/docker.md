# Run the board as a container

For people running the board as a Docker container on a Linux host.

[Back to the documentation index](../README.md)

## Run the board as a container (optional)

`CCBOARD_RUNTIME=docker ./install.sh` runs the dashboard as a Docker container instead of `ccboard.service`, so that **a `git push` to `main` is the deploy**: GitHub Actions runs the tests and builds `ghcr.io/rpandox/ccboard`, and [Watchtower](https://github.com/nicholas-fedor/watchtower) on the box pulls the new image within five minutes. systemd mode stays the default and the fallback; nothing below is needed to use ccboard.

**What moves into the container.** Only the board's own process: the FastAPI app, its pollers and scheduler, the nightly backup run, and `Back up now`. It runs as your uid and gid (it refuses to run as root), on the host network and the host pid namespace, with these host paths mounted at the same location: your home (so `~/.claude`, `~/.codex`, `~/.ssh`, `~/.config/gh` and the host's `~/.local/bin/{claude,codex,ccusage}` are what it sees; the host binaries come first on `PATH`), `PROJECTS_DIR`, `/tmp/tmux-<uid>` (the tmux socket), `/var/run/tailscale` (for `tailscale serve`) and `/etc/ccboard` read-only (every setting still lives in `/etc/ccboard/env`, which the compose file passes in as the container's environment).

**What stays on the host.** `ccboard-tmux.service` (the tmux server that owns every session; install.sh never restarts it, so sessions survive every switch, update and rollback), `ccboard-ttyd`, code-server, ntfy, Tailscale and its `serve` mappings (still `127.0.0.1:CCBOARD_PORT` and `/tty`), the backup timer (its unit now runs `docker exec ccboard ... python -m app.backup`), Docker, and the Claude Code / Codex logins and binaries. The scripts the host runs (Claude hooks, statusline, the ttyd attach wrapper, the MCP server) come from `$CCBOARD_DATA_DIR/app/{bin,scripts}`, which the container refreshes from the image on every start and which install.sh seeds once from the checkout; the hooks, the ttyd unit and the backup unit are pointed there.

Requirements on top of the install requirements: Docker Engine with the compose plugin, and your user in the `docker` group. Run it in three steps; each can be undone, and only the last one changes the running box.

### Where the container runtime works

Container mode is Linux only. The container is just the board's process: the tmux server, ttyd, code-server and Tailscale stay on the host, and the container reaches them through the host network, the host pid namespace, the host's home directory and bind-mounted unix sockets. `install.sh` refuses `CCBOARD_RUNTIME=docker` where that cannot work, and the doctor's `runtime-host` check fails a container that was started there anyway.

| Host | Container runtime | Why |
|---|---|---|
| Linux host with Docker Engine | yes | The case this section describes. |
| Ubuntu in WSL2 with docker-ce installed inside that distro | yes | The engine is the distro's own, so it behaves like Linux. The installer checks `docker info --format '{{.OperatingSystem}}'` and lets a distro engine through. |
| Docker Desktop on macOS | no | The containers run in Docker Desktop's Linux virtual machine, not on the Mac. A host unix socket (tmux, Tailscale) does not cross the hypervisor, `pid: host` means the VM's processes, and the Mac's `claude` and `codex` are not Linux programs. |
| Docker Desktop on Windows (WSL2 integration) | no | Docker Desktop runs containers in its own `docker-desktop` distro, apart from your Ubuntu distro, so the tmux socket and `pid: host` point at the wrong place. The installer stops with the reason. |

An arm64 image does not make Docker Desktop supported: the limit is the virtual machine, not the processor. On a Mac use the launchd runtime and on Windows use WSL2 with the default systemd runtime or docker-ce inside the distro (see [macOS](macos.md#macos-issue-117) and [Windows (WSL2)](windows-wsl2.md#windows-wsl2-issue-118)). The `linuxkit` kernel signal the doctor reads is to verify on a Mac and on Windows.

### A. Build in GitHub (nothing on the box changes)

Push to `main`. The `ci` workflow runs the tests, builds the image, **starts it and checks it before anything is published**, and then publishes `ghcr.io/rpandox/ccboard` with the tags `latest` (main), `sha-<7 chars>` and `vX.Y.Z` (when the commit subject starts with it). The check (`scripts/ci-smoke.sh`, also runnable by hand against a local `docker build -t ccboard:ci .`) starts the image as the non-root board user on a high port with temp directories for home, data, projects and the tmux socket, waits up to 60 s for `/healthz`, and runs `import app.main`, `ccusage --version`, `tmux -V` and `gh --version` inside it; a failure stops the run with the container's logs and `latest` does not move. It proves the image starts and carries its tools; it does not exercise real tmux sessions, Tailscale, sign-in or the box's mounts. Pull requests build the amd64 image without pushing it. Watch the run under Actions, then check that the package exists.

If the package comes out private (an image linked to a public repository is published public, as happened here; check the package page), pick one:

1. Make it public: GitHub, your profile, Packages, `ccboard`, Package settings, Change visibility, Public (the repository is already public).
2. Keep it private and log the box in (the same login is what Watchtower uses, through `~/.docker/config.json`):

   ```sh
   gh auth refresh -s read:packages && gh auth token | docker login ghcr.io -u <github-user> --password-stdin
   ```

### B. Shadow run (no sudo, nothing of the live board touched)

Starts the image next to the live board as a container of its own, from a compose file of its own (`deploy/docker-compose.shadow.yml`; use it alone, never together with the base file). What it has and what it does not:

- **Its own data and HOME**: `~/.local/share/ccboard-shadow` (a fresh database and its own hook token, so no scheduled job runs twice and no hook ever reaches it), with the container's HOME inside that folder. That folder is its only writable host mount.
- **The projects, read-only.** Nothing else of the host is mounted: not your home (so no `~/.claude`, `~/.codex`, `~/.ssh` or host binaries), not the tmux socket, not the Tailscale socket, not `/etc/ccboard`, not the Docker socket.
- **A bridge network with no published port, and no host pid namespace.** Nothing listens on the host. The board inside binds the container's own loopback, so you look at it with `docker exec ccboard-shadow curl ...`; it cannot be opened in a browser.
- **None of the live settings**: no `env_file`, so no identity list, hub token, ntfy address, node list or backup repository comes along; the claude-mem worker, restic, session recovery and notifications are off, and `CCBOARD_SHADOW=1` makes the entrypoint skip every host change (hooks, tmux.conf, MCP registration). Its compose project is its own, so `down` can never touch the real one.

Because it has no tmux and no Claude or Codex login, it shows a board with `tmux_down: true`, the projects as folders and whatever data you give it; it is not a copy of the live board's sessions. (An earlier version of this file was an overlay on the base file and still mounted the live home read-write, the tmux and Tailscale sockets and ran on the host network and pid namespace; the restore drill of 2026-10-09 found that, and this file replaced it.) On the box:

```sh
cd ~/ccboard && git pull
export CCBOARD_HOME=$HOME CCBOARD_UID=$(id -u) CCBOARD_GID=$(id -g) PROJECTS_DIR=/srv/projects
mkdir -p ~/.local/share/ccboard-shadow/home          # a missing folder fails the start instead of becoming a root-owned one
docker compose -f deploy/docker-compose.shadow.yml up -d

docker exec ccboard-shadow curl -fs http://127.0.0.1:8010/healthz
TOKEN=$(cat ~/.local/share/ccboard-shadow/hook-token)
docker exec ccboard-shadow curl -fs -H "X-CCBoard-Token: $TOKEN" http://127.0.0.1:8010/api/state | head -c 400   # projects from the folders, tasks 0, tmux_down true
docker exec ccboard-shadow ccusage --version
tmux -L ccboard list-sessions                                                  # unchanged

docker compose -f deploy/docker-compose.shadow.yml down
rm -r ~/.local/share/ccboard-shadow
```

To try restored data in it, see [Restoring from a backup](../backup-and-restore.md#restoring-from-a-backup).

### C. Switch (you run it; it needs sudo)

A watchdog line in your crontab (`scripts/ccboard-watchdog.sh`, every 2 minutes, no sudo) starts the container when Watchtower's restart did not take on a busy box and restarts it when it stays unhealthy; `crontab -l | grep ccboard-watchdog` shows it, and `~/.local/share/ccboard/watchdog.log` what it did.


```sh
ssh -t <host> 'cd ccboard && git pull && CCBOARD_RUNTIME=docker ./install.sh'
```

In this order, with nothing on the running system changed until the image is on the box:

1. Checks `docker`, `docker compose` and your docker group membership, and says how to fix what is missing.
2. Writes `~/.local/share/ccboard/compose/docker-compose.yml` (plus the shadow override beside it) and its `.env` (`CCBOARD_HOME`, `CCBOARD_UID`, `CCBOARD_GID`, `PROJECTS_DIR`, `CCBOARD_IMAGE_TAG`; a tag you pinned, for example `CCBOARD_IMAGE_TAG=sha-1a2b3c4 ./install.sh`, is kept on reruns), seeds `$CCBOARD_DATA_DIR/app` from the checkout, and runs `docker compose --profile prod pull`. If the pull fails (private package, image not built yet) it prints the two remedies from step A and stops.
3. Does everything the systemd install does (apt, ttyd, code-server, ntfy, restic, the venv in the checkout, which the rollback uses), except that the units and the Claude hooks and MCP registration point at `$CCBOARD_DATA_DIR/app`, the backup unit becomes `docker exec`, `ccboard.service` is installed but not enabled, and the sudoers rule keeps only `systemctl restart ccboard-ttyd` and `tailscale serve *`.
4. `sudo tailscale set --operator=<you>` (previews run `tailscale serve` without sudo), then `systemctl disable --now ccboard.service` and straight away `docker compose --profile prod up -d`, then waits up to 60 s for `/healthz`. If the board does not come up it prints the rollback and exits 1. Hook calls during the few seconds of the switch fail silently.

`CCBOARD_RUNTIME=docker` is remembered in `/etc/ccboard/env`, so a plain `./install.sh` rerun stays in docker mode. Afterwards check `docker compose -f ~/.local/share/ccboard/compose/docker-compose.yml --profile prod ps`, open the board from another tailnet device, start a Claude session and see its events arrive, and compare `tmux -L ccboard list-sessions` with what it was before.

**Rollback.** Sessions are never affected:

```sh
docker compose -f ~/.local/share/ccboard/compose/docker-compose.yml --profile prod down
cd ~/ccboard && CCBOARD_RUNTIME=systemd ./install.sh
```

The second command restarts `ccboard.service` and points the ttyd and backup units, the hooks and the sudoers rule (the MCP registration keeps working from `$CCBOARD_DATA_DIR/app`) back at the checkout. Without the installer: `sudo sed -i 's/^CCBOARD_RUNTIME=.*/CCBOARD_RUNTIME=systemd/' /etc/ccboard/env && sudo systemctl enable --now ccboard.service` (the backup timer keeps trying `docker exec` until you rerun install.sh). Run `down` first: the container holds the board's port, and install.sh refuses a port that something else holds.

**Limitations.**

- **Devcontainer sessions work in docker mode; what they need is on the host, not in the board's container.** "Run in the devcontainer" types one line into the session's tmux window: `devcontainer up --workspace-folder <repo> && devcontainer exec --workspace-folder <repo> -- claude ...` (`-- bash -l` for a shell). The tmux server is the host's (the container only reaches its socket), so the line runs on the host, where docker and the `devcontainer` CLI are. The board process itself never calls docker: all it does is check that `.devcontainer/devcontainer.json` exists, which it can because `PROJECTS_DIR` is mounted at the same path. So the board's container gets no docker CLI and no docker socket (and should not: the socket is root-equivalent). What the host needs: Docker, your user in the `docker` group, and the `devcontainer` CLI (`CCBOARD_DEVCONTAINER=1 ./install.sh`, or `npm install -g --prefix ~/.local @devcontainers/cli`). Without the CLI the launch is refused before anything is typed: the board answers **409** naming the fix (install the CLI, or launch without the devcontainer), using the same probe as the Doctor check, instead of typing a line the pane can only answer with "command not found". Settings > Doctor has a **Devcontainer prerequisites** check (group Box) that looks for them when a repo has a devcontainer; in docker mode it can only see `~/.local/bin/devcontainer` from inside the container, and says so. Checked on the box on 2026-10-09 by reading the code and the prerequisites (docker 29 and the docker group present, no `devcontainer` CLI yet, no docker socket in the board's container); no devcontainer was started there, so a live launch is still to do. `tests/test_devcontainer.py` pins the typed line. **This is also the only place a Claude session may bypass permissions** (owner decision on #104, v0.5.62): with the devcontainer on, the launcher offers *bypass* behind the acknowledgement and the line becomes `devcontainer up ... && devcontainer exec ... -- claude ... --permission-mode bypassPermissions`; without it, or for Codex, the board answers 400 and starts nothing. Mounting the docker socket into the board's container is not part of this and stays off. A second, separate limit: inside the devcontainer `claude` has its own `~/.claude` (log in once there) and the board's hooks only if the image installs them and can reach the board (a default devcontainer is on a bridge network, where `127.0.0.1:8000` is the container itself); without hooks the board shows the session with no state and no permission cards.
- Task previews need operator mode: inside the container there is no `sudo`, so `tailscale serve` runs through the mounted socket as the operator. install.sh sets `tailscale set --operator=<you>` in docker mode; see [the security notes](../security.md#security-model) for what that allows. Without it the preview button reports the error.
- If a preview button finds no listening port for a dev server, see the AppArmor note in `deploy/README.md`.
- The image is a multi-arch manifest, `linux/amd64` and `linux/arm64` (`latest`, `sha-<7>` and the version tag each list both; check with `docker buildx imagetools inspect ghcr.io/rpandox/ccboard:latest`). The arm64 half is built under QEMU in the same CI job and its `ccusage --version` build step runs there; it has not been started on a real ARM machine yet. Claude Code, Codex and ccusage are not in the image as the board uses them: it runs the host's binaries from `~/.local/bin`, so they update and log in on the host as before and must match the host's architecture. This does not make Docker Desktop on a Mac or Windows machine a supported host, and the `install.sh` [platform note](linux.md#install) is a separate statement.
- The container can read and write everything your user can (it mounts your home), exactly like the systemd unit that runs as you. Docker group membership is root-equivalent on the box.
- `CCBOARD_DATA_DIR` must be under your home directory (the only thing besides `PROJECTS_DIR` that is mounted).
- Under docker, `Back up now` runs a detached process inside the container (log in `<data dir>/backup.log`) rather than the systemd unit; the nightly timer still runs the unit, which `docker exec`s into the container.
