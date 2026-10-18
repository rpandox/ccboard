# Hooks

For people who need to know what the Claude Code hooks do.

[Back to the documentation index](../README.md)

### Hooks v2, notifications with context, /command /prompt /resize (v0.5.7)

### Hooks v2 (v0.5.7)

ccboard now registers 15 Claude Code hook events (it was 6): SessionStart, UserPromptSubmit, Notification, Stop, StopFailure, SubagentStart, SubagentStop, PreCompact, PostCompact, PostModelSwitch, TaskCreated, TaskCompleted, PostToolBatch, ConfigChange (all async, timeout 5, via `bin/ccboard-hook`) and SessionEnd (via `bin/ccboard-hook-fast`: not async, timeout 3, a 2 s curl cap, so the event lands before the process exits). PermissionRequest is unchanged. WorktreeCreate and WorktreeRemove are deliberately not registered, and they stay that way (probe on the box, Claude Code 2.1.294, 2026-10-09, a scratch repo and a scratch settings file): a WorktreeCreate hook **replaces** Claude's own worktree creation and has to create the worktree and print its path, so a hook that prints nothing (the async, no-output one the board would register) makes `claude --worktree` fail with "hook succeeded but returned no worktree path" and no worktree is made, and `async: true` does not help because Claude waits for the hook and uses its output; WorktreeRemove on its own is harmless but it never fired for the removals Claude does itself (`ExitWorktree` with remove, print mode). Task worktrees keep working as they do today: Claude creates `.claude/worktrees/<slug>` itself and the board adds the folder to `.git/info/exclude`. Not tried: other Claude Code versions, and the cleanup when an interactive session ends.

The container entrypoint re-runs `scripts/claude_settings.py install` on every start, so a box with the old 6-event set is upgraded in place on the next deploy. No install.sh rerun is needed, and re-running install changes nothing. Other tools' hooks in settings.json are kept. `claude_settings.py` takes `--settings PATH` (edit another settings file; handy with `show`) and `--with-worktree-hooks` (accepted and ignored for good: see above). Both wrappers read `CCBOARD_CURL_MAX` (request cap in seconds, default 3; the fast wrapper is fixed at 2) and send `X-CCBoard-Agent: ${CCBOARD_AGENT:-claude}`.

What the board now does with them:
- A turn that was not typed by a person (a `<\task-notification>`, a `<\system-reminder>` or `[SYSTEM NOTIFICATION`, a `<pasted_content` placeholder, a slash or bash echo) still marks the session working but is never shown as its last prompt. It is kept in `flags.last_system_turn`.
- A permission you answered in the terminal clears 'needs you' at the next tool batch (PostToolBatch flips a permission wait back to working; an idle wait is left alone). PostToolBatch is never stored as an event.
- Waits are typed (`flags.wait_kind`: permission, idle or elicitation). An elicitation answered clears its own wait.
- Hooks from claude-mem's observer sessions, or from a conversation another open session owns, are ignored (`ignored: foreign`), and so are hooks from a nested Claude (`CLAUDE_CODE_CHILD_SESSION=1`: a workflow or SDK subagent, the observer) unless they carry the row's own conversation id (`ignored: child`). A /resume or /clear on the same session rebinds its conversation id.
- The permission hook (`POST /api/permission`, `bin/ccboard-permission`) runs the same guards as the ordinary hook, in the same order: another agent's process (`foreign`), a foreign session (`foreign`), a nested Claude whose conversation is not the row's (`child`; the script sends `X-CCBoard-Child` from `CLAUDE_CODE_CHILD_SESSION`, like `bin/ccboard-hook`) and a Codex sub-thread (`subthread`: a guardian or subagent thread of the row's Codex session, as `thread_relation` classifies it). An ignored request is answered at once with `{"behavior": null, "reason": <word>, "ignored": <word>, "session": <row>}`: no pending permission, no notification, no state change, no cancelled auto-close and no wait, so the asking process falls back to its own prompt and nothing is approved on your behalf. Verified on the box (Claude Code 2.1.294, 2026-10-09): a nested `claude -p` run from a board session's Bash tool **does** run the PermissionRequest hook. The request carried `X-CCBoard-Child: 1` and the parent's `X-CCBoard-Session` and pane (inherited from the environment), and a `session_id` of its own that is not the row's conversation; the live board answered `{"behavior": null, "reason": "child", "ignored": "child"}` at once, no permissions row was added and the row's state and events did not change. The nested run then denied the tool itself (in print mode that is a denial, not a wait). Not probed: a Codex guardian or sub-thread request and a nested process that shares the row's own conversation id.
- Stop uses Claude's own closing message for the row text (first 300 characters) and keeps the full text in `flags.last_result`. A resume records how long it sat and how big it is in `flags.resumed`. /clear and /resume no longer flash the session as ended. A compaction restart no longer flips a working turn to idle.
- The statusline adds effort, fast mode, thinking, session name, prompt-cache state, PR, worktree, repo and the over-200k flag to each session's stats. The newest raw statusline payload is kept in kv `statusline_sample` (at most 8 KB) so the doctor can show the real shape on the box.

### What the hook scripts need (issue #121)

The hook scripts in `bin/` are POSIX `sh` and call two programs, so `sh`, `curl` and `python3` must be on the PATH the hooks run under. `curl` posts the event to the board; `python3` builds the permission reply and the statusline text. Every script fails soft: with no `curl` nothing reaches the board, and with no `python3` the permission hook prints nothing (the normal terminal prompt appears, so Allow and Deny do not show on Needs you) and the statusline shows nothing. Nothing is blocked, but nothing says so either, which is why Settings > Doctor has a **Hook helpers** check (group Claude): it runs `curl --version` and `python3 -c pass` under the board's PATH plus the login shell's, 3 seconds each, and names the missing one, the effect above and the install command for your system. On a Mac without the Command Line Tools `/usr/bin/python3` is a stub; the check says so and gives `xcode-select --install` or `brew install python`. A probe that times out reads as unknown, not as a failure.

The hook commands in `~/.claude/settings.json` are written through `shlex.quote`, so a path with a space, an apostrophe or `&` still works in the shell Claude Code runs them with. A path made of plain characters is written exactly as before, and a settings file written by an earlier install is still recognised by the script names and rewritten once. `install.sh` still refuses a checkout path (`APP_DIR`) that contains whitespace, a quote, `$` or a backslash, with the same message as for `PROJECTS_DIR` and the other guarded values, because the systemd units use it unquoted.

Windows: WSL2 is supported (it is Linux; the scripts run inside the distro). Native Windows is not: the hooks, the tmux session model and the services have no native counterpart there.

### Which hooks the board ignores

Four guards decide whether a hook may change a session's row. They run in this order, and the first one that matches stops the event.

| Guard | What it catches | How it knows | Response |
|---|---|---|---|
| 1. Agent | A hook from another agent's process in the same folder (a `codex exec` run inside a Claude row) | The script sends `X-CCBoard-Agent` (`CCBOARD_AGENT`, default `claude`); a row of another agent does not take it | `ignored: foreign` |
| 2. Foreign session | claude-mem's observer (a folder under `~/.claude-mem`, or a transcript in `observer-sessions/`), or a conversation another open row already owns | The working folder, the transcript path and the session id, checked against the open rows | `ignored: foreign` |
| 3. Child session | A nested Claude (a workflow agent, an SDK subagent, the observer), which inherits the parent's `CCBOARD_SESSION` and `TMUX_PANE` | `X-CCBoard-Child` carries `CLAUDE_CODE_CHILD_SESSION`; the hook is ignored unless its session id is the row's own conversation | `ignored: child` |
| 4. Sub-thread (Codex) | Another thread of the same Codex process, which Codex reports under its own session id | The adapter's `thread_relation()` says own, rebind or sub-thread; a sub-thread event is counted in `flags.subthreads` and stored as kind `subthread` | `ignored: subthread`; the state does not change |
| Permission hook | A permission request from any of the four cases above (`POST /api/permission`, sent by `bin/ccboard-permission`) | The same four guards in the same order; the script sends `X-CCBoard-Child` like the ordinary hook | At once `{"behavior": null, "reason": "<word>", "ignored": "<word>", "session": "<row>"}`: no pending permission, no notification, no state change, so the asking process shows its own prompt |

The permission hook (`POST /api/permission`, `bin/ccboard-permission`) runs the same four guards in the same order, so a nested Claude's permission request never shows as a wait on the parent's row. An ignored permission request is answered at once and leaves no pending prompt.

**Probe result (box, Claude Code 2.1.294, 2026-10-09).** A nested `claude -p` inside a board session does fire the PermissionRequest hook, so the risk was real: one `POST /api/permission` arrived with `X-CCBoard-Child: 1`, the parent's `X-CCBoard-Session` and pane, and a `session_id` of the nested conversation that is not the row's. The live board answered `ignored: child` in 0.00 s, no permissions row appeared and the row's state did not move; the nested run then denied the tool itself.

The hook scripts send these headers, one for each piece of information the board needs:

- `X-CCBoard-Token`: the token from the 0600 token file.
- `X-CCBoard-Agent`: the agent name, from `CCBOARD_AGENT`.
- `X-CCBoard-Child`: `CLAUDE_CODE_CHILD_SESSION`, empty for an ordinary session.
- `X-CCBoard-Session`: `CCBOARD_SESSION`. The board resolves the session from this first, then from the pane, then from the folder.
- `X-CCBoard-Pane`: `TMUX_PANE`.
- `X-CCBoard-Tmux`: `TMUX`.

How to see it: `POST /api/hook` answers an ignored event with `{"ignored": "<guard>"}`. Ignored sub-thread events are stored as events of kind `subthread`, which you can list with:

```sh
sqlite3 <data dir>/ccboard.db "select tmux_name, event, kind from events order by id desc limit 20"
```

There is nothing to configure. To test the child guard, run a nested `claude -p` inside a scratch board session and watch its hooks answer `ignored: child`. Do not do it against the live box.
