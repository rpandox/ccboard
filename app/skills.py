"""The skills installed on the box, for the command palette's Skills group (issue #102).

A skill is a folder with a SKILL.md whose front matter names it. Two places hold them: the user's own, `<claude config dir>/skills/<name>/SKILL.md`,
and the installed plugins', `<install path>/skills/<name>/SKILL.md`, where the install paths come from `<claude config dir>/plugins/installed_plugins.json`
(a plugin cache keeps old versions too; only the installed one counts). A plugin skill is typed `/<plugin>:<skill>`.

Rules, because the files are other people's text:
  * only the front matter is read, at most FRONT_MAX bytes of each file, never the body;
  * names must match NAME_RE, at most LIST_CAP skills, descriptions cut at DESC_MAX characters and drawn as text by the page;
  * a symlink at a skill folder or its SKILL.md is not followed, and nothing outside the skills root or the plugins folder is opened (projects.contained);
  * the scan is cached for TTL seconds and never raises: a root that cannot be read is skipped.

Usage counts: hooks.apply calls bump_use() on a UserPromptSubmit that starts with `/<name>` of a known skill; the counts live in kv `skill_uses`
(a name to count map, capped at USES_CAP). They are what THIS board saw, not lifetime use, and a skill never seen used has no count (not 0).
Whether Claude hands a typed skill command to the UserPromptSubmit hook as the typed text is not verified here."""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path

from . import projects
from .config import settings

log = logging.getLogger("ccboard.skills")

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9:_-]{0,63}$")
PLUGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
FRONT_MAX = 8192                 # bytes of one SKILL.md that are ever read
DESC_MAX = 160
LIST_CAP = 300
USES_CAP = 500
INSTALLED_MAX = 1024 * 1024      # installed_plugins.json is a registry of a few KB
TTL = 60.0
KV_USES = "skill_uses"
AGENT = "claude"                 # only Claude has a skills list here; a Codex equivalent is not verified

clock = time.monotonic           # patched by tests (the cache age)
_lock = threading.Lock()
_cache: dict = {"at": None, "key": None, "items": []}
_use_lock = threading.Lock()     # kv_get + kv_set is two calls: one hook thread at a time


def reset() -> None:
    with _lock:
        _cache.update(at=None, key=None, items=[])


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        v = v[1:-1]
    return v


def parse_front(raw: bytes | str) -> dict:
    """{name?, description?} from the leading `---` block of a SKILL.md. Plain `key: value`, quoted values, and folded or literal blocks (`>`, `|`)
    are read; anything else in the block is ignored. A block that does not close inside what was read (FRONT_MAX) yields {}."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    block: list[str] = []
    for ln in lines[1:]:
        if ln.strip() == "---":
            break
        block.append(ln)
    else:
        return {}
    out: dict = {}
    i = 0
    while i < len(block):
        m = re.match(r"^(name|description):[ \t]*(.*)$", block[i])
        i += 1
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if val in ("", ">", "|", ">-", "|-", ">+", "|+"):
            parts: list[str] = []
            while i < len(block) and (block[i].startswith((" ", "\t")) or not block[i].strip()):
                parts.append(block[i].strip())
                i += 1
            val = " ".join(p for p in parts if p)
        out[key] = " ".join(_unquote(val).split())
    return out


def _read_front(path: Path) -> dict:
    with open(path, "rb") as f:
        return parse_front(f.read(FRONT_MAX))


def _installed_plugins(cfg: Path) -> list[tuple[str, Path]]:
    """[(plugin name, install path)] of the installed plugins, from installed_plugins.json; paths that escape <config>/plugins are dropped."""
    reg = cfg / "plugins" / "installed_plugins.json"
    root = cfg / "plugins"
    try:
        if not reg.is_file() or reg.is_symlink() or reg.stat().st_size > INSTALLED_MAX:
            return []
        data = json.loads(reg.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return []
    plugins = data.get("plugins") if isinstance(data, dict) else None
    out: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for key, entries in (plugins.items() if isinstance(plugins, dict) else []):
        name = str(key).split("@", 1)[0]
        if not PLUGIN_RE.match(name) or name in seen or not isinstance(entries, list):
            continue
        for e in entries:
            p = e.get("installPath") if isinstance(e, dict) else None
            if not isinstance(p, str) or not p:
                continue
            try:
                path = projects.contained(Path(p), root)
            except projects.BadRequest:
                continue
            seen.add(name)
            out.append((name, path))
            break
    return out


def _scan_root(skills_dir: Path, trust: Path, source: str, prefix: str, out: dict) -> None:
    try:
        skills_dir = projects.contained(skills_dir, trust)
        entries = sorted(os.scandir(skills_dir), key=lambda e: e.name)
    except (OSError, projects.BadRequest):
        return
    for e in entries:
        if len(out) >= LIST_CAP:
            return
        try:
            if not e.is_dir(follow_symlinks=False):
                continue
            f = projects.contained(Path(e.path) / "SKILL.md", skills_dir)
            if not f.is_file():
                continue
            fm = _read_front(f)
        except (OSError, projects.BadRequest):
            continue
        name = prefix + (fm.get("name") or e.name)
        if not NAME_RE.match(name) or name in out:
            continue
        out[name] = {"name": name, "description": (fm.get("description") or "")[:DESC_MAX], "source": source}


def scan() -> list[dict]:
    """[{name, description, source: 'user' | 'plugin:<name>'}] sorted by name; cached for TTL seconds per Claude config folder. Never raises."""
    cfg = Path(settings.claude_config_dir)
    key = str(cfg)
    with _lock:
        if _cache["at"] is not None and _cache["key"] == key and clock() - _cache["at"] < TTL:
            return list(_cache["items"])
    found: dict = {}
    try:
        _scan_root(cfg / "skills", cfg, "user", "", found)
        for plugin, path in _installed_plugins(cfg):
            _scan_root(path / "skills", cfg / "plugins", f"plugin:{plugin}", plugin + ":", found)
    except Exception as e:                                   # a surprise in someone's folder must not take the palette down
        log.warning("skills scan failed: %s", e)
    items = sorted(found.values(), key=lambda s: s["name"])[:LIST_CAP]
    with _lock:
        _cache.update(at=clock(), key=key, items=items)
    return list(items)


def uses(db) -> dict[str, int]:
    """{skill name: times this board saw it typed} from kv skill_uses; a value of the wrong shape is dropped."""
    try:
        v = (db.kv_get(KV_USES) or {}).get("value")
    except Exception:
        return {}
    if not isinstance(v, dict):
        return {}
    return {str(k): int(n) for k, n in v.items() if isinstance(n, int) and not isinstance(n, bool) and n > 0}


def listing(db) -> dict:
    """The body of GET /api/skills: {agent, skills: [{name, description, source, uses?}]} most used first, then by name. `uses` only where
    this board saw the skill used."""
    seen = uses(db)
    out = []
    for s in scan():
        n = seen.get(s["name"], 0)
        out.append({**s, **({"uses": n} if n else {})})
    out.sort(key=lambda s: (-s.get("uses", 0), s["name"]))
    return {"agent": AGENT, "skills": out}


_TYPED_RE = re.compile(r"^\s*/([a-z0-9][a-z0-9:_-]{0,63})(?:\s|$)")


def used_skill(prompt, known=None) -> str | None:
    """The skill a typed prompt starts with: `/browse ...` -> 'browse'; `/mem-search` -> 'claude-mem:mem-search' when that bare name is
    unambiguous among the plugin skills. None for anything else (a built-in command, text, a non-string)."""
    if not isinstance(prompt, str):
        return None
    m = _TYPED_RE.match(prompt[:200])
    if not m:
        return None
    names = {s["name"] for s in scan()} if known is None else set(known)
    word = m.group(1)
    if word in names:
        return word
    tails = [n for n in names if ":" in n and n.split(":", 1)[1] == word]
    return tails[0] if len(tails) == 1 else None


def bump_use(db, prompt) -> str | None:
    """Count one use when `prompt` starts with a known skill's slash command. Returns the skill name, or None when nothing was counted."""
    name = used_skill(prompt)
    if not name:
        return None
    with _use_lock:
        cur = uses(db)
        cur[name] = cur.get(name, 0) + 1
        if len(cur) > USES_CAP:
            cur = dict(sorted(cur.items(), key=lambda kv: (-kv[1], kv[0]))[:USES_CAP])
        db.kv_set(KV_USES, cur)
    return name
