# Backup and restore

For people who back up the board, and for anyone who has to restore it.

[Back to the documentation index](README.md)

### Backup on each system (issue #129)

What is backed up does not change (the DB snapshot, claude-mem's snapshot, the transcripts, the Codex rollouts, `CCBOARD_BACKUP_EXTRA`, and the unpushed work copied to `ccboard-backup/<node>/<branch>`); only the job that runs it differs. Linux keeps its timer, unit and wording exactly as they were.

| | Linux (systemd) | Mac (launchd) | WSL2 |
|---|---|---|---|
| The job | `ccboard-backup.timer` and `.service` | LaunchAgent `dev.ccboard.backup`, installed unless `CCBOARD_BACKUP=0` | the systemd timer, when systemd is on in the distribution |
| Schedule (`CCBOARD_BACKUP_ONCALENDAR`) | any systemd calendar spec, default `*-*-* 02:30:00`, plus up to 15 minutes of random delay | the same setting, only `*-*-* HH:MM[:SS]` or `HH:MM` (every day); any other form stops the installer and names these two | as Linux |
| A run missed while the machine was off or asleep | `Persistent=true`: it runs at the next start | launchd runs a calendar job missed during sleep when the Mac wakes (launchd.plist(5)); **to verify on a Mac**. A Mac that is switched off at the set time runs nothing until the next calendar time | as Linux; `Persistent=true` covers a VM that was stopped at 02:30 |
| Priority | `Nice=10`, idle I/O | `Nice` 10, `LowPriorityIO`, `ProcessType` Background | as Linux |
| **Back up now** | `sudo -n systemctl start --no-block ccboard-backup.service`; the log is `journalctl -u ccboard-backup` | `launchctl kickstart gui/$(id -u)/dev.ccboard.backup` (no sudo, no `-k`); the log is `~/Library/Logs/ccboard/backup.log` | as Linux |
| Where there is no job | a detached process, log `<data dir>/backup.log` | the same, when the job is not loaded | the same |

Either way a second start while a run holds the lock answers 409 ("a backup is already running"), and "Backup started" means started: the result shows in the Backup status line from the board's own record.

**A sleeping laptop.** A job set for 02:30 on a Mac that sleeps runs on the next wake, which can be mid-morning while you work. That is fine for a backup, but the "last backup" time will not read 02:30. The launchd catch-up is **to verify**: the sleep part of the device check (issue #130, M10) could not run.

**Pushing needs a key that works without a prompt.** The branch pushes run `ssh -oBatchMode=yes`, so a passphrase prompt is a failure, not a question. A launchd job in the `gui/<uid>` domain, the installer's default, has `SSH_AUTH_SOCK` set; a job in the `user/<uid>` domain with the Background session type does not (measured 2026-10-10 on an Intel Mac, macOS 14, issue #130 M10: only set or unset was recorded, never the value). So the nightly push over ssh needs the gui domain, and `CCBOARD_LAUNCHD_DOMAIN=user` cannot push through an agent. Whether the agent then holds your Keychain-backed key when the job runs is **to verify**. If the last run could not push, the Doctor's `backup-job` row says so and names the fix: load the key without a prompt (`ssh-add --apple-use-keychain <key file>`) or use an https remote (`gh auth setup-git`). The snapshot is not affected.

**The repository setting.** `CCBOARD_RESTIC_REPO` is a folder on this machine (an absolute path), one of the restic remotes (`sftp:`, `rclone:`, `s3:`, `b2:`, `azure:`, `gs:`, `rest:`, `swift:`), or `off`; empty keeps the default under the data dir. A relative path, `~/x` or any other string is refused: the run stops before `restic` is started, and the Backup status line shows the reason. A Windows path such as `C:\backups` is refused the same way, because native Windows is not supported (issue #124). Under WSL2 a drive is a Linux path (`/mnt/c/...`), and that is where the next paragraph applies.

**WSL2.** A repository under `/mnt/` sits on the Windows file system through drvfs: it is slow and ignores file modes, so the Doctor warns. Keep the repository on the WSL file system (ext4) or on a remote. As a second layer for the whole distribution, `wsl --export <distro> <file>.vhd --format vhd` writes a copy of it from Windows.

**Restoring.** See [Restoring from a backup](#restoring-from-a-backup) below: what a restore needs, the read-only look, the restore into a scratch folder, the disaster restore, and getting unpushed work back from a backup branch.

### Restoring from a backup

Drilled on 2026-10-09 on a docker-mode box (issue #111): restoring the newest snapshot (758 MiB, 2354 files) into a scratch folder took 10 s, the database plus one transcript folder took 3 s, and a backup branch (33 commits) cloned back in 13 s. The restored database passed `integrity_check`, and a board started on a copy of it served the Usage history. The disk was quiet during the drill; a busy disk will be slower. Not drilled: the disaster restore below (steps 3 to 5 replace live data), and a repository on another disk or a remote.

**What a restore needs**

1. **The restic password.** It is the file `<data dir>/restic-password` (`CCBOARD_RESTIC_PASSWORD_FILE`). If you did not copy it off the box, the repository cannot be opened. By default the password file sits **on the same disk as the repository**, so one lost disk loses both: copy the password somewhere else once.
2. **The repository.** Default `<data dir>/restic` (`CCBOARD_RESTIC_REPO` empty), which is the same disk as the data: it covers a bad delete or a corrupted file, not a dead disk (the Doctor's `backup-repo` row warns about exactly that). Point `CCBOARD_RESTIC_REPO` at another disk or a remote (`sftp:`, `rclone:`, `s3:`, `b2:`) to cover the dead disk, and keep the password file off the data disk too.
3. **`restic` on the machine that restores** (`apt install restic`). The version need not match: restic 0.16 read a repository written by 0.18 in the drill. Under docker the nightly run uses the restic inside the board's container, but the repository is a plain folder on the host.
4. **For unpushed work:** access to the repository's `origin`.

**Look first (read-only)**

```sh
export RESTIC_REPOSITORY=<data dir>/restic RESTIC_PASSWORD_FILE=<data dir>/restic-password
restic snapshots            # one per day, tag ccboard
restic check                # the structure; add --read-data-subset=5% for a spot check of the data
```

**Restore into a scratch folder, never over live data**

```sh
restic restore latest --target /tmp/restore \
  --include <data dir>/backup-stage/ccboard.db \
  --include <claude config dir>/projects/<project folder>
sqlite3 -readonly /tmp/restore<data dir>/backup-stage/ccboard.db 'pragma integrity_check; select count(*) from sessions;'
```

A snapshot keeps the original absolute paths, so a file lands at `<target><original path>`. The snapshot holds `<data dir>/backup-stage/ccboard.db` (a consistent copy made with SQLite's backup API), `<data dir>/backup-stage/claude-mem.db`, `<claude config dir>/projects` (the transcripts), `<codex home>/sessions` (the Codex rollouts) and whatever `CCBOARD_BACKUP_EXTRA` names. It never holds a login: after a restore on a new machine you log in to Claude and Codex again.

**Try the restored data in a board that cannot touch the live one.** Copy the restored `ccboard.db` into the shadow board's own data folder and start the shadow run ([Run the board as a container, step B](install/docker.md#b-shadow-run-no-sudo-nothing-of-the-live-board-touched)): its compose file has no live mount, no host network and no published port, and you read it with `docker exec ccboard-shadow curl ...`. In the drill, a board started on the restored database served 30 days of Usage history, and its counts were one day behind the live database (59 sessions against 64).

**Disaster restore (a new machine, or a wiped data dir)**

1. Install ccboard as for a first install (see [Install](install/linux.md#install); with `CCBOARD_RUNTIME=docker` that also writes the compose stack). This creates the data dir and the units.
2. Stop the board: `sudo systemctl stop ccboard.service`, or on docker `docker compose -f <data dir>/compose/docker-compose.yml --profile prod stop ccboard`.
3. Restore the snapshot into a scratch folder as above, copy `ccboard.db` over `<data dir>/ccboard.db`, and delete `ccboard.db-wal` and `ccboard.db-shm` beside it. Copy the transcript folders back to `<claude config dir>/projects/`. For claude-mem, stop its worker first, then copy `claude-mem.db` to `<claude-mem dir>/claude-mem.db`.
4. Start the board again, then run `./install.sh` once more so the hooks and units point at the restored data.
5. Log in to Claude and Codex again (Settings, Accounts).

**Unpushed work.** Each repo's unpushed commits are on `origin` as `ccboard-backup/<node>/<branch>`: `git fetch origin && git switch -c <branch> origin/ccboard-backup/<node>/<branch>`. Drilled: a backup branch of a repo whose local branch had no upstream cloned back with all 33 commits and the same tip as the local branch.

#### CI in your repos (backup branches)

The nightly backup pushes unpushed work to `origin` under `ccboard-backup/<node>/<branch>`. Any workflow in that repository with a `push` trigger runs for those pushes. A backup branch never has a pull request, so `pull_request:` stays as it is.

The fix belongs in the other repository's workflow file, not in ccboard. Add a branch filter to the `push` trigger:

```yaml
on:
  push:
    branches-ignore:
      - 'ccboard-backup/**'
  pull_request:
```

A workflow that says `on: [push, pull_request]` is rewritten into the mapping form above. `branches-ignore` is GitHub Actions workflow syntax; check the [workflow syntax page](https://docs.github.com/en/actions/using-workflows/workflow-syntax-for-github-actions) before you rely on it.

The other choice is `CCBOARD_BACKUP_PUSH=0` ([settings table](configuration.md#settings)). It turns the branch copies off on this box, so unpushed work then has no copy outside the box.

## What v0.4 adds: autonomy & fleet

- **Backup**: `ccboard-backup.timer` runs nightly (02:30 by default; on a Mac the launchd job `dev.ccboard.backup`, see [Backup on each system](#backup-on-each-system-issue-129)): a consistent snapshot of the board's SQLite DB, your `~/.claude/projects` transcripts, the Codex rollouts (`~/.codex/sessions`) and a consistent snapshot of claude-mem's `claude-mem.db` (`<claude-mem dir>`, taken through SQLite's backup API from a read-only connection, so claude-mem's database is never written; only that one file, never the directory) go into a restic repository (plus anything in `CCBOARD_BACKUP_EXTRA`; the saved logins are never in it, and neither is a laptop's claude-mem database, which is a different file on a different machine), then every repo under `PROJECTS_DIR` gets its unpushed work copied to backup branches on its `origin` (`ccboard-backup/<node>/<branch>`), so WIP from tasks survives the box. The backup never pushes to `main` or to any branch you work on. The strip shows the last result and the 🔔 panel has "Back up now"; a failed run pushes an ntfy warning.
