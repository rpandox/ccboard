# ccboard

A self-hosted dashboard for **Claude Code CLI sessions** on your own Linux box, reachable only over your Tailscale network.

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
- **Push notifications** two ways: a self-hosted **ntfy** server on your tailnet (installed by `install.sh`, subscribe in the ntfy app to the URL shown under 🔔) and **Web Push** for the installed PWA ("Enable push on this device"). Pushes fire on needs-you, done, error and rate limit, with a deep link, a Terminal button and an Ack button. The ntfy server is tailnet-only, so the phone needs the Tailscale VPN on; on an iPhone the box relays a content-free wake-up through ntfy.sh and the app then fetches the message from your server (the 🔔 panel lists the exact steps).
- **Remote approve/deny**: when Claude asks for permission and nobody is attached to that terminal, the board pushes a notification with **Allow / Deny** buttons and holds the prompt for up to 90 s (`CCBOARD_APPROVE_TIMEOUT`). Answer from the phone, the inbox, or the `y`/`n` keys; with no answer the normal TUI prompt appears. If someone is attached, the TUI prompt appears immediately.
- **PWA**: installable (Add to Home Screen), works offline showing the last known state.
- **Mobile terminal** `/term/<session>`: the terminal with a key bar (Esc, Ctrl-C, Tab, arrows, ⌫, y⏎, Enter), editable quick replies and a text box. Attach links open it.
- **Live** grid: the last 20 lines of every session, refreshed every 2 s over SSE.
- **Reboot recovery**: after a reboot every Claude session is relaunched in its repo with `claude --resume <id>`.
- **Import from GitHub**: pick repos from `gh repo list`, clone them into a project three at a time.

## What v0.3 adds: git & tasks

- **Tasks**: "New task" on a repo takes a title and a prompt (or picks an open GitHub issue) and starts Claude in its own git worktree and branch (`claude --worktree`, `.worktreeinclude` honoured). The Tasks board has columns In progress / Needs you / Done / PR open / Merged, derived from the hook state and the PR.
- **Diff / PR**: a modal shows the branch commits and diff (diff2html), "Describe with Claude" writes the PR title and body with a one-turn `claude -p` over the diff, "Create PR" pushes and runs `gh pr create`, "Merge (squash) & archive" pushes, verifies origin has your HEAD, merges with `gh pr merge --squash --delete-branch` and removes the worktree.
- **PR / CI status** on cards (polled with `gh` every minute) and **Fix CI**, which pastes the failing job log into the task's Claude session (relaunching it in the worktree if needed).
- **Overlap warning** when two open tasks of a repo touch the same files.
- **Cost** per project (today / 7 days / total), repo and task, joined from `ccusage session --json` on the Claude session ids the board recorded. Sessions started outside the board are not attributed.
- **Transcript search**: the header box searches an FTS5 index over `~/.claude/projects` transcripts (display only; hits link to the session when known).

## What v0.4 adds: autonomy & fleet

- **Schedules**: cron or one-off jobs run headless `claude -p` in a fresh worktree (at most 2 at a time, deferred while the 5-hour window is above 85 %, and for 30 minutes after any run comes back rate-limited); each result becomes a task card you can resume in a terminal. The 5-hour percentage comes from the statusline of interactive sessions, so on a box that only runs headless jobs it stays unknown (the Schedules header says so) and only the back-off protects you.
- **Batch prompt** across the repos you pick, drained by the same scheduler, with per-batch progress.
- **MCP server**: `scripts/ccboard_mcp.py` is registered at user scope by `install.sh`, so any Claude session on the box can call `list_projects`, `create_task`, `list_tasks` and `get_task_status`.
- **Preview links**: "Preview" on a task card publishes the dev server the task started on its own tailnet HTTPS port (`tailscale serve`, never Funnel).
- **Devcontainer**: repos with `.devcontainer/devcontainer.json` can run sessions inside the container. Bypass permissions is an explicit per-session choice (host or container); tasks, scheduled and batch runs cannot use it, and settings overrides in extra args (`--settings`, `--setting-sources`, `--permission-prompt`) are rejected everywhere.
- **Backup**: `ccboard-backup.timer` runs nightly (02:30 by default): a consistent snapshot of the board's SQLite DB and your `~/.claude/projects` transcripts go into a restic repository, then every repo under `PROJECTS_DIR` gets `git push --all origin` so WIP branches from tasks survive the box. The strip shows the last result and the 🔔 panel has "Back up now"; a failed run pushes an ntfy warning.
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
| `PREVIEW_HTTPS_BASE` | `9100` | First tailnet HTTPS port used for task preview links |
| `CCBOARD_DEVCONTAINER` | unset | `1` installs the devcontainer CLI (docker required) |
| `CCBOARD_NODE_NAME` | `hostname -s` | This box's name in the fleet strips |
| `CCBOARD_HUB_TOKEN` | generated | Shared secret for node-to-node `GET /api/node/summary`; copy the same value to every box |
| `CCBOARD_NODES` | empty | `name=https://host.tailnet.ts.net:port,…` of the other boxes this one polls |
| `CCBOARD_RESTIC_REPO` | `<data dir>/restic` | restic repository for the nightly backup (`sftp:user@host:/path`, `rclone:remote:path`, `s3:…`, or `off`). The default is on the same disk: fine against deletion and corruption, useless against disk loss |
| `CCBOARD_BACKUP_PUSH` | `1` | `0` skips the nightly `git push --all origin` |
| `CCBOARD_BACKUP_ONCALENDAR` | `*-*-* 02:30:00` | systemd calendar spec of the backup timer (`CCBOARD_BACKUP=0` at install time leaves the timer disabled) |
| `CCBOARD_BACKUP_EXTRA` | empty | Colon-separated extra paths to include in the restic snapshot |
| `CCBOARD_RESTIC_PASSWORD_FILE` | `<data dir>/restic-password` | Where the restic password lives |
| `CCBOARD_RUNTIME` | `systemd` | `systemd` runs the board as `ccboard.service`; `docker` runs it as the `ccboard` container ([below](#run-the-board-as-a-container-optional)). Remembered in `/etc/ccboard/env`; go back with `CCBOARD_RUNTIME=systemd ./install.sh` |
| `CCBOARD_IMAGE_TAG` | `latest` | Docker mode only, install-time only (kept in the compose `.env`, not in `/etc/ccboard/env`): the `ghcr.io/rpandox/ccboard` tag to run, e.g. `sha-1a2b3c4` or `v0.5.2` to pin; Watchtower follows `latest` only |

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

- **New project** → name (+ optional first clone URL). **Add repo** on a project card → blank name or clone URL. Clones run in a visible session (`<project>--<repo>--clone`); if a clone fails the repo shows *clone failed* and *Attach* shows the error.
- **New session** on a repo → pick the launcher (new, `--resume`, `--continue`, shell), the **model** (fable / opus / sonnet / haiku or a full id), the **effort** (low … max) and the **permission mode** (ask, accept edits, plan, auto, don't ask); under *More options*: allowed and disallowed tools, text appended to the system prompt, extra args and which other repos Claude may edit (`--add-dir`). The choices are remembered per repo on that device. *Start & open terminal* opens the terminal in a new tab. The permission control also offers **bypass** (never ask): it is never the default, the form warns, and Claude Code asks you to confirm once in the terminal; scheduled, batch and task runs cannot use it.
- **New session** on the *project folder* row (projects that hold several repos) → Claude starts in the project folder itself, so every repo below is in scope without `--add-dir`. Such sessions use the reserved repo name `root` (`<project>--root--<session>` in tmux), so a repo cannot be called `root`.
- **State badges** come from Claude Code hooks: *working* after you send a prompt, *needs you* when Claude asks for permission or input, *done* when a turn ends (with the last line it printed), *error* on API failures. *Ack* clears a done/error highlight. The hooks are the scripts in `bin/`; they POST to the board over loopback with the token in `~/.local/share/ccboard/hook-token` and never block the TUI (they are registered `async`). `scripts/claude_settings.py show|remove` inspects or removes them. If you already had a `statusLine`, ccboard keeps yours and the per-session model/context numbers stay empty.
- **On a phone** the board is a PWA (add it to the Home Screen): a bottom bar jumps to Needs you / Tasks / Schedules / Projects, action rows scroll sideways, forms open as sheets, and the board re-polls the moment it comes back to the foreground (there is also a ↻ button, since an installed PWA has no browser reload). The UI is built on Blueprint's dark theme.
- The **first launch** of `claude` in a new folder shows Claude Code's workspace-trust prompt (Enter = trust) and possibly its onboarding screens. Answer them in the terminal; a desktop browser is easiest for that.
- tmux names are `<project>--<repo>--<session>`. From a shell on the box: `TMUX_TMPDIR=/tmp tmux -L ccboard ls`.
- Session sizes follow the most recently active client (tmux `window-size latest`), so attaching from a phone reflows the TUI for a desktop tab that is also attached.

## Running on a subscription (no API key)

The box needs no `ANTHROPIC_API_KEY`. Log in once from the board ("Log in" opens the sign-in link, you paste the code back) and Claude Code stores the OAuth login in `~/.claude/.credentials.json`; it refreshes itself and survives reboots, so recovery after a restart needs no new login. Everything the board launches uses that same login: interactive sessions, tasks, "Describe with Claude", scheduled and batch `claude -p` runs. They all draw on the same 5-hour and weekly windows as your interactive use, which is why the scheduler defers runs above 85 % of the window and backs off after a rate-limited run.

If the login expires or you log out, the header shows *Claude: not logged in*, the Schedules header says runs are deferred, nothing headless is started (it would only fail), and ntfy pings you once with a link to the board. Log in again and the deferred jobs pick up on their next tick. Sessions inside a devcontainer have their own `~/.claude` and need their own login (once, inside the container).

## Security model

- The dashboard checks the `Tailscale-User-Login` header that `tailscale serve` injects and allows only the logins in `CCBOARD_ALLOWED_USERS`. Missing, empty or unknown identity → 403. Every non-GET request must also carry an `X-CCBoard: 1` header (blocks cross-site requests), and pages are served with a strict Content-Security-Policy.
- **The terminal (`/tty`) and code-server do not check that allowlist.** Any device your tailnet ACL lets reach this node's `CCBOARD_HTTPS_PORT` and `CODE_HTTPS_PORT` gets a shell as your user. Keep the ACL tight (a single-user tailnet is fine). ttyd runs with `-O`, so a web page you visit cannot open the terminal's WebSocket cross-site; a consequence is that clients without an `Origin` header (curl, wscat) are refused.
- Everything binds to 127.0.0.1. A process running on the box itself can still connect locally and forge the identity header; on a single-user machine that process already is you.
- Hooks and the statusline talk to the board over loopback with the token in `~/.local/share/ccboard/hook-token` (0600); `/api/hook` and `/api/permission` accept only that token. The ntfy server is tailnet-only and, like ttyd and code-server, protected by the tailnet ACL rather than the allowlist; anyone on the tailnet could publish to or read the topic. ntfy's `upstream-base-url` sends only a wake-up (no message content) through ntfy.sh so iOS devices get pushes.
- `install.sh` writes `/etc/sudoers.d/ccboard` so your user can run `systemctl restart ccboard` and `ccboard-ttyd` without a password (deploys), and `gh auth setup-git` so git uses gh's token for https clones.
- The nightly backup pushes **every local branch of every repo** under `PROJECTS_DIR` to its `origin` (`git push --all`, never forced: a branch that is behind is reported, not overwritten). If a repo has branches that must not reach GitHub, set `CCBOARD_BACKUP_PUSH=0` or keep that repo outside `PROJECTS_DIR`. The restic snapshots hold your transcripts; they are encrypted with the password file above.
- Tasks run `git`, `gh` and `claude -p` on your behalf with your credentials: "Describe" sends the branch diff to Claude, "Create PR"/"Merge" push and merge on GitHub, and "Fix CI" pastes CI log text into a Claude session as a prompt. Web Push subscriptions are accepted only for known browser push services.
- If the box has `tailscale set --operator=<you>` configured, every process running as you can change `tailscale serve` (including turning on Funnel). `install.sh` does not set the operator in systemd mode (it uses `sudo` for serve commands); in docker mode it does, because the container cannot use `sudo`, so the container and every other process of yours can then change `tailscale serve`. The board itself never turns Funnel on.
- Tagged nodes and requests from the box itself carry no identity header and are rejected. That is also why fleet polling uses `CCBOARD_HUB_TOKEN` (in `/etc/ccboard/env`, mode 0640) instead of the login header: a box polling another box has no user identity. The summary a node returns is counts, health and the 5-hour usage, never prompts or transcripts. If you tag the boxes (`tailscale up --advertise-tags=tag:ccboard`), make sure your ACL still lets your own devices reach their ports and lets the boxes reach each other on `CCBOARD_HTTPS_PORT`.
- Do not put `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or `CLAUDE_CODE_OAUTH_TOKEN` in `/etc/ccboard/env` or the tmux server's environment: they outrank the interactive login.

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
sudo rm /etc/systemd/system/ccboard*.service /etc/ccboard/env; sudo rm -r /etc/systemd/system/code-server@$USER.service.d
sudo tailscale serve --https=443 --set-path=/tty off      # use your CCBOARD_HTTPS_PORT
sudo tailscale serve --https=443 --set-path=/ off
sudo tailscale serve --https=8443 --set-path=/ off        # use your CODE_HTTPS_PORT
```
In docker mode also run `docker compose -f ~/.local/share/ccboard/compose/docker-compose.yml --profile prod down` first (and `docker image rm ghcr.io/rpandox/ccboard` if you want the image gone).

Projects in `PROJECTS_DIR` and `~/.claude` are left alone.

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

The frontend is plain scripts loaded in order by `app/static/index.html` (`core.js` first, `main.js` last; `term.html` loads `core.js`, `components.js`, `term.js`): no bundler. `GET /sw.js` is rendered from `app/static/sw.js` with the cache name and the precache list generated from every file under `app/static`, and `/api/state.version` is a hash of the same files, so open pages reload after a deploy. `tests/test_static.py` enforces the CSP rules (no inline styles or scripts, no external URLs, text through `textContent`); `node` is needed for the JS checks (`tests/js`).

## License

MIT.
