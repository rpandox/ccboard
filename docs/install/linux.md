# Install on Linux

For people installing ccboard on an Ubuntu machine, and for anyone who wants to know what the installer sets up.

[Back to the documentation index](../README.md)

## Install

On the target box, as the user who will own the Claude sessions (the script calls `sudo` where needed; run it from a terminal, e.g. `ssh -t`):

```sh
tar xzf ccboard.tar.gz && cd ccboard      # or: git clone https://github.com/rpandox/ccboard && cd ccboard
./install.sh
```

The script prints the dashboard URL when it is done. Open it **from another device on your tailnet** (a request to plain `http://localhost` on the box carries no Tailscale identity and is rejected; the box's tailnet https name does carry it, see [Tailscale on each system](tailscale.md#tailscale-on-each-system-issue-126)), then click *Log in* to sign in to Claude Code.

Requirements (the systems ccboard runs on, one line each: [Where ccboard runs](../../README.md#where-ccboard-runs-issue-115)): Ubuntu 22.04 or 24.04 for this installer (macOS 13 or newer has its own installer, see [macOS](macos.md#macos-issue-117); `./install.sh` hands over to it on a Mac; Windows 11 runs this installer inside WSL2, see [Windows (WSL2)](windows-wsl2.md#windows-wsl2-issue-118)), Tailscale installed and logged in, MagicDNS and **HTTPS certificates** enabled for your tailnet (admin console → DNS), `sudo` rights.

### What install.sh sets up

- apt: `git tmux curl ca-certificates python3 python3-venv` (only what is missing)
- `ttyd` 1.7.7 upstream static binary at `/usr/local/bin/ttyd` (checksum verified); the apt package's `ttyd.service` is disabled if present
- `code-server` (pinned version, only when absent) with `~/.config/code-server/config.yaml` set to `auth: none` on loopback (the file is marked `# managed by ccboard`; an existing unmanaged file is never overwritten without `CCBOARD_REPLACE_CODE_SERVER_CONFIG=1`)
- Claude Code via the native installer (only when absent)
- a venv in the checkout (`.venv`)
- `/etc/systemd/system/ccboard-tmux.service` (the tmux server that owns every session), `ccboard-ttyd.service`, `ccboard.service`, plus `code-server@<you>`
- `tailscale serve --bg`: `/` and `/tty` on `CCBOARD_HTTPS_PORT`, `/` on `CODE_HTTPS_PORT`
- `restic` from apt, a random restic password in `<data dir>/restic-password` (0600; **copy it somewhere safe**, without it the backups are unreadable), and `ccboard-backup.service` + `.timer`. `sudo systemctl start ccboard-backup` runs one now; `journalctl -u ccboard-backup` has the log. Remote repos need their credentials in `/etc/ccboard/env` (for example `AWS_ACCESS_KEY_ID`; lines you add there by hand survive reruns of `install.sh`) or an ssh key without passphrase for `sftp:`; both the git pushes and restic's own ssh run with `BatchMode=yes` and a connect timeout, so nothing ever prompts. "Back up now" on the board starts the same unit (`systemctl start ccboard-backup`), so a ccboard restart cannot interrupt it; a run that was killed anyway leaves a restic lock, which the next run clears with `restic unlock` before retrying

- the `claude-mem@thedotmack` Claude Code plugin, when it is missing (`CCBOARD_CLAUDE_MEM=0` skips this; bun is never installed) and, only with `CCBOARD_MEM_SERVICE=1`, `ccboard-mem.service` (see [claude-mem](../guide/claude-mem.md#claude-mem))
- when `codex` is installed (`~/.local/bin/codex` counts; the login shell's PATH is not needed): ccboard's hooks merged into `$CODEX_HOME/hooks.json` (`scripts/codex_hooks.py install`) and the `ccboard` MCP server registered with `codex mcp add ccboard --env CCBOARD_URL=http://127.0.0.1:$CCBOARD_PORT` (a stale entry that names another checkout or port is re-pointed). Without Codex both steps are skipped with a note; a failure is a warning, never an abort. See [Codex](../guide/codex.md#codex)
- with `CCBOARD_RUNTIME=docker`: the container instead of `ccboard.service`, plus `ccboard-watchtower`, a compose file under `<data dir>/compose` and `<data dir>/app` for the scripts the host runs (see [Run the board as a container](docker.md#run-the-board-as-a-container-optional))

## What v0.1 does

- **One idempotent `install.sh`** for Ubuntu 22.04 / 24.04 (amd64, arm64): apt deps, ttyd, code-server, Claude Code, venv, three systemd units, tailscale serve mappings, Claude hooks. Rerun it any time.
