# Tasks, diffs and pull requests

For people who run tasks in worktrees and merge them.

[Back to the documentation index](../README.md)

### Work from GitHub issues (v0.5.20)

In the launcher's task mode, *From a GitHub issue* lists the repo's open issues (`gh issue list`, so `gh` must be installed and logged in on the box). Picking one fills the title and the prompt (with `Closes #N`) and, when the issue has a model line, preselects the agent, model, effort and permissions. Nothing starts until you press Start.

The model line is a block in the issue body. It is read from the first `## Who should do it` heading (HTML comments are ignored) and only from lines that start `- Claude:`, `- Claude Code:` or `- Codex:`, using the first backticked command on the line:

    ## Who should do it

    - Claude: `claude --model sonnet --effort medium --permission-mode acceptEdits`
    - Codex: `codex -m gpt-6.1-sol -c model_reasoning_effort="medium" -s workspace-write -a on-request`

Only `--model`, `--effort` and `--permission-mode` (Claude) and `-m`, `-c model_reasoning_effort=...`, `-s` and `-a` (Codex) are read, as `--flag value` or `--flag=value`, quoted or not. Every other flag is ignored. A bypass (`bypassPermissions`, `--dangerously-skip-permissions`, `--dangerously-bypass-approvals-and-sandbox`, `--yolo`, `danger-full-access`) is dropped and the note says "ignored a bypass setting". What is left is checked against the installed agent (`GET /api/agents`): an unknown model, effort or mode is skipped, the note names it, and your own default stays. A quiet line under the select reads "from the issue: opus, high, acceptEdits" with *Reset to my defaults*. What an issue sets is never remembered as your default for the repo.

Issue text becomes an agent prompt, so an issue by anyone but the owner of the repo's `origin` (from the remote URL) or the GitHub login `gh` uses on the box (`gh api user`, cached for 10 minutes; for an organisation's repo this is the one that matches you) is untrusted, compared case-insensitively: its model line is not read (`GET /api/projects/<p>/repos/<r>/issues/<n>` answers an empty `who`), a callout says "Issue by <login>: its text becomes the agent's prompt", and Start waits for a tick.

A task made from an issue stores `issue_number` and `issue_url` (additive columns, also accepted by `POST /api/tasks`); its card shows `#N` linking to the issue. When the task is done with a result, *Comment on the issue* (two taps, the body shown while it is armed) runs `gh issue comment <n> --body-file -` with the first 800 characters of the result, the branch, the PR link and "Posted from ccboard": `POST /api/tasks/<id>/issue-comment`, once per task (409 when there is no issue, the task is not done, or it was posted); `GET` on the same path returns the body without posting. A failure shows `gh`'s message and the button stays. Not covered: chains created from the launcher do not carry the issue link.

## What v0.3 adds: git & tasks

- **Tasks**: "New task" on a repo takes a title and a prompt (or picks an open GitHub issue) and starts Claude in its own git worktree and branch (`claude --worktree`, `.worktreeinclude` honoured). The Tasks board has columns Backlog / In progress / Needs you / Done / PR open / Merged, derived from the hook state and the PR (see *Tasks* under [*Using it*](getting-started.md#using-it) for Now / Later / Schedule and the Backlog).
- **Diff / PR**: a modal shows the branch commits and diff (diff2html), "Describe with Claude" writes the PR title and body with a one-turn `claude -p` over the diff, "Create PR" pushes and runs `gh pr create`, "Merge (squash) & archive" pushes, verifies origin has your HEAD, merges with `gh pr merge --squash --delete-branch` and removes the worktree.
- **PR / CI status** on cards (polled with `gh` every minute) and **Fix CI**, which pastes the failing job log into the task's Claude session (relaunching it in the worktree if needed).
- **Overlap warning** when two open tasks of a repo touch the same files.

## What v0.4 adds: autonomy & fleet

- **Preview links**: "Preview" on a task card publishes the dev server the task started on its own tailnet HTTPS port (`tailscale serve`, never Funnel). The board's own services (the board, the terminal, code-server, ntfy, the claude-mem worker) are never published as a preview, and a preview never takes port 443, the board's, code-server's, ntfy's or the claude-mem viewer's HTTPS port.
