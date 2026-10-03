#!/usr/bin/env python3
"""Merge (or remove) ccboard's hooks and statusLine into ~/.claude/settings.json. Idempotent.

    claude_settings.py install --app-dir /path/to/ccboard [--settings PATH]
    claude_settings.py remove [--settings PATH]
    claude_settings.py show [--settings PATH]

Only entries whose command points at ccboard's bin/ are touched; everything else is preserved. The settings file is
$CLAUDE_CONFIG_DIR/settings.json (default ~/.claude/settings.json) unless --settings names another one.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Registered with bin/ccboard-hook as ASYNC hooks (timeout 5): they never hold the TUI up.
ASYNC_EVENTS = ["SessionStart", "UserPromptSubmit", "Notification", "Stop", "StopFailure", "SubagentStart", "SubagentStop",
                "PreCompact", "PostCompact", "PostModelSwitch", "TaskCreated", "TaskCompleted", "PostToolBatch", "ConfigChange"]
# Registered with bin/ccboard-hook-fast, NOT async (timeout 3): the process is exiting, the event must land first.
FAST_EVENTS = ["SessionEnd"]
EVENTS = ASYNC_EVENTS + FAST_EVENTS
# WorktreeCreate / WorktreeRemove are deliberately not registered (they can replace Claude's own worktree handling; revisit with V9).
MARK = "ccboard-hook"            # also matches ccboard-hook-fast
FAST_MARK = "ccboard-hook-fast"
PERMISSION_MARK = "ccboard-permission"
STATUS_MARK = "ccboard-statusline"
HOOK_TIMEOUT = 5
FAST_TIMEOUT = 3


def settings_path() -> Path:
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
    return base / "settings.json"


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


def is_ours(hook: dict, mark: str = MARK) -> bool:
    if not isinstance(hook, dict):
        return False
    cmd = str(hook.get("command", ""))
    return hook.get("type") == "command" and (mark in cmd or FAST_MARK in cmd or PERMISSION_MARK in cmd)


def strip_ours(data: dict) -> dict:
    """Remove every entry whose command contains ccboard-hook (so ccboard-hook-fast too) or ccboard-permission, and ccboard's
    statusLine. Entries and event lists of a shape this file does not know are kept untouched."""
    hooks = data.get("hooks")
    if isinstance(hooks, dict):
        for ev in list(hooks):
            if not isinstance(hooks[ev], list):
                continue
            groups = []
            for g in hooks[ev]:
                if not isinstance(g, dict) or not isinstance(g.get("hooks"), list):
                    groups.append(g)
                    continue
                kept = [h for h in g["hooks"] if not is_ours(h)]
                if kept:
                    groups.append({**g, "hooks": kept})
            if groups:
                hooks[ev] = groups
            else:
                del hooks[ev]
        if hooks:
            data["hooks"] = hooks
        else:
            data.pop("hooks", None)
    sl = data.get("statusLine")
    if isinstance(sl, dict) and is_ours(sl, STATUS_MARK):
        data.pop("statusLine", None)
    return data


def install(data: dict, app_dir: Path, remote_approve: bool = True, approve_timeout: int = 90) -> dict:
    if data.get("hooks") is None:
        data.pop("hooks", None)
    elif not isinstance(data["hooks"], dict):
        raise RuntimeError("settings.json `hooks` is not an object; fix it first")
    bad = [ev for ev in [*EVENTS, "PermissionRequest"] if ev in (data.get("hooks") or {}) and not isinstance(data["hooks"][ev], list)]
    if bad:                                           # never overwrite a value this file does not understand
        raise RuntimeError(f"settings.json hooks.{bad[0]} is not a list; fix it first")
    data = strip_ours(data)
    hook_cmd = str(app_dir / "bin" / "ccboard-hook")
    fast_cmd = str(app_dir / "bin" / "ccboard-hook-fast")
    hooks = data.setdefault("hooks", {})
    for ev in EVENTS:
        if ev in FAST_EVENTS:
            entry = {"hooks": [{"type": "command", "command": fast_cmd, "timeout": FAST_TIMEOUT}]}
        else:
            entry = {"hooks": [{"type": "command", "command": hook_cmd, "async": True, "timeout": HOOK_TIMEOUT}]}
        hooks.setdefault(ev, []).append(entry)
    if remote_approve:
        # Synchronous on purpose: it waits for a remote allow/deny, up to the timeout, then yields to the TUI prompt.
        perm_cmd = str(app_dir / "bin" / "ccboard-permission")
        hooks.setdefault("PermissionRequest", []).append(
            {"hooks": [{"type": "command", "command": perm_cmd, "timeout": int(approve_timeout) + 30}]})
    sl = data.get("statusLine")
    if not sl:
        data["statusLine"] = {"type": "command", "command": str(app_dir / "bin" / "ccboard-statusline"),
                              "refreshInterval": 30}
    else:
        print(f"note: keeping your existing statusLine ({sl.get('command', sl.get('type')) if isinstance(sl, dict) else sl}); "
              f"ccboard will not get per-session context/usage numbers. To use ccboard's, remove it and rerun.",
              file=sys.stderr)
    return data


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["install", "remove", "show"])
    ap.add_argument("--app-dir", type=Path, default=Path(__file__).resolve().parent.parent)
    ap.add_argument("--settings", type=Path, default=None, help="the settings file to edit (default: $CLAUDE_CONFIG_DIR/settings.json)")
    ap.add_argument("--no-remote-approve", action="store_true", help="do not register the PermissionRequest hook")
    ap.add_argument("--approve-timeout", type=int, default=90)
    ap.add_argument("--with-worktree-hooks", action="store_true",
                    help="accepted and ignored: WorktreeCreate/WorktreeRemove stay unregistered until V9")
    a = ap.parse_args()
    if a.with_worktree_hooks:
        print("note: --with-worktree-hooks is ignored; WorktreeCreate/WorktreeRemove are not registered until V9.", file=sys.stderr)
    p = a.settings or settings_path()
    data = load(p)
    if a.action == "show":
        print(json.dumps({"hooks": data.get("hooks"), "statusLine": data.get("statusLine")}, indent=2))
        return
    try:
        new = install(data, a.app_dir.resolve(), not a.no_remote_approve, a.approve_timeout) if a.action == "install" else strip_ours(data)
    except RuntimeError as e:
        sys.exit(f"{p}: {e}")
    save(p, new)
    print(f"{a.action}: {p} ({'hooks for ' + ', '.join(EVENTS) if a.action == 'install' else 'ccboard entries removed'})")


if __name__ == "__main__":
    main()
