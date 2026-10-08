# Security audit of the board's own surface (2026-10)

Issue #44. Reviewed on 2026-10-08 in a worktree of `main` plus the uncommitted phase work beside it: the remote `/mcp` endpoint and
device tokens (#13, #14), and the hash-pinned locks and SHA-pinned CI actions (#110). Everything was reproduced on a temp board,
using the test client, temp data and projects dirs and a fake tmux. Nothing was run against a live box, the real home, or the real
`claude` or `codex`.

**Threat model.** The board runs as one Unix user on the box. It listens on `127.0.0.1` and is reached through `tailscale serve`,
which injects the `Tailscale-User-Login` header. It can start agents that edit code and run commands. These callers were
considered:

- a web page in the owner's browser (CSRF), which gets the owner's identity header for free because identity is per device, not a cookie;
- a tailnet member who is not on `CCBOARD_ALLOWED_USERS`;
- a remote MCP client holding a device token;
- a local process holding the hook token;
- content the board did not write: repos, issues, notification text, clone URLs.

Severity scale:

- **High:** a caller outside the allowlist gets code execution or credentials, or the allowlisted owner is attacked without acting.
- **Medium:** the board widens the owner's exposure beyond what the owner asked for.
- **Low:** robustness or hygiene, with no new capability for an attacker.

## Route table

`tests/test_security_surface.py` builds the table from `app.routes` and pins it as `EXPECTED`. A route that is added or removed
fails `test_route_table_is_complete` until it is listed with its auth kind. Every listed route is then probed through the real
middleware; both probes stop in `auth_middleware`, so no handler runs:

- Without an identity, each route must answer 403. The exceptions are `/mcp`, which answers 404 while it is off, and the three
  hook routes, which answer "bad hook token".
- Every person-facing non-GET route must answer 403 "missing X-CCBoard header" when the identity is there but the header is missing,
  or is `true` or `0`, or when an `Origin` header is sent instead.

| Auth kind | Routes | What opens it |
|---|---|---|
| identity | 38 GET routes, plus the `/static` and `/tty` mounts | `Tailscale-User-Login` on `CCBOARD_ALLOWED_USERS` (or the dev bypass on a host dev shell) |
| identity+csrf | 75 non-GET routes | identity and `X-CCBoard: 1`. A cross-site page cannot add the header without a CORS preflight, and the board answers every preflight with 403 and no `Access-Control-Allow-*` |
| hook-token | `POST /api/hook`, `/api/permission`, `/api/deploy/gate` | the 0600 `<data dir>/hook-token` only, compared in constant time |
| identity+device-token | `POST /mcp` | off by default (404). When on, it needs identity, a live device token, no `Origin` header and no hook token |
| hub-token or identity | `GET /api/node/summary` | `CCBOARD_HUB_TOKEN` for a fleet hub, otherwise identity |
| none | `GET /healthz` (answered by the middleware, not in `app.routes`) | a constant `ok` for the compose and Dockerfile healthcheck over loopback. It reads nothing and changes nothing |

Besides the kinds above, the hook token opens every other `/api/*` route without identity or `X-CCBoard`. It never opens the four
device-token routes or `/mcp` (F-A2). A device token opens `/mcp` and nothing under `/api/*`.

`docs_url`, `redoc_url` and `openapi_url` are off. `/`, `/sw.js`, `/static/*` and `/term/*` all need identity: the issue text
assumed they answer without auth, and they do not. An unknown path also answers 403 without an identity, so it does not reveal
whether the path exists.

## Findings

Every row marked fixed has a regression test in `tests/test_security_surface.py`. Each test failed on the tree before its fix:
the failure seen is in the Evidence column, except F-07, where a probe of the same calls showed it before the test was written.
Nothing found is High.

| Id | Severity | Surface | Evidence | Status | Test |
|---|---|---|---|---|---|
| F-01 | Medium | Task previews (`POST /api/tasks/{id}/preview`, `app/previews.py`) | An explicit `{port}` skipped every filter, so any loopback port went on a new tailnet HTTPS port through `tailscale serve`. That included the board (8000), ttyd (a shell), code-server, ntfy and the claude-mem worker (no sign-in, writable API), which are the services the README tells the owner to keep off the tailnet. Before the fix, `{"port": 8000}` answered 200 and ran `serve --https=9100 http://127.0.0.1:8000`. Separately, `allocate_https_port` reserved only the board's and code-server's HTTPS ports: with `PREVIEW_HTTPS_BASE=443` it returned 443, the port of another service's Funnel on the owner's box, and it could also take ntfy's or the claude-mem viewer's port. | Fixed: `previews.infra_ports()` / `check_port()` refuse the board, ttyd, code-server, ntfy and claude-mem ports with a 400, and the autodetected list drops them too. `reserved_https_ports()` adds 443, `NTFY_HTTPS_PORT` (now `settings.ntfy_https_port`) and `CCBOARD_MEM_HTTPS_PORT`. A dev server's port still works. Why Medium: ttyd, code-server and ntfy already have tailnet mappings, so the new exposure is the claude-mem worker (any tailnet device could read and write the owner's memory), any other loopback-only service on the box, and the 443 collision. Exploiting it needs an allowed identity or the hook token. | `test_f01_a_preview_never_publishes_the_boards_own_ports`, `test_f01_preview_https_ports_skip_443_and_the_boxs_other_tailnet_ports` |
| F-02 | Low | Hook and hub token checks (`hooks.check_token`, `health.check_hub_token`) | `hmac.compare_digest` raises `TypeError` on a non-ASCII `str`. A latin-1 byte in `X-CCBoard-Token` (on any `/api/*` route or `/api/hook`) or in the hub header made the request a 500, with a traceback in the log, for a caller with no auth at all. It was never a bypass. | Fixed: both compare UTF-8 bytes and refuse a non-string, so the request is a plain 403. | `test_f02_a_non_ascii_token_header_is_refused_not_a_500`, `test_f02_token_checks_never_raise` |
| F-03 | Low | Name validation (`tmux.valid_name`, which `projects.check_name` uses; `WORKTREE_RE`, codex `SLOT_RE`) | `NAME_RE.match` with a closing `$` also matches before a trailing newline. `valid_name("shop\n")` was True, `split_name("a\n--r--s")` passed, and `POST /api/projects {"name": "shop\n"}` created a directory whose name ends in a newline. The directory was still inside `PROJECTS_DIR`: this broke names, not containment. | Fixed: `valid_name` uses `fullmatch` (the pattern itself stays shared with the wizard's JS regex). `WORKTREE_RE` and `SLOT_RE` end in `\Z`. | `test_f03_names_with_a_trailing_newline_are_refused`, `test_f03_directory_naming_regexes_refuse_a_trailing_newline` |
| F-04 | Low | `POST /api/sessions/{name}/keys` text | It refused `ord(c) < 32` only, while `/prompt` refuses every `Cc` character and dispatch drops them. DEL and the C1 range (U+0080 to U+009F, where U+009B is CSI in 8-bit terminals) passed. A bracketed-paste breakout was **not reproduced**: ESC is refused on every typing route, and tmux sends C1 code points UTF-8 encoded, so no raw 0x9B byte reaches the pane. | Fixed: one rule for every typing route (every `Cc` character except tab and newline). | `test_f04_keys_text_refuses_every_control_character` |
| F-05 | Low | Nightly backup (`backup._holds_saved_logins`) | Only `<data dir>/accounts` and `<data dir>/codex-accounts` were guarded. `CCBOARD_BACKUP_EXTRA=<Claude config dir>` or `<CODEX_HOME>` put the live `.credentials.json` or `auth.json` into the restic snapshot, which contradicted the function's own docstring. The setting is owner-configured. | Fixed: the guard also covers the live login files and every directory above them. | `test_f05_backup_extra_never_takes_the_live_logins` |
| F-06 | Low | Startup log (`app/main.py` lifespan) | `ccboard on ..., allowlist=['<login>']` wrote every allowed login, usually an email, to the journal and `docker logs`, which get pasted into bug reports. | Fixed: `startup_line()` gives the count ("1 allowed login"). | `test_f06_startup_log_names_no_login`, `test_log_hygiene_no_token_digest_password_or_login` |
| F-07 | Low | Claude extra args (`ClaudeAgent.forbidden_extra`) | Codex refuses a bare `--` and Claude did not. `extra: "--"` built `claude -- --session-id <id> --name s -- hi`, which made the board's own session id a prompt word, so that session's hooks never matched its row. Nothing was loosened. | Fixed: a bare `--` in extra args is refused on every tier. A task prompt whose first word is `--` stays plain text, as in the Codex adapter: argv already puts the prompt after its own `--`. | `test_f07_claude_extra_args_refuse_a_bare_double_dash` |
| F-08 | Low | Codex `-c key=value` lines (`CodexAgent.config_lines`) | The check is a blocklist. Probed lines that pass: `otel.exporter=...`, which can send telemetry, and prompts when `log_user_prompt` is set, to any endpoint; `experimental_instructions_file=` / `model_instructions_file=`, which read a file into the instructions; `shell_environment_policy.set.X=`, which sets an environment variable for the agent's commands; and `.mcp_servers.x.command=`, which is not the `mcp_servers` table for Codex (an empty first segment). Not verified against the binary. Only the owner types these lines (launcher, task spec, schedules); device tokens cannot pass them (`app/mcp.py` refuses unknown arguments). | Open: recommend an allowlist of known-harmless keys (`tui.*`, `model_verbosity`, `hide_agent_reasoning`, ...) in place of the blocklist. It changes what the owner may type, so it needs a product decision. | Refusals of 23 loosening spellings pinned in `test_codex_config_lines_cannot_loosen_anything` |
| F-09 | Low | Clone URLs with `user:token@` | They are accepted, then typed into the clone session's shell (shell history, tmux scrollback). git keeps them in the clone's `.git/config` remote. `bulk` echoes the URL in "cannot derive a name for <url>", and a failed queued clone keeps the URL in `clonequeue._done` (shown in state). The refusals on the probed paths do not echo the password (log-hygiene test). | Open: recommend refusing a password in a clone URL ("use `gh auth setup-git`", which install.sh already sets up), or stripping it before the clone and the queue. | `test_log_hygiene_no_token_digest_password_or_login` (the refusal paths) |
| F-10 | Low | Public repo hygiene | The demo fixture `app/static/demo/state.json` still names the owner's box (4 hits), and `deploy/docker-compose.yml` and the `Dockerfile` default to the owner's home path and user name. No token, key or email was found in fixtures, demo files or docs: the only token-shaped strings are the doctor test's sentinels and vendor hashes. | Open: already tracked as "Public repo hygiene: owner host name and home path in docs, fixtures and deploy defaults". | none (grep evidence) |

## Accepted exposures (for the owner to rule on, not clean)

| Id | Surface | What is true | Recommendation |
|---|---|---|---|
| F-A1 | ttyd (`/tty`), code-server, ntfy | These do not check `CCBOARD_ALLOWED_USERS`. Any device the tailnet ACL lets reach the box's HTTPS ports gets a shell (ttyd), an editor with a terminal (code-server), or can read and publish the ntfy topic, which holds prompts and permission summaries and has the predictable default `ccboard`. The ttyd iframes are outside the board's CSP. The README states all of this. ttyd runs with `-O` (Origin check). | Keep the tailnet single-user, or use ACL grants that limit the box's ports to the owner. Consider ttyd `--auth-header Tailscale-User-Login` (it refuses requests without an identity, such as tagged devices; it still does not check the allowlist), an unguessable `NTFY_TOPIC` from install.sh, and ntfy `auth-default-access: deny-all` with a token. |
| F-A2 | The hook token | It opens every `/api/*` route without identity or `X-CCBoard`, which can start sessions, type into terminals and answer permissions. Any process running as the box user can read the 0600 file, agents included. It never opens the device-token routes or `/mcp`, and the MCP shim refuses to send it to a non-loopback URL. | Accepted: a process running as the box user can already drive tmux directly. A narrower token for the shim (only the six calls it makes) would reduce what a leaked token does. |
| F-A3 | Loopback identity | Anything that can connect to `127.0.0.1:<port>` can send its own `Tailscale-User-Login` (README, Security model). In docker mode the container uses host networking, so other host-network containers can too. | Accepted for a single-user box. A per-boot shared secret between `tailscale serve` and the board is not possible with serve today. |
| F-A4 | `tailscale set --operator` (docker mode) | Every process of the box user can change `tailscale serve`, Funnel included (README). | Accepted; documented. |
| F-A5 | Dev bypass | `CCBOARD_DEV_BYPASS_USER` is honoured only when the runtime is `host` and no `INVOCATION_ID` is set (ignored under systemd and in the container). Launch routes refuse while the four directories could reach the real home (#100). | Clean as designed. |

## Reviewed, nothing found

- **Middleware gate order:** `/healthz`, then `/mcp` (its own gate), then a bearer refused on `/api/*`, the hub token, the hook routes,
  the hook token, identity, CSRF. CSP, nosniff and same-origin referrer are set on every identity-checked answer; the only exception
  is the dev harness's fake `/tty/` under the dev bypass. `test_pages_carry_the_csp` checks `/`, `/term/<name>`,
  `/static/term.html`, `/static/index.html` and `/sw.js`.
- **The remote `/mcp` endpoint and device tokens (#13, in this worktree, not yet on main):**
  - tokens are stored as SHA-256 digests, and `mcp_tokens.verify` compares the presented token's hex digest with every stored digest in constant time, with no early exit (read in this review); a non-ASCII bearer hashes without raising;
  - scopes are checked before anything runs;
  - unknown tool arguments are refused, so no permission mode, sandbox, bypass, model or args can be passed;
  - the tools run in-process as `mcp:<name>`, never over HTTP with the hook token;
  - `Origin` is refused, there is a 256 KB cap and rate limits;
  - the audit keeps no prompt or result;
  - a dispatch into a running session needs the `sessions` scope and only reaches a Claude or Codex session the board started, never a shell.
- **Path safety (`app/tree.py`, file preview):** `check_rel` refuses NUL, backslash, absolute paths, `..`, `.git` in any case, and
  paths over 4096 characters. `_resolve` lstats every component, refuses symlinks, and checks that the realpath stays inside the
  repo. The read uses `O_NOFOLLOW|O_NONBLOCK`. Secret-looking names need `reveal`. Existing coverage is
  `tests/test_tree.py::test_unsafe_paths_are_refused`, `test_symlinks_are_refused_everywhere`,
  `test_http_unsafe_paths_through_the_query_string`, `test_secret_names_need_reveal` and `test_fifo_does_not_hang_the_worker`.
  The one gap, names with a trailing newline, is F-03.
- **Command surface:**
  - `KEY_ALLOW` is a fixed set of named keys, with no `M-` or function keys.
  - `/command` types only the adapter's allowlisted slash commands, with checked arguments.
  - `/tune` drives fixed picker tables.
  - `/prompt`, `/keys` (after F-04) and dispatch refuse or drop ESC, so a bracketed paste cannot be ended early.
  - Clone and launch lines are built with `shlex.join`.
  - There is no `shell=True`, `os.system`, `eval` or `exec` in `app/`, `scripts/` or `bin/`.
- **Agent flags:**
  - For Claude, tasks and scheduled runs refuse every bypass and settings-override spelling. Interactive sessions refuse settings
    overrides; bypass there is an explicit acknowledgement.
  - For Codex, `FORBIDDEN_LONG`, `FORBIDDEN_SHORT` (with attached values), `--remote*`, any `dangerously`, `yolo` or `bypass`
    spelling, and a bare `--` are refused.
  - New spellings are pinned in `test_claude_task_extra_args_cannot_loosen_permissions`,
    `test_codex_extra_args_cannot_loosen_sandbox_or_approval` and `test_codex_config_lines_cannot_loosen_anything`.
  - The remaining gaps are F-07 (fixed) and F-08 (open).
- **Clone guard and preflight (#15, #22):** the host comes from a real parse; IP literals in any spelling, internal names and
  single-label names are refused. The resolver refuses any internal answer. The https preflight and clone are pinned to the checked
  addresses and follow no redirects, and `GIT_ALLOW_PROTOCOL` is https and ssh only. DNS rebinding for ssh and for clones started
  after the 10 s cache remains, as the README states (egress policy).
- **Network:**
  - The memory proxy is GET-only to a loopback worker; a non-loopback `CLAUDE_MEM_WORKER_HOST` is refused, and write-back is off by default.
  - `previews.py` only ever serves `http://127.0.0.1:<port>`, now without the board's own ports (F-01).
  - `github.py`, `prpoll.py`, `issues.py` and `gitops.py` pass argv lists.
  - Push endpoints are limited to known browser push services.
- **Browser:**
  - `tests/test_static.py` forbids `innerHTML`, inline scripts, styles and handlers, and external URLs across `app/static`.
  - `sw.js` opens only same-origin URLs (`sameOrigin`), and `router.js` accepts a `nav` message only for a `#/` or `#s=` hash.
  - Notification actions POST same-origin with `X-CCBoard`.
- **Credentials and logs:**
  - `test_log_hygiene_no_token_digest_password_or_login` runs a representative slice under DEBUG capture: the full startup, the
    remote switch, a device token minted, used, refused and revoked, the hook token right and wrong, a hook event, and three clone
    URLs carrying a password.
  - It asserts that no device token, digest, hook token, password or login appears in the log, that `/api/state` and the token
    listing hold no token, digest or password, and that the database holds no raw token.
  - The only failure was F-06.
  - Scope, a deliberate departure from the issue text: the test looks for the literal secrets and the login, not for any home path. `PROJECTS_DIR` stays in the startup line because it is the operator's own configuration, and conftest forbids asserting on temp-dir prefixes.
  - Saved logins are opaque bytes in 0700 / 0600 stores and never parsed or returned; the board never reads Codex's `auth.json`.
- **CORS:** a preflight is refused with 403 and no allow headers (`test_a_cors_preflight_is_refused_and_never_allowed`).

## Not verified

- ttyd's own access control, and that `tailscale serve` deletes client-sent `Tailscale-User-*` headers. That deletion is upstream
  behaviour and the board relies on it for tagged devices; it was not tested here.
- The F-08 key semantics in the Codex binary.
- Anything merged after 2026-10-08.
- The CI and image changes of #110 have not run in CI, and the image was not built here.

## Dependencies and image (recommendations, no change required)

- **Already pinned in this phase (#110):** the Python dependencies are exact versions with hashes (`requirements.lock`,
  `--require-hashes` in the Dockerfile and CI); `ccusage` is `20.0.26`; every CI action is pinned to a commit SHA; Dependabot is
  weekly for pip, actions and docker; and `pip-audit` runs in CI without blocking.
- **Floating:**
  - `FROM ubuntu:24.04`: pin a digest (`ubuntu:24.04@sha256:...`) and let Dependabot's docker ecosystem move it.
  - The `apt-get install` lines take whatever the archive has at build time. Acceptable for an Ubuntu base; a digest-pinned base
    makes rebuilds comparable.
  - Three third-party apt repositories (GitHub CLI, NodeSource, Tailscale) are fetched at build time with their keys from the same
    hosts. Pin the key fingerprints and check them in the build.
  - `nickfedor/watchtower:1` is a floating tag with access to the Docker socket. Pin a digest; Watchtower replaces the board
    automatically, so a compromised tag runs code with Docker socket access on the box.
  - The board image is pulled by tag (`:latest` by default) and swapped by Watchtower. Consider signing the image in CI (cosign)
    and verifying the signature before a swap.

## Follow-up issues to open (one per open finding, label `follow-up`)

- F-08 (Low): Codex `-c` lines: an allowlist in place of the blocklist.
- F-09 (Low): clone URLs with a password: refuse them, or strip the password before the shell and the queue.
- F-A1 (accepted, owner to rule): ttyd, code-server and ntfy behind the tailnet ACL only. Options: ttyd `--auth-header`, an unguessable ntfy topic, ntfy auth.
- F-A2 (accepted): a narrower token for the MCP shim.
- Supply chain: digest-pin `ubuntu:24.04` and `nickfedor/watchtower:1`, check the apt key fingerprints, and sign the image.
- F-10 is already tracked by the public repo hygiene issue.
