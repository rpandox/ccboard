# Contributing

This guide takes an issue from pick-up to a merged phase. It is written for people and for agents (Claude Code, Codex). The repository is public: no emails, tokens, host names or personal paths anywhere.

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
python3.12 -m venv .venv && .venv/bin/pip install --require-hashes -r requirements-dev.lock
```

See README, Development, for running the board locally and for how the lock files are regenerated. Dependabot opens one grouped pull request per ecosystem each week; they are merged together once a week, never one by one, because every merge to `main` deploys the box.

## Work in a worktree

Never work in the checkout the box serves. Make a worktree off `main` (`git worktree add --detach ../ccboard-wt-<issue> main`) and start the agent in that folder, or let Claude make it with `claude --worktree <name> ...`. For headless Codex runs use `codex exec -s workspace-write` (`codex exec` has no `-a`).

Agents never commit, push, stash or touch branches. They edit files and run the targeted tests. The owner or the orchestrator reviews the diff and commits.

## Run the suites

```sh
CCBOARD_TEST_NO_CLAUDE=1 .venv/bin/pytest -q
node --test tests/js/*.test.mjs
```

The first is what CI runs (no `claude` binary). While working, run only the targeted files. Tests use temp directories only. A canary in `tests/conftest.py` fails the run if any test changes the real home's Claude or Codex config, and every test gets a temp `HOME` (README, Development).

## Screenshots

Every UI change needs screenshots at 1280 and at 390 (with `html.force-coarse`), judged by a person; a green suite says nothing about how a screen looks. Use `scripts/qa-ui.sh` in demo mode (README, Development).

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
