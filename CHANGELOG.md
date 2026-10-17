# Changelog

One entry per shipped phase, newest first. The commit that ships a phase adds its entry (see CONTRIBUTING.md). Each entry says in its last line whether you have to rerun `./install.sh` on the box. `Upgrade: nothing to do` means a deploy is enough.

## v0.5.85 - 2026-10-17

### Added
- Start Claude, Codex or a shell on a checked device from the launcher's Node picker.
- Agent starts use explicit permissions (Claude defaults to `default`) and a separate tmux socket for each board.
- Device sessions live in their own `bare_sessions` table for rollback safety and appear in the sidebar with a no board badge and Read action.
- Stop offers Confirm and Cancel, expires after five seconds, and checks for remaining sessions after ten seconds; open sessions block device removal.
- Lost start acknowledgements stay unconfirmed without retries; device actions, including delayed `bare_kill`, record the acting person without prompts.

Upgrade: nothing to do.

## v0.5.84 - 2026-10-17

### Added
- Settings lists your online tailnet devices and tagged devices without a board, excluding shared-in devices.
- Add, check and remove devices with plain results and fixes for SSH, the account and groups, system, tmux, agent commands and projects.
- Checks refuse root and administrator accounts, pin SSH host keys, and record device actions in Activity without prompts or secrets.

Upgrade: nothing to do.

## v0.5.83 - 2026-10-17

### Added

- File-only `CCBOARD_PAGE_WIDEN=1` opt-in and exact typed confirmation before Settings widens agent work.
- File-only options: `CCBOARD_BACKUP_PUSH`, `CCBOARD_AUTO_CONTINUE`, `CCBOARD_CODEX_HOOK_TRUST`, `CCBOARD_CLONE_ALLOWED_HOSTS`, `CCBOARD_MCP_REMOTE`, `CCBOARD_HEADLESS_FABLE_CAP`, `CCBOARD_MCP_NODES`, `CCBOARD_NODE_TAGS`, `CCBOARD_NODE_AUTH`, `CCBOARD_NODE_CAP`, `CCBOARD_NODE_KEYS`, `CCBOARD_LOCAL_SIGNIN`, `CCBOARD_LOCAL_SIGNIN_HOURS` and `CCBOARD_RECOVER`.
- Confirm-class worker caps, node lanes and subagent model; old plain page entries remain ignored.
- Permission changes in a native dialog, audits of saves and real refusals, and a switch explanation in Doctor.

Upgrade: nothing to do.

## v0.5.82 - 2026-10-17

### Added
- Settings > Options can save a claude-mem dashboard URL for the Memory page link, taking precedence over the local viewer port.
- Viewer URLs require HTTPS or loopback HTTP without credentials; invalid values are ignored and reported in Options and Doctor.

Upgrade: nothing to do.

## v0.5.81 - 2026-10-17

### Added
- Restart board controls in Settings, pending banners and the command palette, with confirmation and warnings about work in progress.
- Person-only board restarts, spaced one minute apart and capped at six per hour across boots; systemd exits gracefully with status 75 under the existing hardening.
- Automatic reconnect by boot identity, audited launch failures with immediate retry, and launchd output logs named after a timeout.
- Doctor reports the restart method; older Docker installs must refresh the installed Compose file before recreating the container.

Upgrade: for an older Docker install, rerun `./install.sh` on the host to copy `deploy/docker-compose.yml`, then run `docker compose up -d --force-recreate ccboard`. Native installs: nothing to do.

## v0.5.79 - 2026-10-17

### Fixed
- The macOS installer stops again before Homebrew builds a tool from source (ttyd, and tmux on an Intel Mac) when no one is at a terminal, unless CCBOARD_MACOS_ALLOW_SOURCE_BUILD=1 is also set. v0.5.74 had let CCBOARD_MACOS_INSTALL_TOOLS=1 alone start that build, against the owner's answer on #128 ("keep the stop").
- On a terminal the installer still asks once and builds on a yes; the Command Line Tools stop is unchanged.

Upgrade: nothing to do; an unattended Intel install that relied on v0.5.74 needs CCBOARD_MACOS_ALLOW_SOURCE_BUILD=1 again.

## v0.5.77 - 2026-10-17

### Fixed
- Updated callers opt into pending pairing and acknowledge verified answers; new pairings with older boards still activate without an ack.
- Reverse pairing and replacement of older pairs wait for acknowledgement when requested. A failed re-pair ack preserves the caller's existing row and token.
- Unfinished pairs expire after ten minutes; revoked pairs keep their revocation history and are not audited as expired.
- Settings lists unfinished incoming pairings with their expiry and Remove. Pending calls explain what is unfinished without marking the pair for repair; pending rows count toward the limit only up to three.

Upgrade: nothing to do. A new pairing between an updated and an older board still works.

## v0.5.76 - 2026-10-17

### Added
- Settings > Options can save safe page options and remove overrides to use the file's value again.
- Private atomic saves validate bounded numbers and installer-compatible names, reject stale edits and explain how to reset refused files.
- Options keeps the last 20 saves separate from up to 5 identified-person refusals; anonymous refusals only produce debug logs, without audit writes.
- Options and Home show saved changes waiting for a restart; Doctor reports unsafe files, rejected page names and overdue restarts.
- Approval timeout is installer-only because agent hooks carry it; only whole seconds from 1 to 9999 are accepted, with invalid startup values falling back to 90.

Upgrade: nothing to do.

## v0.5.75 - 2026-10-17

### Added
- Optional local browser sign-in with a single-use link from bin/ccboard-open, off by default.
- Local session expiry, a sign-out-all button in Settings > App, and a Doctor warning while enabled.
- Signing in again replaces this browser's old session; Settings identifies this browser and explains how to sign in, including when no browser is signed in locally.

Upgrade: nothing to do.

## v0.5.74 - 2026-10-17

### Changed
- The macOS installer now builds missing tools from source with CCBOARD_MACOS_INSTALL_TOOLS=1 alone, after warning which formulas need building, how long it may take and that existing packages may be upgraded.
- CCBOARD_MACOS_ALLOW_SOURCE_BUILD=0 forbids source builds, including after a terminal yes. The Command Line Tools stop and measured Intel ttyd finding are unchanged.

Upgrade: CCBOARD_MACOS_INSTALL_TOOLS=1 now includes source builds; leave CCBOARD_MACOS_ALLOW_SOURCE_BUILD unset or set it to 1 to allow them, or set it to 0 to forbid them. No installer rerun is needed for existing tools.

## v0.5.73 - 2026-10-17

### Added
- Nodes keep a private Ed25519 key; both boards prove possession before pairing pins their public keys. Signed calls reject tampering, repeated nonces and changed keys.
- Settings shows key ids to compare and a two-tap Rotate key action; offline peers can catch up for 30 days.
- Existing pairs keep working in prefer mode and can upgrade; require refuses unkeyed pairing. Doctor explains invalid modes and how to clear a corrupt-key warning.
- Nonces persist in SQLite across restarts, closing restart replays while allowing 120 seconds of clock lag; transient signature refusals name the reason without asking to re-pair.
- Pairing distinguishes callback timeouts from key conflicts, and the hub and Doctor identify which board’s key changed.

Upgrade: nothing to do.

## v0.5.72 - 2026-10-17

### Fixed
- Every epic work start checks the full visible edit history and title renames of the issue and its epic before claiming. An outsider edit still blocks after an allowed person's edit; unreadable or incomplete history refuses work, including a newest revision missing from the visible edit and rename timestamps (with one-second tolerance).
- Epic rows show stored edit refusals and disable Work with a reason. Add an editor to `CCBOARD_EPIC_AUTHORS`, or copy the text into a new issue by an allowed author. Checkbox edits count too when GitHub reports them.
- A permanent unknown editor or history holds only that issue; the worker continues to the next ready candidate in the same cycle. Checks rest the worker only with a wait or reset time.
- Checks cache unchanged text and account pins, record GitHub's reported cost and rest on low budget. Matching cache hits write no audit or KV records. Older deleted revisions that disappear and compromised allowed accounts remain limits; real-token behaviour is still unverified.

Upgrade: nothing to do.

## v0.5.71 - 2026-10-17

### Added
- Settings > Options lists all startup options in nine groups, with values, defaults, sources, restart badges and a key or label filter.
- One registry describes the options; the read-only API is for signed-in people, hides secrets and masks URL userinfo and credential query values in displayed values and copy lines.
- Empty environment values report the default source; Linux silently skips service files it cannot safely inspect. Empty values use type-specific wording, with automatic behavior shown only where documented by the option.
- The demo includes every group, and the settings tables record each option's editing class.

Upgrade: nothing to do.

## v0.5.70 - 2026-10-17

### Fixed
- Claude approval dialogs remain answerable from the board and a paired hub after the hook wait ends. Needs you shows the scrubbed command or file details and numbered choices on the board, inbox, epic page and hub.
- Only a signed-in person can answer a permission request. Allow and Deny use only the one-time choices from the same Claude dialog, rechecked against the full screen and foreground process immediately before sending. A changed prompt sends nothing.
- Terminal approvals allow only navigation keys and a confirmed interrupt through the local keys route, and offer no redundant “In terminal” action. Waiting and attention rows share cached captures for the scan interval; answers always read the pane again.
- Epic issues show “waiting for a person” with the same Allow and Deny actions.

Upgrade: nothing to do.

## v0.5.69 - 2026-10-17

### Fixed
- Slow relayed claims get more time, and the epic page reads the node’s result after an unknown answer without claiming again.
- Busy claim refusals say how many claims are ahead; `wait_s` is an upper bound, not a measured queue time. Run respects the wait.
- The relay gained one read-only GET row, `epic_claim_status` (scope `tasks`), on purpose to recover unknown claims from local state without another GitHub write.
- Claim recovery uses attempt tokens, expires abandoned pending results, records exceptions and released wins, and prunes results older than a day. The page paints before status reads finish and preserves the fresh GitHub owner or closed state.
- Claim steps log their duration so queue delays can be measured.

Upgrade: nothing to do.

## v0.5.68 - 2026-10-17

### Fixed
- During the first minute after launch (after a 20-second grace), running Claude tasks with no reported state now fail when a shell pane shows a line starting with the workspace-trust error. The failure explains how to accept trust in a session, including through a hub. Sending prompts and echoed commands quoting the error do not trigger this check.
- The project page and task launcher warn until repository history shows an interactive Claude session, a reported Claude state other than the trust failure, or a completed Claude task. Dispatch to an existing session hides the warning. Hub warnings and failure reasons accept only the exact board sentence for that row's folder. The board never answers the trust dialog.

Upgrade: nothing to do.

## v0.5.67 - 2026-10-17

### Fixed
- Removing a both-ways node pair now names the incoming pairs it revoked automatically in the answer. Settings > Nodes warns that both directions go before removal and names the revoked pairs afterwards.

Upgrade: nothing to do.

## v0.5.66 - 2026-10-17

### Fixed
- The test suite's real-home guard (#235) no longer fails a run because the board installed on the same machine synced its hooks while the suite ran (its auto-update restarts it, and the hook installer rewrites `~/.claude/settings.json` and `~/.codex/hooks.json`). `scripts/claude_settings.py` and `scripts/codex_hooks.py` now touch an empty `.ccboard-hook-sync` beside each file they write (best effort; a failed stamp never fails the install). The guard excuses a change to one of those two files only when that stamp moved during the run and the file changed within 15 seconds of it, and prints one warning line saying the install did it.
- A test that really writes the real home still fails the run, as does any change to the other guarded files, or to a hook file with no fresh stamp. The stamp is written on the next hook sync.

Upgrade: nothing to do.

## v0.5.65a - 2026-10-17

### Fixed
- Allow, Deny and In terminal for a permission request (`POST /api/permission/<id>/<decision>`) now answer only for a signed-in person, like the other routes that change what an agent may do. The local hook token, the hub, node tokens and MCP device tokens get `403`, and the request stays undecided. The phone notification buttons and the board's pages are unchanged (they act as the signed-in person); a hub's relayed answer keeps its own path and scope rules. Found in a review of #249.

Upgrade: nothing to do.

## v0.5.65 - 2026-10-17

### Added
- Home now lists the sessions of the paired nodes (issue #82): a collapsible "Other nodes" block after the projects, one group per node (the box, a Mac), read only. Each row has the node chip, project and repo, session name, state glyph and age, and a tap opens the node's session page (`#/n/<handle>/s/<name>`). It uses the hub's last reading only, so Home asks no node for it: a stale node shows how old the reading is, an offline node's last reading is dimmed with its age.
- Sessions that need you stay under "On other nodes" and are not listed twice; the Home state chips narrow the node rows too, and `?demo=1` shows them for build-box, alice-mac and old-laptop.

Upgrade: nothing to do.

## v0.5.64 - 2026-10-17

### Added
- Settings > Notifications now says that Claude's own apps (the Claude mobile app, Remote Control, Claude's push) can notify you about the same moments, and that if you get two notices you can turn off either the board's notices there or notifications in the Claude app's settings (issue #80). A note only; no notification logic changed.

Upgrade: nothing to do.

## v0.5.63 - 2026-10-17

### Added
- A session the board has queued prompts for wears a **+2 queued** chip (#79) on its Home row, its Quad tile header and its terminal header. The number is only the prompts the board itself holds for that session (the tasks it queued behind a turn in flight, whether the text is already in Claude's queue or still waiting in the board); the chip's title says "prompts the board will send when the session is free". Zero shows nothing. Text typed straight into Claude's own terminal is not counted: Claude exposes no such queue.
- `queued_prompts` on each session of `GET /api/state` and on `GET /api/sessions/{name}`. The demo (`?demo=1`) shows one row with **+2 queued**.

Upgrade: nothing to do.

## v0.5.62 - 2026-10-17

### Changed
- Bypass permissions is now allowed only inside the repo's devcontainer (owner decision on #104). An interactive Claude session can choose it only when it is launched with *Run in the devcontainer*; every other request, whatever its spelling or launch kind, is a 400: "bypass is allowed only inside the repo's devcontainer (owner decision, #104); choose auto or turn on Run in the devcontainer".
- Host Claude sessions and every Codex session (`bypass`, `danger-full-access`, `--yolo`, `--dangerously-bypass-approvals-and-sandbox`) are refused. Tasks, dispatch, schedules and headless runs refused bypass already and still do. The acknowledgement for the devcontainer bypass stays. Host bypass sessions already running are not touched (nothing is killed); only new launches are refused.
- The launcher shows the bypass choice only while *Run in the devcontainer* is on; otherwise it is disabled with its reason, and Codex has no bypass choice. A saved default or preset that holds a bypass falls back to auto with a note.
- Hardening. Inside the devcontainer every bypass spelling now needs the acknowledgement `bypass: true`, not only `mode: "bypass"`: `permission_mode: bypassPermissions`, `mode: bypassPermissions`, `--dangerously-skip-permissions` or `--permission-mode bypassPermissions` in `args`, and a resume or continue with a bypass mode. Without it the answer is a 400 "bypass skips every approval: send bypass: true with the acknowledgement". This changes the flat compat body (`permission_mode: bypassPermissions` plus `devcontainer: true` used to launch alone); the launcher sheet already sent the acknowledgement. A stored schedule whose permission mode is not a headless mode (a hand-edited `bypassPermissions`, a typo) no longer runs in it: the run falls back to `auto` and logs a warning.
- The task, dispatch and schedule refusals now say "bypass is only for an interactive Claude session in the repo's devcontainer (#104)" instead of sending you to start a session and choose bypass. Codex sessions get their own sentence, "Codex sessions never run with bypass or danger-full-access (owner decision, #104); choose auto", since Codex cannot use the devcontainer. When a saved bypass falls back in the launcher, the note names the real mode (ask, where this Claude has no auto).
Upgrade: nothing to do.

## v0.5.61 - 2026-10-17

### Changed
- The default permission mode for new work is now `auto` for Claude (issue #83): a session, task or dispatch opened from the launcher, and a schedule or batch started without a mode, run with Claude's safety checks instead of asking or accepting edits. The form shows it as *auto (default): Claude's safety checks*; *ask* is one tap away.
- Codex opens on `auto` too, but only gets `--approve-for-me` where its installed binary has the flag. Without it `auto` is exactly the old default (`-s workspace-write -a on-request`; the older `on-failure` fallback is gone), and a headless run is unchanged.
- A saved choice still wins: a per-repo or project default, a preset, a Backlog card's own mode and an issue's model line. A remembered *ask* is now kept as a choice. A Claude Code too old to start in `auto` opens on *ask* instead of failing.
- Jobs already stored keep their mode; only the fallback for a new job with none changed (`POST /jobs`, `POST /api/batch`).
- Bypass is never a default, and a request from another node still never carries `auto`, bypass or `dontAsk`.

Upgrade: nothing to do.

## v0.5.60 - 2026-10-16

### Fixed
- A scheduled or headless agent run that times out now ends its whole process tree (#116, slice 8). The run starts in its own process group; on a timeout the board sends SIGTERM to the group, waits 5 seconds, then SIGKILL. Before, only the direct child was killed and its node grandchildren kept running (Linux too). The board never signals its own group. On Windows the child is started in a new process group and killed. Environment handling is unchanged.

Upgrade: nothing to do.

## v0.5.59 - 2026-10-16

### Fixed
- A native board (launchd on a Mac, systemd, a dev shell) now names its version on its node card and in the hub (#233). It is the newest heading of the checkout's `CHANGELOG.md`, read once at start. The container keeps its baked `CCBOARD_IMAGE_VERSION`. Found in the two-board check (#155): the box showed the Mac's version as unknown.

Upgrade: nothing to do.

## v0.5.58 - 2026-10-16

### Added
- The optional device check (#136), **off by default**. `CCBOARD_NODE_AUTH=token` (or nothing set) behaves exactly as before. `whois` makes a node token work only from the device that paired, asked of Tailscale on every call (`403 device_mismatch` for any other); `whois+cap` also needs a permission in your tailnet policy (`CCBOARD_NODE_CAP`, `<a domain you control>/cap/ccboard`) for the operation of the route. The mode is set in the settings file, never from a page.
- Safe by construction. A node call must arrive on loopback through Serve before `X-Forwarded-For` or the capability header is read, and both modes refuse to switch on while the board listens beyond loopback (`503 board_bind`). It fails closed: Tailscale that cannot answer is `503 tailscale_unavailable`, never the token alone. Every refusal writes an audit row with its reason. Lookups are cached 60 s per address and capped at 20 uncached a minute. The board only reads from `tailscale` and never changes the policy or `serve`.
- Settings > Nodes > **Device check**: the mode in words, a banner when the mode is stronger than this host can do (not loopback-only, Tailscale older than 1.92, Serve without the capability flag, whois silent), the board's address, the Tailscale version, the last refusal, the grant (your devices or tagged devices, placeholder login and domain) and the `tailscale serve --accept-app-caps` command with a Copy each, and how to switch. Pairs that call this board show the bound device (short StableNodeID) and its tags; a pair with no bound device says "not bound, pair again" while the check is on. `GET /api/nodes/auth`, Doctor rows `board-bind` and `node-auth`.
- `?demo=1&authstate=whois|whois-cap|tagged|bind|old|noserve|down|nocap|badmode|unknown#/settings?sec=nodes` shows every mode and banner. README: **The device check**, the env rows, the security paragraph and the table of the four runtimes; ROADMAP as-built note.

### Changed
- A forward-only migration adds `node_pairs.peer_ts_id` and `peer_tags` (empty for older pairs and for token-mode pairs). First paint is 541,070 bytes of script (+322 in `core.js`); the section is in the lazy settings bundle. Known limits, to verify on the two-board check (#155): the `AcceptAppCaps` key, the real Serve header, whois for a tagged peer, and whois in the container and on WSL2. The calling board shows no special line yet for `device_mismatch`; the Linux socket API is not used (command line only).

### Fixed
- The device check works behind a real `tailscale serve`. uvicorn's default proxy headers already replace the loopback peer with the caller's address, so the first version refused every call as not coming through Serve. The board now checks that the connection arrived on loopback and carried `X-Forwarded-For`, and takes the caller from what uvicorn set; both modes refuse to switch on (`503 proxy_trust`) while `FORWARDED_ALLOW_IPS` or `--forwarded-allow-ips` trusts more than loopback. Tests now run through uvicorn's own middleware.
- Pairing both ways in a device mode now binds the reverse pair too: the device of the other board is taken when it calls back to confirm the pairing. Without that call the reverse pair stays unbound and its calls are refused.
- Concurrent device lookups for one address share one `tailscale whois` and one slot of the 20 a minute.
- A board that cannot read its own Tailscale status no longer lets an untagged device pair (it could not tell whether it is a tagged setup); it refuses with a plain reason and the code stays.
- README: the device check does not protect against a hostile local user, who can forge the forwarded and capability headers on `127.0.0.1` as they can the sign-in header.

Upgrade: nothing to do.

## v0.5.57 - 2026-10-16

### Fixed
- A paired node that answered `304 Not Modified` to a conditional read was recorded as an error ("the peer answered 304") and its last-seen time did not move. A `304` is now an answer: it moves last-seen and clears the last error (#228). A lost rotation still stays marked until the node is paired again.
- A board that was first started while Tailscale was signed out keeps a random `n_` node id and nothing said so. The Doctor has a row, **Node id is a Tailscale id**, that warns when the id is that fallback while Tailscale now answers with an id. With no pairs the fix is to remove the `node-id` file in the data folder and restart the board; with pairs, remove the pairs, remove the file, restart, and pair again. The board never changes the id by itself (#229).
- Settings > Nodes > This node shows a dim "fallback id" note beside a random node id, with a title that explains it.
- Two tests no longer read this Mac. The installer test for a Mac without Homebrew ran the host's real Homebrew when one was installed, and the card test for session counts while tmux is down used the host's real tmux. They now use a script copy with no Homebrew fallback and an explicit tmux-down answer.

Upgrade: nothing to do.

## v0.5.56 - 2026-10-16

### Added
- Nodes P18, start an epic from the board (#158). **New epic** is a sheet (a native dialog) reached from **Tasks > Epics** (its one filled button), a project's **More** menu and the command palette (`new epic`, or `epic`). You pick one of the GitHub repositories of your projects, type an epic title and a goal, and add up to 20 issue rows (title, a short description, tier **A, B, C or Check**, size **S, M or L**, and optionally one earlier row it depends on). Rows add with a button, move with the arrow buttons or **Alt+Up** and **Alt+Down**, and remove with a button that has an Undo. **Preview** (secondary) shows the exact titles, labels, bodies (collapsed), the number of GitHub calls and whether the repository is public; **Create** (the one filled primary) is enabled only after a Preview of exactly what is on screen, and any change disables it again.
- Create writes the two labels (once), the epic issue, each issue, each issue attached as a sub-issue and one edit that fills the epic's `- [ ] #N` list, **one write at a time, at least 1.1 s apart**, through the same queue as claims and **only with `CCBOARD_EPIC_WRITES=1`**. Next to Create the sheet says how many issues it writes, where, whether the repository is public, and that the board cannot delete them. With writes off, Preview works and Create says why. The issues follow the repository's issue template (nine sections, a `Size:` line, the tier's "Who should do it" block from a fixed table that never names a bypass, an empty checklist for you to complete); what you type is data, shown inside the template and never a heading, marker, label or command.
- A stop midway lists the issues that exist and offers **Resume**. Every issue carries a hidden marker line, and a Resume (or a Create typed again) first looks for them by the board's own `gh` login and writes only what is missing, so there is never a duplicate, and a write whose outcome is unknown is never repeated blindly. The form is locked while it writes or waits for Resume, because a changed title would be a different epic. On success the sheet closes and the epic's page opens, where **Run** is.
- `POST /api/epics/preview`, `POST /api/epics` and `GET /api/epics/new` (a signed-in person only: the hook, MCP, node and device tokens get 403; no MCP tool and no node route reaches them), the repositories only from your own projects' remotes. The write queue's closed list grew from 9 to 11 shapes (create a sub-issue link, edit an issue's body), each locked to this purpose and a validated body. Text that names a bypass or holds a secret, an email, a personal path or a host name is refused beside its field, never edited.
- `?demo=1` shows it all: **New epic** on `#/tasks?tab=epics` (or `#/tasks?tab=epics&new=1`, which opens the sheet), then `&newepic=writesoff|fail|unknown|private|norepo|down` (the default creates the epic; `fail` stops at issue 2 and Resume finishes it; `unknown` stops with an unknown outcome and Resume finds the issue by its marker). README: **Starting an epic from the board**; ROADMAP as-built note.

### Changed
- First paint is 540,748 bytes of script (+358 for one demo line in `core.js`); the sheet is `pages/epic-new.js` in the lazy `epic` bundle (styles in `pages/epic.css`), loaded on first use.
- Known limits, UNVERIFIED until #155: the sub-issue and issue-edit requests against real GitHub, `gh` on a real three-child epic, and whether GitHub's issue list returns bodies for the label reads that Resume depends on. A 20-issue epic is 44 writes, about 74 s in one request (the sheet says so and keeps every value if the connection breaks). Resume reads at most 500 issues per label. Screenshots at 1280, 1024 and 390 pixels are for the orchestrator.

### Fixed
- A description or goal with a line such as `## Who should do it` passed Preview and then failed in the middle of Create with a server error. The board now finds the real heading only, Preview runs the same check on every title and body that Create does and names the field, and a request the writer refuses is a failed step that lists the issues that exist, never a 500.
- A Resume no longer adopts an issue from an earlier try whose text is not what would be written now (same titles, another tier, size, goal or dependency). It writes and attaches nothing and says which issue differs: edit it there or start a new epic. A ticked list and the web editor's line breaks do not count as a difference.
- The text check also refuses private keys, npm, GitLab, Google and Stripe live keys, JSON web tokens, Authorization values and long runs of letters and digits, and it reads full-width, look-alike Cyrillic or Greek and unusual hyphen spellings of a bypass as the bypass. The refusal never repeats the text.
- Attaching a sub-issue or editing the epic body only works for issues the same run created or found; the writer refuses any other number or id before a command exists.
- `POST /api/epics` and `POST /api/epics/preview` refuse a body over 1 MB with 413 before reading it as an epic, and a body that is not an epic gets a plain 422 that does not repeat what was sent.

Upgrade: nothing to do.

## v0.5.55 - 2026-10-16

### Added
- Nodes P17, worker mode (#157). An idle node can take the next ready issue of the epics you list **by itself**, with nobody pressing Run. It is **off by default** (`CCBOARD_EPIC_WORKER=1` turns it on, and only that key does: no page has a switch) and it **refuses to start** without `CCBOARD_EPIC_REPOS`, an allowed author (`CCBOARD_EPIC_AUTHORS`, else the `gh` login) and `CCBOARD_EPIC_WRITES=1`; the Doctor row `epic-worker` fails then, warns whenever the worker is on and lists both allowlists. Off, there is no thread, no GitHub call, no stored row and no new key in `/api/state`. The text of an issue starts an agent here (`acceptEdits`, in a worktree, unattended), so the README says in plain words to use a private repository and a dedicated epic.
- Every rule fails closed and the first one that fails is the reason: repository listed and cloned here, issue open and not held, the epic's author and the issue's author allowed (a user with a numeric id, never a bot), the label `ccboard:ready` **set by an allowed login** (read from the issue's events), no rename or last edit by anyone else, dependencies, pin, lanes, then the shared routing check against this node only. At most one start per cycle (`CCBOARD_EPIC_WORKER_POLL`, 90 s, 30 s minimum, jitter), never above the free lanes, `CCBOARD_EPIC_WORKER_MAX_PER_HOUR` (6) and `CCBOARD_EPIC_WORKER_MAX_PER_DAY` (30) counted from a row on disk so a restart does not reset them. It starts only through the work route of #149 (author check, no bypass, redaction, fence, attempt limit). A task waiting for a person for 30 minutes is parked with one fenced comment and its claim released.
- **Settings > Nodes > Worker**, right under **This node**: the state in words with a glyph (`✽ Working on example/website#12`, `✻ Waiting for a window until 14:30`, `✻ Paused by the daily cap until 09:00`, `∙ Idle`, `○ Stopped by a person`, `✕ Refused to start`), the repositories and authors read only, this hour's and today's starts against their caps and when they clear, the last 20 decisions with their reasons, and **Stop worker** (one tap, no confirmation, kept across a restart) and **Start worker**. Off, it says what the worker would do, the risk and the keys to set. A paired node's page shows the same read only, with the age of the reading.
- `GET /api/epic-worker`, `POST /api/epic-worker/stop` and `/start` (a signed-in person only; the hook, MCP, device and node tokens get 403), and `state.node.epic_worker` only while the worker is on. Every decision (start, skip with a fixed word, refused, cap, rest, wait, park, stop, resume, error) is recorded without any title, body or login: 100 kept, 20 shown.
- `?demo=1` shows it all: `&worker=idle|waiting|paused|pausedday|resting|stopped|refused|refusedwrites|refusedauthors|off` (default: working) on `#/settings?sec=nodes` and `#/n/build-box`, with all ten kinds of decision; `demo/worker.json` is held to the real view by `tests/test_worker_demo.py`. README: **The epic worker**, five settings rows and a Security model line; ROADMAP as-built note.

### Changed
- The read-only GitHub reader gained one allowed shape, an issue's events (`/repos/{owner}/{repo}/issues/{n}/events`, 100 a page, three pages at most), and the editor query answers for an Issue as well as a comment. The work route takes an optional `expect` (a digest of the title and body the worker judged) and refuses with the new word `changed` when the text is not the same, before or after the claim; a peer never sends it.
- First paint is 540,390 bytes of script (+541 for one line in `core.js` and one in `lazy.js`); the panel is the lazy bundle `worker` (`nodes-worker.js`, `pages/worker.css`), loaded on first use by Settings > Nodes and a paired node's page, never at first paint.
- Known limits, UNVERIFIED: an unattended run on a real node (#155), the real cost of polling GitHub and GitHub's `editor` answer for an Issue. GitHub names only the last editor of a body, so an earlier edit by someone else followed by an allowed person's edit passes; the work route still refuses a text that changed after the check. Screenshots at 1280, 1024 and 390 pixels were not taken in this phase. If someone with write access renames a label that an allowed author applied to `ccboard:ready`, GitHub may show the old labeled event under the new name (to verify); the worker would then take that event as the setting of the label.

### Fixed
- Stop now also stops a start that was already past the loop's check: the start re-reads the stop under the lock Stop takes and, if it is set, claims nothing, counts nothing and logs no start. Start worker wakes the loop only when the worker was stopped, so it cannot force cycles faster than the poll.
- A moment of `gh` trouble no longer ends the loop: a running worker that cannot learn the login says refused with the reason, keeps its thread and looks again after the poll wait. The worker's view, `/api/state` and the node card never run `gh`; until the login is known they say the worker is waiting to learn it.
- Allowed logins are tied to a numeric id: the `gh` login of the board is bound to the id `gh` reports for it, and `CCBOARD_EPIC_AUTHORS` takes `login` or `login:id` (an id pins the account, a mismatch is not allowed).
- A parked issue is held for 24 hours and skipped while its record says parked, so the next cycle does not start it again. The log and the view are bounded: an epic by a login that is not allowed is one decision for the epic, the view shows 20 skipped issues at most, and the memory of skips is capped.
- The hub dropped the worker of a paired node, so its page never showed it. The hub now keeps it, rebuilt field by field from fixed words and checked numbers (at most 20 decisions, issues checked as `owner/name#n`, every sentence built on the hub).

Upgrade: nothing to do.

## v0.5.54a - 2026-10-16

### Fixed
- A board in a container can reach the other nodes by name. Docker never gives a container the host's `127.0.0.53` resolver, and a daemon-wide `"dns"` setting (for example `8.8.8.8` and `1.1.1.1` on a box that runs other containers) points it at public servers that know no MagicDNS name, so pairing failed its callback (`callback_unverified`, `reverse_dropped`: the reverse token of "also let that node control this one" was dropped) and the hub could not poll a node it was paired to by name. The compose file now mounts `resolv.conf` from its own folder over `/etc/resolv.conf`; `install.sh` writes it: `100.100.100.100` (Tailscale, answers MagicDNS names and forwards the rest) first, then `1.1.1.1` and `8.8.8.8`, and the tailnet as the search domain. Measured on a box with such a setting, with a throwaway container on the host network: a Mac board's MagicDNS name, `github.com` and `ghcr.io` all resolve, and that board's `/api/node/hello` answers. Only a container board needs the rerun (it writes `resolv.conf` and recreates the container); a systemd or macOS board has nothing to do.

Upgrade: rerun ./install.sh.

## v0.5.54 - 2026-10-16

### Added
- Team P4, the whole team in one place (#200, the last phase of #196). **Team** is a page (`#/team`), a collapsed group in the sidebar below **Other nodes** with the number of teammates who are live, one quiet line on Home (`● 3 teammates active in 2 repos`) and **Team** and **Team: owner/repo** in the command palette. The page groups the rows by person or by repo (a segmented control), each row in glyph and words (`✽ Working`, `✻ Waiting`, `∙ Idle`, `◌ Not confirmed yet`, `! Stale`, `○ Ended`, `? Unknown`, `◦ On GitHub`) with the issue, branch, agent, since, last seen and the age of the read; filters for person, repo and state ride in the address. A repository that cannot be read says so with its next step and shows stale rows, never "no teammates". None of it exists, and nothing is requested, while no repository is shared.
- Free issues per shared repository, in words and never guessed: the open issues nobody holds (no assignee, no live status, no linked branch, no claim, no closing pull request). A live board lists them from one page of the open issues nobody is assigned to; until that is read, or when it is old or failed, it says `unread` or `unknown`, names the numbers that are held and lists nothing as free, and a full page says the list may be longer. `GET /api/team/state` gains `overview` and `free`; no route was added and none is in `NODE_ROUTES`.
- One more notification, **A teammate started on an issue I hold** (Settings > Notifications, listed only while a repository is shared). Off by default; when on, ntfy and Web Push, once per teammate and issue, with the login, the issue number and your repository's name and nothing from a GitHub title; never for your own logins or boards or a pull request of somebody who may not write; silent for the first read, after a gap of an hour or a switch off and on; at most five a read; a tap opens `#/team`.
- README **Working with a team**: what is read, what is published, how to switch it off, what stays private, the limits, and the steps of the two-login check on a private organization repository (an owner action, not run) with what stays UNVERIFIED; a Team paragraph in the Security model; the ROADMAP as-built note.
- `?demo=1` shows all of it; `&team=freeok|freeempty|freecut|twoshared` are new (the free list in its other states; a second shared repository), and the existing `&team=` states change the page too. `demo/team.json` carries `overview` and `free`, held to `app/team.py` by tests for the base and for every variant.

### Changed
- One new read, the free list's: `GET /repos/{owner}/{repo}/issues?state=open&assignee=none&per_page=100&page=1`, one more allowed shape of the read-only reader (owner and repository checked, nothing near it). One page of 100, conditional (a 304 keeps the answer), at most every 10 minutes per repository, only after the repository's own read worked, inside the 600 points an hour and only while 100 of them are left for the core reads, with the shared back-off and low-remainder stop; nothing is asked while nothing is shared. Only number, title, label names and date are cached (no bodies), and pull requests are dropped. Measured with the test fake: 105, 525 and 600 points an hour for 1, 5 and 20 shared repositories (99, 495 and 599 before); with pull request file reads 117, 580 and 599 (111, 555 and 601 before).
- First paint is 539,849 bytes of script (+2,285 for the hooks in `core.js`, `shell.js` and `home.js` and the `#/team` address of `router.js`); the page, the sidebar group, the Home line and the palette rows are the lazy `teamview` bundle, loaded only while `state.team.shared` is true.
- Known limits, UNVERIFIED: the two-login check on a real private organization repository, a real 304, single sign-on and app approval, what a read-only collaborator sees, what watchers are notified of, what a delete leaves behind, a real list of open issues without an assignee (a page of 100 with long bodies can pass the 2 MiB answer cap, then free stays `unread`), and screenshots at 1280, 1024 and 390 pixels. A stale presence line counts as holding an issue.

### Fixed
- No false "started on #N" after a failed check. A read that lost presence lines (comments that could not be read, editors not checked, a cut thread, a cut list, or a read that cleared its editor memory) no longer becomes the baseline, so the next good read is not mistaken for new starts.
- A free list never shows an issue a teammate may hold. It says `unknown` (the new reason `graph_window`) when the presence thread was cut, when 30 assigned issues or 30 open pull requests filled the link read, when there are more open pull requests than it covers, or when a pull request of somebody who may write has no closing links yet; a status line with no clock from GitHub holds its issue. Limit, stated in the page and the README: an unassigned issue with a linked branch and no status line can show as free.
- An app assigned to an issue (GitHub type Bot, or a login ending in `[bot]`) is no longer a teammate in the people, the holders or the notice.
- Less work on every page: the team rows, the live count and the live people are worked out once for each answer, and the board builds the team answer once for repeated polls with nothing new (at most 10 s old, same ETag).
- A repository that moves to its canonical name forgets its old change detection, and only a signed-in person can switch **A teammate started on an issue I hold** on (the hook token and node or device tokens get 403 for that change; the other switches are unchanged).

Upgrade: nothing to do.

## v0.5.53 - 2026-10-15

### Added
- Team P3, conflict warnings (#199, of #196). Before you start work a teammate already holds, the board says so, as advice: a chip on a row of the launcher's issue picker (`! taken by @bob`, `◌ @bob, last seen 3 h ago`), and one line above **Start** in the task form (`! #212 is being worked on by @bob since 14:10`, with the signs behind it: assigned, live status, linked branch, live claim, a pull request that closes it). The same issue from one person is one warning, a stale line says how old it is, a gone line and the owner's own boards never warn, and a login GitHub text could not vouch for is "a teammate". Start is the same button; nothing is written to GitHub (no assignee, label or comment).
- The same branch and the same files. The branch your task would get (`worktree-` and its title) is looked up among the branches teammates have on GitHub (pushed, linked, a pull request head), and a task you start shows `! PR #158 by @bob (draft) also changes 3 of these files` with the first six names and a count. The project page's Team block marks a teammate's row that collides with one of your open tasks (`! Collides: same issue as your task`). File lists of your tasks are compared on the board and never published.
- One new read: a pull request's changed files, `GET /repos/{owner}/{repo}/pulls/{n}/files`, as one more allowed shape of the read-only reader. Only a teammate's open or draft pull requests, the 12 latest, 3 pages a cycle, 200 names for one (3,000 files are cut and said so), again after 30 minutes or when updated, inside a cap of 100 of the 600 points an hour. Measured with the test fake: 111, 555 and 601 points an hour for 1, 5 and 20 shared repositories (99, 495 and 599 before).
- `?demo=1` shows every form: open the launcher on `phasezero/website` and the issue list (#152 to #164), start the backlog task "Add alt text to the product gallery images", and open `#/p/phasezero?tab=team`; `&team=warnnone|warnnoid|warnempty|warnfiles` change what is known.

### Changed
- `GET /api/team/state` gains `warnings` on each shared repository's block and `collides` on a teammate's rows. `GET /api/state` gains `team: {shared: true}` only while a repository is shared, so a board that shares nothing makes no team call and the launcher is what it was. No route was added; none is in `NODE_ROUTES`. First paint is unchanged (537,564 bytes of script): the code is in the lazy `launcher` and `team` bundles.
- Known limits, UNVERIFIED: the two-login check on a real private organization repository, a real files list with its pagination, whether GitHub counts a 304, and screenshots at 1280, 1024 and 390 pixels. A path git quotes (non-ASCII) does not match GitHub's name. The task-create form has no task yet, so its file warning is by task: starting an existing task shows it.

### Fixed
- One pull request whose changed files cannot be read (too large, refused, a GitHub error) no longer stops the files of the pull requests after it: its list is marked unknown, asked again after 30 minutes, and the next one is read. Only a rate limit, no login or no gh stops the file reads.
- The file reads keep file names only: the patch text of a page is dropped before it is cached, so a page of up to 2 MiB no longer sits in the shared cache.
- Only pull requests by an owner, member or collaborator, and not a bot, make an issue "taken", have their files read or warn about files. On a public repository anybody can open a pull request; the others still show in the Team block.
- A warning that says "taken" with an old status line now says "last seen 3 h ago" instead of "since 14:10". The file reads stop when fewer than 100 of the 600 points an hour would be left for the other reads.
- Less work per page poll: the team state looks at the newest 100 tasks, one diff runs at a time for a task (a second poller reuses it), the file cache drops its oldest entry instead of everything, and the check for shared repositories reads a key range instead of the whole table.

Upgrade: nothing to do.

## v0.5.52 - 2026-10-15

### Added
- Team P2, publish my status (#198, of #196). In **Settings > Team** each repository gets a **Publish my status** switch (off by default, separate from **Shared with my team**), plus **Private** per repository and one **Pause team presence**. A board that publishes keeps one line of its own, per board, in one comment on one issue titled `ccboard team presence` (label `ccboard:presence`, made on the first opt-in): the issue, the pushed branch, the agent, the state, when it started and the last activity to 15 minutes. Never a title, prompt, reply, transcript, path, host name, account or cost; the keys are a list pinned by a test. A branch appears only in a private repository and only when GitHub already has it. **What I published** shows the literal last body.
- A teammate's **live line** under their name in the Team block of the project page: `● Live`, `◐ Idle`, `◌ Not confirmed yet`, `! Stale` ("last seen 2 h ago") or `? Unknown`, with the agent, `✽ Running` / `✻ Needs you` / `○ Ended`, the issue and branch as GitHub links, since and last active. Time is GitHub's clock (the `Date` header), never this machine's; a line is hidden after 24 hours. A line whose comment was last edited by someone else, or that GitHub will not vouch for, is left out and the block says so.
- A public repository, or one GitHub has not said is private, needs a second confirm that lists exactly what becomes public, and publishes no branch. Each blocked state has its own words and next step: Issues off, archived, issue locked, closed or deleted, cannot open issues, cannot add the label, GitHub refused, interaction limits, waiting for GitHub, hourly budget spent. The Doctor row **Team presence published** warns for each public repository that publishes.
- Writes use the one serial queue of #148 with exactly four new shapes on its closed allow list; at most 240 presence writes an hour per GitHub login across all repositories and boards (measured with the test fake: 9, 51 and 177 an hour for 1, 5 and 20 repositories, two boards). Only the board's own comment is ever edited or deleted, after a fresh read of the thread; a write with an unknown outcome is never sent again. Audit rows record the switches without any login or text.
- `?demo=1&team=lineneeds|lineended|lineunconfirmed|linestale|lineunknown|linegone|linenone|lineclosed|lineerror|linecut` and `?demo=1&presence=off|paused|private|confirm|waiting|removing|issuesoff|archived|locked|closed|deleted|nocreate|nolabel|forbidden|limited|ratelimit|toolong|budget|error` show every live word and every publishing state without a server; the switches and the second confirm work for the life of the page.

### Changed
- The Team read switch no longer says "never writes": reading still writes nothing, and the page says that publishing your own status is a separate switch, off by default. README, ROADMAP and the project page text say it. First paint is unchanged (537,564 bytes of script); everything is in the lazy `team` bundle.
- `GET /api/team/state` gains `people[].presence`, `repos[].presence` and `counts.live`; the epic switch `CCBOARD_EPIC_WRITES` does not govern presence. The team read grows with the presence comments: 97, 485 and 600 points an hour for 1, 5 and 20 shared repositories (cap 600). Known limits: the two-login check on a real private organization repository, what survives a comment delete (edit history), interaction limits and whether a comment edit counts for GitHub's content limit are UNVERIFIED.

### Fixed
- A presence issue with more than 300 comments is no longer written to blindly: the board stops with the state "Too many comments", writes nothing and says so when it cannot take its line down either. A comment older than the last 50 now has its last editor checked by its own id, so an older teammate's line is no longer dropped and a sibling board's line is no longer lost.
- Two boards that make a status issue at the same moment meet on the lowest open one (the other is left alone, never closed); a line moves there at the next heartbeat. A repository that turned public is noticed before every write, and the line comes down until you confirm. A confirm given for a repository known to be private is no longer kept.
- A state change is written at most once in 90 seconds per repository, and waits for the heartbeat when a quarter of the hourly budget is left. A refusal by GitHub (403, 422, interaction limits) is looked at again after 15 minutes instead of 2.
- A line whose answer was lost is now found by Off and Pause (a record is saved before the first comment). The thread is read again just before every edit and delete, so a line another board of yours added meanwhile is kept. Lines of boards gone for more than a day are dropped on a rewrite, and the newest stay over the cap. A removal may only take out this board's own line.
- A repository name of 41 characters or more is no longer refused as if it were a secret; that rule now applies to the branch only.

Upgrade: nothing to do.

## v0.5.51 - 2026-10-15

### Added
- Team P1, read-only presence of teammates (#197, of #196). Switch a repository on (**Shared with my team**, off by default) and the board shows who else works in it, from what GitHub already shows: issues assigned to a person, branches linked to them or pushed in the last 14 days, and open or draft pull requests, forks included. A teammate does not need ccboard. Nothing is written to GitHub and nothing of your own sessions leaves the box. The project page has a **Team** tab and Settings has a **Team** panel; the block has no button, only **Open on GitHub** links. The code is the lazy bundle `team.js` (first paint grew by about 490 bytes).
- Every state has its own words and next step and is never "no teammates": not shared, reading, **authorise this token for the organization** (single sign-on), no access, not found, waiting for GitHub, few requests left, `gh` not logged in or missing, could not be read, and old data (kept on screen with its age). GitHub text is cut, stripped of control characters and drawn as text; a link is shown only if it is a `https://github.com` address of the right shape.
- A person identity: `owner_login` and `owner_id` on the node card (`gh api user`, cached 10 minutes), two Doctor rows (**GitHub login of this board**, **Team GitHub access** with the least token permission), and `repo_slug` on every session and task row of `/api/node/state`: the lower-cased `github.com/<owner>/<name>`, a fork resolved to its parent, `null` for a non-GitHub remote. `GET /api/team/state`, `PUT /api/team/repos/<owner>/<repo>` and `POST /api/team/refresh` are for a signed-in person only and are in no node scope.
- `?demo=1` shows `example/website` shared with `alice` (you) and `bob`: open `#/p/phasezero?tab=team` and `#/settings?sec=team`; `&team=loading|empty|alone|sso|forbidden|notfound|rate|low|stale|nologin|nogh|error|pending|cut|off|hostile` shows each state without a server.

### Changed
- README and ROADMAP no longer say "multi-user" is never: still one board per person and no shared control, read-only presence of teammates exists, opt-in per repository. The README has a Team section (what is read, what is never written, the points an hour measured with the test fake: 67, 335 and 600 for 1, 5 and 20 shared repositories, a 304 counted as a point). The poll is last in line for the login's 5,000 requests an hour: at most 600 an hour for all shared repositories, slower as the limit falls, nothing under 300 left.
- The project page gains a Team tab (after Files) and Settings gains a Team tab (after Nodes). Known limits: the two-login check on a real private organization repository, a real 304, the GraphQL query against real GitHub and whether a 304 costs a point are UNVERIFIED; the hub view of a paired node does not carry `owner_login` or `repo_slug` yet.

### Fixed
- Team reads no longer show old data as current. A new issue or pull request list is kept as soon as it is read, so a later failure (or a 304) cannot bring the old list back, and the issues and pull requests show even when the slow branch-links read fails. That part is said in words ("could not be read just now") and is tried again soon. A claim's lease now runs out on a repeated 304 too: the clock moves on with each revalidated reply (the epic view had the same stale clock).
- The node card never waits on `gh`: it names the login only once it is known, and a cold start asks in the background, once, however many cards are asked for at the same time.
- A failed GraphQL read is now counted against the team poll's hourly budget. **Check again** keeps the cap off for one round only, and is answered at most once a minute. A repository switched off while GitHub was being asked leaves no stored record behind.
- A claim comment counts as presence only from an owner, member or collaborator, as in the epic view, so a stranger cannot appear on a public repository. A back-off that GitHub asks of the team poll now holds the epic reader too.

Upgrade: nothing to do.

## v0.5.50 - 2026-10-15

### Added
- Nodes P16, failure modes and their tests (#153) and the rest of the Mac and WSL2 node work (#154). Whatever goes wrong with a node now ends in a plain state with a glyph and words: **offline** with the time it was last seen (every action on it is off with that sentence and sends nothing), **re-pair** ("<node> removed this pairing, pair again", with a Re-pair button on the node page and the Settings row), **another device** ("This address now belongs to another device, not <node>: remove it or pair again", none of its data shown), **incompatible, API 2** (both numbers named, its card still shown, every action refused), and a **clock off by N s** chip above 5 seconds. A rotation whose answer is lost reads "Outcome unknown, check <node>", marks the pair `re-pair needed`, turns Rotate token off with its reason, and is never asked twice.
- A write whose outcome is unknown (a timeout or a broken connection) now says "Outcome unknown, check <node>: it may or may not have got this. It was not retried." with a **See its tasks** link to the node's page, in the prompt box, the keys, the permission cards, the launcher and the epic Claim and Work notes. Nothing adds a Retry button and the hub never repeats a write. **Update address** appears under a pair that Refresh found at another address (the old address is checked once more, nothing changes until you press it), at most 12 nodes can be paired (the 13th is refused before anything is sent), and GitHub trouble (gh logged out, a rate limit, an outage) makes a node rest for at least 5 minutes with "GitHub is not available there ...; trying again around 14:10" on the epic page.
- Doctor `nodes-health` (every board, read only, skipped with no pair) lists each node that is unauthorized, another device, incompatible, offline or off by more than 5 seconds, worst first, each with a fix that names the button. `FlakyTransport` in `tests/conftest.py` (drop, delay, lost answer, fixed status, truncated body, partition, per-node clock offset, scripted sequence) drives `tests/test_nodes_failures.py`, one test per scenario, and teardown asserts no thread, socket or temp file is left.
- `?demo=1&nodestate=unauthorized|identity|repair|incompatible|skew|offline|moved|unknown` shows each state without a server (open `#/n/build-box` and `#/settings?sec=nodes`), and `?demo=1&epic=rungithub#/epic/example/website/147` shows the GitHub trouble line. The README has the failure table: each scenario, what you see, what to do.
- #154: routing and repositories on a Mac and WSL2 are pinned by tests: a Mac with saved logins unavailable counts as one account and is never left out, a node with an unknown load sorts after nodes with a load, a clone is found by its `origin` remote wherever it lives (`repo_not_here` otherwise), and sleep and wake past the lease leaves the returning node fenced and parked. The README support table now fills the relay, terminal, tail tile and epic work columns ("to verify" on a real Mac and WSL2).

### Changed
- `POST /api/nodes/<peer>/address` is new (Update address). A hub record gains `peer_api`, `api` and `needs_repair`; the relay answers `identity_changed` and `incompatible` as their own refusals; an unknown outcome carries `outcome: "unknown"` and `check_url`; `/api/node/rotate` takes a compare-and-swap so two rotations with one token make one new token. The first-paint script grew by about 1.2 KB (the demo variant helper in `core.js`); everything else is in the lazy node, settings, epic and launcher bundles.
- The unknown-outcome sentence replaces "Could not confirm whether <node> got this" in the steer panel and the tail tile, and "unconfirmed: ..." in the launcher. The node page's banner for a refused token now reads "<node> removed this pairing, pair again."
- Known limits, written in the README: nothing here has run on real machines (a real sleep and wake, a rotation race and a deploy outage are for the two-node check, #155); the Activity list shows the hub's receipt time next to the row time only where the board sends one, and it sends none today; a Claim or Work button comes back after an unknown outcome because a repeat is idempotent.

### Fixed
- Rotate token, three ways: a node that was rotated while its answer was lost no longer looks healthy (a good poll inside the 60 second grace, or a failed one, used to clear `needs_repair` and hide Re-pair; only pairing the node again clears it); a node that is asleep no longer forces a re-pair (when the connection never came up nothing was sent, so the pair stays healthy with the old token valid and the answer is "try again later"); and two presses at once send one rotation, the second is told it is busy. Rotate token is also off, with its reason, while the node is offline or stale.
- Update address now fails closed: it goes ahead only when the old address is clearly not this node (connection refused, name gone, a 404, or another node's answer), and a timeout, rate limit, server error, redirect or broken answer there refuses with "still answers or could not be checked, try again". It names the Tailscale device of the new address when discovery has it, and no longer brings back a node you removed while it was checking.
- The cap of 12 boards that may call this one now also applies to pairing both ways, and a re-pair that replaces the pair of the same node at the same address is let in at the cap.
- A write that gets a server error (5xx) or a broken answer is shown as an unknown outcome with a link to the node, like a timeout, and is still never retried. A refusal reason from a newer node that this board does not know no longer counts as GitHub trouble in an epic run: the issue is held and the node keeps working.
- A test that cleared the shared claim-lost hooks removed another test file's hook (a hidden order dependence); it now removes only its own.

Upgrade: nothing to do.

## v0.5.49 - 2026-10-15

### Added
- Nodes P15, auto node routing by free lanes and rate-limit headroom, with a pin (#150). **Run** on an epic page sends each ready issue to the node that can take it now, one at a time and never above the free lanes, and shows why ("chose node-b: 2 free lanes, 5-hour window 31 percent") or "waiting: <reason> until <time>" instead of a spinner. A node is a candidate only if it is online, holds the `tasks` scope, has the repository and the issue's agent installed and logged in, is not in a back-off, has its window below the limit (an unknown window counts as 50 and says so) and has a free lane after this board's reservations; then more headroom, more free lanes, lower load and the node least recently used decide. A pin (the page's, a `ccboard:node:<alias>` label, the epic's) overrides the ranking and never a filter: a pin to a node that fails one waits and says why. It needs `CCBOARD_EPIC_WRITES=1` and a paired node; without them Run is off with its reason and no thread, row or key exists.
- On the epic page: **Run** (the one filled primary, **Running** while a run goes), **Pause** and **Resume** as posts that stop or continue new assignments at once, a per-issue **Run on...** menu (Auto or a node, optimistic, refused in the board's own words), the choice or the wait on each row, and in the settings a **Window limit** (10 to 100, default `CCBOARD_EPIC_MAX_WINDOW_PCT`, 85), **Nodes on one subscription** (nodes that share one rate-limit window are counted once) and **Let MCP run this epic** (off by default). A lane is reserved in memory at dispatch until the node's next reading or two poll cycles.
- The launcher's node picker has an **Auto** cell for a new task: it shows the node and the reason in the command preview before Start, then is that node's own form; when no node can take it, it says "waiting: <reason>", lists the nodes left out and why, and offers Check again.
- Two MCP tools (#152), now nine in the stdio server and in `/mcp`: `epic_status(repo, number)` reads an epic, and `epic_run(repo, number, max_issues?)` starts its run, only when a person turned on **Let MCP run this epic** for that epic, the token has `tasks`, this board's `CCBOARD_MCP_NODES` is `tasks`, at most 10 issues a call and 6 calls an hour per token, every call audited as `mcp:<token name>` or `local-token`. No tool takes a permission, sandbox, bypass, arguments or keys property. Settings > Agents says it in words.
- `?demo=1#/epic/example/website/147` shows Run; `&epic=run|runwait|runpaused|runagent|runfinished|runlimit|nonodes|paused|groups|writesoff` show each state, `&run=waiting|claimed|busy|notfound` plays a refusal of Run on..., and a new task in the demo launcher shows the Auto cell (`&auto=wait` plays the wait).

### Changed
- Pause and Resume on the epic page are posts to `/pause` and `/resume` (Resume wakes the loop) and no longer a settings write; menus are at least 240 px wide. The first-paint script grew by 174 bytes (one demo line in `core.js`); everything else is in the lazy epic, launcher and settings bundles.
- Known limits, written in the README: nothing here has run on two real nodes yet (the two-node check, #155, does that), which stored field is a stable subscription key is not verified (the manual group is the safe way), routing never picks this board, and a run an agent started reaches the person-only work route under the label `run for mcp:<token name>`.

### Fixed
- A run an agent started now stops when a person takes the permission back: turning off **Let MCP run this epic**, lowering `CCBOARD_MCP_NODES` below `tasks`, turning GitHub writes off, or revoking the device token that started it ends the run as stopped at its next step, before the next issue is sent, with an audit row that says why. A restart does not bring it back. A run a person started is not ended by any of these, but it now also sends nothing while GitHub writes are off.
- One node answering "too many requests" no longer holds up every run for up to an hour: only that node is skipped until its wait is over and the next issue goes to another node. Stopping the board no longer waits out a pending wait between calls.
- A lane cap a node reports as negative, or not a whole number, is now "lanes not known" and not "no limit", and a window percentage outside 0 to 100 counts as unknown (50) when routing picks a node.

Upgrade: nothing to do.

## v0.5.48 - 2026-10-12

### Added
- Nodes P14, run a claimed epic issue as a task on its node and report back (#149). **Work on <node>** on the epic page asks a paired node to claim the issue, start it as a task in its own worktree with the issue's own model line and keep the issue informed (`POST /api/nodes/<handle>/epics/work`, a signed-in person, scope `tasks`, never an agent). The author check is the first guard: the issue and its epic must be written by a login in `CCBOARD_EPIC_AUTHORS` (or the node's own `gh` login), or the node refuses before any claim comment exists. A model line that names bypass, `danger-full-access` or `--yolo` is refused with one comment and the claim is released as failed; an issue with no model line uses the node's defaults, never bypass. The issue text goes into the prompt as data, never into a shell or a launch line.
- Short phase comments in a fixed template (waiting for a person, pull request opened, done, failed, cancelled), at most one per issue every 5 minutes and only on a change of phase, each checked against the claim just before it is sent. The epic's settings, all off by default and kept per epic: what is reported (phases only, summary or none), whether a done task is pushed to its own branch and opened as a pull request (never `main`), and whether that pull request says `Closes #N`. A summary is cut to 1500 characters after one redaction pass (home folders become `~`, token shapes become `[removed]`, with a note when text was removed).
- The epic page shows **Work on <node>** (the one tinted action of a row), **Try again on <node>** after a failed attempt, the phase of the work with **Open on <node>**, a mark on issues by an author who is not allowed (their **Work** is disabled and names the author, and the line under Run names them), the three settings with one paragraph that says in words what will be posted and pushed, and a plain sentence for every refusal. The task card links the issue and its claim comment and shows a lost claim as its own state, with a glyph.
- `?demo=1#/epic/example/website/147` shows Work, the phases and the settings; `&epic=workrun|workwait|workfail|workcancel|authors|epicauthor|noauthors` and `&work=nothere|author|epicauthor|unreadable|bypass|posture|lanes|launch|taken|lost|unknown|writesoff|closed|done|attempts|offline|scope|unconfirmed` show the other states, and `?demo=1&taskepic=running|waiting|pr|failed|cancelled|lost#/tasks` shows the task card for each phase.

### Changed
- The epic setting `report` is now one of `phase` (default), `summary` or `none`; a stored `true` is read as `summary` and `false` as `phase`. `close` is new. **Claim on <node>** becomes a bordered button on a row where Work is offered, so each row keeps one tinted action.
- The first-paint script grew by the task card's claim link, the lost-claim chip and one demo route (about 2 KB); everything else is in the lazy epic bundle.
- Known limits, written in the README: nothing here has run against real GitHub, a real `claude` or `codex` or a real worktree yet (the two-node check, #155, does that), and there is no route for a board to work an issue on itself, only on a paired node. Only the issue's author (and the epic's) is checked, so someone with write access can still edit an allowed author's issue before it is run.

### Fixed
- Hardening of the epic work, in plain words: a phase comment (done, failed, cancelled) is never posted twice, even when the claim cannot be released for a while and the node tries again every five minutes; the commit and push of `auto_pr` run no repository hook script and use `--no-verify`, because an agent could have edited a hook and nobody has read the diff (the **ship it** chip a person taps is unchanged); the pull request is opened for the issue's own repository (`--repo`) and only when the worktree is really on the task's own branch, otherwise nothing is committed or pushed and the done comment goes out without a link; two Runs of one issue at the same moment start one task, not two; the Stop hook of a finished task no longer waits for a report that is busy with GitHub.
- A result posted to an issue is cleaned more: credentials in a link, email addresses, `/root`, Windows and spaced home folders, `/var/folders` and `/private/var`, `password=`, `token=`, `secret=` and `api_key=` values, and host names ending in `.ts.net`, `.local`, `.internal` or `.lan` are removed. The prompt scan strips HTML comments in one pass (an issue made of thousands of `<!--` no longer takes seconds) and also drops hidden link-reference comments (`[//]: # (text)`), so the agent reads only what a reviewer can see.
- The README says that a change to an epic's settings applies to work started after it.

Upgrade: nothing to do.

## v0.5.47 - 2026-10-12

### Added
- Nodes P13, claim an epic issue (#148). A node takes an issue of an epic with a comment on it, a lease and a fence, using only GitHub and GitHub's own clock: it posts its claim, waits and reads twice without cache, and wins only if it is first in both readings (ordered by `created_at`, then the lower comment id); a loser deletes its own comment. The holder renews every 5 minutes by editing its own comment, a claim not renewed for the lease (30 minutes, `CCBOARD_EPIC_LEASE_MIN`) can be reclaimed, and after three failed attempts the nodes stop and ask for a person. Every later write goes through `fence_ok`: if the claim was lost, nothing more is written, an audit row is made and the Epic page says so (parking a task `lost-claim` comes with the run phase, #149).
- **The epic claim writes are off by default.** `CCBOARD_EPIC_WRITES=1` lets a board make them: a comment of its own, itself as assignee and the label `ccboard:in-progress`, through a separate writer (`app/github_write.py`) with a closed list of eight request shapes, one write at a time, 1.1 s apart, 30 a minute at most, and a back-off on `403` and `429`. Off, this writer builds no `gh` command at all, not even the login check. The switch governs only these claim writes: the pull request actions a person starts on a task (Create PR, Merge, comment) are separate and are not governed by it. `CCBOARD_EPIC_ALIAS` names the board in its comments (default: a short neutral word from the node id, never the host name); the comment is a fixed template and never free text.
- `POST /api/nodes/<handle>/epics/claim` (a signed-in person, scope `tasks`, never an agent) asks a paired node to claim an issue; both boards write an audit row with the repository and the issue number only. The Epic page shows **Claim on <node>** on an unclaimed issue pinned to an online node that holds the `tasks` scope, only when GitHub writes are on, and says each refusal in plain words (taken, lost, claim not known, writes off, closed, already done, attempts used up, offline, scope, unconfirmed). It also shows this board's own claims (held, renewing, lost) and says a lost claim once with a toast.
- Doctor: **Claiming epic issues** says in plain words whether this board writes to GitHub and, when it does, checks the `gh` login and `repo` scope, push access to the epic repositories, the alias (a warning when it looks like a host name) and the lease. Settings > Agents shows the writes switch and the alias read only, with a sentence each.
- `?demo=1#/epic/example/website/147` shows a held claim, a lost claim and a **Claim on node-b**; `&epic=writesoff|offline|notasks` and `&claim=taken|lost|unknown|writesoff|closed|done|attempts|offline|scope|unconfirmed` show the other states.

### Changed
- The first-paint script grew by 30 bytes (one demo route line); everything else is in the lazy epic and settings bundles. The reader gained one more `GET` shape, `/repos/<owner>/<repo>`, for the Doctor.
- Known limits, written in the README: the tie-break and a killed node's reclaim are this board's own rules and are not yet measured on real GitHub (the two-node check, #155, does that), and anyone with write access to the repository can still forge a claim comment.

### Fixed
- Hardening of the claim, in plain words: an unknown `gh` login no longer counts as a lost claim; a login that cannot push to the repository posts no claim comment; a claim decision needs every page of comments confirmed by GitHub in that read; a node takes at most four claims at once and a write that waits too long in the queue is refused as busy; an epic reading makes its issues claimable only for an epic with the `ccboard:epic` label and only for 15 minutes; and a held claim is never dropped from the claim index when old lost ones pile up.

Upgrade: nothing to do.

## v0.5.46 - 2026-10-11

### Added
- Nodes P12, the epic view (#147). **Tasks > Epics** lists the issues labelled `ccboard:epic` in the repositories of this board's own projects, and `#/epic/<owner>/<repo>/<n>` shows one epic: its sub-issues in GitHub's queue order, a progress bar for each node (done, running, queued, failed, in words and glyphs, never colour alone), the node that holds each issue, its claim ("not live" when it was not renewed), the task phase on that node (an offline node shows its last phase and age), the pull request, and a **Pin** menu per issue. Entry points: the Tasks switch, an **Epics** link in the project header, and the palette (`epic`, `epic owner/repo#147`). **In this version the page only reads GitHub**: **Run** is shown disabled ("available when routing is installed") and nothing is claimed, started or commented.
- **Pause** or **Resume** and the pins are kept on this board only (`PUT /api/epics/<owner>/<repo>/<n>/settings`, a signed-in person with the CSRF header; a pin must name `local` or a paired node and an issue of that epic the board has read, and a pin to a node unpaired later shows as ignored). A label `ccboard:node:<alias>` on a child is read as a pin; the page pin wins. Each change paints at once and a refusal puts it back with the board's words.
- A read-only GitHub reader (`app/github.py`): `gh api` with `GET` only, an argv list, strict owner, repository and number checks before anything is run, a 20 s timeout, a 2 MB cap, ETag and `If-None-Match`, a 60 s cache, and a back-off kept in memory when GitHub says wait (the page says "GitHub asked us to wait 40 s" and keeps the last read). The `gh` token is never read or returned, and everything GitHub sends (titles, labels, comments) is treated as untrusted text: capped, stripped of control and bidi characters, and shown with `textContent` only; links are built from the checked owner, repository and number.
- Claim comments (`<!-- ccboard:claim v1 -->`, written by a later phase) are read for display. One counts only when its author is a user listed in the new `CCBOARD_EPIC_AUTHORS` (empty: the login `gh` is authenticated as on this board) **and** GitHub says they can write to the repository (`author_association` owner, member or collaborator); the repository owner is not trusted by being the owner, and with no login known no claim counts. Anyone else's is shown as "not from an allowed author". Doctor: **Epics from GitHub** (`epic-gh`) checks the `gh` login, the `repo` scope and read access, read only.
- `?demo=1#/epic/example/website/147` shows a seven-child epic across two nodes (one under **Claim not known**); `&epic=nologin|noaccess|notfound|wait|stale|labels|empty|norepo|none|down` shows each degraded state.

### Fixed
- This board asks GitHub only about the repositories of its own projects: nothing a paired node reports (a task's issue reference, a slug) and nothing inside a GitHub answer (a `repository_url`, `depends on owner/name#N`, a sub-issue in another repository) chooses a request, an epic in a repository with no project here is a 404, and a node's task joins only a child already read by its exact number and repository.
- A cap is never an answer of "free". Each issue's claim is claimed, none (every comment read: **Unclaimed**) or unknown (**Claim not known**, with "too many comments to read", "GitHub asked us to wait", "not read yet in this refresh", "in another repository" and so on, counted apart from Unclaimed). A long thread is read from both ends, 100 comments each. An epic with 100 or more issues says "more issues than shown", and so does the list for more epics or repositories than it reads.
- "Unclaimed" is a statement about a moment. It needs every page of the thread confirmed by GitHub in that refresh (fetched, or a `304`), read as one steady reading (the issue's comments count and `updated_at` the same before and after, ids strictly increasing, one retry, then "changed while it was read"); a cached page, a stale entry or the last good answer is "not read recently", and each open row says when its claims were read. `fresh_claim_state()` is the uncached check a later phase must use before claiming. Cost: one extra `gh` call per child with comments, inside the existing budget.

### Changed
- The first-paint script grew by about 4 KB (the Epics switch hook on the Tasks page, the `#/epic/` address in the router and one demo dispatch line); the epic page, its list and its palette rows are the lazy `epic` bundle. A board with no GitHub issue task, no paired node and no epic visit keeps its Tasks page and palette byte for byte.

Upgrade: nothing to do.

## v0.5.45 - 2026-10-11

### Added
- Nodes P11, notifications across nodes (#151). Once a node is paired (either direction), every notice a board sends starts with its own name, `node-a · ◆ shop/api · s1: needs you`, and its tag becomes `node-a:<session>` (`node-a:rate-limit`, `node-a:login-problem`, `node-a:test`), so two nodes with a session of one name no longer replace each other on a phone. The name is this board's own, never a peer's; with no paired node every title, body, tag and link is byte for byte what it was (pinned against strings captured before the change). The first pairing changes the tags, so one notice may show again.
- Doctor: **Notification links** warns when a node is paired and `CCBOARD_PUBLIC_URL` is empty (links in notifications are relative and will not open from another device), with the fix; **One subscription on two nodes** says when two nodes work under one Claude or Codex login (the `subscription_key` digest the card already carries), because both send a rate-limit notice for the one window, and to turn the Rate limit switch off on one.
- Settings › Notifications has a row for each paired node, "Notifications from node-b: subscribe this device on that node", with **Open node-b** (that node's own Settings, a new tab, only for an https tailnet address); the page says it can only see its own subscription. The Home strip shows one line for an account window that is used up, shared by the nodes under one subscription, and the Inbox and Home group "On other nodes" now says `needs input` or `error` for each item and marks a node's items stale while it is not online. `?demo=1` shows all three.

### Changed
- The Inbox group is keyed by node id, session, kind and time, so a remote item is one row however many polls list it and goes when the node's next reading drops it. The hub never calls Web Push, ntfy or the badge for a remote item (a test fails if any hub module imports a sender); the app badge stays this board's own count.

Upgrade: nothing to do.

## v0.5.44 - 2026-10-11

### Added
- Nodes P10, a session on another node in the browser (#144). **Open terminal on <node>** on the node page, the peek, the Inbox permission cards, the quad tile and in the palette: it opens `<the node's address>/term/<session>` in a new window with `noopener`, straight from your browser, only for an https tailnet address and a valid session name, with a link to tap when no window opens, and it says when the node is offline and keeps the link. Nothing is sent to the hub; the browser needs a Tailscale identity on that node.
- A session of another node as a tail tile in the quad and in the terminal dock: the node chip, the last 12 lines from the stream relay (read only), a one-line prompt box that sends through the `prompt` relay row and shows each steer refusal as its plain sentence, `offline since 09:14` or `re-pair` with the last lines kept, and never a framed page. An empty tile's pick list groups sessions by node, the quad stores `{node, tmux}`, `o` on a focused remote tile opens its terminal, and the demo (`?demo=1`) shows all of it.
- Nodes P10, MCP across nodes (#152, part 1). The six MCP tools take an optional `node`; `list_nodes` lists the paired nodes from the hub's last reading (handle, name, status, age, free lanes, ready agents, and what this board's level allows). Off unless `CCBOARD_MCP_NODES` is `read` (list nodes, read their projects and tasks; **`1` keeps meaning `read`**, the safer reading of the old value) or `tasks` (also create a task and start it in a new session there); both the level and the token's scope and the pair's scope are needed, and an agent can never type into a session, answer a permission, read a screen or hand a task to a running session. Every call is audited on both boards as `mcp:<token name>` or `local-token`, with the node.
- Settings > Agents shows `CCBOARD_MCP_NODES` read only next to the MCP block, as its level in words (off, read only, read and start tasks) with one sentence on what it allows.

### Changed
- `RELAY` rows are still human-only by default; four carry `mcp` (`state`, `task`, `task_create`, `task_dispatch` in lane mode; no tool reads the card or the agents, so those two stay a person's) and a test walks the table to prove no other row is reachable from an MCP token or the hook token. The peek's `o` key still opens the node's own board; the quad's `o` opens the terminal.
- What an agent may send to a node is checked at the hub route, not only in the tool: the hook token or a device token calling `/api/nodes/<handle>/...` directly is read with a body of exactly the fields its tool sends (`create_task`: project, repo, title, prompt, when, agent, auto_close; `dispatch_task`: agent, auto_close, always a new session), made from the same table as the tool schemas; `model`, `effort`, `issue_ref`, `mode`, `session`, options and any query are a `422`.
- What a node says is untrusted data for the agent that reads it: every tool answer for another node is `{node, age, untrusted, notice, data}`, free text is cut (title 120, error 160), single-line, free of control, bidi and format characters, with `<`, `>` and backticks replaced, names and branches that do not fit their shape are `null`, and every tool description says so. The stdio shim and `/mcp` share the code.
- The stdio shim holds only the scopes the level grants (`read`: read; `tasks`: read and tasks), not every scope; an agent gets a reduced answer built field by field (no account or login detail, usage window, version, address, node id or audit data), and `list_nodes` shows less (no usage windows, scopes of the pair, lane caps or login problems).

Upgrade: nothing to do.

## v0.5.43 - 2026-10-11

### Added
- Nodes P9, steer a session on another node (#142). Six relay rows: `prompt`, `keys`, `ack`, `close` (scope `sessions`) and `permissions`, `permission_answer` (scope `permissions`). All are human-only and need an acting user; the hub and the peer each check the text, the key, the target and the scope. A permission is answered by a signed-in person only (the hook token, a node token and a node user get a `403` and an audit row), `allow` or `deny` only, and no relay row, MCP tool or job can reach it.
- On a remote session's peek: a Reply box with the session's quick replies, **Ack**, a **Keys** panel (Enter, Esc, Up, Down, Tab, y, n, 1 to 9; C-c after a second tap) and **Kill session on <node>** (two taps). A permission request of a node is a card (tool, summary, **Allow**, **Deny**) in the Inbox and Home group "On other nodes", on the node page and on the peek, and in the palette; the palette also has "Reply to <session> on <node>". The demo (`?demo=1`) plays every refusal.
- Every action paints at once and toasts with the node's name; "Allowed on <node>" appears only when the node confirmed, a refusal puts the card or the draft back with a plain sentence, and a timeout reads "Could not confirm whether <node> got this" and is never retried.

### Changed
- A key or a line cannot answer a permission or any dialog from another node (the `permissions` scope is the only way, and `y`, `n`, digits and Enter are refused while a question is asked); ack, close and a permission answer check the session is one of the node's own agent sessions, and an answer is limited to ordinary tool requests, the same ones the list shows. A session started with bypass, `auto` or `dontAsk`, or whose mode cannot be read, cannot be steered from another node; a remote prompt, or the first prompt of a new remote task, cannot start with `/`, `!` or `#` (a `422` on the hub and on the node; a stored card that does is not started from another node), because the terminal reads those as commands. The node page no longer says that answering, typing and closing are for a later phase.

Upgrade: nothing to do.

## v0.5.42 - 2026-10-10

### Added
- Nodes P8, start work on another node (#141). Three relay rows with the `tasks` and `sessions` scopes: create a task (now, or as a Backlog card), hand a Backlog task to a new session or an idle one, and open a plain session. The hub and the node each run the strict body and `guard_launch` (never bypass, arguments, tools, directories or a first prompt for a session; `after_task_id` is refused: "chains stay inside one node"), each writes an audit row, and the task row on the node carries `origin` (the calling node and the login it reported). A write that times out is "unconfirmed" and is never retried.
- The launcher has a **Node** control (`This node | build-box`, a native select above four) when the hub view is on and a node is online. A node swaps in its own projects, repos, agents, models and efforts (the agents row, cached 10 minutes per node), hides every danger option ("Bypass is only available on the node itself."), remembers its choices per node and repo, paints the row at once as "Starting on <node>", and toasts "Started on <node>" with **Open on <node>** and the node's limit warning. Start is off with the reason when the node is offline, needs a new pairing or lacks the scope.
- **New task here** and **New session here** on the node page are live when the pair holds the scope, and a Backlog task's peek has **Start on <node>**. The demo (`?demo=1`) plays all three.
- Nodes P8, the stream relay (#143). `GET /api/node/stream` (scope `sessions`) and `GET /api/nodes/{handle}/stream` (a signed-in person only): the hub parses the peer's stream, re-emits only `lines` and `tick` (cut to the line and width caps, then redacted, on both boards) and always ends with a `gone` event that says why. At most 2 streams per node and 8 per hub, a 60 s idle limit, 64 KB per line, closed when the browser leaves, the pair is removed or the board stops. No new dependency.
- `Live.subscribe(ref, fn)` takes a remote `Ref`: one `EventSource` per node, a `gone` keeps the lines and reads "offline, last seen 09:14", and the page reconnects after 5, 10, 20 and at most 30 seconds. A remote session's peek shows its **Tail** when the pair holds `sessions`.

### Changed
- The README says a tail can hold secrets the agent saw: `sessions` lets everyone signed in on the calling board read every session's tail, and the redaction is best effort. The scopes and relay tables list the new rows; the node page no longer calls itself read only.

### Fixed
- A task handed to a running session on a node (mode `session`) is checked against that session, not the card: the session must be one of the node's own sessions in the task's repo, and the line the board recorded for it must say a Claude permission mode of `manual`, `default`, `acceptEdits` or `plan` (Codex: `-s read-only|workspace-write` with `-a on-request`). A session started with bypass, `auto` or `dontAsk`, or whose line says no mode, is refused with `that session runs with wider permissions than another node may use` (`409`).
- A launch from another node never inherits a local default: a request that leaves the permission out gets `--permission-mode manual` (Claude) or `-s workspace-write -a on-request` (Codex) on its line, a card keeps its own narrower choice, and `CCBOARD_SUBAGENT_MODEL` is not applied. The options the launch ends up with go through the guard on the node, and a line that does not say its permissions inside the allowed set is refused before a session exists.
- A prompt or title with a control character (Ctrl-C, Escape, a lone CR) is a `422` from another node, because the prompt is typed into a terminal; the issue reference is an owner and a repo name in the GitHub shape; a peer's answer keeps only fields of the expected shape (a slug, a branch, a session name, a phase); the origin is a plain label; two session opens cannot choose the same automatic name.
- The launch-line check is an allow list now: a line is accepted only when it is exactly what the board's adapters write (canonical quoting, the board's own `env` names, a table of known flags with value rules, each once), the same function for the target session and for the final plan of a remote launch. Every string field of a remote request has an anchored rule (one helper for the hub, the peer, a stored card and stream names), and free text refuses format and line-separator characters too.
- The stream relay holds at most 6 streams per signed-in person (and still 2 per node and 8 per hub), so one person cannot take the hub's share.

Upgrade: nothing to do.

## v0.5.41 - 2026-10-10

### Fixed
- The Doctor's `tmux-socket` row now sees a tmux server on Linux (#202). It used to know only the macOS process title `tmux: server (<path>)`, so a Linux server whose socket file was gone was not found. It now also reads the command line (`tmux -L <name> ...` or `-S <path>` with a command that starts a server, the daemon before any client), and the existing warning and its `kill -USR1 <pid>` fix text are unchanged. Nothing is signalled. A service that runs with another `TMUX_TMPDIR` than the doctor can make the "socket file gone" warning untrue.
- Codex's dead-login signal rests on an observed capture now (#68). `tests/fixtures/codex_exec/auth_failed_0161.jsonl` is a real `codex exec` run with no login (codex-cli 0.161.0, ids scrubbed) and replaces the invented fixture. A headless run's text is the final `turn.failed` message ("unexpected status 401 Unauthorized: Missing bearer or basic authentication in header") instead of twelve retry lines, and a 401 on a retry followed by a different failure is not a dead login. `codex exec` cannot tell no, empty and garbage `auth.json` apart; only `codex login status` does. A login the server rejected is still assumed, not captured.
- A Claude session parked on a rate limit no longer reads as an error (#71). Its badge, Inbox card and Home row say "limit reached, continues at HH:MM" (a weekday in front when the reset is more than 20 hours away), "limit reached, auto-continue is off" or "limit reset, continuing shortly"; a task card's owner chip says "limit reached". `/api/state` sessions carry `parked`, and a reset time is promised only when the board will act on it.
- The Home banner after a restart no longer says "Claude" and "--resume" for a recovered Codex row (#93): "After a restart, 2 Claude and 1 Codex sessions relaunched: a, b, c", or "After a restart, 1 Codex session relaunched: <name>". The recovery record now names each session's agent; an older record names none.
- The WSL keep-alive script (`scripts/windows/ccboard-wsl-keepalive.ps1`) now says "No WSL distro is installed" when WSL is there but has no distro; it used to say "wsl.exe was not found". The new `check-windows` CI job found it on its first run.

### Added
- Settings > Accounts on a Mac has its own **Log in** button when saved logins are off, Claude Code is installed and not logged in (#119). It does what the Home banner's Log in does, which starts nothing on a Mac: the toast says "Run /login in any terminal; the board notices within a minute."
- The Preview button says why it cannot work before you click (#126). `state.preview` is `{available, code, reason}`; when it is false the task card shows a disabled Preview and "unavailable: <reason>" (`CCBOARD_PUBLIC_URL` is not set, Tailscale on the Windows side of WSL2, Tailscale missing, not running, not logged in, or the last `serve` was refused within 5 minutes). It runs no `serve` to find out.
- The macOS installer installs the claude-mem plugin (#122): `claude plugin marketplace add thedotmack/claude-mem`, then `claude plugin install claude-mem@thedotmack`, each limited to 300 seconds (`CCBOARD_MACOS_PLUGIN_SECONDS`), skipped by `CCBOARD_CLAUDE_MEM=0`, a no-op when the plugin is already listed, and never a reason to fail.
- The macOS installer checks before it builds (#128): it asks Homebrew which missing tools would build from source, stops with "the Xcode Command Line Tools are not installed, and these would be built from source: ttyd. Nothing was installed." when the tools are missing, and `scripts/macos_tools.py bottle --explain` prints the measured ttyd finding.
- A `check-windows` CI job (#123) runs a PowerShell parse check, PSScriptAnalyzer and a Pester test of the WSL keep-alive script on `windows-latest`. It is advisory (`continue-on-error`), read only, skipped for docs-only changes, and gates nothing.

### Changed
- `CCBOARD_MACOS_INSTALL_TOOLS=1` is a yes for bottles only. A source build needs a "y" on a terminal or `CCBOARD_MACOS_ALLOW_SOURCE_BUILD=1` as well; an unattended Intel run that used to pass with only `=1` (tmux is a source build there) now stops and names both variables. An optional tool that would build from source is left out with a warning.
- README: a "Where ccboard runs" section with one support line per system, linked from the top and from Install; the corrections from the device check on an Intel Mac with macOS 14 (#130): ttyd has no bottle there and is a source build of more than ten minutes that upgrades 11 packages, the Mac's tmux socket limit is 103 bytes, a `unix:` serve target answers 502, `SSH_AUTH_SOCK` is set in `gui/<uid>` jobs only (#129), the first https request after `serve` waits for the certificate, a socket file is not proof of a server, and a same-Mac request through the tailnet name carries identity. The Tailscale App Store row reads "works as the logged-in user, no sudo, no operator (measured 1.102.4)". Mac survival and Keychain rows all say "to verify".
- README launcher: `--fallback-model` (up to 3) and `codex fork <thread id>` work in an interactive session; a fork does not carry model or reasoning, and the board binds the new thread id after the first prompt (#92).
- Help text only: a headless `permission_mode` error now ends "(bypass is never allowed for unattended runs)" instead of the stale "(bypass only inside a devcontainer)" (#104; the owner decision on bypass inside a devcontainer is still open), and the Doctor's socket fix text says 103 bytes on macOS.

Upgrade: nothing to do.

## v0.5.40 - 2026-10-10

### Added
- Nodes P7, the allowlisted relay (#140). `RELAY` in `app/nodes_relay.py` is the one table through which a hub may ask a peer for something: each row has a scope, a strict body model, a rate class and an audit action, and adds a hub route (`/api/nodes/{handle}/...`) and a peer wrapper (`/api/node/...`, a node token with the scope). The first rows only read: the card, the state, one task's detail (a fixed list of fields, never its prompt or result), the agents the launcher can offer, and the last 40 lines of a session's screen.
- The screen tail needs the `sessions` scope, never `read` (which stays names, titles and counts). It is cut to 40 lines before anything reads it, then redacted on the peer and again on the hub: key blocks, the board's own tokens (also when tmux wrapped one over lines), URL credentials and connection strings, `Authorization`, `Cookie` and `X-...-Key` lines, `Bearer`/`Basic` values, provider keys (Anthropic, OpenAI, GitHub, AWS, Google, Stripe, npm, GitLab, Hugging Face, Slack), JWTs, `--token VALUE` flags and `NAME=VALUE` for names that hold a key, token, secret or password. Task titles and error text from a peer are redacted too. Best effort, and the README says so.
- Every relay row is human-only: a signed-in allowed person with `X-CCBoard`, at most 120 reads and 30 writes a minute. The local hook token that every agent session holds gets a `403`, so an agent cannot read other nodes through the hub; a later phase can open a row to it on purpose. The acting user sent to the peer is only the authenticated login.
- The hub refuses in a fixed order and sends nothing when it does: caller, handle (`local`, unknown, this board itself, a legacy row), the pair's scope (`needs the sessions scope on build-box`), the node's status (`re-pair`, offline with its age, or `not read yet` until a poll has finished), then the body. The call has a 10 s timeout, 256 KB and 512 KB caps and follows no redirect; the answer is `{node, age, data}` and every error has a stable `reason`.
- `guard_launch`, the one guard for every row that will start or steer an agent, runs on the hub and again on the peer (a permission mode beyond default, acceptEdits and plan, bypass flags, `args` and the tool and prompt fields, a Codex sandbox or approval beyond the safe ones, six dangerous spellings in any string, unknown agents, the shell launcher). Both boards audit each relayed call; `GET /api/nodes/audit` takes `direction`, `node`, `action` and `failures=1`, and Settings > Nodes > Activity has the filters.

### Changed
- A node token on a path it may call with another method now gets `405` with `Allow` (it was `403`), only after its token is verified (a bad token is still `401`). Every other path is still a `403`, and the walk test fails for a route that opens without a row.

Upgrade: nothing to do.

## v0.5.39 - 2026-10-10

### Added
- Nodes P6, the Mac and WSL2 as nodes. A Mac's card says `Darwin` and `launchd`, its cpu, memory and disk are null and show as "n/a" (never 0) on the node page and in Settings > Nodes, and "Saved logins are not available on a Mac. Normal login still works." A WSL2 card says `os.wsl: true`, gets a random `n_` id when no Tailscale client is reachable, and takes its address from `CCBOARD_PUBLIC_URL`. A platform chip with one short line sits beside the node on both screens; a Linux box shows nothing extra.
- Discovery on both: the Mac finds the Tailscale app's own binary. In WSL2 with no Tailscale in the distro the list says "Tailscale runs on the Windows side: type the other node's address in Pair a node" and Add node stays available. `tailscale.exe` through Windows interop is used only with `CCBOARD_TAILSCALE_PLACEMENT=host` (off by default, to verify).
- Four read-only Doctor rows on a Mac or in WSL2 only: `node-mac-public-url`, `node-sleep` (a sleeping Mac shows as stale; the fix names `caffeinate -s`, to verify), `node-wsl-keepalive` and `node-gh`, each with a fix text.
- README "Several devices": a support table with one line each for Linux box, Linux container, Mac, WSL2 and native Windows, kept equal to a list in the code by a test.

Upgrade: nothing to do.

## v0.5.38 - 2026-10-10

### Fixed
- A Claude session no longer stays on "Claude needs your permission" after you answered "In terminal" and dismissed Claude's dialog with Esc (#193). Claude sends no hook for that. The board now reads the pane only for a session that waits on a permission you answered "In terminal" with nothing pending since: when the pane shows "Interrupted · What should Claude do instead?" directly above Claude's own input box, the session goes to idle on the next scan and the Needs-you count drops. Text a program prints can never clear a wait on its own, a new request landing mid-scan keeps the session waiting (one conditional write), and there is no timer.
- The launcher sheet stops a fourth fallback model before the round trip (#92): "Up to 3 fallback models." under the field, Start off, and a preview that does not show a command the server would refuse. Duplicates count, as the server counts them.

Upgrade: nothing to do.

## v0.5.37 - 2026-10-10

### Added
- Nodes P5, the hub view in the UI. Once a board is paired, Home gets a Nodes strip after the away line: this board first, then one cell per node with its name, a glyph and a word for its state (online, stale 3 min, offline since 09:14, re-pair), live sessions, how many need you and the 5-hour pill. A tap opens the node page `#/n/<handle>`, and a line above the cells counts what waits on other nodes ("2 need you on build-box").
- The node page shows the node's name, status and the age of the reading, system and version, Open board (a new tab), then Needs you, Sessions, Tasks, Repos and Account windows. A row opens a read-only peek (`#/n/<handle>/s/<name>`, `#/n/<handle>/t/<id>`) with the row's fields and Open on that node. Controls that need the relay (send, answer, close, new task) are shown but off, each with its reason, for example "needs the sessions scope on build-box".
- A node chip (a two-letter monogram and the name, neutral, never an agent or state colour) marks the rows of other nodes; this board's own rows wear a dim one once a node is paired. Other nodes' items that need you are listed under "On other nodes" in the inbox and on Home, and Tasks gets an All nodes / This node / each node filter with the other nodes' tasks, read only.
- The sidebar keeps "This node" and adds one collapsed group per node with its needs-you count and up to 20 sessions. The command palette gets a Nodes group (Go to, Open board on), remote sessions and tasks with their chip, the `@node` prefix (`@build fix` narrows to one node) and `g n` goes to the first node. Settings > Nodes shows the hub's reading under each paired node (state and age, clock off by more than 5 s, agents, accounts).
- `?demo=1` shows three nodes: online, stale with a clock warning, and offline.

### Changed
- The hub view is a lazy bundle that loads only when a node is paired (or for a `#/n/` address): a board with no node paired loads nothing new, asks nothing at `/api/nodes*` and draws every screen as before. The page asks `GET /api/nodes/state` on its own address every 6 seconds while the tab is visible, with `If-None-Match`, keeps the last reading when the board does not answer, and says how old it is. A node's address becomes a link only when it is an https tailnet name or address.

Upgrade: nothing to do.

## v0.5.36 - 2026-10-10

### Added
- Nodes P4, the hub read model: every paired board answers `GET /api/node/state` (projects and repos, sessions, tasks, needs-you counts, usage windows, lanes; no prompt, reply, result, transcript, account label or path) to a node token with scope `read`. It is cut to 200 sessions and 200 tasks and 100 KB, carries a weak ETag, and answers 304 when nothing changed.
- The hub polls its paired boards in parallel (at most 4 at once, every `CCBOARD_NODES_POLL` seconds, 20 by default and 5 at least, 5 second timeout) and keeps the last good reading of each with its age: `GET /api/nodes/state` (and `?handle=`) says online, stale, offline, unauthorized or unpaired, why the last poll failed, and how far the other clock is off. A slow board never delays another, and an offline board keeps showing its last state.
- A board with no paired board still starts no thread and makes no request. `state.nodes` keeps its shape and `state.nodes_enabled` says whether any board is paired.

### Changed
- `CCBOARD_NODES` boards are polled by the same threads (still the summary with the hub token). A node's answer is rebuilt from a whitelist and its name, address and id always come from your list of nodes, never from what it says.

Upgrade: nothing to do.

## v0.5.35 - 2026-10-10

### Added
- Pairing two boards: Settings > Nodes > Create pairing code makes a one-time code (10 minutes by default, five wrong tries burn it) with the scopes you choose (read and tasks by default; sessions and permissions only if you tick them). On the other board, Add node takes the address and the code. Each board then holds a token for the other (`ccbnode_...`; the receiving board keeps only its hash), shown in Paired nodes and in "Who can control this node", with Rotate (the old token works 60 more seconds) and Remove or Revoke (works even when the other board is offline).
- A node token opens only the node routes on a closed list, each with the scope it needs; it is never accepted on the hook, MCP or any other route, and the hook token, the hub token and an MCP device token never open a node route. Every pairing step, refusal and call is in the Activity list, with no token or code in it.
- The older read-only fleet (`CCBOARD_NODES` with the hub token) keeps working, and its rows are offered for pairing.
- A pair is verified only when the address it names confirms that it is the board redeeming that code right now (`POST /api/nodes/pair/confirm`, answered only while that board's own Add node runs). The confirmation is bound to the address you typed: a board that forwards your pair request to the board whose code you hold gets "not verified", because that board's own address is not the one you typed. Someone holding a code who claims another node's id and address is paired as "not verified" too.
- A node id is only the word of the board at its address, so removing a node, an unpair, replacing an older pair of the same node and the list of found devices all ask for the same address as well as the same id: a board that claims another node's id from its own address cuts none of that node's pairs. Nothing is left quiet either: when a node pairs again from another address, its older pair keeps working but is listed as "Another pair from ... says it is this node too", and Remove asks the board which other pairs use the same node id (from another address, or never confirmed) and shows them as boxes to tick and revoke with it, none ticked by default.

Upgrade: nothing to do.

## v0.5.34 - 2026-10-09

### Fixed
- Several tasks queued to one busy Claude session in quick succession each get their own turn: the board types the next one only after the previous queued prompt has started, so Claude never merges two of them into one turn.
- Reopen resumes the task's previous conversation (Claude keeps a `--worktree` session's transcript under the repo's own project folder, and the board now finds it there); a close that waits for claude-mem says so on the card.
- A Codex dialog that was already answered no longer blocks prompts and restarts; a fresh launch behind a trust dialog reports the dialog, not "not ready".
- A scheduled run that produced its answer is `ok` with a "denied N tool calls" note even when one tool call was refused; `denied` stays for runs that could not do their work.
- A devcontainer launch on a host without the devcontainer CLI is refused with the fix, instead of typing a command that fails.

### Changed
- The Codex Tune panel offers only the current model generation and the reasoning levels the model accepts, shows Model first, and wraps on a phone instead of scrolling sideways.
- The Usage estimates disclosure counts the same sessions as the numbers above it.

Upgrade: nothing to do.

## v0.5.33 - 2026-10-09

### Added
- Settings > Nodes shows this node (name, node id, system, agents, free lanes) and, on Refresh, the ccboard boards found on your tailnet: your own devices and devices tagged `tag:ccboard` (`CCBOARD_NODE_TAGS`), never a device shared in from another user. Each row says whether ccboard answers there (`CCBOARD_NODE_PORTS`, default 443 and 8443). Finding a device gives it no access; pairing comes next.
- The probe only talks HTTPS to tailnet addresses, resolves a name once and connects to that checked address, follows no redirect, runs at most 4 at a time and reads nothing but the hello's three fields. Nothing is sent until you press Refresh. Doctor rows `tailscale-status`, `nodes-port`, `nodes-tagged-self`.

Upgrade: nothing to do.

## v0.5.32 - 2026-10-09

### Added
- Nodes, first step: every board has a stable node id (its Tailscale node id, else a random one kept in the data folder), a display name and a node card (`GET /api/node`: agents, accounts, load, free lanes; no secret, prompt, email or path), shown as `state.node`. `GET /api/node/hello` answers only `{app, api, node_id}` without sign-in, at most 30 times a minute per caller, so another board can find this one.
- One naming scheme for things on another node: `#/n/<node>/s/<session>` links and storage keys that never collide with a local session of the same name; local links, bookmarks and push links are unchanged. A link to a node that is not paired says so.
- `CCBOARD_NODE_LANES` (default 3) sets how many task lanes this board advertises as free; a dispatch over it still runs, with a warning. Doctor row `node`.

Upgrade: nothing to do.

## v0.5.31 - 2026-10-09

### Fixed
- A task handed to a session that is still busy waits for its own turn: it shows `queued`, is not credited with the earlier turn's result, and auto-close never closes the session in the middle of its turn.
- Closing a task session that left an uncommitted file answers Claude's "keep or remove the worktree" question with Keep instead of killing the session; Claude's idle notice no longer cancels a planned close.
- Reopen resumes the task's previous conversation when Claude still has it, and keeps the task's model, effort and permission mode.
- A scheduled run whose tool calls were all denied is shown as `denied`, not `ok`; `--bare` is refused (it cannot log in on a box signed in to claude.ai); a job with no schedule and no "run now" waits until it is started.

### Changed
- Board-started sessions are priced from Claude Code's own statusline figure; the price table gains Haiku 5.5, prices 1-hour cache writes at 2x input and Sonnet 5.5 cache reads at 0.20.
- The claude-mem worker 13.35.0 is the tested version; the memory API notes are updated from a box check.
- README: restoring from a backup (a drill on the box: database and one transcript folder in 3 s, everything in 10 s, a backup branch in 13 s), why the worktree hooks stay unregistered, the permission-hook probe, and what the devcontainer needs on the host. The shadow compose file no longer mounts live data.

Upgrade: nothing to do.

## v0.5.30 - 2026-10-09

### Added
- Codex tuning from the terminal page and the quad tile: model through Codex's own picker, and reasoning, approvals and sandbox by restarting the session into the same conversation (the board shows the exact command first and runs nothing until you press Restart). `untrusted`, `on-failure` and `danger-full-access` are never offered.
- A dead Codex login raises the same banner and notification as a dead Claude login, from Codex's own error item in the session log; `codex login status` texts are recorded from the box.
- Scheduled Codex runs keep their token usage, and a finished run's task card moves to Done.

### Fixed
- A Codex started by hand in the same folder no longer takes over an idle board session.
- Settings > Agents lists the Codex sessions started outside the board again (Codex 0.161 changed how it marks them).
- Board-started Codex sessions skip Codex's update dialog (whose default answer runs a global npm install); a trust or update dialog shows as needs you and is never answered by the board; a prompt is never typed into a pane that went back to the shell, and that session shows as errored.
- The launch preview for Codex now shows every flag the board passes.
- Deleting a scheduled job removes its runs, so a new job never shows an old job's history.

Upgrade: nothing to do.

## v0.5.29 - 2026-10-09

### Changed
- Pages load their scripts and styles on first visit: the first paint needs about 520 KB of script instead of 1.1 MB. The Usage, Quad, Memory, Settings, project and wizard pages, the launcher, the palette, drag and drop, the file tree and the terminal kit come in when first used, once; a page that cannot load says so with a Retry button. The service worker still precaches everything, so offline works as before.
- Lighthouse scores are recorded in the README: desktop performance 97 to 99, accessibility 100 everywhere. Mobile performance (73 to 86) stays under 90 as a stated exception: the largest paint waits for the shell's scripts on a slowed phone CPU.

Upgrade: nothing to do.

## v0.5.28 - 2026-10-09

### Added
- Auto-continue can be switched off per session: from the session's menu, the quad tile's menu and Tune panel, and the terminal page's strip. A session switched off shows a `no auto-continue` chip and gets no `continue` after a limit reset or after a power cut; the switch survives a board restart and the relaunch after a reboot. Settings > Notifications shows the board-wide setting.
- The charts on the Usage page can be read from the keyboard: each is one tab stop, the arrow keys move between days, sessions or heat-map cells, and the readout is announced.

### Changed
- The power-cut path is pinned end to end: a session that was working when the box went down is relaunched with its resume line and gets one `continue` once it is back at its prompt (checked with a drill on a sandbox board: tmux killed, board restarted).
- The board reads Claude Code's session registry from its files only; `claude agents --json` was checked on the box and adds nothing.

Upgrade: nothing to do.

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
