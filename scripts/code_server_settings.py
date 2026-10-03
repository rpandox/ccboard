#!/usr/bin/env python3
"""Merge ccboard's code-server user settings into ~/.local/share/code-server/User/settings.json (or show / remove them).

code-server on a small box spends most of its time watching, searching and type-acquiring over node_modules, virtualenvs,
build output and linked worktrees, and asking about workspace trust, telemetry and updates. These keys make a window open fast
and stay cheap. Only keys the person has NOT set are written; the keys ccboard wrote are remembered under "ccboard.managed"
so `remove` takes back exactly those. Map-valued keys (watcherExclude, search.exclude) are merged entry by entry. A file that
is not strict JSON (VS Code allows comments) is left alone with a note: nothing of the person's is ever rewritten.

    python3 scripts/code_server_settings.py install|show|remove [--settings PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

MANAGED_FLAG = "ccboard.managed"
EXCLUDE_DIRS = ["**/.git/objects/**", "**/.git/subtree-cache/**", "**/node_modules/**", "**/.venv/**", "**/venv/**",
                "**/dist/**", "**/build/**", "**/.next/**", "**/.nuxt/**", "**/.cache/**", "**/__pycache__/**",
                "**/.pytest_cache/**", "**/.mypy_cache/**", "**/.turbo/**", "**/coverage/**", "**/.claude/worktrees/**",
                "**/.ccboard/**"]
SETTINGS = {
    "files.watcherExclude": {p: True for p in EXCLUDE_DIRS},
    "search.exclude": {p: True for p in EXCLUDE_DIRS if ".git/" not in p},
    "search.followSymlinks": False,
    "telemetry.telemetryLevel": "off",
    "update.mode": "none",
    "extensions.autoCheckUpdates": False,
    "extensions.autoUpdate": False,
    "workbench.enableExperiments": False,
    "workbench.startupEditor": "none",
    "workbench.tips.enabled": False,
    "security.workspace.trust.enabled": False,
    "git.autofetch": False,
    "git.autorefresh": True,
    "typescript.disableAutomaticTypeAcquisition": True,
    "typescript.tsserver.maxTsServerMemory": 2048,
    "npm.autoDetect": "off",
    "task.autoDetect": "off",
    "editor.minimap.enabled": False,
    "editor.renderWhitespace": "selection",
    "files.hotExit": "onExitAndWindowClose",
    "terminal.integrated.enablePersistentSessions": False,
    "terminal.integrated.gpuAcceleration": "off",
}


def default_path() -> Path:
    return Path(os.environ.get("CODE_SERVER_SETTINGS") or (Path.home() / ".local" / "share" / "code-server" / "User" / "settings.json"))


def load(path: Path) -> dict | None:
    """The settings as a dict; {} when the file is missing; None when it exists but is not strict JSON (never rewritten)."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def install(data: dict) -> tuple[dict, list[str]]:
    """Add every managed key the person has not set; merge map entries; return (new data, keys written now)."""
    managed = list(data.get(MANAGED_FLAG) or [])
    written: list[str] = []
    for key, want in SETTINGS.items():
        cur = data.get(key)
        if isinstance(want, dict):
            if cur is None:
                data[key] = dict(want)
                written.append(key)
            elif isinstance(cur, dict):
                added = {k: v for k, v in want.items() if k not in cur}
                if added:
                    cur.update(added)
                    written.append(key)
        elif key not in data:
            data[key] = want
            written.append(key)
    for k in written:
        if k not in managed:
            managed.append(k)
    if managed:
        data[MANAGED_FLAG] = managed
    return data, written


def remove(data: dict) -> tuple[dict, list[str]]:
    """Take back exactly the keys ccboard wrote (map keys: only the entries ccboard knows)."""
    managed = list(data.get(MANAGED_FLAG) or [])
    removed: list[str] = []
    for key in managed:
        want = SETTINGS.get(key)
        cur = data.get(key)
        if isinstance(want, dict) and isinstance(cur, dict):
            for k in want:
                cur.pop(k, None)
            if not cur:
                data.pop(key, None)
            removed.append(key)
        elif key in data:
            data.pop(key, None)
            removed.append(key)
    data.pop(MANAGED_FLAG, None)
    return data, removed


def write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".ccboard-tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("action", choices=["install", "show", "remove"])
    ap.add_argument("--settings", type=Path, default=None, help="settings.json path (default: code-server's User settings)")
    a = ap.parse_args(argv)
    path = a.settings or default_path()
    data = load(path)
    if data is None:
        print(f"note: {path} is not strict JSON (comments?); left untouched. Add the keys from scripts/code_server_settings.py by hand.")
        return 0
    if a.action == "show":
        print(json.dumps({k: data.get(k) for k in SETTINGS if k in data} | {MANAGED_FLAG: data.get(MANAGED_FLAG)}, indent=2))
        return 0
    if a.action == "install":
        data, written = install(data)
        if written:
            write(path, data)
        print(f"code-server settings: {'wrote ' + ', '.join(written) if written else 'nothing to add'} ({path})")
        return 0
    data, removed = remove(data)
    write(path, data)
    print(f"code-server settings: removed {', '.join(removed) if removed else 'nothing'} ({path})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
