#!/usr/bin/env python3
"""Merge (or remove) ccboard's hooks and statusLine into ~/.claude/settings.json. Idempotent.

    claude_settings.py install --app-dir /path/to/ccboard
    claude_settings.py remove
    claude_settings.py show

Only entries whose command points at ccboard's bin/ are touched; everything else is preserved.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

EVENTS = ["SessionStart", "UserPromptSubmit", "Notification", "Stop", "StopFailure", "SessionEnd"]
MARK = "ccboard-hook"
STATUS_MARK = "ccboard-statusline"


def settings_path() -> Path:
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
    return base / "settings.json"


def load(p: Path) -> dict:
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text() or "{}")
    except json.JSONDecodeError as e:
        sys.exit(f"{p} is not valid JSON ({e}); fix it first")


def save(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, p)


def is_ours(hook: dict, mark: str = MARK) -> bool:
    return hook.get("type") == "command" and mark in str(hook.get("command", ""))


def strip_ours(data: dict) -> dict:
    hooks = data.get("hooks") or {}
    for ev in list(hooks):
        groups = []
        for g in hooks[ev]:
            kept = [h for h in (g.get("hooks") or []) if not is_ours(h)]
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
    sl = data.get("statusLine") or {}
    if is_ours(sl, STATUS_MARK):
        data.pop("statusLine", None)
    return data


def install(data: dict, app_dir: Path) -> dict:
    data = strip_ours(data)
    hook_cmd = str(app_dir / "bin" / "ccboard-hook")
    hooks = data.setdefault("hooks", {})
    for ev in EVENTS:
        entry = {"hooks": [{"type": "command", "command": hook_cmd, "async": True, "timeout": 5}]}
        hooks.setdefault(ev, []).append(entry)
    sl = data.get("statusLine")
    if not sl:
        data["statusLine"] = {"type": "command", "command": str(app_dir / "bin" / "ccboard-statusline"),
                              "refreshInterval": 30}
    else:
        print(f"note: keeping your existing statusLine ({sl.get('command', sl.get('type'))}); "
              f"ccboard will not get per-session context/usage numbers. To use ccboard's, remove it and rerun.",
              file=sys.stderr)
    return data


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["install", "remove", "show"])
    ap.add_argument("--app-dir", type=Path, default=Path(__file__).resolve().parent.parent)
    a = ap.parse_args()
    p = settings_path()
    data = load(p)
    if a.action == "show":
        print(json.dumps({"hooks": data.get("hooks"), "statusLine": data.get("statusLine")}, indent=2))
        return
    new = install(data, a.app_dir.resolve()) if a.action == "install" else strip_ours(data)
    save(p, new)
    print(f"{a.action}: {p} ({'hooks for ' + ', '.join(EVENTS) if a.action == 'install' else 'ccboard entries removed'})")


if __name__ == "__main__":
    main()
