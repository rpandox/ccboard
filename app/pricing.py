"""List prices for the Claude models ccusage prices at zero, so the Usage page can show an API-equivalent ESTIMATE next to the reported cost (issue #95).

Why: a model ccusage has no rate for comes back `missingPricing: true` with cost 0 (on the box, 2026-10-08: claude-sonnet-5-5, 37 per-model rows and about
950 M tokens; earlier in the same month claude-opus-5, claude-fable-5-1 and claude-opus-5-5 were in that state too, and ccusage has priced them since).
The dollars then understate what the work is worth. Nothing here changes a reported figure: an estimate is added beside it, never into it.

Prices are USD per million tokens, from the Anthropic pricing page read on PRICE_DATE (list prices; the Batch API is 50 % off and Fast mode is dearer, neither is
modelled). Cache-read prices are on the page; CACHE-WRITE PRICES ARE NOT in the material this table was made from, so `cache_write` is None and a cache write is
priced at CACHE_WRITE_FALLBACK_X times the input price (1.25, Anthropic's multiplier for the 5-minute cache) and the estimate says so. The rates of the legacy ids (Fable 5,
Opus 5, Sonnet 5) are not on the page either: such an id takes its family's newest entry as a SIBLING and the estimate says that too.

    estimate_model(model, tokens) -> {usd, basis: 'list' | 'sibling', family, cache_write_assumed} | None      None = no defensible price: stays unpriced
    estimate_rough(models, tokens) -> {usd, basis: 'rough', ...} | None           several models and no per-model split: the mean of their rates, labelled rough

A server-side override file (CCBOARD_PRICE_TABLE, a JSON path) can add or replace entries and move the date; the browser can never change the table."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .config import settings

log = logging.getLogger("ccboard.pricing")

PRICE_DATE = "2026-10-07"
PRICE_SOURCE = "Anthropic pricing page (list prices per million tokens)"
CACHE_WRITE_FALLBACK_X = 1.25      # x the input price when a family's cache-write price is not known (assumed; says so in the estimate)
FIELDS = ("input", "output", "cache_read", "cache_write")

# family key (the model id without `claude-` and without a date suffix) -> USD per million tokens. cache_write None = not on the page.
PRICES: dict[str, dict[str, float | None]] = {
    "fable-5-1": {"input": 10.0, "output": 50.0, "cache_read": 0.25, "cache_write": None},
    "opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.20, "cache_write": None},
    "sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.20, "cache_write": None},
    "haiku-4-5": {"input": 1.0, "output": 5.0, "cache_read": 0.10, "cache_write": None},
}

_DATE_SUFFIX = re.compile(r"-(?:20\d{6}|latest)$")
_FAMILY = re.compile(r"^([a-z]+)-(\d+(?:-\d+)*)$")


def family_key(model) -> str | None:
    """`claude-sonnet-5-5-20260901` -> `sonnet-5-5`; anything that is not a Claude id (gpt-..., a path, empty) -> None."""
    m = str(model or "").strip().lower()
    if m.startswith("anthropic/"):
        m = m[len("anthropic/"):]
    if not m.startswith("claude-"):
        return None
    m = _DATE_SUFFIX.sub("", m[len("claude-"):])
    return m if _FAMILY.match(m) else None


def _version(key: str) -> tuple[int, ...]:
    m = _FAMILY.match(key)
    return tuple(int(x) for x in m.group(2).split("-")) if m else ()


class Table:
    def __init__(self, prices: dict, date: str, source: str):
        self.prices, self.date, self.source = prices, date, source

    def resolve(self, model) -> tuple[str, dict, str] | None:
        """(family key, price entry, basis) for a model id: 'list' when the table has the id itself, 'sibling' when it takes the newest entry of the same family
        (opus, sonnet, haiku, fable), None when there is nothing of that family."""
        key = family_key(model)
        if key is None:
            return None
        if key in self.prices:
            return key, self.prices[key], "list"
        fam = _FAMILY.match(key).group(1)
        sibs = [k for k in self.prices if _FAMILY.match(k) and _FAMILY.match(k).group(1) == fam]
        if not sibs:
            return None
        best = max(sibs, key=_version)
        return best, self.prices[best], "sibling"


_table_cache: dict = {"key": None, "table": None}


def _override(path: str) -> tuple[dict, str | None, str | None]:
    """(entries, date, source) from the override file; a missing or malformed file changes nothing (logged once per path+mtime)."""
    try:
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8")) if p.stat().st_size < 200_000 else None
    except (OSError, ValueError):
        log.warning("price table override %s is unreadable; using the built-in table", path)
        return {}, None, None
    if not isinstance(data, dict):
        log.warning("price table override %s is not a JSON object; ignored", path)
        return {}, None, None
    out: dict = {}
    for k, v in (data.get("models") if isinstance(data.get("models"), dict) else {}).items():
        key = family_key(f"claude-{k}") if not str(k).startswith("claude-") else family_key(k)
        if key is None or not isinstance(v, dict):
            continue
        entry = {}
        for f in FIELDS:
            x = v.get(f)
            entry[f] = float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and x >= 0 else None
        if entry["input"] is None or entry["output"] is None:
            continue
        entry["cache_read"] = entry["cache_read"] if entry["cache_read"] is not None else 0.0
        out[key] = entry
    date = data.get("date") if isinstance(data.get("date"), str) and re.match(r"^\d{4}-\d{2}-\d{2}$", data["date"]) else None
    source = data.get("source") if isinstance(data.get("source"), str) else None
    return out, date, source[:120] if source else None


def table() -> Table:
    """The built-in table with the override file (settings.price_table) applied; rebuilt when that file changes."""
    path = getattr(settings, "price_table", "") or ""
    try:
        stamp = (path, Path(path).stat().st_mtime_ns) if path else ("", 0)
    except OSError:
        stamp = (path, -1)
    if _table_cache["key"] == stamp and _table_cache["table"] is not None:
        return _table_cache["table"]
    prices = {k: dict(v) for k, v in PRICES.items()}
    date, source = PRICE_DATE, PRICE_SOURCE
    if path:
        extra, d, s = _override(path)
        prices.update(extra)
        date, source = d or date, (s or source) + (" + local override" if extra else "")
    t = Table(prices, date, source)
    _table_cache.update(key=stamp, table=t)
    return t


def _usd(entry: dict, tokens: dict) -> tuple[float, bool]:
    """(USD, cache_write_assumed) for {input, output, cache_creation, cache_read} token counts at one entry's rates."""
    cw = entry.get("cache_write")
    assumed = cw is None and tokens.get("cache_creation", 0) > 0
    if cw is None:
        cw = float(entry["input"]) * CACHE_WRITE_FALLBACK_X
    usd = (tokens.get("input", 0) * float(entry["input"]) + tokens.get("output", 0) * float(entry["output"])
           + tokens.get("cache_creation", 0) * cw + tokens.get("cache_read", 0) * float(entry.get("cache_read") or 0.0)) / 1e6
    return usd, assumed


def estimate_model(model, tokens: dict) -> dict | None:
    """The estimate for one model's {input, output, cache_creation, cache_read} tokens, or None when the model has no entry or sibling."""
    hit = table().resolve(model)
    if hit is None:
        return None
    key, entry, basis = hit
    usd, assumed = _usd(entry, tokens)
    return {"usd": usd, "basis": basis, "family": key, "cache_write_assumed": assumed}


def estimate_rough(models: list, tokens: dict) -> dict | None:
    """Several models and no per-model split of the tokens: the session's totals at the mean of the resolvable models' rates. Labelled 'rough'."""
    hits = [h for h in (table().resolve(m) for m in models) if h]
    if not hits or len(hits) != len(models):
        return None                                   # a model with no price among them: not guessed
    mean = {f: sum((h[1].get(f) if h[1].get(f) is not None else 0.0) for h in hits) / len(hits) for f in ("input", "output", "cache_read")}
    cws = [h[1].get("cache_write") for h in hits]
    mean["cache_write"] = sum(cws) / len(cws) if all(c is not None for c in cws) else None
    usd, assumed = _usd(mean, tokens)
    return {"usd": usd, "basis": "rough", "family": ",".join(sorted({h[0] for h in hits})), "cache_write_assumed": assumed}


def info() -> dict:
    """What the Usage page shows about the table: {date, source}."""
    t = table()
    return {"date": t.date, "source": t.source}
