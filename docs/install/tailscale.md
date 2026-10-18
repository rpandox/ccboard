# Tailscale on each system

For anyone setting up Tailscale for the board, on any supported system.

[Back to the documentation index](../README.md)

The board is reachable only over your Tailscale network. This page says why, what to switch on, and which `tailscale` command the board runs on each system.

## Why the board needs Tailscale

The board knows who you are from the `Tailscale-User-Login` header that `tailscale serve` adds to every request, and it lets in only the logins in `CCBOARD_ALLOWED_USERS` (by default your own tailnet login). Everything in the install binds to loopback (`127.0.0.1`), and `tailscale serve` is the only way in. The board is never exposed publicly: it never turns Funnel on.

A request that does not come through the tailnet https name, such as plain `http://localhost:<port>`, carries no identity and gets a 403. For that address there is an optional local sign-in ([Security](../security.md#security-model)).

## Before you start

- [ ] Tailscale is installed and signed in on the machine that runs the board (the table below says which variant each system uses).
- [ ] **MagicDNS** is on for your tailnet (admin console, DNS page).
- [ ] **HTTPS certificates** are on for your tailnet (the same page). Without them `tailscale serve` cannot apply the board's ports, and the installer stops with "Is HTTPS enabled for the tailnet?".
- [ ] A second device is signed in to the same tailnet (your phone or laptop). You open the board from it.
- [ ] You know which ports you want. The defaults are 443 for the board and the terminal (`/tty`) and 8443 for code-server.

## Set it up

1. In the Tailscale admin console, open the DNS page and turn on MagicDNS and HTTPS certificates.
2. Install Tailscale on the machine that runs the board and sign in. The variants per system are in the table below.
3. Check it: `tailscale status` lists this machine and your second device.
4. Run the installer for your system ([Linux](linux.md), [a Mac](macos.md), [Windows with WSL2](windows-wsl2.md), [Docker](docker.md)). It checks Tailscale and the ports before it changes anything, then maps the board, `/tty` and code-server with `tailscale serve`.
5. Check the mappings with `tailscale serve status`. The first request to a port right after `tailscale serve` started it can wait for its certificate; retry it (see the evidence at the end of this page).

## Ports

- The board and the terminal share `CCBOARD_HTTPS_PORT` (default 443, the board at `/` and the terminal at `/tty`). code-server uses `CODE_HTTPS_PORT` (default 8443).
- Tailnet-only `tailscale serve` accepts any HTTPS port. If a chosen port already carries something else (another serve handler or a Funnel), the installer stops and tells you. Pick other ports, for example `CCBOARD_HTTPS_PORT=8443 CODE_HTTPS_PORT=10000 ./install.sh`, or set `CCBOARD_REPLACE_SERVE=1` to replace that port's handlers.
- Port 443 is never taken from another service: it is never touched unless it is the board's own HTTPS port. The installer never runs `tailscale serve reset` and never touches a port you did not name.
- A port that is already a Funnel is never replaced unless you set `CCBOARD_REPLACE_SERVE=1`.

## Tailscale on each system (issue #126)

Everything the board knows about the `tailscale` command lives in `app/tailscale.py`: which command to run, which variant is installed (from the file layout, no network call), and how `tailscale serve` is run for preview links, the macOS installer and the Doctor. Linux behaves exactly as before. `scripts/tailscale_serve.py` is the command line over the same module for the macOS installer (`status`, `check`, `apply`, `off`; there is no `reset`).

| System | The command the board runs | Serve without `sudo` | What works |
|---|---|---|---|
| Linux (systemd) | `tailscale` on `PATH`, else `/usr/bin/tailscale` | no: a denied `serve` is retried once with `sudo -n` through the rule `install.sh` writes in `/etc/sudoers.d/ccboard` | serve ports, files, Funnel (the board never turns Funnel on) |
| Linux, container | the same, through the socket mounted at `/var/run/tailscale/tailscaled.sock` | yes, once `sudo tailscale set --operator=<you>` was run on the host (`install.sh` does it in container mode); there is no `sudo` in the container | serve ports |
| macOS, App Store app | `tailscale` on `PATH` if there is one, else `/Applications/Tailscale.app/Contents/MacOS/Tailscale` run with `TAILSCALE_BE_CLI=1` (there was no `tailscale` on `PATH` on the measured Mac: the CLI is reachable only as the app binary with `TAILSCALE_BE_CLI=1`) | works as the logged-in user, no sudo, no operator (measured 1.102.4) | serve ports; no files, no Funnel, no run-before-login; a `unix:` target answers 502, so loopback TCP stays |
| macOS, Standalone package | the launcher `/usr/local/bin/tailscale` (installed from the app's Settings), else the app binary as above | expected yes; to verify | serve ports; no files, no Funnel, no run-before-login |
| macOS, open-source `tailscaled` (Homebrew) | `/opt/homebrew/bin/tailscale` (or `/usr/local/bin`); start the daemon with `sudo brew services start tailscale` | after `sudo tailscale set --operator=<you>` once | serve ports, files, Funnel, run before login |
| Windows | `tailscale.exe` under Program Files (to verify) | to verify | serve ports; the rest to verify |
| WSL2, Tailscale on the Windows host | `tailscale.exe` through Windows interop, when the distro has no `tailscale` (to verify) | to verify | serve ports; the rest to verify |

- **No `sudo` off Linux.** The board never calls `sudo` on a Mac or on Windows. A refused `serve` becomes a message that names the variant and the fix for it, and the Doctor's `tailscale` check (group box) reports the command found, the variant, whether Tailscale is signed in, the MagicDNS name, HTTPS certificates and whether `serve status` can be read by the board's user. On Linux a `serve status` that is denied to your user passes when the `sudo -n` retry works, and the check says so.
- **Funnel and the variants.** Tailscale's pages disagree: the Funnel page says macOS needs one of the open-source variants, while an older page says the App Store and Standalone apps can Funnel ports (not files). The board treats Funnel as unsupported on those two. A port that is already a Funnel is never replaced unless you set `CCBOARD_REPLACE_SERVE=1`, and port 443 is never touched unless it is the board's own HTTPS port.
- **Identity on the same machine.** Through the tailnet, the board knows who you are from the header `tailscale serve` adds. A request that reaches the board through the tailnet https name should carry it even when it is made from the machine that runs the board; the one run that measured it was on a Mac, with curl (under "Evidence and open checks"). Only a request to plain `http://localhost:<port>` carries none; use the optional local sign-in ([Security](../security.md#security-model)) for that address (issue #127).
- **The first https request waits for the certificate.** The first request to a port right after `tailscale serve` started it can return nothing for a while, and the next ones answer. A caller retries the first request or gives it a longer time-out (the one run is under "Evidence and open checks").
- **Previews say why they cannot work (issue #126).** When a preview cannot be published, the task card's Preview button is disabled and a line under it says "unavailable: <reason>" before you click: `CCBOARD_PUBLIC_URL` is not set (rerun install.sh); Tailscale runs on the Windows side of a WSL2 distro, which has no route to expose a preview; Tailscale is not installed; Tailscale is not running or cannot be reached from the board; Tailscale is not logged in on this device; or the last `tailscale serve` was refused within 5 minutes (the sentence names the fix, for example `sudo tailscale set --operator=<user>`). The board reads its cached Tailscale reading and runs no `serve` to decide; a refusal can only be known after one real try. A preview that is already running keeps its link.

## Verify

- `tailscale status` shows this machine as connected.
- `tailscale serve status` lists the board (`/`), `/tty` and code-server on the ports you chose.
- Open the printed Dashboard address from your second device. A page that does not load usually means HTTPS certificates are off or the first request is still waiting for its certificate.
- Settings > Doctor has a `tailscale` check that reports the command found, the variant, sign-in, the MagicDNS name, HTTPS certificates and whether `serve status` can be read.

## Next steps

Install the board: [Linux](linux.md), [a Mac](macos.md), [Windows with WSL2](windows-wsl2.md) or [Docker](docker.md). Who can reach what once it runs: [Security](../security.md).

## Evidence and open checks

- **Measured (issue #130, M2 and M3, measured 2026-10-10 on an Intel Mac, macOS 14, App Store build 1.102.4).** `serve status`, `serve --bg --https=<port> http://127.0.0.1:<port>` and `serve --https=<port> off` all worked as the logged-in user, with no `sudo`, no `--operator` and no denial text. A `unix:` target is accepted by `serve` but every request answered 502 for a socket under `/private/tmp` (the socket itself answered locally; whether the cause is the sandbox or the path was not separated), so the board keeps serving `http://127.0.0.1:<port>` and nothing relies on `unix:` on a Mac.
- **Identity on the same machine (measured 2026-10-10 on an Intel Mac, macOS 14, App Store build 1.102.4, with curl).** `Tailscale-User-Login`, `Tailscale-User-Name`, `Tailscale-User-Profile-Pic` and `Tailscale-Headers-Info` arrived on a request to the tailnet https name made from the Mac itself. A real browser and a phone were not run.
- **The first https request waits for the certificate (measured 2026-10-10 on an Intel Mac, macOS 14).** The first request to a port right after `tailscale serve` started it returned nothing within 20 seconds, and the next ones answered 200.
- **Background.** Issue #126 (Tailscale on each system), #127 (local sign-in) and #130 (the device check).
- **Still unverified:** the Standalone and Homebrew variants (not installed on the measured Mac), and every Windows and WSL detail above. The device check (issue #130) records the measured answers, and this table is corrected to match.
