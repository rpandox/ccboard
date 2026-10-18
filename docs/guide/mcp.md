# Calling the board over MCP

For people who call the board's tools from another machine.

[Back to the documentation index](../README.md)

In this page, "the box" means the machine that runs the board.

### MCP from other devices

Claude Code and Codex on a laptop or another machine of your tailnet can use the board's MCP tools (`list_projects`, `create_task`, `list_tasks`, `get_task_status`, `dispatch_task`, `get_task_result`, `list_nodes`, `epic_status` and `epic_run`) through `/mcp`, the board's remote MCP endpoint, each device with a token of its own. On the box itself nothing is needed: `install.sh` registers the stdio `ccboard` server there, and it stays box-only.

**Turn it on and add a device.** Settings > Agents > Connect from another device: switch on "Allow other devices", type a device name, keep the scopes (Read and Tasks are on, Sessions is off) and the expiry (90 days), and tap Add device. A dialog shows the token once, with a Copy button, and the two commands below with the token and the board's address filled in. Closing the dialog removes the token from the page; it cannot be shown again (mint a new one instead). The address is `CCBOARD_PUBLIC_URL` plus `/mcp`; while that variable is empty, Settings shows a warning and no command.

Claude Code (options go before the server name; `--scope user` keeps the entry in your own config, never in a repository):

```
claude mcp add --transport http --scope user --header "Authorization: Bearer <token>" ccboard https://<board-host>:<port>/mcp
```

Check it with `claude mcp list` (or `claude mcp get ccboard`); `claude mcp remove ccboard` undoes it. Do not use `--scope project`: it writes `.mcp.json`, which is shared through git, and a token must never be in it. The `.mcp.json` form with `${VAR}` in `headers` is not offered: Claude Code reads some credential-like variables as empty there, and whether a name like `CCBOARD_MCP_TOKEN` is affected has not been tested.

Codex (the option takes the NAME of an environment variable, not the token; `codex mcp add` has no `--header`):

```
export CCBOARD_MCP_TOKEN=<token>
codex mcp add ccboard --url https://<board-host>:<port>/mcp --bearer-token-env-var CCBOARD_MCP_TOKEN
```

Check it with `codex mcp list` (or `/mcp` in the Codex TUI). Codex reads the variable when it starts, so the shell that runs `codex` needs it every time (your shell profile, or a secrets manager). The same entry in `config.toml` is a `[mcp_servers.ccboard]` table with `url` and `bearer_token_env_var = "CCBOARD_MCP_TOKEN"`.

**Revoke.** Revoke on the device's row (two taps) and the token fails on its very next request. A client then reports the server as failing or as needing authentication: that means the token is wrong, revoked or expired; mint a new one in Settings > Agents. The board's answer says the same (`401`, "mint a new token in Settings > Agents"). Both clients also know OAuth for remote servers; this endpoint has none, so a "log in" or "authenticate" offer in the client's `/mcp` panel leads nowhere. How each client words a `401` here is unverified until both have been run against the box.

**What a token can do.** Exactly these nine tools (`list_nodes` and `epic_status` are `read` tools; `epic_run` needs `tasks`; the other six take an optional `node`, which stays on this board unless the board allows more, see [Using nodes from an agent](../nodes/agents-and-mcp.md#using-nodes-from-an-agent)), as the scopes allow: `read` (list projects and tasks, task status and results, read an epic), `tasks` (`create_task`, `dispatch_task` into a new session of its own, and `epic_run` for an epic whose owner allowed it) and `sessions` (`dispatch_task` with a `session`, which types the task's prompt into a session that is already running). A new token gets `read` and `tasks`; `sessions` is an explicit choice. A call outside the token's scopes is a JSON-RPC error and nothing runs. The tools cannot loosen permissions: no tool takes a permission mode, sandbox, bypass, model or extra-arguments argument, any argument a tool does not list is refused, and the tasks API's refusal of `bypassPermissions` for tasks applies as everywhere. Titles are at most 120 characters, prompts at most 20000.

**Security.**
- Off by default (`CCBOARD_MCP_REMOTE`, then the Settings switch). Off, `/mcp` answers 404 to everything.
- A request needs both an allowed Tailscale identity (`CCBOARD_ALLOWED_USERS`, as for the board) and `Authorization: Bearer <device token>`. `tailscale serve` adds the identity header only for a device owned by a user, so a tagged device is refused (403); this is how Tailscale documents it and has not been tried on the box.
- A request with an `Origin` header is refused (a web page, including one on an allowed laptop, cannot use the endpoint). Whether the real Claude Code and Codex clients ever send `Origin` is unverified; if one does, it gets a 403 and this rule has to become a same-origin check.
- Tokens are `ccbmcp_` plus 256 random bits, shown once, stored only as a SHA-256 digest (with the name, scopes, created, last used, by whom and expiry) and compared in constant time. At most 10 tokens, names unique, expiry 30, 90 (default) or 365 days. Neither a token nor its digest is logged, put in an API answer, an event or a notification; the log names the token's id and name.
- The device token opens `/mcp` only: under `/api/` any `Authorization: Bearer` is refused (403), including on the token routes. The box's hook token never opens `/mcp` and never leaves the box; the endpoint runs the tools in the board's process as `mcp:<device name>`, not over HTTP with the hook token. Tokens are minted and revoked only by a person in Settings (identity plus the `X-CCBoard` header; the hook token is refused there).
- Limits per token: 60 requests a minute, 4 at a time, 10 `create_task` or `dispatch_task` calls a minute; 240 requests a minute and 16 at a time across all tokens. One JSON-RPC message per request (no batches, 400), at most 256 KB (413). Over a limit: 429 with `Retry-After`.
- Every tool call is recorded (token id and name, tool, task id, outcome; never the prompt or a result) in a log of the last 50, and the device row in Settings shows when it was last used and for what.
- Transport: `POST /mcp` with one JSON-RPC message, answered as `application/json` (no SSE stream, no session id); a notification or a response gets 202; `GET` and `DELETE` get 405. `MCP-Protocol-Version` may be `2025-06-18` or `2025-03-26` (none at all is read as `2025-03-26`); anything else is a 400.
- Settings > Doctor, Box > "MCP from other devices": a skip while off; on, the board sends its own `/mcp` a request with an allowed identity and no token and fails the check if that gets in; a warning when the probe cannot run, when `CCBOARD_PUBLIC_URL` is empty, or for a token unused for 90 days or expiring within 7.

Not verified yet (it needs a second device and the real clients): the two commands end to end (list the tools, create a Backlog task, see it on the board, revoke, the next call refused), whether `--header` may be repeated, how each client shows a 401, whether either sends `Origin`, and what a tagged device gets.

## The MCP shim for sessions on the same machine

- **MCP server**: `scripts/ccboard_mcp.py` is registered at user scope by `install.sh`, so any Claude session on the box can call `list_projects`, `create_task`, `list_tasks` and `get_task_status`. The shim is box-only: it sends the box's hook token, so it talks only to plain `http` on a loopback address (`127.0.0.0/8`, `::1` or `localhost`) and never follows a redirect. Pointed anywhere else (`https://`, a LAN address, a tailnet name, any other name) it answers every tool call with an error and sends nothing. Another device needs the board's remote MCP endpoint with a token of its own, not a copy of the shim and the token file.
