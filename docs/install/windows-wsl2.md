# Install on Windows with WSL2

For people running ccboard on Windows 11 inside WSL2.

[Back to the documentation index](../README.md)

**This path has not yet been run on a real Windows machine (issue #130, W1 to W9): everything below is to verify.** Nothing here is worded as tested. The board's code does not change for WSL2: it rests on the Linux install, which is the reference platform, and the keep-alive script and the Doctor rows added for WSL2 have tests of their own. The open checks are listed at the end of this page under "Evidence and open checks". Other systems: [Linux](linux.md), [a Mac](macos.md). One line per system: [Supported systems](../../README.md#supported-systems).

## Before you start

The board runs on Windows inside WSL2, as the unmodified Linux install inside an Ubuntu distro. WSL2 is Linux. What is added is a Windows-side keep-alive (without it WSL stops the machine and takes every tmux session with it), a stated networking mode, a stated place for Tailscale, the installer's WSL branch and a few Doctor checks.

- [ ] Windows 11 22H2 or newer, with WSL2 and an Ubuntu 22.04 or 24.04 distro that runs systemd (parts 1 and 2 below set that up).
- [ ] Tailscale, signed in, on the Windows host or inside the distro (part 7 says which placement and why), with MagicDNS and HTTPS certificates enabled for your tailnet ([Tailscale](tailscale.md)).
- [ ] A second device on the same tailnet, such as your phone. You open the board from it. A Windows browser at `http://localhost:<port>` is rejected with a 403 (part 8 has the optional local sign-in).
- [ ] The checkout, the projects folder, the data folder, `~/.claude` and `~/.codex` on the distro's own disk, never under `/mnt/c`.
- [ ] A Windows PowerShell window for the WSL commands and the keep-alive script, and a terminal inside the distro for `sudo`.
- [ ] Claude Code and Codex installed inside the distro, not as Windows programs (part 4).

| Setup | Status |
|---|---|
| Windows 11 22H2 or newer, WSL2, Ubuntu 22.04 or 24.04 with systemd, this recipe | supported |
| Windows 10 build 19041 or newer, WSL2 | best effort: NAT networking only, and whether `[boot] systemd` works there is UNVERIFIED (Microsoft documents `[boot]` for Windows 11 and Server 2022) |
| Native Windows (no WSL) | not supported (issue #124) |
| WSL1 | not supported (Claude Code has no WSL1 sandbox; Codex ended WSL1 support at 0.114) |
| Docker Desktop's WSL backend | not supported; docker-ce installed inside the distro is fine (see [where the container runtime works](docker.md#where-the-container-runtime-works)) |
| Projects, data folder, `~/.claude` or `~/.codex` on `/mnt/c` | not supported: the Doctor warns (see below) |

## Install

Do the parts in order: 1 and 2 prepare the distro, 3 and 4 install the board and the agents, 5 keeps the distro alive, 6 and 7 settle networking and where Tailscale runs, 8 opens the board in a Windows browser, 9 is a backup tip and 10 lists the Doctor rows to check.

### 1. WSL and the distro

In Windows PowerShell:

```powershell
wsl --install -d Ubuntu
wsl --update
wsl -l -v          # the VERSION column must say 2
```

### 2. systemd

Inside the distro, put this in `/etc/wsl.conf` (`[interop] appendWindowsPath=false` stops a Windows `claude` or `codex` from shadowing the Linux ones, and it takes Windows programs off the distro's PATH):

```ini
[boot]
systemd=true

[interop]
appendWindowsPath=false
```

Then, in Windows PowerShell, `wsl --shutdown` (it ends every running session, so do it when nothing runs), open the distro again and check `systemctl is-system-running`. It must print `running` (`degraded` also works: a unit failed). Needs WSL 0.67.6 or newer. `install.sh` refuses early, with these lines, when systemd is not running.

### 3. Install the board inside the distro, on the distro's own disk

Use the normal Linux install ([Install on Linux](linux.md#install)) from a checkout under your Linux home, never under `/mnt/c`:

```sh
git clone https://github.com/rpandox/ccboard ~/ccboard && cd ~/ccboard
./install.sh
```

**Decide where Tailscale runs before you start.** `./install.sh` as written needs a `tailscale` command inside the distro, and it stops with an error when there is none. If Tailscale runs only on Windows, you must run it with `CCBOARD_TAILSCALE_PLACEMENT=host` and name your tailnet login and the PC's address yourself, because the installer cannot read them from Windows:

```sh
CCBOARD_TAILSCALE_PLACEMENT=host CCBOARD_ALLOWED_USERS=you@provider \
  CCBOARD_PUBLIC_URL=https://<this PC>.<your tailnet>.ts.net ./install.sh
```

`CCBOARD_ALLOWED_USERS` is the login you use on the tailnet and `CCBOARD_PUBLIC_URL` is the PC's name as `tailscale status` shows it on Windows; the installer stops when either is missing. `host` also widens who can sign in, and it prints the `tailscale serve` commands to run on Windows afterwards: read part 7 first.

Keep the projects, the data folder, `~/.claude` and `~/.codex` on the distro's ext4 disk. On a Windows drive (`/mnt/c`, a drvfs or 9p mount) file owners look wrong (the projects folder must be owned by the board's user), modes 0600 and 0700 are ignored, change stamps are weak and everything is slow. The installer warns when `PROJECTS_DIR`, `CCBOARD_DATA_DIR` or the checkout is below `/mnt/`, and the Doctor's `wsl-files` check warns when any of the four folders is on a Windows drive.

### 4. The agents, inside the distro

```sh
curl -fsSL https://claude.ai/install.sh | bash            # Claude Code
curl -fsSL https://chatgpt.com/codex/install.sh | sh      # Codex
sudo apt-get install -y bubblewrap socat                  # what their sandboxes use
which claude codex                                        # neither may point under /mnt/c
```

The login callback usually cannot reach the distro. The board's own login flow already works for that: click *Log in*, copy the link, open it in your Windows browser and paste the code back. The installer warns when `claude` or `codex` resolves under `/mnt/`, and the Doctor's `wsl-path` check does too.

### 5. The keep-alive (mandatory)

Microsoft documents idle timeouts in minutes, not hours: `[general] instanceIdleTimeout` (default 15000 ms) and, on Windows 11, `[wsl2] vmIdleTimeout` (default 60000 ms), and says the machine shuts down when no Windows process holds a file handle to it. It also says that systemd services do NOT keep a WSL instance alive. So a `ccboard.service` inside the distro is not enough: without a keep-alive, the tmux server and every agent session end minutes after the last terminal closes.

`scripts/windows/ccboard-wsl-keepalive.ps1` registers a per-user Scheduled Task, "At log on", named `ccboard-wsl-keepalive`, that keeps one hidden `wsl.exe -d <distro>` process open. Copy it out of the distro to Windows and run it from Windows PowerShell (no administrator rights are needed):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ccboard-wsl-keepalive.ps1 -Distro Ubuntu
```

| Parameter | Meaning |
|---|---|
| `-Distro <name>` | The distro as `wsl -l -q` lists it. Default: the first one listed. A name that is not installed prints a message and the script exits 0 without changing anything |
| `-Method sleep` (default) | The task runs `wsl.exe -d <distro> --exec sleep infinity` in a hidden window and stays running. It follows from Microsoft's file-handle sentence; that it holds on every WSL version is to verify |
| `-Method dbus` | The task runs `wsl.exe -d <distro> --exec dbus-launch true`, the community recipe (needs the `dbus` package in the distro) |
| `-Remove` | Unregisters the task and stops; touches nothing else |
| `-WriteWslConfig` | Writes the belt-and-braces lines below into `%UserProfile%\.wslconfig`, after copying the file to `.wslconfig.ccboard-bak-<time>`. Without it the lines are only printed |
| `-Mirrored` | Adds `networkingMode=mirrored` to those lines (Windows 11 22H2 or newer) |

Running it again replaces the task, it never makes a second one. The lines it prints (and writes only with `-WriteWslConfig`) are `[general]` `instanceIdleTimeout=-1` and `[wsl2]` `vmIdleTimeout=-1`. Field reports say those two alone do not stop the distro after the last terminal closes (WSL discussion 9245), and that WSL 2.6.1.0 stops an instance 15 to 20 seconds after the last terminal closes even with an active unit (WSL issue 13416); the task is what holds the distro. Behaviour on WSL 3.x is UNVERIFIED. The script edits nothing else.

Check it in Windows with `Get-ScheduledTask -TaskName ccboard-wsl-keepalive` (State `Running`), or read the Doctor's `wsl-keepalive` row.

**Reboots and logon.** WSL starts after you log on to Windows, not before (starting before logon is UNVERIFIED). After a Windows restart there is no board until someone logs on, unless Windows auto-logon is on. A Windows restart, `wsl --shutdown` or an idle-out ends the tmux server exactly like a power cut; the board's reboot recovery (`CCBOARD_RECOVER`, `CCBOARD_AUTO_CONTINUE`) relaunches the sessions and types `continue` into the ones that were working.

### 6. Networking

- **NAT** (the default, and the only mode on Windows 10): a Windows browser reaches a server in the distro on `http://localhost:<port>` (`localhostForwarding`, relayed by `wslrelay.exe`). Reaching it from the LAN needs `netsh interface portproxy` on Windows.
- **Mirrored** (`networkingMode=mirrored` in `%UserProfile%\.wslconfig`, Windows 11 22H2 or newer): `127.0.0.1` works both ways (`::1` does not), and the LAN can reach the distro once a Hyper-V firewall rule allows the inbound port.

The board, ttyd and code-server bind to `127.0.0.1` only, so nothing is reachable from the LAN unless you add a rule on purpose. The Doctor's `wsl-network` row names the mode (from `wslinfo --networking-mode`; older WSL has no `wslinfo`, and the row says the mode is unknown) and warns when one of the three ports listens beyond loopback.

### 7. Where Tailscale runs

The board's only identity is the `Tailscale-User-Login` header that `tailscale serve` adds, so `serve` must sit in front of it. Tailscale recommends running it on the Windows host only, "and not inside WSL 2" (both at once breaks traffic from the distro through the host). Two placements:

| `CCBOARD_TAILSCALE_PLACEMENT` | Where | What the installer does |
|---|---|---|
| `wsl` | Tailscale inside the distro (against Tailscale's advice) | The unchanged Linux path: checks Tailscale, maps `/`, `/tty` and code-server with `tailscale serve`, previews work |
| `host` | Tailscale on Windows | Skips the Tailscale checks and the `serve` mappings. You must give `CCBOARD_ALLOWED_USERS` and `CCBOARD_PUBLIC_URL`. It prints one PowerShell `tailscale serve` command per mapping (`/`, `/tty`, code-server, and ntfy when it is on) to run on Windows |

Left empty, the installer uses `wsl`, and it stops with an error when the distro has no `tailscale` command; it never falls back to `host` for you, so set `host` yourself when Tailscale runs only on Windows. The default may change after the device check (issue #130). The choice is remembered in `/etc/ccboard/env` so the Doctor can read it.

```sh
CCBOARD_TAILSCALE_PLACEMENT=host CCBOARD_ALLOWED_USERS=you@provider \
  CCBOARD_PUBLIC_URL=https://<this PC>.<your tailnet>.ts.net ./install.sh
```

**`host` widens who can sign in.** The board trusts the `Tailscale-User-Login` header on its loopback port. Inside the distro only the distro's own programs reach that port; with `host`, Windows forwards its localhost to the distro, so every program on the Windows PC, and every other Windows user signed in to it, can send that header and act as any allowed user. Use `host` only on a PC you alone use; the installer never picks it for you and the Doctor warns while it is set.

`tailscale serve` only takes `http://127.0.0.1` proxies. That the Windows host reaches a port of the distro on its own `127.0.0.1` follows from the localhost forwarding rules but is not documented (UNVERIFIED until the device check). Previews open ports with the local `tailscale` command and cannot reach a Windows Tailscale from the distro (calling `tailscale.exe` through interop is the route described in [Tailscale on each system](tailscale.md#tailscale-on-each-system), also UNVERIFIED), so with `host` the Doctor's `wsl-placement` row marks previews unavailable and says why. If you use port 443 for something else on Windows, choose `CCBOARD_HTTPS_PORT=8443`; the board never runs `tailscale serve reset`.

### 8. The browser on Windows

Open the tailnet HTTPS address `install.sh` printed (`https://<name>.<tailnet>.ts.net:<port>/`) in Chrome or Edge, then install it: the install icon at the right of the address bar, or menu, *Install ccboard* (Edge: ..., Apps, *Install this site as an app*); see [Install as an app](../guide/install-as-app.md#install-as-an-app). Edge and Chrome on Windows can get Web Push (the board allows the `.notify.windows.com` push endpoints).

A Windows browser at `http://localhost:<port>` gets a 403, because a request to that address carries no Tailscale identity. To sign in there (issue #127), enable `CCBOARD_LOCAL_SIGNIN=1` in the board's service environment inside WSL, restart the board, then run `bin/ccboard-open` in that checkout inside WSL. It uses `wslview` or `cmd.exe /c start` to open the Windows browser, or prints the link. Set `CCBOARD_PORT` for a non-default port. The loopback connection must remain loopback as seen by WSL; other peer addresses are refused. Install the PWA from the loopback address the command opens; a host-only cookie is not shared between `127.0.0.1` and `localhost`. Local sign-in is off by default; the tailnet address keeps its existing identity check.

### 9. Backups

Besides the board's own backup ([Backup on each system](../backup-and-restore.md#backup-on-each-system)), `wsl --export <distro> <file>.vhd --format vhd` in Windows PowerShell writes a copy of the whole distro.

### 10. The Doctor on WSL

These rows (group Box, ids start with `wsl-`) exist only when the board runs inside WSL:

| Row | Passes when |
|---|---|
| `wsl-systemd` | `systemctl is-system-running` says `running` or `degraded` |
| `wsl-files` | the projects, data, Claude config and Codex folders are not on a Windows drive |
| `wsl-path` | `claude` and `codex` resolve inside the distro, not under `/mnt/` |
| `wsl-network` | the mode is NAT or mirrored and the board, ttyd and code-server listen on loopback only |
| `wsl-keepalive` | Windows answers (through `powershell.exe`, interop) that the `ccboard-wsl-keepalive` task is registered and running. With interop off it shows as unknown with the fix, never as passing |
| `wsl-restarts` | fewer than 2 restarts of the distro or its virtual machine in the last 24 hours, counted from the board's own record of when PID 1 started (it is kept on the Sampler's 15 s tick, so a board restart alone is not a restart of the distro) |
| `wsl-placement` | Tailscale is where `CCBOARD_TAILSCALE_PLACEMENT` says; `host` is a warning (every Windows program can reach the board and claim a login; previews unavailable) |

A Doctor inside the distro cannot say that the distro is stopped, because the board stops with it: `wsl-restarts` reports the stop afterwards. Settings, Box host says the CPU, memory and disk numbers describe the Linux machine, not Windows.

## What you should see

- Part 1: `wsl -l -v` lists the distro with VERSION 2. Part 2: `systemctl is-system-running` prints `running`.
- Part 3: the installer ends with the same summary as on Linux ([Install on Linux](linux.md#what-you-should-see)): "ccboard is installed." and the Dashboard address. With `host` it also prints one PowerShell `tailscale serve` command per mapping, to run on Windows before the addresses answer.
- Part 5: `Get-ScheduledTask -TaskName ccboard-wsl-keepalive` shows State `Running`.
- Open the Dashboard address from another tailnet device and click *Log in*.

## Verify

- Settings > Doctor lists the seven `wsl-` rows of part 10. A row that is not passing says why and what to do.
- Close every terminal, wait a few minutes, and check that `tmux -L ccboard list-sessions` inside the distro still answers: that is the keep-alive working.
- `systemctl status ccboard ccboard-tmux ccboard-ttyd` inside the distro shows three running services.

## What the installer changes

Inside the distro it is the Linux install ([What the installer changes](linux.md#what-the-installer-changes)). The WSL branch adds: an early stop when systemd is not running, warnings when `PROJECTS_DIR`, `CCBOARD_DATA_DIR`, the checkout, `claude` or `codex` are on a Windows drive, and, with `CCBOARD_TAILSCALE_PLACEMENT=host`, no Tailscale checks and no `serve` mappings. The `ccboard` MCP server is registered with Claude Code (and Codex) by the board's own venv python, `<checkout>/.venv/bin/python`, exactly as on Ubuntu: the distro's `/usr/bin/python3` is not used for it (only the docker runtime does, because Claude starts the shim on the host). A Claude registration that already exists is kept as it is, not added twice. On the Windows side, only the keep-alive script changes anything: it registers the `ccboard-wsl-keepalive` Scheduled Task, and with `-WriteWslConfig` edits `%UserProfile%\.wslconfig` after a backup copy. `-Remove` undoes the task.

## Next steps

- [Your first ten minutes](../../README.md#your-first-ten-minutes) on the board.
- [Configuration](../configuration.md), [Upgrade](../upgrade.md), [Backup and restore](../backup-and-restore.md), [Security](../security.md) and [Troubleshooting](../troubleshooting.md).

## Evidence and open checks

Not run on a real Windows machine (issue #130, W1 to W9). Open:

- Whether `[boot] systemd` works on Windows 10 is UNVERIFIED.
- That `-Method sleep` holds the distro on every WSL version is to verify. Field reports say `instanceIdleTimeout=-1` and `vmIdleTimeout=-1` alone do not stop the distro after the last terminal closes (WSL discussion 9245), and that WSL 2.6.1.0 stops an instance 15 to 20 seconds after the last terminal closes even with an active unit (WSL issue 13416); the task is what holds the distro. Behaviour on WSL 3.x is UNVERIFIED.
- Whether WSL can start before logon to Windows is UNVERIFIED.
- That the Windows host reaches a port of the distro on its own `127.0.0.1` is UNVERIFIED until the device check.
- Calling `tailscale.exe` through Windows interop from the distro is UNVERIFIED.
- The default value of `CCBOARD_TAILSCALE_PLACEMENT` may change after the device check.
- Microsoft's documented idle timeouts (15000 ms and 60000 ms) and its statement that systemd services do not keep an instance alive are from its documentation, not from a run here.
- **Background.** Issue #118 (WSL2 support), #124 (native Windows is not supported), #127 (local sign-in) and #130 (the device check, W1 to W9).
