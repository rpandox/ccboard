#!/usr/bin/env python3
"""Merge (or remove) ccboard's hooks into ~/.codex/hooks.json. Idempotent. The Codex twin of claude_settings.py.

    codex_hooks.py install --app-dir /path/to/ccboard [--hooks PATH] [--no-remote-approve] [--approve-timeout N] [--no-async]
    codex_hooks.py remove [--hooks PATH]
    codex_hooks.py show [--hooks PATH]
    codex_hooks.py trust-help [--hooks PATH]

Only entries whose command points at ccboard's bin/ are touched; everything else is preserved. The file is
$CODEX_HOME/hooks.json (default ~/.codex/hooks.json; an empty CODEX_HOME counts as unset, as it does for Codex itself)
unless --hooks names another one.

The file shape (checked against codex-cli 0.157.1's own parser through its app-server `hooks/list`, not only against the docs):

    {"hooks": {"<Event>": [{"matcher": "<optional>", "hooks": [{"type": "command", "command": "...", "timeout": 5}]}]}}

the same nesting as Claude's settings.json. What was observed on Codex, and why the file looks the way it does:

* The seven standard events carry `async: true` (as in claude_settings.py); Interrupt, SessionEnd and PermissionRequest do not.
  Observed on codex-cli 0.157.1 through its app-server (a shim hook per event, all marked async): the async SessionStart,
  UserPromptSubmit and PreCompact hooks ran in the background (no `hook/started` notification, so no "running hook" line in the
  TUI); a SessionEnd hook marked async is run synchronously with the warning "running async SessionEnd hook synchronously", and
  Interrupt and SessionEnd timeouts above 3 s are clamped with the warning "clamping ... hook timeout to 3s". So those two are
  written synchronous with timeout 3 (bin/ccboard-hook-fast, a 2 s request cap), and the file parses with no warnings. Not
  observed: whether codex 0.145 (the box's build) honours `async`; if its events never arrive, set CCBOARD_CODEX_HOOKS_ASYNC=0 (or
  pass --no-async) and every hook is written synchronous, at the cost of the board's 3 s request cap holding the TUI up on a slow hook.
* Codex runs a hook only after you have reviewed it once, and records that per hook under
  [hooks.state."<file>:<event in snake_case>:<group index>:<hook index>"] trusted_hash in config.toml; the hash covers the
  definition (command, timeout, async). So the command strings are byte-stable and a re-run never moves an entry: ccboard's hook is
  replaced where it already sits, and a new one is appended after whatever is there, so the group and hook indexes (part of that
  key) survive a re-install. (Verified on 0.157.1: trusted stays trusted across a re-install, a timeout change reads "modified".)
* The agent is named in the command (`env CCBOARD_AGENT=codex ...`, on the PermissionRequest hook too), not left to the session's
  environment: a Codex run started from inside a Claude session (`codex exec`) would otherwise report itself as the session's agent,
  and the board ignores a hook whose agent is not the row's (a Hermes run in a board repo's directory must not flip that row).
* The top level of hooks.json takes only `hooks` and `description`: Codex rejects the whole file on any other key.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path

# Written with `async: true` and a 5 s ceiling (see the module doc).
STANDARD_EVENTS = ["SessionStart", "UserPromptSubmit", "Stop", "SubagentStart", "SubagentStop", "PreCompact", "PostCompact"]
# Registered with bin/ccboard-hook-fast (a 2 s request cap), synchronous, timeout 3 (Codex clamps both events to 3 s): the turn is
# being cut or the process is exiting, so the event has to land first.
FAST_EVENTS = ["Interrupt", "SessionEnd"]
EVENTS = STANDARD_EVENTS + FAST_EVENTS
PERMISSION_EVENT = "PermissionRequest"          # bin/ccboard-permission, unless --no-remote-approve
# Not registered on purpose: PreToolUse / PostToolUse fire per tool call (noise, and PreToolUse can block a tool).
MARK = "ccboard-hook"                           # also matches ccboard-hook-fast
FAST_MARK = "ccboard-hook-fast"
PERMISSION_MARK = "ccboard-permission"
AGENT = "codex"
HOOK_TIMEOUT = 5
FAST_TIMEOUT = 3


def hooks_path() -> Path:
    base = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    return Path(base) / "hooks.json"


def load(p: Path) -> dict:
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text() or "{}")
    except json.JSONDecodeError as e:
        sys.exit(f"{p} is not valid JSON ({e}); fix it first")
    if not isinstance(data, dict):
        sys.exit(f"{p} is not a JSON object; fix it first")
    return data


def save(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, p)


def is_ours(hook) -> bool:
    if not isinstance(hook, dict):
        return False
    cmd = str(hook.get("command", ""))
    return hook.get("type") == "command" and (MARK in cmd or FAST_MARK in cmd or PERMISSION_MARK in cmd)


def _drop_ours(groups: list) -> list:
    """The groups of one event without ccboard's hooks; a group left empty goes too. Groups of a shape this file does not know stay."""
    out = []
    for g in groups:
        if not isinstance(g, dict) or not isinstance(g.get("hooks"), list):
            out.append(g)
            continue
        kept = [h for h in g["hooks"] if not is_ours(h)]
        if kept:
            out.append({**g, "hooks": kept})
    return out


def strip_ours(data: dict) -> dict:
    """Remove every entry whose command contains ccboard-hook (so ccboard-hook-fast too) or ccboard-permission. Events whose value
    is not a list, and groups of a shape this file does not know, are kept untouched."""
    hooks = data.get("hooks")
    if isinstance(hooks, dict):
        for ev in list(hooks):
            if not isinstance(hooks[ev], list):
                continue
            groups = _drop_ours(hooks[ev])
            if groups:
                hooks[ev] = groups
            else:
                del hooks[ev]
        if not hooks:
            data.pop("hooks", None)
    return data


def _upsert(groups: list, hook: dict) -> list:
    """Put `hook` where ccboard's hook already is (its group and index inside the group are part of Codex's trust key, so they must not
    move), drop any further ccboard hook of this event, or append a group of its own at the end when there was none."""
    out, placed = [], False
    for g in groups:
        if not isinstance(g, dict) or not isinstance(g.get("hooks"), list):
            out.append(g)
            continue
        hooks = []
        for h in g["hooks"]:
            if not is_ours(h):
                hooks.append(h)
            elif not placed:
                hooks.append(dict(hook))
                placed = True
        if hooks:
            out.append({**g, "hooks": hooks})
    if not placed:
        out.append({"hooks": [dict(hook)]})
    return out


def _command(app_dir: Path, script: str) -> str:
    """`env CCBOARD_AGENT=codex <app>/bin/<script>`: every hook names its agent itself (ccboard-hook, -fast and -permission send it as
    X-CCBoard-Agent). Byte-stable: Codex's trust hash covers this string."""
    return f"env CCBOARD_AGENT={AGENT} {shlex.quote(str(app_dir / 'bin' / script))}"


def async_default() -> bool:
    return os.environ.get("CCBOARD_CODEX_HOOKS_ASYNC", "1") != "0"


def wanted_hooks(app_dir: Path, remote_approve: bool = True, approve_timeout: int = 90, async_hooks: bool = True) -> dict[str, dict]:
    """event -> the one hook definition ccboard registers for it, in registration order."""
    hook_cmd = _command(app_dir, "ccboard-hook")
    fast_cmd = _command(app_dir, "ccboard-hook-fast")
    out: dict[str, dict] = {}
    for ev in EVENTS:
        if ev in FAST_EVENTS:
            out[ev] = {"type": "command", "command": fast_cmd, "timeout": FAST_TIMEOUT}
        elif async_hooks:
            out[ev] = {"type": "command", "command": hook_cmd, "async": True, "timeout": HOOK_TIMEOUT}
        else:
            out[ev] = {"type": "command", "command": hook_cmd, "timeout": HOOK_TIMEOUT}
    if remote_approve:
        # Synchronous on purpose: it waits for a remote allow/deny, up to the timeout, then yields to the TUI prompt.
        out[PERMISSION_EVENT] = {"type": "command", "command": _command(app_dir, "ccboard-permission"),
                                 "timeout": int(approve_timeout) + 30}
    return out


def install(data: dict, app_dir: Path, remote_approve: bool = True, approve_timeout: int = 90, async_hooks: bool = True) -> dict:
    if data.get("hooks") is None:
        data.pop("hooks", None)
    elif not isinstance(data["hooks"], dict):
        raise RuntimeError("hooks.json `hooks` is not an object; fix it first")
    wanted = wanted_hooks(app_dir, remote_approve, approve_timeout, async_hooks)
    bad = [ev for ev in [*EVENTS, PERMISSION_EVENT] if ev in (data.get("hooks") or {}) and not isinstance(data["hooks"][ev], list)]
    if bad:                                           # never overwrite a value this file does not understand
        raise RuntimeError(f"hooks.json hooks.{bad[0]} is not a list; fix it first")
    hooks = data.setdefault("hooks", {})
    for ev in list(hooks):                            # an event ccboard no longer registers (remote approve off): take its hook out
        if ev not in wanted and isinstance(hooks[ev], list):
            groups = _drop_ours(hooks[ev])
            if groups:
                hooks[ev] = groups
            else:
                del hooks[ev]
    for ev, hook in wanted.items():
        hooks[ev] = _upsert(hooks.get(ev, []), hook)
    return data


def trust_help(path: Path, data: dict) -> str:
    present = [ev for ev, groups in (data.get("hooks") or {}).items() if isinstance(groups, list)
               and any(is_ours(h) for g in groups if isinstance(g, dict) for h in (g.get("hooks") or []))]
    state = (f"{len(present)} ccboard hooks are in {path} ({', '.join(present)})." if present
             else f"{path} has no ccboard hooks yet: run  python3 {Path(__file__).resolve()} install  first.")
    return f"""{state}

Codex runs a hook only after you have reviewed it once. Until then every Codex session ccboard starts shows "no hooks (untrusted?)"
and the board gets no events from it (it still works as a terminal).

  1. On the box, in any terminal:  codex
  2. Newer builds may open the review themselves ("N hooks need review before they can run"); otherwise type  /hooks
  3. Trust every ccboard hook (each one's command ends in ccboard-hook, ccboard-hook-fast or ccboard-permission).
  4. Start a Codex session from the board and send a prompt: its state should now follow the turn.

Codex records the review per hook, as  [hooks.state."{path}:<event>:<group>:<index>"] trusted_hash = "sha256:..."  in config.toml
(<event> in snake_case, e.g. session_start). The hash covers the definition (command, timeout, async) and the key covers the file and the
hook's position, so you review again only when ccboard's checkout or data directory moves (the command path changes), when ccboard
changes a hook definition (the status reads "modified"), or when somebody puts a hook group above ccboard's in the same event.
Re-running install never moves ccboard's entries.

Opt out of the review:  CCBOARD_CODEX_HOOK_TRUST=bypass  makes the board start Codex with --dangerously-bypass-hook-trust. That also
runs every .codex/hooks.json inside a repository you cloned without asking, the same risk class as bypassPermissions. The default
is  review.
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["install", "remove", "show", "trust-help"])
    ap.add_argument("--app-dir", type=Path, default=Path(__file__).resolve().parent.parent)
    ap.add_argument("--hooks", type=Path, default=None, help="the hooks file to edit (default: $CODEX_HOME/hooks.json)")
    ap.add_argument("--no-remote-approve", action="store_true", help="do not register the PermissionRequest hook")
    ap.add_argument("--approve-timeout", type=int, default=90)
    ap.add_argument("--no-async", action="store_true",
                    help="write every hook synchronous (also CCBOARD_CODEX_HOOKS_ASYNC=0): for a codex build that skips async hooks")
    a = ap.parse_args()
    p = a.hooks or hooks_path()
    data = load(p)
    if a.action == "show":
        print(json.dumps({"hooks": data.get("hooks")}, indent=2))
        return
    if a.action == "trust-help":
        print(trust_help(p, data), end="")
        return
    try:
        new = (install(data, a.app_dir.resolve(), not a.no_remote_approve, a.approve_timeout, not a.no_async and async_default())
               if a.action == "install" else strip_ours(data))
    except RuntimeError as e:
        sys.exit(f"{p}: {e}")
    save(p, new)
    if a.action == "install":
        events = [*EVENTS, *([] if a.no_remote_approve else [PERMISSION_EVENT])]
        print(f"install: {p} (hooks for {', '.join(events)})")
        print("note: Codex runs them only after you trust them once (/hooks in any Codex terminal); "
              f"python3 {Path(__file__).resolve()} trust-help  says how.")
    else:
        print(f"remove: {p} (ccboard entries removed)")


if __name__ == "__main__":
    main()
