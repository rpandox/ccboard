# Security policy

ccboard is a self-hosted dashboard that gives a shell on your machine to whoever may open it, so a report about how it decides who may open it is welcome. This page says which versions get fixes, how to report a problem privately and what to expect. How the board is protected, and what it deliberately leaves to your tailnet rules, is in [docs/security.md](docs/security.md#who-can-reach-what).

## Supported versions

Only the newest code gets security fixes:

- the latest commit on `main`, and
- the latest released version, which is the newest `vX.Y.Z` tag of the container image `ghcr.io/rpandox/ccboard` (every merge to `main` is tested before it is published).

Older versions are not patched. Update first ([Upgrade](docs/upgrade.md)) and check whether the problem is still there.

## How to report a vulnerability

Report it privately, so nobody can use it before it is fixed. Do not open a public issue, a pull request or a discussion with the details.

1. Open <https://github.com/rpandox/ccboard/security/advisories/new>, or go to the repository's **Security** tab and choose **Report a vulnerability**. This is GitHub's private vulnerability reporting: only you and the maintainer can see the report.
2. If that page is not available to you, open a public issue that says only that you have a security report and asks for a private way to send it. Put no details in that issue.

## What to include

- The version: the commit, or the version in the CHANGELOG entry you run, and how you installed it (Linux, Mac, WSL2 or Docker).
- What you can do that you should not be able to do, and what you need to do it (a device on the tailnet, an allowed login, a token, local access to the machine).
- The steps, as short as you can make them, and what you saw.
- What you think the impact is.

Please leave out real tokens, credentials, host names, tailnet names and personal paths. Replace them with placeholders.

## What to expect

ccboard has one maintainer and no guaranteed response time. You can expect the report to be read, an answer in the report's own thread, and, when it is valid, a fix prepared in private and released before the details are made public, with credit to you if you want it. Say in the report if there is a date by which you plan to publish.

## What is not a vulnerability

Some things are the documented design, and a report that only restates them is not treated as one:

- The terminal (`/tty`), code-server and ntfy are protected by your tailnet access rules, not by the board's list of allowed logins ([Who can reach what](docs/security.md#who-can-reach-what)).
- A process already running as the board's user can do what that user can do.
- A tailnet whose access rules are wider than you meant.
- A flaw in Claude Code, Codex, Tailscale, tmux, ttyd or code-server themselves. Report those to their own projects.

The October 2026 review of the board's own routes, with what was fixed and what was accepted, is [docs/security-audit-2026-10.md](docs/security-audit-2026-10.md).
