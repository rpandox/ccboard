"""Agent adapters: one interface per coding-agent CLI (plan v0.5.4). Claude only for now; `shell` is not an adapter.

    agents.get("claude")      -> the adapter (KeyError for an unknown name)
    agents.names()            -> ["claude"]
    agents.status_all()       -> {"claude": {installed, version, loggedIn, authMethod?, email?, glyph, hooks{installed}}}

status_all adds no cache of its own: claude_auth.status() is the 60 s cached layer (one `claude auth status` per minute, as before) and
the hooks read is a small JSON file, so nothing here can go stale or leak between tests.

Import rule: this package imports config, projects and claude_auth; scheduler.py imports it; tasks.py never does.
"""
from __future__ import annotations

from .base import Agent, Check, HookNorm, LaunchPlan, LaunchReq, OptField, SlashSpec
from .claude import ClaudeAgent

_AGENTS: dict[str, Agent] = {"claude": ClaudeAgent()}


def get(name: str) -> Agent:
    try:
        return _AGENTS[name]
    except KeyError:
        raise KeyError(f"unknown agent {name!r}") from None


def all() -> list[Agent]:                      # noqa: A001 (the plan's name; the builtin is not used in this module)
    return list(_AGENTS.values())


def names() -> list[str]:
    return list(_AGENTS)


def status_all() -> dict[str, dict]:
    return {n: a.status_summary() for n, a in _AGENTS.items()}
