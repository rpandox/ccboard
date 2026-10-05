# ccboard

A self-hosted dashboard for **Claude Code CLI sessions** (and, since v0.5.11, **Codex CLI sessions**: see [Codex](#codex)) on your own Linux box, reachable only over your Tailscale network.

ccboard is a *status-and-attention layer*. It launches the real `claude` TUI inside tmux, shows you what is running where, lets you attach from any device (including your phone), and tells you when a session needs you. It never reimplements Claude or its chat: every Claude Code feature (slash commands, MCP, hooks, plan mode, resume, skills, worktrees) works untouched because the real terminal is what runs.

```
 browser (any tailnet device)
   │ https://box.tailnet.ts.net:443/      https://box.tailnet.ts.net:8443/
   ▼                                        ▼
 tailscale serve ──── /    ──► ccboard (FastAPI, 127.0.0.1:8000)
                 └─── /tty ──► ttyd (127.0.0.1:7681) ──► tmux attach ─┐
                                                                       ▼
                                                        ccboard-tmux server (-L ccboard)
                                                          shop--api--s1   : claude
                                                          shop--web--s2   : claude --continue
                                                          blog--site--s1  : shell
                                                                       ▲
                                        code-server (127.0.0.1:8080) ──┘ (same repos, same user)
```

## What v0.1 does

- **Projects and repos.** A project is a folder under `/srv/projects/<project>/`; each repo is a git repo in a subfolder (`/srv/projects/shop/api`, `/srv/projects/shop/web`). One-repo projects have one subfolder. Create a project, add repos (blank `git init` or clone from a URL), remove repos, delete projects (kills their sessions and removes the folder). Each repo shows branch and dirty state.
- **Sessions.** Unlimited, all concurrent, one tmux session each, always started *inside a repo*. Launchers: `claude` (new session with a board-minted `--session-id`), `claude --resume`, `claude --continue`, and a plain `shell`. Extra CLI args are passed through. **One session can edit several repos:** the launch form pre-checks the sibling repos of the same project and passes them as `--add-dir`.
- **Attach** from the browser through ttyd, **kill** from the board.
- **Open in code-server**, per repo or per project (all repos in one explorer).
- **Claude login from the board.** Click *Log in*: ccboard runs `claude auth login` in a tmux session, shows you the sign-in link, and you paste the code the browser gives you back into the board. Nothing is scraped from transcripts; the real CLI does the login.
- **Session state from Claude Code hooks.** `install.sh` registers async hooks and a statusLine command in `~/.claude/settings.json`; each session shows idle / working / needs you / done / error, the last prompt, the last line Claude printed, its model and context usage. Nothing reads transcripts.
- **One idempotent `install.sh`** for Ubuntu 22.04 / 24.04 (amd64, arm64): apt deps, ttyd, code-server, Claude Code, venv, three systemd units, tailscale serve mappings, Claude hooks. Rerun it any time.

## What v0.2 adds: the attention layer

- **Needs-attention inbox** at the top: sessions waiting for you, done, or errored, oldest first. `j`/`k` move, `Enter` attaches, `a` acks, `y`/`n` allow or deny a pending permission. The tab title shows the count.
- **Usage strip**: 5-hour and weekly limits with reset countdowns (from Claude's statusline), the active ccusage block (cost, burn rate, projection), and a chip per session with model and context %. A red banner when Claude reports a rate limit.
- **Push notifications** two ways: a self-hosted **ntfy** server on your tailnet (installed by `install.sh`, subscribe in the ntfy app to the URL shown under 🔔) and **Web Push** for the installed PWA ("Enable push on this device"). Pushes fire on needs-you, done, error, rate limit and a login that stopped working (each one a switch under Settings › Notifications), with a deep link and buttons (Allow and Deny for a permission, else Terminal and Ack). The ntfy server is tailnet-only, so the phone needs the Tailscale VPN on; on an iPhone the box relays a content-free wake-up through ntfy.sh and the app then fetches the message from your server (the 🔔 panel lists the exact steps).
- **Remote approve/deny**: when Claude asks for permission and nobody is attached to that terminal, the board pushes a notification with **Allow / Deny** buttons and holds the prompt for up to 90 s (`CCBOARD_APPROVE_TIMEOUT`). Answer from the phone, the inbox, or the `y`/`n` keys; with no answer the normal TUI prompt appears. If someone is attached, the TUI prompt appears immediately.
- **PWA**: installable (Add to Home Screen), works offline showing the last known state.
- **Mobile terminal** `/term/<session>`: the terminal with a header (project, session, state, model, context %), a collapsible strip with the task, the last prompt and what Claude is asking, a 44 px key bar (Esc, Tab, Shift+Tab, Ctrl-C, Enter, arrows, ⌫, PgUp, PgDn, Top, Bottom, Ctrl+O), swipe and button scrolling that works on a phone, A−/A+ font size, editable quick replies and a multi-line text box that rides above the soft keyboard. Attach links open it.
- **Live** grid: the last 20 lines of every session, refreshed every 2 s over SSE.
- **Reboot recovery**: after a reboot every Claude session is relaunched in its repo with `claude --resume <id>`.
- **Import from GitHub**: pick repos from `gh repo list`, clone them into a project three at a time.

## What v0.3 adds: git & tasks

- **Tasks**: "New task" on a repo takes a title and a prompt (or picks an open GitHub issue) and starts Claude in its own git worktree and branch (`claude --worktree`, `.worktreeinclude` honoured). The Tasks board has columns Backlog / In progress / Needs you / Done / PR open / Merged, derived from the hook state and the PR (see *Tasks* under *Using it* for Now / Later / Schedule and the Backlog).
- **Diff / PR**: a modal shows the branch commits and diff (diff2html), "Describe with Claude" writes the PR title and body with a one-turn `claude -p` over the diff, "Create PR" pushes and runs `gh pr create`, "Merge (squash) & archive" pushes, verifies origin has your HEAD, merges with `gh pr merge --squash --delete-branch` and removes the worktree.
- **PR / CI status** on cards (polled with `gh` every minute) and **Fix CI**, which pastes the failing job log into the task's Claude session (relaunching it in the worktree if needed).
- **Overlap warning** when two open tasks of a repo touch the same files.
- **Cost** per project (today / 7 days / total), repo and task, joined from `ccusage session --json` on the Claude session ids the board recorded. Sessions started outside the board are not attributed.
- **Transcript search**: the header box searches an FTS5 index over `~/.claude/projects` transcripts (display only; hits link to the session when known).

## What v0.4 adds: autonomy & fleet

- **Schedules**: cron or one-off jobs run headless in a fresh worktree, with Claude (`claude -p`) or, since v0.5.16, Codex (`codex exec --json`; see *Headless Codex runs* under *Codex*). At most 2 run at a time; each agent is deferred while its own window is above 85 % (Claude: the 5-hour window; Codex: its usage window) and for 30 minutes (or until its window resets) after one of its runs comes back rate-limited; each result becomes a task card you can resume in a terminal. Claude's percentage comes from the statusline of interactive sessions, so on a box that only runs headless Claude jobs it stays unknown (the Schedules header says so) and only the back-off protects you; Codex's comes from the rollout tailer.
- **Batch prompt** across the repos you pick, drained by the same scheduler, with per-batch progress.
- **MCP server**: `scripts/ccboard_mcp.py` is registered at user scope by `install.sh`, so any Claude session on the box can call `list_projects`, `create_task`, `list_tasks` and `get_task_status`.
- **Preview links**: "Preview" on a task card publishes the dev server the task started on its own tailnet HTTPS port (`tailscale serve`, never Funnel).
- **Devcontainer**: repos with `.devcontainer/devcontainer.json` can run sessions inside the container. Bypass permissions is an explicit per-session choice (host or container); tasks, scheduled and batch runs cannot use it, and settings overrides in extra args (`--settings`, `--setting-sources`, `--permission-prompt`) are rejected everywhere.
- **Backup**: `ccboard-backup.timer` runs nightly (02:30 by default): a consistent snapshot of the board's SQLite DB and your `~/.claude/projects` transcripts go into a restic repository, then every repo under `PROJECTS_DIR` gets its unpushed work copied to backup branches on its `origin` (`ccboard-backup/<node>/<branch>`), so WIP from tasks survives the box. The backup never pushes to `main` or to any branch you work on. The strip shows the last result and the 🔔 panel has "Back up now"; a failed run pushes an ntfy warning.
- **Fleet**: every box shows its own cpu / ram / disk / uptime in the usage strip. Install ccboard on a second box with the same `CCBOARD_HUB_TOKEN`, set `CCBOARD_NODES=name=https://box.tailnet.ts.net:8443,…` on the one you look at, and it polls the others every minute into a Nodes strip (online, sessions, needs-you, health, 5h usage) that links to each board. Any box can be the hub; they are the same app.

See [ROADMAP.md](ROADMAP.md) for how each item was built and what comes next.

## What it is not

- Not a chat UI. The Claude TUI is the interface; the board is around it.
- Not multi-user. One Unix user, one Tailscale identity allowlist.
- Never exposed publicly. No Funnel, everything binds to loopback.
- Not a transcript parser. State comes from tmux (and, from v0.2, Claude Code hooks).

## Install

On the target box, as the user who will own the Claude sessions (the script calls `sudo` where needed; run it from a terminal, e.g. `ssh -t`):

```sh
tar xzf ccboard.tar.gz && cd ccboard      # or: git clone https://github.com/rpandox/ccboard && cd ccboard
./install.sh
```

The script prints the dashboard URL when it is done. Open it **from another device on your tailnet** (requests from the box itself carry no Tailscale identity and are rejected), then click *Log in* to sign in to Claude Code.

Requirements: Ubuntu 22.04 or 24.04, Tailscale installed and logged in, MagicDNS and **HTTPS certificates** enabled for your tailnet (admin console → DNS), `sudo` rights.

### Settings

Every setting is an environment variable. Values are remembered in `/etc/ccboard/env`; a value given on the command line overrides the remembered one.

| Variable | Default | Meaning |
|---|---|---|
| `PROJECTS_DIR` | `/srv/projects` | Where projects live (created and chowned to you) |
| `CCBOARD_HTTPS_PORT` | `443` | Tailscale HTTPS port for the dashboard **and** the terminal (`/tty`) |
| `CODE_HTTPS_PORT` | `8443` | Tailscale HTTPS port for code-server |
| `CCBOARD_PORT` | `8000` | Loopback port of the dashboard |
| `TTYD_PORT` | `7681` | Loopback port of ttyd |
| `CODE_SERVER_PORT` | `8080` | Loopback port of code-server |
| `CCBOARD_ALLOWED_USERS` | your tailnet login | Comma list of `Tailscale-User-Login` values allowed in |
| `CCBOARD_DATA_DIR` | `~/.local/share/ccboard` | SQLite database |
| `CODE_SERVER_VERSION` | `4.139.1` | Version installed when code-server is absent |
| `NTFY_HTTPS_PORT` | `8444` | Tailscale HTTPS port for the ntfy server (`CCBOARD_NTFY=0` skips ntfy) |
| `NTFY_TOPIC` | `ccboard` | ntfy topic the board publishes to |
| `CCBOARD_APPROVE_TIMEOUT` | `90` | Seconds a permission prompt waits for a remote answer (`CCBOARD_REMOTE_APPROVE=0` disables the hook) |
| `CCBOARD_RECOVER` | `1` | Relaunch Claude sessions after a reboot |
| `CCBOARD_AUTO_CONTINUE` | `1` | Type `continue` once into a session parked on a limit after its window resets, and into a session that was working before a reboot once its relaunch is back at the prompt (`0` turns both off; `flags.no_autoresume` opts one session out) |
| `PREVIEW_HTTPS_BASE` | `9100` | First tailnet HTTPS port used for task preview links |
| `CCBOARD_DEVCONTAINER` | unset | `1` installs the devcontainer CLI (docker required) |
| `CCBOARD_NODE_NAME` | `hostname -s` | This box's name in the fleet strips |
| `CCBOARD_HUB_TOKEN` | generated | Shared secret for node-to-node `GET /api/node/summary`; copy the same value to every box |
| `CCBOARD_NODES` | empty | `name=https://host.tailnet.ts.net:port,…` of the other boxes this one polls |
| `CCBOARD_RESTIC_REPO` | `<data dir>/restic` | restic repository for the nightly backup (`sftp:user@host:/path`, `rclone:remote:path`, `s3:…`, or `off`). The default is on the same disk: fine against deletion and corruption, useless against disk loss |
| `CCBOARD_BACKUP_PUSH` | `1` | `0` skips the nightly copy of unpushed work to `ccboard-backup/<node>/<branch>` on every repo's `origin` |
| `CCBOARD_BACKUP_ONCALENDAR` | `*-*-* 02:30:00` | systemd calendar spec of the backup timer (`CCBOARD_BACKUP=0` at install time leaves the timer disabled) |
| `CCBOARD_BACKUP_EXTRA` | empty | Colon-separated extra paths to include in the restic snapshot |
| `CCBOARD_RESTIC_PASSWORD_FILE` | `<data dir>/restic-password` | Where the restic password lives |
| `CCBOARD_RUNTIME` | `systemd` | `systemd` runs the board as `ccboard.service`; `docker` runs it as the `ccboard` container ([below](#run-the-board-as-a-container-optional)). Remembered in `/etc/ccboard/env`; go back with `CCBOARD_RUNTIME=systemd ./install.sh` |
| `CCBOARD_IMAGE_TAG` | `latest` | Docker mode only, install-time only (kept in the compose `.env`, not in `/etc/ccboard/env`): the `ghcr.io/rpandox/ccboard` tag to run, e.g. `sha-1a2b3c4` or `v0.5.2` to pin; Watchtower follows `latest` only |
| `CCBOARD_CLAUDE_MEM` | `1` | `1` verifies the claude-mem plugin (installs it when it is missing) and lets the board watch its worker; `0` skips both ([claude-mem](#claude-mem)) |
| `CCBOARD_MEM_PORT` | empty | Port the board probes for claude-mem's worker. Empty finds it by itself (`~/.claude-mem/worker.pid`, then claude-mem's settings, then the plugin's own default, 37700 plus your uid modulo 100: 37700 on ubu2, 37701 on a Mac). It only tells the board where to look; the worker's own port is `CLAUDE_MEM_WORKER_PORT` in `~/.claude-mem/settings.json` |
| `CCBOARD_MEM_SERVICE` | `0` | `1` runs the worker as `ccboard-mem.service` from a clean environment; off until it has been verified on the box ([claude-mem](#claude-mem)). Remembered in `/etc/ccboard/env`; `0` disables the unit again |
| `CCBOARD_CODEX_HOOK_TRUST` | `review` | `review` (default): trust ccboard's Codex hooks once in Codex itself. `bypass`: every Codex session the board starts gets `--dangerously-bypass-hook-trust`, which also runs the `.codex/hooks.json` of any repository you cloned without asking (the same risk class as bypassPermissions; the doctor warns about repo-level hook files). Remembered in `/etc/ccboard/env` ([Codex](#codex)) |
| `CODEX_HOME` | empty | Where Codex keeps its config (empty means `~/.codex`, as for Codex itself). `hooks.json` is written there and `codex mcp add` registers there. Remembered in `/etc/ccboard/env`; absolute path, no spaces |

Tailnet-only `tailscale serve` accepts any HTTPS port. If a chosen port already carries something else (another serve handler or a Funnel), `install.sh` stops and tells you; pick other ports or rerun with `CCBOARD_REPLACE_SERVE=1` to replace that port's handlers. It never runs `tailscale serve reset` and never touches ports you did not name.

Example for a box where 443 and 8080 are already taken:

```sh
CCBOARD_HTTPS_PORT=8443 CODE_HTTPS_PORT=10000 CODE_SERVER_PORT=8081 ./install.sh
```

### What install.sh sets up

- apt: `git tmux curl ca-certificates python3 python3-venv` (only what is missing)
- `ttyd` 1.7.7 upstream static binary at `/usr/local/bin/ttyd` (checksum verified); the apt package's `ttyd.service` is disabled if present
- `code-server` (pinned version, only when absent) with `~/.config/code-server/config.yaml` set to `auth: none` on loopback (the file is marked `# managed by ccboard`; an existing unmanaged file is never overwritten without `CCBOARD_REPLACE_CODE_SERVER_CONFIG=1`)
- Claude Code via the native installer (only when absent)
- a venv in the checkout (`.venv`)
- `/etc/systemd/system/ccboard-tmux.service` (the tmux server that owns every session), `ccboard-ttyd.service`, `ccboard.service`, plus `code-server@<you>`
- `tailscale serve --bg`: `/` and `/tty` on `CCBOARD_HTTPS_PORT`, `/` on `CODE_HTTPS_PORT`
- `restic` from apt, a random restic password in `<data dir>/restic-password` (0600; **copy it somewhere safe**, without it the backups are unreadable), and `ccboard-backup.service` + `.timer`. `sudo systemctl start ccboard-backup` runs one now; `journalctl -u ccboard-backup` has the log. Remote repos need their credentials in `/etc/ccboard/env` (for example `AWS_ACCESS_KEY_ID`; lines you add there by hand survive reruns of `install.sh`) or an ssh key without passphrase for `sftp:`; both the git pushes and restic's own ssh run with `BatchMode=yes` and a connect timeout, so nothing ever prompts. "Back up now" on the board starts the same unit (`systemctl start ccboard-backup`), so a ccboard restart cannot interrupt it; a run that was killed anyway leaves a restic lock, which the next run clears with `restic unlock` before retrying

- the `claude-mem@thedotmack` Claude Code plugin, when it is missing (`CCBOARD_CLAUDE_MEM=0` skips this; bun is never installed) and, only with `CCBOARD_MEM_SERVICE=1`, `ccboard-mem.service` (see [claude-mem](#claude-mem))
- when `codex` is installed (`~/.local/bin/codex` counts; the login shell's PATH is not needed): ccboard's hooks merged into `$CODEX_HOME/hooks.json` (`scripts/codex_hooks.py install`) and the `ccboard` MCP server registered with `codex mcp add ccboard --env CCBOARD_URL=http://127.0.0.1:$CCBOARD_PORT` (a stale entry that names another checkout or port is re-pointed). Without Codex both steps are skipped with a note; a failure is a warning, never an abort. See [Codex](#codex)
- with `CCBOARD_RUNTIME=docker`: the container instead of `ccboard.service`, plus `ccboard-watchtower`, a compose file under `<data dir>/compose` and `<data dir>/app` for the scripts the host runs (see [Run the board as a container](#run-the-board-as-a-container-optional))

Update: pull or extract the new version into the same directory and rerun `./install.sh`. It restarts only `ccboard` (stateless) unless something else changed. Restarting `ccboard` never touches running Claude sessions, because they live under `ccboard-tmux.service`. `install.sh` also writes `/etc/sudoers.d/ccboard`, which lets your user restart `ccboard` and `ccboard-ttyd` without a password, so code-only updates are `git pull && sudo systemctl restart ccboard` (or `scripts/deploy.sh <host>` from your machine).

## Run the board as a container (optional)

`CCBOARD_RUNTIME=docker ./install.sh` runs the dashboard as a Docker container instead of `ccboard.service`, so that **a `git push` to `main` is the deploy**: GitHub Actions runs the tests and builds `ghcr.io/rpandox/ccboard`, and [Watchtower](https://github.com/nicholas-fedor/watchtower) on the box pulls the new image within five minutes. systemd mode stays the default and the fallback; nothing below is needed to use ccboard.

**What moves into the container.** Only the board's own process: the FastAPI app, its pollers and scheduler, the nightly backup run, and `Back up now`. It runs as your uid and gid (it refuses to run as root), on the host network and the host pid namespace, with these host paths mounted at the same location: your home (so `~/.claude`, `~/.codex`, `~/.ssh`, `~/.config/gh` and the host's `~/.local/bin/{claude,codex,ccusage}` are what it sees; the host binaries come first on `PATH`), `PROJECTS_DIR`, `/tmp/tmux-<uid>` (the tmux socket), `/var/run/tailscale` (for `tailscale serve`) and `/etc/ccboard` read-only (every setting still lives in `/etc/ccboard/env`, which the compose file passes in as the container's environment).

**What stays on the host.** `ccboard-tmux.service` (the tmux server that owns every session; install.sh never restarts it, so sessions survive every switch, update and rollback), `ccboard-ttyd`, code-server, ntfy, Tailscale and its `serve` mappings (still `127.0.0.1:CCBOARD_PORT` and `/tty`), the backup timer (its unit now runs `docker exec ccboard ... python -m app.backup`), Docker, and the Claude Code / Codex logins and binaries. The scripts the host runs (Claude hooks, statusline, the ttyd attach wrapper, the MCP server) come from `$CCBOARD_DATA_DIR/app/{bin,scripts}`, which the container refreshes from the image on every start and which install.sh seeds once from the checkout; the hooks, the ttyd unit and the backup unit are pointed there.

Requirements on top of the install requirements: Docker Engine with the compose plugin, and your user in the `docker` group. Run it in three steps; each can be undone, and only the last one changes the running box.

### A. Build in GitHub (nothing on the box changes)

Push to `main`. The `ci` workflow runs the tests and then publishes `ghcr.io/rpandox/ccboard` with the tags `latest` (main), `sha-<7 chars>` and `vX.Y.Z` (when the commit subject starts with it). Watch the run under Actions, then check that the package exists.

If the package comes out private (an image linked to a public repository is published public, as happened here; check the package page), pick one:

1. Make it public: GitHub, your profile, Packages, `ccboard`, Package settings, Change visibility, Public (the repository is already public).
2. Keep it private and log the box in (the same login is what Watchtower uses, through `~/.docker/config.json`):

   ```sh
   gh auth refresh -s read:packages && gh auth token | docker login ghcr.io -u <github-user> --password-stdin
   ```

### B. Shadow run (no sudo, nothing else touched)

Starts the image next to the live board on `127.0.0.1:8010` with its own empty data directory (`~/.local/share/ccboard-shadow`: a fresh database and its own hook token, so no scheduled job runs twice and no hook ever reaches it). `CCBOARD_SHADOW=1` makes the entrypoint skip every host change (hooks, tmux.conf, MCP registration), the projects are mounted read-only, and it has its own compose project, so `down` can never touch the real one. On the box:

```sh
cd ~/ccboard && git pull
export CCBOARD_HOME=$HOME CCBOARD_UID=$(id -u) CCBOARD_GID=$(id -g) PROJECTS_DIR=/srv/projects
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.shadow.yml up -d ccboard

curl -fs http://127.0.0.1:8010/healthz
curl -fs -H "X-CCBoard-Token: $(cat ~/.local/share/ccboard-shadow/hook-token)" http://127.0.0.1:8010/api/state | head -c 400   # same sessions and projects as the live board
docker exec ccboard-shadow claude auth status --json
docker exec ccboard-shadow ccusage --version
docker exec ccboard-shadow git -C /srv/projects/<repo> status
tmux -L ccboard list-sessions                                                  # unchanged

docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.shadow.yml down
rm -r ~/.local/share/ccboard-shadow
```

### C. Switch (you run it; it needs sudo)

A watchdog line in your crontab (`scripts/ccboard-watchdog.sh`, every 2 minutes, no sudo) starts the container when Watchtower's restart did not take on a busy box and restarts it when it stays unhealthy; `crontab -l | grep ccboard-watchdog` shows it, and `~/.local/share/ccboard/watchdog.log` what it did.


```sh
ssh -t ubu2 'cd ccboard && git pull && CCBOARD_RUNTIME=docker ./install.sh'
```

In this order, with nothing on the running system changed until the image is on the box:

1. Checks `docker`, `docker compose` and your docker group membership, and says how to fix what is missing.
2. Writes `~/.local/share/ccboard/compose/docker-compose.yml` (plus the shadow override beside it) and its `.env` (`CCBOARD_HOME`, `CCBOARD_UID`, `CCBOARD_GID`, `PROJECTS_DIR`, `CCBOARD_IMAGE_TAG`; a tag you pinned, for example `CCBOARD_IMAGE_TAG=sha-1a2b3c4 ./install.sh`, is kept on reruns), seeds `$CCBOARD_DATA_DIR/app` from the checkout, and runs `docker compose --profile prod pull`. If the pull fails (private package, image not built yet) it prints the two remedies from step A and stops.
3. Does everything the systemd install does (apt, ttyd, code-server, ntfy, restic, the venv in the checkout, which the rollback uses), except that the units and the Claude hooks and MCP registration point at `$CCBOARD_DATA_DIR/app`, the backup unit becomes `docker exec`, `ccboard.service` is installed but not enabled, and the sudoers rule keeps only `systemctl restart ccboard-ttyd` and `tailscale serve *`.
4. `sudo tailscale set --operator=<you>` (previews run `tailscale serve` without sudo), then `systemctl disable --now ccboard.service` and straight away `docker compose --profile prod up -d`, then waits up to 60 s for `/healthz`. If the board does not come up it prints the rollback and exits 1. Hook calls during the few seconds of the switch fail silently.

`CCBOARD_RUNTIME=docker` is remembered in `/etc/ccboard/env`, so a plain `./install.sh` rerun stays in docker mode. Afterwards check `docker compose -f ~/.local/share/ccboard/compose/docker-compose.yml --profile prod ps`, open the board from another tailnet device, start a Claude session and see its events arrive, and compare `tmux -L ccboard list-sessions` with what it was before.

**Updating.** Watchtower runs as `ccboard-watchtower` with `--interval 300 --cleanup --label-enable --scope ccboard --include-restarting`. It only touches containers labelled `com.centurylinklabs.watchtower.enable=true` **and** `com.centurylinklabs.watchtower.scope=ccboard`, which only the ccboard container carries, so the other containers on the box are never updated, restarted or removed. `scripts/deploy.sh <host>` detects docker mode on the box (a running container named `ccboard`), pushes, waits for `ghcr.io/rpandox/ccboard:sha-<7>`, pulls and recreates the container instead of waiting for Watchtower, and keeps the checkout pulled so the rollback stays current. Rerun `./install.sh` when `install.sh`, `deploy/`, the units or `tmux.conf` change. After editing `/etc/ccboard/env` by hand: `docker compose -f ~/.local/share/ccboard/compose/docker-compose.yml up -d --force-recreate ccboard` (Watchtower recreates a container from its existing configuration and would not see the edit).

**Rollback.** Sessions are never affected:

```sh
docker compose -f ~/.local/share/ccboard/compose/docker-compose.yml --profile prod down
cd ~/ccboard && CCBOARD_RUNTIME=systemd ./install.sh
```

The second command restarts `ccboard.service` and points the ttyd and backup units, the hooks and the sudoers rule (the MCP registration keeps working from `$CCBOARD_DATA_DIR/app`) back at the checkout. Without the installer: `sudo sed -i 's/^CCBOARD_RUNTIME=.*/CCBOARD_RUNTIME=systemd/' /etc/ccboard/env && sudo systemctl enable --now ccboard.service` (the backup timer keeps trying `docker exec` until you rerun install.sh). Run `down` first: the container holds the board's port, and install.sh refuses a port that something else holds.

**Limitations.**

- Devcontainer sessions need the docker CLI and socket inside the board's container, which are not mounted: they stay a systemd-mode feature (`CCBOARD_DEVCONTAINER=1` warns in docker mode).
- Task previews need operator mode: inside the container there is no `sudo`, so `tailscale serve` runs through the mounted socket as the operator. install.sh sets `tailscale set --operator=<you>` in docker mode; see the security notes for what that allows. Without it the preview button reports the error.
- If a preview button finds no listening port for a dev server, see the AppArmor note in `deploy/README.md`.
- The image is `linux/amd64` only for now, and Claude Code and Codex are not in it: the board runs the host's binaries from `~/.local/bin`, so they update and log in on the host as before.
- The container can read and write everything your user can (it mounts your home), exactly like the systemd unit that runs as you. Docker group membership is root-equivalent on the box.
- `CCBOARD_DATA_DIR` must be under your home directory (the only thing besides `PROJECTS_DIR` that is mounted).
- Under docker, `Back up now` runs a detached process inside the container (log in `<data dir>/backup.log`) rather than the systemd unit; the nightly timer still runs the unit, which `docker exec`s into the container.

## Using it

- **New project** (the **+** menu, or `c` then `p`) → name (+ optional first clone URL); *Import from GitHub* and *Batch prompt* are in the same menu (`c` then `r` imports). Clones run in a visible session (`<project>--<repo>--clone`) and the sheet shows the clone queue; if a clone fails the repo shows *clone failed* and *Attach* shows the error. Home has no per-project *Add repo*, *Delete* or *Remove* buttons any more: they live on the project page (see *The project page* below; the API is `POST /api/projects/<project>/repos` and `DELETE /api/projects/<project>[/repos/<repo>]`).
- **New session** on a repo → the launcher sheet (the **+** menu, a project block on Home, a project page, the **+** on a project's row in the sidebar, an empty quad slot, the terminal dock, the Move sheet's *Options…*): pick the agent (Claude, Codex or a plain shell), the launch (new, resume, continue, from a pull request, fork) and the fields of that agent; *Start & open* opens the terminal. Everything about it is under [The launcher](#the-launcher-v0513). Scheduled, batch and task runs cannot use bypass.
- **New session** on the *project folder* row (projects that hold several repos) → Claude starts in the project folder itself, so every repo below is in scope without `--add-dir`. Such sessions use the reserved repo name `root` (`<project>--root--<session>` in tmux), so a repo cannot be called `root`.
- **Tasks** (**+ task** on a project page or on `#/tasks`, the **+** menu, or `c` then `t`). On a project page the form opens at once, for the repo in the address, else the one the Files tab shows, else the one a task was last started in, else the first repo (the **in** select at the top of the form switches repo and keeps what you typed), so **+ task**, type, **Start task** is three taps and the prompt box already has the focus. From Home the + menu asks for the repo first. The form is a prompt (Enter adds a line, ⌘/Ctrl+Enter submits; the title is optional and defaults to the first line of the prompt), a **Run** control and *Options*:
  - **Now** starts Claude on a new branch in its own worktree (`claude --worktree`, session `<project>--<repo>--t-<slug>`), toasts *started <slug>* and opens that session's peek (the terminal is one tap from there).
  - **Later** adds a card to the **Backlog** column and nothing starts.
  - **Schedule** swaps the lower half of the form to the schedule fields (name from the title, cron presets *nightly 02:30* / *weekdays 09:00* / *hourly* / *one-off*, which runs once now, permission mode, max turns and budget) and creates the job; the prompt is carried over. The agent picker stays: with Codex the form shows its model and reasoning and drops the turn and budget limits (Codex has none).
  - The submit button follows the choice (*Start task* / *Add to backlog* / *Schedule*) and ignores a second tap while the first is in flight. Model, effort and permission mode are under *Options*; they, the Run choice and the cron are remembered per repo on this device (`ccboard:task:<project>/<repo>`), so the next task is a prompt and a key press. Errors show inside the form and nothing you typed is lost. Bypass permissions is never offered for a task, and the server refuses it.
  - The **project folder** is a target too when it is itself a git repo (listed as *<project> · project folder*, repo name `root`): the task runs on a branch of that repo. A project folder that is not a git repo is offered for sessions only; the picker says *tasks need git*.
- **The Backlog.** A backlog card shows the title, the first two lines of the prompt, the repo and when it was added, and has four actions:
  - **Start** (one tap) runs it in a new worktree session, opens its peek, and the card is in *In progress* before the next poll.
  - **Send to session** lists the project's Claude sessions, ready ones first (idle, done, or waiting for an answer) with their last prompt; busy ones (working, or waiting on a permission decision) are listed greyed with the reason. Picking one pastes the prompt into it as a single multi-line paste: no worktree is made, and the card moves to *In progress* with an *in <session>* chip that opens that session. A session of another repo asks *work there anyway?* and then puts `Work in <repo path>.` in front of the prompt. When exactly one session in the task's own repo is ready, the card also has a **→ <session>** button that does it in one tap.
  - **Edit** changes the title and the prompt (the full prompt is loaded first when the poll only carried its head); **Delete** takes two taps.
  - A card waiting behind another step (phase *queued*) can be edited or deleted but not started. Only Backlog and queued tasks can be edited or deleted; a started task is archived.
  - Cards, Start, Send, Edit and Delete update the board at once (the poll only confirms), and an optimistic change that the server refuses puts the card back with the reason as a toast.

  The API: `POST /api/tasks {project, repo, title, prompt, when: now|later, auto_close?, model?, effort?, permission_mode?, args?, add_dirs?}` (`agent` is `claude` until Codex arrives; `repo` may be `root` when the project folder is a git repo), `GET /api/tasks?project=&repo=&phase=` and `GET /api/tasks/<id>` (with the full prompt and the launch choices), `PATCH`/`DELETE /api/tasks/<id>`, and `POST /api/tasks/<id>/dispatch` with `{mode: "lane"}` (a new worktree session) or `{session: "<tmux name>", force?}` (hand it to a running one; 409 with the reason when the session is busy, has a pending permission or belongs to another repo). The per-repo `POST /api/projects/<p>/repos/<r>/tasks` and the MCP `create_task` still work (`create_task` takes `dispatch: false` to add to the Backlog instead of starting). In demo mode (`?demo=1`) the demo state has two Backlog cards (one repo with a single ready session, which gets the one-tap button, and one whose sessions are all busy) and a task handed to a running session, and the buttons act on the page only. Drag and drop, auto-close and chains come later (v0.5.14b and v0.5.15).
- **State badges** come from Claude Code hooks: *working* after you send a prompt, *needs you* when Claude asks for permission or input, *done* when a turn ends (with the last line it printed), *error* on API failures. *Ack* clears a done/error highlight. The hooks are the scripts in `bin/`; they POST to the board over loopback with the token in `~/.local/share/ccboard/hook-token` and never block the TUI (they are registered `async`). `scripts/claude_settings.py show|remove` inspects or removes them. If you already had a `statusLine`, ccboard keeps yours and the per-session model/context numbers stay empty.
- **On a phone** the board is a PWA (add it to the Home Screen): a bottom bar jumps to Needs you / Tasks / Schedules / Projects, action rows scroll sideways, forms open as sheets, and the board re-polls the moment it comes back to the foreground (there is also a ↻ button, since an installed PWA has no browser reload). The UI is built on Blueprint's dark theme.
- The **first launch** of `claude` in a new folder shows Claude Code's workspace-trust prompt (Enter = trust) and possibly its onboarding screens. Answer them in the terminal; a desktop browser is easiest for that.
- tmux names are `<project>--<repo>--<session>`. From a shell on the box: `TMUX_TMPDIR=/tmp tmux -L ccboard ls`.
- Session sizes follow the most recently active client (tmux `window-size latest`), so attaching from a phone reflows the TUI for a desktop tab that is also attached.

### Home and the inbox

**Home** (`#/`) answers "what needs me, what is working, what did it cost" on one screen. The **summary line** under the page title counts the sessions per state (`✻ needs you · ✽ working · ∙ idle · ✓ done`, plus `✕ error` only when there is one); every segment is a filter (`#/?f=waiting|working|idle|done|errored`, tap it again to clear) and the line scrolls sideways on a phone. When you were away, a **while you were away** strip says what happened since the board last saw you (done, needs you, error, schedule runs, PRs opened, rate-limit hits, blocked background jobs, each count a link to the matching filter or page; it stays out of the way when you left less than ten minutes ago or nothing happened, and it can be dismissed). Next come the **inbox** (only when something needs you: the plan or question text first, then project/repo · session · age, then Allow / Deny, Open, Ack, the reply chips and a send box; the full list is `#/inbox`), the **schedules** strip (the next three runs), the **project blocks** and the **usage card**. Blocks group by project (directory) by default, attention first and then most recent activity, and a segmented control in the page head regroups them by state or by agent (remembered on the device); projects with no session activity for 7 days fold into one *older* block (collapsed until you open it; which blocks you collapsed is remembered on the device). A session row shows the state and agent glyphs, name, age of the last activity, the model, a context meter (amber from 60 %, red from 85 %, with a one-tap **compact** chip that types `/compact`), a worktree badge, the PR number of its task, a subagents count, the session cost (amber from $10, red from $100, API-equivalent), a *limit* chip while that session is rate limited, and a **tail** toggle that shows the last 12 lines of its pane (one shared event stream, `GET /api/stream?names=a,b&lines=12`, opened only while a tail is expanded and closed when the last one collapses). The **usage card** shows the 5H and 7D gauges with the reset countdown, the 24 h sparkline of the 5H window and the last seven days of cost, and stays hidden until the board has data. A rate-limit hit also raises a banner with the reset time. Keys: `g q` opens the quad view and `c` then `s` / `t` / `p` / `r` opens the new session, task, project and GitHub import sheets from anywhere.

### The project page

`#/p/<project>` (or `#/p/<project>/<repo>`, which narrows every tab to that repo and says so) is one page per project, opened from the sidebar, a Home block, a repo chip or the + menu (a new project opens here). The **header** has the project name and its folder, one chip per repo (a link to its Files tab) with its branch, a dot when it has uncommitted changes, its cost and a code-server link for the repo, the cost today and over 7 days (API-equivalent, from the cost record) and the active hours of the last 7 days (read once from `GET /api/usage/summary?days=7`; left out when that fails), the buttons **+ session**, **+ task** and **+ schedule** (each opens the same sheet as the + menu with this project, and the repo in the address, preselected), **Add repo** and **code-server** (the project folder). The overflow menu opens the *repos and danger zone*: **Remove** per repo and **Delete project**, each two taps (Remove, then Confirm Remove). The **tabs** follow `?tab=` and change without reloading the page (the count of sessions, tasks and schedules sits on the tab):

- **Sessions** (default): this project's sessions as the same rich rows as Home, grouped by the project folder and each repo in Home's order (attention first, then the most recent). A project with none shows a *+ session* prompt.
- **Tasks**: the task columns *Backlog | In progress | Needs you | Done | PR open | Merged* for this project, and a *+ task* button (it opens the task form for the repo the Files tab shows, else the first repo, else the project folder when that is a git repo). Backlog holds the tasks added with *Later*: Start, Send to session, Edit and Delete sit on the card (see *Tasks* under *Using it*). A task that was handed to a running session shows an *in <session>* chip that opens that session.
- **Schedules**: this project's jobs with the next and the last run and the last three runs of each, an agent glyph (◆ Claude, ◇ Codex) on every job and run, and a line naming the window each agent waits on. *+ schedule* opens the job form (an agent picker, Claude | Codex, with Codex's model and reasoning; Codex is off with its reason when it is not installed) with cron presets as chips (nightly 02:30, weekdays 09:00, hourly, and one-off, which clears the cron field and so runs once now), and **Batch prompt** opens the batch form for several repos, with the same agent picker. Below the jobs, every chain of this project is drawn as connected cards, one per step: the agent glyph, the step's state, the head of its result, and what a waiting step waits for (the previous step, or a limit window); a running step has an *Open* button.
- **Files**: a read-only tree of one repo (`#/p/<project>/<repo>?tab=files`; the first repo by default) with the file preview beside it from 840 px and below it on a phone. The toolbar above has a switcher with the repos and the project folder, the branch (with ahead and behind, or *not a git repo*), the **hidden** and **ignored** toggles, **Refresh** and a code-server link for the folder. The tree loads one directory at a time when you open it, remembers what you opened (per project and repo, on this device), checks the open directories every 10 s while the page is visible (a conditional request: nothing is redrawn when nothing changed) and marks what git knows: a dot on every directory that holds a change, and the size and *M* / *A* / *?* / *D* / *U* beside each file. Keyboard: Up / Down move, Right opens a directory (or steps into it), Left closes it (or steps out), Home / End jump, Enter opens a file or toggles a directory, `*` opens every directory at that level, and typing letters jumps to the next name that starts with them. Opening a file puts it in the address (`&path=<file>`, without a history entry per file), so the page can be reloaded or shared, and `?path=` on a directory opens it and its parents. Editing stays in code-server: the preview header has an **open in code-server** link that opens the file in the folder of its repo, at the line you click in the gutter.
- **Memory** appears once the board has memory data (v0.5.20).

In the sidebar and the phone menu the current project's repos and its project folder are folders that load their sub-folders when you open them (the chevron toggles, the name goes to `?tab=files&path=<folder>`, and a dot marks folders with changes).

What the tree leaves out, and how to see it: names that start with a dot are hidden (the **hidden** toggle lists them), `.git` is never shown, files and folders that git ignores are hidden (the **ignored** toggle lists them), and in a plain directory without git (the project folder, a non-repo subfolder) the usual build folders (`node_modules`, `.venv`, `venv`, `__pycache__`, `.cache`, `dist`, `build`, `.next`, `target`) are left out the same way. A repo inside the project folder shows as a repo node you can open in place. A level longer than 1500 entries shows the first 1500 and says how many there are. Symlinks are listed but never followed. In the preview, files over 200 KB are cut (and say so), binary or non-UTF-8 files show no preview, and names that look like secrets (`.env*`, `*.pem`, `id_rsa*`, `*credentials*`, `*.key`, `*secret*`) ask for a click on **reveal** before their text is read. For a big repo, `git config core.untrackedCache true` makes the untracked-file scan cheap.

The two read endpoints behind it sit behind the same identity check as the rest of the API: `GET /api/projects/<project>/repos/<repo>/tree?path=&hidden=0&ignored=0&repos=1&refresh=0` (the repo name `root` means the project folder; a weak `ETag`, and a matching `If-None-Match` gets a bare 304) and `GET /api/projects/<project>/repos/<repo>/file?path=&reveal=0` (400 for a bad path or a symlink, 403 for a secret-looking name without `reveal=1`, 404, 415 for a binary file). Demo mode (`?demo=1`) answers both from `app/static/demo/tree.json` and `file.json`, maps keyed `<repo>|<path>`.

### The launcher (v0.5.13)

One sheet starts every kind of run, from every page: the **+** in the top bar, a project block on Home, a project page (*+ session*, *+ task*), the **+** on a project's row in the sidebar, an empty slot of the quad view, the terminal dock and the *Options…* of the Move sheet. It is a right-hand panel from 840 px and a bottom sheet below that; the footer is sticky (*Cancel* and the one filled button, *Start & open*), inputs are 16 px on touch so a phone does not zoom, and a new session from anywhere is two taps once the defaults are remembered (the **+**, then *Start & open*).

- **The agent picker** is a segmented control, *Claude · Codex · Shell*. An agent that is not installed on the box is disabled and says why; the one you used last in this repo is chosen when the sheet opens. Changing the agent swaps the fields.
- **The fields come from `GET /api/agents`** (read once per page load, never on the 3 s poll). The launcher embeds a static copy of the same schema and uses it until the answer arrives or when the request fails, so the sheet always opens at once. Each field is `{key, label, kind, choices, default, help, group: basic|advanced, danger, when}`: `basic` ones are on the sheet, `advanced` ones sit under *More options*, `when` shows a field only while another is set (`{"launcher": ["resume", "continue"]}` = whichever of those is chosen; `{"worktree": true}`; `{"repo.devcontainer": true}`), and the one `danger` field is the bypass switch.
- **Claude** (the defaults are `opus` and effort `high`): *Start* (new, `--resume` with an optional id, `--continue`, `--from-pr` with a number or URL), name, model (`opus`, `fable`, `sonnet`, `haiku` first; `opusplan`, `best`, the `[1m]` variants and any id after them), effort, fast, permission mode, first prompt. Under *More options*: allowed and disallowed tools, `--tools`, text appended to the system prompt, `--agent`, `--fallback-model` (up to three), `--autocompact` (`auto` or a number), a new worktree (with a name), *fork instead of continuing* (`--fork-session`, only for resume and continue), `--add-dir` (the repo's siblings are pre-checked, other projects' repos can be added), the devcontainer (only when the repo has one), an MCP config file and extra arguments. Claude does not restore `--mcp-config`, `--fallback-model`, `--add-dir` and the others on a resume, so the board stores them with the session and passes them again on recovery.
- **Codex**: *Start* (new, resume by id **or name**, continue = `resume --last`, and *fork* where this Codex has `codex fork`), name (the board's own label: Codex has no `--name`), model (the catalogue of `codex debug models`, cached for an hour; the box's visible slugs until it has been read), reasoning (the levels each model accepts, the others are disabled), and **one mode picker**, a segmented control: *default* (`-s workspace-write -a on-request`), *auto* (the automatic approval review), *read-only*, *bypass* and *custom* (which shows a sandbox and an approval select). Under *More options*: web search, `--add-dir`, a managed worktree, `-c key=value` lines, a profile, the scrollback switch and extra arguments. **Config lines** must match `^[A-Za-z0-9_.]+=.+$`, at most 20 lines of 400 characters, and the server refuses the keys the launcher owns or that would loosen the sandbox (`model`, `model_reasoning_effort`, `sandbox*`, `approval*`, `profile(s)`, `hooks`, `features.hooks…`, `notify`, `mcp_servers`, `model_providers`, base URLs and logins, `projects`, any bypass or yolo spelling); everything else goes to Codex as `-c`.
- **Shell**: a name, and the devcontainer when the repo has one.
- **Presets** are chips above the fields: *opus high worktree* and *ultracode* for Claude, plus the ones you save per repo.
- **Defaults are remembered per repo and agent** on the device (`ccboard:launch:<project>/<repo>:<agent>`; the old `ccboard:launch:<project>/<repo>` is read once as Claude's; `ccboard:agent:<project>/<repo>` is the agent used last, `ccboard:defaults:<project>` a project-wide default, `ccboard:task:<project>/<repo>:<agent>` the task form's).
- **The danger gate.** Choosing *bypass* (Claude's `bypassPermissions`, Codex's bypass mode) or a Codex sandbox of `danger-full-access` shows the red warning and turns *Start & open* off until you tick *I understand*. The tick is asked **once per repo** (`ccboard:bypass-ack:<project>/<repo>`), not on every launch, and the sheet then sends `mode: "bypass"` with `bypass: true` for Claude as it does for Codex. The gate is never offered for a task, a dispatch or a schedule, and the server refuses it there too: tasks, dispatch and headless runs take no `mode` at all and reject every spelling of bypass (`bypassPermissions`, `danger-full-access`, any `bypass`/`yolo` option or extra argument).
- **The command preview** is a read-only monospace line under the fields with a *Copy* button. It is approximate (it is built in the browser from the same flags); the `cmd` the server answers with after *Start & open* is the truth and replaces it.
- **The account** a new session will run on is shown in the footer (Claude: the current account; Codex: the current Codex account) with the window left. When it is at 85 % or more and another saved account has more room, a quiet *Switch first* button switches (the same switch as Settings) and repaints.
- **Task mode** adds a title, the prompt, *from a GitHub issue*, *Run now · Later · Schedule* and *Close when finished*; it never offers bypass.

**`POST /api/projects/<project>/repos/<repo>/sessions`** takes the sheet's words and builds the flags itself; the UI never sends raw flags. `launcher` is `new` (or `claude`), `resume`, `continue`, `from_pr`, `fork` (Codex), `shell`, or the older `codex`, `codex-resume`, `codex-continue`, `codex-fork`; `agent` (`claude`, `codex`, `shell`) turns a plain launcher into that agent's. The body:

| Field | Meaning |
|---|---|
| `name`, `resume_id` | the session name (blank: the next free `s1`, `s2`…); a UUID for Claude, a UUID **or a session name** for Codex |
| `mode` | `default`, `auto`, `read-only`, `bypass`, `custom` (Codex's picker); Claude reads it as its permission mode (`default` = `manual`, `read-only` = `plan`, `bypass` = `bypassPermissions`, or a Claude mode by its own name) |
| `permission_mode`, `model`, `effort`, `allowed_tools`, `disallowed_tools`, `append_system_prompt`, `args`, `add_dirs`, `devcontainer`, `bypass`, `opts` | the flat fields the API always took, unchanged |
| `reasoning` | Codex's reasoning effort (Claude reads it as `effort` when none is sent) |
| `prompt` | the first message of a new session (8000 characters at most; longer ones are a task) |
| `worktree`, `worktree_name` | `true` (or the name itself) starts in a new worktree: Claude with `--worktree <name>`, Codex with a worktree the board creates (`git worktree add -b worktree-<slug> .ccboard/worktrees/<slug>`, `.worktreeinclude` copied in, taken away again if the launch fails). The name defaults to the session name |
| `from_pr`, `fork_session` | Claude `--from-pr <n or URL>` (also what `launcher: from_pr` means); `--fork-session` on a resume or continue (Codex: a resume by id becomes `codex fork`) |
| `fallback_model`, `autocompact`, `tools`, `agent_name`, `mcp_config` | Claude `--fallback-model`, `--autocompact`, `--tools`, `--agent`, `--mcp-config` (a file under the projects folder or the Claude config folder) |
| `config`, `sandbox`, `approval`, `search` | Codex `-c key=value` lines (a list or one text), `-s`, `-a`, `--search` |
| `fast` | Claude fast mode: there is no CLI flag, so the route only records it with the session's options; the launcher sheet sends `/fast` through `POST /api/sessions/<tmux>/command` once the session is at its prompt, after `/effort ultracode on` when both are ticked (the Fast toggle of the terminal's tuning strip does the same later). `/fast` switches fast mode on or off, so a session whose settings already turn it on is turned off by it |
| `cwd_rel` | start in that folder of the repo (relative, no `..`, an existing directory; not with a worktree or the devcontainer) |

The answer is `{tmux, attach_url, agent, agent_session_id, claude_session_id, cmd}`. A Claude-only field sent to Codex is refused by name (`tools: not supported by codex`), and the other way round. **Bypass:** `mode: "bypass"` needs `bypass: true` beside it (the acknowledgement of the gate above), and so does a Codex `sandbox: "danger-full-access"`; the spellings that existed before (`permission_mode: "bypassPermissions"`, `bypass: true`, the arguments) keep their old rules: an explicit choice on an interactive host session, never stored for resume, never for tasks, dispatch or headless runs.

**Ultracode.** `--effort ultracode` is accepted only where the box's `claude` takes it: its `--help` names it on the `--effort` option (read once per binary), or `CCBOARD_CLAUDE_ULTRACODE_FLAG=1` records that `claude --effort ultracode -p 'say ok'` worked on the box (`0` records that it did not). Then `ultracode` is one of the efforts of `GET /api/agents` and of the launch request; until then it is not, and the *ultracode* preset starts the session and sends `/effort ultracode on` through `POST /api/sessions/<tmux>/command` once the session is at its prompt. `GET /api/agents` shows which one this box is on as `claude.capabilities.ultracode_flag`.

### Hooks v2, notifications with context, /command /prompt /resize (v0.5.7)

### Notifications (v0.5.7, v0.5.18)

A push tells you which session wants you and why, without opening the board. The title is `<glyph> <project>/<repo> · <session>: needs you | done | error` (◆ claude, ◇ codex, ▸ shell; a session in a project's root folder shows the project alone). The body has up to three lines, empties left out: the task title, `› <your last prompt, 120 chars>` (never a prompt a harness typed, such as a finished background task), and `? <what is asked>`. The ask is `Bash: npm test` for a permission, Claude's message for idle or a question, the first 200 characters of the answer for done, and the error text for an error. A done notice of a task whose session closes itself ends with `(session closes in 45s)` (the grace is `CCBOARD_AUTOCLOSE_GRACE`), unless the answer ends with a question, which holds the close. The rate-limit notice is titled `<Agent> rate limited` and names the session that hit it first; a login that stopped working is `<Agent> login not valid` and opens Settings › Accounts.

| Notice | ntfy priority | ntfy tags | Web Push |
|---|---|---|---|
| needs you, permission | 4 | bell, key | yes |
| needs you, anything else | 4 | bell | yes |
| error | 4 | rotating_light | yes |
| done | 3 | white_check_mark | yes |
| done, a chain step with another step queued behind it | 2 | white_check_mark | no (ntfy only) |
| rate limit | 5 | no_entry | yes |

**Buttons, by platform.** ntfy (Android and iPhone) shows up to three: a pending permission offers Allow, Deny and Terminal; every other notice offers Terminal and Ack; a tap on the notice opens `/#/s/<tmux>`. Web Push shows the first two of the same list where the system draws action buttons (Chrome on Android, desktop Chrome): Allow and Deny answer the permission from the notification itself, with a short "Allowed." note and no window opened; Ack marks the session seen; Terminal opens `/term/<tmux>` in its own window. An installed iPhone app ignores action buttons: it shows the title and body, and a tap opens the session's peek (`#/s/<tmux>`), where Allow, Deny, Ack, Open terminal and a reply box are one tap away. A plain tap focuses the board window that is already open and moves it to the session (the service worker posts `{type: 'nav', url}` to the page); with no board window open it opens one. A notice that waits on you (a waiting session or a permission) stays on screen until you deal with it, and the home-screen icon badge shows how many sessions need a look (iOS 16.4 and later, Android Chrome).

**One notice per session on the phone.** Every Web Push carries the session's tmux name as its tag, so a session's notices replace each other on the lock screen instead of piling up, and the replacement buzzes again (renotify). The payload is `{title, body, url, tag, renotify, agent, state, tmux, perm_id, actions, badge, ts}`; a service worker that only knows the old `{title, body, url, tag}` still shows it.

**Switches.** Settings › Notifications has one switch per kind: needs you, done, rate limit, error or crash, login problem (all on by default, kept on the box as kv `notify_prefs`, `GET/PUT /api/notify/prefs`). A kind that is switched off sends nothing on ntfy or Web Push, a permission request included (the request still waits on the board for its answer). Below the switches an example card shows the notice each kind sends, built by the same code as the real ones, and says what an iPhone shows. The two test buttons ignore the switches. Claude's own input-needed notification and the agent's own push are separate from these.

**Cooldowns.** One notice per session and state per 60 s, and a done notice per session at most every 120 s (`STATE_COOLDOWN`: the longer of it and the 60 s wins). A permission push is never throttled (each request needs an answer), but it counts as the session's waiting notice, so the notification Claude raises when its own prompt appears does not buzz a second time. A rate limit is the account's, not the session's: it is announced once per agent and window (kv `rl_notified:<agent>:<resets_at>`), never twice for one agent within 15 minutes, and the session's own limit error adds nothing (one session once retried the same limit 482 times). A login problem is announced once per account per 15 minutes. Without a public URL there is no link and no ntfy buttons.

### Terminal commands (v0.5.7)

Three endpoints let the board type into an agent's pane safely. `POST /api/sessions/{name}/command {cmd, arg?, wait_ms?, confirm?}` types one slash command from the agent's allowlist (Claude: /clear /compact /usage /effort /model /rename /context /status /cost /fast; the board never types anything else). It clears the composer with C-u first. A command that takes an argument (/model, /effort, /rename) needs one, as one line of at most 200 characters. The read commands (/usage /context /status /cost) wait `wait_ms` (default 1200, at most 4000) and return the pane in `screen`. /clear is destructive and needs `confirm: true`. `POST /api/sessions/{name}/prompt {text, enter=true, queue=false}` pastes up to 20000 characters with bracketed paste, so newlines stay inside the prompt. It does not set `last_prompt`: the UserPromptSubmit hook does that, which also proves the paste landed.

Both refuse with 409 `{error, message, state, wait_kind, retry}` instead of typing into a pane that cannot take it: a shell or unknown row (`not_an_agent`), no open row, a session that is working (`/prompt` with `queue: true` is allowed, Claude queues the text), asking a permission or an elicitation question, with an undecided permission request, compacting, ended or with no state yet. `retry` is the number of seconds to wait before asking again (null when waiting will not help). The board types only when the session is idle, done, errored or idle-waiting.

The result of a command is confirmed passively. /command records `flags.pending_cmd = {cmd, arg, at, before}`, where `before` holds the model, effort and fast values on screen at that moment. The next statusline settles it into `flags.last_cmd = {cmd, arg, at, confirmed}`: /model is confirmed when the statusline's model name or id contains the argument or differs from `before`, /effort and /fast by their own statusline fields, and every other command by the next statusline. A command not confirmed within 20 s is reported `confirmed: false`. Every command and prompt is recorded as a `BoardCommand` or `BoardPrompt` event.

`POST /api/sessions/{name}/resize {cols, rows}` sizes the tmux window (40..400 columns by 10..200 rows) with `resize-window`, then unsets the window's `window-size` pin so the next client that attaches sizes it the usual way. It answers 409 with the viewers while a full (writable, sized) client is attached, because that client owns the size and tmux would snap the window back. The new size shows up as `win: [cols, rows]` on `/api/sessions/{name}` and `/api/state`. `POST /api/permission/{pid}/tui` is a third decision next to allow and deny: nobody answers from the board, the waiting hook returns no behavior, and Claude shows its own prompt.

### Hooks v2 (v0.5.7)

ccboard now registers 15 Claude Code hook events (it was 6): SessionStart, UserPromptSubmit, Notification, Stop, StopFailure, SubagentStart, SubagentStop, PreCompact, PostCompact, PostModelSwitch, TaskCreated, TaskCompleted, PostToolBatch, ConfigChange (all async, timeout 5, via `bin/ccboard-hook`) and SessionEnd (via `bin/ccboard-hook-fast`: not async, timeout 3, a 2 s curl cap, so the event lands before the process exits). PermissionRequest is unchanged. WorktreeCreate and WorktreeRemove are deliberately not registered.

The container entrypoint re-runs `scripts/claude_settings.py install` on every start, so a box with the old 6-event set is upgraded in place on the next deploy. No install.sh rerun is needed, and re-running install changes nothing. Other tools' hooks in settings.json are kept. `claude_settings.py` takes `--settings PATH` (edit another settings file; handy with `show`) and `--with-worktree-hooks` (accepted and ignored until V9). Both wrappers read `CCBOARD_CURL_MAX` (request cap in seconds, default 3; the fast wrapper is fixed at 2) and send `X-CCBoard-Agent: ${CCBOARD_AGENT:-claude}`.

What the board now does with them:
- A turn that was not typed by a person (a `<\task-notification>`, a `<\system-reminder>` or `[SYSTEM NOTIFICATION`, a `<pasted_content` placeholder, a slash or bash echo) still marks the session working but is never shown as its last prompt. It is kept in `flags.last_system_turn`.
- A permission you answered in the terminal clears 'needs you' at the next tool batch (PostToolBatch flips a permission wait back to working; an idle wait is left alone). PostToolBatch is never stored as an event.
- Waits are typed (`flags.wait_kind`: permission, idle or elicitation). An elicitation answered clears its own wait.
- Hooks from claude-mem's observer sessions, or from a conversation another open session owns, are ignored. A /resume or /clear on the same session rebinds its conversation id.
- Stop uses Claude's own closing message for the row text (first 300 characters) and keeps the full text in `flags.last_result`. A resume records how long it sat and how big it is in `flags.resumed`. /clear and /resume no longer flash the session as ended. A compaction restart no longer flips a working turn to idle.
- The statusline adds effort, fast mode, thinking, session name, prompt-cache state, PR, worktree, repo and the over-200k flag to each session's stats. The newest raw statusline payload is kept in kv `statusline_sample` (at most 8 KB) so the doctor can show the real shape on the box.

### Doctor and the agents API

`GET /api/doctor` (optional `?group=box|terminal|claude|notify|memory|codex` and `&refresh=1`) runs the box checks (tmux, ttyd, code-server, git, gh, ccusage, ntfy, push, identity, Claude binary / login / hooks) with a 5 s cap each and a 20 s cache; every failing check carries a fix text and, where it makes sense, the command to run. `GET /api/agents` describes the installed agents (Claude and Codex: identity, install, login and hooks state, the launcher's option schema, permission modes, efforts, models, the reasoning levels per Codex model, what this box's binary can do as `capabilities`, and the slash commands the board knows; [The launcher](#the-launcher-v0513) says how it is used; the Codex catalogue and the flag sets of both binaries are cached per binary, so only the first request after a start, or after the binary changes, runs a probe), `GET /api/sessions/<tmux>` returns one session with its task and flags, and `GET /api/external?agent=claude` lists Claude Code sessions and background jobs the board did not start (read from `~/.claude/sessions` and `~/.claude/jobs`, display only). All four are read-only and sit behind the same identity check as the rest of the API.

### The terminal on a phone

`/term/<session>` is built for a thumb on a 390 px screen (and an iPad in Split View), and just as usable in a laptop window.

- **Key bar**: three rows of 44 px keys under the terminal. Row 1 Esc, Tab, Shift+Tab, Ctrl-C, Enter; row 2 the four arrows and ⌫; row 3 PgUp, PgDn, Top, Bottom and Ctrl+O (Claude Code's transcript view). The keys never take the focus, so the soft keyboard stays up while you use them. Hold an arrow or ⌫ for 0.4 s and it repeats about eleven times a second (the repeats travel in batches of at most 20 keys, so a slow connection catches up in one request). While the soft keyboard is open the bar shrinks to Esc, Ctrl-C, Tab, Shift+Tab, Enter and a *More* toggle for the other rows. The *keys: auto / compact / full* button beside the quick replies overrides that for this device. A pending permission adds a y⏎ / n⏎ row (remote approve is off while this page is open, so the question is answered in the terminal).
- **Scrolling**, three ways that do not depend on each other. (1) Swipe up or down on the terminal: the swipe becomes mouse-wheel steps inside the terminal (one per 18 px of travel, with a short flick of momentum; a swipe that starts within 24 px of the left screen edge is left to iOS, where it is the back gesture), and tmux's mouse mode routes the wheel: Claude Code's fullscreen UI scrolls its own transcript, a shell pane enters tmux's scrollback. (2) The round buttons on the right edge (page up, page down, top, bottom). (3) PgUp, PgDn, Top and Bottom on the key bar. Buttons (2) and (3) ask the board to scroll (`POST /api/sessions/<tmux>/scroll`), which drives tmux's copy-mode for a shell pane (and for Codex without its alternate screen) and forwards PageUp, PageDown, Ctrl-Home and Ctrl-End to Claude's fullscreen UI. While a pane is in tmux's scrollback a **History** chip shows over the terminal: tap it to go live again. Typing from the page (the send box, the quick replies, the keys other than the scroll keys, arrows and Esc) leaves the scrollback first, so the text lands at the prompt.
- **A− / A+** change the terminal's font size (remembered on the device). The page never zooms or bounces: it is pinned to the visible viewport, so the header, the key bar and the send box stay where they are when the soft keyboard opens, and pulling down does not refresh the page. On a phone with the keyboard open the context strip and the quick replies fold away to give the terminal the room.
- **Header and context strip**: session, agent glyph and state, then project / repo, model and context %; the chevron at the right edge shows or hides a strip with the task title, your last prompt (two lines, tap to expand) and the question Claude asked or the end of its last message (remembered per device). The page re-reads the session and its pane every 3 seconds, and pauses while it is hidden. The back chevron returns to where you came from when that was the board, and goes to the Agents page otherwise (a notification tap or a shared link).
- **Send box**: Enter sends, Shift+Enter or the ↵ button adds a line, and multi-line text goes into the pane as one paste. The quick replies above it are editable (the pencil). An empty box plus Enter sends a bare Enter.
- **Wide windows** (840 px and up) put the terminal on the left and a 300 px column on the right with the strip, the keys, the quick replies and the send box.
- `POST /api/sessions/<tmux>/scroll` takes `{"dir": "up|down|top|bottom|exit", "n": 1..10}` and answers `{ok, mode, alt, pos}`; `GET /api/sessions/<tmux>/pane` reports the pane (alternate screen, copy-mode, scroll position, sizes) and who is attached; `/keys` also accepts Shift+Tab (`BTab`), `C-o`, `C-Home` and `C-End`.
- `bin/ccboard-attach <session> [full|grid|ro]` is what ttyd runs: `full` (the default, what this page uses) is a normal client, `grid` a client that never resizes the window (`attach -f ignore-size`, for tiles) and `ro` a read-only one. tmux's `aggressive-resize` and `focus-events` are off. `install.sh` applies a changed `tmux.conf` to a running tmux server without restarting it.

### Series and usage summary

The board keeps its own time series in the `samples` table (rate-limit windows, context and cost per session, state changes, hook events per hour, host health and session counts, rate-limit episodes), written by the hooks and a 15 s sampler and pruned per series (14 to 180 days). `GET /api/series?series=rl_5h,rl_7d&key=claude&since=24h` returns aligned, downsampled columns (at most 8 series/key pairs and 500 points), `GET /api/series/events?series=lim&since=7d` the raw events, and `GET /api/usage/summary?days=30&tz_min=345` the per-day, per-project and per-session roll-up in the viewer's time zone (Kathmandu by default). These feed the Usage page; the header pills still come from the statusline (and, when no session reports, Claude Code's own usage cache) and ccusage.


### The terminal page (tuning strip, quick replies, queue)

"Replace the clause 'editable quick replies' in the Mobile terminal bullet (README l.39) with: 'quick replies you edit in a dialog (the pencil, or hold a chip: one reply per row, add, Reset to the defaults, Enter saves, Esc cancels, at most 12; the same list shows as the nudge chips in the session peek)'.\n\nSuggested paragraph for the terminal section:\n\n- **Quick replies**: the chips under the key bar and the nudge chips in the session peek share one list per session (browser storage, key `ccboard:quick:<session>`). Until you edit it, it is *continue, merge, push, pr, add commit push, do it*. Tap a chip to send it with Enter. Tap the pencil, or hold a chip for half a second (right-click on a desktop), to open the editor: one reply per row, *+ add*, *Reset* (back to the defaults), *Save* or Enter to keep, *Cancel* or Esc to leave the list as it was. Blank and duplicate lines are dropped and the list holds up to 12 replies.\n\nSuggested addition to the command palette section:\n\n- **Controls**: /compact, /context, /cost, /usage and /status for the session in focus go through `POST /api/sessions/<name>/command`. When the board refuses (the session is working, asking a permission, compacting), the palette shows the reason and when to try again instead of typing into a busy pane. A read command (/usage, /context, /cost, /status) opens the captured screen in a dialog and presses Escape when you close it, so Claude's own dialog does not stay open. Labels and order follow the agent's command registry once the terminal page has fetched it (kept for 10 minutes in the tab); an older server without `/command` still gets the text typed through `/keys`.\n\nSuggested line for the Tasks section: the Preview button's dev-server port question is now a dialog, not a browser prompt.\n\nSuggested line for the changelog or the shell/CSP notes: no screen uses `window.prompt` any more; `askLine()` and `quickReplyEditor()` in components.js are the dialogs, and a node test fails the build if one comes back."

**Terminal page, tuning strip (v0.5.8)**

Under the context strip the terminal page now has a tuning strip for Claude sessions. It is hidden for shell rows and for the login terminal. It has two rows of chips:

- **Commands.** Clear, Compact, Usage, Rename, Context, Status, in the order of the agent's slash registry weight (measured use counts). Cost and Fast are weight 0 and stay out. Set `localStorage` key `ccboard:term:allchips` to `1` to show them.
- **Effort and model.** Effort is a segmented row (low, medium, high, xhigh, max, ultracode). Model is a row of chips (opus, fable, sonnet, haiku). The current effort and model are highlighted from the statusline.

Each chip is one `POST /api/sessions/<name>/command`.

- **Gate.** The chips work only while the session sits at its prompt: idle, done, or waiting on the idle prompt, with no compaction and no permission request open. Otherwise they are dimmed, and a tap on the strip explains why ("Available when the session is at its prompt").
- **Clear.** It is a two-tap action. The first tap turns the chip into "Confirm clear" for 4 s. The second tap sends `{cmd:'clear', confirm:true}`.
- **Rename.** It opens a small dialog with one input.
- **Read commands.** Usage, Context and Status open a bottom-sheet readout with what Claude printed. Closing it sends one Escape, because the command leaves Claude's own dialog open. The next command waits for that Escape.
- **Set commands.** A set command shows its chip as pending until the next statusline confirms it, then flashes green for 1.5 s. After 20 s without a confirmation it toasts "not confirmed by the statusline".
- **Refusals.** A refused command (409) toasts the reason and when to retry.

**Registry.** The strip reads `GET /api/agents` once per page load. It keeps the answer in sessionStorage under `ccboard:agents` for 10 minutes. The command palette reads the same entry. If the fetch fails, an embedded copy of Claude's registry is used.

**Send box.** The send box posts `POST /api/sessions/<name>/prompt`. While the session is working the button reads "queue" and posts with `queue:true`, so Claude queues the text. A shell row, a permission or question dialog, a row that has not reported a state yet, and an older server without `/prompt` still use the raw key path (`/keys`). A refused send puts the text back in the box.

### The Usage page

`#/usage?range=24h|7d|30d` (default `7d`, remembered in this browser; a `range` in the link wins and is written back, and `r` cycles it) is the page for "am I about to hit a limit, and where does the money go", in the order a phone-heavy evening needs it: **Limits** (the 5H and 7D gauges with their reset countdowns, and under them one chart of both windows over the range, with dashed guides at 60 and 85 %, a marker at every limit episode and a tick at each reset; labelled *statusline (official)*), **Cost per day** (stacked bars by project or by agent, top six plus *other*, a day without spend drawn as a hatched stub; labelled *API-equivalent (ccusage list price)*, with tokens beside the dollars in the hover text and a note when sessions have tokens but no price), **Sessions** (the ten biggest, with short model chips, dollars, tokens and hours; a session that is live right now has an **Open** button that goes straight to its terminal, any other row links to its project, and tapping a row opens a ctx % and session $ chart for the last 24 h), **Projects** (cost and active hours side by side, so a $/h that differs fifteen-fold shows), **Activity** (a 24 x 7 heatmap and an hour-of-day profile of hook events, in your local time) and **Timeline** (a Gantt of every session's state over the last 24 h, 48 h on a desktop, ended rows faded). `(unattributed)` is its own bar and row everywhere: sessions ccusage lists that the board did not start (started by hand, workflow subagent runs, history from before the board) are never folded into a project. The page paints a skeleton first, fetches `/api/usage/summary` and `/api/series` in parallel, refetches every 60 s while the tab is visible (never on the 3 s state poll), switches range from cache at once, and loads uPlot (vendored, MIT, about 50 KB) only when you open this page. The Home usage card is the way in: its **Usage →** link and both gauges open `#/usage` in one tap. `?demo=1` fills every section from the fixtures under `app/static/demo/` (`series.json`, `usage_summary.json`, `series_events.json`).

### Accounts and subscription usage

A Claude subscription is measured in windows, not dollars, so the board tracks usage per account: the 5-hour and 7-day window percentage (the real unit), tokens, active hours, and API-equivalent dollars (secondary, never the lead), with a total across accounts. It keys an account by `accountUuid`, read from the `oauthAccount` object of Claude's state file (`<config dir>/.claude.json`, or `~/.claude.json` for the default dir). If the file names no account, it falls back to the cached `claude auth status` and keys by email. Only the whitelisted identity fields are read, at most once a minute. The credentials file is never opened. The statusline payload names no account, so a reading is attributed by its fingerprint: the pair of `five_hour.resets_at` and `seven_day.resets_at`. An account whose remembered reset times match the reading's (equal, or whole windows apart) owns it. So a session still running on account A's token after a `/login` to B keeps feeding A's series, while the pills, the scheduler's quota guard, the limit banner and auto-continue keep following the current account. When a reading matches nothing, the identity is re-read at once, so a switch is noticed even before the 15 s tick. Per-account windows are recorded as `rl_5h` and `rl_7d` with key `acct:<key>` (for example `/api/series?series=rl_5h&key=acct:<key>`). `/api/series/events?series=acct` lists every switch (meta `{from, to}`). `lim`, `state` and `cost` samples carry `acct`, and session rows carry `account`. `GET /api/accounts` lists the known accounts with their latest windows, `PATCH /api/accounts/{key}` with `{label}` renames one (label only), `state.accounts` carries the same list in small form, and `accounts.headroom()` gives per-account room left for later features such as the launcher's "most headroom" choice and the dispatch gate. The usage summary adds `accounts[]` and a `total` with per-account windows and headroom lists. History from before account tracking is shown as "(before account tracking)".

ccboard measures usage per Claude subscription account, in the subscription's own unit first. Each account has a 5-hour and a 7-day window, and the board shows how much of each is used and when it resets. Tokens, active hours and the API-equivalent dollars sit next to those. The dollars stay secondary, because on a subscription they are not what you pay. The identity of an account comes from `~/.claude.json` (its `oauthAccount`) and, as a fallback, `claude auth status --json`. No credential file is ever read. The sampler records each account's rate-limit readings under the series key `acct:<key>`. The older `claude` key keeps following the current account, so the pills, the scheduler's quota guard and auto-continue behave as before. Switch accounts with `/login` and the board notices, writes an `acct` event, and starts crediting new samples to the other account. Samples that carry no account tag are placed by the account that was current at the time. History from before the tracking shipped appears as a single "(before account tracking)" row. `GET /api/usage/summary` returns `accounts[]`, one row per account with its current flag, 5h and 7d windows (value, reset time, when read), today / 7d / 30d totals (dollars, tokens, active hours, sessions) and its limit-hit count. It also returns `total`, the sum over accounts with the number of accounts and per-account headroom lists (what is left in each window, most room first), `rate_limits.by_account`, and an `acct` on every episode. Only Claude usage is counted per account; Codex has its own row in `by_agent`. When a session spans a `/login`, the day's spend is split between the accounts by the steps of its cost samples, so the account figures always add up to the day's bar. A window whose reset time has passed counts as fully open in the headroom lists, and an account with no reading yet is left out rather than assumed empty.

**When no session is running.** The pills, the gauges and the account rows keep a live-looking picture from the last reading the board holds, from Claude Code's own `cachedUsageUtilization` in its state file (the same file the identity comes from; the 15 s tick reads only that key, and only when the file changed and the cache is newer than every reading the board has) and from the reset arithmetic: a window whose reset time has passed with no newer reading shows 0 % and counts down to the next reset (the old one plus whole windows of 5 hours or 7 days), and every figure says how old it is and where it came from (*updated 5m ago · from the last session*, or *from Claude Code's cache*). A running session's statusline is still the live source: its readings are newer than the cache, so they always win.

**Refresh.** The *Refresh* button beside that caption asks one of your idle Claude sessions for `/usage` (`POST /api/usage/refresh`: a session at its prompt, one nobody is attached to first; 409 with a plain reason when none qualifies, and with none running the button opens the launcher); Claude Code fetches the numbers with its own login, and the board reads them from Claude Code's state file. Opening the Usage page asks once by itself when the newest reading is older than five minutes and a session nobody is attached to is at its prompt; nothing runs on a timer, and no credential file is read.

### Several Claude accounts

Claude Code keeps one login per config dir, so using more than one subscription means putting the right login in place when you want it. The board saves each login and swaps them for you. (Linux only: macOS keeps Claude's credentials in the Keychain, so there Settings says saved logins need Linux and the account calls answer 409.)

**Adding an account.** In Settings, Add account (with the email to pre-fill, if you like) starts `claude auth login` in a login session of its own with an empty config directory, so the login you are using is not touched. Copy the sign-in link from Settings, open it in a browser that is signed in to the other account, and paste the code the browser shows into Settings. The board saves the new login and lists the account. It becomes the live one only when nobody was logged in or it is the account you were already using. One add runs at a time; Cancel gives it up. If the login session ends without credentials for 20 seconds, or an add sits for an hour, Settings shows "the login did not complete".

**Switching.** Switch on an account replaces the live credentials with its saved copy and changes the account named in Claude's state file (every `.claude.json` Claude may read, and no other key in it). The login you leave is saved first, so a switch is always one tap from being undone, and a switch that cannot be applied puts the previous login back. Sessions that are already running pick the new login up on their next request (Claude re-reads its credentials file when it changes); nothing restarts. Switching with *continue parked sessions* ticked also types `continue` into the sessions that stopped on a limit, because the new account has room. The board refuses a switch while an add is waiting for its code, and when it cannot first save the login it is leaving.

**Where the logins live.** `<data dir>/accounts/<slot>/`, a slot being the first 24 hex characters of a hash of the account key (no email appears in a path). Directories are 0700, files 0600: `.credentials.json` (a byte copy of Claude's file), `.credentials.json.prev` (the generation before it, for recovery) and `.claude.json` (which account it is). The credentials are treated as an opaque blob: the board copies the bytes and never parses, logs or returns any part of them, and never runs `claude` against a slot. A refresh rotates a token, so only one copy of a login is ever in use. While an account is live its live file is the truth, and the board saves it again whenever it changes and has been still for 30 seconds; while the account is not live the saved copy is. The nightly backup never takes them: they are not among the snapshot paths, and a `CCBOARD_BACKUP_EXTRA` entry that is the data dir, the accounts dir, inside it or above it is skipped with a warning. Copy them off the box yourself if you want them kept.

**`/login` in a terminal still works.** The board notices the new account within a tick, saves its login once the credentials file has been quiet for a few seconds, and lists it. A session that was running before a switch may write its old account name back into Claude's state file; for 90 seconds the board reads that as the new account, and it puts the right name back into the file (up to five times) while the credentials are still the ones the switch wrote.

**Forgetting.** Forget on an account deletes its saved login; the account and its usage history stay, and the live account cannot be forgotten. The calls behind Settings are `POST /api/accounts/login` (`{email?, restart?}`), `POST /api/accounts/login/code` (`{code}`), `DELETE /api/accounts/login`, `POST /api/accounts/{key}/switch` (`{continue_parked?}`) and `DELETE /api/accounts/{key}/saved`; `GET /api/accounts` and `/api/state` carry `saved` on every account, `store: {supported, reason, count}` and, in `login`, `adding`, `email`, `started_at` and `result`.

### Several Codex accounts

The same idea for Codex: the board keeps a saved login per Codex account and puts the one you pick in place with one tap (Settings, Accounts, below the Claude section). Codex keeps its login in one file, `$CODEX_HOME/auth.json` (`~/.codex/auth.json` unless CODEX_HOME says otherwise), on every OS, so this works wherever a `codex` binary does; without one the section says "codex is not installed".

**What is stored.** `<data dir>/codex-accounts/<slot>/`, never under `/tmp` (Codex refuses to create its helper binaries under a temporary directory, and the login runs with a CODEX_HOME in there). A slot is the first 24 hex characters of a hash of a random token minted when the account is added, so the account key is the slot's name and no email, id or label appears in a path. Directories are 0700, files 0600: `auth.json` (a byte copy of Codex's file, treated as an opaque blob: never parsed, logged or returned), `auth.json.prev` (the generation before it) and `meta.json` (label, when it was added, and the account id, plan and last-seen time once known; it lets a restored database find its slots again). The kv keeps `codex_accounts` and `codex_account_current`. The nightly backup never takes the directory (an extra path that is, holds or sits inside it is skipped with a warning).

**Naming.** Codex shows no email at login and nothing readable in the file says whose it is, so you name an account when you add it (required, 1 to 60 characters) and rename it later (Rename; a name is always required). The board then learns the account's id and plan by itself: the first line of every rollout Codex writes (`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`) names the account that created the session, and its rate-limit events name the plan. Only sessions started after the account became live count, and when they name more than one account (another Codex process may still hold the previous login) nothing is learned. Two saved accounts that turn out to have the same id are one account: the older key stays, with the newer label, and the newer slot goes.

**Adding an account.** Settings, Add a Codex account: type the name, then the board runs `codex login --device-auth` in its login session with a CODEX_HOME of its own (an empty directory under the store, seeded with a copy of your `config.toml` so model and provider settings apply, and nothing else), so the login in use is not touched. Settings shows the link to open and a one-time code, large, to type on the page that opens (Copy link, Open, Copy code); nothing is pasted back. The page then waits ("waiting for the sign-in…", the board looks for the login every second, for a device code's 15 minutes) and the account appears as a saved login. If no Codex login was in place the new one is put in place at once; a login that was in place and that no slot holds is kept as an account called "codex login <date>" first, so nothing is ever replaced without a copy. `--device-auth` needs codex-cli 0.157 or newer (the box runs 0.160); on an older one the add block says `update Codex to 0.157 or newer: npm install -g @openai/codex` and switching still works.

**Switching.** Switch replaces `auth.json` with the account's saved copy in one atomic replace; the login you leave is saved first (a token Codex refreshed meanwhile travels with its account), and a switch that cannot be applied leaves the previous login in place. It is refused (409) while one of the board's own Codex sessions is open, because a running Codex keeps its login in memory and would write it back: close them first. Codex programs the board did not start (a Hermes agent's app-server daemon, a Codex in a terminal) keep the login they read when they started until they restart, and may write that account's refreshed token back into `auth.json`: the switch answers `warnings` when it sees any (`other Codex processes on this box keep the previous login until they restart`), Settings shows it as a toast and under the list, and for ten minutes the tick puts the new login back (up to five times) if the file reverts to the previous account's exact bytes. Bytes that match no slot are left alone in that window (nothing in the file says whose refresh they are) and saved into the current account's slot only after it. The live file is the truth: a file identical to one slot's copy names that slot as current, so a hand-made copy is adopted; a file that differs from the current account's slot and has been quiet for 30 seconds is its refreshed token and is saved into the slot.

**Forgetting.** Forget login deletes the saved login; the account's record stays (no saved login) and the account in use cannot be forgotten. Log in again on such a row repeats the add flow with its name.

**The calls.** `GET /api/codex-accounts` (`{current, list: [{key, label, account_id, plan, saved, current, added_at, last_seen}], store: {supported, add, reason, count}, login: {running, adding, label, started_at, url, code, tail, result}}`), `POST /api/codex-accounts/login` (`{label, restart?}`, 202; 400 without a label; 409 without a codex that has `--device-auth` or while a login runs), `DELETE /api/codex-accounts/login`, `PATCH /api/codex-accounts/{key}` (`{label}`), `POST /api/codex-accounts/{key}/switch` (`{ok, already, from, to, warnings, accounts}`; 404 unknown, 409 for no saved login, a login in progress, an open board Codex session or an unsaved current login) and `DELETE /api/codex-accounts/{key}/saved` (409 for the live account). Errors are `{detail, error}`. `/api/state` carries one new key, `codex_accounts`, the GET body without `login.tail`; the switch writes a `cacct` event (key = the new account, meta `{from, to}`).


### Login expiry and re-login

Claude Code and Codex renew their own access tokens with their refresh tokens whenever they run; the board does nothing there. What can expire is a login that sits unused for a long time: the first request after switching to it fails with an authentication error and the agent asks to log in. The board never reads a credentials file (`.credentials.json`, `auth.json`) to find that out: it copies them as opaque bytes and learns about a dead login only from a failure (a Claude session's `StopFailure` hook reporting an authentication error) and from file ages. A failure raises `state.accounts.problem`: the Home banner ("Claude's login (<label>) is not valid any more", with Log in again, Switch back to <previous> when it follows a switch made in the last ten minutes, and dismiss), an amber topbar chip, an amber "login not valid any more" chip on that account's row in Settings > Accounts (where its Log in again becomes the row's lead action), one notification per account per 15 minutes and a warning in the doctor's Claude login check. It ends by itself when a session on that account shows a sign of life (a statusline reading from a working session, a successful turn), when a login for it finishes, or with dismiss (`DELETE /api/accounts/problem`).

To log a saved account in again, use Log in again on its row (every row has it, the account in use included): the finished login replaces that account's saved login (the old one is kept as `.prev`), the account stays one row, and for the account in use the fresh login goes live at once. For Codex, `POST /api/codex-accounts/login {replace_key}` does the same into the same slot, keeping its key and name; if one of the board's own Codex sessions is open the fresh login stays saved ("not in use yet") and goes live by itself on a later tick once none is open. A login made as somebody else than the row asked for is saved as its own row and the result says so. Every saved row shows a quiet "login saved <age> ago". Codex has no failure hook and its rollouts carry no authentication-error item the board knows, so a dead Codex login shows in the terminal; the row's Log in again fixes it.

### Install as an app

The board is a PWA, so it can live in its own window on a laptop and on the Home Screen of an iPad or phone (open it from a tailnet device over the HTTPS URL `install.sh` printed; installation needs HTTPS). **Settings › App** shows whether this window is installed, has the Install button where the browser offers one, lists these steps, shows the build id and has a *Reload app* button (an installed app has no browser reload).

- **Chrome or Edge (Windows, Linux, macOS)**: the install icon at the right of the address bar, or menu › *Install ccboard* (Edge: … › Apps › *Install this site as an app*). The installed window uses the title-bar overlay: the board's topbar is the title bar, its empty part drags the window, and the window buttons sit over its right edge (macOS: the traffic lights over its left edge). A shortcut or a notification tap refocuses the window that is already open instead of starting a second one.
- **Safari on a Mac (Sonoma or later)**: File › *Add to Dock*. The board then opens in its own window.
- **Safari on iPadOS or iPhone**: Share › *Add to Home Screen*. This is the only way to get Web Push on iOS and iPadOS (16.4 or later). In Split View the board follows the window width (a narrow split gets the phone layout, a wider one the rail layout), and it works with a hardware keyboard and a trackpad.
- **Chrome on Android**: menu › *Install app*.

What the installed app adds:

- **Keyboard shortcuts** (⌘ on a Mac and iPad, Ctrl elsewhere; `?` lists them in the app): ⌘/Ctrl+K opens the command palette (sessions by name, routes, the nudges *continue / merge / push / pr / add commit push / do it*, `/compact`, `/context`, `/cost`, `/usage`, `/status` for the selected session, the *ultracode* and *plan* modes), ⌘/Ctrl+1…9 jumps to the nth session of the Agents roster, ⌘/Ctrl+\ toggles the sidebar, ⌘/Ctrl+J the terminal dock (once the dock exists), `/` focuses the search, `g` then `h i a t q u m s` goes to Home, Needs you, Agents, Tasks, Quad, Usage, Memory, Settings, `c` then `s t p r` opens the new session, task, project and import sheets, and in the Needs you and Agents lists `j`/`k` move, Enter opens the peek, `o` the terminal, `a` acks, `y` allows, `d` denies and `r` focuses the reply box. Shortcuts do nothing while you type in a field, while a dialog is open or while the terminal has the focus (the terminal keeps every key).
- **Resizable sidebar**: drag the strip at the sidebar's right edge (200 to 400 px; arrow keys work when it is focused, double-click resets). The width is remembered per device.
- **Share target** (Chrome and Edge, desktop and Android): once installed, *ccboard* appears in the system share sheet; sharing a page or a text snippet opens the board with the palette's *send to session* list and the shared text prefilled, so it can go to a session as a prompt. Safari does not support share targets: on iPadOS copy the text and paste it into a session's send box instead.
- **Fast and never stale**: the app shell is cached by a service worker whose cache name changes with every deploy, vendored fonts and Blueprint are served `immutable`, and an open window reloads itself when the box is updated.

## claude-mem

[claude-mem](https://github.com/thedotmack/claude-mem) is a Claude Code plugin (`claude-mem@thedotmack`) that records what your sessions do and feeds it back to later ones. It runs one **worker** per box: an HTTP service on loopback (port from `CLAUDE_MEM_WORKER_PORT` in `~/.claude-mem/settings.json`, recorded in `~/.claude-mem/worker.pid`) that spawns an observer `claude` session per observed session. The board never writes to it; it reads its health.

**The install step.** `install.sh` looks for the plugin's `claude-mem@thedotmack` key in `~/.claude/plugins/installed_plugins.json`. When it is missing it prints and runs

```sh
claude plugin marketplace add thedotmack/claude-mem
claude plugin install claude-mem@thedotmack
```

(the second runs even if the first answers that the marketplace is already added) and goes on whatever happens: a failure is a warning, never an abort. It warns when the plugin is installed but disabled (`claude plugin enable claude-mem@thedotmack`) and when no `bun` can be found. The plugin's worker runs on bun. In 13.29.0 the plugin only looks for it (`bun-runner.js` and `version-check.js` search `PATH`, `BUN`, `BUN_PATH`, `BUN_INSTALL`, `~/.bun/bin`, `/usr/local/bin` and a few more; nothing in its hooks installs it), and `install.sh` does not install it either. `CCBOARD_CLAUDE_MEM=0` skips the step. Nothing here touches Codex (that comes with the Codex adapter). Docker mode needs nothing extra: the worker is a host process, the container shares the host network, and `docker-entrypoint.sh` never installs the unit.

**Why a service.** A worker is started by the plugin's SessionStart hook, so it is a child of whichever Claude session ran that hook first, and it inherits that session's environment. The plugin strips `CLAUDECODE` and `CLAUDE_CODE_*` when it spawns, but `CCBOARD_SESSION`, `CCBOARD_URL` and `TMUX_PANE` pass through. Every observer session the worker starts then runs the box's hooks as if it were that board session (seen on ubu2: its events landed on the user's row; v0.5.7 ignores child-session events, the service removes the cause). The doctor's *memory* group warns while a worker's environment still carries `CCBOARD_SESSION` or `TMUX_PANE`.

**`ccboard-mem.service`** (off by default: the hooks start a worker on demand today, and two workers must never race on `worker.pid`). Turn it on with `CCBOARD_MEM_SERVICE=1 ./install.sh`; turn it off with `CCBOARD_MEM_SERVICE=0 ./install.sh` (the unit file stays, the hooks start workers again). It is a system unit running as you, with `Restart=on-failure`, `Nice=5`, best-effort I/O priority 7 (not idle: the observer has to keep up) and `OOMScoreAdjust=200`. It is ordered `Before=ccboard-tmux.service ccboard.service` (ordering only, no `Requires`, so a failed or disabled unit blocks nothing), so after a boot the worker is up before the sessions the board recovers; the order is not a guarantee, and the doctor's `memory-env` check is the safety net: it warns while the worker holds `CCBOARD_SESSION` or `TMUX_PANE`, i.e. when a session's hook won the race and started a worker first. It does **not** read `/etc/ccboard/env` (that file holds the hub token and every `CCBOARD_*` setting). `ExecStart` is

```
/usr/bin/env -u CCBOARD_SESSION -u CCBOARD_URL -u CCBOARD_APPROVE_TIMEOUT -u TMUX -u TMUX_PANE -u CLAUDECODE -u CLAUDE_CODE_CHILD_SESSION  <checkout>/bin/ccboard-mem-run
```

(`<data dir>/app/bin/ccboard-mem-run` in docker mode, where `install.sh` copies the launcher next to the other host-side scripts), and `bin/ccboard-mem-run` resolves, on every start, the newest `~/.claude/plugins/cache/thedotmack/claude-mem/<version>/` that has no `.orphaned_at` file (Claude Code replaces that directory on every plugin update) and bun, drops every other `CCBOARD_*` variable, and execs

```
<bun> ~/.claude/plugins/cache/thedotmack/claude-mem/<version>/scripts/worker-service.cjs --daemon
```

That is the plugin's own start, in the foreground: `worker-service.cjs start` only spawns `setsid <bun> worker-service.cjs --daemon` detached and exits (useless under `Type=simple`), and `bun-runner.js worker-service.cjs --daemon` is wrong because bun-runner treats any argument other than `start|stop|restart|status` as a hook call and, with no stdin payload, writes `~/.claude-mem/CAPTURE_BROKEN` and kills its child. A second `--daemon` while one is healthy exits 0 without doing anything, so the unit then sits inactive and `Restart=on-failure` leaves it alone; a boot failure exits 78 and is retried every 5 s until the start limit (5 starts in 5 minutes) leaves the unit `failed`; fix the cause, then `sudo systemctl reset-failed ccboard-mem && sudo systemctl start ccboard-mem`. `ccboard-mem-run --print` shows what it resolved (exit 78 when the plugin or bun is missing). Override with `sudo systemctl edit ccboard-mem` (`Environment=CCBOARD_MEM_BUN=/path/to/bun` or `CCBOARD_MEM_PLUGIN_DIR=...`). The worker's port is not forced from here: the hooks read it from `~/.claude-mem/settings.json`, so change it there.

**Handing over from a hook-started worker.** If one is running when you enable the unit, `install.sh` enables it for the next boot, starts nothing, and prints these steps plus the `CLAUDE_MEM_WORKER_AUTOSTART` note below (do them when no session is mid-turn; the observer's queue is kept in claude-mem's database):

```sh
<bun> <plugin>/scripts/worker-service.cjs stop       # the plugin's own stop
sudo systemctl start ccboard-mem
# verify: the unit owns the worker, and its environment is clean (expect 0)
systemctl show -p MainPID --value ccboard-mem
tr '\0' '\n' < /proc/$(systemctl show -p MainPID --value ccboard-mem)/environ | grep -c '^CCBOARD_SESSION='
curl -s "http://127.0.0.1:$(python3 -c 'import json,os;print(json.load(open(os.path.expanduser("~/.claude-mem/worker.pid")))["port"])')/health"
```

If a hook wins the race after the `stop` and starts a worker first, the unit's start exits at once: repeat both commands.

**Day to day and plugin updates.** Restart the worker with `sudo systemctl restart ccboard-mem`, not with the plugin's `restart` (it hands the worker over to a process it spawns itself, outside the unit's supervision). Logs: `journalctl -u ccboard-mem`, plus `~/.claude-mem/logs/`. The unit is not in `/etc/sudoers.d/ccboard`.

A plugin update is the one thing that undoes the clean environment, and the old worker does not keep running until you restart it. Per the plugin's code (13.29.0, `worker-service.cjs`), the first hook of any Claude session after an update compares the running worker's version with the installed plugin's, logs `Worker version mismatch`, kills the unit's worker and spawns a replacement itself, with that hook's environment: `CCBOARD_SESSION` and `TMUX_PANE` are back. The unit's next start (its own `Restart=on-failure` after 5 s, or your `systemctl restart`) then runs a second `--daemon` that finds the hook's worker healthy and exits 0, so the unit sits inactive and the leaky worker stays. The doctor's `memory-env` check warns when that has happened. Recover as in the hand-over above: `<bun> <plugin>/scripts/worker-service.cjs stop`, then `sudo systemctl start ccboard-mem` (the launcher re-resolves the newest plugin version on every start).

**Optional, for boxes that run the unit: `CLAUDE_MEM_WORKER_AUTOSTART=false`.** The plugin has a switch for a worker that something else manages: the key `CLAUDE_MEM_WORKER_AUTOSTART` in `~/.claude-mem/settings.json` (default `"true"`; the string `"false"`, in any case, turns it off. Write it quoted: the plugin calls `.trim()` on the value as read, and a JSON boolean has no such method). Per the plugin's code (13.29.0), with it set the hooks never lazy-spawn a worker, never recycle a worker whose version differs from the plugin's (they log `using it as is`) and never recycle a wedged one, and the plugin's own `worker-service.cjs start` does not launch one either; `--daemon`, which is what the unit runs, and `stop` are not affected. So the unit becomes the only thing that starts a worker, and after a plugin update `sudo systemctl restart ccboard-mem` is the whole procedure. The trade-off is the mirror image: with the setting on, nothing starts a worker unless the unit runs, so while the unit is failed, inactive or stopped there is no memory capture at all, and the doctor's *claude-mem worker* check (`memory-worker`, *no worker on 127.0.0.1:...*) is what tells you. It is your decision and ccboard never writes it (`install.sh` only prints this hint after enabling the unit). To opt in, add this line yourself inside the top-level object of `~/.claude-mem/settings.json` (a fragment, not a file; mind the comma next to its neighbours):

```json
  "CLAUDE_MEM_WORKER_AUTOSTART": "false",
```

## Codex

ccboard starts, resumes and tracks Codex sessions next to Claude ones. Codex is a second agent, not a mode: the launcher takes `codex`, `codex-resume`, `codex-continue` and `codex-fork` (or `agent: "codex"` on the plain launchers, or the agent picker of the launcher sheet), a task takes `agent: "codex"`, and a Codex row carries the `◇` glyph. The adapter was written against codex-cli 0.145.0 (what ubu2 runs) and uses a newer flag only when `codex --help` lists it. Below: how the board starts Codex, then the box setup (hooks, the one-time trust review, the MCP server). `./install.sh` does the box setup when `codex` is on the box, and in container mode `docker-entrypoint.sh` repeats it on every start (guarded by `command -v codex`; `CCBOARD_SHADOW=1` leaves Codex alone).

### How the board starts Codex

**The launch line.** One line, built in `app/agents/codex.py` and nowhere else. Extra arguments never carry a flag the board owns.

```
codex [--no-daemon] [--dangerously-bypass-hook-trust] --no-alt-screen <permission flags> [-m <model>]
      [-c model_reasoning_effort="<level>"] [--search] [--add-dir <dir>]... [-p <profile>] [<extra>] [-- "<first prompt>"]
codex resume <same flags> <conversation id>      # resume
codex resume <same flags> --last                  # continue the last conversation of this directory
```

`--no-alt-screen` keeps Codex's scrollback in tmux. The prompt follows `--`, so a prompt that starts with a dash is still a prompt. There is no `-C`: the tmux session starts in the repo (or the task's worktree), which is where Codex works. `--no-daemon` is added only on a Codex that has it (0.157 and newer); `--dangerously-bypass-hook-trust` only with `CCBOARD_CODEX_HOOK_TRUST=bypass`.

**The permission map.**

| Mode | 0.145 (the ubu2 build) | Newer Codex |
|---|---|---|
| `default`, `acceptEdits` | `-s workspace-write -a on-request` | the same |
| `plan` | `-s read-only -a on-request` | the same |
| `dontAsk` | `-s workspace-write -a never` | the same |
| `auto` | `-s workspace-write -a on-request` (0.145's `-a` offers `untrusted`, `on-request` and `never`; there is no `on-failure`) | `--approve-for-me -s workspace-write` where `--approve-for-me` exists (0.157+); `-a on-failure -s workspace-write` on a build whose `-a` lists `on-failure` |
| `bypassPermissions` | `--dangerously-bypass-approvals-and-sandbox`, alone | the same |

The advanced Sandbox and Approval options replace their half of the mode (`approval: never` on `plan` is `-s read-only -a never`). Without a mode the launch line has no permission flags and Codex's own `config.toml` decides, as it does when you type `codex` yourself.

**Tasks and managed worktrees.** Codex has no worktree flag, so for a Codex task ccboard runs `git worktree add -b worktree-<slug> <repo>/.ccboard/worktrees/<slug>` (Claude's stay under `.claude/worktrees`), copies the repo's `.worktreeinclude` files into it, and starts the session inside it. Both folders are kept out of `git status` through `.git/info/exclude`; a launch that fails takes its worktree and branch with it. A project folder that is not a git repo runs the task in place, as Claude's does. A task never inherits its permissions from `config.toml`: with no permission mode it runs `default`, so its line always carries both `-s` and `-a`; an explicit sandbox or approval replaces only its own half (`approval: never` is `-s workspace-write -a never`). After a reboot a task comes back inside its worktree with its conversation id, or `codex resume --last` when none was bound (the worktree is its own directory, so that can only be its own conversation).

**Bypass rules.**

- `bypassPermissions` and `bypass: true` skip every prompt and the sandbox. They are an explicit choice on an interactive session; the launcher sheet asks for the red acknowledgement once per repo and sends `bypass: true` with `mode: "bypass"` (see [The launcher](#the-launcher-v0513)).
- `sandbox: danger-full-access` is the same class. It is refused unless `bypass: true` comes with it, and then the launch line is the one bypass flag. `approval: never` together with an explicit sandbox is your own call (Claude's `dontAsk`), not a bypass.
- Tasks, backlog dispatch, scheduled and headless runs, resume and reboot recovery never carry one: they refuse `bypassPermissions`, `danger-full-access`, any `bypass`/`yolo`/`dangerous` option and any such spelling in extra arguments, and a stored option set never holds one, so a relaunch cannot inherit it.
- Extra arguments cannot carry `-c`, `-p`, `-s`, `-a`, `-m`, `-C`, `--enable`, `--disable`, `--strict-config`, `--remote*`, `--search`, `--no-alt-screen`, `--sandbox`, `--ask-for-approval`, a bare `--`, or anything containing `dangerously`, `yolo` or `bypass`; use the launch controls instead.
- `--dangerously-bypass-hook-trust` is a separate switch (`CCBOARD_CODEX_HOOK_TRUST`, below).

**The capability probe.** `codex --help` and `codex exec --help` run once per binary (path, mtime and size, so a `codex update` is noticed) and are cached; a flag is used only when it is defined as an option in that text, not merely mentioned. A failed probe is retried after a minute and the 0.145 flag set applies meanwhile, so a line can always be built (with no Codex installed too). `codex exec`, which headless runs use (v0.5.16), gets `-a never` and `--search` only when `codex exec --help` lists them; neither 0.145 nor 0.157 does. The model list comes from `codex debug models`, cached for an hour (the first `/api/agents` after a start shows a built-in list and refreshes it in the background). The real help texts of the ubu2 build are test fixtures (`tests/fixtures/codex_help_0145_real.txt`, `codex_exec_help_0145_real.txt`, `codex_resume_help_0145_real.txt`); the tests check that no launch, resume or continue line carries a flag those texts lack.

**The doctor group.** `GET /api/doctor?group=codex` (every check is a skip without Codex): `codex-bin` (the version; 0.145 is a pass that says 0.157+ adds `--approve-for-me` and `--no-daemon`), `codex-auth` (`codex login status`), `codex-hooks` (every ccboard event registered, PermissionRequest included unless `CCBOARD_REMOTE_APPROVE=0`), `codex-trust` (a trust record in `config.toml` for each ccboard hook; warns in `bypass` mode), `codex-alt-screen`, `codex-features` (Codex's `hooks` feature on), `codex-sessions` (the rollouts folder is readable), `codex-mcp` (`codex mcp get ccboard`) and `codex-repo-hooks` (`.codex/hooks.json` files inside projects: Codex reviews each one in `review` mode, they run unreviewed in `bypass` mode). The probes run side by side (`codex --version` once, beside `codex login status`), so the group fits the 5 s cap of one check.

**Other Codex processes.** ccboard's hooks sit in the global `hooks.json`, so every Codex process on the box calls the board: Hermes, `codex exec`, Codex Desktop. Every hook command names its agent (`env CCBOARD_AGENT=codex`, the permission hook included), and a hook or permission request whose agent is not the agent of the row it resolves to is ignored (`{"ignored": "foreign"}`): a Codex run that merely shares a Claude row's directory changes nothing and never parks a prompt on it. A shell row takes either, since you may run `claude` or `codex` by hand in it. Events of another thread of the same Codex process (a guardian reviewer, a subagent) carry their own session id: they are counted in `flags.subthreads` and never move the row's state. A SessionStart with a new id (`/new`, `/clear`, `codex resume`, `codex fork`) is the same session's next thread and rebinds the row, and so is any SessionStart with a new id while the row is idle, done or ended (those threads start mid-turn, never at rest).

**Recovery limits.** Until the rollout Tailer of v0.5.12 binds conversation ids, a Codex session row has no id before you have trusted the hooks. After a reboot such a row comes back with `codex resume --last` only when it is the only open Codex row in its directory; with a second one (or a bound one) next to it, it starts a fresh `codex` instead (the earlier conversation is not resumed) and `last_recovery.notes` says so, and no `continue` is typed into it. Another program's Codex run in the same directory is invisible to the board, so even a lone row's `--last` is a best guess; trust the hooks once and the ids bind on the first event.

### Headless Codex runs (schedules, batches, v0.5.16)

A schedule or a batch takes `agent: "claude" | "codex"` (`POST /api/projects/{p}/repos/{r}/jobs` and `POST /api/batch`; unset is Claude, so every old client works). A Codex job also takes `model` and `reasoning_effort` (or `opts: {model, reasoning_effort}`), stored in `jobs.opts` (an additive column). What a Codex run does:

1. `git worktree add -b worktree-<slug> <repo>/.ccboard/worktrees/<slug>` (the same managed worktree a Codex task gets, `.worktreeinclude` files copied in), because `codex exec` has no `--worktree`.
2. In that worktree, with `CCBOARD_SESSION=none` (the board's hooks recognise it as no board session and stay silent) and the board's `CODEX_HOME`: `codex exec --json -s <sandbox> [-a never] -o <data dir>/runs/<run id>.txt [-m <model>] [-c model_reasoning_effort="<level>"] [extra args] --skip-git-repo-check -- <prompt>`. The permission mode maps to the sandbox (`plan` = `read-only`; `default`, `acceptEdits`, `auto`, `dontAsk` = `workspace-write`); a headless run never prompts and never bypasses. It is not `--ephemeral`, so the rollout stays on disk. `-a never` appears only where `codex exec --help` lists it (neither 0.145 nor 0.157 does).
3. The run's result is the `-o` file (Codex writes its final message there), else the last `agent_message` of the JSON stream. `thread.started` gives the thread id (stored as the run's session id), `turn.completed` the token usage, `turn.failed` the error. A rate-limit message ("usage limit", 429) makes the run `rate_limited` and backs Codex off (its own key, `sched_backoff_until_codex`) for 30 minutes or until its window resets, whichever is later within 5 hours; Claude's jobs keep running.
4. The worktree becomes a task card with `agent = codex`, its branch and its worktree; *Resume in terminal* types `codex resume <thread id>` in it. A Codex that could not even start leaves no empty worktree behind.

What it does not do: no `max_turns` or `max_budget_usd` (Codex has neither, and the board refuses them for Codex with `max_turns: not supported by codex`), no dollar cost (Codex reports tokens; the cost join prices them later), no hook events (there is no session to attach to), and no `-c`, `-p`, `--profile`, `--enable`, `--disable`, `--sandbox`, `-a`, `-m`, `--add-dir` or bypass spelling in the extra args. Before each tick the scheduler checks, per agent, the login (`codex login status`; a logged-out Codex defers its jobs and pushes one notice, Claude's keep running) and the window: kv `rate_limits_codex` primary at 85 % or more (a window that already reset is not a reading), or a secondary window at 100 %. A chain step waits on its own agent's window the same way. A chain may cross agents (Claude writes, Codex reviews): each step runs in its own lane session with its own permission map.

### The box: hooks, trust and MCP

**The hooks.** `scripts/codex_hooks.py` is the twin of `claude_settings.py` and writes `$CODEX_HOME/hooks.json` (default `~/.codex/hooks.json`). The shape is Claude's: `{"hooks": {"<Event>": [{"hooks": [{"type": "command", "command": "...", "timeout": 5}]}]}}`.

| Event | Script | Mode and timeout |
|---|---|---|
| SessionStart, UserPromptSubmit, Stop, SubagentStart, SubagentStop, PreCompact, PostCompact | `bin/ccboard-hook` | `async`, 5 s |
| Interrupt, SessionEnd | `bin/ccboard-hook-fast` (2 s request cap: the event has to land before the turn is cut or the process exits) | synchronous, 3 s |
| PermissionRequest | `bin/ccboard-permission` (waits for a remote allow/deny, then yields to the TUI prompt; `CCBOARD_REMOTE_APPROVE=0` skips it) | synchronous, `CCBOARD_APPROVE_TIMEOUT` + 30 s |

PreToolUse and PostToolUse are deliberately not registered (they fire per tool call, and PreToolUse can block a tool). All ten commands read `env CCBOARD_AGENT=codex <app>/bin/<script>`: the board hears "codex" from the command itself, not from the session's environment, so a `codex exec` started inside a Claude session cannot pass itself off as Claude, and the board ignores a hook or permission request whose agent is not its row's. A Codex permission request shows on the phone with the files an `apply_patch` touches (its `*** Update/Add/Delete File:` lines), not the patch.

Why only those seven are `async`: on codex-cli 0.157.1 (checked through its app-server, one shim hook per event) an async hook runs in the background and shows no "running hook" line, while Codex runs a SessionEnd hook synchronously even when it says async (with a warning) and clamps the Interrupt and SessionEnd timeouts to 3 s. So those two are written synchronous with timeout 3, and `hooks/list` reports no warnings. If a Codex build skips `async` hooks (not checked on 0.145), set `CCBOARD_CODEX_HOOKS_ASYNC=0` where the installer runs (or pass `--no-async`): every hook is then written synchronous.

Other tools' hooks in `hooks.json` are kept, and the installer is idempotent: it rewrites ccboard's hook where it already sits (or appends a group after what is there), never moving it, because Codex files its trust under `<file>:<event>:<group index>:<hook index>`. `codex_hooks.py` takes `--hooks PATH`, `--no-remote-approve`, `--approve-timeout N` and `--no-async`; `show` prints the hooks, `remove` takes ccboard's out again, `trust-help` prints the review steps for this box.

**The trust review.** Codex runs a hook only after you have reviewed it once. Once, on the box, in any terminal: run `codex`, type `/hooks` ("view and manage lifecycle hooks"; newer builds may also open the review by themselves, "N hooks need review before they can run") and trust every ccboard hook. Until then a Codex session started from the board works as a terminal but reports no state (the "no hooks (untrusted?)" chip for it arrives with the launcher in v0.5.13). Codex stores the review as `[hooks.state."<file>:<event>:<group>:<index>"] trusted_hash = "sha256:..."` in `config.toml`; the hash covers the definition (command, timeout, async). You review again when the checkout or data directory moves (the command path changes), when ccboard changes a hook definition (the review reads "modified"), or when somebody puts a hook group above ccboard's in the same event. `CCBOARD_CODEX_HOOK_TRUST=bypass` skips the review: every Codex session the board starts then gets `--dangerously-bypass-hook-trust`, so ccboard's hooks run unreviewed and so does the `.codex/hooks.json` of any repository you cloned (the same risk class as `bypassPermissions`; the doctor's `codex-trust` and `codex-repo-hooks` checks warn). The default is `review`.

**MCP.** The `ccboard` server (`list_projects`, `create_task`, `list_tasks`, `get_task_status`) is registered in Codex's `config.toml` with `CCBOARD_URL` pointing at this board's loopback port. Check it with `codex mcp get ccboard`; remove it with `codex mcp remove ccboard`.

**Check what Codex sees.** `codex_hooks.py` was checked against Codex's own parser (the app-server `hooks/list` call: no warnings or errors, every hook parsed, the async flags and timeouts as above). To repeat it anywhere there is a `codex` binary, in a throwaway home: `CCBOARD_TEST_CODEX_BINARY=1 .venv/bin/python -m pytest -p no:cacheprovider -q tests/test_codex_hooks.py -k parses_the_installed`.

**Going back.** `python3 scripts/codex_hooks.py remove && codex mcp remove ccboard`. Your other hooks and Codex's own files are left alone; the `[hooks.state]` lines Codex wrote stay in `config.toml` and are harmless.

## Running on a subscription (no API key)

The box needs no `ANTHROPIC_API_KEY`. Log in once from the board ("Log in" opens the sign-in link, you paste the code back) and Claude Code stores the OAuth login in `~/.claude/.credentials.json`; it refreshes itself and survives reboots, so recovery after a restart needs no new login. Everything the board launches uses that same login: interactive sessions, tasks, "Describe with Claude", scheduled and batch `claude -p` runs. They all draw on the same 5-hour and weekly windows as your interactive use, which is why the scheduler defers runs above 85 % of the window and backs off after a rate-limited run.

If the login expires or you log out, the header shows *Claude: not logged in*, the Schedules header says runs are deferred, nothing headless is started (it would only fail), and ntfy pings you once with a link to the board. Log in again and the deferred jobs pick up on their next tick. Sessions inside a devcontainer have their own `~/.claude` and need their own login (once, inside the container).

## Security model

- The dashboard checks the `Tailscale-User-Login` header that `tailscale serve` injects and allows only the logins in `CCBOARD_ALLOWED_USERS`. Missing, empty or unknown identity → 403. Every non-GET request must also carry an `X-CCBoard: 1` header (blocks cross-site requests), and pages are served with a strict Content-Security-Policy.
- **The terminal (`/tty`) and code-server do not check that allowlist.** Any device your tailnet ACL lets reach this node's `CCBOARD_HTTPS_PORT` and `CODE_HTTPS_PORT` gets a shell as your user. Keep the ACL tight (a single-user tailnet is fine). ttyd runs with `-O`, so a web page you visit cannot open the terminal's WebSocket cross-site; a consequence is that clients without an `Origin` header (curl, wscat) are refused.
- Everything binds to 127.0.0.1. A process running on the box itself can still connect locally and forge the identity header; on a single-user machine that process already is you.
- Hooks and the statusline talk to the board over loopback with the token in `~/.local/share/ccboard/hook-token` (0600); `/api/hook` and `/api/permission` accept only that token. The ntfy server is tailnet-only and, like ttyd and code-server, protected by the tailnet ACL rather than the allowlist; anyone on the tailnet could publish to or read the topic. ntfy's `upstream-base-url` sends only a wake-up (no message content) through ntfy.sh so iOS devices get pushes.
- `install.sh` writes `/etc/sudoers.d/ccboard` so your user can run `systemctl restart ccboard` and `ccboard-ttyd` without a password (deploys), and `gh auth setup-git` so git uses gh's token for https clones.
- The nightly backup copies **unpushed work of every repo** under `PROJECTS_DIR` to its `origin`, into branches of its own: `ccboard-backup/<node>/<branch>` (`<node>` is `CCBOARD_NODE_NAME`). It runs `git fetch --prune origin` first, then pushes each local branch that has commits which are on no real branch of the remote. It **never writes `main` or any branch under its own name**, so nothing is published, no PR is updated and nothing can be rejected because the remote moved; a branch that is merely behind is simply up to date. The push into `ccboard-backup/<node>/` is forced (the namespace belongs to that box, and WIP gets rebased). A backup branch is deleted again once its commits are on a real branch (pushed or merged); one whose local branch was deleted on the box stays until then. Restore with `git fetch origin && git switch -c <branch> origin/ccboard-backup/<node>/<branch>`. Two things to know: the backup branches are visible to everyone with access to the repo, and a repository whose CI runs on every pushed branch (`on: push` without a branch filter) runs it for them too; add `branches-ignore: ['ccboard-backup/**']` to the `push` trigger there. If a repo has work that must not reach GitHub, set `CCBOARD_BACKUP_PUSH=0` or keep that repo outside `PROJECTS_DIR`. A branch rule on the remote that also matches `ccboard-backup/*` makes the run `partial` (the refusal is listed in Settings). The restic snapshots hold your transcripts; they are encrypted with the password file above.
- Tasks run `git`, `gh` and `claude -p` on your behalf with your credentials: "Describe" sends the branch diff to Claude, "Create PR"/"Merge" push and merge on GitHub, and "Fix CI" pastes CI log text into a Claude session as a prompt. Web Push subscriptions are accepted only for known browser push services.
- If the box has `tailscale set --operator=<you>` configured, every process running as you can change `tailscale serve` (including turning on Funnel). `install.sh` does not set the operator in systemd mode (it uses `sudo` for serve commands); in docker mode it does, because the container cannot use `sudo`, so the container and every other process of yours can then change `tailscale serve`. The board itself never turns Funnel on.
- Tagged nodes and requests from the box itself carry no identity header and are rejected. That is also why fleet polling uses `CCBOARD_HUB_TOKEN` (in `/etc/ccboard/env`, mode 0640) instead of the login header: a box polling another box has no user identity. The summary a node returns is counts, health and the 5-hour usage, never prompts or transcripts. If you tag the boxes (`tailscale up --advertise-tags=tag:ccboard`), make sure your ACL still lets your own devices reach their ports and lets the boxes reach each other on `CCBOARD_HTTPS_PORT`.
- Do not put `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or `CLAUDE_CODE_OAUTH_TOKEN` in `/etc/ccboard/env` or the tmux server's environment: they outrank the interactive login.
- Codex: the board never reads Codex's credentials (`~/.codex/auth.json`); login state comes from `codex login status`. Skipping approvals and the sandbox (`bypassPermissions`, `bypass: true`, `sandbox: danger-full-access`) is an explicit choice on an interactive session and is refused for tasks, backlog dispatch, scheduled or headless runs and every relaunch. `CCBOARD_CODEX_HOOK_TRUST=bypass` is a second, separate opt-in: it runs ccboard's Codex hooks without Codex's one-time review and also runs the `.codex/hooks.json` of any repository you cloned, without asking. Leave it on `review` unless you vet every repository on the box.

## Troubleshooting

- *403 from the box itself*: expected; open the URL from another tailnet device. `curl http://127.0.0.1:8000/healthz` is the on-box health check.
- *"ccboard-tmux is not running"* banner: `sudo systemctl start ccboard-tmux`. Never `PrivateTmp`/`ProtectHome` these units; the tmux socket is `/tmp/tmux-<uid>/ccboard`.
- *Login link never appears*: open `/tty/?arg=_ccboard-login` and finish the login in the terminal. The pasted value must be the whole `code#state` string; codes expire quickly.
- *Ubuntu 22.04*: tmux 3.2a lacks `allow-passthrough` (Shift+Enter / notifications inside the TUI degrade); `tmux.conf` uses `-q` so it still loads.
- Logs: `journalctl -u ccboard -u ccboard-ttyd -u ccboard-tmux -f`; in docker mode the board's log is `docker logs -f ccboard` (the startup line shows the runtime and image version) and Watchtower's is `docker logs ccboard-watchtower`.
- *Docker mode, board not answering*: `docker compose -f ~/.local/share/ccboard/compose/docker-compose.yml --profile prod ps`, then `docker logs --tail 50 ccboard`; roll back as described in [Run the board as a container](#run-the-board-as-a-container-optional).

### Uninstall

```sh
sudo systemctl disable --now ccboard ccboard-ttyd ccboard-tmux code-server@$USER
sudo systemctl disable --now ccboard-mem        # only when you enabled CCBOARD_MEM_SERVICE; the plugin itself stays (claude plugin uninstall claude-mem@thedotmack)
python3 ~/.local/share/ccboard/app/scripts/codex_hooks.py remove   # or scripts/codex_hooks.py from the checkout; then: codex mcp remove ccboard
sudo rm /etc/systemd/system/ccboard*.service /etc/ccboard/env; sudo rm -r /etc/systemd/system/code-server@$USER.service.d
sudo tailscale serve --https=443 --set-path=/tty off      # use your CCBOARD_HTTPS_PORT
sudo tailscale serve --https=443 --set-path=/ off
sudo tailscale serve --https=8443 --set-path=/ off        # use your CODE_HTTPS_PORT
```
In docker mode also run `docker compose -f ~/.local/share/ccboard/compose/docker-compose.yml --profile prod down` first (and `docker image rm ghcr.io/rpandox/ccboard` if you want the image gone).

Projects in `PROJECTS_DIR`, `~/.claude` and `~/.codex` are left alone.

## Deviations from the original spec (v0.1)

Kept deliberately small; each is the simplest safe option.

1. ttyd runs a validating wrapper (`bin/ccboard-attach`) instead of `-a tmux attach-session -t`: with raw URL args a client could append `;`-separated tmux commands, i.e. get a shell.
2. ttyd is the upstream 1.7.7 static binary on both LTS releases: Ubuntu 22.04's apt ttyd (1.6.3) has no `-W`, and both apt packages ship a root `ttyd.service` on 7681.
3. Units are `ccboard-tmux`, `ccboard-ttyd`, `ccboard` (not `ttyd`): a dedicated tmux server unit keeps sessions alive across board restarts, and the name avoids the apt unit.
4. Two HTTPS ports with the terminal under `/tty` on the dashboard port (defaults 443 and 8443) instead of 443/8443/10000: same origin for the board and the terminal, one port fewer to reserve.
5. Session names are `<project>--<repo>--<session>`: the project/repo model (a project folder holding one or more repos) was added during the build.
6. Sessions start as your login shell and the launcher command is typed in, instead of `cmd; exec $SHELL`: this gives Claude your full login PATH (nvm, cargo, docker, …); the finished run still leaves a prompt.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest httpx
.venv/bin/pytest -q
TMUX_TMPDIR=/tmp tmux -L ccboard -f tmux.conf start-server
PROJECTS_DIR=$PWD/tmp-projects CCBOARD_DATA_DIR=$PWD/tmp-data CCBOARD_DEV_BYPASS_USER=dev \
  .venv/bin/uvicorn app.main:app --port 8000
```
`CCBOARD_DEV_BYPASS_USER` skips the identity check for local development only; it is honoured only when the board runs directly on a host, and ignored under systemd and in the container (`CCBOARD_RUNTIME`, `INVOCATION_ID`).

The frontend is plain scripts loaded in order by `app/static/index.html` (`core.js` first, `main.js` last; `term.html` loads `core.js`, `components.js`, `termkit.js`, `term.js`, in that order, and `termkit.js` is definition-only like `core.js` and `components.js`): no bundler. `GET /sw.js` is rendered from `app/static/sw.js` with the cache name and the precache list generated from every file under `app/static`, and `/api/state.version` is a hash of the same files, so open pages reload after a deploy. Open the board with `?demo=1` to see every page rendered from fixtures (`app/static/demo/`, no tmux needed; the project page of `phasezero` has a tree, files and a binary and a secret-looking file to preview), and run `scripts/qa-ui.sh <base-url> <out-dir>` to drive the gstack browser (or headless Chrome) over the routes (Home, the inbox, Agents, Tasks, the project page and its Files and Tasks tabs, Settings, Search and the session peek) at phone, tablet and desktop widths, plus an installed-desktop pass (1440x900 with `html.pwa`: topbar at least 48 px, no horizontal overflow), two iPad passes (1024x1366 expanded, 834x1194 medium) and, when `npx --no-install lighthouse` works, the Lighthouse performance, pwa and accessibility scores for the demo Agents page (`QA_MANIFEST_SHOTS=1` re-captures the manifest screenshots). `tests/test_static.py` enforces the CSP rules (no inline styles or scripts, no external URLs, text through `textContent`); `node` is needed for the JS checks (`tests/js`).

**Driving the terminal page without ttyd.** While `CCBOARD_DEV_BYPASS_USER` is set the board also serves `scripts/dev/fake_tty/` at `/tty/`: a page that stubs what ttyd's page gives the terminal page (`window.term` with `fit()` and `options.fontSize`, `.xterm > .xterm-screen`, `.xterm-helper-textarea`) and counts the wheel events it receives (`window.__wheel`, `window.__lastDelta`) and the `fit()` calls (`window.__fits`). It carries no CSP (it stands in for ttyd's page, which is not ours) and, without the dev bypass, `/tty/` is a 404 on the board: on the box tailscale serve sends `/tty` to ttyd. `scripts/qa_terminal.sh <base-url> <out-dir>` drives the gstack browser over `/term/<session>` at 390x844, 768x1024 and 1280x800 against such a board and prints a PASS/FAIL table (screenshots in the out dir). Per size it checks that `#ttywrap` does not scroll and the iframe fills it, that the page does not bounce (`overscroll-behavior: none`, `body.term` fixed), that no key-bar key is under 44 px (every visible button at 390 px under `html.force-coarse`), that the send box is on screen, that a synthetic swipe on the fake terminal produces wheel events (and one from the left edge none), that PgUp on the scroll rail and on the key bar each post `{dir: "up"}` to `/api/sessions/<tmux>/scroll`, that the History chip follows tmux's copy-mode, that the compact key bar shows five keys and More, and that the console has no errors. The page needs a session to watch: start one on the board's tmux socket (named like `qa--terminal--s1`, or set `QA_TMUX`), or let the script make and remove one with `QA_TMUX_CREATE=1` (run it with the board's `TMUX_TMPDIR` and `CCBOARD_TMUX_SOCKET`; an isolated socket is the safest, see the commands below; the script stops the tmux server it started). Without a session the page shows *ended*, the History check is skipped and the expected `/api/sessions` 404 lines are ignored in the console check. `QA_SERVER_LOG=<file>` makes the scroll check also look for the POST in the board's log, and `QA_WIDTHS="390 1280"` limits the sizes.

```sh
# terminal 1: a board on its own tmux socket, dev bypass on (so /tty/ is the fake)
mkdir -p /tmp/ccb-qa/projects /tmp/ccb-qa/data
TMUX_TMPDIR=/tmp CCBOARD_TMUX_SOCKET=ccboard-qa PROJECTS_DIR=/tmp/ccb-qa/projects CCBOARD_DATA_DIR=/tmp/ccb-qa/data \
  CCBOARD_DEV_BYPASS_USER=dev .venv/bin/uvicorn app.main:app --port 8777 > /tmp/ccb-qa/server.log 2>&1
# terminal 2: the run (390x844, 768x1024, 1280x800); exit 0 = every assertion held, 2 = nothing to drive
TMUX_TMPDIR=/tmp CCBOARD_TMUX_SOCKET=ccboard-qa QA_TMUX_CREATE=1 QA_SERVER_LOG=/tmp/ccb-qa/server.log \
  scripts/qa_terminal.sh http://127.0.0.1:8777 /tmp/ccb-qa/shots
```

## License

MIT.
