# Install on a Mac

For people running ccboard on a Mac.

[Back to the documentation index](../README.md)

### macOS tools (issue #128)

On a Mac the tools come from Homebrew, listed in two files under `scripts/macos/`: `Brewfile` (required) and `Brewfile.optional`. Neither holds a cask, and nothing here runs `sudo`. The macOS installer first asks Homebrew (`brew info`) whether each missing tool has a bottle for this Mac. For bottles it asks once before it runs `brew bundle --no-upgrade` (without a terminal it stops and says what is missing, unless `CCBOARD_MACOS_INSTALL_TOOLS=1`, which is a yes for bottles only). A tool Homebrew would build from source is never built silently: on a terminal the installer asks "Homebrew will BUILD from source: ttyd. That takes several minutes and may upgrade packages you have. Build them? [y/N] ", and without one it builds only when `CCBOARD_MACOS_INSTALL_TOOLS=1` and `CCBOARD_MACOS_ALLOW_SOURCE_BUILD=1` are both set (an optional tool that would build from source is left out with a warning). If the Xcode Command Line Tools are missing and something would build from source, it stops before installing anything with "the Xcode Command Line Tools are not installed, and these would be built from source: ttyd. Nothing was installed." and tells you to run `xcode-select --install`, then installs the optional tools named in `CCBOARD_MACOS_OPTIONAL` (default `code-server,restic,bun`; `none` skips them); `brew bundle --file=scripts/macos/Brewfile` installs the required set by hand. The table is the same list as the files (a test keeps them equal).

| Tool | Homebrew formula | Needed | Intel Mac | Apple silicon | If it is missing (the Doctor says) |
|---|---|---|---|---|---|
| git | `git` | required | bottle (to verify) | bottle (to verify) | "git is not installed", with the install command |
| tmux | `tmux` | required | no Intel bottle: builds from source | bottles on current macOS | "tmux is not installed" (3.2 or newer is needed) |
| Python 3.12 | `python@3.12` | required | bottle (to verify) | bottle (to verify) | no check: the installer builds the venv with it |
| Node | `node` | required | bottle (to verify) | bottle (to verify) | no check; `ccusage` is skipped with a warning |
| GitHub CLI | `gh` | required | bottle (to verify) | bottle (to verify) | "gh is not installed", with the install command |
| ttyd | `ttyd` | required | no bottle: a source build of many minutes that may upgrade other packages (measured 2026-10-10 on an Intel Mac, macOS 14, see the Intel bullet) | bottles on Sequoia and Tahoe, source on Sonoma | "ttyd is not installed and nothing listens on its port"; it also needs version 1.7.4 or newer with `-W`, `-O` and `-a` |
| code-server | `code-server` | optional | bottle (to verify) | bottle (to verify) | "nothing listens on 127.0.0.1:<port>", with the restart command |
| restic | `restic` | optional | bottle (to verify) | bottle (to verify) | the [Backup section](../backup-and-restore.md#backup-on-each-system-issue-129) reports it (wording to verify) |
| bun | `oven-sh/bun/bun` (tap `oven-sh/bun`) | optional | to verify | to verify | "bun not found and no worker is running, so hooks cannot start one" (claude-mem) |
| shellcheck | `shellcheck` | optional | bottle (to verify) | bottle (to verify) | not checked: only people who change the scripts need it |

- **Intel.** Homebrew treats Intel Macs as Tier 3 (problems may be closed without investigation) and stops supporting them entirely from about September 2027. That is a date to plan around, not a reason to refuse an Intel Mac today; tmux and ttyd are built from source there and that takes many minutes (ttyd more than ten, see the next bullet; the Xcode Command Line Tools are needed for it: `xcode-select --install`). `scripts/macos_tools.py bottle` reads `brew info --json=v2` and says "bottle available" or "builds from source" for this macOS and processor, and says "could not tell" rather than guess.
- **ttyd on an Intel Mac is a long source build with side effects (measured 2026-10-10 on an Intel Mac, macOS 14, Homebrew 7.0.5, issue #130 M8).** There is no bottle: Homebrew printed "Homebrew no longer builds bottles for this configuration" and planned to build ttyd 1.7.7 with four new dependencies and to upgrade 11 packages that were already installed (ca-certificates, openssl@3, libevent, cmake, readline, sqlite, xz, pkgconf, expat, python@3.14 and libuv). cmake alone was still compiling after 10 minutes 38 seconds, when the run was stopped by hand; nothing was installed. `brew bundle --no-upgrade` does not prevent the upgrades: Homebrew's help says it skips the check for outdated dependencies and that they "may still be upgraded by brew install if needed". Plan for a long wait and for a changed Python, or use an Apple silicon Mac on macOS 15 or newer, where Homebrew lists a ttyd bottle (not run here). The installer says this before it builds and never builds without a yes.
- **ttyd.** The upstream release has no macOS binary. After the install, `scripts/macos_tools.py ttyd --bin <path to ttyd>` checks the version and that `--help` lists `-W`, `-O` and `-a`, and names the missing flag otherwise (ttyd 1.6.3 has no `-W`). The terminal job listens on `lo0`, the Mac's loopback interface name; whether the address `127.0.0.1` works for `-i` as well is to verify, and so is every flag above: the measured install was stopped before ttyd existed (issue #130, M8).
- **code-server** runs with `auth: none` on `127.0.0.1`, as on Linux, so any local user of the Mac can open the editor. Its `config.yaml` is the same text `install.sh` writes on Linux (`scripts/macos/code-server-config.yaml.in`, marked `# managed by ccboard`); Homebrew cannot pin a version, so a version different from the pinned one is a warning, not an error. The link built from a macOS home path (`?folder=<home>/<project>`) is a plain posix path and opens the folder.
- **ntfy is not installed on a Mac.** Push to phones works through Web Push without it; to use ntfy anyway, set `NTFY_URL` to a server you already run (and `NTFY_PUBLIC_URL` for the phone).
- **Not in the Brewfiles on purpose:** Tailscale (the App Store, Standalone and open-source variants differ; you choose), Claude Code (its native installer), Codex (`brew install --cask codex`, npm or its install script; the installer never installs it).
- **Jobs.** Each service is a launchd job rendered from `launchd/dev.ccboard.<job>.plist.in` (`board`, `tmux`, `ttyd`, `awake`, `code-server`, `mem`) by `scripts/launchd_render.py`, which fills the `__NAME__` tokens with XML-escaped values, checks the result with plistlib and refuses a `TMUX_TMPDIR` that would make the tmux socket path longer than 100 bytes (the limit is 103 bytes on macOS, measured 2026-10-10 on an Intel Mac, macOS 14: a 104-byte path fails with "File name too long"; the board keeps three bytes in hand). Logs are `~/Library/Logs/ccboard/<job>.log`.

## macOS (issue #117)

**Local browser sign-in (issue #127).** A request from the Mac to its own tailnet address received Tailscale identity in the issue's device check (using curl; a real browser was not run). For a Mac without Tailscale, or plain localhost, put `CCBOARD_LOCAL_SIGNIN=1` in the board's settings file and restart the board. Run `bin/ccboard-open` from the checkout (set `CCBOARD_PORT` and `CCBOARD_DATA_DIR` if non-default), or `.venv/bin/python -m app.config open`, which reads the board's settings file. The command opens a single-use link in the default browser; if no opener works it prints the link. Local sign-in starts off on every runtime.

The board runs natively on a Mac as per-user launchd jobs, for a person who is logged in. `./install.sh` on a Mac hands over to `scripts/install-macos.sh`; Linux behaves exactly as before (the dispatch is one line in `install.sh`).

**Requirements:** macOS 13 or newer, [Homebrew](https://brew.sh), and Tailscale with MagicDNS and HTTPS certificates enabled (see [Tailscale on each system](tailscale.md#tailscale-on-each-system-issue-126) for the three variants). The tools come from Homebrew ([macOS tools](#macos-tools-issue-128)). 

**Install.** From a terminal, as yourself (the installer never uses `sudo` and refuses to run as root):

```sh
git clone https://github.com/rpandox/ccboard && cd ccboard
./install.sh                        # or: CCBOARD_RUNTIME=launchd ./install.sh, or ./install.sh --runtime launchd
CCBOARD_HTTPS_PORT=8443 CODE_HTTPS_PORT=10000 ./install.sh      # settings are environment variables, as on Linux
```

It runs under the stock bash 3.2 of macOS and, in order: checks the Mac (not root, macOS 13+, Homebrew) and every setting; checks Tailscale and the `tailscale serve` ports before changing anything; installs missing Homebrew tools (it asks once, or set `CCBOARD_MACOS_INSTALL_TOOLS=1`; `CCBOARD_MACOS_OPTIONAL=none` skips the optional ones); builds `.venv` from `requirements.txt`; writes `<data dir>/env` (mode 0600, previous values remembered, a value you pass wins); renders and loads the launchd jobs; merges the Claude and Codex hooks; registers the MCP server with Claude under the board's own venv python (`.venv/bin/python`, never the system `python3`); maps the board, `/tty` and code-server with `tailscale serve`; waits for `/healthz`; and prints the URLs, the restart command, the log folder and the update command. `CCBOARD_RUNTIME=docker` and `systemd` are refused on a Mac, with the reason. Switches that apply to the jobs: `CCBOARD_KEEP_AWAKE=1` (`dev.ccboard.awake`), `CCBOARD_MEM_SERVICE=1` (`dev.ccboard.mem`), `CCBOARD_BACKUP=0` (no `dev.ccboard.backup`; otherwise it runs at `CCBOARD_BACKUP_ONCALENDAR`, as `*-*-* HH:MM[:SS]` or `HH:MM`), `CCBOARD_LAUNCHD_DOMAIN=user` (experimental).

**A rerun** changes nothing it should not: a plist that renders the same is neither rewritten nor reloaded, the board restarts (`kickstart -k`) only when the checkout, the requirements or the settings file changed, a changed plist of the board, ttyd or an optional job is booted out and in again, and `dev.ccboard.tmux` is never booted out or kickstarted once it runs (a changed tmux plist only prints a warning, because reloading it would end every session). A job you switched off is booted out and its plist removed.

**What the macOS installer leaves to you:** installing Claude Code and Codex (it prints how when one is missing), ntfy (see the tools section), and the claude-mem viewer mapping on the tailnet (`CCBOARD_MEM_HTTPS_PORT` is remembered but not mapped yet).

**The claude-mem plugin (issue #122).** When `claude` is installed the installer reads `<Claude config dir>/plugins/installed_plugins.json`: if `claude-mem@thedotmack` is there it prints "present: claude-mem@thedotmack" and does nothing, so a rerun makes no plugin call. Otherwise it runs `claude plugin marketplace add thedotmack/claude-mem` and then `claude plugin install claude-mem@thedotmack`, each with no input and at most 300 seconds (`CCBOARD_MACOS_PLUGIN_SECONDS`, a whole number of 1 or more; `gtimeout` when it exists, else a small python wrapper that ends the whole process group). It needs github.com. Every outcome continues and nothing makes the installer fail: an error from the add is noted and the install goes on; a time-out of the add skips the install; a failed or timed-out install, or one that leaves no entry in `installed_plugins.json`, prints a warning with the command to run by hand. `CCBOARD_CLAUDE_MEM=0` skips the step ("skipped (CCBOARD_CLAUDE_MEM=0)"); without `claude` it prints the two commands and calls nothing. Claude sessions that are already open load the plugin's hooks when they next start. Not yet run with a real `claude plugin install` on a Mac.

**The jobs.** Three always (`dev.ccboard.board`, `dev.ccboard.tmux`, `dev.ccboard.ttyd`), three optional (`awake`, `code-server`, `mem`). They are LaunchAgents of the `gui/<uid>` domain, so they belong to the logged-in user's session and can reach that user's Keychain, Homebrew tools and `~/.local/bin`. `CCBOARD_LAUNCHD_DOMAIN=user` selects the experimental Background-session layout (`user/<uid>`); it is labelled experimental until a Mac proves it, see the table. Logs: `~/Library/Logs/ccboard/<job>.log` (stdout and stderr; launchd never rotates them, so the Doctor warns over 50 MB).

**Daily commands** (the Doctor prints these as its fix texts on a Mac):

| To | Run |
|---|---|
| restart the board (sessions and ttyd untouched) | `launchctl kickstart -k gui/$(id -u)/dev.ccboard.board` |
| look at a job | `launchctl print gui/$(id -u)/dev.ccboard.board` (also `.tmux`, `.ttyd`) |
| read a log | `tail -n 30 ~/Library/Logs/ccboard/board.log` (also `tmux.log`, `ttyd.log`) |
| stop the board, keep the sessions | `launchctl bootout gui/$(id -u)/dev.ccboard.board` |
| start it again | `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.ccboard.board.plist` |
| update | `git pull`, then rerun `./install.sh` |
| uninstall | `scripts/uninstall-macos.sh` (asks first; `--yes` skips the question, `--keep-tmux` leaves the sessions running). It boots out and removes every `dev.ccboard.*` job, the tmux job last, and keeps the data folder, the settings file, the logs, the hook registrations and the `tailscale serve` mappings, saying how to remove each |

Never restart or boot out `dev.ccboard.tmux` while sessions run: it is the tmux server that owns them, the same rule as `ccboard-tmux.service` on Linux. Measured (measured 2026-10-10 on an Intel Mac, macOS 14, tmux 3.6b, M5 and M6): `bootout` of the tmux job kills the server and every session, and the socket file stays behind as a stale file, as it did after most `tmux kill-server` runs. A socket file that exists therefore does not mean a server is alive: ask tmux (`tmux -L ccboard list-sessions`, which says "no server running on <path>"), never test for the file. tmux started under launchd with `-D` takes no command and stays in the foreground, which is how the job holds it.

**The settings file.** launchd has no `EnvironmentFile`, so the board reads `<data dir>/env` itself when it starts (`CCBOARD_ENV_FILE` names another file, and an empty `CCBOARD_ENV_FILE` turns the file off). The lines are `KEY=value` data, never run by a shell (`X=$(touch y)` is the text `$(touch y)`), only the keys `install.sh` accepts for `/etc/ccboard/env` are read (`python -m app.config keys` lists them), and a value set in the environment wins over the file. The file holds the hub token, so the board refuses it, and says why in its log, unless it is a regular file owned by you with no group or world permission (`chmod 600`). On Linux the board never reads it by default (the data dir is writable by the board's user and its sessions; settings stay in root-owned `/etc/ccboard/env` or the container's environment); only a `CCBOARD_ENV_FILE` set in the service's own environment names a file there.

**Finding your tools.** A LaunchAgent starts with `PATH=/usr/bin:/bin:/usr/sbin:/sbin`, so Homebrew and `~/.local/bin` would be invisible. The installer puts the Homebrew folders and `~/.local/bin` on each job's PATH, and the board looks for `claude` and `codex` in this order: `PATH`, `~/.local/bin`, what your login shell finds (`$SHELL -lic 'command -v claude'`, so an nvm or asdf install is found; the answer is kept for 10 minutes, a miss for 5, and the shell gets 3 seconds), then `/opt/homebrew/bin` and `/usr/local/bin`. Where `PATH` or `~/.local/bin` has the program, no shell is started, which is also what Linux always did.

**What survives what.** The default is the supported setup: a Mac that stays logged in (a locked screen is fine). A Mac that must come back by itself after a reboot is not covered by the default mode.

| Event | The jobs and the sessions | Status |
|---|---|---|
| Restart the board job (`kickstart -k`) | the board restarts; tmux sessions and ttyd are untouched (they are separate jobs) | measured for an unrelated job: sessions survived `kickstart -k` and `bootout` of other jobs (measured 2026-10-10 on an Intel Mac, macOS 14, M5); the board job itself: to verify |
| Boot the board job out and in again | sessions keep running; the board comes back | by design; to verify on a Mac |
| Screen locked | everything keeps running | expected; to verify |
| Idle sleep, no `caffeinate` | the Mac sleeps and the board is unreachable until it wakes | expected; whether sessions resume intact is to verify |
| Idle sleep with `dev.ccboard.awake` (`caffeinate -i`) | the Mac stays awake | expected; to verify |
| Lid closed on battery | the Mac sleeps | to verify (issue #130, M11: not run) |
| Log out | a `gui/<uid>` LaunchAgent gets SIGTERM, so the jobs stop and every tmux session and agent in it is lost | from launchd's documented behaviour; to verify (M4: not run) |
| Reboot | nothing starts until someone logs in (FileVault disables automatic login); the sessions are gone either way | from Apple's documentation; to verify (M4: not run) |
| Log in after a reboot | the jobs start (`RunAtLoad`); the login Keychain is unlocked | expected; to verify |
| Background layout (`CCBOARD_LAUNCHD_DOMAIN=user`) outliving a logout | a mailing-list post and a forum answer say it does, no Apple page does | to verify (M4: not run) |
| A Background job reaching the Keychain | an attribute-only lookup (`security find-generic-password -s "Claude Code-credentials"` without `-w` or `-g`) returned at once, with no dialog, from a normal shell and from a `gui/<uid>` job and a `user/<uid>` Background job (measured 2026-10-10 on an Intel Mac, macOS 14, M17) | reading the secret itself from a Background job: to verify |
| Tailscale before login | the App Store and Standalone variants cannot run before login; only the open-source `tailscaled` can | from Tailscale's documentation |

**Intel and Apple silicon.** Homebrew's prefix is `/usr/local` on Intel and `/opt/homebrew` on Apple silicon; both are searched. Homebrew treats Intel as Tier 3 and ends Intel support around September 2027, and tmux and ttyd build from source there; ttyd took more than ten minutes before the measured run was stopped (see [macOS tools](#macos-tools-issue-128)). The device check (issue #130) has one run, on an Intel Mac with macOS 14 (2026-10-10); there is none on Apple silicon yet, and this section is corrected to match what the runs show.

**Privacy folders.** `PROJECTS_DIR` defaults to `~/projects` because `/srv` cannot be made on a Mac. Under `~/Desktop`, `~/Documents`, `~/Downloads` or iCloud Drive macOS asks the process for a Files and Folders grant, and a Mac with nobody at the screen cannot answer; the Doctor warns, and fails when macOS refuses the listing. Move the folder, or grant access in System Settings > Privacy & Security > Files and Folders.

**Case.** APFS ignores letter case by default, so `Foo` and `foo` are one folder. When the volume holding `PROJECTS_DIR` does, the board refuses a new project or repo name that differs from an existing entry only by case, and the message names the entry.

**Doctor checks on a Mac** (group box; they exist only on macOS): launchd jobs loaded and running; logout and reboot (FileVault, automatic login, the job domain); the tools the board finds (`claude`, `codex`, `tmux`, `git`, `gh`); the privacy folders; name case; sleep (`pmset -g assertions`); the log folder size. Off a Mac, and where the board is not a launchd job, a check says so and skips.

**Not supported on a Mac:** the container runtime (Docker Desktop runs containers in a virtual machine that cannot reach the Mac's tmux and processes, see the support table in [the container section](docker.md#where-the-container-runtime-works)), saved Claude logins (account switching) until issue #119 lands, and native Windows (issue #124).
