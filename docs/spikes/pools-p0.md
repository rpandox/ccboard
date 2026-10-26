# Pools P0 spike notes

For the maintainer who builds the account pools: what the installed Claude Code and Codex CLIs do when a session is resumed under a second login, where the usage numbers and the subscription id come from, and which questions only a run with two real logins can settle.

[Back to the documentation index](../README.md)

Decision line: pending the owner's run. The proposal from the research is at the end of this page.

Nothing on this page was observed with a real login. The answers come from the installed binaries read as text, from the Codex source at a pinned commit, from the vendor docs and from ccboard's own code. The part that needs two logins is a script, [How to run the pools spike](#how-to-run-the-pools-spike), and the list of what it settles is at the end.

Each answer carries a status:

| Status | Meaning |
|---|---|
| from source | Read in the Codex source at the pinned commit, or in the embedded JavaScript of the installed Claude Code binary. |
| from docs | Stated by the vendor documentation. |
| UNVERIFIED | Read in code but never observed, or not found either way. |
| needs the two-login run | Cannot be answered without logging in twice. |

Pins used throughout:

- Claude Code 2.1.303. The binary is a single-file executable that embeds readable JavaScript. "bundle" below means the literal strings of that binary (`strings -a <binary>`). Minified function names change between releases, so only literal strings are quoted here.
- Codex 0.162.1: tag `rust-v0.162.1`, commit `092d3acd6bec3e3a14bdc7e7a2810ab628ab759d`. Cites are `codex-rs/<path>:<line>` at that commit.
- Claude docs: `https://code.claude.com/docs/en/<page>`, cited by page and section.
- ccboard: `app/<file>:<line>` at commit `41396ea` (main at 0.5.109).

## What changes the design of the pools

These are the findings that contradict or add to the epic text. Each is detailed in the tables below.

1. Claude's subscription identity is not in `.credentials.json`. The credential block has no uuid at all. Identity lives in the state file `<config dir>/.claude.json`, under `oauthAccount` (`accountUuid`, `organizationUuid`). A vault bundle for a Claude account must carry that slice next to the credential bytes, and the adapter reads exactly those two fields, keyed as a pair.
2. Claude's usage fetch is rate-limited. The CLI itself remembers a 429 or 403 for the `Retry-After` value (5 minutes when absent, at most 1 hour). A 60 second poll per account will hit that wall. Poll per token every 5 minutes or slower, honour `Retry-After`, and prefer a source that makes no authenticated call (the profile's cached value, the statusline, or a `/usage` run) unless the collector owns the refresh chain.
3. A direct poll of the Claude usage endpoint needs a live access token. A poller that refreshes it races the process that owns the chain, because the CLI saves refreshed tokens with a compare-and-swap on the stored refresh token. The poller must never refresh on its own.
4. Codex: sharing `sessions/` is necessary but not sufficient on 0.162.1. Each home also holds `state_5.sqlite`, `thread_history_1.sqlite`, `logs_2.sqlite`, `session_index.jsonl` and `history.jsonl`. New threads are paginated and project into the SQLite files, and listing a thread's turns fails when that home's database has no row for it. Share one `CODEX_SQLITE_HOME` across the Codex profiles (or test that it is not needed), and always resume by thread UUID, never by name or `--last`.
5. Codex: the default credential store is `file`, so the macOS Keychain is not involved unless `config.toml` says `keyring` or `auto`. Pin `cli_auth_credentials_store = "file"` in every profile. (Claude on macOS does use the Keychain, one item per config dir; see below.)
6. Codex usage attribution from the rollout tail is unsound under rotation. A rollout records the account that created the thread and never updates it, so after B resumes a thread A created, B's `token_count` rate limits are appended to a file whose first line names A, and ccboard's `_owner()` would credit them to A. Attribute by the profile the process was launched with, or use the usage endpoint.
7. Codex exposes window usage outside the TUI: the app-server JSON-RPC method `account/rateLimits/read` calls a backend usage endpoint. Claude exposes it through the statusline JSON (`rate_limits`) and `/usage`.
8. Deduplication keys. Claude: the pair `organizationUuid` + `accountUuid`; the organization alone is wrong for Team and Enterprise (several seats share one). Codex: `tokens.account_id` is one workspace identifier, so it is a right key for a personal login and probably a wrong one for a workspace plan, where it needs the user id as well.
9. The environment scrub is longer than the epic lists. Claude: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN` (outranks the stored login), `ANTHROPIC_PROFILE`, `ANTHROPIC_FEDERATION_*`, `CLAUDE_SECURESTORAGE_CONFIG_DIR`, `CLAUDE_CODE_ACCOUNT_UUID`, `CLAUDE_CODE_USER_EMAIL`, `CLAUDE_CODE_ORGANIZATION_UUID`, `CLAUDE_CODE_PROJECT_DIR_NAME`, `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`, `CLAUDE_CODE_USE_FOUNDRY`. Codex: `OPENAI_API_KEY`, `CODEX_API_KEY`, `CODEX_ACCESS_TOKEN`, `CODEX_HOME`, `CODEX_SQLITE_HOME`.
10. On macOS the Claude Keychain item name hashes the config dir string exactly as given. A trailing slash or a symlinked spelling of the same directory is a second item, so the pool must always pass one canonical absolute path.
11. A resumed Claude session keeps its stored cost total (`cost-state`), and its first turn under B has a cold prompt cache (caching is per organization). Per-account cost must not be read from the transcript total as "spent on B".
12. Never resume a session while the old process is alive: Claude's docs say two terminals on one session interleave into one transcript, and Codex's writer locks are per home, so two profiles holding one thread are not mutually excluded. The drain waits for the old process to exit.
13. `CLAUDE_CODE_PROJECT_DIR_NAME` (docs, 2.1.234 and later; honoured only when `CLAUDE_CONFIG_DIR` is also set) pins the transcript directory name, so the same task lands at the same place under any working directory or node path.
14. Two copies of one Claude transcript in different project directories make the id "not found" instead of resolving. This matters for transcript sync between nodes.

## Claude Code

### A second profile

| Question | Answer | Source | Status |
|---|---|---|---|
| Where does a second login live? | `CLAUDE_CONFIG_DIR`. The directory is `(CLAUDE_CONFIG_DIR ?? <home>/.claude)`, memoised, no realpath and no trailing-slash strip. | bundle: the config-dir function; docs `authentication`, "Log in with multiple accounts" | from source, from docs |
| What moves with it? | `<dir>/.credentials.json`, `<dir>/settings.json`, `<dir>/projects/<key>/<session id>.jsonl`, and the global state file `<dir>/.claude.json` when the variable is set (otherwise the file next to the home directory). Also per dir: `sessions`, `todos`, `file-history`, `shell-snapshots`, `history.jsonl`, `plans`, `jobs`, `teams`, `statsig`, `ide`, `logs`, `backups`. | bundle: the resume-bundle helper builds a temp config dir from exactly the first four | from source |
| What does sharing only `projects/` leave behind? | File history (rewind checkpoints) and todos of a resumed session start empty under the other profile. Whether that matters is not known. | bundle | UNVERIFIED |
| How is the transcript directory named? | The working directory with every character outside `[a-zA-Z0-9]` replaced by `-`; above 200 characters it is cut to 200 and a base-36 hash is appended. | docs `sessions`, "Where transcripts are stored" | from docs |
| Can `projects/` be a symlink? | Every transcript access found (`mkdir -p`, `readdir`, `stat`, `appendFile`, `readFile`) follows symlinks, and the only `O_NOFOLLOW` found is in an unrelated module. The docs forbid nothing. | bundle | UNVERIFIED, needs the two-login run |
| macOS Keychain: is the item keyed by config dir? | Yes. Service `Claude Code-credentials` when `CLAUDE_CONFIG_DIR` is unset, otherwise `Claude Code-credentials-<first 8 hex of sha256(dir string)>`. `CLAUDE_SECURESTORAGE_CONFIG_DIR` overrides the string that is hashed. The account name is the login user name when it matches `^[a-zA-Z0-9._-]+$`, else `claude-code-user`. The value is the same JSON as `.credentials.json` (top level `claudeAiOauth`). | docs `authentication`, "Credential management"; bundle: the secure-storage service-name function and the `security` command literals | from docs, from source; the exact name needs the run |
| Is there a file fallback? | Yes. When the Keychain rejects a write (locked, for example over SSH) the CLI falls back to `<dir>/.credentials.json`. A Mac node driven from tmux over SSH will probably land on the file. Both paths are in scope for the Mac adapter. | docs `authentication`; bundle | from docs |
| What does ccboard do on macOS today? | It refuses mutating calls (`REASON_MACOS`, `app/account_store.py:78`) and lists the Keychain question as a follow-up (`app/account_store.py:250`). | ccboard | from source |

### Resume

| Question | Answer | Source | Status |
|---|---|---|---|
| What does `--resume <id>` read? | Only `<config dir>/projects`: first the current project directory, then other git worktrees of the repository, then a scan of every project directory. The scan resolves only when exactly one other project holds a transcript with messages for that id; otherwise "No conversation found". `--resume <path to .jsonl>` is also accepted. | docs `cli-reference` (`--resume`) and `sessions`, "Resume by session ID or name"; bundle events `tengu_resume_worktree_fallback`, `tengu_transcript_id_scan_fallback` | from docs, from source |
| Is anything account-tied in a message row? | No. A row carries `sessionKind, userType, entrypoint, cwd, sessionId, version, parentUuid, isSidechain, gitBranch, slug, promptId, agentId`; `userType` is the constant `external`. | bundle: the entry builder | from source |
| Side rows that carry account fields | `memory-mode` rows carry `account` and `accountChoice` (`on`, `read_only`, `off`); `bridge-session` rows carry `bridgeOwnerAccountUuid` and `bridgeOwnerOrganizationUuid` (Remote Control only); `atis-latch` replays a latched request-header value; `cost-state` is restored. What they do under a different login is not known. | bundle | UNVERIFIED, needs the two-login run |
| What else comes back on resume? | The system prompt recorded at the first request, the model and the permission mode. | docs `cli-reference`, "System prompt flags in resumed conversations"; `sessions`, "What a resumed session restores" | from docs |
| What happens to usage and cost? | The stored cost total continues under the new login. The first turn under B probably misses the prompt cache (caching is per organization), which is a one-turn input spike on B's window, not an error. | bundle; reasoning | UNVERIFIED, needs the two-login run |
| Is there a hazard for the drain? | "If you resume the same session in two terminals without forking, messages from both interleave into one transcript." | docs `sessions` | from docs |
| Does `cachedUsageUtilization` follow a shared directory? | It lives in `.claude.json`, which is per profile and never shared. The CLI drops it when its `accountUuid` is not the current one. | bundle | from source |

### Usage endpoint

| Question | Answer | Source | Status |
|---|---|---|---|
| URL and method | `GET <API base>/api/oauth/usage`, the API base being `https://api.anthropic.com` in production. Variants with `?at_wall=1&skip_spend=1` or `?cedar_ember=1&skip_spend=1` serve the limit wall and experiments. | bundle: literal `/api/oauth/usage`, `fetchUtilization` | from source |
| Headers | `Authorization: Bearer <claudeAiOauth.accessToken>`, `anthropic-beta: oauth-2025-04-20`, `Content-Type: application/json`, a `User-Agent` of the form `claude-cli/<version>`. Timeout 5000 ms; only a 2xx is accepted. | bundle | from source |
| When is it skipped? | Unless the active credential is a claude.ai OAuth login with inference scope and the token scopes include `user:profile`. A token from `setup-token` cannot call it ("can only make model requests"). | bundle; docs `authentication`, "Generate a long-lived token" | from source, from docs |
| Body fields | `five_hour`, `seven_day`, `seven_day_oauth_apps`, `seven_day_opus`, `seven_day_sonnet`, `cinder_cove`: each `{utilization, resets_at}`, both nullable. `wattle_ember` adds `label`, `limit_dollars`, `used_dollars`, `remaining_dollars`, `periods`. `extra_usage`: `{is_enabled, monthly_limit, used_credits, utilization, currency, disabled_reason}`. `limits[]`: `{kind, group, percent, resets_at, severity, is_active, scope}` including the per-model weekly windows. The schema is `passthrough`. | bundle: the response schema | from source |
| Units | Endpoint `utilization` is percent 0 to 100 (the `/usage` dialog prints `floor(utilization)% used` straight from the body); `resets_at` is ISO 8601. The rate-limit response headers differ: `anthropic-ratelimit-unified-5h-utilization` is a fraction, resets are epoch seconds. The statusline JSON gives percent (`rate_limits.five_hour.used_percentage`) with epoch-second `resets_at`. | bundle; docs `statusline`, "Available data" | from source for the endpoint (not observed live), from docs for the statusline |
| Free sources | The headers `anthropic-ratelimit-unified-{5h,7d}-{utilization,reset,surpassed-threshold}`, `-status`, `-representative-claim`, `-overage-*`, `-fallback` arrive on every model response. The statusline `rate_limits` are present only for Pro and Max, only after the first API response, and only while `resets_at` has not passed. | bundle; docs `statusline` | from source, from docs |
| The CLI's own polling | It writes `cachedUsageUtilization` to `.claude.json` at most once a minute and treats the cache as valid for 1 hour. A 429 or 403 is remembered per token for `Retry-After` or 300 s, capped at 1 hour; an auth rejection for 1 hour. In-flight fetches are de-duplicated on `organizationUuid:accountUuid`. | bundle | from source |
| Token refresh | Before a call the CLI refreshes the token when needed (`POST` to the platform token endpoint, refresh-token grant) and saves the new pair with a compare-and-swap on the stored refresh token. Events named `tengu_oauth_refresh_compromised_cas_*` imply refresh tokens rotate. | bundle | from source |
| What does ccboard read today? | The CLI's cache in the profile's `.claude.json` (`app/accounts.py:309-358`, polled at `:406-421`) matches the CLI's schema field for field. `app/usage.py` (`POLL_SECONDS = 120`) reads the statusline `rate_limits` and `ccusage blocks`; `app/usage_refresh.py` types `/usage` into an idle Claude pane so the CLI refreshes the cache. Nothing in ccboard calls the endpoint. | ccboard | from source |
| Source order for P2 | 1. The profile's cache file: free, no credential, stale when idle. 2. A `/usage` run in the profile: confidence about 0.9, needs a live session. 3. A direct call with the profile's bearer: best fidelity, but the poller reads the access token, must never refresh, and treats a 401 as a request to the owner or `needs_login`. | derived | design input |
| Does a live body match? | Percent 0 to 100, ISO `resets_at`, the real `limits[]`, the 429 pattern at a short interval, the shape of a 401. | | needs the two-login run |

### Subscription identity

| Question | Answer | Source | Status |
|---|---|---|---|
| Where is it? | In `<config dir>/.claude.json` under `oauthAccount`, not in `.credentials.json` or the Keychain item. The credential block `claudeAiOauth` holds `accessToken, refreshToken, expiresAt, refreshTokenExpiresAt, scopes, subscriptionType, rateLimitTier, clientId` and no identity. | bundle: the account-info store and the profile fetch | from source |
| `oauthAccount` field names | Identity: `accountUuid`, `organizationUuid`, `emailAddress`, `displayName`, `fullName`, `organizationName`. Plan: `organizationType`, `userRateLimitTier`, `organizationRateLimitTier`, `seatTier`, `billingType`, `planDisplayName`, `hasExtraUsageEnabled`. Other: `accountCreatedAt`, `subscriptionCreatedAt`, `ccOnboardingFlags`, `claudeCodeTrialEndsAt`, `claudeCodeTrialDurationDays`, `profileFetchedAt`. | bundle | from source |
| What may the adapter read? | Exactly two fields: `oauthAccount.accountUuid` and `oauthAccount.organizationUuid`, keyed as the pair. It is the pair the CLI keys its own usage fetch on and what ccboard uses today (`app/accounts.py:31`). A plan label may come from `claudeAiOauth.subscriptionType`, `rateLimitTier` or `oauthAccount.planDisplayName`. | bundle; ccboard | from source |
| Is the organization alone enough? | No. For Team and Enterprise several seats share an organization. Whether their windows are per seat or per organization is not known, so organization-only dedup could be wrong. Dedup then collapses only the same login present twice. | reasoning | UNVERIFIED, needs the two-login run |
| A credential-free identity read? | `claude auth status` prints `loggedIn, authMethod, apiProvider, configDirectory, projectsDirectory, email, orgId, orgName, subscriptionType` but no `accountUuid`. Whether it makes a network call is not known, so the poller does not use it. | docs `cli-reference`; bundle literals | from docs; network use UNVERIFIED |
| What does a vault bundle carry? | A version, the `.credentials.json` bytes and the `oauthAccount` object. Moving only the credential bytes leaves the CLI to refetch the profile on first start, which works but needs the network. | design | design input |

## Codex

### The home directory

| Question | Answer | Source | Status |
|---|---|---|---|
| How is `CODEX_HOME` resolved? | The environment variable, ignored when empty, must exist and be a directory, and is canonicalized. Unset means the default under the user's home. So a profile dir is created before launch, and a symlinked profile dir resolves to its target. | `codex-rs/utils/home-dir/src/lib.rs:13-50` | from source |
| What is in it? | `auth.json` (`login/src/auth/storage.rs:162-164`), `config.toml` (`core/src/config/mod.rs:270`), `sessions/YYYY/MM/DD/rollout-<time>-<thread uuid>.jsonl` and `archived_sessions/` (`rollout/src/lib.rs:86-87`; path built at `rollout/src/recorder.rs:1741-1765`), `history.jsonl` (`message-history/src/lib.rs:52`), `session_index.jsonl` (`rollout/src/session_index.rs:20`), locks under `thread-writer-locks/` (`rollout/src/writer_lock.rs:15`) and `.tmp/` (`rollout/src/maintenance.rs`), `log/`, skills, and an MCP `.credentials.json` in file mode (`config/src/types.rs:157-158`). | as listed | from source |
| The SQLite files | `state_5.sqlite`, `logs_2.sqlite`, `thread_history_1.sqlite`, in the SQLite home, which defaults to `CODEX_HOME`. Redirect with the `sqlite_home` config key or the `CODEX_SQLITE_HOME` variable. | `state/src/sqlite.rs:34-39`; `state/src/lib.rs:133`; `core/src/config/mod.rs:4112-4124` | from source |
| Fields of `auth.json` | `auth_mode, OPENAI_API_KEY, tokens, last_refresh, agent_identity, personal_access_token, bedrock_api_key, bedrock_access_keys`. `tokens`: `id_token` (raw JWT string), `access_token`, `refresh_token`, `account_id`. | `login/src/auth/storage.rs:48-75`; `login/src/token_data.rs:10-24` | from source |
| Default credential store | `file`. | `config/src/types.rs:136-146` (`#[default] File`) | from source |
| Keychain names, when the store is `keyring` or `auto` | Default backend on macOS and Linux is direct: service `Codex Auth`, account `cli|<first 16 hex of sha256(canonical CODEX_HOME string)>`, value the serialized auth. A secrets backend (default on Windows) uses service `codex` and account `secrets|<same hash>` for an encryption key only. `auto` tries the keyring and falls back to the file. Saving to the keyring deletes `auth.json`. Two profile symlinks that canonicalize to one directory share one item. | `login/src/auth/storage.rs:243-257, 315-332, 453-546`; `config/src/types.rs:168-181`; `secrets/src/lib.rs:23, 184-205` | from source; the live item needs the run |
| Can `sessions/` be a symlink to a shared directory? | By reading, yes. The filename scan follows a symlinked root and skips symlinked files and nested symlinked directories; new rollouts use create-dir plus append with no symlink check; the thread store handles a symlinked home root by comparing canonical paths. A path component named `archived_sessions` marks a rollout archived, so the shared directory must not contain that name. | `rollout/src/list.rs:1443-1475`; `rollout/src/recorder.rs:1741-1776`; `thread-store/src/local/rollout_lineage.rs:112-135`; `thread-store/src/local/helpers.rs:64-69` | from source, needs the two-login run |
| What stays per home if only `sessions/` is shared? | `state_5.sqlite`, `thread_history_1.sqlite`, `session_index.jsonl`, `history.jsonl`, the writer locks and the maintenance lock. | as above | from source |
| Is there a shared daemon? | The plain TUI runs an in-process app server. `codex app-server daemon` and `codex agents` use a shared local daemon; keep them out of pools. How the daemon keys on `CODEX_HOME` beyond forwarding it is not known. | `tui/src/lib.rs:700-725`; `app-server-daemon/src/background_command.rs:16-31` | from source; daemon keying UNVERIFIED |

### Resume

| Question | Answer | Source | Status |
|---|---|---|---|
| How is a thread found? | By UUID: the live writer, then the SQLite row's `rollout_path` (checked against the file's own `session_meta`), then a filesystem scan of `<home>/sessions` for the file name containing the id (newest wins), then the archive. With an empty database under profile B it still finds the file through the shared `sessions/`. Resume by name uses the per-home `session_index.jsonl`; `--last` and the picker use the per-home database. So always pass the UUID. | `thread-store/src/local/thread_rollout_resolver.rs:67-146`; `rollout/src/list.rs:1248-1300, 1412-1440, 1501-1508` | from source |
| Where does the model context come from? | Rebuilt from the rollout JSONL, not from the database; resume reopens the same file in append mode. | `thread-store/src/local/live_writer.rs:40-97`; `thread-store/src/local/model_context.rs:60-90`; `rollout/src/recorder.rs:1173-1186, 1776` | from source |
| What is new in 0.162.1? | New threads default to `history_mode = paginated` whenever a state database exists. JSONL stays canonical and `thread_history_1.sqlite` is a rebuildable view projected on write, flush, shutdown, fork and revert. Listing turns or items (what a UI uses to show history) returns `Unsupported` when the thread has no row in this home's database. No materialize-on-resume was found. | `app-server/src/request_processors/thread_processor.rs:1462-1465`; `thread-store/src/local/mod.rs:730-732`; `thread-store/src/local/thread_history/read.rs:188-217` | from source; effect UNVERIFIED, needs the two-login run |
| Is the account checked on resume? | Recorded, never enforced. `SessionMeta.creator_user_id` and `creator_account_id` are written when the thread is created and "never updated on resume"; the first value wins in the database. No comparison with the current login was found. The only "another account" text concerns token refresh. | `protocol/src/protocol.rs:3152-3158`; `core/src/session/session.rs:1037-1038`; `state/src/extract.rs:88-95`; `login/src/auth/manager.rs:211` | from source |
| Encrypted reasoning from account A replayed under B | Requests ask for `reasoning.encrypted_content` and rollouts store the blobs. Whether the backend accepts them from another account cannot be read from source. | `core/src/client.rs:961` | UNVERIFIED, needs the two-login run |
| Does a resumed process inherit old rate limits? | No. `latest_rate_limits` starts empty and merges only within a process. | `core/src/state/session.rs:77, 129, 348-352` | from source |
| Credential precedence | `CODEX_API_KEY` (only where the entry point enables it; the TUI does not), `CODEX_ACCESS_TOKEN` (always consulted), `OPENAI_API_KEY`; the in-memory store wins over persisted auth. Refresh tokens are single use: reuse gives `refresh_token_reused` and "log out and sign in again". That confirms one live owner per credential chain. | `login/src/auth/manager.rs:207, 953-958, 1337, 1503-1545, 1688, 1702`; `tui/src/lib.rs:719` | from source |
| Headless equivalent | `codex exec resume <uuid> "<prompt>"` exists in the installed CLI. Whether it behaves like the TUI resume, including the working-directory filter, is not known. | `codex exec resume --help` on 0.162.1; `tui/src/lib.rs` `effective_resume_cwd_mode` | UNVERIFIED, needs the two-login run |

### Usage

| Question | Answer | Source | Status |
|---|---|---|---|
| Response headers | On every model call: `x-codex-primary-used-percent`, `-window-minutes`, `-reset-at`, the same three for `secondary`, `x-<limit>-limit-name`, `x-codex-credits-has-credits`, `-unlimited`, `-balance`, `x-codex-promo-message`, `x-codex-rate-limit-reached-type`. | `codex-api/src/rate_limits.rs:57-96, 181-233` | from source |
| Stream event | `codex.rate_limits` with `plan_type`, `rate_limits.{primary,secondary}.{used_percent,window_minutes,reset_at}`, `credits`, `limit_name`. | `codex-api/src/rate_limits.rs:135-170` | from source |
| Endpoint | `GET <chatgpt base URL>/wham/usage` when the base URL contains `/backend-api` (the default, `https://chatgpt.com/backend-api/`), else `/api/codex/usage`. Auth: the login's bearer plus the header `ChatGPT-Account-ID`. Body: `plan_type`, `rate_limit{allowed, limit_reached, primary_window, secondary_window}`, `credits`, `spend_control`, `additional_rate_limits[]`, `rate_limit_reached_type`. A window: `used_percent` (integer), `limit_window_seconds`, `reset_after_seconds`, `reset_at` (epoch seconds). | `backend-client/src/client/rate_limit_resets.rs:124-129`; `backend-client/src/client.rs:140-156, 741-754, 812-819`; `model-provider/src/auth.rs:106`; `core/src/config/mod.rs:4455`; the generated `rate_limit_status_payload` models | from source |
| Outside the TUI? | Yes: the app-server JSON-RPC method `account/rateLimits/read`, which needs ChatGPT auth. `codex app-server` exists in the installed binary. | `app-server-protocol/src/protocol/common.rs:1328`; `app-server/src/request_processors/account_processor.rs:1190-1222` | from source; the call itself needs the run |
| In the rollout | `event_msg` `token_count` carries `rate_limits` with windows `{used_percent (float), window_minutes, resets_at}`. | `protocol/src/protocol.rs:2372-2430`; `rollout/src/policy.rs` | from source |
| What does ccboard read today? | Only the passive rollout tail (`app/agents/codex_rollout.py`: `parse_tail`, `_window`, `_rate_limits`, `_select`), attributed by the rollout's `creator_account_id` (`_owner`). Windows are classified by `window_minutes` (5 hours at 360 or less, weekly at 7200 or more), not by `primary` and `secondary`. No endpoint call, and nothing is read for an idle account. Consumers: `app/main.py:965`, `app/scheduler.py:100`, `app/taskflow.py:259`. | ccboard | from source |
| Which window is primary on each plan? | Not known; classify by minutes. | | UNVERIFIED, needs the two-login run |

### Subscription identity

| Question | Answer | Source | Status |
|---|---|---|---|
| Where is it? | Claims of the `id_token` under the namespace `https://api.openai.com/auth`: `chatgpt_account_id`, `chatgpt_user_id` (fallback `user_id`), `chatgpt_plan_type`, `chatgpt_account_is_fedramp`. A further claim `chatgpt_account_user_id` (one workspace membership) is read from the access token and accepted only when its account id matches. | `login/src/token_data.rs:72-101, 112-120, 152-200`; `login/src/auth/manager.rs:662-673` | from source |
| The one field the adapter may read | `tokens.account_id` in `auth.json`, a plain string written at login from the `chatgpt_account_id` claim and sent as `ChatGPT-Account-ID`. No JWT decoding is needed. A refresh does not rewrite it. It exists only for the `file` store; with `keyring` or `auto` there may be no `auth.json`, and the fallback is `session_meta.creator_account_id`. | `login/src/server.rs:853-861`; `login/src/token_data.rs:21-23`; `login/src/auth/manager.rs:1596-1618`; ccboard `app/codex_accounts.py:1155-1210` | from source |
| Is it a right dedup key? | The claim is documented as an "Organization/workspace identifier". For a personal Plus or Pro login it is probably per user. For Team, Business and Enterprise it is the workspace, shared by every seat, and the reached-type values have workspace-member and workspace-owner variants, which implies per-member limits. Proposed key: the account id alone for non-workspace plans; the account id plus the user id (or the membership id) for workspace plans. | `login/src/token_data.rs:58-63`; `protocol/src/protocol.rs:2397-2403` | UNVERIFIED, needs the two-login run |

## How to run the pools spike

[`scripts/dev/pools_spike.py`](../../scripts/dev/pools_spike.py) is the part of the spike that needs your two logins. It uses the standard library only (Python 3.11 or newer), and you run it by hand on a machine that has `claude` and `codex` installed:

```
python3 scripts/dev/pools_spike.py --dry-run     # prints every command, starts no CLI
python3 scripts/dev/pools_spike.py               # the real run
```

Options: `--only claude|codex|both`, `--base DIR` (where to create the scratch tree, default the system temp directory), `--codex-sqlite shared|separate` (default `shared`; run it once more with `separate` to test the empty-database case).

What it does, in order:

1. Creates a scratch tree under the temp directory and prints it: `claude/A`, `claude/B` (each `projects/` is a symlink to one shared directory), `codex/A`, `codex/B` (each `sessions/` is a symlink to one shared directory, each `config.toml` pins `cli_auth_credentials_store = "file"`), one shared `CODEX_SQLITE_HOME`, a working directory and a results directory. It refuses to run when a profile would resolve into the real Claude or Codex home, or where `CLAUDE_CONFIG_DIR`, `CODEX_HOME` or `CODEX_SQLITE_HOME` in your own environment already point.
2. For each profile it prints the login command (`CLAUDE_CONFIG_DIR=<A> claude auth login`, `CODEX_HOME=<A> codex login --device-auth`, and the same for B) and waits for Enter while you run it in another terminal. It then asks the CLI's own status command whether the login worked. It never opens a credential file; it only asks whether the expected file exists.
3. Claude: three turns in A (`claude -p ... --session-id <id> --output-format json`, then `--resume <id>`), the first one giving a word, then the question "What was the word I gave you in turn 1?" in B. Codex: the same with `codex exec` and `codex exec resume <thread id>`.
4. Records whether B answered with the word, the account B reports (an address is masked, no id is printed), whether the transcript landed in the shared directory and not in the real home, the token counts of B's first turn (the prompt-cache question), and which top-level entries of the real `~/.claude`, `~/.claude.json` and `~/.codex` changed (names and modification times only; a session of yours running at the same time also changes them, so read the names).
5. Records usage only through the CLIs' own commands: `claude -p /usage`, the Codex app-server method `account/rateLimits/read` (the numbers only), and a line you type from `/status` and `/usage` in an interactive B. The script makes no authenticated HTTP call of its own.
6. Prints the macOS Keychain commands you can run to look for the expected items (attributes only, no `-w`). The script itself never queries the Keychain.
7. Writes `results.json`, `summary.md` and `issue-comment.md` into the scratch tree's `results/` directory and prints the comment, ready to paste into the issue (results only).

Every command runs without the inherited login variables listed in finding 9. Run the login commands in a shell where they are unset too. The scratch tree holds two real logins until you delete it; the last lines of the run print the `rm -rf` command.

Two checks stay by hand because a print-mode run cannot do them: stop an interactive session with Ctrl-C and resume it in B (`CLAUDE_CONFIG_DIR=<B> claude --resume <id>`, `CODEX_HOME=<B> codex resume <thread id>`), and look at `/status` for the account.

## What the owner's run settles

From the script's results:

1. Claude, resume under B: `claude.context_present`, the error of turn 4 if any, `claude.account_b_after_resume`.
2. Claude, a symlinked `projects/`: `claude.shared_transcript_present` is true, `claude.real_home_transcript_present` is false, and the resume in B worked through it.
3. Claude, prompt cache: `cache_creation_input_tokens` against `cache_read_input_tokens` in B's turn, and how large the first-turn spike is for a long context.
4. Claude, macOS Keychain: whether the item named `Claude Code-credentials-<8 hex>` exists per profile, whether the two coexist without a prompt, and whether `claude.login.*.credentials_file_exists` is true (the file fallback).
5. Codex, resume under B with a shared SQLite home, then again with `--codex-sqlite separate`: does `codex exec resume` keep the context, and does the interactive `codex resume <thread id>` in B show the earlier turns or fail to list them.
6. Codex, encrypted reasoning: does turn 4 in B succeed after a thread created in A, in a same-workspace pair and in a different-workspace pair.
7. Codex, the rollout and the real home: `codex.shared_transcript_present`, `codex.real_home_transcript_present` false, and no change to `~/.codex` in `real_home_changed`.
8. Codex, Keychain: with the file store pinned, no `Codex Auth` item per profile.
9. Usage: what `claude -p /usage` prints (and whether print mode answers at all), the `account/rateLimits/read` numbers for B, and the figures you type from `/usage` and `/status`.
10. Whether A and B were different accounts. For Claude the script compares the address and organization the CLI reports (`claude.accounts_differ`). Codex's status line may print no identity, and then `codex.accounts_differ` is unknown: take it from the account line you type from `/status` in each Codex profile. If A and B turn out to be the same account, the run proves nothing about a cross-account resume.

By hand, with the same scratch profiles:

11. Claude, the `memory-mode`, `atis-latch` and `bridge-session` rows written under A: does B ignore, refuse or restamp them, and does anything change in what B can do in the resumed session.
12. Claude, a second account in the same organization (a Team seat): are `five_hour` and `seven_day` per account or per organization. Decides whether the organization alone could ever be a dedup key.
13. Claude, token ownership: after copying a profile's credentials to a second location, does one side's refresh invalidate the other's refresh token. Fixes the one-live-owner rule for the vault.
14. Claude, rewind checkpoints and todos of a resumed session under the other profile: lost, and do they need sharing.
15. Claude, does `claude auth status` make a network call. If not, it is a safe identity read for the health check.
16. Claude, a spelling check: does a trailing slash or a symlinked spelling of a config dir create a second Keychain item.
17. Codex, rate-limit attribution after a cross-account resume: do B's `token_count` figures in the rollout reflect B's quota while `creator_account_id` still names A, and how do they compare with the app-server figures (freshness, integer against float, which window is primary on each plan).
18. Codex, the deduplication identity: for two logins on one personal subscription, are `chatgpt_account_id` and `chatgpt_user_id` identical; for two seats in one workspace, is the account id equal while limits differ per seat; does `tokens.account_id` always equal the id-token claim.
19. Codex, concurrency: when the old process is still flushing at the moment B opens the same rollout, is Ctrl-C at the safe point enough or must the drain wait for exit; and do compression and background migration stay off.
20. Codex, `codex exec resume` from a different working directory: is there a directory filter or a prompt.
21. Codex, refresh-token ownership: after copying a login between profiles and refreshing in both, does the loser get `refresh_token_reused`.

## Decision line

Pending the owner's run.

Proposed from the research, to be confirmed or changed by the results above:

- Proceed with resume-based continuation. For Claude the mechanism is structurally supported: `projects/<key>/<id>.jsonl` is the only conversation store and nothing in a message row ties it to an account. Write "proceed" once results 1 and 2 pass.
- For Codex, proceed with resume-based continuation gated on results 5, 6 and 7. The model context is rebuilt from the rollout, so the risk is the on-screen history and the backend's acceptance of the encrypted reasoning. If result 5 fails with a shared SQLite home, or result 6 fails, rewrite P4 for Codex as summarise-and-restart only and keep resume for Claude.
- Whatever the decision, fold findings 1 to 4, 6 and 8 into the P1 and P2 designs before P1 starts: the vault carries `oauthAccount`, the usage collector polls no faster than every 5 minutes per Claude token and never refreshes, Codex profiles share one SQLite home and always resume by UUID, and Codex usage is attributed by launch profile.

**Background.** Issue #219, the first phase of the pools epic #218. The scope is the notes and the one-command spike; no code in `app/` changes.
