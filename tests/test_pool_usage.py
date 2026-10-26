"""P2 uses temporary profiles and fake CLIs only."""
import base64
import json
import re
import time
import threading
import os
import signal
from pathlib import Path

import pytest

from app import pool_usage as pu, profiles, usage_refresh
from app.config import settings
from app.db import DB


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'pools', True)
    monkeypatch.setattr(settings, 'data_dir', tmp_path)
    monkeypatch.setattr(pu.shutil, 'which', lambda name: None)
    db = DB(tmp_path / 'test.db')
    yield db
    db.close()


def account(db, aid='one', provider='claude', seat='seat-one', plan='plus'):
    path = profiles.profile_dir(provider, aid)
    path.mkdir(parents=True, exist_ok=True)
    if provider == 'claude':
        data = {'oauthAccount': {'organizationUuid': 'organization-one', 'accountUuid': seat}}
        leaf = '.claude.json'
    else:
        claims = {'https://api.openai.com/auth': {'chatgpt_user_id': seat, 'chatgpt_plan_type': plan}}
        # Identity-only JWT-shaped fixture, no credential material.
        encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
        data = {'tokens': {'account_id': 'workspace-one', 'id_token': 'header.' + encoded + '.signature'}}
        leaf = 'auth.json'
    (path / leaf).write_text(json.dumps(data))
    db.conn.execute('INSERT INTO pool_account(id,provider,label,state,created_at) VALUES (?,?,?,?,?)', (aid, provider, aid, 'ready', pu.iso(time.time())))
    return path


def cache(path, at, pct=32):
    file = path / '.claude.json'
    data = json.loads(file.read_text())
    data['cachedUsageUtilization'] = {'fetchedAtMs': at * 1000, 'utilization': {'five_hour': {'utilization': pct, 'resets_at': pu.iso(at + 3600)}, 'seven_day': {'utilization': 65, 'resets_at': pu.iso(at + 86400)}}}
    file.write_text(json.dumps(data))


def attempt(db, aid='one', provider='claude', at=None):
    rid = db.add_session(tmux_name='pane-' + aid, project='p', repo='r', name=aid, launcher=provider)
    n = profiles.start_attempt(db, provider, aid, session_row=rid)
    if at:
        db.conn.execute('UPDATE runtime_attempt SET started_at=? WHERE id=?', (pu.iso(at), n))
    return n


def test_dedup_cache_headroom_and_surface(box):
    now = int(time.time())
    first = account(box)
    account(box, 'two')
    cache(first, now)
    pu.collector(box).tick(now)
    groups = pu.groups_view(box, now)
    assert len(groups) == 1 and groups[0]['accounts'] == ['one', 'two']
    assert groups[0]['source'] == 'cache' and groups[0]['confidence'] == 1
    assert groups[0]['window_5h_pct'] == 32
    assert box.conn.execute('SELECT count(*) FROM usage_sample').fetchone()[0] == 1
    pu.collector(box).tick(now + 300)
    assert box.conn.execute('SELECT count(*) FROM usage_sample').fetchone()[0] == 1
    heads = pu.account_headroom(box, now)
    assert len(heads) == 2 and heads[0]['group_id'] == heads[1]['group_id']
    assert re.fullmatch('[0-9a-f]{32}', box.conn.execute('SELECT subscription_key FROM quota_group').fetchone()[0])
    def keys(v):
        if isinstance(v, dict):
            for k, child in v.items():
                assert not re.search('cred|token|auth|secret|email|uuid', k)
                keys(child)
        elif isinstance(v, list):
            for child in v:
                keys(child)
    keys(groups)
    assert 'organization-one' not in json.dumps(groups)


def test_cache_decay_and_statusline_preference(box):
    now = int(time.time())
    path = account(box)
    n = attempt(box, at=now - 900)
    cache(path, now - 360)
    hit = pu.cache_reading({'id': 'one', 'provider': 'claude'}, now)
    assert hit[2] == 1  # base confidence; best() applies the common freshness curve
    cache(path, now - 601)
    assert pu.observe_statusline(box, 'pane-one', {'five_hour': {'used_percentage': 42}}, now)
    pu.collector(box).tick(now, account_id='one', active=False)
    g = pu.groups_view(box, now)[0]
    assert (g['source'], g['confidence'], g['window_5h_pct']) == ('statusline', .8, 42)
    assert box.kv_get('pool_statusline:' + str(n))
    assert box.kv_get('rate_limits') is None


def test_unknown_preserves_missing_and_stale_values(box):
    now = int(time.time())
    path = account(box)
    pu.ensure_groups(box)
    g = pu.groups_view(box, now)[0]
    assert g['state'] == 'unknown' and g['window_5h_pct'] is None and g['weekly_pct'] is None
    cache(path, now - 360, 27)
    pu.collector(box).tick(now, active=False)
    g = pu.groups_view(box, now)[0]
    assert g['state'] == 'unknown' and g['age_s'] == 360 and g['window_5h_pct'] == 27


def test_backoff_and_cadence(box):
    now = int(time.time())
    path = account(box)
    cache(path, now)
    c = pu.collector(box)
    c.tick(now, active=False)
    g = pu.groups_view(box, now)[0]
    assert c.due[g['id']] == now + 300
    a = pu.ensure_groups(box)[0]
    for i, seconds in enumerate((120, 240, 480)):
        c.failure(a, now + i * 600)
        rec = box.kv_get('pool_refresh:one')['value']
        assert rec['backoff_s'] == seconds
        assert rec['next_at'] == now + i * 600 + max(300, seconds)
    assert pu.groups_view(box, now)[0]['confidence'] == .7
    attempt(box, at=now)
    c.tick(now, account_id='one', active=False)
    assert c.due[g['id']] == now + 60
    assert pu.account_headroom(box, now)[0]['open_attempts'] == 1


def test_start_only_marks_due_without_sampling(box, monkeypatch):
    now = int(time.time())
    path = account(box)
    cache(path, now)
    c = pu.collector(box)
    g = pu.ensure_groups(box)[0]['quota_group_id']
    c.due[g] = now + 300
    monkeypatch.setattr(c, 'tick', lambda **kw: pytest.fail('launch must not collect'))
    monkeypatch.setattr(pu, 'identity', lambda *a: pytest.fail('launch must not parse'))
    attempt(box)
    assert c.due[g] == 0
    assert pu.groups_view(box, now)[0]['source'] is None
    assert pu.collector(box).thread is None


def test_off_and_empty_do_nothing(box, monkeypatch):
    assert pu.start(box) is None
    account(box)
    monkeypatch.setattr(settings, 'pools', False)
    assert pu.start(box) is None
    pu.Collector(box).tick()
    assert pu.groups_view(box) == [] and pu.account_headroom(box) == []
    assert box.conn.execute('SELECT count(*) FROM quota_group').fetchone()[0] == 0
    assert box.conn.execute('SELECT count(*) FROM usage_sample').fetchone()[0] == 0
    assert not pu.observe_statusline(box, 'pane-one', {})


def test_codex_plain_account_id_only_no_jwt_seat(box):
    account(box, 'one', 'codex', 'seat-one', 'team')
    account(box, 'two', 'codex', 'seat-two', 'team')
    account(box, 'three', 'codex', 'seat-one', 'team')
    pu.ensure_groups(box)
    groups = pu.groups_view(box)
    assert len(groups) == 1 and groups[0]['accounts'] == ['one', 'three', 'two']
    assert groups[0]['plan'] is None


def test_codex_rollout_credits_attempt_not_creator(box):
    now = int(time.time())
    path = account(box, 'two', 'codex')
    n = attempt(box, 'two', 'codex', at=now - 60)
    sid = '11111111-1111-4111-8111-111111111111'
    box.conn.execute('UPDATE runtime_attempt SET provider_session_id=? WHERE id=?', (sid, n))
    root = path / 'sessions'
    root.mkdir()
    records = [{'type': 'session_meta', 'payload': {'creator_account_id': 'creator-one'}},
               {'type': 'event_msg', 'timestamp': pu.iso(now), 'payload': {'type': 'token_count', 'rate_limits': {'primary': {'used_percent': 44, 'window_minutes': 300, 'resets_at': now + 3600}}}}]
    (root / ('rollout-test-' + sid + '.jsonl')).write_text('\n'.join(json.dumps(r) for r in records))
    pu.collector(box).tick(now, account_id='two', active=False)
    g = pu.groups_view(box, now)[0]
    assert (g['accounts'], g['source'], g['confidence'], g['window_5h_pct']) == (['two'], 'rollout', .8, 44)
    box.conn.execute('UPDATE runtime_attempt SET started_at=? WHERE id=?', (pu.iso(now + 1), n))
    assert pu.rollout_reading(dict(box.conn.execute('SELECT * FROM runtime_attempt').fetchone()), now) is None


def test_cli_refresh_only_idle_bound_once(box, monkeypatch):
    from app import main
    now = int(time.time())
    path = account(box)
    attempt(box, at=now - 10)
    box.set_state('pane-one', 'idle', 'Stop')
    monkeypatch.setattr(main, 'db', box)
    monkeypatch.setattr(usage_refresh.tmux, 'list_sessions', lambda: {'pane-one': {'command': 'claude', 'attached': 0}})
    monkeypatch.setattr(usage_refresh.tmux, 'viewers', lambda: {})
    monkeypatch.setattr(usage_refresh.tmux, 'has_session', lambda _: True)
    pane = ['❯ ']
    monkeypatch.setattr(usage_refresh.tmux, 'capture', lambda *a, **kw: pane[0])
    typed, timers = [], []
    monkeypatch.setattr(usage_refresh.tmux, 'send_text', lambda *args, **kw: typed.append(args))
    monkeypatch.setattr(usage_refresh, 'later', lambda delay, fn: timers.append(fn))
    monkeypatch.setattr(usage_refresh.tmux, 'send_keys', lambda *args: pane.__setitem__(0, '❯ '))
    usage_refresh.release()
    c = pu.collector(box)
    c.tick(now, account_id='one')
    assert len(typed) == 1 and len(timers) == 1
    cache(path, now)
    pane[0] = (Path(__file__).parent / 'fixtures/agents/claude/pane_usage.txt').read_text()
    timers.pop()()
    assert pu.groups_view(box, now)[0]['source'] == 'cli'
    assert pu.groups_view(box, now)[0]['confidence'] == .9
    (path / '.claude.json').write_text('{}')
    c.tick(now + 60, account_id='one')
    assert len(typed) == 1
    box.set_state('pane-one', 'working', 'UserPromptSubmit')
    c.tick(now + 301, account_id='one')
    assert len(typed) == 1


def test_app_server_backoff_and_profile_env(box, monkeypatch, tmp_path):
    path = account(box, provider='codex')
    exe = tmp_path / 'fake-codex'
    exe.write_text('''#!/usr/bin/env python3
import json, os, sys
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get('id') == 1:
        print(json.dumps({'id': 1, 'result': {}}), flush=True)
    if msg.get('id') == 2:
        assert os.environ.get('CODEX_HOME').endswith('/profiles/codex/one')
        assert 'OPENAI_API_KEY' not in os.environ
        print(json.dumps({'id': 2, 'result': {'rateLimits': {'primary': {'usedPercent': 31, 'windowDurationMins': 300}}}}), flush=True)
''')
    exe.chmod(0o700)
    monkeypatch.setattr(pu.shutil, 'which', lambda _: str(exe))
    assert pu.app_server({'id': 'one'})['window_5h_pct'] == 31
    n = attempt(box, provider='codex')
    profiles.finish_attempt(box, n, 'done')
    c = pu.collector(box)
    c.tick()
    assert pu.groups_view(box)[0]['source'] == 'app-server'
    assert pu.groups_view(box)[0]['confidence'] == .85


def test_peer_groups_fail_closed_and_retention_is_hourly(box):
    now = int(time.time())
    path = account(box)
    cache(path, now - 10)
    c = pu.collector(box)
    c.tick(now, active=False)
    g = pu.groups_view(box, now)[0]
    assert pu.peer_view(box) == {'enabled': True, 'groups': [], 'accounts': []}
    for hostile in [g, {**g, 'sampled_at': pu.iso(now + 100), 'confidence': float('nan')},
                    {**g, 'window_5h_pct': None, 'weekly_pct': None},
                    {**g, 'group_key': box.conn.execute('SELECT subscription_key FROM quota_group').fetchone()[0]}]:
        pu.merge_peer(box, {'groups': [hostile]}, 'peer-one', now)
        assert pu.clean_peer({'groups': [hostile]}, now)['groups'] == []
        assert pu.groups_view(box, now)[0]['window_5h_pct'] == 32
    statements = []
    box.conn.set_trace_callback(statements.append)
    for offset in (1, 60, 300, 600, 3599):
        c.tick(now + offset, active=False)
    assert not any('DELETE FROM usage_sample' in q for q in statements)
    c.tick(now + 15 * 86400, active=False)
    assert box.conn.execute('SELECT count(*) FROM usage_sample').fetchone()[0] == 0


def test_usage_api_and_state(lite_client, monkeypatch):
    from app import main
    now = time.time()
    monkeypatch.setattr(pu.time, 'time', lambda: now)
    monkeypatch.setattr(settings, 'allowed_users', {'owner'})
    headers = {'Tailscale-User-Login': 'owner', 'X-CCBoard': '1'}
    monkeypatch.setattr(settings, 'pools', False)
    assert lite_client.get('/api/pools/usage', headers=headers).status_code == 404
    assert lite_client.get('/api/state', headers=headers).json()['pools'] is None
    monkeypatch.setattr(settings, 'pools', True)
    path = account(main.db)
    account(main.db, 'codex-copy', 'codex')
    cache(path, int(time.time()))
    pu.collector(main.db).tick(active=False)
    body = lite_client.get('/api/pools/usage', headers=headers).json()
    assert body['groups'][0]['source'] == 'cache'
    assert lite_client.get('/api/state', headers=headers).json()['pools']['groups'] == body['groups']
    assert not re.search(r'"[^" ]*(?:cred|token|auth|secret|email|uuid)[^" ]*"\s*:', json.dumps(body))
    assert 'organization-one' not in json.dumps(body)
    assert 'seat-one' not in json.dumps(body)
    assert 'workspace-one' not in json.dumps(body)
    assert 'subscription_key' not in json.dumps(body)


def test_real_failure_path_keeps_streak_and_five_minute_floor(box, monkeypatch):
    now = int(time.time())
    account(box, provider='codex')
    n = attempt(box, provider='codex')
    profiles.finish_attempt(box, n, 'done')
    a = pu.ensure_groups(box)[0]
    pu.save(box, a['quota_group_id'], {'window_5h_pct': 34}, 'cache', 1, now - 1)
    monkeypatch.setattr(pu.shutil, 'which', lambda _: 'fake-codex')
    calls = []
    monkeypatch.setattr(pu, 'app_server', lambda a: calls.append(a['id']))
    c = pu.collector(box)
    for offset, expected in [(0, 120), (300, 240), (600, 480)]:
        c.tick(now + offset, account_id='one')
        assert box.kv_get('pool_refresh:one')['value']['backoff_s'] == expected
        c.tick(now + offset + 60, account_id='one')
    assert calls == ['one'] * 3
    assert pu.groups_view(box, now)[0]['confidence'] == .7


def test_unchanged_cache_confidence_decays_without_new_timestamp(box):
    now = int(time.time())
    path = account(box)
    cache(path, now)
    c = pu.collector(box)
    c.tick(now, active=False)
    c.tick(now + 360, active=False)
    g = pu.groups_view(box, now + 360)[0]
    assert g['confidence'] == pytest.approx(pu.freshness(now, now + 360)) and g['age_s'] == 360
    assert box.conn.execute('SELECT count(*) FROM usage_sample').fetchone()[0] == 1


def test_identity_reads_follow_cadence(box, monkeypatch):
    now = int(time.time())
    account(box)
    calls = []
    original = pu.identity
    monkeypatch.setattr(pu, 'identity', lambda *args: (calls.append(args[0]), original(*args))[1])
    c = pu.collector(box)
    c.tick(now, active=False)
    c.tick(now + 1, active=False)
    c.tick(now + 299, active=False)
    assert calls == ['claude']
    c.tick(now + 300, active=False)
    assert calls == ['claude', 'claude']


def test_claude_estimate_only_counts_bound_attempt_slice(box):
    now = int(time.time())
    path = account(box)
    n = attempt(box, at=now - 60)
    a = pu.ensure_groups(box)[0]
    box.conn.execute("UPDATE quota_group SET plan='pro' WHERE id=?", (a['quota_group_id'],))
    sid = '11111111-1111-4111-8111-111111111111'
    box.conn.execute('UPDATE runtime_attempt SET provider_session_id=? WHERE id=?', (sid, n))
    folder = path / 'projects' / 'project'
    folder.mkdir(parents=True)
    def message(at, ident, count):
        return {'type': 'assistant', 'timestamp': pu.iso(at), 'message': {'id': ident, 'usage': {'input_tokens': count, 'output_tokens': 100}}}
    old = message(now - 120, 'old', 90000)
    fresh = message(now - 30, 'new', 900)
    (folder / (sid + '.jsonl')).write_text('\n'.join(json.dumps(r) for r in [old, fresh, fresh]))
    pu.collector(box).tick(now, account_id='one', active=False)
    g = pu.groups_view(box, now)[0]
    assert g['source'] == 'estimate' and g['confidence'] == .4 and g['state'] == 'unknown'
    assert g['window_5h_pct'] == 1 and g['weekly_pct'] == pytest.approx(1 / 7)




def test_hook_bound_reading_never_enters_legacy_windows(box):
    from app import hooks
    account(box)
    attempt(box)
    hooks.apply(box, 'pane-one', 'statusline', {'rate_limits': {'five_hour': {'used_percentage': 38}}})
    assert box.kv_get('rate_limits') is None
    assert box.sample_last('rl_5h', 'claude') is None
    assert box.kv_get('pool_statusline:1')['value']['reading']['window_5h_pct'] == 38


def test_codex_legacy_readings_exclude_bound_thread(box, monkeypatch):
    from app.agents import codex_rollout as cr
    account(box, provider='codex')
    n = attempt(box, provider='codex')
    sid = '11111111-1111-4111-8111-111111111111'
    box.conn.execute('UPDATE runtime_attempt SET provider_session_id=? WHERE id=?', (sid, n))
    path = Path('/tmp') / ('rollout-test-' + sid + '.jsonl')
    unbound = Path('/tmp/rollout-unbound.jsonl')
    monkeypatch.setattr(cr, 'recent_rollouts', lambda since: [(time.time(), 1, path), (time.time(), 1, unbound)])
    seen = []
    monkeypatch.setattr(cr, '_rollout_reading', lambda path, *a: seen.append(path))
    cr.Tailer().rate_limits(box, time.time())
    assert seen == [unbound]


def test_local_node_is_reported_and_global_refresh_skips_pool_pane(box, monkeypatch):
    from app import main, nodes
    now = int(time.time())
    path = account(box)
    nodes.id_path().write_text('n_' + 'a' * 16)
    cache(path, now)
    attempt(box)
    pu.collector(box).tick(now, active=False)
    assert pu.groups_view(box)[0]['nodes'] == ['n_' + 'a' * 16]
    monkeypatch.setattr(main, 'db', box)
    monkeypatch.setattr(main.tmux, 'list_sessions', lambda: {'pane-one': {'command': 'claude', 'attached': 0}})
    monkeypatch.setattr(main.tmux, 'viewers', lambda: {})
    assert main._refresh_panes() == []


def test_node_read_model_is_opt_in_and_recomputes_unknown(box, monkeypatch):
    from app import nodes_hub
    now = int(time.time())
    path = account(box)
    cache(path, now)
    pu.collector(box).tick(now, active=False)
    node = 'n_' + 'b' * 16
    raw = {'node': {'id': node}, 'pools': pu.peer_view(box)}
    clean = nodes_hub.clean_state(raw, {'node_id': node})
    assert clean['pools']['groups'] == []
    assert clean['pools']['accounts'] == []
    hub = nodes_hub.NodeHub(box)
    row = {'peer_id': 'peer-one'}
    view = hub._public(row, {'state': clean, 'ok_ts': now}, now + 360)
    assert view['state']['pools']['accounts'] == []
    monkeypatch.setattr(settings, 'pools', False)
    assert 'pools' not in nodes_hub.clean_state(raw, {'node_id': node})
    assert 'pools' not in hub._public(row, {'state': clean, 'ok_ts': now}, now)['state']


def test_usage_failure_does_not_abort_profile_launch(box, monkeypatch):
    account(box)
    c = pu.collector(box)
    monkeypatch.setattr(c, 'tick', lambda **kw: (_ for _ in ()).throw(OSError('fixture unavailable')))
    assert attempt(box) == 1
    assert c.wake.is_set()


def test_rollout_before_resume_in_same_second_is_not_credited(box):
    now = int(time.time())
    path = account(box, provider='codex')
    n = attempt(box, provider='codex', at=now - .2)
    sid = '11111111-1111-4111-8111-111111111111'
    box.conn.execute('UPDATE runtime_attempt SET provider_session_id=? WHERE id=?', (sid, n))
    root = path / 'sessions'
    root.mkdir()
    event = {'type': 'event_msg', 'timestamp': pu.iso(now - .8), 'payload': {'type': 'token_count', 'rate_limits': {'primary': {'used_percent': 88, 'window_minutes': 300}}}}
    (root / ('rollout-test-' + sid + '.jsonl')).write_text(json.dumps(event))
    a = dict(box.conn.execute('SELECT * FROM runtime_attempt').fetchone())
    assert pu.rollout_reading(a, now) is None


def test_stop_prevents_later_groups_from_starting_active_sources(box, monkeypatch):
    now = int(time.time())
    account(box, 'one', 'codex', 'seat-one', 'team')
    account(box, 'two', 'codex', 'seat-two', 'team')
    for aid in ('one', 'two'):
        n = attempt(box, aid, 'codex')
        profiles.finish_attempt(box, n, 'done')
    monkeypatch.setattr(pu.shutil, 'which', lambda _: 'fake-codex')
    c = pu.collector(box)
    calls = []
    def stop_after_first(a):
        calls.append(a['id'])
        c.stop.set()
        return {'window_5h_pct': 12}
    monkeypatch.setattr(pu, 'app_server', stop_after_first)
    c.tick(now)
    assert calls == ['one']
    c.tick(now + 300)
    assert calls == ['one']


def test_stale_due_regroup_and_deleted_groups_cannot_spin(box):
    now = int(time.time())
    path = account(box)
    c = pu.collector(box)
    c.tick(now, active=False)
    old = pu.groups_view(box)[0]['id']
    schedule = {999: now - 100}
    pu.ensure_groups(box, now, schedule)
    assert 999 not in schedule
    c.due[999] = now - 100
    data = json.loads((path / '.claude.json').read_text())
    data['oauthAccount']['accountUuid'] = 'changed'
    (path / '.claude.json').write_text(json.dumps(data))
    c.tick(now + 300, active=False)
    assert old not in c.due and 999 not in c.due
    assert c.wait_delay(now + 300) == 300
    assert box.conn.execute('SELECT count(*) FROM quota_group').fetchone()[0] == 1
    box.conn.execute('DELETE FROM pool_account')
    c.tick(now + 600, active=False)
    assert c.due == {} and c.wait_delay(now + 600) == 300


def test_attempts_window_cap_and_no_old_file_reads(box, monkeypatch):
    now = int(time.time())
    account(box)
    box.conn.executemany('''INSERT INTO runtime_attempt(n,account_id,provider,profile_dir,started_at,ended_at,provider_session_id)
        VALUES (1,'one','claude','unused',?,?,?)''', [(pu.iso(now - 9 * 86400), pu.iso(now - 8 * 86400), '11111111-1111-4111-8111-111111111111')] * 500)
    c = pu.collector(box)
    assert c.attempts('one', now) == []
    monkeypatch.setattr(pu, 'attempt_path', lambda *a: pytest.fail('old attempts must not read files'))
    a = pu.ensure_groups(box)[0]
    box.conn.execute("UPDATE quota_group SET plan='pro'")
    c.tick(now, active=False)
    old = dict(box.conn.execute('SELECT * FROM runtime_attempt LIMIT 1').fetchone())
    assert pu.transcript_tokens(old, now, 7 * 86400) is None
    assert pu.rollout_reading(old, now) is None
    box.conn.execute('UPDATE runtime_attempt SET ended_at=?', (pu.iso(now),))
    assert len(c.attempts('one', now)) == pu.MAX_ATTEMPTS
    box.conn.execute('UPDATE runtime_attempt SET ended_at=NULL WHERE id=1')
    assert c.attempts('one', now)[0]['id'] == 1


def test_one_attempt_load_and_no_estimate_when_copy_has_real_reading(box, monkeypatch):
    now = int(time.time())
    account(box)
    path = account(box, 'two')
    cache(path, now)
    c = pu.collector(box)
    calls, original = [], c.attempts
    monkeypatch.setattr(c, 'attempts', lambda aid, *a: (calls.append(aid), original(aid, *a))[1])
    monkeypatch.setattr(c, 'estimate', lambda *a: pytest.fail('real group reading exists'))
    c.tick(now, active=False)
    assert calls == ['one', 'two']


def test_rollout_resolves_once_and_reads_only_tail(box, monkeypatch):
    from app.agents import codex_rollout as cr
    now = int(time.time())
    path = account(box, provider='codex')
    sid = '11111111-1111-4111-8111-111111111111'
    n = attempt(box, provider='codex', at=now - 60)
    box.conn.execute('UPDATE runtime_attempt SET provider_session_id=? WHERE id=?', (sid, n))
    folder = path / 'sessions' / '2026' / '10' / '26'
    folder.mkdir(parents=True)
    file = folder / ('rollout-test-' + sid + '.jsonl')
    file.write_text('irrelevant\n' * 100000 + json.dumps({'type': 'event_msg', 'timestamp': pu.iso(now), 'payload': {'type': 'token_count', 'rate_limits': {'primary': {'used_percent': 20, 'window_minutes': 300}}}}))
    original, scans, tails = Path.glob, [], []
    def glob(self, pattern):
        assert '**' not in pattern, 'no per-attempt recursive traversal'
        scans.append(pattern)
        return original(self, pattern)
    monkeypatch.setattr(Path, 'glob', glob)
    read_tail = cr.read_tail
    monkeypatch.setattr(cr, 'read_tail', lambda path: (tails.append(path), read_tail(path))[1])
    a = dict(box.conn.execute('SELECT * FROM runtime_attempt').fetchone())
    assert pu.rollout_reading(a, now)[0]['window_5h_pct'] == 20
    assert pu.rollout_reading(a, now + 60)[0]['window_5h_pct'] == 20
    assert scans == [] and tails == [file, file]
    assert len(pu._ROLLOUT_INDEX[str((path / 'sessions').resolve())][1]) == 1


def test_transcript_incremental_offsets_and_window_expiry(box, monkeypatch):
    now = int(time.time())
    path = account(box)
    sid = '11111111-1111-4111-8111-111111111111'
    n = attempt(box, at=now - 7 * 86400)
    box.conn.execute('UPDATE runtime_attempt SET provider_session_id=? WHERE id=?', (sid, n))
    folder = path / 'projects' / 'p'
    folder.mkdir(parents=True)
    file = folder / (sid + '.jsonl')
    def msg(at, ident):
        return json.dumps({'type': 'assistant', 'timestamp': pu.iso(at), 'message': {'id': ident, 'usage': {'input_tokens': 100}}}) + '\n'
    file.write_text(msg(now - 19000, 'old') + msg(now - 10, 'new'))
    a = dict(box.conn.execute('SELECT * FROM runtime_attempt').fetchone())
    reads, original = [], Path.open
    monkeypatch.setattr(Path, 'open', lambda self, *a, **kw: (reads.append((str(self), a)), original(self, *a, **kw))[1])
    assert pu.transcript_tokens(a, now) == 100
    offset = pu._TRANSCRIPTS[str(file)]['offset']
    assert offset == file.stat().st_size
    assert pu.transcript_tokens(a, now, 7 * 86400) == 200
    assert len(reads) == 1
    with original(file, 'a') as f:
        f.write(msg(now, 'latest'))
    assert pu.transcript_tokens(a, now) == 200
    assert len(reads) == 2 and pu._TRANSCRIPTS[str(file)]['offset'] > offset
    assert pu.transcript_tokens(a, now + 18001) is None


def test_launch_does_not_wait_for_other_accounts_probe(box, monkeypatch):
    account(box, provider='codex')
    account(box, 'two')
    n = attempt(box, provider='codex')
    profiles.finish_attempt(box, n, 'done')
    entered, unblock, launched = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr(pu.shutil, 'which', lambda _: 'fake')
    def probe(a):
        entered.set()
        assert unblock.wait(3)
        return {'window_5h_pct': 12}
    monkeypatch.setattr(pu, 'app_server', probe)
    c = pu.collector(box)
    t = threading.Thread(target=c.tick, daemon=True)
    t.start()
    try:
        assert entered.wait(2)
        def launch():
            attempt(box, 'two')
            launched.set()
        launch_thread = threading.Thread(target=launch, daemon=True)
        launch_thread.start()
        assert launched.wait(.5), 'unrelated launch blocked behind collector I/O'
        launch_thread.join(1)
    finally:
        unblock.set()
        t.join(3)


@pytest.mark.parametrize('state,history,opened,expected', [('ready', False, False, 0), ('ready', True, True, 0), ('releasing', True, False, 0), ('ready', True, False, 1)])
def test_app_server_state_and_attempt_gates(box, monkeypatch, state, history, opened, expected):
    account(box, provider='codex')
    if history:
        n = attempt(box, provider='codex')
        if not opened:
            profiles.finish_attempt(box, n, 'done')
    box.conn.execute('UPDATE pool_account SET state=?', (state,))
    calls = []
    monkeypatch.setattr(pu.shutil, 'which', lambda _: 'fake')
    monkeypatch.setattr(pu, 'app_server', lambda a: calls.append(a['id']))
    pu.collector(box).tick()
    assert len(calls) == expected


def test_fresh_statusline_beats_400_second_cache(box):
    now = int(time.time())
    path = account(box)
    attempt(box, at=now - 600)
    box.set_state('pane-one', 'idle', 'Stop')
    box.conn.execute('UPDATE sessions SET state_at=?', (pu.iso(now - 2),))
    cache(path, now - 400, 80)
    pu.observe_statusline(box, 'pane-one', {'five_hour': {'used_percentage': 42}}, now)
    pu.collector(box).tick(now, active=False)
    g = pu.groups_view(box, now)[0]
    assert (g['source'], g['state'], g['window_5h_pct'], g['age_s']) == ('statusline', 'ok', 42, 2)


def test_statusline_keeps_last_activity_and_past_reset_is_unknown(box):
    now = int(time.time())
    account(box)
    n = attempt(box)
    box.set_state('pane-one', 'idle', 'Stop')
    box.conn.execute('UPDATE sessions SET state_at=?', (pu.iso(now - 601),))
    raw = {'five_hour': {'used_percentage': 10}, 'seven_day': {'used_percentage': 80, 'resets_at': pu.iso(now - 1)}}
    pu.observe_statusline(box, 'pane-one', raw, now)
    hit = box.kv_get('pool_statusline:' + str(n))['value']
    assert hit['at'] == now - 601 and hit['confidence'] == .95
    assert 'weekly_pct' not in hit['reading']
    assert pu.windows({'five_hour': {'used_percentage': 10, 'resets_at': pu.iso(now - 1)}}, now) is None
    pu.collector(box).tick(now, active=False)
    assert pu.groups_view(box, now)[0]['state'] == 'unknown'
    box.conn.execute('UPDATE sessions SET state_at=NULL')
    pu.observe_statusline(box, 'pane-one', raw, now)
    assert box.kv_get('pool_statusline:' + str(n))['value']['confidence'] == .8


@pytest.mark.parametrize('error', [profiles.BadRequest('fixture'), OSError('fixture')])
def test_bad_identity_isolated_and_logged_hourly(box, monkeypatch, caplog, error):
    now = int(time.time())
    account(box)
    path = account(box, 'two', seat='other')
    cache(path, now)
    original = pu.identity
    def broken(provider, path):
        if path.name == 'one':
            raise error
        return original(provider, path)
    monkeypatch.setattr(pu, 'identity', broken)
    c = pu.collector(box)
    c.tick(now, active=False)
    groups = pu.groups_view(box, now)
    assert next(g for g in groups if g['accounts'] == ['one'])['reason'] == 'identity file unavailable'
    assert next(g for g in groups if g['accounts'] == ['two'])['state'] == 'ok'
    c.tick(now + 300, active=False)
    assert caplog.text.count('Pool identity unavailable') == 1
    c.tick(now + 3600, active=False)
    assert caplog.text.count('Pool identity unavailable') == 2


def test_identity_one_read_no_jwt_decode(box, monkeypatch):
    path = account(box, provider='codex')
    original, reads = profiles._plain, []
    monkeypatch.setattr(profiles, '_plain', lambda file: (reads.append(file), original(file))[1])
    import base64
    monkeypatch.setattr(base64, 'urlsafe_b64decode', lambda *a: pytest.fail('JWT must not be decoded'))
    key, plan = pu.identity('codex', path)
    assert len(reads) == 1 and plan is None and re.fullmatch('[0-9a-f]{32}', key)


def test_attempt_close_prunes_statusline(box):
    account(box)
    n = attempt(box)
    pu.observe_statusline(box, 'pane-one', {'five_hour': {'used_percentage': 20}})
    assert box.kv_get('pool_statusline:' + str(n))
    profiles.finish_attempt(box, n, 'done')
    assert box.kv_get('pool_statusline:' + str(n)) is None


def test_unbound_statusline_with_pools_on_keeps_legacy_attribution(box):
    from app import hooks
    box.add_session(tmux_name='unbound', project='p', repo='r', name='unbound', launcher='claude')
    raw = {'rate_limits': {'five_hour': {'used_percentage': 38}}}
    hooks.apply(box, 'unbound', 'statusline', raw)
    pooled = box.kv_get('rate_limits')['value']
    sample = box.sample_last('rl_5h', 'claude')
    settings.pools = False
    hooks.apply(box, 'unbound', 'statusline', raw)
    assert box.kv_get('rate_limits')['value'] == pooled
    assert box.sample_last('rl_5h', 'claude')['value'] == sample['value']


@pytest.mark.parametrize('mode', ['bad-json', 'error', 'oversized', 'initialize-flood', 'hung-child', 'exited-parent'])
def test_app_server_faults_and_process_group_cleanup(box, monkeypatch, tmp_path, mode):
    import sys
    account(box, provider='codex')
    exe, pidfile = tmp_path / 'fake-codex', tmp_path / 'pid'
    code = f'''#!{sys.executable}
import os, sys, json, time, signal
mode = {mode!r}
if mode in ('hung-child', 'exited-parent'):
    pid = os.fork()
    if pid == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True:
            with open({str(pidfile) + '.heartbeat'!r}, 'a') as f: f.write('x')
            time.sleep(.01)
    with open({str(pidfile)!r}, 'w') as f: f.write(str(pid))
    if mode == 'exited-parent': sys.exit(0)
    while True: time.sleep(1)
if mode == 'bad-json': print('broken json', flush=True)
if mode == 'error': print(json.dumps({{'id': 1, 'error': {{'code': 1}}}}), flush=True)
if mode == 'initialize-flood':
    for i in range(100000): print(json.dumps({{'id': 1, 'result': {{}}}}), flush=True)
if mode == 'oversized':
    sys.stdout.write('x' * (2 * 1024 * 1024)); sys.stdout.flush()
time.sleep(5)
'''
    exe.write_text(code)
    exe.chmod(0o700)
    monkeypatch.setattr(pu.shutil, 'which', lambda _: str(exe))
    monkeypatch.setattr(pu, 'APP_SERVER_TIMEOUT', .25)
    killed, original = [], os.killpg
    def killpg(pid, sig):
        killed.append((pid, sig))
        original(pid, sig)
    monkeypatch.setattr(pu.os, 'killpg', killpg)
    before = time.monotonic()
    assert pu.app_server({'id': 'one'}) is None
    assert time.monotonic() - before < 2
    assert len(killed) == 1 and killed[0][1] == signal.SIGKILL
    if pidfile.exists():
        child = int(pidfile.read_text())
        heartbeat = Path(str(pidfile) + '.heartbeat')
        threading.Event().wait(.05)
        size = heartbeat.stat().st_size if heartbeat.exists() else 0
        threading.Event().wait(.1)
        assert (heartbeat.stat().st_size if heartbeat.exists() else 0) == size, 'grandchild survived'



@pytest.mark.parametrize('guard', ['attached', 'full', 'draft', 'working', 'permission', 'compacting', 'screen-working', 'multiline-draft'])
def test_usage_real_guards_never_type_or_clear_drafts(box, monkeypatch, guard):
    from app import main
    account(box)
    attempt(box)
    box.set_state('pane-one', 'idle', 'Stop')
    monkeypatch.setattr(main, 'db', box)
    monkeypatch.setattr(usage_refresh.tmux, 'list_sessions', lambda: {'pane-one': {'command': 'claude', 'attached': int(guard == 'attached')}})
    monkeypatch.setattr(usage_refresh.tmux, 'viewers', lambda: {'pane-one': {'full': int(guard == 'full')}})
    text = '❯\n  keep my second line\n──────────' if guard == 'multiline-draft' else '❯ keep my draft' if guard == 'draft' else '❯\nesc to interrupt' if guard == 'screen-working' else '❯ '
    monkeypatch.setattr(usage_refresh.tmux, 'capture', lambda *a, **kw: text)
    if guard == 'working':
        box.set_state('pane-one', 'working', 'UserPromptSubmit')
    if guard == 'permission':
        box.set_state('pane-one', 'waiting', 'PermissionRequest')
        box.update_flags('pane-one', {'wait_kind': 'permission'})
    if guard == 'compacting':
        box.update_flags('pane-one', {'compacting': True})
    sent = []
    monkeypatch.setattr(usage_refresh.tmux, 'send_text', lambda *a, **kw: sent.append(a))
    monkeypatch.setattr(usage_refresh.tmux, 'send_keys', lambda *a, **kw: sent.append(a))
    monkeypatch.setattr(usage_refresh, 'later', lambda *a: None)
    usage_refresh.release()
    pu.collector(box).tick()
    assert sent == []
    usage_refresh.release()


@pytest.mark.parametrize('when,change', [('type', 'full'), ('type', 'working'), ('finish', 'full'), ('finish', 'working'), ('finish', 'draft'), ('finish', 'leaving'), ('finish', 'none')])
def test_usage_rechecks_viewers_idle_draft_and_escape(box, monkeypatch, when, change):
    from app import main
    account(box)
    attempt(box)
    box.set_state('pane-one', 'idle', 'Stop')
    monkeypatch.setattr(main, 'db', box)
    monkeypatch.setattr(usage_refresh.tmux, 'list_sessions', lambda: {'pane-one': {'command': 'claude', 'attached': 0}})
    calls = []
    changed = [False]
    def viewers():
        calls.append(1)
        if when == 'type' and len(calls) == 2:
            changed[0] = True
        if changed[0] and change == 'working':
            box.set_state('pane-one', 'working', 'UserPromptSubmit')
        return {'pane-one': {'full': int(changed[0] and change == 'full')}}
    monkeypatch.setattr(usage_refresh.tmux, 'viewers', viewers)
    monkeypatch.setattr(usage_refresh.tmux, 'capture', lambda *a, **kw: '❯ draft' if changed[0] and change == 'draft' else (Path(__file__).parent / 'fixtures/agents/claude/pane_usage.txt').read_text() if changed[0] and not keys else '❯ ')
    monkeypatch.setattr(usage_refresh.tmux, 'has_session', lambda _: True)
    sent, keys, timers = [], [], []
    monkeypatch.setattr(usage_refresh.tmux, 'send_text', lambda *a, **kw: sent.append(a))
    monkeypatch.setattr(usage_refresh.tmux, 'send_keys', lambda *a: keys.append(a))
    monkeypatch.setattr(usage_refresh, 'later', lambda delay, fn: timers.append(fn))
    usage_refresh.release()
    pu.collector(box).tick()
    assert keys == [], 'no C-u'
    if when == 'type':
        assert sent == [] and timers == []
    else:
        assert sent == [('pane-one', '/usage')]
        changed[0] = True
        if change == 'leaving':
            box.conn.execute("UPDATE pool_account SET state='releasing'")
        timers[0]()
        assert keys == ([('pane-one', ['Escape'])] if change == 'none' else [])
    usage_refresh.release()


def test_shared_rollout_index_reused_across_attempts(box, monkeypatch):
    now = int(time.time())
    path = account(box, provider='codex')
    root = path / 'sessions'
    root.mkdir()
    ids = ['11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222']
    for sid in ids:
        (root / ('rollout-test-' + sid + '.jsonl')).write_text('{}')
    calls, scan = [], os.scandir
    monkeypatch.setattr(pu.os, 'scandir', lambda path: (calls.append(path), scan(path))[1])
    for i, sid in enumerate(ids):
        a = {'id': i, 'account_id': 'one', 'provider_session_id': sid}
        assert pu.attempt_path(a, 'codex', now).name.endswith(sid + '.jsonl')
    assert len(calls) == 1


def test_statusline_after_close_cannot_recreate_kv(box, monkeypatch):
    account(box)
    n = attempt(box)
    original = pu.windows
    def finish_during_parse(raw, now):
        profiles.finish_attempt(box, n, 'done')
        return original(raw, now)
    monkeypatch.setattr(pu, 'windows', finish_during_parse)
    pu.observe_statusline(box, 'pane-one', {'five_hour': {'used_percentage': 20}})
    assert box.kv_get('pool_statusline:' + str(n)) is None


def test_pool_inserts_name_columns_for_rollback():
    for path in (Path(profiles.__file__), Path(pu.__file__)):
        for match in re.finditer(r'INSERT(?: OR \w+)? INTO (pool_account|quota_group|runtime_attempt|usage_sample)\s*(.)', path.read_text()):
            assert match[2] == '(', f'{path.name}: positional insert into {match[1]}'
