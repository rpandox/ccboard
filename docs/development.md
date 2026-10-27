# Developing ccboard

For people changing ccboard, and for anyone who wants to try the demo mode.

[Back to the documentation index](README.md)

In this page, "the box" means the machine that runs the board.

## Development

```sh
python3.14 -m venv .venv && .venv/bin/pip install --require-hashes -r requirements-dev.lock   # the CI set: exact versions with hashes
.venv/bin/pytest -q
TMUX_TMPDIR=/tmp tmux -L ccboard -f tmux.conf start-server
T=$(mktemp -d)   # a dev board must not reach your real home: all four directories on temp paths, stand-in agents first on PATH
PATH=$PWD/scripts/dev/fake_agents:$PATH PROJECTS_DIR=$T/projects CCBOARD_DATA_DIR=$T/data CLAUDE_CONFIG_DIR=$T/claude CODEX_HOME=$T/codex \
  CCBOARD_DEV_BYPASS_USER=dev .venv/bin/uvicorn app.main:app --port 8000
```
Under the dev bypass the routes that start a real agent (new session, external open, task dispatch and reopen, fix-ci, run resume, job run, Claude and Codex logins, headless job runs) answer 409 "dev mode: set the four directories to temp paths" unless `PROJECTS_DIR`, `CCBOARD_DATA_DIR`, `CLAUDE_CONFIG_DIR` and `CODEX_HOME` are absolute and resolve outside your home (`settings.dev_sandboxed()`; `/api/state` carries `dev.sandboxed`, only under the bypass). `scripts/dev/fake_agents/claude` and `codex` are harmless stand-ins (a banner, `--version`, `--help`, `login status`, exit on `/exit` or `/quit`); `scripts/qa-ui.sh` and `scripts/qa_terminal.sh` refuse a board whose `dev.sandboxed` is not true (`QA_REAL_TTYD=1`, which drives the box itself, is not checked). Production, with no bypass, is unchanged.
The suite cannot reach your real home. Every test runs with `HOME` and `USERPROFILE` pointed at a fresh temp directory, so a stray `Path.home()` or `~` lands in the sandbox (a test that must read the real one is marked `@pytest.mark.real_home("reason")`). A session-wide canary in `tests/conftest.py` also records the existence, size and mtime of the real `~/.claude/settings.json`, `settings.local.json`, `.credentials.json` and `~/.codex/config.toml`, `hooks.json`, `auth.json` (stat only, never read) and fails the run, naming the file relative to home, if any changed; `~/.claude.json` and `~/.claude-mem` are compared by existence only because a running Claude Code session rewrites them. `tests/test_home_guard.py` proves the canary with a fake home and requires every direct home access in `app/`, `scripts/` and `bin/` to be in `app/config.py` or on its allowlist with a reason. Demo mode (`?demo=1`) renders from `app/static/demo/*.json`; `tests/test_api_v054.py` fails when `demo/state.json` lacks a key of the real `/api/state`.

**Markers and the suite on a Mac (issue #123).** `pytest.ini` registers `linux_only`, `needs_tmux`, `needs_systemd`, `needs_proc`, `posix_sh` and `needs_macos` and runs with `--strict-markers`, so a marker that is not listed is an error. `tests/conftest.py` skips a marked test on a host that lacks what the marker names (the platform flags of `app/platform.py`, `shutil.which`, `/proc`) and says why in the report. `posix_sh` marks the tests that run the shell scripts under `sh`; the other markers are for tests that need a real tmux, systemd or `/proc`. CI runs the suite on macOS as well (`test-macos`) and checks the Windows PowerShell scripts (`check-windows`). Both are required: the owner made them required on 2026-10-19, the image job waits for them, and a red one stops the image. They have read-only permissions and no secrets, and are skipped for changes to `*.md` and `docs/` only (a skip does not block the image; a failure or a cancel does). A separate nightly workflow, `ci-intel.yml`, runs the macOS steps on an Intel runner; it is advisory and never builds an image. To run the suite on a Mac yourself, never against your real home or tmux socket: `export HOME=$(mktemp -d) TMUX_TMPDIR=$(mktemp -d) CCBOARD_TMUX_SOCKET=ccboard-test CCBOARD_TEST_NO_CLAUDE=1`, then `python -m pytest -q` (about 15 minutes; the socket name must contain `ccboard`). `brew install tmux shellcheck` first, so those tests run instead of skipping.

**CI runner minutes (issue #123).** Measured on 2026-10-19 (UTC) from the GitHub API over the last 20 completed CI runs on `main` (2026-10-17 to 2026-10-19, all green). Minutes are the job's own run time, not the queue wait; a skipped job is left out.

| Job | Runner | Runs counted | Median (min) | Max (min) |
| --- | --- | --- | --- | --- |
| `test` | Ubuntu 24.04 | 20 | 18.4 | 21.9 |
| `test-macos` | `macos-latest` (arm64) | 19 | 24.0 | 26.4 |
| `check-windows` | `windows-latest` | 19 | 0.8 | 1.0 |
| `image` | Ubuntu 24.04 | 20 | 1.2 | 1.6 |

One push to `main` therefore uses about 45 runner minutes, 24 of them on macOS. On private repositories GitHub bills macOS and Windows minutes at higher rates than Linux minutes (see GitHub's Actions billing page for the current rates); this repository is public, where standard hosted runners are free, so nothing is charged. A fork that makes the repository private, or a private copy, should check those rates against the macOS row and may want `test-macos` to run on pull requests that touch code only (it already skips documentation-only changes). The nightly Intel leg adds one macOS run a day and is free for the same reason.

**Developer note: the exec form.** Claude Code can run a hook as `args` (an argument list, no shell), which would remove the [quoting problem](reference/hooks.md#what-the-hook-scripts-need) and the dependency on `sh`. It needs a minimum Claude Code version (to verify) and a real executable, and native Windows is undecided, so the hooks stay shell commands for now.

**Accessibility check (issue #45).** `scripts/dev/axe-run.sh <url> [WxH]` opens a page of a running board (demo mode, for example `http://127.0.0.1:8777/?demo=1#/agents`) with the gstack browser, injects the vendored axe-core (`scripts/dev/axe/`, MPL-2.0, pinned in `VERSION`, never under `app/static/`, so the service worker does not precache it and the CSP stays strict), runs `axe.run()` and prints one line per violation (impact, rule, selector). Exit 0 means no serious or critical violation, 1 at least one, 2 nothing was measured (no browser or axe did not run). `AXE_COARSE=1` adds the `force-coarse` class first; use `390x844` for the phone size. `tests/test_a11y_static.py` fails on an icon-only button without an `aria-label` and on an `outline: none` without a visible replacement; `tests/js/a11y.test.mjs` pins the tab, menu and glyph roles and the arrow-key behaviour.

**Reproducible builds.** The Python dependencies are locked: `requirements.in` holds the ranges, `requirements.lock` the exact versions with `--hash` entries for the image and the board, and `requirements-dev.lock` the same set plus `pytest` and `httpx` for CI. They are generated for Python 3.14, for every platform (`--universal`, so amd64 and arm64 wheels are covered), with [uv](https://github.com/astral-sh/uv) 0.12.5:

```sh
uv pip compile --generate-hashes --universal --python-version 3.14 -o requirements.lock requirements.in
uv pip compile --generate-hashes --universal --python-version 3.14 -c requirements.lock -o requirements-dev.lock requirements-dev.in requirements.in
```

The Dockerfile and CI install with `pip install --require-hashes -r requirements.lock` (CI: `requirements-dev.lock`), and `tests/test_requirements_lock.py` fails when the lock stops satisfying `requirements.in`. Change a range in `requirements.in`, regenerate, and keep `requirements.txt` (the unlocked file `install.sh` uses for the systemd venv) identical to `requirements.in`; a test checks that too. Every action in `.github/workflows/ci.yml` is pinned to a full commit sha with its version in a trailing comment, and `ccusage` in the Dockerfile to an exact version. CI also runs `pip-audit` on the runtime lock as a non-blocking step (the run recorded when the lock was made: no known vulnerabilities). Dependabot (`.github/dependabot.yml`) opens one grouped weekly pull request each for pip, GitHub Actions and Docker. **Merge those pull requests together once a week, never one by one:** every merge to `main` rebuilds the image and swaps the box's container (about 70 s of board outage). After merging the pip one, regenerate the lock files with the commands above.

How to take an issue from pick-up to a merged phase (worktrees, model lines, the two suites, one pull request per phase) is in [CONTRIBUTING.md](../CONTRIBUTING.md).

`CCBOARD_DEV_BYPASS_USER` skips the identity check for local development only; it is honoured only when the board runs directly on a host, and ignored under systemd and in the container (`CCBOARD_RUNTIME`, `INVOCATION_ID`).

The frontend is plain scripts loaded by `boot.js` and the lazy manifest (`core.js` first, `main.js` last; only the shell and the pages it reads from are in that list: the Usage, Quad, Memory, Settings, project and wizard pages, the launcher, the palette, the drag and drop module, the file tree and the terminal kit are lazy bundles listed in `app/static/lazy.js`, which the router and the entry points that open from anywhere load on first use, once, with the page's own stylesheet awaited before it mounts, and the service worker precaches all of them; `term.html` loads `core.js`, `components.js`, `termkit.js`, `term.js`, in that order, and `termkit.js` is definition-only like `core.js` and `components.js`): no bundler. `GET /sw.js` is rendered from `app/static/sw.js` with the cache name and the precache list generated from every file under `app/static`, and `/api/state.version` is a hash of the same files, so open pages reload after a deploy. Open the board with `?demo=1` to see every page rendered from fixtures (`app/static/demo/`, no tmux needed; the project page of `phasezero` has a tree, files and a binary and a secret-looking file to preview), and run `scripts/qa-ui.sh <base-url> <out-dir>` to drive the gstack browser (or headless Chrome) over the routes (Home, the inbox, Agents, Tasks, the project page and its Files and Tasks tabs, Settings, Search and the session peek) at phone, tablet and desktop widths, plus an installed-desktop pass (1440x900 with `html.pwa`: topbar at least 48 px, no horizontal overflow), two iPad passes (1024x1366 expanded, 834x1194 medium) and, when a Lighthouse is available, the Lighthouse performance, pwa and accessibility scores of Home, Agents, Usage, a project page, Settings and the terminal page, each with the desktop preset and the mobile default (`QA_MANIFEST_SHOTS=1` re-captures the manifest screenshots). **Lighthouse (issue #103).** Install one outside the repo (`npm i --prefix /tmp/lh lighthouse@<version>`) and point the script at it: `LIGHTHOUSE_BIN=/tmp/lh/node_modules/.bin/lighthouse QA_LIGHTHOUSE_STRICT=1 bash scripts/qa-ui.sh <base-url> <out-dir>`. Each page and preset runs `QA_LIGHTHOUSE_RUNS` times (3 in strict mode, else 1), the median and the worst are printed and written to `<out-dir>/lighthouse-summary.tsv`, and the median is held to the targets (performance 90, pwa 95 where this Lighthouse still has the category, accessibility 95 on desktop; performance 90 on mobile). Strict mode fails on a median under target, on a run that writes no report and on a Lighthouse that is not there (it says it measured nothing; without it the step is advisory and skips quietly). **Scores (2026-10-10, Lighthouse 12.6.0, demo mode on a sandboxed dev board on an Intel Mac, median/worst of 3 runs).** This Lighthouse has no pwa category, so `tests/test_shell.py` holds the manifest and installability assertions. Desktop performance: Home 99/98, Agents 99/99, Usage 98/97, project 97/97, Settings 98/98, terminal 97/97. Mobile performance: Home 78/78, Agents 84/81, Usage 76/75, project 73/73, Settings 80/79, terminal 86/85. Accessibility: 100 on every page and preset. Mobile is under its target of 90 and is a stated exception: first paint is 1.5 to 2.1 s, but the largest paint waits for the shell's scripts to run under the mobile profile's 4x CPU slowdown (render delay is about 89 % of a 4.1 s LCP); these historical numbers predate the v0.5.119 first-paint split described below; the repository still serves plain scripts without a build step. The live-board (tailnet) numbers are not measured. `tests/test_qa_lighthouse.py` pins the switch with a stand-in Lighthouse. `tests/test_static.py` enforces the CSP rules (no inline styles or scripts, no external URLs, text through `textContent`); `node` is needed for the JS checks (`tests/js`).

**Driving the terminal page without ttyd.** While `CCBOARD_DEV_BYPASS_USER` is set the board also serves `scripts/dev/fake_tty/` at `/tty/`: a page that stubs what ttyd's page gives the terminal page (`window.term` with `fit()` and `options.fontSize`, `.xterm > .xterm-screen`, `.xterm-helper-textarea`) and counts the wheel events it receives (`window.__wheel`, `window.__lastDelta`) and the `fit()` calls (`window.__fits`). It carries no CSP (it stands in for ttyd's page, which is not ours) and, without the dev bypass, `/tty/` is a 404 on the board: on the box tailscale serve sends `/tty` to ttyd. `scripts/qa_terminal.sh <base-url> <out-dir>` drives the gstack browser over `/term/<session>` at 390x844, 768x1024 and 1280x800 against such a board and prints a PASS/FAIL table (screenshots in the out dir). Per size it checks that `#ttywrap` does not scroll and the iframe fills it, that the page does not bounce (`overscroll-behavior: none`, `body.term` fixed), that no key-bar key is under 44 px (every visible button at 390 px under `html.force-coarse`), that the send box is on screen, that a synthetic swipe on the fake terminal produces wheel events (and one from the left edge none), that PgUp on the scroll rail and on the key bar each post `{dir: "up"}` to `/api/sessions/<tmux>/scroll`, that the History chip follows tmux's copy-mode, that the compact key bar shows five keys and More, and that the console has no errors. The page needs a session to watch: start one on the board's tmux socket (named like `qa--terminal--s1`, or set `QA_TMUX`), or let the script make and remove one with `QA_TMUX_CREATE=1` (run it with the board's `TMUX_TMPDIR` and `CCBOARD_TMUX_SOCKET`; an isolated socket is the safest, see the commands below; the script stops the tmux server it started). Without a session the page shows *ended*, the History check is skipped and the expected `/api/sessions` 404 lines are ignored in the console check. `QA_SERVER_LOG=<file>` makes the scroll check also look for the POST in the board's log, and `QA_WIDTHS="390 1280"` limits the sizes.

```sh
# terminal 1: a board on its own tmux socket, dev bypass on (so /tty/ is the fake)
mkdir -p /tmp/ccb-qa/projects /tmp/ccb-qa/data
TMUX_TMPDIR=/tmp CCBOARD_TMUX_SOCKET=ccboard-qa PROJECTS_DIR=/tmp/ccb-qa/projects CCBOARD_DATA_DIR=/tmp/ccb-qa/data \
  CCBOARD_DEV_BYPASS_USER=dev .venv/bin/uvicorn app.main:app --port 8777 > /tmp/ccb-qa/server.log 2>&1
# terminal 2: the run (390x844, 768x1024, 1280x800); exit 0 = every assertion held, 2 = nothing to drive
TMUX_TMPDIR=/tmp CCBOARD_TMUX_SOCKET=ccboard-qa QA_TMUX_CREATE=1 QA_SERVER_LOG=/tmp/ccb-qa/server.log \
  scripts/qa_terminal.sh http://127.0.0.1:8777 /tmp/ccb-qa/shots
```

Later simplification (not done): on Linux the board could use `sudo tailscale set --operator=<you>` once, as container mode already does, and drop the `tailscale serve *` line from `/etc/sudoers.d/ccboard`. `app/tailscale.py` keeps the `sudo -n` retry for now because it is what the box runs today; the trade-off is that with an operator every process of yours can change `tailscale serve` (see [Security model](security.md#security-model)).

### Demo mode

`?demo=1` on any board URL (or `localStorage.setItem('ccboard:demo', '1')`) turns on demo mode: the page reads `app/static/demo/*.json` instead of the API, so every screen draws with realistic data and no tmux, agent or box. It is the board's screenshot and QA harness: `scripts/qa-ui.sh` opens every route with `?demo=1` at the phone, tablet and desktop widths, and the node tests load the same fixtures. v0.5.21 decided to keep it; every fixture is read by a route in `demoApi()` (`core.js`), so none was trimmed.

- **What is faked.** Every GET maps to one fixture (state, agents, doctor, search, tree, file, series, series events, usage summary, issues, the Memory routes, and the task diff). Timestamps are shifted from the fixture's `demo.epoch` so ages and countdowns stay live. Every write waits 150 ms and answers `{"ok": true}`; a few (new project, new repo, refresh usage) also update the in-page state so the next draw shows them. `?mem=down|degraded|stale|partial|ambiguous|incompatible|untested` lays a degraded Memory state over the fixtures. A GET that has no fixture answers `{}`.
- **Refreshing a fixture.** Edit the JSON by hand, keeping the shape of the real answer: `tests/test_launcher_api.py` holds `agents.json` to `GET /api/agents`, and `tests/test_static.py` fails when `state.json` lacks a key the real `/api/state` has. Fixtures carry made-up names only (`box`, `/home/demo/...`, `demo@example.com`); never paste real host names, paths or emails.
- **The epic view.** `demo/epic.json` answers `GET /api/epics` and `GET /api/epics/<owner>/<repo>/<n>` through `demoEpics()` in `pages/epic.js` (the one line in `demoApi()` hands them over), plays `PUT .../settings` (pins and Pause) in the page, and `?epic=nologin|noaccess|notfound|wait|stale|labels|empty|norepo|none|down|writesoff|offline|notasks` lays a degraded state over it, and the claim (`POST /api/nodes/<handle>/epics/claim`) is played there too, with `?claim=taken|lost|unknown|writesoff|closed|done|attempts|offline|scope|unconfirmed` for each refusal.
- **The device check.** `demo/nodes-auth.json` answers `GET /api/nodes/auth` through `demoAuth()` in `nodes-pair.js`: one state per `&authstate=` (token, whois, whois-cap, tagged, bind, old, noserve, down, nocap, badmode, unknown), and Check on the token state plays the host reading. `tests/test_nodes_whois.py` holds each state to the board's own answer and banner words.
- **Served as static files.** The fixtures stay out of the asset version and the service-worker shell, and are served no-cache.


## First paint

The static skeleton uses only `skeleton.css` and deferred `boot.js`. Two animation
frames precede loading the app; `lazy.js` waits for shared styles before running
`main.js`, and the router waits for each view's styles before mounting it. Page
styles carry ranks so visiting routes in a different order preserves the cascade.
The usage card loads as it approaches the viewport; shortcuts load on first use,
including the first key of a chord. No build, bundler or minifier is involved.

Regenerate the checked-in Blueprint subset with
`python3 scripts/dev/blueprint_subset.py` (or verify it with `--check`). The script
uses only Python's standard library. It scans app-owned JS, CSS and HTML (including
lazy pages) for literal `bp5-*` / `bp6-*` classes. Its documented keep list preserves
runtime state modifiers and whole icon, intent and elevation families, including
concatenated names. Attribute selectors, negated classes, font faces and keyframes
are retained conservatively. `--check` rejects undocumented computed prefixes and
stale output. Add new dynamic families to the keep list before regenerating.
The generated file revalidates (`no-cache`) despite its vendor directory; only the
unchanged vendor originals receive immutable caching. The full vendor stylesheet
stays unchanged for the separate terminal document and future regeneration.
Shared controls belong in shared CSS: a page that reuses a pressed toggle must not
rely on visiting another page to load its styles.

`tests/test_static_perf.py` runs `tests/js/first-view.mjs`, a fake browser loader
that executes index/boot/main, the real `core.api()` demo path, and the Home mount.
It records every requested script and stylesheet, including the demo prerequisites,
widgets and features enabled in the default demo state (Nodes, Team and bare
sessions). Raw JS + CSS is **1,075,413 bytes**, with a **1,129,184-byte budget**
(review baseline plus 5%; the parallel loader now measures **1,076,529 bytes**
within that unchanged budget). This exceeds 700,000 bytes; the former manifest-only
budget omitted runtime requests. HTML, JSON data, fonts and images are outside
this JS + CSS budget. The small skeleton budget and subset reproducibility checks
remain separate.

The server writes the existing asset hash into an HTML data attribute and the
external boot script. Boot, lazy bundles and `core.loadAsset()` request release
URLs (`?v=<hash>`). The worker installs every file before activating; it never
updates the installed release cache with responses from a newer deployment.
Older caches remain available to tabs still running older boot scripts. A request
for a release the server no longer holds fails instead of returning newer bytes.
Offline failures show Retry. No inline script or CSP relaxation is needed.

Bundles wait for prerequisites and check a declared export from every module.
Independent prerequisites load together. Each bundle inserts all its scripts with
`async=false` to download in parallel and execute in order; dependent scripts are
preloaded at the same release URL but execute only after prerequisites succeed.
Stylesheets retain their cascade ranks and gate mount. Failure waits for every
in-flight member before exposing Retry, so attempts cannot overlap.
Failed bundles and their dependents can retry; successful modules are reused only
while their exports exist. Every lazy action stub shares visible error/Retry
handling. Keymap installs idempotently on load, and feature bindings use
`KeyBindings.bind()` so registration and disposal work before or after it arrives.
`tests/js/lazy.test.mjs` and `sw-release.test.mjs` cover these cold-load and update
cases, including the previous-worker/new-boot/offline scenario.
