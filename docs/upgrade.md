# Upgrade

For people updating a running install and reading the CHANGELOG `Upgrade:` line.

[Back to the documentation index](README.md)

## Updating a Linux install

Update: pull or extract the new version into the same directory and rerun `./install.sh`. It restarts only `ccboard` (stateless) unless something else changed. Restarting `ccboard` never touches running Claude sessions, because they live under `ccboard-tmux.service`. `install.sh` also writes `/etc/sudoers.d/ccboard`, which lets your user restart `ccboard` and `ccboard-ttyd` without a password, so code-only updates are `git pull && sudo systemctl restart ccboard` (or `scripts/deploy.sh <host>` from your machine).

### Changelog

`CHANGELOG.md` has one entry per shipped phase, newest first. The commit that ships a phase adds its entry in the same commit (CONTRIBUTING.md says how). `tests/test_changelog.py` checks the heading, the date order and the `Upgrade:` line, which says whether `./install.sh` has to be rerun on the box.

## Updating Claude Code

The board does not update Claude Code. `install.sh` installs it with the native installer (`curl -fsSL https://claude.ai/install.sh | bash`) only when it is missing, and does not update it afterwards. Update it on the box as the user who owns the sessions: run `claude update`, or update it the way you installed it. On a docker-mode box the container uses the host's `claude`, so update that one, on the host.

Some board options need a newer Claude Code than the one installed (for example `auto` as the starting permission mode needs 2.1.283, `--permission-prompts none` needs 2.1.259). Until you update, the launcher hides or refuses those options with the reason, instead of sending a flag the CLI would reject. Settings > Doctor lists them in the `claude-features` row (a warning, with `claude update` as the fix) and makes no network call; the full table of versions is in [Doctor and agents reference](reference/doctor.md#backup-ci-and-version-checks-v0521). Press Re-check in the Doctor after updating.
