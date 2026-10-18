# Install on Linux

For people installing ccboard on an Ubuntu machine, and for anyone who wants to know what the installer sets up.

[Back to the documentation index](../README.md)

This page installs the board as systemd services on Ubuntu 22.04 or 24.04, on amd64 or arm64. The installer is `install.sh` in the checkout. It stops on any other system, and it needs a terminal because it calls `sudo`. Other systems: [a Mac](macos.md), [Windows 11 with WSL2](windows-wsl2.md), [a Docker container on a Linux host](docker.md). One line per system, with what has been run: [Supported systems](../../README.md#supported-systems).

## Before you start

- [ ] Ubuntu 22.04 or 24.04 (amd64 or arm64) with systemd. `install.sh` stops with "Ubuntu 22.04 or 24.04 required" on anything else.
- [ ] A user account with `sudo` rights, and a terminal. The installer stops with "run interactively" when it has none, because `sudo` needs one. On a remote machine run it through `ssh -t`.
- [ ] Tailscale installed and signed in on this machine ([Tailscale](tailscale.md)).
- [ ] MagicDNS and **HTTPS certificates** turned on for your tailnet (Tailscale admin console, DNS page).
- [ ] A second device on the same tailnet, such as your phone or laptop. You open the board from that device. A request to plain `http://localhost` on the machine that runs the board carries no Tailscale identity, so it is rejected with a 403.
- [ ] Port 443 free for the board, or ports you choose yourself (see "If port 443 or 8080 is taken" below). The installer never takes a port from another service.
- [ ] Run the installer as the user who will own the Claude sessions.

## Install

1. Get the code and go into it:

   ```sh
   git clone https://github.com/rpandox/ccboard && cd ccboard
   ```

   Installing from `git clone` is the supported route. `tar xzf ccboard.tar.gz && cd ccboard` also works if you were given a tarball.

2. Run the installer from a terminal. It calls `sudo` where it needs to, and it is safe to run again:

   ```sh
   ./install.sh
   ```

### If port 443 or 8080 is taken

Settings are environment variables. Pass them on the command line, and the installer remembers them in `/etc/ccboard/env`. For a machine where 443 and 8080 are already in use:

```sh
CCBOARD_HTTPS_PORT=8443 CODE_HTTPS_PORT=10000 CODE_SERVER_PORT=8081 ./install.sh
```

Tailnet-only `tailscale serve` accepts any HTTPS port. If a chosen port already carries something else, the installer stops and tells you; the rules are in [Configuration](../configuration.md#ports-and-addresses).

### What you should see

The installer prints one heading per step and ends with a summary like this (your tailnet name and ports will differ):

```text
ccboard is installed.
  Dashboard:   https://<name>.<tailnet>.ts.net:443/
  code-server: https://<name>.<tailnet>.ts.net:8443/
  Open them from another device on your tailnet (requests from this box carry no Tailscale identity).
  Then click "Log in" on the dashboard to sign in to Claude Code.
```

Further lines appear for ntfy, Codex, the backup and, in container mode, the runtime. If the board does not answer on `127.0.0.1:8000` within ten seconds the installer prints the service status and stops.

### Open the board

Open the printed Dashboard address on another device on your tailnet, then click *Log in* to sign in to Claude Code. Plain `http://localhost` carries no Tailscale identity. The tailnet https name should carry it even when you open it from the machine that runs the board, but the one measurement of that was on a Mac, with curl, not a browser ([Tailscale](tailscale.md#tailscale-on-each-system)).

### Verify

- `curl -fsS http://127.0.0.1:8000/healthz` on the machine itself answers while the board runs (8000 is the default `CCBOARD_PORT`; this is the check the installer makes).
- `systemctl status ccboard ccboard-tmux ccboard-ttyd` shows three running services.
- `tailscale serve status` lists the board and `/tty` on `CCBOARD_HTTPS_PORT` and code-server on `CODE_HTTPS_PORT`.
- Settings > Doctor on the board lists every check and the fix for a failing one.

### Run it again

The installer is idempotent: it is one `install.sh` for Ubuntu 22.04 and 24.04 on amd64 and arm64 (apt dependencies, ttyd, code-server, Claude Code, a venv, three systemd units, the `tailscale serve` mappings and the Claude hooks), and you can rerun it at any time. To update an install, see [Upgrade](../upgrade.md#updating-a-linux-install).

## What the installer changes

- apt: `git tmux curl ca-certificates python3 python3-venv` (only what is missing)
- `ttyd` 1.7.7 upstream static binary at `/usr/local/bin/ttyd` (checksum verified); the apt package's `ttyd.service` is disabled if present
- `code-server` (pinned version, only when absent) with `~/.config/code-server/config.yaml` set to `auth: none` on loopback (the file is marked `# managed by ccboard`; an existing unmanaged file is never overwritten without `CCBOARD_REPLACE_CODE_SERVER_CONFIG=1`)
- Claude Code via the native installer (only when absent)
- a venv in the checkout (`.venv`)
- `/etc/systemd/system/ccboard-tmux.service` (the tmux server that owns every session), `ccboard-ttyd.service`, `ccboard.service`, plus `code-server@<you>`
- `tailscale serve --bg`: `/` and `/tty` on `CCBOARD_HTTPS_PORT`, `/` on `CODE_HTTPS_PORT`
- `restic` from apt, a random restic password in `<data dir>/restic-password` (0600; **copy it somewhere safe**, without it the backups are unreadable), and `ccboard-backup.service` + `.timer`. `sudo systemctl start ccboard-backup` runs one now; `journalctl -u ccboard-backup` has the log. Remote repos need their credentials in `/etc/ccboard/env` (for example `AWS_ACCESS_KEY_ID`; lines you add there by hand survive reruns of `install.sh`) or an ssh key without passphrase for `sftp:`; both the git pushes and restic's own ssh run with `BatchMode=yes` and a connect timeout, so nothing ever prompts. "Back up now" on the board starts the same unit (`systemctl start ccboard-backup`), so a ccboard restart cannot interrupt it; a run that was killed anyway leaves a restic lock, which the next run clears with `restic unlock` before retrying

- when `claude` is installed: the `ccboard` MCP server registered at user scope with the board's own venv python (`claude mcp add --scope user ccboard -- <checkout>/.venv/bin/python <checkout>/scripts/ccboard_mcp.py`; with `CCBOARD_RUNTIME=docker` it is `/usr/bin/python3` and the copy under `<data dir>/app`). An entry that already exists is kept, not added twice. WSL2 is the same step
- the `claude-mem@thedotmack` Claude Code plugin, when it is missing (`CCBOARD_CLAUDE_MEM=0` skips this; bun is never installed) and, only with `CCBOARD_MEM_SERVICE=1`, `ccboard-mem.service` (see [claude-mem](../guide/claude-mem.md#claude-mem))
- when `codex` is installed (`~/.local/bin/codex` counts; the login shell's PATH is not needed): ccboard's hooks merged into `$CODEX_HOME/hooks.json` (`scripts/codex_hooks.py install`) and the `ccboard` MCP server registered with `codex mcp add ccboard --env CCBOARD_URL=http://127.0.0.1:$CCBOARD_PORT` (a stale entry that names another checkout or port is re-pointed). Without Codex both steps are skipped with a note; a failure is a warning, never an abort. See [Codex](../guide/codex.md#codex)
- with `CCBOARD_RUNTIME=docker`: the container instead of `ccboard.service`, plus `ccboard-watchtower`, a compose file under `<data dir>/compose` and `<data dir>/app` for the scripts the host runs (see [Run the board as a container](docker.md#run-the-board-as-a-container-optional))

## Next steps

- [Your first ten minutes](../../README.md#your-first-ten-minutes) on the board.
- [Configuration](../configuration.md) for every setting, [Upgrade](../upgrade.md), [Backup and restore](../backup-and-restore.md), [Security](../security.md) and [Troubleshooting](../troubleshooting.md).

## Evidence and open checks

- Ubuntu 22.04 and 24.04 are the only systems `install.sh` accepts; it dies on anything else. This is the reference platform: everything is checked on it first.
- A fresh-machine walkthrough of this page by someone who did not write it is still to do (issue #244).
- **Background.** Issue #115 (running on more systems than one Linux machine) lists where each system is supported; see [Supported systems](../../README.md#supported-systems).
