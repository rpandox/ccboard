# Memory with claude-mem

For people who run the claude-mem plugin beside the board.

[Back to the documentation index](../README.md)

In this page, "the box" means the machine that runs the board.

## claude-mem

[claude-mem](https://github.com/thedotmack/claude-mem) is a Claude Code plugin (`claude-mem@thedotmack`) that records what your sessions do and feeds it back to later ones. It runs one **worker** per box: an HTTP service on loopback (port from `CLAUDE_MEM_WORKER_PORT` in `~/.claude-mem/settings.json`, recorded in `~/.claude-mem/worker.pid`) that spawns an observer `claude` session per observed session. The board reads its health, and the Memory page reads a project's memory through the board (never from the browser). The only write is the optional write-back below, which is off by default.

**The install step.** `install.sh` looks for the plugin's `claude-mem@thedotmack` key in `~/.claude/plugins/installed_plugins.json`. When it is missing it prints and runs

```sh
claude plugin marketplace add thedotmack/claude-mem
claude plugin install claude-mem@thedotmack
```

(the second runs even if the first answers that the marketplace is already added) and goes on whatever happens: a failure is a warning, never an abort. It warns when the plugin is installed but disabled (`claude plugin enable claude-mem@thedotmack`) and when no `bun` can be found. The plugin's worker runs on bun. In 13.29.0 the plugin only looks for it (`bun-runner.js` and `version-check.js` search `PATH`, `BUN`, `BUN_PATH`, `BUN_INSTALL`, `~/.bun/bin`, `/usr/local/bin` and a few more; nothing in its hooks installs it), and `install.sh` does not install it either. `CCBOARD_CLAUDE_MEM=0` skips the step. Nothing here touches Codex (that comes with the Codex adapter). Docker mode needs nothing extra: the worker is a host process, the container shares the host network, and `docker-entrypoint.sh` never installs the unit.

**Why a service.** A worker is started by the plugin's SessionStart hook, so it is a child of whichever Claude session ran that hook first, and it inherits that session's environment. The plugin strips `CLAUDECODE` and `CLAUDE_CODE_*` when it spawns, but `CCBOARD_SESSION`, `CCBOARD_URL` and `TMUX_PANE` pass through. Every observer session the worker starts then runs the box's hooks as if it were that board session (seen on the box: its events landed on the user's row; v0.5.7 ignores child-session events, the service removes the cause). The doctor's *memory* group warns while a worker's environment still carries `CCBOARD_SESSION` or `TMUX_PANE`. A board in the container cannot read the host worker's `/proc/<pid>/environ`, so the host watchdog (the same crontab line, every 2 minutes) runs `scripts/ccboard-mem-env`, which leaves the worker's pid and the NAMES of its `CCBOARD_*`, `TMUX`, `TMUX_PANE`, `CLAUDECODE` and `CLAUDE_CODE_*` variables in `<data dir>/mem-env.json` (mode 0600); the check uses that report while it is under ten minutes old and about the same pid, and says "the host watchdog has not reported" otherwise. Values are never read into the file.

**On a Mac.** The board finds the worker the same way. Before the first start the port is the plugin's default, `37700+(uid%100)`, so uid 501 gives 37701 (checked against worker 13.34.2 on a Mac; the plugin falls back to 77, port 37777, only when the system has no uid). The leak check above runs there too: the board reads the worker's environment through `psutil` (installed with the board's requirements off Linux; same-user processes only, measured 2026-10-10 on an Intel Mac, macOS 14: another user's process raises AccessDenied, and so does the system-wide `psutil.net_connections()`, which the board never calls, asking each process for its own sockets or `lsof` instead), names only, and a process it may not read leaves the check on *skip*. Everything the board reads from claude-mem is the worker on that same computer: a Mac's observations are not visible on another board, and another computer's are not visible on the Mac's.

**The worker as a launchd job on a Mac.** `CCBOARD_MEM_SERVICE=1 ./install.sh` installs `dev.ccboard.mem`, the Mac counterpart of `ccboard-mem.service` (off by default, for the same reason: two workers must never race on `worker.pid`). It runs `bin/ccboard-mem-run` from an environment of exactly `HOME`, `LANG` and `PATH`, so no `CCBOARD_SESSION` or `TMUX_PANE` can reach the worker, and it does not read the settings file. `CCBOARD_MEM_SERVICE=0 ./install.sh` removes it, and so does `scripts/uninstall-macos.sh`. A failure to load it is a warning. Log: `~/Library/Logs/ccboard/mem.log`. Details, and what is not yet checked on a real Mac, are on the [macOS page](../install/macos.md#what-the-installer-changes).

**In WSL2.** WSL2 is Linux: the port rule, the leak check (`/proc`) and the unit all work as on a Linux box, inside the distro.

**`ccboard-mem.service`** (off by default: the hooks start a worker on demand today, and two workers must never race on `worker.pid`). Turn it on with `CCBOARD_MEM_SERVICE=1 ./install.sh`; turn it off with `CCBOARD_MEM_SERVICE=0 ./install.sh` (the unit file stays, the hooks start workers again). It is a system unit running as you, with `Restart=on-failure`, `Nice=5`, best-effort I/O priority 7 (not idle: the observer has to keep up) and `OOMScoreAdjust=200`. It is ordered `Before=ccboard-tmux.service ccboard.service` (ordering only, no `Requires`, so a failed or disabled unit blocks nothing), so after a boot the worker is up before the sessions the board recovers; the order is not a guarantee, and the doctor's `memory-env` check is the safety net: it warns while the worker holds `CCBOARD_SESSION` or `TMUX_PANE`, i.e. when a session's hook won the race and started a worker first. It does **not** read `/etc/ccboard/env` (that file holds the hub token and every `CCBOARD_*` setting). `ExecStart` is

```
/usr/bin/env -u CCBOARD_SESSION -u CCBOARD_URL -u CCBOARD_APPROVE_TIMEOUT -u TMUX -u TMUX_PANE -u CLAUDECODE -u CLAUDE_CODE_CHILD_SESSION  <checkout>/bin/ccboard-mem-run
```

(`<data dir>/app/bin/ccboard-mem-run` in docker mode, where `install.sh` copies the launcher next to the other host-side scripts), and `bin/ccboard-mem-run` resolves, on every start, the newest `~/.claude/plugins/cache/thedotmack/claude-mem/<version>/` that has no `.orphaned_at` file (Claude Code replaces that directory on every plugin update) and bun, drops every other `CCBOARD_*` variable, and execs

```
<bun> ~/.claude/plugins/cache/thedotmack/claude-mem/<version>/scripts/worker-service.cjs --daemon
```

That is the plugin's own start, in the foreground: `worker-service.cjs start` only spawns `setsid <bun> worker-service.cjs --daemon` detached and exits (useless under `Type=simple`), and `bun-runner.js worker-service.cjs --daemon` is wrong because bun-runner treats any argument other than `start|stop|restart|status` as a hook call and, with no stdin payload, writes `~/.claude-mem/CAPTURE_BROKEN` and kills its child. A second `--daemon` while one is healthy exits 0 without doing anything, so the unit then sits inactive and `Restart=on-failure` leaves it alone; a boot failure exits 78 and is retried every 5 s until the start limit (5 starts in 5 minutes) leaves the unit `failed`; fix the cause, then `sudo systemctl reset-failed ccboard-mem && sudo systemctl start ccboard-mem`. `ccboard-mem-run --print` shows what it resolved (exit 78 when the plugin or bun is missing). Override with `sudo systemctl edit ccboard-mem` (`Environment=CCBOARD_MEM_BUN=/path/to/bun` or `CCBOARD_MEM_PLUGIN_DIR=...`). The worker's port is not forced from here: the hooks read it from `~/.claude-mem/settings.json`, so change it there.

**Handing over from a hook-started worker.** If one is running when you enable the unit, `install.sh` enables it for the next boot, starts nothing, and prints these steps plus the `CLAUDE_MEM_WORKER_AUTOSTART` note below (do them when no session is mid-turn; the observer's queue is kept in claude-mem's database):

```sh
<bun> <plugin>/scripts/worker-service.cjs stop       # the plugin's own stop
sudo systemctl start ccboard-mem
# verify: the unit owns the worker, and its environment is clean (expect 0)
systemctl show -p MainPID --value ccboard-mem
tr '\0' '\n' < /proc/$(systemctl show -p MainPID --value ccboard-mem)/environ | grep -c '^CCBOARD_SESSION='
curl -s "http://127.0.0.1:$(python3 -c 'import json,os;print(json.load(open(os.path.expanduser("~/.claude-mem/worker.pid")))["port"])')/health"
```

If a hook wins the race after the `stop` and starts a worker first, the unit's start exits at once: repeat both commands.

**Day to day and plugin updates.** Restart the worker with `sudo systemctl restart ccboard-mem`, not with the plugin's `restart` (it hands the worker over to a process it spawns itself, outside the unit's supervision). Logs: `journalctl -u ccboard-mem`, plus `~/.claude-mem/logs/`. The unit is not in `/etc/sudoers.d/ccboard`.

A plugin update is the one thing that undoes the clean environment, and the old worker does not keep running until you restart it. Per the plugin's code (13.29.0, `worker-service.cjs`), the first hook of any Claude session after an update compares the running worker's version with the installed plugin's, logs `Worker version mismatch`, kills the unit's worker and spawns a replacement itself, with that hook's environment: `CCBOARD_SESSION` and `TMUX_PANE` are back. The unit's next start (its own `Restart=on-failure` after 5 s, or your `systemctl restart`) then runs a second `--daemon` that finds the hook's worker healthy and exits 0, so the unit sits inactive and the leaky worker stays. The doctor's `memory-env` check warns when that has happened. Recover as in the hand-over above: `<bun> <plugin>/scripts/worker-service.cjs stop`, then `sudo systemctl start ccboard-mem` (the launcher re-resolves the newest plugin version on every start).

**Optional, for boxes that run the unit: `CLAUDE_MEM_WORKER_AUTOSTART=false`.** The plugin has a switch for a worker that something else manages: the key `CLAUDE_MEM_WORKER_AUTOSTART` in `~/.claude-mem/settings.json` (default `"true"`; the string `"false"`, in any case, turns it off. Write it quoted: the plugin calls `.trim()` on the value as read, and a JSON boolean has no such method). Per the plugin's code (13.29.0), with it set the hooks never lazy-spawn a worker, never recycle a worker whose version differs from the plugin's (they log `using it as is`) and never recycle a wedged one, and the plugin's own `worker-service.cjs start` does not launch one either; `--daemon`, which is what the unit runs, and `stop` are not affected. So the unit becomes the only thing that starts a worker, and after a plugin update `sudo systemctl restart ccboard-mem` is the whole procedure. The trade-off is the mirror image: with the setting on, nothing starts a worker unless the unit runs, so while the unit is failed, inactive or stopped there is no memory capture at all, and the doctor's *claude-mem worker* check (`memory-worker`, *no worker on 127.0.0.1:...*) is what tells you. It is your decision and ccboard never writes it (`install.sh` only prints this hint after enabling the unit). To opt in, add this line yourself inside the top-level object of `~/.claude-mem/settings.json` (a fragment, not a file; mind the comma next to its neighbours):

```json
  "CLAUDE_MEM_WORKER_AUTOSTART": "false",
```

**Open a claude-mem dashboard.** To point the board at a central dashboard that already exists, set `CCBOARD_MEM_VIEWER_URL=https://memory.example.ts.net:8446/`, for example in Settings > Options, then restart the board. The Memory page’s "Open claude-mem viewer" link opens that URL in a new tab. The dashboard controls access; the board creates no port mapping and does not forward its sign-in. URLs must use HTTPS (HTTP is allowed only on loopback) and contain no credentials. This changes only the link; the Memory page still reads its local worker.

To expose the **local worker’s viewer**, leave `CCBOARD_MEM_VIEWER_URL` empty and set `CCBOARD_MEM_HTTPS_PORT` to a free port with `./install.sh`. The link then uses the board’s public host and that port. The worker has no sign-in of its own: every device on your tailnet can open and change the claude-mem worker; restrict the port with a Tailscale ACL if needed. The explicit dashboard URL wins when both settings are set, but does not close an existing mapping; use `CCBOARD_MEM_HTTPS_PORT=off ./install.sh` to remove it. With both settings empty there is no viewer link.

**The Memory proxy (v0.5.20).** `GET /api/memory/health` (add `?refresh=1` for one live probe, at most 3.5 s) and `GET /api/memory/<project>/observations|summaries|search|timeline|palace` answer from the claude-mem worker. The board calls the worker on loopback with GET only, so the browser never reaches it. A project's keys are its repo folder names, the project folder's own name, and `<repo>/<slug>` for a Claude or ccboard worktree (a worktree folder named like its repo shows as `<repo>`). Names from `CLAUDE_MEM_PROJECT_ENVIRONMENTS` win. Keys the worker does not list are dropped, so noise keys never show. Lists are newest first, at most 100 rows a page, and `before` is the cursor (the answer gives `next_before`). Search, palace and the other routes get a 6 s budget, one worker call at most 5 s: the first, cold read of a project on the box's spinning disk took over 2 s, a warm one a few ms. Every answer carries a `state`:

- `up`: fresh data.
- `stale`: the worker failed, so the last good answer is served with `stale: true` and `stale_at`. Errors are never cached.
- `partial`: some keys did not answer; the answer names them.
- `ambiguous`: another project has a repo with the same folder name, so rows are filtered by file path where they name one (best effort).
- `incompatible`: the worker answered in a shape the board does not know. The lists are empty and `compat` says why; the page never shows that as "no memory".
- `down` (503): the worker is not listening (connection refused) and it was running before (a stale `worker.pid`), or `CCBOARD_MEM_PORT` points at nothing. The body is `{error, state, up: false, reason, reason_code, compat, worker_version, tested_worker}`.
- `not_started` (503): connection refused and there is no `worker.pid`, so no Claude session has started a worker on this box yet. Nothing is wrong; the Memory page shows it in a neutral colour with a New session button, and the Doctor passes. The body has the same fields as `down`.
- `degraded` (503): the worker is slow, answered an HTTP error, sent an answer that is too big, or reported an error. A timeout reads as "slow", never "down", because a broad search can take over 15 s on the box while the worker is fine.

The routes, fields and states are in [docs/memory-api.md](../memory-api.md).

**The Memory page** (`#/memory[/<project>]`, v0.5.20). A project select, the worker pill (`up v13.31.0 · N obs`, `degraded`, `down` or `stale HH:MM`, always with the real reason), a search box (debounced, kept in `?q=`) and four tabs: Palace (a card per repo with room chips, a stacked bar of types with its counts as text, and drawers that open in place), Timeline (newest first, day headers, Load more; a row's "Send to session..." menu offers the project's idle sessions and only pastes after a confirm step), Summaries and Search. Filters (type, repo, agent, session, range) are kept per project in the browser and mirrored in the address, so a link reproduces the view. "N new, Load" appears when the worker's total grows; it adds no polling. A health tile (state, observations and summaries per day from the board's own samples, observer queue, active sessions with an estimate of stale ones, last provider error) sits under the header, on the Usage page and in Settings > Box. The project page has the same Palace as its Memory tab and shows the project's three newest gotchas in its header and in the new-session sheet (asked after the sheet painted; a stopped or slow worker shows nothing). Everything claude-mem wrote is shown as text, never as markup. Try it without the box: `#/memory/ccboard?demo=1` (add `&mem=down`, `degraded`, `stale`, `partial`, `ambiguous`, `incompatible` or `untested` for a state).

**After a claude-mem update.** The proxy was verified against worker 13.35.0 (`TESTED_WORKER` in `app/memory_proxy.py`; box check of 2026-10-09): a read-only check of the running worker on the box (stats, projects, observations, summaries, a JSON search with one key and with a list of keys, a filter-only search and a timeline) passed the proxy's shape checks, and its scrubbed answers are in `tests/fixtures/claude_mem_13_35/` (the older `tests/fixtures/claude_mem_*.json` are the 13.31.0 ones). The first calls after a worker restart are slow (a cold search took 26 s against the board's 6 s budget), so for a while after every worker restart or plugin update the Memory page can show *degraded: the worker is slow*; it clears by itself once the worker is warm. With a newer worker, the proxy's answers carry `compat: "untested"` and the doctor's `memory-api` check warns. The Memory page shows this as a quiet line. An answer in a shape the board does not know comes back as `state: "incompatible"` with one line of reason, never as an empty list. After a plugin update, check the live worker's shapes on the box (GET only; skipped unless the variable is set):

```sh
CCBOARD_TEST_CLAUDE_MEM=1 .venv/bin/pytest -q tests/test_memory_contract.py -k live
```

**Write-back (off by default).** Settings > Box > claude-mem has a switch: "Save each finished task's result to claude-mem." When it is on and a task finishes as done with a result, the board sends one note. The note is titled `Task: <title>`, carries the result (at most 8 KB, cut at a line break) and is filed under the repo's key, so it lands in the same wing of the palace. The prompt is never sent. A stopped worker has no effect on tasks. Results can contain anything the agent printed, which is why the switch is off by default. The switch is stored on the box and read with `GET /api/memory/prefs`; `PUT /api/memory/prefs` needs the `X-CCBoard: 1` header.
