# Changelog

One entry per shipped phase, newest first. The commit that ships a phase adds its entry (see CONTRIBUTING.md). Each entry says in its last line whether you have to rerun `./install.sh` on the box. `Upgrade: nothing to do` means a deploy is enough.

## v0.5.27 - 2026-10-08

### Fixed
- Pages load faster: the board compresses its static files (scripts, styles, demo data; never the live stream, the API or the terminal), loads scripts deferred, fetches the icon font early and keeps the sidebar, the Home usage card and the Usage sections from jumping while the page fills in. Lighthouse in demo mode went from 39-62 to 92-98 on desktop and from 31-58 to 73-88 on a phone.
- Accessibility: every page passes axe-core with no violation at 1280 and 390 (before: a low-contrast "ended" tag, an issue link marked by colour only, a scrollable strip with no keyboard stop, skipped heading levels, the sidebar resize handle outside a landmark and two empty table headers). The terminal page's context strip opens from the keyboard. Nothing looks different.
- `scripts/qa-ui.sh` passes on main again: its task, chip and session-peek checks follow the current design.

Upgrade: nothing to do.

## v0.5.26 - 2026-10-08

### Added
- Windows through WSL2: a README section in the order of the setup, `scripts/windows/ccboard-wsl-keepalive.ps1` (a per-user Scheduled Task that keeps the distro running), and an installer branch that runs only inside WSL. It refuses without systemd, warns about projects, data or agents on a Windows drive, and with `CCBOARD_TAILSCALE_PLACEMENT=host` prints the Tailscale serve commands to run on Windows instead of mapping from the distro. Doctor checks `wsl-*` (systemd, files, agents, network, keep-alive, placement, restarts in the last day) appear on WSL only.
- Saved logins say why they are off on each system: on a Mac, Claude Code keeps its login in the Keychain, so the board cannot swap it, and Codex logins can still be saved. Doctor checks `claude-creds-store` (macOS) and `codex-cred-store` report where each login lives without reading it.

### Changed
- Saved Codex logins work only where Codex keeps its login in a file; with `cli_auth_credentials_store` set to `keyring` or `ephemeral` the board says so and how to change it.

Upgrade: nothing to do.

## v0.5.25 - 2026-10-08

### Added
- A macOS installer: `./install.sh` on a Mac hands over to `scripts/install-macos.sh` (stock bash 3.2, no sudo), which installs the Homebrew tools, writes the settings file, maps the board through Tailscale serve, renders and loads the launchd jobs, sets up the Claude and Codex hooks and the MCP server (with the board's own venv python), and checks `/healthz`. A rerun changes nothing that is already right, and the tmux job is never restarted once it runs. `scripts/uninstall-macos.sh` removes the jobs and keeps your data.
- The nightly backup runs on a Mac as a launchd job at the same schedule, and "Back up now" starts it through launchd without sudo. A new `backup-job` doctor check says whether the job or timer is installed, how old the last run is and whether restic is on the job's PATH.

### Fixed
- A restic repository that is neither an absolute path nor a known remote (`sftp:`, `s3:`, `rclone:` and the others) is refused with a clear message instead of being handed to restic.
- `CCBOARD_RUNTIME=docker` on a Mac is refused with the same reason the Linux installer gives.

Upgrade: nothing to do.

## v0.5.24 - 2026-10-08

### Added
- One Tailscale module (`app/tailscale.py`) finds the CLI on Linux, macOS (open-source, App Store and standalone builds) and WSL2, reads serve status and turns serve on and off; previews use it. `scripts/tailscale_serve.py` checks and applies the board's serve mapping and never touches another port's handler or port 443 unless it is the board's own. A `tailscale` doctor check says what is missing and how to fix it.
- The board's macOS side: it reads its settings from `<data dir>/env` when launchd starts it (a private file, `KEY=value` lines, only the installer's keys), finds claude and codex through the login shell and the Homebrew folders, and refuses a project or repo name that differs from an existing one only by letter case on a case-insensitive disk. Seven macOS doctor checks (jobs, survival, PATH, privacy folders, case, sleep, logs) appear on a Mac only.
- launchd job templates for the board, tmux, ttyd, keep-awake, code-server and claude-mem with a renderer that checks every value, and a macOS Brewfile with ttyd and prebuilt-package checks. The macOS installer that uses them comes next.

### Changed
- Off Linux the doctor finds tools the LaunchAgent PATH cannot see. Linux answers are unchanged: no login shell is started and no case probe runs there.

Upgrade: nothing to do.

## v0.5.23 - 2026-10-08

### Added
- One platform module (`app/platform.py`) answers every operating-system question: uid, locks, file writes, default paths, the runtime, process lists, host numbers and fix hints. On a Mac the Box panel shows CPU, memory and uptime, preview ports and the Codex foreign-process warning work (through psutil), and no doctor fix names apt-get, systemctl or journalctl.
- `launchd` is accepted as a runtime and never honours the dev bypass; a refused bypass is logged.
- Doctor checks: `tmux-socket` (path, mode, length, a missing socket file), `runtime-host` (the container runtime on Docker Desktop) and `hook-helpers` (curl and python3 under the hooks' PATH).
- CI runs the suite on macOS as an advisory job that never gates the image; pytest markers for platform tests.

### Fixed
- The board finds its tmux socket by asking tmux, not by guessing from the uid and /tmp.
- Claude hook commands are quoted, so an install path with a space works; the installer refuses such an APP_DIR for the systemd units.
- The installer refuses `CCBOARD_RUNTIME=docker` under Docker Desktop in WSL2.
- With no uid the claude-mem worker port is 37777, as claude-mem 13.34.2 computes it.

Upgrade: nothing to do.

## v0.5.22 - 2026-10-08

### Added
- Usage has a Reported / Estimated switch that prices the models ccusage leaves at zero, and sessions started outside the board are counted by their folder.
- The command palette lists the box's skills, most used first, and headless Claude runs get a subagent model default, no permission prompts and a Fable guard that needs a per-run acknowledgement.
- The claude-mem viewer can be opened over tailscale serve on a port you choose, and the Files tab hints at core.untrackedCache when a repo scan is slow.
- Accessibility checks: vendored axe-core, focus rings, aria labels and a Lighthouse strict mode in scripts/qa-ui.sh.

### Fixed
- A context-compacting SessionStart no longer moves a session's id, the done notice says when the session will close, the memory-env doctor check works in the container, and the scheduler cap test is no longer flaky.

Upgrade: rerun ./install.sh.

## v0.5.18 - 2026-10-06

### Added
- Notifications carry context: the body names the session and the reason, with actions you can tap.
- Each notification kind has its own on or off switch.
- Rate-limit notices collapse until the limit resets.

Upgrade: nothing to do.

## v0.5.16 - 2026-10-06

### Added
- Schedules and chains per agent, so a schedule can run Claude or Codex.
- Codex runs headless, from schedules and batches.

Upgrade: nothing to do.

## v0.5.9c - 2026-10-06

### Added
- Quad view holds up to ten tiles (layouts of 1, 2, 4, 6, 8 and 10).
- Fullscreen for the quad (F or Ctrl+Alt+F).
- Every tile has a menu with view, input and tune groups.

Upgrade: nothing to do.

## v0.5.13 - 2026-10-06

### Added
- One launcher sheet for every entry point, with an agent picker.
- Options come from the agent's own schema; the sheet shows the command it will run.
- Defaults are remembered per repo and per agent.

Upgrade: nothing to do.

## v0.5.17h - 2026-10-05

### Added
- Refresh on the Usage page asks an idle Claude session for `/usage`, so the limits are current on demand.

Upgrade: nothing to do.

## v0.5.9b - 2026-10-05

### Added
- A Quad button on each project page; the quad then shows only that project's sessions.

Upgrade: nothing to do.

## v0.5.17g - 2026-10-05

### Added
- A dead login is flagged, and Log in again renews the saved login in place.

### Fixed
- Re-logging in to Codex no longer creates a second account.

Upgrade: nothing to do.

## v0.5.17f - 2026-10-05

### Changed
- Usage stays current when nothing runs: Claude Code's own cached figures are a second source, and the page shows how old they are.

Upgrade: nothing to do.

## v0.5.9 - 2026-10-05

### Added
- Quad view with one, two or four live terminals, two-up by default.
- A terminal dock on wide screens.

Upgrade: nothing to do.

## v0.5.12 - 2026-10-05

### Added
- Codex sessions show their context, tokens, model and limits from their rollout (display only).
- Rate limits per Codex account, and a cost figure for Codex.
- Codex threads started outside the board are found.

Upgrade: nothing to do.

## v0.5.15 - 2026-10-05

### Added
- A task's result is kept when its session stops, and the next chain step starts with it.
- A task session closes itself after a grace period, unless it is waiting on you.
- Tasks move with a Move sheet or by drag and drop.

Upgrade: nothing to do.

## v0.5.17e - 2026-10-04

### Added
- Several Codex accounts: save logins, add one from Settings, switch with one tap.

Upgrade: nothing to do.

## v0.5.17d - 2026-10-04

### Added
- Settings > Accounts can add a Claude account, switch with one tap and forget a saved login.

Upgrade: nothing to do.

## v0.5.11 - 2026-10-04

### Added
- Codex as a second agent: launch, resume, recover, hooks, identity and MCP for the Codex CLI.

Upgrade: rerun ./install.sh.

## v0.5.17c - 2026-10-04

### Added
- Several saved Claude logins with a one-tap account switch (backend).

Upgrade: nothing to do.

## v0.5.8a - 2026-10-04

### Changed
- The terminal page's side panel is an inspector, not a wall of boxes.

Upgrade: nothing to do.

## v0.5.17b - 2026-10-04

### Added
- Usage per subscription account, and the total across accounts, on the Usage page and in the topbar.

Upgrade: nothing to do.

## v0.5.5d - 2026-10-04

### Changed
- The nightly backup no longer runs `git push --all`. Unpushed work goes only to `ccboard-backup/<node>/<branch>` on each repo's `origin`, never to `main`.

Upgrade: rerun ./install.sh.

## v0.5.8 - 2026-10-04

### Added
- Terminal page v2: a tuning strip, quick replies and a queue.

### Changed
- UI polish pass: one button hierarchy and muted state colours.

Upgrade: nothing to do.

## v0.5.10 - 2026-10-04

### Added
- claude-mem on the box: its memory health shows in the state and in the doctor.

Upgrade: rerun ./install.sh.

## v0.5.7 - 2026-10-04

### Added
- Hooks v2: 15 Claude Code hook events, notifications with context, and the `/command`, `/prompt` and `/resize` commands.
- Hooks from claude-mem's observer and from nested Claude sessions are ignored.

Upgrade: rerun ./install.sh.

## v0.5.14a - 2026-10-04

### Added
- Tasks from anywhere: Now, Later and Schedule, in a project folder or in place.

Upgrade: nothing to do.

## v0.5.6c - 2026-10-03

### Changed
- Deploy gate: a Watchtower swap waits while a terminal is open, and the stop is short.

Upgrade: nothing to do.

## v0.5.6b - 2026-10-03

### Added
- After a power cut, a session that was working gets `continue` once its relaunch is back.

Upgrade: nothing to do.

## v0.5.6a - 2026-10-03

### Added
- Auto-continue after a limit reset.

Upgrade: rerun ./install.sh.

## v0.5.6 - 2026-10-03

### Added
- Project page with a lazy directory tree, file preview, code-server links and tabs.

Upgrade: nothing to do.

## v0.5.5c - 2026-10-03

### Added
- Container watchdog: starts the board when a Watchtower restart did not take, and restarts it when it stays unhealthy.

Upgrade: rerun ./install.sh.

## v0.5.5b - 2026-10-03

### Changed
- code-server is cheap to open: managed user settings, and lower I/O and CPU priority for the interactive units.

Upgrade: rerun ./install.sh.

## v0.5.5a - 2026-10-03

### Changed
- A rejected mirror push makes the backup run `partial`, not `failed`. The usage card and Settings tell the three states apart.

Upgrade: nothing to do.

## v0.5.5 - 2026-10-03

### Added
- Home page with a summary line whose states are filters, an away strip and project blocks.
- Inbox with context and the keyboard layer.

Upgrade: nothing to do.

## v0.5.4b - 2026-10-03

### Added
- Samples backbone: a time series store, its endpoints and the rate-limit episodes.

Upgrade: nothing to do.

## v0.5.4a-term - 2026-10-03

### Added
- Terminal page on a phone: scroll and send from the phone, with a 44 px key bar.

Upgrade: rerun ./install.sh.

## v0.5.4a - 2026-10-03

### Changed
- Backend foundations: the Claude agent adapter seam, idempotent migrations, the tasks data layer and the doctor.

Upgrade: nothing to do.

## v0.5.3b - 2026-10-03

### Added
- PWA for iPad and laptop: install prompt, title-bar topbar, keyboard layer and command palette.

Upgrade: nothing to do.

## v0.5.3a - 2026-10-03

### Added
- Terminal composer: a multi-line send box (Enter sends, Shift+Enter adds a line).

Upgrade: nothing to do.

## v0.5.3 - 2026-10-03

### Added
- Shell: hash routes, a topbar, a sidebar tree, a bottom nav with a drawer, the Agents roster and demo mode.

Upgrade: nothing to do.

## v0.5.2 - 2026-10-03

### Changed
- Visual system: JetBrains Mono and Inter, design tokens, and state and agent glyphs.

Upgrade: nothing to do.

## v0.5.1-docker - 2026-10-02

### Added
- Container delivery: the image is built on GitHub Actions and Watchtower updates the board.

Upgrade: rerun ./install.sh.

## v0.5.1 - 2026-10-02

### Changed
- Foundations: the classic-script split, a generated service-worker shell and a glob-based asset version.

Upgrade: nothing to do.
