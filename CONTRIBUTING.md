# Contributing

Thank you for helping. The first half of this file is the short path for a person: fork, run the tests, try the board, open a pull request. The second half, from "For agents and the maintainer's workflow", is how the maintainer runs Claude Code and Codex on issues; you can skip it. The repository is public: no emails, tokens, host names or personal paths anywhere.

## The short path

1. **Fork and clone.** Fork <https://github.com/rpandox/ccboard> on GitHub, then `git clone https://github.com/<you>/ccboard`, `cd ccboard` and `git switch -c my-change`.
2. **Python environment.** CI uses Python 3.14: `python3.14 -m venv .venv && .venv/bin/pip install --require-hashes -r requirements-dev.lock`. The lock file holds the exact versions with hashes; [docs/development.md](docs/development.md) says how it is regenerated.
3. **Run the tests.** The command CI uses is `CCBOARD_TEST_NO_CLAUDE=1 .venv/bin/pytest -q`. While you work, run only the files for what you touched, for example `.venv/bin/pytest -q tests/test_docs.py`. The tests use temp directories and cannot reach your real home, and they never run the real `claude` or `codex`. A test that needs a real tmux, systemd or a Mac is skipped on a machine that has none.
4. **Run the JavaScript tests.** `node --test tests/js/*.test.mjs` (CI uses Node 22). There is nothing to install: the frontend is plain scripts with no build step.
5. **Try the board without Tailscale.** The demo mode draws every screen from fixtures, with no tmux, no agent and no login. Start a board on temporary folders with the stand-in agents first on `PATH`:

   ```sh
   T=$(mktemp -d)
   PATH=$PWD/scripts/dev/fake_agents:$PATH PROJECTS_DIR=$T/projects CCBOARD_DATA_DIR=$T/data CLAUDE_CONFIG_DIR=$T/claude CODEX_HOME=$T/codex \
     CCBOARD_DEV_BYPASS_USER=dev .venv/bin/uvicorn app.main:app --port 8000
   ```

   Then open `http://127.0.0.1:8000/?demo=1`. `CCBOARD_DEV_BYPASS_USER` skips the identity check and is for development only: it is ignored under systemd and in the container. [docs/development.md](docs/development.md#demo-mode) lists what the demo fakes.
6. **Change one thing, and add its test.** A change that alters behavior comes with a test that fails without it. A change to the interface needs screenshots at 1280 and at 390 pixels wide ([Screenshots](#screenshots)). A new setting touches four places ([A new setting](#a-new-setting)).
7. **Add a CHANGELOG entry.** At the top of `CHANGELOG.md`, newest first: a heading `## vX.Y.Z - YYYY-MM-DD` with the next version number after the newest entry, up to five bullets under `### Added`, `### Changed` or `### Fixed`, then one last line `Upgrade: nothing to do.` The line says `Upgrade: rerun ./install.sh.` instead only when your change touched `install.sh`, a systemd unit, `tmux.conf` or the Claude settings. `tests/test_changelog.py` checks the shape.
8. **Open a pull request.** Push your branch to your fork and open a pull request against `main` of the original repository. The template asks for what changed, the issue it closes and the two test commands. Pull request CI runs the full suite and a docker build that does not push. Keep one change to a pull request.

## For agents and the maintainer's workflow

The rest of this file takes an issue from pick-up to a merged phase. It is written for agents (Claude Code, Codex) and for the maintainer who runs them.

## Pick an issue

Issues carry labels for the phase, the kind (`kind:*`), the size (`size:*`) and the model line (`model:*`). New issues start from the template in `.github/ISSUE_TEMPLATE/task.md`; pull requests from `.github/pull_request_template.md`.

## Read the tier block

Every issue has a "Who should do it" block with a Claude line and a Codex line for its tier. Use the line the issue names.

| Tier | Claude | Codex |
|---|---|---|
| A | `claude --model opus --effort high --permission-mode acceptEdits` | `codex -m gpt-6.1-sol -c model_reasoning_effort="high" -s workspace-write -a on-request` |
| B | `claude --model sonnet --effort medium --permission-mode acceptEdits` | `codex -m gpt-6.1-sol -c model_reasoning_effort="medium" -s workspace-write -a on-request` |
| C | `claude --model haiku --effort low --permission-mode acceptEdits` | `codex -m gpt-6-luna -c model_reasoning_effort="low" -s workspace-write -a on-request` |
| check | `claude --model sonnet --effort medium --permission-mode acceptEdits` | `codex -m gpt-6.1-sol -s workspace-write -a on-request` |

Tier A uses `--effort xhigh` (Claude) or `model_reasoning_effort="xhigh"` (Codex) for open designs. Model names change: before using a line, check `claude --help`, `codex debug models` and the vendors' model pages (https://code.claude.com/docs/en/model-config and https://learn.chatgpt.com/docs/models). The lines here were written on 2026-10-07.

## Set up

```sh
python3.14 -m venv .venv && .venv/bin/pip install --require-hashes -r requirements-dev.lock
```

See [docs/development.md](docs/development.md) for running the board locally and for how the lock files are regenerated. Dependabot opens one grouped pull request per ecosystem each week; they are merged together once a week, never one by one, because every merge to `main` deploys the box.

## Work in a worktree

Never work in the checkout the box serves. Make a worktree off `main` (`git worktree add --detach ../ccboard-wt-<issue> main`) and start the agent in that folder, or let Claude make it with `claude --worktree <name> ...`. For headless Codex runs use `codex exec -s workspace-write` (`codex exec` has no `-a`).

Agents never commit, push, stash or touch branches. They edit files and run the targeted tests. The owner or the orchestrator reviews the diff and commits.

## Run the suites

```sh
CCBOARD_TEST_NO_CLAUDE=1 .venv/bin/pytest -q
node --test tests/js/*.test.mjs
```

The first is what CI runs (no `claude` binary). While working, run only the targeted files. Tests use temp directories only. A canary in `tests/conftest.py` fails the run if any test changes the real home's Claude or Codex config, and every test gets a temp `HOME` ([docs/development.md](docs/development.md)). If the board installed on the same machine syncs its hooks during a run, the canary only warns (the installers touch `.ccboard-hook-sync` beside the file they write, #235); a hook file changed with no fresh stamp still fails.

## A new setting

A new environment variable touches four places: the reader (`app/config.py` or the script that reads it), the `ENV_KEYS` line and a bound default in `install.sh`, a row in the settings table of [docs/configuration.md](docs/configuration.md), and, when the installer should not remember it, one line with a reason in `NOT_REMEMBERED` or `INTERNAL` in `tests/test_env_keys.py`. That test fails with the setting's name when one of them is missing.

## Screenshots

Every UI change needs screenshots at 1280 and at 390 (with `html.force-coarse`), judged by a person; a green suite says nothing about how a screen looks. Use `scripts/qa-ui.sh` in demo mode ([docs/development.md](docs/development.md)).

## Commits and pull requests

Work lands through a pull request, never by a direct push to main.

- One branch and one pull request per phase. The owner or orchestrator commits once per phase, with a subject that starts `vX.Y.Z ...` (CI tags the image from it), and keeps the agent's `Co-Authored-By` trailer on that commit.
- Pull request CI runs the full test suite and a docker build that does not push.
- The phase's commit also adds its entry to `CHANGELOG.md`: a `## vX.Y.Z - YYYY-MM-DD` heading, newest first, up to five bullets under Added, Changed or Fixed, and one `Upgrade:` line that says `rerun ./install.sh` only when the phase changed `install.sh`, a systemd unit, `tmux.conf` or the Claude settings. `tests/test_changelog.py` checks the shape. The agent drafts the entry; the owner or orchestrator includes it in that one commit.
- Only a merge to main builds and publishes the image, which Watchtower then deploys to the box. A deploy is a short board outage, so merge one phase at a time. The deploy gate holds while a terminal is open.

## Not now

An idea that is on the list of things not to build (ROADMAP, "Not now") needs new evidence before an issue is filed for it. Each entry there names the number that rules it out and the signal that would reopen it.

## Never

- Break the CSP rules: no inline styles, no `cssText`, no `innerHTML`.
- Touch the real `~/.claude`, `~/.claude.json` or `~/.codex` from tests, or run the real `claude` or `codex` binaries from tests.
- Touch port 443, run `tailscale serve reset`, or restart `ccboard-tmux.service`.
- Use `bypassPermissions`, `--dangerously-skip-permissions` or `danger-full-access` as a default.
- Parse, log or return credential files: they are opaque bytes.
- Put emails, tokens, host names or personal paths in code, fixtures, docs, commits or comments.
