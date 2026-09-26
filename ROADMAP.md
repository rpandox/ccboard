# ccboard roadmap

Working rules: one phase at a time; each item is one commit; tick items as they land.

## v0.1 — skeleton ✅

- [x] Projects: list with branch / dirty / session count; create blank (git init); clone from URL; delete (kills its sessions, removes folder)
- [x] Sessions: unlimited per project, all concurrent; launchers: claude, claude --resume, claude --continue, shell; attach (ttyd link), kill
- [x] Open project in code-server
- [x] README.md, ROADMAP.md, package as tarball, git commit as v0.1
- [x] *(added during the build)* Claude login from the board: sign-in link + paste the code
- [x] *(added during the build)* Project folder holding one or more repos; sessions run inside a repo; sibling repos via `--add-dir`
- [x] *(added during the build)* Public GitHub repo

## v0.2 — attention layer (design for it, build later, in this order)

1. [x] Hook status engine: global ~/.claude/settings.json hooks (SessionStart, UserPromptSubmit, Notification with matchers permission_prompt / idle_prompt / agent_needs_input / agent_completed, Stop, StopFailure, SessionEnd) POST stdin JSON to the board; map cwd + $TMUX_PANE → session; per-session state working / waiting / done / errored
   - *as built:* `install.sh` merges async hooks (`bin/ccboard-hook`) and a statusLine command (`bin/ccboard-statusline`) into `~/.claude/settings.json` via `scripts/claude_settings.py`; the Notification hook has no matcher and the board routes on `notification_type`; `POST /api/hook` is authenticated by a 0600 token file instead of the Tailscale identity; resolution order is `CCBOARD_SESSION` env → `$TMUX_PANE` on the ccboard socket → unique session in the hook's cwd; states are idle / working / waiting / done / errored / ended
2. [x] "Needs attention" inbox at top, oldest first, keyboard jump to next
   - *as built:* derived client-side from sessions in waiting / done / errored that are not acked; `j`/`n` and `k`/`p` move, `Enter` attaches, `a` acks, `Esc` clears; the tab title shows the count
3. [x] Usage & limits strip at top: 5h block %, weekly %, burn rate, reset countdown, per-session model + context %. Sources: statusLine hook JSON (per session) + `ccusage blocks --json` (plan quota). Red banner when StopFailure reports rate_limit
   - *as built:* the 5h / weekly / spend percentages and reset times come from the statusline's `rate_limits` (account-wide, stored on every statusline event); `ccusage blocks --json --active` is polled every 2 minutes by a background thread for cost, burn rate and projection (install.sh installs ccusage under `~/.local` when npm exists); the banner clears with a dismiss button or after 5 hours
4. [x] ntfy push (self-hosted on tailnet) on done / needs input / rate-limited, with deep link and last assistant line
   - *as built:* install.sh installs ntfy from archive.ntfy.sh, binds it to 127.0.0.1:2586 and serves it on `NTFY_HTTPS_PORT` (default 8444); the board publishes over loopback on waiting / done / errored / rate limit with a `#s=<session>` deep link, a "Terminal" view action and an "Ack" http action; one push per session and state per minute; the 🔔 header button shows the subscribe URL and sends a test
5. [x] PWA: manifest, service worker, installable, Web Push (VAPID), offline shell showing last known state
   - *as built:* `/static/manifest.webmanifest` + icons, `/sw.js` at the root (network-first shell cache, API and terminal never cached, push + notificationclick handlers), VAPID keys generated into the data dir on first start (`vapid.pem`, 0600), subscriptions in SQLite (dead ones dropped on 404/410), "Enable push on this device" in the 🔔 panel; the page keeps the last `/api/state` in localStorage and shows it with an "Offline" banner when the board is unreachable
6. [x] Mobile terminal: fullscreen ttyd + key toolbar (Esc, Ctrl-C, Tab, arrows, y, Enter) + per-session quick-reply buttons via tmux send-keys
   - *as built:* `/term/<session>` wraps ttyd in a full-height iframe (the outer page carries the viewport meta ttyd lacks) with a key bar (Esc, Ctrl-C, Tab, arrows, ⌫, y⏎, Enter), editable per-session quick replies (localStorage) and a text box; keys go through `POST /api/sessions/<name>/keys` (allowlisted key names, literal text via `send-keys -l`). Attach links and ntfy "Terminal" actions open this page; raw `/tty/?arg=` still works. Not done: `attach -f ignore-size` for phones
7. [x] Remote approve/deny: PermissionRequest hook blocks until I tap an ntfy http action button; on timeout falls back to the normal TUI prompt
   - *as built:* `bin/ccboard-permission` (synchronous hook, timeout = `CCBOARD_APPROVE_TIMEOUT` + 30) long-polls `POST /api/permission`; the board records the request, pushes ntfy with Allow / Deny http actions (and Web Push) and waits up to `CCBOARD_APPROVE_TIMEOUT` (90 s); a decision from the phone, the inbox buttons or the `y` / `n` keys is returned as `hookSpecificOutput.decision.behavior`; no answer (or the board down) prints nothing so the TUI prompt appears. When a client is attached to the tmux session the hook returns at once so the person at the terminal is not delayed. `CCBOARD_REMOTE_APPROVE=0` skips registering the hook
8. [x] Live last-lines grid: `tmux capture-pane -p -S -20` every 2s over SSE
   - *as built:* `GET /api/stream` (FastAPI `EventSourceResponse`) captures every non-internal session every 2 s and emits a `lines` event only when a session's tail changed plus a `tick` with the live session list; the "Live" header button toggles a grid of monospace tiles (remembered in localStorage); tiles of sessions needing attention are outlined. The visible screen is what is captured, which is the alt-screen for Claude's fullscreen TUI
9. [ ] Reboot recovery: active sessions recorded in SQLite; boot unit relaunches each with `claude --resume <id>`
   - *note:* the `sessions` table already has `claude_session_id`, `add_dirs` and `ended_at`; the startup reconcile that closes stale rows becomes "relaunch"
10. [ ] Bulk clone every repo via `gh repo list`

## v0.3 — git & tasks

- [ ] Worktree + branch per task via `claude --worktree`; honour .worktreeinclude for .env
  - *note:* runs per repo (a project folder is not a git repo)
- [ ] Task card (title + prompt) → worktree + tmux window + claude; columns derived from hook state
- [ ] Diff view (diff2html) → AI PR description via `claude -p` → `gh pr create` → merge & archive
- [ ] PR/CI status on cards; "fix CI" re-dispatch with failing logs; start task from GitHub issue
- [ ] Cross-worktree file-overlap warning
- [ ] Cost per project/task, daily/weekly, from ccusage joined on cwd
- [ ] Transcript full-text search (SQLite FTS5 over ~/.claude/projects JSONL, display only)
  - *note:* Claude's transcript folder name encodes cwd with non-alphanumerics replaced by `-`, so `shop/api` and `shop-api` collide; key on ccboard's own `claude_session_id`, not the folder

## v0.4 — autonomy & fleet

- [ ] Durable scheduler (APScheduler/systemd timers) for headless `claude -p` runs in fresh worktrees, results as cards
- [ ] Batch one prompt across N repos with concurrency cap and quota awareness
- [ ] Board as MCP server: create_task / list_tasks / get_task_status
- [ ] Port-per-worktree preview links through tailscale serve
- [ ] Devcontainer option per project; bypassPermissions allowed only inside it
- [ ] Second box: same app in node mode, hub over MagicDNS, ACL tags, health panel (cpu/ram/disk)
- [ ] Nightly restic of DB + transcripts; `git push --all` for WIP branches

## Never

- A chat UI replacing the Claude TUI (Remote Control already exists)
- Transcripts as the source of state (format is internal; hooks only)
- Multi-user / team features, Funnel, any public exposure
- bypassPermissions by default; use auto or acceptEdits + deny rules
- Cloud VM orchestration, tsnet rewrite, Kubernetes, every-agent-CLI support on day one

## Proposed additions (not in the original spec)

- [ ] Terminal behind the board's own WebSocket proxy, so the `Tailscale-User-Login` allowlist also covers `/tty` (today ttyd and code-server rely on the tailnet ACL only)
