# Changelog

One entry per shipped phase, newest first. The commit that ships a phase adds its entry (see CONTRIBUTING.md). Each entry says in its last line whether you have to rerun `./install.sh` on the box. `Upgrade: nothing to do` means a deploy is enough.

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
