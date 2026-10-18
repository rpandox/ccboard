# ccboard documentation

For anyone who installs, runs or changes ccboard. Pick a page below, or start with [Install on Linux](install/linux.md), [Install on a Mac](install/macos.md), [Install on Windows with WSL2](install/windows-wsl2.md) or [Run the board as a container](install/docker.md).

[Back to the project README](../README.md)

## Install

| Page | What it covers |
|---|---|
| [Install on Linux](install/linux.md) | A procedure for Ubuntu 22.04 or 24.04: checklist, install, what you should see, how to verify, and what `install.sh` changes. |
| [Install on a Mac](install/macos.md) | A procedure for macOS 13 or newer: checklist, install, verify, what the installer changes, the Homebrew tools, the launchd jobs and what survives sleep and logout. |
| [Install on Windows with WSL2](install/windows-wsl2.md) | A procedure in ten parts, with the keep-alive and the networking choices. Not yet run on a real Windows machine. |
| [Run the board as a container](install/docker.md) | A procedure for container mode: checklist, where it works, the image, a shadow run, the switch, rollback and limits. |
| [Tailscale on each system](install/tailscale.md) | Why the board needs Tailscale, the two admin-console switches, ports, and the `tailscale` command the board uses per system. |

## Run and look after it

| Page | What it covers |
|---|---|
| [Configuration reference](configuration.md) | The few settings most people set, then one table per group, the options page, the advanced and test-only variables, and Restart board. |
| [Upgrade](upgrade.md) | How to update an install, and how the changelog is written. |
| [Uninstall ccboard](uninstall.md) | The uninstall commands for Linux. |
| [Backup and restore](backup-and-restore.md) | What is backed up on each system, the restore runbook and backup branches in your repos. |
| [Security](security.md) | The security model, node scopes and routes, and the settings permission classes. |
| [Troubleshooting and FAQ](troubleshooting.md) | The known problems and what to do. |

## Use it

| Page | What it covers |
|---|---|
| [Getting started with the board](guide/getting-started.md) | The overview of Using it, and the v0.5 feature list. |
| [Home and projects](guide/home-and-projects.md) | Home, the attention inbox, the project page, repos, code-server and transcript search. |
| [The launcher](guide/launcher.md) | The one sheet that starts every kind of run. |
| [Tasks, diffs and pull requests](guide/tasks-and-prs.md) | Tasks, the diff and PR modal, CI status, previews and work from GitHub issues. |
| [Schedules, batches and auto-continue](guide/automation.md) | Schedules, batch prompts, auto-continue and reboot recovery. |
| [Notifications](guide/notifications.md) | Push through ntfy and Web Push, remote approve, and notifications across nodes. |
| [The terminal](guide/terminal.md) | Terminal commands, the terminal page, the terminal on a phone and the live grid. |
| [Usage and cost](guide/usage-and-cost.md) | The usage strip and page, series, accounts usage and cost. |
| [Accounts and login](guide/accounts.md) | Several Claude and Codex accounts, login expiry and running on a subscription. |
| [Installing the board as an app](guide/install-as-app.md) | The board as a PWA on a laptop, an iPad or a phone. |
| [Calling the board over MCP](guide/mcp.md) | The board's MCP endpoint and the Claude and Codex commands. |
| [Memory with claude-mem](guide/claude-mem.md) | The claude-mem step, unit and handover. |
| [Using Codex with the board](guide/codex.md) | How the board starts Codex, headless runs, hooks, trust and MCP, and updating Codex. |
| [New project and clone](guide/new-project-and-clone.md) | The new-project wizard and the clone preflight. |

## Several devices

| Page | What it covers |
|---|---|
| [Several devices: overview](nodes/overview.md) | What a node is, identity, names and finding other nodes. |
| [Pairing two boards](nodes/pairing.md) | Pairing, tokens and node signing keys. |
| [The hub and the relay](nodes/hub-and-relay.md) | The hub read model and view, the relay, remote work, steering, streams and terminals. |
| [Nodes on each platform](nodes/platforms.md) | The platform table for nodes. |
| [Using nodes from an agent](nodes/agents-and-mcp.md) | The node tools an agent can call. |
| [Epics](nodes/epics.md) | Epics, claiming, working an issue, routing, failures, the epic worker and starting an epic. |
| [Devices without a board and the device check](nodes/device-check.md) | Devices without a board, and the optional device check. |

## Reference

| Page | What it covers |
|---|---|
| [Doctor and agents reference](reference/doctor.md) | The Doctor checklist, the agents API, backup, CI and version checks. |
| [Hooks](reference/hooks.md) | Hooks v2, what the hook scripts need, and which hooks the board ignores. |

## Team and development

| Page | What it covers |
|---|---|
| [Team](team.md) | Who is working on what, publishing your status, conflict warnings and working with a team. |
| [Developing ccboard](development.md) | Run the board locally, the tests, the lock files and demo mode. |
| [Deploying](maintainers/deploying.md) | Updating the container and the outage per push, and what to do when GitHub Actions is down. |
| [Design notes](maintainers/design-notes.md) | Deviations from the original spec. |

## Other files

| Page | What it covers |
|---|---|
| [memory-api.md](memory-api.md) | The claude-mem worker API the board talks to. |
| [security-audit-2026-10.md](security-audit-2026-10.md) | The October 2026 security audit and what it changed. |
| [moved.md](moved.md) | Where each old README heading went (temporary). |
| [img/README.md](img/README.md) | The two README screenshots: what they show and how to retake them. |
