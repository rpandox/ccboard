# ccboard

A self-hosted dashboard for **Claude Code CLI sessions** (and, since v0.5.11, **Codex CLI sessions**: see [Codex](docs/guide/codex.md#codex)) on your own Linux box, reachable only over your Tailscale network.

ccboard is a *status-and-attention layer*. It launches the real `claude` TUI inside tmux, shows you what is running where, lets you attach from any device (including your phone), and tells you when a session needs you. It never reimplements Claude or its chat: every Claude Code feature (slash commands, MCP, hooks, plan mode, resume, skills, worktrees) works untouched because the real terminal is what runs.

**Several devices:** [Set up nodes, a hub and epics](docs/nodes/overview.md#several-devices-nodes).

**Where it runs:** a Linux box (the reference), a Linux container, a Mac and Windows 11 through WSL2; native Windows is not supported. One line per system, with what is still to verify: [Where ccboard runs](#where-ccboard-runs-issue-115).

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

## What it is not

- Not a chat UI. The Claude TUI is the interface; the board is around it.
- Not multi-user: still one board per person and no shared control. One Unix user, one Tailscale identity allowlist. What exists is presence of teammates, opt-in per repository and exchanged only through GitHub, never board to board: a read of what GitHub shows ([Team](docs/team.md#team-who-is-working-on-what-issue-197-team-p1)) and, if you switch it on, your own live status in a comment on that repository ([Publish my status](docs/team.md#team-publish-my-status-issue-198-team-p2)). The board warns, as advice and never as a lock, before you start an issue, a branch or files a teammate already holds ([Conflict warnings](docs/team.md#team-conflict-warnings-issue-199-team-p3)), and shows the whole team in one read-only place ([Working with a team](docs/team.md#working-with-a-team-issue-200-team-p4)).
- Never exposed publicly. No Funnel, everything binds to loopback.
- Not a transcript parser. State comes from tmux and the agents' hooks. Transcripts and Codex rollouts are read for display, discovery, usage and cost only, never as the state source.

## Where ccboard runs (issue #115)

One line per system. "To verify" means the claim has not been run on a real machine of that kind yet; the device checks are in issue #130. The first column is the list of systems in the platform table of [Several devices](docs/nodes/platforms.md#several-devices-issue-154-phase-p6), and a test keeps the two equal.

| System | Support | Read |
|---|---|---|
| Linux box | Supported, the reference platform: Ubuntu 22.04 or 24.04 with systemd, installed by `./install.sh`. Everything below is checked on it first. | [Install](docs/install/linux.md#install) |
| Linux container | Supported on a Linux host with Docker Engine (`CCBOARD_RUNTIME=docker`); the container is only the board's process, the tmux server and Tailscale stay on the host. An arm64 image is not Docker Desktop support. | [Run the board as a container](docs/install/docker.md#run-the-board-as-a-container-optional) |
| Mac | Supported for a Mac that stays logged in: macOS 13 or newer, Homebrew, its own installer (`./install.sh` hands over), per-user launchd jobs. Use the board from another device; saved Claude logins are not available. What it survives (sleep, lid closed, logout, reboot) and a Background job reaching the Keychain secret are to verify. Measured so far on one Intel Mac running macOS 14 only (2026-10-10); not run on Apple silicon. On Intel, ttyd is a long source build. | [macOS](docs/install/macos.md#macos-issue-117), [macOS tools](docs/install/macos.md#macos-tools-issue-128) |
| WSL2 | Supported on Windows 11 22H2 or newer: the Linux install inside an Ubuntu distro with systemd, plus the Windows keep-alive (mandatory). Windows 10 is best effort. Not yet run on a real Windows machine (issue #130, W1 to W9): to verify. | [Windows (WSL2)](docs/install/windows-wsl2.md#windows-wsl2-issue-118) |
| Native Windows | Not supported (issue #124). Use WSL2. | [Windows (WSL2)](docs/install/windows-wsl2.md#windows-wsl2-issue-118) |

## Documentation

The documentation is one page per job under [docs/](docs/README.md). The old README headings and where each went are in [docs/moved.md](docs/moved.md).

### Install

| Page | What it covers |
|---|---|
| [Install on Linux](docs/install/linux.md) | Install on Ubuntu 22.04 or 24.04, and what `install.sh` sets up. |
| [Install on a Mac](docs/install/macos.md) | The macOS tools, the macOS installer, the launchd jobs and what survives sleep and logout. |
| [Install on Windows with WSL2](docs/install/windows-wsl2.md) | The ten parts of the WSL2 setup, with the keep-alive and the networking choices. |
| [Run the board as a container](docs/install/docker.md) | Container mode: where it works, build, shadow run, switch, rollback and limits. |
| [Tailscale on each system](docs/install/tailscale.md) | The `tailscale` commands the board uses, per system. |

### Run and look after it

| Page | What it covers |
|---|---|
| [Configuration reference](docs/configuration.md) | Every setting, the options page, the advanced and test-only variables, and Restart board. |
| [Upgrade](docs/upgrade.md) | How to update an install, and how the changelog is written. |
| [Uninstall ccboard](docs/uninstall.md) | The uninstall commands for Linux. |
| [Backup and restore](docs/backup-and-restore.md) | What is backed up on each system, the restore runbook and backup branches in your repos. |
| [Security](docs/security.md) | The security model, node scopes and routes, and the settings permission classes. |
| [Troubleshooting and FAQ](docs/troubleshooting.md) | The known problems and what to do. |

### Use it

| Page | What it covers |
|---|---|
| [Getting started with the board](docs/guide/getting-started.md) | The overview of Using it, and the v0.5 feature list. |
| [Home and projects](docs/guide/home-and-projects.md) | Home, the attention inbox, the project page, repos, code-server and transcript search. |
| [The launcher](docs/guide/launcher.md) | The one sheet that starts every kind of run. |
| [Tasks, diffs and pull requests](docs/guide/tasks-and-prs.md) | Tasks, the diff and PR modal, CI status, previews and work from GitHub issues. |
| [Schedules, batches and auto-continue](docs/guide/automation.md) | Schedules, batch prompts, auto-continue and reboot recovery. |
| [Notifications](docs/guide/notifications.md) | Push through ntfy and Web Push, remote approve, and notifications across nodes. |
| [The terminal](docs/guide/terminal.md) | Terminal commands, the terminal page, the terminal on a phone and the live grid. |
| [Usage and cost](docs/guide/usage-and-cost.md) | The usage strip and page, series, accounts usage and cost. |
| [Accounts and login](docs/guide/accounts.md) | Several Claude and Codex accounts, login expiry and running on a subscription. |
| [Installing the board as an app](docs/guide/install-as-app.md) | The board as a PWA on a laptop, an iPad or a phone. |
| [Calling the board over MCP](docs/guide/mcp.md) | The board's MCP endpoint and the Claude and Codex commands. |
| [Memory with claude-mem](docs/guide/claude-mem.md) | The claude-mem step, unit and handover. |
| [Using Codex with the board](docs/guide/codex.md) | How the board starts Codex, headless runs, hooks, trust and MCP, and updating Codex. |
| [New project and clone](docs/guide/new-project-and-clone.md) | The new-project wizard and the clone preflight. |

### Several devices

| Page | What it covers |
|---|---|
| [Several devices: overview](docs/nodes/overview.md) | What a node is, identity, names and finding other nodes. |
| [Pairing two boards](docs/nodes/pairing.md) | Pairing, tokens and node signing keys. |
| [The hub and the relay](docs/nodes/hub-and-relay.md) | The hub read model and view, the relay, remote work, steering, streams and terminals. |
| [Nodes on each platform](docs/nodes/platforms.md) | The platform table for nodes. |
| [Using nodes from an agent](docs/nodes/agents-and-mcp.md) | The node tools an agent can call. |
| [Epics](docs/nodes/epics.md) | Epics, claiming, working an issue, routing, failures, the epic worker and starting an epic. |
| [Devices without a board and the device check](docs/nodes/device-check.md) | Devices without a board, and the optional device check. |

### Reference

| Page | What it covers |
|---|---|
| [Doctor and agents reference](docs/reference/doctor.md) | The Doctor checklist, the agents API, backup, CI and version checks. |
| [Hooks](docs/reference/hooks.md) | Hooks v2, what the hook scripts need, and which hooks the board ignores. |

### Team and development

| Page | What it covers |
|---|---|
| [Team](docs/team.md) | Who is working on what, publishing your status, conflict warnings and working with a team. |
| [Developing ccboard](docs/development.md) | Run the board locally, the tests, the lock files and demo mode. |
| [Deploying](docs/maintainers/deploying.md) | Updating the container and the outage per push, and what to do when GitHub Actions is down. |
| [Design notes](docs/maintainers/design-notes.md) | Deviations from the original spec. |

## License

MIT.
