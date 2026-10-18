# Uninstall ccboard

For people removing ccboard from a Linux machine, a Mac, a WSL2 distro or a Docker host.

[Back to the documentation index](README.md)

## Uninstall

There is one uninstall script, for macOS (`scripts/uninstall-macos.sh`). Linux has none, so this page lists the commands in order, says what each one removes and says what it leaves. Nothing here is run for you.

Read three things first, then the order.

- **Ending the tmux server ends every agent session.** `ccboard-tmux.service` (Linux) and `dev.ccboard.tmux` (Mac) own the tmux server that every Claude Code and Codex session lives in. Stopping that one unit closes all of them, with whatever they were doing. Every step below that touches it comes last and says so. If you want the sessions to keep running, skip that step.
- **The data folder holds things you may want.** By default it is `~/.local/share/ccboard` (`CCBOARD_DATA_DIR`). It holds the board's database, the hook token, the restic password file and, unless you pointed `CCBOARD_RESTIC_REPO` elsewhere, the local restic backup repository, plus any saved Claude and Codex logins and the container's compose files. Deleting it deletes the backups together with the password that opens them. It is the last step, and it is optional.
- **Port 443 is not yours to clear blindly.** Other services on the same tailnet name may use `tailscale serve` on 443. Run `tailscale serve status` first, turn off only the handlers that point at the loopback ports below, and never run `tailscale serve reset`. The installer never does either.

**The order.** Back up first, while everything still runs.

- **Systemd runtime** (`CCBOARD_RUNTIME` is not `docker`): Linux step 1 (back up, and copy `<data dir>/restic-password`), then steps 2 to 13.
- **Container runtime** (`CCBOARD_RUNTIME=docker`): Linux step 1 (back up, and copy `<data dir>/restic-password`) **before anything else**, then the Docker section, then steps 2 to 13. The backup runs inside the `ccboard` container (the installer's backup command is `docker exec ccboard ... -m app.backup`), so it fails once `docker compose ... down` has removed the container. Step 13 deletes the data folder together with the restic repository.

### What the installer made

| What | Where | Removed by |
|---|---|---|
| The board, terminal and tmux units | `ccboard.service`, `ccboard-ttyd.service`, `ccboard-tmux.service` in `/etc/systemd/system/` | Linux steps 2 and 12 |
| The nightly backup | `ccboard-backup.service` and `ccboard-backup.timer` in `/etc/systemd/system/` | Linux steps 2 and 12 |
| The claude-mem worker unit (only with `CCBOARD_MEM_SERVICE=1`) | `ccboard-mem.service` | Linux steps 2 and 12 |
| The settings file | `/etc/ccboard/env` | Linux step 11 |
| The restart rule | `/etc/sudoers.d/ccboard` | Linux step 11 |
| `tailscale serve` mappings | the board and `/tty` on `CCBOARD_HTTPS_PORT`, code-server on `CODE_HTTPS_PORT`, ntfy on `NTFY_HTTPS_PORT`, the claude-mem viewer if you set `CCBOARD_MEM_HTTPS_PORT`, and one port per preview | Linux step 3 |
| Claude Code hooks and status line | `~/.claude/settings.json` (or `$CLAUDE_CONFIG_DIR/settings.json`) | Linux step 4 |
| Codex hooks | `$CODEX_HOME/hooks.json` (default `~/.codex/hooks.json`) | Linux step 4 |
| The `ccboard` MCP server for Claude (user scope) and for Codex | Claude's and Codex's own configuration | Linux step 4 |
| code-server settings and config | `~/.local/share/code-server/User/settings.json`, `~/.config/code-server/config.yaml`, `/etc/systemd/system/code-server@<you>.service.d/ccboard.conf` | Linux step 5 |
| ntfy | the `ntfy` apt package, `/etc/apt/sources.list.d/ntfy.list`, `/etc/apt/keyrings/ntfy.gpg`, `/etc/ntfy/server.yml` | Linux step 6 |
| ttyd | `/usr/local/bin/ttyd` | Linux step 8 |
| The container runtime (only with `CCBOARD_RUNTIME=docker`) | the `ccboard` and `ccboard-watchtower` containers, `<data dir>/compose`, `<data dir>/app`, a crontab line, `/etc/systemd/system/docker.service.d/ccboard.conf`, the Tailscale operator setting | the Docker section |
| The checkout's virtual environment | `.venv` in the folder you cloned | deleting that folder |

## Linux with the systemd runtime

Run these as the user who installed ccboard, from the ccboard folder you cloned (the hook, MCP and settings scripts live there, so keep it until step 13).

**Before step 1**, look at what is there. These commands only read.

```sh
tailscale serve status
grep -E '^(CCBOARD_RUNTIME|CCBOARD_PORT|TTYD_PORT|CODE_SERVER_PORT|CCBOARD_HTTPS_PORT|CODE_HTTPS_PORT|NTFY_HTTPS_PORT|CCBOARD_MEM_HTTPS_PORT|PROJECTS_DIR|CCBOARD_DATA_DIR)=' /etc/ccboard/env
systemctl list-unit-files 'ccboard*' 'code-server@*' ntfy.service
tmux -L ccboard list-sessions
```

If `CCBOARD_RUNTIME=docker` appears, do step 1 now, then the Docker section, then steps 2 to 13 (see The order, above). Stop or finish the sessions you care about before step 12.

1. **Back up what you want to keep, first.** Press Back up now in Settings, or run `sudo systemctl start ccboard-backup`, and copy `<data dir>/restic-password` somewhere safe. With `CCBOARD_RUNTIME=docker` the backup runs inside the container, so do this while the container is still up, before the Docker section. See [Backup and restore](backup-and-restore.md).

2. **Stop the services, all but tmux.** This stops the board, the terminal, the nightly backup, code-server and the optional claude-mem unit. Nothing is deleted yet. Keep `"code-server@$USER"` in the first command only when the installer installed code-server (its output said "installing code-server") or you use it for nothing else. The installer leaves a code-server that was already there alone, but it did enable and restart `code-server@<you>`; if you use code-server otherwise, drop that name from the command, or bring it back afterwards with `sudo systemctl enable --now code-server@$USER`.

   ```sh
   sudo systemctl disable --now ccboard ccboard-ttyd ccboard-backup.timer "code-server@$USER"   # drop "code-server@$USER" if you keep using code-server
   sudo systemctl stop ccboard-backup.service
   sudo systemctl disable --now ccboard-mem     # only when you enabled CCBOARD_MEM_SERVICE; the claude-mem plugin itself stays
   ```

3. **Turn off the `tailscale serve` mappings that ccboard added.** Compare each line with the output of `tailscale serve status` first. A mapping is ccboard's only when it points at the loopback port named here (the defaults are in the comments; your values are in `/etc/ccboard/env`). Use your own `CCBOARD_HTTPS_PORT`, `CODE_HTTPS_PORT` and `NTFY_HTTPS_PORT` in place of 443, 8443 and 8444.

   ```sh
   sudo tailscale serve --https=443 --set-path /tty off    # to 127.0.0.1:7681 (TTYD_PORT)
   sudo tailscale serve --https=443 --set-path / off       # to 127.0.0.1:8000 (CCBOARD_PORT); skip if 443 serves something else at /
   sudo tailscale serve --https=8443 --set-path / off      # to 127.0.0.1:8080 (CODE_SERVER_PORT)
   sudo tailscale serve --https=8444 --set-path / off      # to 127.0.0.1:2586 (NTFY_PORT); only if ntfy was set up
   ```

   Two more kinds may be listed. The claude-mem viewer is on `CCBOARD_MEM_HTTPS_PORT` when you set that (empty by default): turn it off the same way with `--set-path / off`. A task preview has its own port, counted up from `PREVIEW_HTTPS_BASE` (9100 by default), and points at a dev server in one of your sessions: end previews from the board first, or turn each off with `sudo tailscale serve --https=<port> off` after checking that port in the status output. A port that nothing of yours points at is left alone.

4. **Take ccboard's entries out of the Claude and Codex configuration.** Each command touches only the entries that name ccboard's scripts; your other hooks, settings and MCP servers stay.

   ```sh
   python3 scripts/claude_settings.py remove
   python3 scripts/codex_hooks.py remove        # skip when Codex is not installed
   claude mcp remove --scope user ccboard
   codex mcp remove ccboard                     # skip when Codex is not installed
   ```

   The claude-mem plugin was installed by the installer when it was missing and is your tool now. To remove it too: `claude plugin uninstall claude-mem@thedotmack`.

5. **Undo the code-server tweaks.**

   ```sh
   python3 scripts/code_server_settings.py remove
   sudo rm /etc/systemd/system/code-server@$USER.service.d/ccboard.conf
   sudo rmdir /etc/systemd/system/code-server@$USER.service.d      # refuses if you added files of your own: leave it then
   ls ~/.config/code-server/config.yaml.ccboard-bak-*               # your own config, if the installer replaced it (CCBOARD_REPLACE_CODE_SERVER_CONFIG=1)
   head -1 ~/.config/code-server/config.yaml                        # "# managed by ccboard" means the installer wrote this file
   ```

   If a `config.yaml.ccboard-bak-<seconds>` file exists, restore it over the managed file: `mv ~/.config/code-server/config.yaml.ccboard-bak-<seconds> ~/.config/code-server/config.yaml` (with several, take the one you want; the oldest is your original). If there is none and the first line says `# managed by ccboard`, delete the managed file: `rm ~/.config/code-server/config.yaml`. If you stopped code-server in step 2 and want it back, run `sudo systemctl enable --now code-server@$USER` after restoring.

   `code_server_settings.py remove` takes back exactly the keys ccboard wrote and no others. The code-server program itself, its extensions and `~/.local/share/code-server` are kept. code-server came from code-server's own install script, which on Ubuntu normally installs a package: `dpkg -s code-server` tells you, and then `sudo apt-get remove code-server` removes it (to verify on your machine; this page did not run it).

6. **ntfy, only if the installer installed it.** The install output said "installing ntfy from archive.ntfy.sh" in that case. If you had ntfy before, restore your own `/etc/ntfy/server.yml` (when you ran the installer with `CCBOARD_REPLACE_NTFY_CONFIG=1` it copied yours to `/etc/ntfy/server.yml.ccboard-bak-<seconds>` first; without that setting it stopped instead of replacing it) and skip the package steps.

   ```sh
   sudo systemctl disable --now ntfy
   sudo apt-get remove ntfy
   sudo rm /etc/apt/sources.list.d/ntfy.list /etc/apt/keyrings/ntfy.gpg
   sudo apt-get update
   ```

   `/etc/ntfy/server.yml` is ccboard's file when its first line says `# managed by ccboard`; delete it yourself if the package leaves it behind. Phones subscribed to the topic simply stop receiving.

7. **Note on the apt package's ttyd unit.** The installer disabled the apt package's `ttyd.service` when it found one running as root. It does not re-enable it; do so only if you want it back.

8. **ttyd.** The installer put the upstream static binary at `/usr/local/bin/ttyd` unless a ttyd 1.7.4 or newer was already there. Remove it only if nothing else of yours uses it: `sudo rm /usr/local/bin/ttyd`.

9. **Backup tooling.** The installer installed `restic` from apt when it was missing (the output said "installing restic"). Remove it with `sudo apt-get remove restic` if you do not use it. The repository is in the data folder (step 13), or wherever `CCBOARD_RESTIC_REPO` points; a remote repository is never touched. The nightly run also pushed unpushed work to branches named `ccboard-backup/<node>/<branch>` on each repository's `origin`. They stay on GitHub until you delete them: in each repository `git ls-remote --heads origin 'ccboard-backup/*'` lists them and `git push origin --delete <branch>` removes one.

10. **Small extras.** `ccusage` and the devcontainer CLI, when the installer put them there, live under `~/.local`: `npm uninstall -g --prefix ~/.local ccusage` (and `@devcontainers/cli`). When `gh` was logged in, the installer ran `gh auth setup-git`, which adds a GitHub credential helper to your global git configuration; it is not undone here, and `git config --global --edit` lets you remove those lines if you want git to stop using the `gh` login. Claude Code itself (`~/.local/bin/claude`) was installed by Claude's own installer and stays.

11. **Settings and the restart rule.**

    ```sh
    sudo rm /etc/sudoers.d/ccboard
    sudo rm /etc/ccboard/env
    sudo rmdir /etc/ccboard
    ```

    `/etc/ccboard/env` holds the hub token and any lines you added by hand (for example restic remote credentials): copy those first if you need them. `rmdir` refuses when anything else is in that folder.

12. **tmux, last. This ends every agent session.** `ccboard-tmux.service` runs `tmux -L ccboard kill-server` when it stops. Finish or hand over your sessions first; there is no undo.

    ```sh
    sudo systemctl disable --now ccboard-tmux
    sudo rm /etc/systemd/system/ccboard.service /etc/systemd/system/ccboard-ttyd.service /etc/systemd/system/ccboard-tmux.service
    sudo rm /etc/systemd/system/ccboard-backup.service /etc/systemd/system/ccboard-backup.timer
    sudo rm -f /etc/systemd/system/ccboard-mem.service
    sudo systemctl daemon-reload
    tmux -L ccboard list-sessions     # "no server running": the socket /tmp/tmux-<uid>/ccboard is gone with it
    ```

    If a tmux server on that socket is still running after this (one you started by hand), `tmux -L ccboard kill-server` ends it. To keep the sessions alive instead, leave `ccboard-tmux` out of this step: do not run the first command and do not delete `ccboard-tmux.service`. The sessions keep running until the machine restarts.

13. **The data folder and the checkout (optional).**

    ```sh
    rm -r ~/.local/share/ccboard        # your CCBOARD_DATA_DIR: database, hook token, restic password and local repository, saved logins
    rm -r <your ccboard folder>         # the checkout, with its .venv
    ```

**What this leaves alone, on purpose:** the projects in `PROJECTS_DIR` (the installer created that folder and handed it to you if it was missing; it is not removed), `~/.claude` and `~/.codex` apart from the entries removed in step 4, the apt packages git, tmux, curl, ca-certificates, python3 and python3-venv, the claude-mem plugin, Tailscale and its other mappings, and the `ccboard-backup/<node>/*` branches on GitHub.

**Check that it is gone.**

```sh
systemctl list-unit-files 'ccboard*'          # no ccboard unit left
tailscale serve status                         # none of the mappings of step 3
ls /etc/ccboard /etc/sudoers.d/ccboard         # both: No such file or directory
claude mcp get ccboard                         # not found
```

## Linux with the container runtime

The container is only the board's own process; tmux, ttyd, code-server, ntfy, the backup timer and Tailscale stay on the host. Do Linux step 1 first (back up, and copy `<data dir>/restic-password`), then these, then steps 2 to 13 above (they apply as written; `ccboard.service` was installed but never enabled in this mode).

```sh
# before `down`: the backup (Linux step 1) has to be done, because it runs inside this container and `down` removes it
docker compose -f ~/.local/share/ccboard/compose/docker-compose.yml --profile prod down
crontab -l | grep -v '# ccboard-watchdog$' | crontab -     # the watchdog line install.sh added; if nothing else is left, `crontab -r` is the same
sudo rm /etc/systemd/system/docker.service.d/ccboard.conf  # ordered docker after tmux and tailscaled
sudo rmdir /etc/systemd/system/docker.service.d             # refuses if the folder holds anything of yours
sudo systemctl daemon-reload
sudo tailscale set --operator=                              # an empty value clears it; see The Tailscale operator below
docker image rm ghcr.io/rpandox/ccboard:latest nickfedor/watchtower:1      # optional; use the tag you pinned, if any. Docker refuses an image another container still uses
```

Use your own data folder in the first path if `CCBOARD_DATA_DIR` is not the default.

- **`down` without `-v`.** `down` stops and removes the `ccboard` and `ccboard-watchtower` containers and the compose network. It deletes no data: the compose file declares no named volume and the image has no `VOLUME`, so every folder the container used (your home, `PROJECTS_DIR`, the tmux socket directory, `/etc/ccboard`) is a bind mount from the host. `-v` is therefore not needed and removes nothing here, and `--rmi all` would only add the image removal above.
- **The Tailscale operator.** In container mode the installer ran `tailscale set --operator=<you>` so the container can run `tailscale serve` without `sudo`. While it is set, every process of yours can change `tailscale serve`. The installer overwrote any operator you had set before, so if you had one, set it back with `sudo tailscale set --operator=<that user>` instead of clearing it. To see the current value first, `tailscale debug prefs` prints the preferences as JSON, with `OperatorUser` in it (to verify: this page did not run it); if that is not available on your version, check your notes. `sudo tailscale set --operator=` with an empty value should clear it (to verify: this page did not run it); check `tailscale serve status` afterwards.
- **The compose files and the container's script copies** (`<data dir>/compose`, `<data dir>/app`) go with the data folder in step 13. `~/.docker/config.json` (an empty `{}` if the installer made it) is Docker's own file and is left alone.
- **Roll back to the systemd board instead** of removing it: [Run the board as a container](install/docker.md#rollback).

## macOS

Run `scripts/uninstall-macos.sh` from the ccboard folder. It needs no `sudo`, writes nothing outside your home folder and asks before it ends the sessions.

```sh
scripts/uninstall-macos.sh                # asks before it ends every session
scripts/uninstall-macos.sh --yes          # answers the question for you (required without a terminal)
scripts/uninstall-macos.sh --keep-tmux    # keeps dev.ccboard.tmux running, so the sessions survive
```

**What it does.** It boots out every `dev.ccboard.*` job that has a plist in `~/Library/LaunchAgents` and deletes the plist: `dev.ccboard.board`, `dev.ccboard.ttyd`, `dev.ccboard.tmux`, and the optional `dev.ccboard.backup`, `dev.ccboard.code-server`, `dev.ccboard.mem` and `dev.ccboard.awake`. `dev.ccboard.tmux` goes last, because booting it out ends the tmux server and every Claude and Codex session in it; with `--keep-tmux` it and its plist stay. With no `dev.ccboard.*` plist it prints "nothing to remove".

**What it leaves, and says so at the end.** Each of these has a command, from the script's own list and the installer's steps:

- The settings and data folder (`~/.local/share/ccboard` unless you chose another): the `env` file with the hub token, the database, the restic password file and the local restic repository. Remove it with `rm -r` when you are sure.
- The logs in `~/Library/Logs/ccboard`: `rm -r ~/Library/Logs/ccboard`.
- Your projects folder, and the checkout with its `.venv`.
- The Claude hooks and status line: `python3 scripts/claude_settings.py remove`. The Codex hooks: `python3 scripts/codex_hooks.py remove`.
- The MCP registrations: `claude mcp remove --scope user ccboard` and `codex mcp remove ccboard`.
- The `tailscale serve` mappings, which it does not touch: `python3 scripts/tailscale_serve.py off --https <port> --path / --target http://127.0.0.1:<port>`. That command turns off only a mapping that is ccboard's, one at a time, and there is no reset. Run it for the board (`/`, loopback port 8000 by default), for `/tty` (7681) on the same HTTPS port, and for code-server (8080) on its own port. It refuses 443 unless you give `--board-https 443` (or have `CCBOARD_HTTPS_PORT=443` in the environment). Read `tailscale serve status` first.
- The Homebrew tools (tmux, ttyd and the optional ones): `brew uninstall <name>` for each one you want gone.

Three more items come from the installer and are not in the script's list:

- code-server's settings: `python3 scripts/code_server_settings.py remove`, and `~/.config/code-server/config.yaml` when its first line is `# managed by ccboard`.
- The claude-mem plugin, if you want it gone: `claude plugin uninstall claude-mem@thedotmack`.
- `ccusage` in `~/.local` (`npm uninstall -g --prefix ~/.local ccusage`) and, when `gh` was logged in, the GitHub credential helper that `gh auth setup-git` added to your git configuration.

The `ccboard-backup/<node>/*` branches on your repositories' `origin` stay until you delete them (see step 9 above). Details of the Mac install: [Install on a Mac](install/macos.md).

## Windows with WSL2

The board runs inside the distro, so remove it there with the Linux steps above. Two Windows parts are not inside the distro.

- **The keep-alive task.** In Windows PowerShell, from the folder you copied the script to: `powershell -NoProfile -ExecutionPolicy Bypass -File .\ccboard-wsl-keepalive.ps1 -Remove`. It unregisters the scheduled task `ccboard-wsl-keepalive` and touches nothing else, not even `.wslconfig`. If you used `-WriteWslConfig`, the lines it added to `%UserProfile%\.wslconfig` stay, and the copy it made before that is next to the file as `.wslconfig.ccboard-bak-<time>`; edit the file by hand, and `wsl --shutdown` applies a change (it ends every running session, in every distro).
- **Tailscale on Windows** (`CCBOARD_TAILSCALE_PLACEMENT=host`). The installer mapped nothing from the distro in that case; you ran the `tailscale serve` commands it printed in PowerShell. Turn them off the same way in PowerShell, after `tailscale serve status`: `tailscale serve --https=<port> --set-path / off` for each of them, and `--set-path /tty` for the terminal.

See [Install on Windows with WSL2](install/windows-wsl2.md) for the install side.
