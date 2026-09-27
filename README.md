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
- **Push notifications** two ways: a self-hosted **ntfy** server on your tailnet (installed by `install.sh`, subscribe in the ntfy app to the URL shown under 🔔) and **Web Push** for the installed PWA ("Enable push on this device"). Pushes fire on needs-you, done, error and rate limit, with a deep link, a Terminal button and an Ack button.
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

- **Schedules**: cron or one-off jobs run headless `claude -p` in a fresh worktree (at most 2 at a time, deferred while the 5-hour window is above 85 %); each result becomes a task card you can resume in a terminal.
- **Batch prompt** across the repos you pick, drained by the same scheduler, with per-batch progress.
- **MCP server**: `scripts/ccboard_mcp.py` is registered at user scope by `install.sh`, so any Claude session on the box can call `list_projects`, `create_task`, `list_tasks` and `get_task_status`.
- **Preview links**: "Preview" on a task card publishes the dev server the task started on its own tailnet HTTPS port (`tailscale serve`, never Funnel).
- **Devcontainer**: repos with `.devcontainer/devcontainer.json` can run sessions inside the container, and only there is "bypass permissions" offered.
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
- `restic` from apt, a random restic password in `<data dir>/restic-password` (0600; **copy it somewhere safe**, without it the backups are unreadable), and `ccboard-backup.service` + `.timer`. `sudo systemctl start ccboard-backup` runs one now; `journalctl -u ccboard-backup` has the log. Remote repos need their credentials in `/etc/ccboard/env` (for example `AWS_ACCESS_KEY_ID`, or an ssh key without passphrase for sftp; the run uses `BatchMode=yes` and never prompts)

Update: pull or extract the new version into the same directory and rerun `./install.sh`. It restarts only `ccboard` (stateless) unless something else changed. Restarting `ccboard` never touches running Claude sessions, because they live under `ccboard-tmux.service`. `install.sh` also writes `/etc/sudoers.d/ccboard`, which lets your user restart `ccboard` and `ccboard-ttyd` without a password, so code-only updates are `git pull && sudo systemctl restart ccboard` (or `scripts/deploy.sh <host>` from your machine).

## Using it

- **New project** → name (+ optional first clone URL). **Add repo** on a project card → blank name or clone URL. Clones run in a visible session (`<project>--<repo>--clone`); if a clone fails the repo shows *clone failed* and *Attach* shows the error.
- **New session** on a repo → pick a launcher, optionally a name and extra args, tick which other repos Claude may edit (`--add-dir`), then *Start & attach* opens the terminal in a new tab.
- **State badges** come from Claude Code hooks: *working* after you send a prompt, *needs you* when Claude asks for permission or input, *done* when a turn ends (with the last line it printed), *error* on API failures. *Ack* clears a done/error highlight. The hooks are the scripts in `bin/`; they POST to the board over loopback with the token in `~/.local/share/ccboard/hook-token` and never block the TUI (they are registered `async`). `scripts/claude_settings.py show|remove` inspects or removes them. If you already had a `statusLine`, ccboard keeps yours and the per-session model/context numbers stay empty.
- The **first launch** of `claude` in a new folder shows Claude Code's workspace-trust prompt (Enter = trust) and possibly its onboarding screens. Answer them in the terminal; a desktop browser is easiest for that.
- tmux names are `<project>--<repo>--<session>`. From a shell on the box: `TMUX_TMPDIR=/tmp tmux -L ccboard ls`.
- Session sizes follow the most recently active client (tmux `window-size latest`), so attaching from a phone reflows the TUI for a desktop tab that is also attached.

## Security model

- The dashboard checks the `Tailscale-User-Login` header that `tailscale serve` injects and allows only the logins in `CCBOARD_ALLOWED_USERS`. Missing, empty or unknown identity → 403. Every non-GET request must also carry an `X-CCBoard: 1` header (blocks cross-site requests), and pages are served with a strict Content-Security-Policy.
- **The terminal (`/tty`) and code-server do not check that allowlist.** Any device your tailnet ACL lets reach this node's `CCBOARD_HTTPS_PORT` and `CODE_HTTPS_PORT` gets a shell as your user. Keep the ACL tight (a single-user tailnet is fine). ttyd runs with `-O`, so a web page you visit cannot open the terminal's WebSocket cross-site; a consequence is that clients without an `Origin` header (curl, wscat) are refused.
- Everything binds to 127.0.0.1. A process running on the box itself can still connect locally and forge the identity header; on a single-user machine that process already is you.
- Hooks and the statusline talk to the board over loopback with the token in `~/.local/share/ccboard/hook-token` (0600); `/api/hook` and `/api/permission` accept only that token. The ntfy server is tailnet-only and, like ttyd and code-server, protected by the tailnet ACL rather than the allowlist; anyone on the tailnet could publish to or read the topic. ntfy's `upstream-base-url` sends only a wake-up (no message content) through ntfy.sh so iOS devices get pushes.
- `install.sh` writes `/etc/sudoers.d/ccboard` so your user can run `systemctl restart ccboard` and `ccboard-ttyd` without a password (deploys), and `gh auth setup-git` so git uses gh's token for https clones.
- The nightly backup pushes **every local branch of every repo** under `PROJECTS_DIR` to its `origin` (`git push --all`, never forced: a branch that is behind is reported, not overwritten). If a repo has branches that must not reach GitHub, set `CCBOARD_BACKUP_PUSH=0` or keep that repo outside `PROJECTS_DIR`. The restic snapshots hold your transcripts; they are encrypted with the password file above.
- Tasks run `git`, `gh` and `claude -p` on your behalf with your credentials: "Describe" sends the branch diff to Claude, "Create PR"/"Merge" push and merge on GitHub, and "Fix CI" pastes CI log text into a Claude session as a prompt. Web Push subscriptions are accepted only for known browser push services.
- If the box has `tailscale set --operator=<you>` configured, every process running as you can change `tailscale serve` (including turning on Funnel). `install.sh` does not set the operator; it uses `sudo` for serve commands.
- Tagged nodes and requests from the box itself carry no identity header and are rejected. That is also why fleet polling uses `CCBOARD_HUB_TOKEN` (in `/etc/ccboard/env`, mode 0640) instead of the login header: a box polling another box has no user identity. The summary a node returns is counts, health and the 5-hour usage, never prompts or transcripts. If you tag the boxes (`tailscale up --advertise-tags=tag:ccboard`), make sure your ACL still lets your own devices reach their ports and lets the boxes reach each other on `CCBOARD_HTTPS_PORT`.
- Do not put `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or `CLAUDE_CODE_OAUTH_TOKEN` in `/etc/ccboard/env` or the tmux server's environment: they outrank the interactive login.

## Troubleshooting

- *403 from the box itself*: expected; open the URL from another tailnet device. `curl http://127.0.0.1:8000/healthz` is the on-box health check.
- *"ccboard-tmux is not running"* banner: `sudo systemctl start ccboard-tmux`. Never `PrivateTmp`/`ProtectHome` these units; the tmux socket is `/tmp/tmux-<uid>/ccboard`.
- *Login link never appears*: open `/tty/?arg=_ccboard-login` and finish the login in the terminal. The pasted value must be the whole `code#state` string; codes expire quickly.
- *Ubuntu 22.04*: tmux 3.2a lacks `allow-passthrough` (Shift+Enter / notifications inside the TUI degrade); `tmux.conf` uses `-q` so it still loads.
- Logs: `journalctl -u ccboard -u ccboard-ttyd -u ccboard-tmux -f`.

### Uninstall

```sh
sudo systemctl disable --now ccboard ccboard-ttyd ccboard-tmux code-server@$USER
sudo rm /etc/systemd/system/ccboard*.service /etc/ccboard/env; sudo rm -r /etc/systemd/system/code-server@$USER.service.d
sudo tailscale serve --https=443 --set-path=/tty off      # use your CCBOARD_HTTPS_PORT
sudo tailscale serve --https=443 --set-path=/ off
sudo tailscale serve --https=8443 --set-path=/ off        # use your CODE_HTTPS_PORT
```
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
`CCBOARD_DEV_BYPASS_USER` skips the identity check for local development only; it is ignored under systemd.

## License

MIT.
