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
- **One idempotent `install.sh`** for Ubuntu 22.04 / 24.04 (amd64, arm64): apt deps, ttyd, code-server, Claude Code, venv, three systemd units, tailscale serve mappings. Rerun it any time.

See [ROADMAP.md](ROADMAP.md) for what comes next (hook-driven status, attention inbox, usage strip, push notifications, PWA, worktrees per task, …).

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

Update: pull or extract the new version into the same directory and rerun `./install.sh`. It restarts only `ccboard` (stateless) unless something else changed. Restarting `ccboard` never touches running Claude sessions, because they live under `ccboard-tmux.service`.

## Using it

- **New project** → name (+ optional first clone URL). **Add repo** on a project card → blank name or clone URL. Clones run in a visible session (`<project>--<repo>--clone`); if a clone fails the repo shows *clone failed* and *Attach* shows the error.
- **New session** on a repo → pick a launcher, optionally a name and extra args, tick which other repos Claude may edit (`--add-dir`), then *Start & attach* opens the terminal in a new tab.
- The **first launch** of `claude` in a new folder shows Claude Code's workspace-trust prompt (Enter = trust) and possibly its onboarding screens. Answer them in the terminal; a desktop browser is easiest for that.
- tmux names are `<project>--<repo>--<session>`. From a shell on the box: `TMUX_TMPDIR=/tmp tmux -L ccboard ls`.
- Session sizes follow the most recently active client (tmux `window-size latest`), so attaching from a phone reflows the TUI for a desktop tab that is also attached.

## Security model

- The dashboard checks the `Tailscale-User-Login` header that `tailscale serve` injects and allows only the logins in `CCBOARD_ALLOWED_USERS`. Missing, empty or unknown identity → 403. Every non-GET request must also carry an `X-CCBoard: 1` header (blocks cross-site requests), and pages are served with a strict Content-Security-Policy.
- **The terminal (`/tty`) and code-server do not check that allowlist.** Any device your tailnet ACL lets reach this node's `CCBOARD_HTTPS_PORT` and `CODE_HTTPS_PORT` gets a shell as your user. Keep the ACL tight (a single-user tailnet is fine). ttyd runs with `-O`, so a web page you visit cannot open the terminal's WebSocket cross-site; a consequence is that clients without an `Origin` header (curl, wscat) are refused.
- Everything binds to 127.0.0.1. A process running on the box itself can still connect locally and forge the identity header; on a single-user machine that process already is you.
- If the box has `tailscale set --operator=<you>` configured, every process running as you can change `tailscale serve` (including turning on Funnel). `install.sh` does not set the operator; it uses `sudo` for serve commands.
- Tagged nodes and requests from the box itself carry no identity header and are rejected.
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
