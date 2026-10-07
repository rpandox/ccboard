---
name: Task
about: A unit of work for a person or an agent
labels: ''
---

<!--
This repository is public: no emails, tokens, host names or personal paths in
anything you write here. Say "the box" and `<data dir>`.

Tiers (copied from CONTRIBUTING.md; check the vendors' current model names first):

| Tier | Claude | Codex |
|---|---|---|
| A | `claude --model opus --effort high --permission-mode acceptEdits` | `codex -m gpt-6.1-sol -c model_reasoning_effort="high" -s workspace-write -a on-request` |
| B | `claude --model sonnet --effort medium --permission-mode acceptEdits` | `codex -m gpt-6.1-sol -c model_reasoning_effort="medium" -s workspace-write -a on-request` |
| C | `claude --model haiku --effort low --permission-mode acceptEdits` | `codex -m gpt-6-luna -c model_reasoning_effort="low" -s workspace-write -a on-request` |
| check | `claude --model sonnet --effort medium --permission-mode acceptEdits` | `codex -m gpt-6.1-sol -s workspace-write -a on-request` |

Tier A uses xhigh for open designs.
-->

## Goal

<!-- One or two sentences: what changes for the user. -->

## Why / evidence

<!-- What was seen, where, and on which commit. -->

## Scope

<!-- A checklist, one box per change. -->

- [ ]

## Files

<!-- "Read first" and "Change" lists. -->

## Tests

<!-- The tests to add or change, and the two suites staying green. -->

## Acceptance

<!-- Observable results, including the two suite commands. -->

## Who should do it

<!-- Pick the tier. Keep the two line shapes exactly. -->

- Claude: `claude --model sonnet --effort medium --permission-mode acceptEdits`
- Codex: `codex -m gpt-6.1-sol -c model_reasoning_effort="medium" -s workspace-write -a on-request`
- Why this tier:
- Cheaper parts:

## Rules

- This repository is public: no emails, tokens, host names or personal paths. Say "the box" and `<data dir>`.
- Run both suites before opening the pull request. Work lands through a pull request; merging to main deploys the box.

## References

<!-- Plan sections and related issues. See CONTRIBUTING.md. -->

<!-- ccboard-uiux-v1:start -->
## UI/UX acceptance: ccboard 10x

**Applicability:** <!-- UI, API, Docs, Infra or None -->

**Current state:** <!-- Planned, Partial, Shipped or Unverified -->

**User outcome:**

**Surface and scope:**

**Dependencies:**

**Source of truth:**

- [ ] **Truth:**
- [ ] **Meaning:**
- [ ] **Safety:**
- [ ] **Wording:**
- [ ] **Evidence:** <!-- Named tests, screenshots at 1280, 1024 force-coarse and 390 force-coarse, any device or probe result, and what stays unverified. -->
<!-- ccboard-uiux-v1:end -->
