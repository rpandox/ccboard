# Notifications

For people who want to know on the phone when a session needs them.

[Back to the documentation index](../README.md)

In this page, "the box" means the machine that runs the board.

### Notifications

A push tells you which session wants you and why, without opening the board. The title is `<glyph> <project>/<repo> · <session>: needs you | done | error` (◆ claude, ◇ codex, ▸ shell; a session in a project's root folder shows the project alone). The body has up to three lines, empties left out: the task title, `› <your last prompt, 120 chars>` (never a prompt a harness typed, such as a finished background task), and `? <what is asked>`. The ask is `Bash: npm test` for a permission, Claude's message for idle or a question, the first 200 characters of the answer for done, and the error text for an error. A done notice ends with `(session closes in 45s)` only when the board has actually planned that close: the task runtime stamps the close (`flags.autoclose.due`) before the notice is built, and the seconds are the time left to that due, rounded to 5 (the grace is `CCBOARD_AUTOCLOSE_GRACE`). When another task keeps the session, when the answer ends with a question (which holds the close) or when the task does not close its session, the line is left out. The ntfy and Web Push bodies read the same stamp. The rate-limit notice is titled `<Agent> rate limited` and names the session that hit it first; a login that stopped working is `<Agent> login not valid` and opens Settings › Accounts.

| Notice | ntfy priority | ntfy tags | Web Push |
|---|---|---|---|
| needs you, permission | 4 | bell, key | yes |
| needs you, anything else | 4 | bell | yes |
| error | 4 | rotating_light | yes |
| done | 3 | white_check_mark | yes |
| done, a chain step with another step queued behind it | 2 | white_check_mark | no (ntfy only) |
| rate limit | 5 | no_entry | yes |

**Buttons, by platform.** ntfy (Android and iPhone) shows up to three: a pending permission offers Allow, Deny and Terminal; every other notice offers Terminal and Ack; a tap on the notice opens `/#/s/<tmux>`. Web Push shows the first two of the same list where the system draws action buttons (Chrome on Android, desktop Chrome): Allow and Deny answer the permission from the notification itself, with a short "Allowed." note and no window opened; Ack marks the session seen; Terminal opens `/term/<tmux>` in its own window. An installed iPhone app ignores action buttons: it shows the title and body, and a tap opens the session's peek (`#/s/<tmux>`), where Allow, Deny, Ack, Open terminal and a reply box are one tap away. A plain tap focuses the board window that is already open and moves it to the session (the service worker posts `{type: 'nav', url}` to the page); with no board window open it opens one. A notice that waits on you (a waiting session or a permission) stays on screen until you deal with it, and the home-screen icon badge shows how many sessions need a look (iOS 16.4 and later, Android Chrome).

**One notice per session on the phone.** Every Web Push carries the session's tmux name as its tag (`<node>:<tmux>` once a node is paired, see below), so a session's notices replace each other on the lock screen instead of piling up, and the replacement buzzes again (renotify). The payload is `{title, body, url, tag, renotify, agent, state, tmux, perm_id, actions, badge, ts}`; a service worker that only knows the old `{title, body, url, tag}` still shows it.

**Switches.** Settings › Notifications has one switch per kind: needs you, done, rate limit, error or crash, login problem (all on by default, kept on the box as kv `notify_prefs`, `GET/PUT /api/notify/prefs`). A kind that is switched off sends nothing on ntfy or Web Push, a permission request included (the request still waits on the board for its answer). Below the switches an example card shows the notice each kind sends, built by the same code as the real ones, and says what an iPhone shows. The two test buttons ignore the switches. Claude's own input-needed notification and the agent's own push are separate from these. A note under the switches says that Claude's own apps (the Claude mobile app, Remote Control) may notify you about the same moments, and that if you get two notices you can turn off either the board's notices there or notifications in the Claude app's settings; the board changes nothing on its own.

**Cooldowns.** One notice per session and state per 60 s, and a done notice per session at most every 120 s (`STATE_COOLDOWN`: the longer of it and the 60 s wins). A permission push is never throttled (each request needs an answer), but it counts as the session's waiting notice, so the notification Claude raises when its own prompt appears does not buzz a second time. A rate limit is the account's, not the session's: it is announced once per agent and window (kv `rl_notified:<agent>:<resets_at>`), never twice for one agent within 15 minutes, and the session's own limit error adds nothing (one session once retried the same limit 482 times). A login problem is announced once per account per 15 minutes. Without a public URL there is no link and no ntfy buttons.

For per-node pushes, the merged inbox and the badge rule, see [Notifications across nodes](#notifications-across-nodes).

#### Notifications across nodes

Each node sends its own ntfy and Web Push notices for its own events; once a node is paired the notice names it (`node-a · ◆ shop/api · s1: needs you`) and its tag becomes `node-a:<session>` (`node-a:rate-limit`, `node-a:login-problem`, `node-a:test`), so two nodes with a session of the same name do not replace each other on one phone, and a board with no paired node sends exactly the notices it always sent. The hub re-sends nothing for what a node reports: Inbox and Home list those items once under "On other nodes" (one entry per node, session, kind and time; gone when the node's next reading no longer lists it; marked stale while the node is not online), so one event never buzzes twice. Web Push belongs to one address, so a phone subscribes once on each node: Settings › Notifications has a row for each paired node with **Open <node>**, the page can only see its own subscription and says so, and the app-icon badge counts the sessions of the board that last drew it, never those of other nodes. The first pairing changes the tags, so a notice may show once more on the phone; links in notices need `CCBOARD_PUBLIC_URL` (the Doctor row *Notification links* warns when a node is paired and it is empty), and when two nodes work under one Claude or Codex login (the same subscription digest on their cards) both send a "rate limited" notice for the one window, which the Doctor row *One subscription on two nodes* says and the Home strip shows as a single line.

| Event on a node | That node sends | The hub shows |
|---|---|---|
| needs you, permission | `<node> · <glyph> <project>/<repo> · <session>: needs you`, tag `<node>:<session>`, Allow, Deny, Terminal | a card under "On other nodes" with the node chip; Allow and Deny when the pair holds the `permissions` scope |
| needs you (a question), done, error | the same title with `done` or `error`, tag `<node>:<session>`, Terminal, Ack | a row under "On other nodes": node chip, session, `needs input` or `error`, age |
| rate limit | `<node> · Claude rate limited`, tag `<node>:rate-limit` | one line on Home for each account window used up; two nodes under one subscription share the line |
| login problem | `<node> · Claude login not valid`, tag `<node>:login-problem` | nothing in the list; the notice and the node's own Settings carry it |
| test | `<node> · ccboard test`, tag `<node>:test` | nothing |
| no node paired | the notices above without `<node> · `, and the session name (or `rate-limit`, `login-problem`, `test`) as the tag | not applicable |

## Push notifications and remote approve

- **Push notifications** two ways: a self-hosted **ntfy** server on your tailnet (installed by `install.sh`, subscribe in the ntfy app to the URL shown under 🔔) and **Web Push** for the installed PWA ("Enable push on this device"). Pushes fire on needs-you, done, error, rate limit and a login that stopped working (each one a switch under Settings › Notifications), with a deep link and buttons (Allow and Deny for a permission, else Terminal and Ack). The ntfy server is tailnet-only, so the phone needs the Tailscale VPN on; on an iPhone the box relays a content-free wake-up through ntfy.sh and the app then fetches the message from your server (the 🔔 panel lists the exact steps).
- **Remote approve/deny**: when Claude asks for permission and nobody is attached to that terminal, the board pushes a notification with **Allow / Deny** buttons and holds the prompt for up to 90 s (`CCBOARD_APPROVE_TIMEOUT`). Answer from the phone, the inbox, or the `y`/`n` keys; with no answer the normal TUI prompt appears. If someone is attached, the TUI prompt appears immediately. Recognized Claude tool approval dialogs still appear in Needs you as `terminal_prompt`, with scrubbed command or file details and numbered choices. Only a signed-in person can answer. Allow and Deny select only the one-time Yes and No choices after rechecking the full screen and Claude foreground process; a changed dialog sends nothing. The same actions work through a paired hub with the permissions scope and on the epic page, which shows “waiting for a person”. Persistent approvals and auto mode are never selected.

**Background.** Notifications: v0.5.7 and v0.5.18; Notifications across nodes: issue #151.
