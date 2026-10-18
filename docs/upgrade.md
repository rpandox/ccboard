# Upgrade

For people updating a running install and reading the CHANGELOG `Upgrade:` line.

[Back to the documentation index](README.md)

Every kind of install updates differently, and every release says in one line whether you have to do more than update. Find your install in the table, read the changelog first ([below](#changelog)), back up, then follow its section.

| Your install | Update with | Section |
|---|---|---|
| Linux (Ubuntu, systemd) | `git pull`, then `./install.sh`, or only `sudo systemctl restart ccboard` when the changelog line allows it | [Updating a Linux install](#updating-a-linux-install) |
| Mac (launchd) | `git pull`, then `./install.sh` | [Updating a Mac](#updating-a-mac) |
| Windows with WSL2 | the Linux steps, inside the distro | [Updating on WSL2](#updating-on-wsl2) |
| Docker (container runtime) | nothing for most releases: Watchtower pulls the new image | [Updating the container](#updating-the-container) |

## Before you update

### Changelog

`CHANGELOG.md` has one entry per shipped change, newest first: a heading `## vX.Y.Z - date`, up to five bullets under Added, Changed or Fixed, and last a line that starts `Upgrade:`. That line is the one to read. It says what you have to do besides getting the new version:

- `Upgrade: nothing to do.` means a plain update is enough. You do not have to rerun `./install.sh`. On Linux you still need the new code to be running, so pull it and restart the board ([Updating a Linux install](#updating-a-linux-install)); with Docker, Watchtower does that for you.
- `Upgrade: rerun ./install.sh.` means the release changed something the installer sets up: `install.sh` itself, a systemd unit, `tmux.conf` or the Claude settings. Pull, then rerun the installer once.
- A longer sentence names a condition or an extra step, for example "for an older Docker install, rerun `./install.sh` on the host ..., then run `docker compose up -d --force-recreate ccboard`. Native installs: nothing to do." Read the whole line, and act only on the part that describes your install.

**Read it before you pull.** `git fetch origin` downloads the new history without changing what is running. Then:

```sh
git log --oneline HEAD..origin/main            # what is new, one line each; subjects start with the version
git diff HEAD origin/main -- CHANGELOG.md      # the entries you are about to take, each with its Upgrade: line
git diff --stat HEAD origin/main               # the files that change; install.sh, systemd/, tmux.conf or requirements.txt in the list means a rerun
```

If you skip several versions, read every entry between yours and the new one. One `rerun ./install.sh` anywhere in them means one rerun, at the end. Rerunning is always safe: the installer is idempotent and changes nothing it does not have to.

### Back up first

Press Back up now in Settings, or run `sudo systemctl start ccboard-backup` (on a Mac: `launchctl kickstart gui/$(id -u)/dev.ccboard.backup`). See [Backup and restore](backup-and-restore.md) for what is in a backup and for restoring one.

### What an update never touches

Your running sessions. They live in the tmux server that `ccboard-tmux.service` (Linux) or the `dev.ccboard.tmux` job (Mac) owns, and neither the installer nor the commands below restart it: the installer prints a warning instead when that unit's file changed, and leaves the restart to you. Restarting the board never closes a session. Do not restart the tmux unit while sessions run; that is the one thing that ends them.

## Updating a Linux install

Update: pull or extract the new version into the same directory and rerun `./install.sh`. It restarts only `ccboard` (stateless) unless something else changed. Restarting `ccboard` never touches running Claude sessions, because they live under `ccboard-tmux.service`. `install.sh` also writes `/etc/sudoers.d/ccboard`, which lets your user restart `ccboard` and `ccboard-ttyd` without a password, so code-only updates are `git pull && sudo systemctl restart ccboard` (or `scripts/deploy.sh <host>` from your machine).

A code-only update is enough only when two things hold: the `Upgrade:` line says nothing to do, and `requirements.txt` is not among the changed files. The installer is what refreshes the Python environment in `.venv` when `requirements.txt` changed, so a plain restart would leave the old packages in place. When in doubt, rerun the installer.

```sh
cd ~/ccboard && git pull
./install.sh                            # on a remote machine: ssh -t <host> 'cd ccboard && ./install.sh' (sudo needs a terminal)
sudo systemctl restart ccboard          # instead of the line above, when the changelog says nothing to do
curl -fsS http://127.0.0.1:8000/healthz # answers ok while the board runs (8000 is the default CCBOARD_PORT)
```

The installer remembers your settings in `/etc/ccboard/env`, so a rerun keeps them ([Configuration](configuration.md)). Settings > Doctor lists anything the update left unhappy.

## Updating a Mac

```sh
cd <your ccboard folder> && git pull && ./install.sh
```

This is the line the installer prints at the end ("Update:"). `./install.sh` hands over to the Mac installer, needs no `sudo`, and is safe to rerun at any time.

A rerun changes nothing it should not: a plist that renders the same is neither rewritten nor reloaded, the board restarts (`kickstart -k`) only when the checkout, the requirements or the settings file changed, a changed plist of the board, ttyd or an optional job is booted out and in again, and `dev.ccboard.tmux` is never booted out or kickstarted once it runs (a changed tmux plist only prints a warning, because reloading it would end every session). A job you switched off is booted out and its plist removed.

To restart only the board after an update that needs no rerun: `launchctl kickstart -k gui/$(id -u)/dev.ccboard.board` (sessions and the terminal stay). More commands are in [Install on a Mac](install/macos.md#daily-use).

## Updating on WSL2

WSL2 is the Linux install inside an Ubuntu distro, so [Updating a Linux install](#updating-a-linux-install) applies as written, run inside the distro. Two WSL2 details:

- If `git diff --stat HEAD origin/main` ([above](#changelog)) lists `scripts/windows/ccboard-wsl-keepalive.ps1`, copy the new script out of the distro to Windows and run it again from Windows PowerShell; the script says it replaces the scheduled task and does not duplicate it ([Install on Windows with WSL2](install/windows-wsl2.md)).
- `wsl --update` updates WSL itself, not ccboard, and `wsl --shutdown` ends every running session in every distro. Neither is part of a ccboard update.

This path has not been run on a real Windows machine yet (see [the supported systems](../README.md#supported-systems)).

## Updating the container

With `CCBOARD_RUNTIME=docker` the board is a container of the image `ghcr.io/rpandox/ccboard`, and [Watchtower](https://github.com/nicholas-fedor/watchtower) on the host pulls a new image every five minutes. For a release whose changelog says nothing to do, you do nothing: the board restarts once, about ten seconds in the maintainer's measurements, and your sessions are untouched because tmux stays on the host. The swap waits while someone is attached to a terminal, a permission is pending or a clone is running, for at most 30 minutes.

What you do yourself:

- Rerun `./install.sh` on the host when a release changes `install.sh`, `deploy/`, the units or `tmux.conf` (the changelog says so). It keeps `CCBOARD_RUNTIME=docker`. `git pull` first, in your checkout.
- After editing `/etc/ccboard/env` by hand, recreate the container so it sees the edit: `docker compose -f ~/.local/share/ccboard/compose/docker-compose.yml up -d --force-recreate ccboard`. Watchtower recreates a container from its existing configuration and would not see the change.
- To stay on one version, pin the image tag: `CCBOARD_IMAGE_TAG=sha-1a2b3c4 ./install.sh` (a `vX.Y.Z` tag works the same way). The installer keeps the tag you gave on later reruns. `CCBOARD_IMAGE_TAG=latest ./install.sh` goes back to following `latest`.
- Check the result: `docker compose -f ~/.local/share/ccboard/compose/docker-compose.yml --profile prod ps`, then `docker logs --tail 50 ccboard` (its startup line shows the image version).

The maintainer's side of this (how the image is built and swapped, the outage per push, building on the host when GitHub Actions is down) is in [Deploying](maintainers/deploying.md).

## Going back

Nothing here undoes a release for you. The board's tests check that the previous release's queries still work on a database the newer one has migrated; going back further is not checked. Take the backup first.

- **Linux and WSL2.** Check out the older commit in your clone (`git log --oneline` lists them; each subject starts with its version), then rerun `./install.sh` so the units, the environment and the hooks match that version. Not run on a real machine: to verify.
- **Mac.** The same: check out the older commit, then rerun `./install.sh`. Not run on a real Mac: to verify.
- **Container.** Pin the older image tag and rerun the installer ([above](#updating-the-container)). To leave container mode for the systemd board instead, see [Rollback](install/docker.md#rollback).

If the older version fails to start, restore the backup you made ([Restoring from a backup](backup-and-restore.md#restoring-from-a-backup)) and ask for help with the log line (`journalctl -u ccboard -n 50`, `tail -n 30 ~/Library/Logs/ccboard/board.log` on a Mac, `docker logs --tail 50 ccboard` in a container).

## Updating Claude Code

The board does not update Claude Code. `install.sh` installs it with the native installer (`curl -fsSL https://claude.ai/install.sh | bash`) only when it is missing, and does not update it afterwards. Update it on the box as the user who owns the sessions: run `claude update`, or update it the way you installed it. On a docker-mode box the container uses the host's `claude`, so update that one, on the host.

Some board options need a newer Claude Code than the one installed (for example `auto` as the starting permission mode needs 2.1.283, `--permission-prompts none` needs 2.1.259). Until you update, the launcher hides or refuses those options with the reason, instead of sending a flag the CLI would reject. Settings > Doctor lists them in the `claude-features` row (a warning, with `claude update` as the fix) and makes no network call; the full table of versions is in [Doctor and agents reference](reference/doctor.md#backup-ci-and-version-checks-v0521). Press Re-check in the Doctor after updating.

Codex is updated by hand too, and never through its own update prompt: see [Updating Codex on the box](guide/codex.md#updating-codex-on-the-box).
