# Memory API (v0.5.20): the board's claude-mem proxy

Developer notes for the Memory page (#6), the project Memory tab and the gotchas strip (#7), the health tile (#8) and the write-back
switch (#10). The backend is `app/memory_proxy.py` (routes in `app/main.py`, the low-level client in `app/memory.py`). The worker facts
come from the box check on issue #12 (claude-mem worker 13.31.0; plugin 13.34.2 on disk).

The browser never talks to the claude-mem worker. Every route below is a plain board route behind the normal identity check
(Tailscale identity, or the local token). The board talks to the worker over loopback only, with GET only, except for the one
write-back POST described at the end.

## Routes

| Route | What it answers |
|---|---|
| `GET /api/memory/health[?refresh=1]` | The worker's health as the monitor last saw it (every 20 s). `refresh=1` runs one live probe (at most 3.5 s) and stores it. |
| `GET /api/memory/{project}/observations` | The project's observations, newest first, merged over its claude-mem keys. This is the Timeline tab's list. |
| `GET /api/memory/{project}/summaries` | Session summaries, merged the same way. |
| `GET /api/memory/{project}/search?q=` | A text search over the project's keys. |
| `GET /api/memory/{project}/timeline?anchor=<id>` | The window around one observation. Use it for "around this one", not for the Timeline tab. |
| `GET /api/memory/{project}/palace` | Wings, rooms, drawers and gotchas. Cached for 30 s. |
| `GET /api/memory/prefs`, `PUT /api/memory/prefs` | The write-back switch. It is off by default. A PUT needs `X-CCBoard: 1`. |

`{project}` is a ccboard project name and follows the usual name rules. A bad name is a 400, and an unknown project is a 404. Query
values are checked, and a value that is out of range is a 400 with `{error}`. Unknown query parameters are ignored and never reach the
worker.

### Query parameters

**observations** and **summaries**

| Param | Meaning |
|---|---|
| `limit` | 1 to 100. The default is 50. |
| `offset` | 0 to 300, applied after the merge. |
| `before` | The cursor: epoch milliseconds, exclusive. Pass the previous answer's `next_before`. |
| `repo` | Only that ccboard repo's wing, including its worktree keys. |
| `type` | Observations only. A comma list of types, such as `bugfix,decision`. |
| `agent` | `claude` or `codex`. It is sent to the worker as `platformSource`. |
| `session` | A Claude session id. It is sent as `contentSessionId` and also matched on `memory_session_id`. |
| `since`, `until` | Epoch ms, epoch seconds, or ISO text. Inclusive. |

**search**

| Param | Meaning |
|---|---|
| `q` | Required, at most 500 characters. The worker's own name for it is `query`; the board translates. |
| `type` | `observations`, `sessions` or `prompts`. |
| `obs_type` | A comma list of observation types. |
| `limit` | 1 to 100. The default is 20. |
| `offset` | 0 to 300. |
| `agent` | `claude` or `codex`. |
| `order` | `relevance`, `date_desc` or `date_asc`. |

**timeline**

| Param | Meaning |
|---|---|
| `anchor` | Required. An observation id. It must belong to one of this project's keys, otherwise the answer is a 404. |
| `depth_before`, `depth_after` | 0 to 50. The default is 5 each. |

**palace**

| Param | Meaning |
|---|---|
| `subagents=1` | Include workflow-subagent observations. They are left out by default. |
| `drawers` | 1 to 20 drawers per wing. The default is 8. |

## Project keys

claude-mem files memories under a project key. A session's key is the name of its git top-level folder. In a git worktree the key is
`<parent repo folder>/<worktree folder>`, and a worktree folder named like its repo collapses to plain `<repo>`. The board's keys for
one project (`keys_for`) are:

- each repo folder's name, which becomes that repo's wing;
- the project folder's own name, for sessions started in the project folder (the `root` wing);
- `<repo>/<slug>` for every task worktree on disk, under `.claude/worktrees` and `.ccboard/worktrees`;
- the names that `CLAUDE_MEM_PROJECT_ENVIRONMENTS` gives these folders, if that setting is set;
- every worker key `<repo>/<anything>` of the project's repos, so a worktree that is gone from the disk keeps its memory.

These candidate keys are intersected with the worker's `/api/projects` list, which is cached for 60 s. If that list cannot be read,
the disk keys are used as they are and `keys_checked` is `false`. Noise keys, such as the unix user, `tmp`, a notes folder or a hidden
agent folder, never match, so they never leave the box.

`keys: []` together with `keys_checked: true` means the worker has no memory for this project yet. It is not an error.

## The envelope

Every project route answers these fields next to its own data:

```json
{
  "project": "shop",
  "keys": ["api", "web", "api/fix-login"],
  "keys_checked": true,
  "state": "ok",
  "up": true,
  "stale": false,
  "stale_at": null,
  "reason": null,
  "reason_code": null,
  "partial": [],
  "ambiguous": [],
  "ambiguous_filter": null,
  "shape_error": false,
  "compat": "ok",
  "worker_version": "13.31.0",
  "tested_worker": "13.31.0",
  "at": "2026-10-08T09:00:00+00:00"
}
```

- `compat` compares the version the monitor last saw with `tested_worker`. It is `ok` for the same major version that is not newer, and
  `untested` for a newer minor or patch version. It is `unknown` for another major version, or when the version is not known yet (for
  example before the first sample). It is never `ok` for a version it cannot read.
- `partial` lists keys that did not answer within the budget. The rest of the answer is still served.
- `ambiguous` is `[{key, shared_with: ["other-project/repo", ...]}]` when another ccboard project has a repo with the same folder name.
  The rows of such a key are filtered by path (`ambiguous_filter: "files"`): a row that names absolute file paths is kept only when one
  of them lies under `<PROJECTS_DIR>/<project>/`. A row without absolute paths cannot be told apart and is kept. This is best effort,
  and the page should say so.

## States and reasons

| `state` | HTTP | When | What the page should say |
|---|---|---|---|
| `ok` | 200 | Fresh data. `stale`, `partial` and `ambiguous` may still be set, see below. | nothing |
| `ok` with `stale: true` | 200 | The worker failed, and the last good answer is served. `stale_at` is when that answer was fetched. | "Showing data from HH:MM" plus `reason` |
| `ok` with `partial` not empty | 200 | Some keys did not answer. | name the keys |
| `ok` with `ambiguous` not empty | 200 | A folder name is shared with another project. | "another project shares a folder name; filtered by file path, best effort" |
| `incompatible` | 200 | The worker answered in a shape this board does not know. The lists are `[]`, `shape_error` is `true`, and `reason_code` is `shape`. The worker is running. | "The Memory page may be wrong until the board is updated; report it." |
| `down` | 503 | Connection refused, or a non-loopback worker host. | `reason` (the real cause) and Retry |
| `degraded` | 503 | The worker is slow, answered an HTTP error, sent an answer that is too big, or reported an error, and nothing is cached. | `reason` and Retry |
| `off` | 503 | `CCBOARD_CLAUDE_MEM=0`. | hide the page's memory parts |

`reason_code` is one of the following:

| `reason_code` | Meaning |
|---|---|
| `refused` | Nothing accepted the connection, so the worker is not running. |
| `not_loopback` | `CLAUDE_MEM_WORKER_HOST` names a host that is not loopback, so the board refuses to connect. |
| `timeout` | The worker is slow. |
| `http` | The worker answered an HTTP error; `reason` carries the status. |
| `too_large` | The answer was over 2 MB. |
| `worker_error` | The worker answered HTTP 200 with `isError: true`. The timeline reports errors this way. |
| `shape` | The answer had a shape this board does not know (`incompatible`). |
| `off` | claude-mem is turned off. |

`reason` is always one line of plain text, and the proxy never makes up a generic "worker stopped". A `timeout` reads as slow, not
down: the box saw a broad text search take 15.7 s. Search and palace get 6 s; the other routes get about 3 s for the whole fan-out and
2 s per worker request. With the worker stopped, every route answers within its budget: stale data when it has some, otherwise a 503.

The 503 body is `{error, state, up: false, reason, reason_code, compat, worker_version, tested_worker}`.

## observations and summaries

```json
{
  "items": ["…"],
  "has_more": true,
  "next_before": 1791043138470,
  "scan_capped": false,
  "…": "plus the envelope"
}
```

An observation item has these fields: `id`, `type`, `title`, `subtitle`, `narrative`, `facts[]`, `concepts[]`, `files_read[]`,
`files_modified[]`, `created_at` (ISO), `created_at_epoch` (ms), `project`, `merged_into_project`, `platform_source`,
`memory_session_id`, `content_session_id`, `prompt_number`, `discovery_tokens`, `agent_type`, `agent_id`, `key` (the claude-mem key)
and `repo` (the wing: the ccboard repo name, or `root`).

The worker's list route leaves out `discovery_tokens`, `agent_type` and `agent_id`, so they are `null` here. Lists that the worker
stores as JSON text arrive as real arrays.

A summary item has these fields: `id`, `session_id`, `request`, `investigated`, `learned`, `completed`, `next_steps`, `project`,
`platform_source`, `created_at`, `created_at_epoch`, `key` and `repo`. Fields that only search rows carry (`notes`, `files_read`,
`files_edited`, ...) are `null` or `[]`.

### Paging

- Pass `before = next_before` to get the next page. The cursor is exclusive and is never repeated, so pages have no duplicates.
- If several rows share the same millisecond exactly at a page edge, the ones after the edge can be skipped. This is rare.
- Each key reads at most 300 rows for one page. When that cap leaves a page short, `scan_capped` is `true` and `has_more` is `true`.
  Do not read a short page as "no more": `next_before` still moves on.
- `has_more: false` means there is nothing older.
- "N new" detection belongs to the page. Compare `state.memory.observations` now with its value at load time, then fetch page one
  again.

## search

```json
{
  "query": "login",
  "observations": ["…"],
  "sessions": ["…"],
  "prompts": ["…"],
  "total": 7,
  "fallback": null,
  "…": "plus the envelope"
}
```

The board makes one worker call with `query=`, `projects=a,b` and `format=json`; the worker returns the union of those keys. If the
worker refuses `projects=` (400 `INVALID_PROJECTS`), the board makes one call per key and sets `fallback: "per_key"`. Rows of a key
outside the project are dropped.

- `sessions` rows use the summary fields.
- `prompts` rows have `id`, `prompt_text`, `prompt_number`, `project`, the session ids, `platform_source` and the times. `prompt_text`
  is the user's own prompt text.
- `total` is the worker's `totalResults`.

## timeline

```json
{
  "anchor": {"…": "the full observation (agent_type included)"},
  "key": "api",
  "items": [
    {"id": 5930, "day": "Sep 27, 2026", "time": "1:23 PM", "glyph": "○", "title": "…", "tokens": 95}
  ],
  "…": "plus the envelope"
}
```

The worker answers the timeline as markdown only, and the board parses the table rows. The titles are text and must be set with
`textContent`.

The project's Timeline tab should use `/observations` with the cursor. There is no timeline by query.

## palace

```json
{
  "wings": [
    {
      "name": "api",
      "keys": ["api", "api/fix-login"],
      "total": 4,
      "types": {"bugfix": 2},
      "rooms": [{"name": "gotcha", "kind": "concept", "count": 3}],
      "drawers": ["…"]
    }
  ],
  "rooms": ["…the same, over the whole project"],
  "gotchas": ["…at most 3 drawers, newest first"],
  "total": 7,
  "scanned": 8,
  "truncated": false,
  "summaries": 170,
  "summaries_scope": "worker",
  "tokens_saved": null,
  "subagents": false,
  "subagent_filter": "applied",
  "excluded_subagents": 1,
  "cached": false,
  "…": "plus the envelope"
}
```

- A **wing** is a ccboard repo, or `root` for the project folder. Worktree keys fold into their repo's wing. Wings are ordered by size.
- A **room** is a concept or a type. The concept rooms come first, in this order: `gotcha`, `problem-solution`, `pattern`,
  `how-it-works`. Other concepts follow by count, then the types `bugfix`, `feature`, `change`, `decision` and `discovery`, then other
  types by count. Rooms with no observations are left out. An observation with several concepts counts in each of them, so room counts
  do not add up to the wing total.
- A **drawer** has these fields: `id`, `title`, `subtitle`, `type`, `concepts[]`, `files[]` (read and modified, without duplicates),
  `created_at`, `created_at_epoch`, `key`, `narrative` (at most 2000 characters), `facts[]` (at most 10) and `agent_type`. A drawer
  expands in place to its narrative and facts without another call.
- The counts cover the newest `scanned` observations, at most 300. `truncated: true` means there are more; say "of the newest 300".
- `summaries` is the worker's summary count over every project (`summaries_scope: "worker"`), or `null` when the worker does not
  report it.
- `tokens_saved` is `null` because worker 13.31.0 does not report it.
- **UNVERIFIED on the box:** the palace reads one filter-only search (`projects=…&type=observations&orderBy=date_desc&format=json`),
  because only search rows carry `agent_type`. The box check verified a filter-only search with `type`, `obs_type` and `dateStart`, but
  not with `projects` alone. If the worker refuses it, the palace falls back to the list route with `subagent_filter: "unavailable"`.
  In that case subagent rows are included and cannot be told apart. The opt-in live contract test checks this search.
- Only a clean answer is cached, meaning one that is not stale and not partial.

## health (also `state.memory`)

`GET /api/memory/health` and `state.memory` (sent with every 3 s state poll) carry the monitor's record, and both add
`stale_sessions`. The health route also adds `up`, `compat`, `worker_version` and `tested_worker`.

| Field | Meaning |
|---|---|
| `state` | `up`, `degraded` or `down`. The route can also answer `off`, or `unknown` before the first sample. |
| `reason` | Why the state is not `up`, in one line. |
| `version` | The running worker's version. |
| `plugin_version` | The version installed on disk. It differs from `version` until the worker restarts. |
| `port`, `port_source`, `pid` | Where the worker is. |
| `observations`, `sessions`, `summaries`, `db_size` | Totals across the whole worker. `null` means unknown, never 0. |
| `queue_depth`, `processing` | Observations waiting for the observer, and whether it is working on them now. |
| `active_sessions` | The worker's count of active sessions. |
| `stale_sessions` | An **estimate**: the worker's `active_sessions` minus the board's live Claude sessions, never below 0. `null` when the worker does not say, or (health route) before the first state poll. |
| `last_error` | `{at, message, provider, failures, last_success_at}`, or `null`. It has recovered when `last_success_at` is later than `at` and `failures` is 0. |
| `rates` | `{obs: {d1, d7}, sum: {d1, d7}}`: per day over the last 24 h and 7 days, from the board's own samples (series `mem_obs` and `mem_sum`, one sample every 5 min). A rate is `null` ("collecting") until the samples span 1 h (for `d1`) or 1 day (for `d7`). A counter reset never gives a negative rate. |
| `rates_since` | The time of the oldest sample used. |
| `compat`, `tested_worker` | As in the envelope. |

`state.memory` is `null` when `CCBOARD_CLAUDE_MEM=0` or before the first sample. Hide the tile in that case.

## Write-back (prefs)

`GET /api/memory/prefs` answers:

```json
{
  "prefs": {"writeback": false},
  "available": true,
  "reason": null,
  "writeback_sends": {"title": "…", "text": "…", "project": "…", "metadata": ["…"], "never": ["the prompt"]}
}
```

`PUT /api/memory/prefs {"writeback": true|false}` answers the same shape. `available: false` comes with a `reason`: claude-mem is off,
or the plugin is not installed or is disabled.

When the switch is on, a task that finishes as `done` with a non-empty result sends one note, from a daemon thread. Nothing is sent
for a task that failed or has no result. The note is a `POST /api/memory/save` with:

```json
{
  "title": "Task: <title>",
  "text": "<result>",
  "project": "<key>",
  "metadata": {"source": "ccboard", "task_id": 1, "agent": "claude"}
}
```

- The text is capped at 8 KB, cut at a line break and marked.
- The key is `<repo>`, or `<repo>/<worktree>` for a task in a worktree, so the note lands in the same wing.
- At most one note is sent per task. The marker is kv `mem_wb_task:<id>`.
- A refused, slow or failing worker never reaches the task and never shows a toast.
- The prompt is never sent.
- This is the board's only write to the worker, and no browser route leads to it.

## Demo mode (`?demo=1`)

`core.js demoApi` maps each route to a fixture in `app/static/demo/`. The same fixture answers for any project:

| Route | Fixture |
|---|---|
| health | `memory_health.json` |
| prefs | `memory_prefs.json` |
| observations | `memory_observations.json` |
| summaries | `memory_summaries.json` |
| search | `memory_search.json` |
| timeline | `memory_timeline.json` |
| palace | `memory_palace.json` |

Add `&mem=down|degraded|stale|partial|ambiguous|incompatible|untested` to lay a variant from `memory_states.json` over every project
route. `down` and `degraded` are thrown as 503 errors, the way `api()` throws them (`err.status`, `err.body`). The fixtures have no
`demo.epoch` (`demoRebase` would shift the millisecond epochs as if they were seconds), so their times stay where they were captured.

`app/static/demo/state.json` still has `memory: null`, so the project Memory tab stays hidden in demo. If you set it, use the fields
of `memory_health.json`.

## Pinned worker names (from the box check, #12)

The board sends these names to the worker:

| Name | Use |
|---|---|
| `query` | The search text. Not `q`: the worker ignores `q=`. |
| `project` | One key. |
| `projects` | A comma list of keys. |
| `format=json` | Always sent with a search. |
| `limit` (at most 100), `offset` | Paging. |
| `platformSource` | The agent. |
| `contentSessionId` | The session. |
| `type`, `obs_type`, `orderBy` | Search filters and order. |
| `anchor`, `depth_before`, `depth_after` | The timeline window. |

They are pinned in `memory_proxy` (`TESTED_WORKER = "13.31.0"`) and in `tests/test_memory_contract.py`.

After a plugin update, run this on the box:

```
CCBOARD_TEST_CLAUDE_MEM=1 .venv/bin/pytest -q tests/test_memory_contract.py -k live
```
