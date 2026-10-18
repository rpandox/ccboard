# Schedules, batches, auto-continue and reboot recovery

For people who let the board run work unattended.

[Back to the documentation index](../README.md)

In this page, "the box" means the machine that runs the board.

### Auto-continue

The board types `continue` once into a session parked on a limit after its window resets, and into a session that was working when the box went down once its relaunch is back at its prompt (a power cut or a reboot must not leave a half-done turn waiting for someone to notice). Each session has its own switch, **Auto-continue: on / off**: in the `...` menu of a session row (Agents, Home and the project page), in the tile menu of the Quad, in the Options of the Quad's Tune panel and as a chip in the terminal page's strip. While it is off the row wears a dim `no auto-continue` chip. The switch is `POST /api/sessions/<name>/flags` with `{"no_autoresume": true}` (the only flag it accepts) and is stored on the session's row, so it survives a board restart and a reboot: recovery copies it to the relaunched row. A session with the switch off gets no `continue` after a limit reset, after an account switch (the sessions parked on a limit continue at once) or after a reboot; one with it on still gets all three. It is kept when a new conversation starts in the same session (`/clear`, a new id), because the person turned it off for that session, not for one conversation. `CCBOARD_AUTO_CONTINUE=0` is the board-wide setting for the limit reset and the reboot, shown read-only under Settings > Notifications; with it off the per-session item says so and promises nothing (an account switch is an explicit request and still continues the sessions that have the switch on). After a reboot only a row that was `working` less than 24 hours before is continued, only once, only when nobody is at its terminal, and not when the person typed first. While a Claude session is parked on a limit, its badge, Inbox card and Home row say so instead of "error": "limit reached, continues at HH:MM" (with a weekday in front when the reset is more than 20 hours away), "limit reached, auto-continue is off" (this session opted out, the board setting is off, or no reset time is known) or "limit reset, continuing shortly" (the reset has passed and the `continue` is not typed yet); a task card's owner chip says "limit reached". A reset time is only promised when the board will act on it. Codex rows and a task's step strip are not covered.

## Schedules

- **Schedules**: cron or one-off jobs run headless in a fresh worktree, with Claude (`claude -p`) or, since v0.5.16, Codex (`codex exec --json`; see [*Headless Codex runs*](codex.md#headless-codex-runs-schedules-and-batches) under *Codex*). At most 2 run at a time; each agent is deferred while its own window is above 85 % (Claude: the 5-hour window; Codex: its usage window) and for 30 minutes (or until its window resets) after one of its runs comes back rate-limited; each result becomes a task card you can resume in a terminal. Claude's percentage comes from the statusline of interactive sessions, so on a box that only runs headless Claude jobs it stays unknown (the Schedules header says so) and only the back-off protects you; Codex's comes from the rollout tailer.
- **Batch prompt**: one prompt sent to each of the repos you pick, run by the same scheduler, with progress for the whole batch.

## Reboot recovery

After a restart of the machine the board relaunches every Claude session in its repo with `claude --resume <id>` and every Codex session with `codex resume`, and Home says what came back, for example "After a restart, 2 Claude and 1 Codex sessions relaunched: a, b, c". `CCBOARD_RECOVER=0` turns this off (see the [settings table](../configuration.md#settings)).

See [ROADMAP.md](../../ROADMAP.md) for how each item was built and what comes next.

**Background.** Auto-continue: issue #84.
