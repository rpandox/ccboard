# Configuration reference

For people changing how the board starts: every setting, the page overrides and the restart button.

[Back to the documentation index](README.md)

In this page, "the box" means the machine that runs the board.

### Settings

Startup settings combine environment variables with optional page overrides. The installer remembers values in `/etc/ccboard/env` on Linux and the board data folder's `env` file on macOS; a non-empty environment value wins when the board loads the file.

**Settings page.** Settings > Options lists the option registry in nine groups with the current parsed value, default, source and restart requirement. It loads when opened; `/api/state` carries only the pending count, keys and restart hint. Filter by key or label, or open `#/settings?sec=options&group=epics`. Secret values stay hidden, URL credentials are masked and unrelated file lines are only counted. The existing remote MCP page switch still takes precedence over its startup default.

The class in each table note describes the intended editing policy: `page` for ordinary page settings, `confirm` for settings needing acknowledgement, `file` for file-only settings, and `installer` for settings that also require `install.sh`. Only `page` options save directly. `confirm` options are editable when the file-only `CCBOARD_PAGE_WIDEN=1` switch is on; widening requires the exact typed phrase. `file` and `installer` remain read only. Copy lines for secrets contain a placeholder. Empty defaults use the reader's automatic behavior where supported; the Options defaults describe the board without installer values, so generated URLs, tokens and names may differ from this installer's defaults below. Restart instructions follow the platform.

**How to set one.** Pass it as an environment variable when you run the installer, for example `CCBOARD_HTTPS_PORT=8443 ./install.sh`. The installer remembers it, and a value you pass wins over a remembered one. The tables below are grouped by job, each key is in exactly one of them, and the first table is the short list most people ever touch.

#### Changing options from the page

In **Settings > Options**, choose **Edit**, change a `page` option, then **Save**. Only a signed-in person can save; hook, hub, node and MCP device tokens cannot. The board validates every value with its startup parsers. A batch with an invalid value saves nothing. Secrets and URL credentials cannot be saved here. The last 20 saves appear below the options, separately from at most 5 recent refusals. Refusals never record the submitted value or phrase; unidentified callers are recorded without their headers.

Values live in `<data dir>/settings.page.json`, a private mode `0600` file with `{"version": 1, "set": {"KEY": "value"}}`. Writes use a temporary file and atomic replacement. The page sends the file revision it loaded; if another tab saved first, it reloads instead of overwriting that change. For `page` options, precedence is **page > non-empty environment > settings file > default**. No page option changes the running process. Options shows **saved, needs restart**, with running and next-start values, and Home shows the pending keys and the platform's restart command. Use Restart board to apply pending values.

**Use the file's value** removes that page entry; the environment still takes precedence over the settings file. To reset every page override, remove `settings.page.json` and restart the board. `CCBOARD_PAGE_FILE` can name another file; an explicitly empty value disables page settings. The installer does not write this switch or the page file. A symlink, unsafe owner or permissions, invalid JSON, unsupported version, or a file larger than 64 KB is ignored; only valid `page` entries and opted-in `confirm` entries with confirmation metadata can load. Old plain page entries for the worker caps, node lanes and subagent model are ignored and reported. Saving against a refused file reports the specific problem and a shell command with its actual path to reset it. Symlinks and files owned by someone else are never overwritten. Doctor reports refusals, ignored entries, and changes waiting over a day for a restart. The board never edits the installer's environment file.

**Source limits.** On Linux a matching line is attributed to the file only if the existing file safety check permits reading it; otherwise the source remains environment. The check rejects group-readable or differently owned files, including a root-owned `0640` file for an ordinary board user. **UNVERIFIED, to verify on the box:** whether the board user can read `/etc/ccboard/env` inside the container. This checkout does not establish that.

#### Most people set at most these

These seven cover a normal install: where projects live, the two HTTPS ports, who may sign in, where the data goes, how the board runs and where the backup goes. Everything else has a default that works.

| Variable | Default | Meaning |
|---|---|---|
| `PROJECTS_DIR` | `/srv/projects` | Where projects live (created and chowned to you). Class: `file`. |
| `CCBOARD_HTTPS_PORT` | `443` | Tailscale HTTPS port for the dashboard **and** the terminal (`/tty`). Class: `installer`. |
| `CODE_HTTPS_PORT` | `8443` | Tailscale HTTPS port for code-server. Class: `installer`. |
| `CCBOARD_ALLOWED_USERS` | your tailnet login | Comma list of `Tailscale-User-Login` values allowed in. Class: `file`. |
| `CCBOARD_DATA_DIR` | `~/.local/share/ccboard` | SQLite database. Class: `file`. |
| `CCBOARD_RUNTIME` | `systemd` | `systemd` runs the board as `ccboard.service`; `docker` runs it as the `ccboard` container ([below](install/docker.md#run-the-board-as-a-container-optional)). Remembered in `/etc/ccboard/env`; go back with `CCBOARD_RUNTIME=systemd ./install.sh`. Class: `installer`. |
| `CCBOARD_RESTIC_REPO` | `<data dir>/restic` | restic repository for the nightly backup (`sftp:user@host:/path`, `rclone:remote:path`, `s3:…`, or `off`). The default is on the same disk: fine against deletion and corruption, useless against disk loss. Class: `file`. |

#### Ports and addresses

| Variable | Default | Meaning |
|---|---|---|
| `CCBOARD_PORT` | `8000` | Loopback port of the dashboard. Class: `installer`. |
| `TTYD_PORT` | `7681` | Loopback port of ttyd. Class: `installer`. |
| `CODE_SERVER_PORT` | `8080` | Loopback port of code-server. Class: `installer`. |
| `NTFY_HTTPS_PORT` | `8444` | Tailscale HTTPS port for the ntfy server (`CCBOARD_NTFY=0` skips ntfy). Class: `installer`. |
| `NTFY_PORT` | `2586` | Loopback port the ntfy server listens on. Class: `installer`. |
| `PREVIEW_HTTPS_BASE` | `9100` | First tailnet HTTPS port used for task preview links. Class: `installer`. |
| `CCBOARD_PUBLIC_URL` | set by `install.sh` | The board's tailnet HTTPS address, `https://<tailnet name>:<CCBOARD_HTTPS_PORT>`, used in links the board sends out. Class: `installer`. |

Tailnet-only `tailscale serve` accepts any HTTPS port. If a chosen port already carries something else (another serve handler or a Funnel), `install.sh` stops and tells you; pick other ports or rerun with `CCBOARD_REPLACE_SERVE=1` to replace that port's handlers. It never runs `tailscale serve reset` and never touches ports you did not name.

Example for a box where 443 and 8080 are already taken:

```sh
CCBOARD_HTTPS_PORT=8443 CODE_HTTPS_PORT=10000 CODE_SERVER_PORT=8081 ./install.sh
```

#### Sign-in and access

| Variable | Default | Meaning |
|---|---|---|
| `CCBOARD_PAGE_WIDEN` | empty (off) | File-only opt-in: exactly `1` allows confirmed Settings changes. The page cannot set it. Turning it off disarms saved confirm values on the next start. Class: `file`. |
| `CCBOARD_LOCAL_SIGNIN` | `0` | Set `1` explicitly to allow local browser sign-in, including under systemd or docker; Doctor warns while enabled. Class: `file`. |
| `CCBOARD_LOCAL_SIGNIN_HOURS` | `12` | Local session lifetime in hours, from 1 to 168; restarting the board revokes all sessions. Class: `file`. |
| `CCBOARD_CLONE_ALLOWED_HOSTS` | empty | Comma list of hosts a clone may name although they look local or private: exact names or IP literals (`gitea.lan`, `192.168.1.5`), or `*.example.com` for the names below a domain (never an address). Empty means public hosts only; see [Clone preflight and the clone URL rule](guide/new-project-and-clone.md#clone-preflight-and-the-clone-url-rule). A listed host never brings back a refused scheme. Class: `file`. |
| `CCBOARD_LINK_ROOTS` | `$HOME` on native runtimes; off in Docker | Unset or empty (after stripping whitespace) uses the board’s own runtime default. `off` (case-insensitive) disables linking everywhere (403). Otherwise, a colon-separated list of absolute roots is honoured even in Docker, where it names paths inside the container. Relative entries are ignored. Installers write an empty default. Root directories themselves, PROJECTS_DIR, board data and private configuration folders cannot be linked. Class: `file`. See [Linked folders](guide/home-and-projects.md#linked-folders). |
| `CCBOARD_POOLS` | `0` | `1`, `true` or `on` enables isolated runtime profiles and migration of saved logins. Restart required. Nothing changes by default. See [Runtime profiles (P1)](pools.md). Class: `file`. |
| `CCBOARD_MCP_REMOTE` | `0` | `1` starts the remote MCP endpoint `/mcp` switched on; `0` (or empty) leaves it off. Once the switch in Settings > Agents has been used, the stored choice wins. Off, `/mcp` answers 404. See [MCP from other devices](guide/mcp.md#mcp-from-other-devices). Remembered in `/etc/ccboard/env`. Class: `file`. |
| `CCBOARD_MCP_NODES` | `0` | How far an agent's MCP tools (the stdio shim and `/mcp`) may act on a paired node, through the relay and only the rows listed for agents. `0` (or empty, or any word not listed here) keeps every tool on this board, and a tool call that names another node is a tool error. `read` lets them list the nodes and read a node's projects and tasks. `tasks` also lets them create a task and start it in a new session there. `1` keeps meaning `read` (the safer reading of the old value: an upgrade never hands agents a write). Settings > Agents shows the level in words, read-only. See [Using nodes from an agent](nodes/agents-and-mcp.md#using-nodes-from-an-agent). Remembered in `/etc/ccboard/env`. Class: `file`. |

#### Notifications

| Variable | Default | Meaning |
|---|---|---|
| `NTFY_URL` | empty | Set by `install.sh` to the loopback address of the ntfy server (empty when ntfy is skipped). Class: `file`. |
| `NTFY_PUBLIC_URL` | empty | Set by `install.sh` to the tailnet HTTPS address of ntfy, the one the phone subscribes to (empty when ntfy is skipped). Class: `installer`. |
| `NTFY_TOPIC` | `ccboard` | ntfy topic the board publishes to: 1–64 letters, digits, `-` or `_`. Invalid startup values use `ccboard`. Class: `page`. |
| `CCBOARD_APPROVE_TIMEOUT` | `90` | Whole seconds from 1 to 9999 a permission prompt waits for a remote answer; invalid startup values use 90. Set with the installer because Claude and Codex hooks carry this timeout plus 30 seconds. Page-file entries are ignored (`CCBOARD_REMOTE_APPROVE=0` disables the hook). Class: `installer`. |

#### Backup

| Variable | Default | Meaning |
|---|---|---|
| `CCBOARD_BACKUP_PUSH` | `1` | `0` skips the nightly copy of unpushed work to `ccboard-backup/<node>/<branch>` on every repo's `origin`. Class: `file`. |
| `CCBOARD_BACKUP_ONCALENDAR` | `*-*-* 02:30:00` | systemd calendar spec of the backup timer (`CCBOARD_BACKUP=0` at install time leaves the timer disabled). On a Mac only `*-*-* HH:MM[:SS]` and `HH:MM` (every day) are understood; see [Backup on each system](backup-and-restore.md#backup-on-each-system). Class: `installer`. |
| `CCBOARD_BACKUP_EXTRA` | empty | Colon-separated extra paths to include in the restic snapshot. Class: `file`. |
| `CCBOARD_RESTIC_PASSWORD_FILE` | `<data dir>/restic-password` | Where the restic password lives. Class: `file`. |

#### Install and runtime

| Variable | Default | Meaning |
|---|---|---|
| `CODE_SERVER_VERSION` | `4.139.1` | Version installed when code-server is absent. Class: `installer`. |
| `CCBOARD_IMAGE_TAG` | `latest` | Docker mode only, install-time only (kept in the compose `.env`, not in `/etc/ccboard/env`): the `ghcr.io/rpandox/ccboard` tag to run, e.g. `sha-1a2b3c4` or `v0.5.2` to pin; Watchtower follows `latest` only |
| `CCBOARD_DEVCONTAINER` | unset | `1` installs the devcontainer CLI on the host (docker required; works in docker mode too) |
| `CCBOARD_RECOVER` | `1` | Relaunch Claude sessions after a reboot. Class: `file`. |
| `CCBOARD_AUTO_CONTINUE` | `1` | Type `continue` once into a session parked on a limit after its window resets, and into a session that was working before a reboot once its relaunch is back at the prompt (`0` turns both off for every session; one session opts out from its own menu or Tune panel, see [Auto-continue](guide/automation.md#auto-continue)). Class: `file`. |
| `CCBOARD_AUTOCLOSE_GRACE` | `45` | Seconds after a task session's Stop before the board closes that session itself (a person can still say "keep open" in that time). A number; an empty value means 45. Applies to task sessions only. Class: `page`. |

#### Agents and claude-mem

| Variable | Default | Meaning |
|---|---|---|
| `CODEX_HOME` | empty | Where Codex keeps its config (empty means `~/.codex`, as for Codex itself). `hooks.json` is written there and `codex mcp add` registers there. Remembered in `/etc/ccboard/env`; absolute path, no spaces. Class: `file`. |
| `CCBOARD_CODEX_HOOK_TRUST` | `review` | `review` (default): trust ccboard's Codex hooks once in Codex itself. `bypass`: every Codex session the board starts gets `--dangerously-bypass-hook-trust`, which also runs the `.codex/hooks.json` of any repository you cloned without asking (the same risk class as bypassPermissions; the doctor warns about repo-level hook files). Remembered in `/etc/ccboard/env` ([Codex](guide/codex.md#codex)). Class: `file`. |
| `CCBOARD_CODEX_HOOKS_ASYNC` | `1` | `0` writes every Codex hook synchronous, for a Codex build that skips async hooks. Read by the installer when it writes Codex's `hooks.json`, so a rerun keeps the choice. Class: `installer`. |
| `CCBOARD_SUBAGENT_MODEL` | empty | The model Claude's subagents use on the sessions, tasks and scheduled runs the board starts: `haiku`, `sonnet`, `opus` or a full model id. Empty (or `inherit`) leaves them on the main model, which is the expensive way when that is Opus or Fable. The board puts `CLAUDE_CODE_SUBAGENT_MODEL=<value>` in the launch line (`env CLAUDE_CODE_SUBAGENT_MODEL=haiku claude ...`) and in the environment of `claude -p`; a subagent that names its own model still gets it. The launcher's *Subagent model* field overrides it per launch. It never writes `~/.claude/settings.json` (the manual alternative is an `env` entry there) and never sets `CLAUDE_CODE_SUBAGENT_MODEL_FORCE`, which overrides definitions that ask for a stronger model and has its own switch in the launcher. Class: `confirm`. |
| `CCBOARD_HEADLESS_FABLE_CAP` | `25` | The most Max $ a schedule or batch Claude run that resolves to Fable (`fable`, `best`, any id with `fable` in it) may carry. `claude -p` never asks before billing Fable usage credits, so such a job also needs its own acknowledgement; a cap alone is not consent and a stored job without both is held, not run. Class: `file`. |
| `CCBOARD_CLAUDE_ULTRACODE_FLAG` | empty | `1` or `0` records the result of the box check whether `claude --effort ultracode` works on this box; empty asks `claude --help`. A wrong `1` makes the launcher pass a flag Claude rejects. Class: `file`. |
| `CCBOARD_PRICE_TABLE` | empty | Absolute path of a JSON file that adds to or replaces the built-in list prices behind the Usage page's Estimated basis (`{"date": "YYYY-MM-DD", "source": "...", "models": {"sonnet-5-5": {"input": 2, "output": 10, "cache_read": 0.2, "cache_write": 2.5}}}`, USD per million tokens; `cache_write` may be left out). Server side only: the browser cannot change prices. The file is re-read when it changes; one that cannot be read changes nothing. Class: `file`. |
| `CCBOARD_CLAUDE_MEM` | `1` | `1` verifies the claude-mem plugin (installs it when it is missing) and lets the board watch its worker; `0` skips both ([claude-mem](guide/claude-mem.md#claude-mem)). Class: `installer`. |
| `CCBOARD_MEM_PORT` | empty | Port the board probes for claude-mem's worker. Empty finds it by itself (`~/.claude-mem/worker.pid`, then claude-mem's settings, then the plugin's own default, 37700 plus your uid modulo 100: 37700 on a Linux box with uid 1000, 37701 on a Mac with uid 501, 37777 where the system has no uid). It only tells the board where to look; the worker's own port is `CLAUDE_MEM_WORKER_PORT` in `~/.claude-mem/settings.json`. Class: `file`. |
| `CCBOARD_MEM_VIEWER_URL` | empty | Existing claude-mem viewer or dashboard URL; HTTPS, or HTTP on loopback only, without credentials or userinfo. Set it in Settings > Options and restart the board. It takes precedence over `CCBOARD_MEM_HTTPS_PORT` for the Memory page link and creates no port mapping. Invalid URLs are ignored and flagged in Options and Doctor. Class: `page`. |
| `CCBOARD_MEM_HTTPS_PORT` | empty | Optional tailnet HTTPS port for claude-mem's own viewer; empty (the default) exposes nothing. The Memory page uses this mapping only when `CCBOARD_MEM_VIEWER_URL` is empty or invalid. Set, `install.sh` maps it with `tailscale serve --bg --https=<port>` to the worker (found as the board finds it: `CCBOARD_MEM_PORT`, `worker.pid`, claude-mem's settings, then the plugin's default) and `state.config.mem_viewer_url` carries `https://<your board host>:<port>/` for the "Open claude-mem viewer" link. **Who can reach it:** the worker has no sign-in of its own and its API can write, so every device on your tailnet can open and change the claude-mem worker; restrict the port with a Tailscale ACL if that is too wide. 443 and the board's, code-server's and ntfy's ports are refused before anything changes, as is a port something else already serves (unless `CCBOARD_REPLACE_SERVE=1`). `CCBOARD_MEM_HTTPS_PORT=off ./install.sh` turns the mapping off again (only that port) and forgets it. The Doctor's `memory-viewer` check warns while the mapping is open. Class: `installer`. |
| `CLAUDE_MEM_PROJECT_ENVIRONMENTS` | empty | Read by the board from its own environment (the same name as claude-mem's setting). A JSON array of `{name, patterns}` that gives a folder a fixed key on the Memory page, for example `[{"name": "<repo>", "patterns": ["~/work/<repo>/**"]}]`. Invalid values are ignored. Empty means the default key rules ([claude-mem](guide/claude-mem.md#claude-mem)) |
| `CCBOARD_MEM_SERVICE` | `0` | `1` runs the worker as `ccboard-mem.service` (on a Mac: the `dev.ccboard.mem` launchd job) from a clean environment; off until it has been verified on the box ([claude-mem](guide/claude-mem.md#claude-mem)). Remembered in `/etc/ccboard/env`; `0` disables the unit again. Class: `installer`. |

#### Nodes and epics

| Variable | Default | Meaning |
|---|---|---|
| `CCBOARD_NODE_NAME` | `hostname -s` | This box's name in the fleet strips: 1–41 letters, digits, `-` or `_`, starting with a letter or digit. Invalid startup values use the automatic name. Class: `page`. |
| `CCBOARD_HUB_TOKEN` | generated | **Legacy** shared secret for node-to-node `GET /api/node/summary` with `CCBOARD_NODES` (deprecated: it opens that one route and nothing else, and every box shares it; [pair the boards](nodes/pairing.md#pairing-two-boards) instead). Copy the same value to every box that still uses it. Class: `file`. |
| `CCBOARD_NODE_AUTH` | `token` | How a call from a paired node is checked (issue #136): `token` (no extra device check; node signatures still apply, the default), `whois` (also the device the node was paired from, asked of Tailscale) or `whois+cap` (also a permission granted in the tailnet policy). Unset or blank is `token`; a value that is none of the three is never read as `token`: every node call is refused (`503 bad_mode`) until it is fixed. The two device modes refuse to switch on while the board listens beyond loopback. See [The device check](nodes/device-check.md#the-device-check). Class: `file`. |
| `CCBOARD_NODE_CAP` | empty | The capability name for `whois+cap`, `<a domain you control>/cap/ccboard` (lower case; `tailscale.com` and its subdomains are refused). `whois+cap` stays off, with `503 no_cap_name`, until it is set. The grant and the `tailscale serve --accept-app-caps` command in Settings > Nodes show a placeholder domain while it is empty. See [The device check](nodes/device-check.md#the-device-check). Class: `file`. |
| `CCBOARD_NODE_KEYS` | `require` | Node signing keys. Unset, it is `require` once every pair has a key, and `prefer` while some pair has none (nothing is cut; Doctor and Settings > Nodes name the pair). Set on purpose: `prefer` keeps old unkeyed pairs working and upgrades them on first use; `require` refuses unkeyed pairs at once; `off` makes and sends no key. Existing pins always require signatures, including in `off`. Invalid values fail closed as `require`. See [Node signing keys](nodes/pairing.md#node-signing-keys). Class: `file`. |
| `CCBOARD_NODE_LANES` | `3` | Task lanes this board advertises on its node card (`lanes.cap`; `0` = no limit). Advice only: it feeds routing and a warning after a hand start, and never refuses a start. A value that is not a whole number from 0 to 999 falls back to `3` and the Doctor's `node` row says so. Class: `confirm`. |
| `CCBOARD_NODE_PORTS` | `443,8443` | HTTPS ports a board tries, in this order, on another tailnet device when it looks for ccboard (issue #134). Every board that should be found must be served on one of them (`CCBOARD_HTTPS_PORT`); the Doctor's `nodes-port` row warns when this board is not. An item that is not a port is dropped and the row says so. Class: `page`. |
| `CCBOARD_NODE_TAGS` | `tag:ccboard` | Tailscale tags, separated by commas, that make a device a candidate node besides your own devices (`none` = your own devices only). A device shared in from another user is never a candidate. See [Finding other nodes](nodes/overview.md#finding-other-nodes). Class: `file`. |
| `CCBOARD_NODES` | empty | Legacy summary-only peers, as `name=https://<node>.<tailnet>.ts.net:<port>,…`; use pairing for new nodes. Class: `file`. |
| `CCBOARD_NODES_POLL` | `20` | Seconds between two polls of one paired node by the hub read model (`GET /api/node/state`; a little jitter is added). From `5` to `3600`; invalid startup values fall back to `20`. A board with no paired node polls nothing. See [The hub read model](nodes/hub-and-relay.md#the-hub-read-model). Class: `page`. |
| `CCBOARD_EPIC_AUTHORS` | empty | GitHub logins, separated by commas or spaces, whose claim comments count on an epic (issue #147), when GitHub also says they can write to the repository (`author_association` owner, member or collaborator). Since issue #149 it also decides whose issues a node will run as a task: the issue and its epic must each be written by one of these logins (or the `gh` login, when empty). Empty means the login `gh` is authenticated as on this board only; the repository owner is never trusted by being the owner, and with neither known no claim counts ("cannot tell who may claim: gh login unknown"). A comment with the claim marker from anyone else is shown as "not from an allowed author" and never as a claim. An entry can be `login` or `login:id` (issue #157, worker mode): with the numeric GitHub user id (from `gh api users/<login>`) it pins that account, so a stranger who takes over the login later, or a renamed account, is not allowed; without an id the edit check and worker still bind the `gh` login of this board to the id `gh` reports for it. The same list and id pins apply to every body edit and title rename of the issue and its epic. A collaborator who ticks a checkbox in an allowed author's issue makes it unrunnable until that collaborator is added to this list. A word that is not a login, or an id that is not a number, is ignored. Class: `confirm`. |
| `CCBOARD_EPIC_WRITES` | `0` | `1` lets this board write to GitHub to claim an epic issue (issue #148): one comment of its own, itself as assignee and the label `ccboard:in-progress`, and nothing else. Anything but `1` (the default) turns the epic claim writes off: the claim writer refuses before it builds a command and a claim answers "writes_off". Since issue #149 it also governs the work of a claimed issue (the phase comments and, if the epic asks for it, the pull request of `auto_pr`); see [Working an epic issue](nodes/epics.md#working-an-epic-issue). Since issue #158 it also governs **Create** of the New epic sheet (labels, issues, sub-issue links and one edit of the epic; see [Starting an epic from the board](nodes/epics.md#starting-an-epic-from-the-board)). The switch governs only these epic writes; the pull request actions a person starts on a task (Create PR, Merge, comment) are separate and are not governed by it. Remembered in `/etc/ccboard/env`. Class: `confirm`. |
| `CCBOARD_EPIC_ALIAS` | empty | The name this board gives itself in its claim comments. Comments on a public repository are public, so it is never the host name: empty means a short neutral word derived from the node id. A name with a dot, or equal to the machine's short host name, is not used (the neutral word is) and the doctor says so. Letters, digits, `-` and `_`, up to 40. Class: `page`. |
| `CCBOARD_EPIC_LEASE_MIN` | `30` | Minutes an epic claim stays live without a renewal, 10 to 120 (anything else is 30). The holder renews every 5 minutes by editing its own claim comment. Class: `page`. |
| `CCBOARD_EPIC_MAX_WINDOW_PCT` | `85` | Auto routing does not send an epic issue to a node whose rate-limit window for the issue's agent is at or above this percent, 10 to 100 (anything else is 85). A window whose percentage is not known counts as 50. An epic can set its own limit on its page. See [Routing an epic to a node](nodes/epics.md#routing-an-epic-to-a-node). Remembered in `/etc/ccboard/env`. Class: `page`. |
| `CCBOARD_EPIC_WORKER` | `0` | `1` lets this node take ready issues of the epics you list **by itself**, with nobody pressing Run (issue #157). Anything but exactly `1` is off, and off means off: no thread, no GitHub call, no stored row and no new key in `/api/state`. **The text of an issue then starts an agent on this machine** (`acceptEdits`, in a worktree, unattended), so read [The epic worker](nodes/epics.md#the-epic-worker) first. It needs `CCBOARD_EPIC_REPOS`, a known allowed author and `CCBOARD_EPIC_WRITES=1`, and refuses to start without them (the Doctor row `epic-worker` fails and says which). Settings > Options > Epics & worker can save it after the owner opts in with `CCBOARD_PAGE_WIDEN=1` and types the confirmation phrase. Remembered in `/etc/ccboard/env`. Class: `confirm`. |
| `CCBOARD_EPIC_REPOS` | empty | The repositories the worker may take issues from, as `owner/name` separated by commas or spaces (lower-cased, at most 25; a word that is not a repository is dropped and the Doctor says so). Empty means the worker refuses to start. A repository must also be cloned on this node. Use a private repository: anyone who can create or edit issues in a listed repository can steer the agent. Remembered in `/etc/ccboard/env`. Class: `confirm`. |
| `CCBOARD_EPIC_WORKER_POLL` | `90` | Seconds between two looks at the listed epics, 30 to 3600 (anything else is 90 and the Doctor says so), plus or minus 10 percent each time and never below 30 s. Reads use ETags, inside a budget of 80 GitHub reads a cycle. Remembered in `/etc/ccboard/env`. Class: `page`; the Worker form still requires `CCBOARD_PAGE_WIDEN=1` to save this key. |
| `CCBOARD_EPIC_WORKER_MAX_PER_HOUR` | `6` | The most issues the worker starts in any hour, 1 to 60 (anything else is 6). Counted from a row on disk, so a restart does not reset it. A cap is a limit, not consent. Remembered in `/etc/ccboard/env`. Class: `confirm`. |
| `CCBOARD_EPIC_WORKER_MAX_PER_DAY` | `30` | The most issues the worker starts in any 24 hours, 1 to 500 (anything else is 30). Counted the same way. Remembered in `/etc/ccboard/env`. Class: `confirm`. |

#### Platform specific

Only on WSL2.

| Variable | Default | Meaning |
|---|---|---|
| `CCBOARD_TAILSCALE_PLACEMENT` | empty | WSL2 only: where Tailscale runs, `wsl` (inside the distro, the normal install) or `host` (on Windows; the installer skips the Tailscale checks and the `serve` mappings, needs `CCBOARD_ALLOWED_USERS` and `CCBOARD_PUBLIC_URL`, and prints the PowerShell commands to run on Windows). Empty means `wsl`; `host` is never picked for you, because every program on the Windows PC can then reach the board and claim any allowed login. Ignored with a warning anywhere else; see [Windows (WSL2)](install/windows-wsl2.md#install). Class: `installer`. |

### Advanced and testing variables

`install.sh` remembers only the names in its `ENV_KEYS` list, and the settings tables above are that list. The variables below are read by the app, the hook scripts or the container entrypoint and are not in that list; set them in the environment of the process that reads them. Three names from the settings tables are left out of `ENV_KEYS` on purpose: `CCBOARD_DEVCONTAINER` is a one-time install step, `CCBOARD_RECOVER` is read at each start, and `CCBOARD_IMAGE_TAG` is kept in the compose `.env`, not in the env file. `tests/test_env_keys.py` keeps the three lists (the installer's `ENV_KEYS`, this page and the names the code reads) in step: a new setting touches the reader, `ENV_KEYS`, a row here and the test's two reason lists.

| Variable | Default | Meaning | Read by |
|---|---|---|---|
| `CCBOARD_REMOTE_APPROVE` | `1` | `0` leaves the permission hook out of Codex and the container entrypoint, so Codex permission prompts are answered in the terminal only | `scripts/docker-entrypoint.sh`, `app/agents/codex.py` |
| `CCBOARD_TMUX_SOCKET` | `ccboard` | Name of the tmux socket the board's sessions live on. Class: `file`. | `app/config.py`, `bin/ccboard-attach`, `scripts/docker-entrypoint.sh` |
| `CCBOARD_URL` | `http://127.0.0.1:8000` | Where the hook scripts, the statusline and the MCP shim send their requests | `bin/ccboard-hook`, `bin/ccboard-hook-fast`, `bin/ccboard-statusline`, `bin/ccboard-permission`, `scripts/ccboard_mcp.py` |
| `CCBOARD_HOOK_TOKEN_FILE` | `~/.local/share/ccboard/hook-token` | The token file the hook scripts and the MCP shim send with each request | `bin/ccboard-hook`, `bin/ccboard-hook-fast`, `bin/ccboard-statusline`, `bin/ccboard-permission`, `scripts/ccboard_mcp.py` |
| `CCBOARD_AGENT` | `claude` | Name of the agent whose hook this is; Codex's hook commands set `codex` | `bin/ccboard-hook`, `bin/ccboard-permission`, `scripts/codex_hooks.py` |
| `CCBOARD_MEM_BUN` | empty | Path to `bun` for the claude-mem worker; tried first | `bin/ccboard-mem-run`, `app/doctor.py` |
| `CCBOARD_MEM_BUN_DIRS` | `/usr/local/bin /opt/homebrew/bin /home/linuxbrew/.linuxbrew/bin /usr/bin /snap/bin` | Space-separated directories searched for `bun`; setting it, even empty, replaces the list | `bin/ccboard-mem-run` |
| `CCBOARD_MEM_PLUGIN_DIR` | newest cached plugin version | The claude-mem plugin folder the worker runs from | `bin/ccboard-mem-run` |
| `CCBOARD_CONTAINER` | `ccboard` | Container name the watchdog checks | `scripts/ccboard-watchdog.sh` |
| `CCBOARD_SHADOW` | `0` | `1` runs the container entrypoint side by side with a live board: hooks, tmux and MCP registration are left alone | `scripts/docker-entrypoint.sh` |
| `CCBOARD_APP_ROOT` | `/opt/ccboard` | Where the container keeps the app; tests point it elsewhere | `scripts/docker-entrypoint.sh` |
| `CCBOARD_ENV_FILE` | `<data dir>/env` | macOS: the settings file the board reads at start, because launchd has no `EnvironmentFile` (see [macOS](install/macos.md#what-the-installer-changes)); an empty value turns the file off. Class: `file`. | `app/config.py` |
| `CCBOARD_PAGE_FILE` | `<data dir>/settings.page.json` | Page overrides file; an explicit empty value disables it. Set in the service environment, never in the page file or installer. Class: `file`. | `app/config.py`, `app/options.py` |
| `CCBOARD_LAUNCHD_DOMAIN` | `gui` | macOS: `user` selects the experimental Background-session layout for the launchd jobs; anything else is `gui` | `app/platform.py` |
| `CCBOARD_TMUX_TMPDIR` | `/tmp` | macOS installer switch, not remembered: the `TMUX_TMPDIR` written into the board, tmux and ttyd launchd jobs (an absolute directory; the installer refuses one that makes the socket path over 100 bytes) | `scripts/install-macos.sh`, `scripts/launchd_render.py` |

## Restart board

Use **Restart board** in Settings > Options or the pending-settings banner. The command palette also links to it (open the palette with its usual keyboard shortcut). Tap once to arm, then Confirm. Open terminals, pending permissions, clones and backups are listed; if the server reports busy, review the warnings and confirm again to proceed. Pending permission answers are lost. Only the board restarts: tmux sessions, ttyd, code-server, ntfy and Tailscale stay running. The page retries until a new boot ID arrives. After 90 seconds it shows log and hand-restart commands.

- **systemd:** signals its own process for graceful shutdown, then exits with status 75 after cleanup. The board unit restarts it after two seconds (`Restart=on-failure`). `NoNewPrivileges=yes` stays enabled; no sudo is needed.
- **Docker:** the supplied Compose file declares `CCBOARD_RESTART_POLICY=unless-stopped` alongside its matching restart policy. Only containers created with that declaration offer the button; others require you to refresh the installed compose file first: rerun `./install.sh` on the host (which copies `deploy/docker-compose.yml`), then run `docker compose up -d --force-recreate ccboard`. The board exits gracefully and Docker restarts it with the same container environment. Page values in the mounted data directory are read again. A restart will not read /etc/ccboard/env. On the box run: `docker compose up -d --force-recreate ccboard`.
- **launchd:** uses `launchctl kickstart -k gui/$(id -u)/dev.ccboard.board` (or the configured user domain). Detached launcher output is appended to `<data dir>/restart.log`, named by the 90-second timeout message. The separate tmux job is untouched.
- **Dev shell / WSL without a supervisor:** no restart button or self-exit. Settings explains how to stop the board in its shell and start uvicorn again.

The route requires a signed-in person, including local browser sign-in, and the page's CSRF header. Agent, hook, hub and node tokens cannot invoke it. Accepted restarts and refusals for identified people are audited; unidentified refusals are logged at debug level. Restarts are limited to one per minute and six accepted requests per rolling hour, including across boots. A launch exception is audited with a short reason and releases the minute spacing for retry; it still counts toward the hourly limit. Local browser sign-ins are held in memory and expire on restart: run `bin/ccboard-open` again when prompted. Tailnet identity reconnects automatically.

Device verification is still required: compare tmux session counts, check a terminal reconnects, measure tap-to-new-boot time on Docker, launchd and available systemd installs, and confirm Docker restarts the container after the board exits (uvicorn may exit 0 or 143 after SIGTERM; `unless-stopped` restarts either way). These timings are not the duration of a Watchtower image swap.
