"""Local pool assignment. Selection reads database snapshots only; reservation uses P1's account lock.

No credential reads, probes, reconciliation, or launch preparation belong in choose().
"""
from __future__ import annotations

import json
import math
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from . import accounts, pool_usage, profiles, projects
from .config import POOL_DEFAULTS, POOL_POLICIES, settings
from .db import now

FIELDS = ('account_mode', 'pinned_account_id', 'pool_id', 'on_limit_policy')
BINDING_FIELDS = ('account_id', *FIELDS, 'node')
POLICIES = ('auto', 'wait')
ROLLBACK_POOL_ACCOUNT = '!pool-requires-p3'  # P1 rejects this before any process can run.
P4_RESUME_REASON = 'resuming on another account comes in P4'
P5_REASON = 'pool accounts run on this node until account grants (P5)'
# Lock order: P1 account -> migration -> assignment -> database. Never acquire an
# account lock from inside this lock. CRUD only takes assignment -> database.
assignment_lock = threading.RLock()


class RetryAssignment(Exception):
    """The preferred account changed while acquiring its P1 reservation lock."""


def binding(values):
    """Validate once for API bodies and persisted scheduler requests; legacy stays empty."""
    mode = values.get('account_mode', 'legacy')
    if (mode != 'legacy' or any(values.get(k) is not None for k in ('account_id', 'pool_id', 'pinned_account_id', 'on_limit_policy'))) and not settings.pools:
        raise projects.BadRequest('pools are off')
    if mode not in ('legacy', 'pinned', 'pool'):
        raise projects.BadRequest('account_mode must be legacy, pinned or pool')
    for key in ('account_id', 'pool_id', 'pinned_account_id'):
        if values.get(key) is not None:
            profiles.validate_account_id(values[key])
    policy = values.get('on_limit_policy')
    if policy is not None and policy not in POLICIES:
        raise projects.BadRequest('on_limit_policy must be auto or wait')
    aid, pid = values.get('pinned_account_id'), values.get('pool_id')
    if mode == 'legacy':
        if aid is not None or pid is not None or policy is not None:
            raise projects.BadRequest('pool fields require pinned or pool account_mode')
        return {'account_id': values['account_id']} if values.get('account_id') else {}
    if mode == 'pool' and (not pid or aid is not None or values.get('account_id') is not None):
        raise projects.BadRequest('pool mode requires pool_id and no pinned account')
    if mode == 'pinned':
        aid = aid or values.get('account_id')
        if not aid or pid is not None or values.get('account_id') not in (None, aid):
            raise projects.BadRequest('pinned mode requires one pinned_account_id and no pool_id')
    return {'account_mode': mode, **({'pool_id': pid} if mode == 'pool' else {'pinned_account_id': aid}),
            **({'on_limit_policy': policy} if policy else {}),
            **({'node': values['node']} if values.get('node') is not None else {})}


def validate_target(db, provider, values):
    """Validate durable requests even when queued, without requiring a slot today."""
    b = binding(values)
    if b.get('account_mode', 'legacy') == 'legacy':
        return b
    if b.get('node') not in (None, '', 'local'):
        raise projects.BadRequest(P5_REASON)
    if b.get('pool_id'):
        pool = _pool(db, b['pool_id'])
        if pool['provider'] != provider:
            raise projects.BadRequest(f"pool provider {pool['provider']} differs from requested provider {provider}")
        b.setdefault('on_limit_policy', pool['on_limit_policy'])
    else:
        profiles.require_account(db, provider, b['pinned_account_id'])
        b.setdefault('on_limit_policy', POOL_POLICIES['pinned'])
    return b


def job_binding(db, provider, values):
    """Persist P1's account_id too, so an older scheduler cannot use the global login."""
    b = validate_target(db, provider, values)
    if b.get('account_mode') == 'pinned':
        b['account_id'] = b['pinned_account_id']
    elif b.get('account_mode') == 'pool':
        b['account_id'] = ROLLBACK_POOL_ACCOUNT
    return b


def scheduled_binding(values):
    """Only the scheduler decodes the rollback sentinel; API validation stays strict."""
    b = dict(values)
    if b.get('account_mode') == 'pool' and b.get('account_id') == ROLLBACK_POOL_ACCOUNT:
        b.pop('account_id')
    return b


def resume_binding(values, account_id=None):
    """A continuation is pinned to its recorded account, never a fresh pool choice."""
    if values.get('account_mode', 'legacy') == 'legacy':
        return dict(values)
    aid = account_id or values.get('account_id') or values.get('pinned_account_id')
    if not aid or aid == ROLLBACK_POOL_ACCOUNT:
        raise projects.BadRequest(P4_RESUME_REASON + ': no recorded account')
    return {**{k: v for k, v in values.items() if k not in BINDING_FIELDS},
            'account_mode': 'pinned', 'pinned_account_id': aid, 'account_id': aid,
            'on_limit_policy': values.get('on_limit_policy') or 'wait',
            **({'node': values['node']} if values.get('node') is not None else {})}


def safe_label(value, fallback):
    # Labels are user text. Reuse the public redactor and also suppress identity-like labels.
    from .nodes_relay import redact
    label = accounts.clean_label(value) or fallback
    if '@' in label or re.search(r'\b[0-9a-f]{8}-[0-9a-f-]{27,}\b', label, re.I):
        return fallback
    return redact(label)


def thresholds(pool, member=None):
    return {key: (member or {}).get(key) if (member or {}).get(key) is not None else
            pool.get(key) if pool.get(key) is not None else default for key, default in POOL_DEFAULTS.items()}


def _pool(db, pid):
    profiles.validate_account_id(pid)
    with db.lock:
        row = db.conn.execute('SELECT * FROM pool WHERE id=?', (pid,)).fetchone()
    if row is None:
        raise projects.BadRequest('no such pool')
    return dict(row)


def list_pools(db, compact=False):
    with db.lock:
        rows = [dict(r) for r in db.conn.execute('SELECT * FROM pool ORDER BY id')]
        members = [dict(r) for r in db.conn.execute('SELECT * FROM pool_member ORDER BY pool_id,account_id')]
    out = []
    for row in rows:
        row['name'] = safe_label(row['name'], row['id'])
        if compact:
            out.append({k: row[k] for k in ('id', 'provider', 'name')})
        else:
            out.append({**row, **thresholds(row), 'members': [
                {**m, 'effective': thresholds(row, m)} for m in members if m['pool_id'] == row['id']]})
    return out


def _settings(values, member=False):
    allowed = {'soft_pct_5h', 'soft_pct_weekly', 'concurrency'} if member else {*POOL_DEFAULTS, 'name', 'on_limit_policy'}
    if set(values) - allowed:
        raise projects.BadRequest('unknown pool setting')
    clean = dict(values)
    for key in allowed & values.keys():
        value = values[key]
        if key == 'name':
            if not isinstance(value, str) or not value.strip():
                raise projects.BadRequest('name is required')
            clean[key] = safe_label(value, 'Pool')
        elif key == 'on_limit_policy':
            if value not in POLICIES:
                raise projects.BadRequest('on_limit_policy must be auto or wait')
        elif value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise projects.BadRequest(f'{key} must be a finite number')
            if key == 'concurrency':
                if not isinstance(value, int) or not 1 <= value <= 1000:
                    raise projects.BadRequest('concurrency must be 1..1000')
            elif not 0 <= value <= 100:
                raise projects.BadRequest(f'{key} must be 0..100')
    return clean


def _check_thresholds(pool, member=None):
    t = thresholds(pool, member)
    if any(t['soft_pct_' + window] > t['hard_pct_' + window] for window in ('5h', 'weekly')):
        raise projects.BadRequest('soft thresholds must not exceed hard thresholds')


def save_pool(db, values, pid=None):
    profiles.require_enabled()
    values = dict(values)
    with assignment_lock:
        if pid is None:
            pid = profiles.validate_account_id(values.pop('id', None))
            provider = values.pop('provider', None)
            if provider not in ('claude', 'codex'):
                raise projects.BadRequest('provider must be claude or codex')
            row = {'id': pid, 'provider': provider, 'name': pid, 'on_limit_policy': POOL_POLICIES['pool'], 'created_at': now(), **_settings(values)}
            _check_thresholds(row)
            with db.lock:
                if db.conn.execute('SELECT 1 FROM pool WHERE id=?', (pid,)).fetchone():
                    raise projects.Conflict('pool already exists')
                db.conn.execute(f"INSERT INTO pool ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))
        else:
            row = {**_pool(db, pid), **_settings(values)}
            _check_thresholds(row)
            with db.lock:
                members = [dict(r) for r in db.conn.execute('SELECT * FROM pool_member WHERE pool_id=?', (pid,))]
            for member in members:
                _check_thresholds(row, member)
            clean = _settings(values)
            if clean:
                with db.lock:
                    db.conn.execute(f"UPDATE pool SET {','.join(k+'=?' for k in clean)} WHERE id=?", (*clean.values(), pid))
    return next(p for p in list_pools(db) if p['id'] == pid)


def delete_pool(db, pid):
    with assignment_lock:
        _pool(db, pid)
        with db.lock:
            busy = db.conn.execute('''SELECT 1 FROM runtime_attempt a LEFT JOIN tasks t ON CAST(t.id AS TEXT)=a.task_id
                WHERE a.ended_at IS NULL AND (a.pool_id=? OR t.pool_id=?) LIMIT 1''', (pid, pid)).fetchone()
            if busy:
                raise projects.Conflict('pool has an open attempt')
            db.conn.execute('DELETE FROM pool_member WHERE pool_id=?', (pid,))
            db.conn.execute('DELETE FROM pool WHERE id=?', (pid,))
    return {'deleted': pid}


def member(db, pid, aid, values=None):
    profiles.validate_account_id(aid)
    with assignment_lock:
        pool = _pool(db, pid)
        if values is None:
            with db.lock:
                db.conn.execute('DELETE FROM pool_member WHERE pool_id=? AND account_id=?', (pid, aid))
            return {'removed': aid}
        clean = _settings(values, member=True)
        _check_thresholds(pool, clean)
        with db.lock:
            account = db.conn.execute('SELECT provider FROM pool_account WHERE id=?', (aid,)).fetchone()
            if not account:
                raise projects.BadRequest('no such account profile')
            if account[0] != pool['provider']:
                raise projects.BadRequest(f"account provider {account[0]} differs from pool provider {pool['provider']}")
            db.conn.execute('''INSERT INTO pool_member(pool_id,account_id,soft_pct_5h,soft_pct_weekly,concurrency)
                VALUES (?,?,?,?,?) ON CONFLICT(pool_id,account_id) DO UPDATE SET
                soft_pct_5h=excluded.soft_pct_5h,soft_pct_weekly=excluded.soft_pct_weekly,concurrency=excluded.concurrency''',
                (pid, aid, clean.get('soft_pct_5h'), clean.get('soft_pct_weekly'), clean.get('concurrency')))
    return {'pool_id': pid, 'account_id': aid, **clean}


def capabilities(account):
    """Only explicit account metadata is authoritative; global catalogs are not plan entitlements."""
    raw = account.get('allowed_models')
    try:
        models = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        models = []  # malformed explicit restrictions fail closed
    return account.get('billing_class', 'subscription'), models


def _reset(candidate):
    values = [pool_usage.epoch(v) for v in candidate['resets'].values()]
    current = datetime.now(timezone.utc).timestamp()
    return min((v for v in values if v is not None and v > current), default=float('inf'))


def _reset_text(candidate):
    at = _reset(candidate)
    return 'reset unknown' if not math.isfinite(at) else 'resets ' + datetime.fromtimestamp(at, timezone.utc).strftime('%H:%M UTC')


def choose(db, provider, pool_id=None, pinned_account_id=None, requested_model=None, repo=None, node=None):
    """Read-only assignment for an already authorized local repo; never changes launch constraints."""
    profiles.require_enabled()
    if bool(pool_id) == bool(pinned_account_id):
        raise projects.BadRequest('choose requires a pool or a pinned account')
    pool = _pool(db, pool_id) if pool_id else {}
    if pinned_account_id:
        profiles.validate_account_id(pinned_account_id)
    with db.lock:
        members = {r['account_id']: dict(r) for r in db.conn.execute('SELECT * FROM pool_member WHERE pool_id=?', (pool_id,))} if pool_id else {pinned_account_id: {}}
        all_accounts = {r['id']: dict(r) for r in db.conn.execute('SELECT * FROM pool_account')}
        abandoned = profiles.abandoned_reservations(db)
        activity = {r['account_id']: dict(r) for r in db.conn.execute(f'''SELECT account_id,MAX(started_at) AS last,
            SUM(ended_at IS NULL AND id NOT IN ({','.join('?' for _ in abandoned) or '0'})) AS active
            FROM runtime_attempt GROUP BY account_id''', abandoned)}
    usage = {r['account_id']: r for r in pool_usage.account_headroom(db)}
    candidates, ranks = [], {}
    for aid, member_row in sorted(members.items()):
        a = all_accounts.get(aid, {})
        u, t = usage.get(aid, {}), thresholds(pool, member_row)
        c = {'account_id': aid, 'label': safe_label(a.get('label'), aid),
             'pct_5h': u.get('window_5h_pct'), 'pct_weekly': u.get('weekly_pct'),
             'resets': {'5h': u.get('window_5h_reset_at'), 'weekly': u.get('weekly_reset_at')},
             'state': a.get('state', 'missing'), 'eligible': True, 'why_not': None}
        known = u.get('state') == 'ok' and c['pct_5h'] is not None and c['pct_weekly'] is not None
        billing, models = capabilities(a)
        why = None
        if a.get('provider') != provider or pool.get('provider', provider) != provider:
            why = 'provider mismatch'
        elif c['state'] != 'ready':
            why = 'account is not ready'
        elif node not in (None, '', 'local'):
            why = P5_REASON
        elif any(c['pct_' + w] is not None and c['pct_' + w] >= t['soft_pct_' + w] for w in ('5h', 'weekly')):
            why = f"usage at or above soft thresholds {t['soft_pct_5h']:g}% / {t['soft_pct_weekly']:g}%"
        elif activity.get(aid, {}).get('active', 0) >= t['concurrency']:
            why = f"concurrency cap {t['concurrency']} reached"
        elif requested_model and models is not None and (not isinstance(models, list) or requested_model not in models):
            why = 'requested model is not offered by this account'
        elif billing != 'subscription':
            why = 'subscription launch requires a subscription account'
        c.update(eligible=why is None, why_not=why)
        candidates.append(c)
        ranks[aid] = (not known, max(c['pct_5h'], c['pct_weekly']) if known else 0,
                      _reset(c) if known else 0, activity.get(aid, {}).get('last') or '', aid)
    known_eligible = any(c['eligible'] and not ranks[c['account_id']][0] for c in candidates)
    for c in candidates:
        if c['eligible'] and known_eligible and ranks[c['account_id']][0]:
            c.update(eligible=False, why_not='usage unknown; a known account is eligible')
    eligible = [c for c in candidates if c['eligible']]
    if not eligible:
        details = []
        for c in sorted(candidates, key=lambda c: (_reset(c), c['account_id'])):
            pct = ' / '.join('unknown' if c[k] is None else f'{c[k]:g}%' for k in ('pct_5h', 'pct_weekly'))
            details.append(f"{c['label']} {pct} ({_reset_text(c)}; {c['why_not']})")
        prefix = P5_REASON if node not in (None, '', 'local') else f"no account under {thresholds(pool)['soft_pct_5h']:g}% with a free slot"
        return {'refused': True, 'reason': prefix + (': ' + ', '.join(details) if details else ': no members'), 'candidates': candidates}
    winner = min(eligible, key=lambda c: ranks[c['account_id']])
    aid = winner['account_id']
    if ranks[aid][0]:
        reason = f"{winner['label']}: usage unknown, least recently used"
    else:
        reason = f"{winner['label']}: {winner['pct_5h']:g}% used (5 h), {winner['pct_weekly']:g}% weekly, {_reset_text(winner)}"
        above = sum('soft thresholds' in (c['why_not'] or '') for c in candidates)
        if above:
            reason += f'; {above} others above soft thresholds'
    if capabilities(all_accounts[aid])[1] is None:
        reason += '; model availability unknown (known restrictions only)'
    return {'account_id': aid, 'node': 'local', 'reason': reason, 'candidates': candidates}


def selection(values, provider, repo=None, requested_model=None):
    b = binding(values)
    return {'provider': provider, 'pool_id': b.get('pool_id'), 'pinned_account_id': b.get('pinned_account_id'),
            'requested_model': requested_model, 'repo': repo, 'node': b.get('node')}


@contextmanager
def checked_assignment(db, account_id, selection):
    """Called inside P1 serialization; hold only across selection and the row INSERT."""
    with assignment_lock:
        result = choose(db, **selection) if selection else None
        if result and result.get('refused'):
            raise projects.BadRequest(result['reason'])
        if result and result['account_id'] != account_id:
            raise RetryAssignment()
        yield result


def reserve(db, provider, values, *, repo=None, requested_model=None, **attempt_fields):
    profiles.reconcile_reservations(db)
    b = binding(values)
    request = selection(b, provider, repo, requested_model)
    while True:
        result = choose(db, **request)
        if result.get('refused'):
            raise projects.BadRequest(result['reason'])
        try:
            aid = profiles.start_attempt(db, provider, result['account_id'], selection=request,
                                         account_mode=b['account_mode'], pool_id=b.get('pool_id'),
                                         pinned_account_id=b.get('pinned_account_id'), on_limit_policy=b.get('on_limit_policy'),
                                         **attempt_fields)
        except RetryAssignment:
            continue
        with db.lock:
            recorded = db.conn.execute('SELECT assignment FROM runtime_attempt WHERE id=?', (aid,)).fetchone()[0]
        return aid, json.loads(recorded)


def requested_model(opts=None, words=None):
    """Extra arguments follow controls in launch argv, so the last explicit model wins."""
    model = (opts or {}).get('model')
    words = words or []
    for i, word in enumerate(words):
        if word == '--':
            break
        if word in ('--model', '-m') and i + 1 < len(words):
            model = words[i + 1]
        elif word.startswith('--model='):
            model = word.split('=', 1)[1]
        elif word.startswith('-m') and not word.startswith('--') and len(word) > 2:
            model = word[2:]
    return model.strip() if isinstance(model, str) else model
