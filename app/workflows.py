"""Does a GitHub Actions workflow run for a push to a ccboard backup branch? (issue #51)

push_runs_on_backup_branches(text) answers True / False / None (cannot tell) from the `on:` block of a workflow file. There is no YAML library
in the image (and a check this small does not earn a new layer), so this is a tolerant scan of the one block that matters. Pure: it reads the
text it is given and nothing else. When anything is unusual (an anchor, a template expression, a block scalar, a tab for indentation, a
flow mapping it cannot take apart, an empty filter list, both `branches` and `branches-ignore`) the answer is None, never True: an exotic
workflow must not become a false warning.

GitHub's rules, as far as the scan needs them: a workflow runs for `push` unless a filter says otherwise. `branches` is an allow-list,
`branches-ignore` a deny-list, a pattern starting with `!` takes branches back out. `*` matches anything but `/`, `**` anything, `?` and `+`
repeat the character before them. Only `tags` (or `tags-ignore`) with no branch filter means branch pushes do not run it. `paths` filters
do not look at branch names, so they never exclude a backup branch.
"""
from __future__ import annotations

import re

BACKUP_BRANCH = "ccboard-backup/node/branch"      # a stand-in shaped like ccboard-backup/<node>/<branch> (two levels below the prefix, so `ccboard-backup/*` does not match it)
FILTER_KEYS = ("branches", "branches-ignore", "tags", "tags-ignore", "paths", "paths-ignore")


class _Unknown(Exception):
    """The text is not something this scan can judge."""


def _strip_comment(line: str) -> str:
    out: list[str] = []
    quote = None
    for i, ch in enumerate(line):
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        else:
            out.append(ch)
    return "".join(out).rstrip()


def _rows(text: str) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        lead = raw[:len(raw) - len(raw.lstrip())]
        if "\t" in lead:
            raise _Unknown("tab indentation")
        line = _strip_comment(raw)
        if not line.strip() or line.strip() in ("---", "..."):
            continue
        rows.append((len(line) - len(line.lstrip(" ")), line.strip()))
    return rows


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    if s[:1] in "\"'" or "${{" in s or s[:1] in "&*!|>%@`":
        raise _Unknown("quoting or template")
    return s


def _flow_items(text: str) -> list[str]:
    """'[a, "b/**"]' -> ['a', 'b/**'] (one level; nested flow collections are not understood)."""
    t = text.strip()
    if not (t.startswith("[") and t.endswith("]")):
        raise _Unknown("not a flow list")
    inner = t[1:-1]
    if "[" in inner or "{" in inner:
        raise _Unknown("nested flow")
    return [_unquote(x) for x in inner.split(",") if x.strip()]


def _balanced(text: str, start: int) -> tuple[str, int]:
    """text[start] is '{' or '[': the balanced group and the index after it."""
    open_ch = text[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    quote = None
    for i in range(start, len(text)):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start:i + 1], i + 1
    raise _Unknown("unbalanced flow")


def _flow_map(text: str) -> dict[str, str]:
    """'{push: {branches: [main]}, pull_request: null}' -> {'push': '{branches: [main]}', 'pull_request': 'null'} (values stay text)."""
    t = text.strip()
    if not (t.startswith("{") and t.endswith("}")):
        raise _Unknown("not a flow map")
    s, i, out = t[1:-1], 0, {}
    while i < len(s):
        while i < len(s) and s[i] in " ,":
            i += 1
        if i >= len(s):
            break
        m = re.match(r"""(?:"([^"]+)"|'([^']+)'|([\w-]+))\s*:\s*""", s[i:])
        if not m:
            raise _Unknown("flow key")
        key = m.group(1) or m.group(2) or m.group(3)
        i += m.end()
        if i < len(s) and s[i] in "{[":
            val, i = _balanced(s, i)
        else:
            j = s.find(",", i)
            j = len(s) if j < 0 else j
            val, i = s[i:j].strip(), j
        out[key] = val
    return out


def _scalar_list(value: str) -> list[str]:
    v = value.strip()
    if v.startswith("["):
        return _flow_items(v)
    return [_unquote(v)]


def _block_filters(rows: list[tuple[int, str]]) -> dict[str, list[str]]:
    """The rows under `push:`: filter keys with inline or block lists."""
    out: dict[str, list[str]] = {}
    if not rows:
        return out
    base = rows[0][0]
    i = 0
    while i < len(rows):
        ind, line = rows[i]
        if ind != base:
            raise _Unknown("filter layout")
        m = re.match(r"""^(?:"([^"]+)"|'([^']+)'|([\w-]+))\s*:\s*(.*)$""", line)
        if not m:
            raise _Unknown("filter line")
        key, rest = m.group(1) or m.group(2) or m.group(3), m.group(4)
        i += 1
        if rest:
            if key in FILTER_KEYS:
                out[key] = _scalar_list(rest)
            continue
        items: list[str] = []
        while i < len(rows) and (rows[i][0] > base or (rows[i][0] == base and rows[i][1].startswith("- "))):
            ln = rows[i][1]
            if ln.startswith("- "):
                items.append(_unquote(ln[2:]))
            elif rows[i][0] <= base:
                break
            else:
                raise _Unknown("filter item")
            i += 1
        if key in FILTER_KEYS:
            out[key] = items
    return out


def _flow_filters(text: str) -> dict[str, list[str]]:
    t = text.strip()
    if t in ("", "null", "~", "{}"):
        return {}
    out: dict[str, list[str]] = {}
    for key, val in _flow_map(t).items():
        if key in FILTER_KEYS:
            out[key] = _scalar_list(val)
    return out


def _events(rows: list[tuple[int, str]], inline: str) -> dict[str, dict]:
    """event name -> its filters, from what follows `on:`."""
    inline = inline.strip()
    if inline:
        if inline.startswith("["):
            return {e: {} for e in _flow_items(inline)}
        if inline.startswith("{"):
            return {e: (_flow_filters(v) if v.startswith("{") else {}) for e, v in _flow_map(inline).items()}
        return {_unquote(inline): {}}
    if not rows:
        raise _Unknown("empty on")
    if rows[0][1].startswith("- "):
        out: dict[str, dict] = {}
        for ind, ln in rows:
            if not ln.startswith("- ") or ":" in ln:
                raise _Unknown("on list")
            out[_unquote(ln[2:])] = {}
        return out
    base = rows[0][0]
    out = {}
    i = 0
    while i < len(rows):
        ind, ln = rows[i]
        m = re.match(r"""^(?:"([^"]+)"|'([^']+)'|([\w-]+))\s*:\s*(.*)$""", ln)
        if ind != base or not m:
            raise _Unknown("on layout")
        name, rest = m.group(1) or m.group(2) or m.group(3), m.group(4)
        i += 1
        sub: list[tuple[int, str]] = []
        while i < len(rows) and rows[i][0] > base:
            sub.append(rows[i])
            i += 1
        if rest:
            if sub:
                raise _Unknown("value and block")
            if not (rest in ("null", "~") or rest.startswith("{")):
                raise _Unknown("event value")                    # an alias, a tag, a block scalar, a list
            out[name] = _flow_filters(rest) if name == "push" and rest.startswith("{") else {}
        else:
            out[name] = _block_filters(sub) if name == "push" else {}      # only push's filters matter; another event's block is not read
    return out


def _glob(pattern: str) -> re.Pattern:
    out, i = [], 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if ch == "*":
            out.append("[^/]*")
        elif ch in "?+":
            if not out:
                raise _Unknown("pattern starts with a repeat")
            out.append(ch)
        elif ch == "[":
            j = pattern.find("]", i + 1)
            if j < 0:
                raise _Unknown("open bracket")
            out.append(pattern[i:j + 1])
            i = j
        else:
            out.append(re.escape(ch))
        i += 1
    return re.compile("".join(out) + r"\Z")


def _matches(patterns: list[str], branch: str) -> bool:
    """GitHub's ordered include/exclude: a pattern that matches switches the answer on, a `!` pattern that matches switches it off."""
    hit = False
    for p in patterns:
        if p.startswith("!"):
            if _glob(p[1:]).match(branch):
                hit = False
        elif _glob(p).match(branch):
            hit = True
    return hit


def _push_runs(filters: dict, branch: str) -> bool:
    inc, exc = filters.get("branches"), filters.get("branches-ignore")
    if inc is not None and exc is not None:
        raise _Unknown("branches and branches-ignore")
    if inc is None and exc is None:
        return not ("tags" in filters or "tags-ignore" in filters)       # a tags-only filter never starts on a branch push
    patterns = inc if inc is not None else exc
    if not patterns:
        raise _Unknown("empty filter")
    if inc is not None:
        return _matches(inc, branch)
    return not any(_glob(p).match(branch) for p in exc)


def push_runs_on_backup_branches(text, pattern: str = BACKUP_BRANCH) -> bool | None:
    """True: a push to `pattern` starts this workflow. False: it does not (no push trigger, or a filter excludes it). None: cannot tell.
    Never raises."""
    if not isinstance(text, str):
        return None
    try:
        rows = _rows(text)
        tops = [(i, re.match(r"""^(?:on|"on"|'on')\s*:\s*(.*)$""", ln)) for i, (ind, ln) in enumerate(rows) if ind == 0]
        tops = [(i, m) for i, m in tops if m]
        if len(tops) != 1:
            return None
        i, m = tops[0]
        j = i + 1
        block: list[tuple[int, str]] = []
        while j < len(rows) and (rows[j][0] > 0 or rows[j][1].startswith("- ")):
            block.append(rows[j])
            j += 1
        events = _events(block, m.group(1))
        if "push" not in events:
            return False
        return _push_runs(events["push"], pattern)
    except (_Unknown, re.error, ValueError, IndexError):
        return None
