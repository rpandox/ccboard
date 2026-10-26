"""Profile-owned quota readings. No provider HTTP client and no credential refresh.

Observed timestamps survive polling: re-reading an old file never makes it fresh.
All public views are constructed from an allow list, never from profile JSON.
"""
from __future__ import annotations

import hashlib
import json
import logging
import signal
from collections import OrderedDict
from itertools import islice
import os
import re
import selectors
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone

from . import accounts, profiles
from .config import settings

SOURCES = frozenset(('cache', 'statusline', 'cli', 'rollout', 'app-server', 'estimate'))
PLANS = frozenset(('free', 'pro', 'plus', 'max', 'team', 'business', 'enterprise', 'edu'))
# Deliberately coarse token budgets, not provider promises. Estimates always remain unknown.
PLAN_TOKENS = {'free': 10000, 'plus': 100000, 'pro': 100000, 'max': 1000000,
               'team': 100000, 'business': 100000, 'enterprise': 100000, 'edu': 100000}
log = logging.getLogger(__name__)
MAX_ATTEMPTS = 128
APP_SERVER_TIMEOUT = 15
_PATHS = OrderedDict()
_ROLLOUT_INDEX = OrderedDict()
_TRANSCRIPTS = OrderedDict()

FIELDS = ('window_5h_pct', 'window_5h_reset_at', 'weekly_pct', 'weekly_reset_at')


def iso(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


def epoch(value):
    at = accounts._num(value)
    if at is None and isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            at = (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
        except (ValueError, OverflowError, OSError):
            return None
    return at if at is not None and 0 < at < 253402300800 else None


def number(value):
    return accounts._num(value)


def identity(provider, path):
    """Read once; retain only the hash of P0's non-JWT identity fields."""
    leaf = '.claude.json' if provider == 'claude' else 'auth.json'
    if (path / leaf).stat().st_size > accounts.MAX_FILE_BYTES:
        raise profiles.BadRequest('identity file too large')
    data = json.loads(profiles._plain(path / leaf) or b'{}')
    if provider == 'claude':
        oa = data.get('oauthAccount') or {}
        parts = [oa.get('organizationUuid'), oa.get('accountUuid')]
    else:
        parts = [(data.get('tokens') or {}).get('account_id')]
    if not all(isinstance(p, str) and 0 < len(p) <= 256 for p in parts):
        return None, None
    return hashlib.sha256(json.dumps(parts, separators=(',', ':')).encode()).hexdigest()[:32], None


def ensure_groups(db, now=None, due=None):
    if not settings.pools:
        return []
    now = time.time() if now is None else now
    if not hasattr(db, '_pool_identity_errors'):
        db._pool_identity_errors = {}
        db._pool_identity_logs = {}
    with db.lock:
        rows = [dict(r) for r in db.conn.execute("SELECT * FROM pool_account WHERE state='ready' ORDER BY id")]
    for row in rows:
        if due is not None and row['quota_group_id'] is not None and now < due.get(row['quota_group_id'], 0):
            continue
        try:
            with profiles.account_lock(row['id']):
                with db.lock:
                    ready = db.conn.execute("SELECT 1 FROM pool_account WHERE id=? AND state='ready'", (row['id'],)).fetchone()
                if not ready:
                    continue
                path = profiles.profile_dir(row['provider'], row['id'])
                key, plan = identity(row['provider'], path)
            db._pool_identity_errors.pop(row['id'], None)
        except (profiles.BadRequest, OSError, ValueError, TypeError, AttributeError):
            key, plan = None, None
            db._pool_identity_errors[row['id']] = 'identity file unavailable'
            if now >= db._pool_identity_logs.get(row['id'], 0):
                log.warning('Pool identity unavailable for account %s', row['id'])
                db._pool_identity_logs[row['id']] = now + 3600
        if plan is None:
            kv = 'accounts' if row['provider'] == 'claude' else 'codex_accounts'
            records = (db.kv_get(kv) or {}).get('value') or {}
            for rec in records.values() if isinstance(records, dict) else []:
                if isinstance(rec, dict) and rec.get('pool_account') == row['id']:
                    label = rec.get('plan')
                    label = label.lower() if isinstance(label, str) else None
                    if label in PLANS:
                        plan = label
        if key is None:
            # Unknown identity is private to this profile. Never dedup by organization alone.
            with db.lock:
                previous = db.conn.execute('SELECT subscription_key FROM quota_group WHERE id=?', (row['quota_group_id'],)).fetchone()
            key = previous[0] if previous else hashlib.sha256(uuid.uuid4().bytes).hexdigest()[:32]
        with db.lock:
            db.conn.execute('INSERT OR IGNORE INTO quota_group(provider,subscription_key,plan) VALUES (?,?,?)', (row['provider'], key, plan))
            group = db.conn.execute('SELECT id FROM quota_group WHERE provider=? AND subscription_key=?', (row['provider'], key)).fetchone()[0]
            db.conn.execute('UPDATE pool_account SET quota_group_id=? WHERE id=?', (group, row['id']))
            if plan:
                db.conn.execute('UPDATE quota_group SET plan=? WHERE id=?', (plan, group))
        row['quota_group_id'] = group
    with db.lock:
        db.conn.execute('DELETE FROM quota_group WHERE id NOT IN (SELECT quota_group_id FROM pool_account WHERE quota_group_id IS NOT NULL)')
        rows = [dict(r) for r in db.conn.execute("SELECT * FROM pool_account WHERE state='ready' ORDER BY id")]
    if due is not None:
        existing = {r['quota_group_id'] for r in rows}
        for group in list(due):
            if group not in existing:
                due.pop(group, None)
    present = {r['id'] for r in rows}
    for mapping in (db._pool_identity_errors, db._pool_identity_logs):
        for aid in list(mapping):
            if aid not in present:
                mapping.pop(aid, None)
    return rows


def windows(raw, now=None):
    now = time.time() if now is None else now
    out = {}
    for name, pct, reset in (('five_hour', FIELDS[0], FIELDS[1]), ('seven_day', FIELDS[2], FIELDS[3])):
        w = raw.get(name) if isinstance(raw, dict) else None
        if not isinstance(w, dict):
            continue
        n = number(w.get('used_percentage'))
        if n is not None:
            at = epoch(w.get('resets_at'))
            if at is not None and at <= now:
                continue
            out[pct] = max(0.0, min(100.0, n))
            out[reset] = iso(at) if at else None
    return out or None


def codex_windows(raw):
    from .agents.codex_rollout import window_kind
    out = {}
    if not isinstance(raw, dict):
        return None
    for name in ('primary', 'secondary'):
        w = raw.get(name)
        if not isinstance(w, dict):
            continue
        kind = window_kind(w.get('window_minutes', w.get('windowDurationMins')))
        n = number(w.get('used_percent', w.get('usedPercent')))
        if kind not in ('5h', '7d') or n is None:
            continue
        pct, reset = FIELDS[:2] if kind == '5h' else FIELDS[2:]
        out[pct] = max(0.0, min(100.0, n))
        at = epoch(w.get('resets_at', w.get('resetsAt')))
        out[reset] = iso(at) if at else None
    return out or None


def save(db, group, reading, source, confidence, at, node_id=None):
    if not settings.pools or source not in SOURCES or not reading:
        return False
    stamp = iso(at)
    if node_id is None:
        from . import nodes
        node_id = nodes._read_id_file(nodes.id_path()) or None
    with db.lock:
        if not db.conn.execute("SELECT 1 FROM pool_account WHERE quota_group_id=? AND state='ready' LIMIT 1", (group,)).fetchone():
            return False
        last = db.conn.execute('SELECT sampled_at FROM usage_sample WHERE quota_group_id=? ORDER BY sampled_at DESC,id DESC LIMIT 1', (group,)).fetchone()
        if last and last[0] >= stamp:
            if last[0] == stamp:
                db.conn.execute('UPDATE usage_sample SET confidence=MIN(confidence,?) WHERE quota_group_id=? AND sampled_at=? AND source=?', (confidence, group, stamp, source))
            return False
        db.conn.execute('INSERT INTO usage_sample(quota_group_id,sampled_at,window_5h_pct,window_5h_reset_at,weekly_pct,weekly_reset_at,source,confidence,node_id) VALUES (?,?,?,?,?,?,?,?,?)',
                        (group, stamp, *(reading.get(k) for k in FIELDS), source, confidence, node_id))
    return True


def groups_view(db, now=None):
    if not settings.pools:
        return []
    now = time.time() if now is None else now
    with db.lock:
        groups = [dict(r) for r in db.conn.execute('SELECT id,provider,plan FROM quota_group WHERE id IN (SELECT quota_group_id FROM pool_account) ORDER BY provider,id')]
        for g in groups:
            members = list(db.conn.execute('SELECT id,label,state FROM pool_account WHERE quota_group_id=? ORDER BY id', (g['id'],)))
            g['accounts'] = [r[0] for r in members]
            g['account_labels'] = {r[0]: r[1] for r in members if r[1] and r[1] != r[0]}
            row = db.conn.execute('SELECT * FROM usage_sample WHERE quota_group_id=? ORDER BY sampled_at DESC,id DESC LIMIT 1', (g['id'],)).fetchone()
            g.update({k: row[k] if row else None for k in (*FIELDS, 'sampled_at', 'confidence', 'source')})
            g['age_s'] = max(0, int(now - epoch(row['sampled_at']))) if row else None
            g['state'] = 'ok' if row and g['age_s'] <= 300 and row['confidence'] >= .5 else 'unknown'
            for pct, reset in (FIELDS[:2], FIELDS[2:]):
                if epoch(g[reset]) is not None and epoch(g[reset]) <= now:
                    g[pct] = None
            errors = getattr(db, '_pool_identity_errors', {})
            g['reason'] = next((errors[a] for a in g['accounts'] if a in errors), None)
            if g['reason'] or not any(r[2] == 'ready' for r in members) or all(g[k] is None for k in (FIELDS[0], FIELDS[2])):
                g['state'] = 'unknown'
            g['nodes'] = [r[0] for r in db.conn.execute('SELECT DISTINCT node_id FROM usage_sample WHERE quota_group_id=? AND node_id IS NOT NULL ORDER BY node_id', (g['id'],))]
    return groups


def account_headroom(db, now=None):
    out = []
    for g in groups_view(db, now):
        with db.lock:
            count = db.conn.execute('SELECT count(*) FROM runtime_attempt WHERE ended_at IS NULL AND account_id IN (SELECT id FROM pool_account WHERE quota_group_id=?)', (g['id'],)).fetchone()[0]
        for aid in g['accounts']:
            out.append({'account_id': aid, 'group_id': g['id'], **{k: g[k] for k in (*FIELDS, 'sampled_at', 'age_s', 'confidence', 'state')}, 'open_attempts': count})
    return out


def bound_attempt(db, name):
    if not settings.pools:
        return None
    with db.lock:
        row = db.conn.execute("""SELECT a.* FROM runtime_attempt a JOIN kv k ON json_extract(k.value,'$.attempt')=a.id
            JOIN sessions s ON k.key='runtime_session:' || s.id
            WHERE s.tmux_name=? AND a.ended_at IS NULL ORDER BY a.id DESC LIMIT 1""", (name,)).fetchone()
    return dict(row) if row else None


def observe_statusline(db, name, raw, now=None):
    attempt = bound_attempt(db, name)
    if not attempt:
        return False
    now = time.time() if now is None else now
    reading = windows(raw, now)
    if reading:
        row = db.open_row(name) or {}
        activity = epoch(row.get('state_at'))
        at = min(activity, now) if activity else now
        with db.lock:
            if db.conn.execute('SELECT 1 FROM runtime_attempt WHERE id=? AND ended_at IS NULL', (attempt['id'],)).fetchone():
                db.conn.execute('INSERT OR REPLACE INTO kv(key,value,at) VALUES (?,?,?)',
                                ('pool_statusline:' + str(attempt['id']), json.dumps({'reading': reading, 'at': at, 'confidence': .95 if activity else .8}), iso(at)))
    return True


def cache_reading(account, now):
    cache = accounts.read_usage_cache(profiles.profile_dir(account['provider'], account['id']), profile_only=True)
    if not cache or not 0 <= now - cache['at'] <= 600:
        return None
    return windows(cache['windows'], now), cache['at'], 1.0 - .5 * max(0, now - cache['at'] - 120) / 480


def rollout_paths(root, now):
    """One bounded directory index per shared tree, never a recursive search per attempt."""
    root = root.resolve()
    key = str(root)
    cached = _ROLLOUT_INDEX.get(key)
    if cached and now < cached[0]:
        return cached[1]
    paths, pending, remaining = {}, [root], 8192
    while pending and remaining > 0:
        folder = pending.pop()
        try:
            with os.scandir(folder) as entries:
                entries = list(islice(entries, remaining))
            remaining -= max(1, len(entries))
            children = []
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    children.append(type(root)(entry.path))
                elif entry.name.startswith('rollout-'):
                    match = re.search(r'([a-fA-F0-9-]{36})\.jsonl$', entry.name)
                    if match:
                        paths[match[1].lower()] = type(root)(entry.path)
            pending.extend(sorted(children))  # pop newest date directory first
        except OSError:
            continue
    _ROLLOUT_INDEX[key] = (now + 300, paths)
    _ROLLOUT_INDEX.move_to_end(key)
    while len(_ROLLOUT_INDEX) > 8:
        _ROLLOUT_INDEX.popitem(last=False)
    return paths


def attempt_path(attempt, provider, now):
    """Resolve once per attempt; bounded cache, missing files retried at idle cadence."""
    sid = attempt.get('provider_session_id')
    if not isinstance(sid, str) or not re.fullmatch(r'[a-fA-F0-9-]{36}', sid):
        return None
    root = profiles.profile_dir(provider, attempt['account_id']) / ('sessions' if provider == 'codex' else 'projects')
    key = (str(root), attempt['id'], sid)
    cached = _PATHS.get(key)
    if cached and (cached[0] is not None or now < cached[1]):
        return cached[0]
    path = rollout_paths(root, now).get(sid.lower()) if provider == 'codex' else next(root.glob(f'*/{sid}.jsonl'), None)
    _PATHS[key] = (path, now + 300)
    _PATHS.move_to_end(key)
    while len(_PATHS) > 256:
        _PATHS.popitem(last=False)
    return path


def rollout_reading(attempt, now):
    from .agents import codex_rollout as cr
    if attempt['ended_at'] and (epoch(attempt['ended_at']) or 0) < now - 300:
        return None
    path = attempt_path(attempt, 'codex', now)
    if path is None:
        return None
    parsed = cr.parse_tail(cr.read_tail(path))
    at = epoch(parsed.get('rate_limits_at'))
    if at is not None and at >= epoch(attempt['started_at']) and (not attempt['ended_at'] or at <= epoch(attempt['ended_at'])) and 0 <= now - at <= 300:
        reading = codex_windows(parsed['rate_limits'])
        if reading:
            return reading, at
    return None


def transcript_tokens(attempt, now, period=18000):
    """Incremental, bounded transcript parsing; retain only seven days of numeric usage."""
    start = max(now - period, epoch(attempt['started_at']) or now)
    end = epoch(attempt['ended_at']) or now
    if end < start:
        return None
    path = attempt_path(attempt, 'claude', now)
    if path is None:
        return None
    try:
        stat = path.stat()
        key = str(path)
        state = _TRANSCRIPTS.get(key)
        if not state or state['inode'] != stat.st_ino or stat.st_size < state['offset']:
            state = {'inode': stat.st_ino, 'offset': 0, 'messages': {}}
        messages = state['messages']
        if stat.st_size > state['offset']:
            with path.open('rb') as f:
                f.seek(state['offset'])
                data = f.read(1024 * 1024)
                # Leave a partial JSON line for the next poll.
                cut = data.rfind(b'\n') + 1
                if state['offset'] + len(data) == stat.st_size:
                    cut = len(data)
                for line in data[:cut].splitlines():
                    try:
                        row = json.loads(line)
                        at = epoch(row.get('timestamp'))
                        msg = row.get('message') or {}
                        usage, ident = msg.get('usage'), msg.get('id')
                        if row.get('type') != 'assistant' or not at or at < now - 7 * 86400 or not isinstance(usage, dict) or not isinstance(ident, str):
                            continue
                        values = [number(usage.get(k)) for k in ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')]
                        if any(v is not None for v in values):
                            messages[ident] = (at, sum(max(0, v) for v in values if v is not None))
                    except (ValueError, TypeError, AttributeError):
                        continue
                state['offset'] += cut or len(data)  # discard an oversized malformed line
        state['messages'] = {k: v for k, v in list(messages.items())[-10000:] if v[0] >= now - 7 * 86400}
        _TRANSCRIPTS[key] = state
        _TRANSCRIPTS.move_to_end(key)
        while len(_TRANSCRIPTS) > 128:
            _TRANSCRIPTS.popitem(last=False)
        values = [count for at, count in state['messages'].values() if start <= at <= end]
        return sum(values) if values else None
    except OSError:
        return None


def app_server(account):
    """A bounded CLI JSON-RPC exchange. The child owns authentication; output is never logged."""
    exe = shutil.which('codex')
    if not exe:
        return None
    from scripts.profile_env import scrub
    _, selected = profiles.launch_env('codex', account['id'], prepare=False)
    env = scrub(os.environ)
    env.update(selected)
    proc = subprocess.Popen([exe, 'app-server'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, env=env, start_new_session=True)
    sel = selectors.DefaultSelector()
    try:
        sel.register(proc.stdout, selectors.EVENT_READ)
        def send(obj):
            proc.stdin.write((json.dumps(obj) + '\n').encode())
            proc.stdin.flush()
        send({'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'ccboard', 'version': '1'}}})
        end, buf, total = time.monotonic() + APP_SERVER_TIMEOUT, b'', 0
        initialized = False
        while time.monotonic() < end:
            if not sel.select(max(0, end - time.monotonic())):
                break
            chunk = os.read(proc.stdout.fileno(), 65536)
            total += len(chunk)
            if not chunk or total > 1024 * 1024:
                break
            buf += chunk
            while b'\n' in buf:
                line, buf = buf.split(b'\n', 1)
                msg = json.loads(line)
                if not isinstance(msg, dict) or 'error' in msg:
                    return None
                if msg.get('id') == 1 and not initialized:
                    initialized = True
                    send({'method': 'initialized'})
                    send({'id': 2, 'method': 'account/rateLimits/read', 'params': {}})
                elif msg.get('id') == 2:
                    result = msg.get('result') or {}
                    return codex_windows(result.get('rateLimits'))
        return None
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
        return None
    finally:
        sel.close()
        # The CLI may leave descendants behind even after its leader exits.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        proc.stdin.close()
        proc.stdout.close()


class Collector:
    def __init__(self, db):
        self.db = db
        self.lock = threading.RLock()
        self.due = {}
        self.retention_at = 0
        self.tick_lock = threading.Lock()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread = None

    def attempts(self, aid, now=None, period=7 * 86400):
        now = time.time() if now is None else now
        with self.db.lock:
            return [dict(r) for r in self.db.conn.execute('''SELECT * FROM runtime_attempt
                WHERE account_id=? AND (ended_at IS NULL OR ended_at>=?)
                ORDER BY ended_at IS NOT NULL, started_at DESC LIMIT ?''', (aid, iso(now - period), MAX_ATTEMPTS))]

    def failure(self, account, now):
        key = 'pool_refresh:' + account['id']
        old = (self.db.kv_get(key) or {}).get('value') or {}
        misses = min(4, old.get('misses', 0) + 1)
        backoff = min(600, 60 * 2 ** misses)
        self.db.kv_set(key, {'misses': misses, 'backoff_s': backoff, 'next_at': now + max(300, backoff)})
        with self.db.lock:
            self.db.conn.execute('UPDATE usage_sample SET confidence=ROUND(MAX(0,confidence-.1),2) WHERE id=(SELECT id FROM usage_sample WHERE quota_group_id=? ORDER BY sampled_at DESC,id DESC LIMIT 1)', (account['quota_group_id'],))

    def active_due(self, aid, now):
        rec = (self.db.kv_get('pool_refresh:' + aid) or {}).get('value') or {}
        return now >= rec.get('next_at', 0)

    def sample(self, account, now, active=True, attempts=None):
        attempts = self.attempts(account['id'], now) if attempts is None else attempts
        candidates = []
        if account['provider'] == 'claude':
            cache = cache_reading(account, now)
            if cache and cache[0]:
                candidates.append((*cache[:2], 'cache', cache[2]))
            for a in attempts:
                hit = (self.db.kv_get('pool_statusline:' + str(a['id'])) or {}).get('value') or {}
                if hit and 0 <= now - hit['at'] <= 300:
                    confidence = hit.get('confidence', .8) * max(0, 1 - (now - hit['at']) / 600)
                    candidates.append((hit['reading'], hit['at'], 'statusline', confidence))
        else:
            for a in attempts:
                hit = rollout_reading(a, now)
                if hit:
                    candidates.append((*hit, 'rollout', .8 * max(0, 1 - (now - hit[1]) / 600)))
        if candidates:
            return best(candidates)
        return self.active_sample(account, now) if active else None

    def active_sample(self, account, now):
        if not settings.pools or self.stop.is_set() or not self.active_due(account['id'], now):
            return None
        # Release and launch take this same reservation. Recheck state under it.
        with profiles.account_lock(account['id']):
            if not settings.pools or self.stop.is_set() or not self.active_due(account['id'], now):
                return None
            with self.db.lock:
                row = self.db.conn.execute('SELECT state FROM pool_account WHERE id=?', (account['id'],)).fetchone()
                opened = self.db.conn.execute('SELECT 1 FROM runtime_attempt WHERE account_id=? AND ended_at IS NULL LIMIT 1', (account['id'],)).fetchone()
                ever = self.db.conn.execute('SELECT 1 FROM runtime_attempt WHERE account_id=? LIMIT 1', (account['id'],)).fetchone()
            if not row or row[0] != 'ready':
                return None
            if account['provider'] == 'claude':
                from . import usage_refresh
                usage_refresh.pool_refresh(self, account, now)
                return None
            if opened or not ever or not shutil.which('codex'):
                return None
            old = (self.db.kv_get('pool_refresh:' + account['id']) or {}).get('value') or {}
            self.db.kv_set('pool_refresh:' + account['id'], {**old, 'next_at': now + 300})
            try:
                reading = app_server(account)
            except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
                reading = None
            if reading:
                if any((reading.get(k) or 0) >= 100 for k in (FIELDS[0], FIELDS[2])):
                    self.failure(account, now)
                else:
                    self.db.kv_set('pool_refresh:' + account['id'], {'next_at': now + 300, 'misses': 0})
                return reading, now, 'app-server', .85
            self.failure(account, now)
        return None

    def estimate(self, account, attempts, now):
        with self.db.lock:
            plan = self.db.conn.execute('SELECT plan FROM quota_group WHERE id=?', (account['quota_group_id'],)).fetchone()[0]
        budget = PLAN_TOKENS.get(plan)
        if not budget:
            return None
        reading = {}
        # Weekly capacity is a coarse seven-block budget, always confidence 0.4.
        for field, period, capacity in ((FIELDS[0], 18000, budget), (FIELDS[2], 7 * 86400, budget * 7)):
            total, found = 0, False
            for a in attempts:
                if a['ended_at'] and (epoch(a['ended_at']) or 0) < now - period:
                    continue
                if account['provider'] == 'claude':
                    measured = transcript_tokens(a, now, period)
                    if measured is not None:
                        total += measured
                        found = True
                    continue
                with self.db.lock:
                    rows = list(self.db.conn.execute("""SELECT s.tmux_name FROM sessions s JOIN kv k ON k.key='runtime_session:' || s.id
                        WHERE json_extract(k.value,'$.attempt')=?""", (a['id'],)))
                for row in rows:
                    points = self.db.samples_query('stok', [row[0]], max(iso(now - period), a['started_at']), a['ended_at'])
                    values = [number(p[2]) for p in points if number(p[2]) is not None]
                    if len(values) >= 2:
                        total += max(0, values[-1] - values[0])
                        found = True
            if found:
                reading[field] = min(100, total / capacity * 100)
        return (reading, now, 'estimate', .4) if reading else None

    def tick(self, now=None, account_id=None, active=True):
        if not settings.pools or self.stop.is_set() or not self.tick_lock.acquire(blocking=False):
            return
        try:
            self._tick(time.time() if now is None else now, account_id, active)
        finally:
            self.tick_lock.release()

    def _tick(self, now, account_id, active):
        with self.lock:
            schedule = dict(self.due)
        rows = ensure_groups(self.db, now, schedule)
        grouped = {}
        for row in rows:
            grouped.setdefault(row['quota_group_id'], []).append(row)
        with self.lock:
            for group in list(self.due):
                if group not in grouped:
                    self.due.pop(group, None)
        for group, members in grouped.items():
            if not settings.pools or self.stop.is_set():
                break
            if account_id is not None and not any(a['id'] == account_id for a in members):
                continue
            with self.lock:
                if account_id is None and now < self.due.get(group, 0):
                    continue
                # Reserve before I/O so a launch during sampling can mark it due again.
                self.due[group] = now + 300
            attempts = {m['id']: self.attempts(m['id'], now) for m in members}
            opened = any(a['ended_at'] is None for aa in attempts.values() for a in aa)
            with self.lock:
                self.due[group] = min(self.due.get(group, now), now + (60 if opened else 300))
            candidates = []
            valid = [m for m in members if m['id'] not in self.db._pool_identity_errors]
            for m in valid:
                # Nonblocking: never read files during release or another profile operation.
                reservation = profiles.account_lock(m['id'])
                if not reservation.acquire(blocking=False):
                    continue
                try:
                    with self.db.lock:
                        ready = self.db.conn.execute("SELECT 1 FROM pool_account WHERE id=? AND state='ready'", (m['id'],)).fetchone()
                    if ready:
                        hit = self.sample(m, now, active=False, attempts=attempts[m['id']])
                        if hit:
                            candidates.append(hit)
                finally:
                    reservation.release()
            if not candidates and active:
                for m in valid:
                    hit = self.active_sample(m, now)
                    if hit:
                        candidates.append(hit)
                        break
            if not candidates:
                for m in valid:
                    with profiles.account_lock(m['id']):
                        with self.db.lock:
                            ready = self.db.conn.execute("SELECT 1 FROM pool_account WHERE id=? AND state='ready'", (m['id'],)).fetchone()
                        hit = self.estimate(m, attempts[m['id']], now) if ready else None
                    if hit:
                        candidates.append(hit)
            if candidates:
                reading, at, source, confidence = best(candidates)
                save(self.db, group, reading, source, confidence, at)
        if now >= self.retention_at:
            with self.db.lock:
                self.db.conn.execute('DELETE FROM usage_sample WHERE sampled_at < ? OR quota_group_id NOT IN (SELECT id FROM quota_group)', (iso(now - 14 * 86400),))
            self.retention_at = now + 3600

    def wait_delay(self, now):
        with self.lock:
            return max(1, min(300, min(self.due.values(), default=now + 300) - now))

    def run(self):
        while not self.stop.is_set() and settings.pools:
            self.wake.clear()
            try:
                self.tick()
            except Exception:
                pass  # No profile data or subprocess output in logs.
            self.wake.wait(self.wait_delay(time.time()))


def best(candidates):
    rank = {'cache': 0, 'statusline': 1, 'rollout': 0, 'cli': 2, 'app-server': 1, 'estimate': 3}
    return min(candidates, key=lambda r: (-r[3], rank[r[2]], -r[1]))


def collector(db):
    with db.lock:
        if not hasattr(db, '_pool_usage'):
            db._pool_usage = Collector(db)
        return db._pool_usage


def start(db):
    if not settings.pools:
        return None
    with db.lock:
        if not db.conn.execute('SELECT 1 FROM pool_account LIMIT 1').fetchone():
            return None
    c = collector(db)
    with c.lock:
        if c.thread is None:
            c.thread = threading.Thread(target=c.run, name='pool-usage', daemon=True)
            c.thread.start()
    return c


def attempt_started(db, account_id):
    if not settings.pools:
        return
    c = collector(db)
    with db.lock:
        row = db.conn.execute('SELECT quota_group_id FROM pool_account WHERE id=?', (account_id,)).fetchone()
    if row and row[0] is not None:
        with c.lock:
            c.due[row[0]] = 0
    c.wake.set()


def clean_peer(value, now=None):
    """P2 has no account grants. Fail closed until P5 supplies the grant model."""
    return {'enabled': True, 'groups': [], 'accounts': []} if settings.pools else None


def peer_view(db):
    return clean_peer(None)


def merge_peer(db, value, node_id, now=None):
    """No pool groups may cross peers without an account grant."""
    return None
