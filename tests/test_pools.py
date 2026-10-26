"""P3 assignment uses database readings and fake local launches; never real agents."""
import json
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import agents, main, pool_usage, pools, profiles, projects, scheduler
from app.config import POOL_DEFAULTS, settings
from tests.test_profiles import HEADERS, SID, account, box, rows  # shared P1 isolated profiles


MODE = {'account_mode': 'pool', 'pool_id': 'pool-a'}


def seed(db, pcts=(75, 20), *, provider='claude', cap=2):
    pools.save_pool(db, {'id': 'pool-a', 'provider': provider, 'concurrency': cap})
    stamp = time.time()
    for i, pct in enumerate(pcts):
        aid = f'{provider}-{chr(97+i)}'
        account(db, provider, aid)
        pools.member(db, 'pool-a', aid, {})
        with db.lock:
            gid = db.conn.execute('INSERT INTO quota_group(provider,subscription_key) VALUES (?,?)', (provider, f'fixture-{i}')).lastrowid
            db.conn.execute('UPDATE pool_account SET quota_group_id=? WHERE id=?', (gid, aid))
        if pct is not None:
            pool_usage.save(db, gid, {'window_5h_pct': pct, 'weekly_pct': 10,
                'window_5h_reset_at': pool_usage.iso(stamp + 3600 + 60 * i),
                'weekly_reset_at': pool_usage.iso(stamp + 86400)}, 'cache', 1, stamp, node_id='local')


def pick(db, **kw):
    return pools.choose(db, 'claude', pool_id='pool-a', **kw)


def launch(db, **kw):
    # Low-level selector tests model a launch that reached a session binding.
    row = db.add_session(tmux_name='fixture-' + str(time.time_ns()), project='project', repo='root', name='fixture', launcher='claude')
    with profiles.inflight_launch(db) as owner:
        return pools.reserve(db, 'claude', MODE, session_row=row, reservation_owner=owner, **kw)


def test_provider_wall_and_defaults(box):
    seed(box)
    account(box, 'codex', 'codex-a')
    with pytest.raises(projects.BadRequest, match='codex.*claude'):
        pools.member(box, 'pool-a', 'codex-a', {})
    row = pools.list_pools(box)[0]
    assert all(row[k] == v for k, v in POOL_DEFAULTS.items())
    assert row['on_limit_policy'] == 'auto'


def test_soft_threshold_ten_sequential_and_open_cap(box):
    seed(box)
    for _ in range(10):
        attempt, choice = launch(box)
        assert choice['account_id'] == 'claude-b'
        profiles.finish_attempt(box, attempt, 'done')
    for _ in range(2):
        assert launch(box)[1]['account_id'] == 'claude-b'
    result = pick(box)
    assert result['refused']
    for text in ('claude-a', '75%', 'claude-b', '20%', 'concurrency', 'resets'):
        assert text in result['reason']
    with pytest.raises(projects.BadRequest):
        launch(box)
    assert len([r for r in rows(box) if r['ended_at'] is None]) == 2


def test_all_above_soft_names_both_and_earliest_reset(box):
    seed(box, (75, 81))
    result = pick(box)
    assert result['refused'] and result['reason'].startswith('no account under 70%')
    assert result['reason'].index('claude-a') < result['reason'].index('claude-b')
    assert '75%' in result['reason'] and '81%' in result['reason']
    assert pools._reset_text(result['candidates'][0]) in result['reason']


@pytest.mark.parametrize('tie', ['reset', 'recent', 'id'])
def test_ranking_ties(box, tie):
    seed(box, (20, 20))
    if tie == 'reset':
        box.conn.execute('UPDATE usage_sample SET window_5h_reset_at=? WHERE id=2', (pool_usage.iso(time.time()+1800),))
    else:
        box.conn.execute('UPDATE usage_sample SET window_5h_reset_at=?,weekly_reset_at=?',
                         (pool_usage.iso(time.time()+3600), pool_usage.iso(time.time()+86400)))
        if tie == 'recent':
            n = profiles.start_attempt(box, 'claude', 'claude-a')
            profiles.finish_attempt(box, n, 'done')
    assert pick(box)['account_id'] == ('claude-a' if tie == 'id' else 'claude-b')


def test_rank_uses_maximum_of_both_windows(box):
    seed(box, (10, 20))
    box.conn.execute('UPDATE usage_sample SET weekly_pct=60 WHERE id=1')
    assert pick(box)['account_id'] == 'claude-b'


@pytest.mark.parametrize('pcts,expected', [((None, None), 'claude-b'), ((20, None), 'claude-a'), ((None, 75), 'claude-a')])
def test_unknown_fallback(box, pcts, expected):
    seed(box, pcts)
    n = profiles.start_attempt(box, 'claude', 'claude-a')
    profiles.finish_attempt(box, n, 'done')
    result = pick(box)
    assert result['account_id'] == expected
    assert ('usage unknown' in result['reason']) == (pcts != (20, None))
    if pcts == (20, None):
        assert result['candidates'][1]['why_not'] == 'usage unknown; a known account is eligible'


@pytest.mark.parametrize('field,value,model', [('allowed_models', '["haiku"]', 'opus'), ('billing_class', 'api-key', None), ('state', 'needs_login', None)])
def test_hard_constraints(box, field, value, model):
    seed(box, (1, 20))
    box.conn.execute(f'UPDATE pool_account SET {field}=? WHERE id=?', (value, 'claude-a'))
    result = pick(box, requested_model=model)
    assert result['account_id'] == 'claude-b'
    assert not result['candidates'][0]['eligible']
    assert 'model availability unknown' in result['reason']


def test_model_override_in_arguments_is_the_model_checked(box):
    seed(box, (1, 20))
    box.conn.execute('UPDATE pool_account SET allowed_models=? WHERE id=?', ('["haiku"]', 'claude-a'))
    model = pools.requested_model({'model': 'haiku'}, ['--model=opus', '--', 'ignore --model haiku'])
    assert pick(box, requested_model=model)['account_id'] == 'claude-b'


@pytest.mark.parametrize('fallback', [False, True])
def test_concurrent_reservations_one_slot(box, monkeypatch, fallback):
    seed(box, (10, 20 if fallback else 75), cap=1)
    barrier = threading.Barrier(2)
    original = profiles.start_attempt
    local = threading.local()
    def synchronized(*args, **kw):
        if not getattr(local, 'seen', False):
            local.seen = True
            barrier.wait(timeout=5)
        return original(*args, **kw)
    monkeypatch.setattr(profiles, 'start_attempt', synchronized)
    def run():
        try:
            return launch(box)[1]['account_id']
        except projects.BadRequest:
            return 'refused'
    with ThreadPoolExecutor(max_workers=2) as executor:
        result = list(executor.map(lambda _: run(), range(2)))
    assert sorted(result) == ['claude-a', 'claude-b' if fallback else 'refused']
    assert len(rows(box)) == (2 if fallback else 1)


def test_member_overrides_and_shared_quota_account_caps(box):
    seed(box, (75, 20))
    pools.member(box, 'pool-a', 'claude-a', {'soft_pct_5h': 80, 'concurrency': 1})
    pools.member(box, 'pool-a', 'claude-b', {'concurrency': 1})
    assert launch(box)[1]['account_id'] == 'claude-b'
    assert launch(box)[1]['account_id'] == 'claude-a'
    assert pick(box)['refused']
    # P2's headroom counts the whole subscription group; P3 caps each account separately.
    box.conn.execute('DELETE FROM runtime_attempt')
    box.conn.execute('UPDATE pool_account SET quota_group_id=2')
    assert {launch(box)[1]['account_id'] for _ in range(2)} == {'claude-a', 'claude-b'}


@pytest.mark.parametrize('node', ['peer-a', 'auto'])
def test_remote_refused(box, node):
    seed(box)
    assert pools.P5_REASON in pick(box, node=node)['reason']


def test_pinned_metadata_and_default_policy(box):
    seed(box)
    n, result = pools.reserve(box, 'claude', {'account_mode': 'pinned', 'pinned_account_id': 'claude-b'})
    attempt = rows(box)[0]
    assert result['account_id'] == 'claude-b'
    assert attempt['account_mode'] == 'pinned' and attempt['pinned_account_id'] == 'claude-b'
    assert attempt['on_limit_policy'] == 'wait' and json.loads(attempt['assignment']) == result


@pytest.fixture
def api(lite_client, box, projects_dir, monkeypatch, fake_tmux):
    monkeypatch.setattr(main, 'db', box)
    monkeypatch.setattr(settings, 'allowed_users', {'owner'})
    monkeypatch.setattr(settings, 'claude_bin', lambda: 'fake-claude')
    monkeypatch.setattr(agents.get('codex'), 'bin', lambda: 'fake-codex')
    monkeypatch.setattr(agents.get('codex'), '_probe_caps', lambda: ({}, True))
    monkeypatch.setattr(profiles, 'discover_attempt', lambda *a: None)
    (projects_dir / 'project').mkdir()
    return lite_client


def request_body(kind='task', provider='claude'):
    return {'project': 'project', 'repo': 'root', 'agent': provider, **MODE,
            **({'launcher': provider, 'name': 'pool-session'} if kind == 'session' else {'title': 'Pool task', 'prompt': 'Do it'})}


@pytest.mark.parametrize('kind', ['task', 'session'])
@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_dry_run_no_side_effects_and_same_following_launch(api, box, fake_tmux, monkeypatch, kind, provider):
    seed(box, provider=provider)
    body = request_body(kind, provider)
    before = list(box.conn.iterdump())
    def fail(*a, **kw):
        pytest.fail('dry-run must not start or probe anything')
    with monkeypatch.context() as m:
        for key in ('new_session', 'send_line', 'has_session', 'list_sessions'):
            m.setattr(main.tmux, key, fail)
        m.setattr(subprocess, 'Popen', fail)
        m.setattr(box, 'kv_set', fail)
        m.setattr(profiles, 'start_attempt', fail)
        m.setattr(profiles, 'ensure_profile', fail)
        response = api.post('/api/launch/dry-run', json=body, headers=HEADERS)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['will_start_as']['account_id'] == f'{provider}-b', result
    assert list(box.conn.iterdump()) == before
    url = '/api/tasks' if kind == 'task' else '/api/projects/project/repos/root/sessions'
    response = api.post(url, json=body, headers=HEADERS)
    assert response.status_code == 201, response.text
    attempt = rows(box)[0]
    assignment = json.loads(attempt['assignment'])
    assert assignment['account_id'] == result['will_start_as']['account_id']
    assert assignment['reason'] == result['will_start_as']['reason']
    assert assignment['candidates'] == result['candidates']
    assert attempt['pool_id'] == 'pool-a' and attempt['account_mode'] == 'pool'
    assert attempt['on_limit_policy'] == 'auto'
    assert fake_tmux['created'] and 'profile_exec.py' in fake_tmux['sent'][-1][1]
    if kind == 'task':
        task = box.task_get(response.json()['id'])
        assert task['account_mode'] == 'pool' and task['pool_id'] == 'pool-a'


@pytest.mark.parametrize('body', [MODE, {'account_mode': 'pinned', 'pinned_account_id': 'claude-a'}, {'pool_id': 'pool-a'}])
def test_off_refuses_fields_but_dry_run_is_empty(api, monkeypatch, body):
    monkeypatch.setattr(settings, 'pools', False)
    payload = {**request_body(), **body}
    response = api.post('/api/tasks', json=payload, headers=HEADERS)
    assert response.status_code == 400 and 'pools are off' in response.text
    response = api.post('/api/launch/dry-run', json=payload, headers=HEADERS)
    assert response.status_code == 200 and response.json() == {'will_start_as': None, 'refused_reason': None, 'candidates': []}
    assert api.get('/api/pools', headers=HEADERS).status_code == 404


@pytest.mark.parametrize('danger', [{'permission_mode': 'bypassPermissions'}, {'opts': {'sandbox': 'danger-full-access'}}, {'args': '--dangerously-skip-permissions'}, {'devcontainer': True, 'bypass': True}, {'mode': 'bypass'}])
def test_dry_run_and_session_never_bypass(api, box, fake_tmux, danger):
    seed(box)
    body = {**request_body('session'), **danger}
    response = api.post('/api/launch/dry-run', json=body, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()['will_start_as'] is None and response.json()['refused_reason']
    response = api.post('/api/projects/project/repos/root/sessions', json=body, headers=HEADERS)
    assert response.status_code == 400, response.text
    assert not rows(box) and not fake_tmux['created']


def test_crud_provider_wall_validation_delete_guard(api, box):
    account(box, 'claude', 'claude-a')
    account(box, 'codex', 'codex-a')
    response = api.post('/api/pools', json={'id': 'pool-a', 'provider': 'claude', 'name': 'Work'}, headers=HEADERS)
    assert response.status_code == 201
    assert api.put('/api/pools/pool-a/members/codex-a', json={}, headers=HEADERS).status_code == 400
    assert api.put('/api/pools/pool-a/members/claude-a', json={'concurrency': 1}, headers=HEADERS).status_code == 200
    assert api.patch('/api/pools/pool-a', json={'soft_pct_5h': 90}, headers=HEADERS).status_code == 400
    assert api.patch('/api/pools/pool-a', json={'name': 'Team', 'on_limit_policy': 'wait'}, headers=HEADERS).status_code == 200
    n, _ = launch(box)
    assert rows(box)[0]['on_limit_policy'] == 'wait'
    assert api.delete('/api/pools/pool-a', headers=HEADERS).status_code == 409
    profiles.finish_attempt(box, n, 'done')
    assert api.delete('/api/pools/pool-a/members/claude-a', headers=HEADERS).status_code == 200
    assert api.delete('/api/pools/pool-a', headers=HEADERS).status_code == 200
    assert api.get('/api/pools', headers=HEADERS).json() == {'pools': []}


@pytest.mark.parametrize('bad', ['A', 'a..b', '../pool', 'pool/a', '', 'a'*49])
def test_pool_ids_use_p1_validator(api, bad):
    assert api.post('/api/pools', json={'id': bad, 'provider': 'claude'}, headers=HEADERS).status_code == 400


def test_failed_preparation_closes_reservation(api, box, monkeypatch, fake_tmux):
    seed(box)
    def fail(*a, **kw):
        raise projects.BadRequest('fixture preparation failure')
    monkeypatch.setattr(profiles, 'wrap_command', fail)
    response = api.post('/api/tasks', json=request_body(), headers=HEADERS)
    assert response.status_code == 400
    assert rows(box)[0]['end_reason'] == 'launch_failed' and not fake_tmux['created']


def test_pool_surfaces_and_person_only(api, box):
    from app import hooks
    seed(box)
    for method, url, body in [('GET', '/api/pools', None), ('POST', '/api/launch/dry-run', request_body()),
                              ('PATCH', '/api/pools/pool-a', {'name': 'Work'})]:
        response = api.request(method, url, json=body, headers=HEADERS)
        assert response.status_code == 200
        assert not re.search(r'cred|token|auth|secret|email|uuid', json.dumps(response.json()), re.I)
        response = api.request(method, url, json=body, headers={hooks.TOKEN_HEADER: hooks.ensure_token()})
        assert response.status_code == 403
    box.conn.execute('UPDATE pool_account SET label=? WHERE id=?', ('private' + chr(64) + 'invalid.test', 'claude-b'))
    response = api.post('/api/launch/dry-run', json=request_body(), headers=HEADERS)
    assert 'private' + chr(64) not in response.text


def test_scheduler_reserves_and_closes_attempt(box, monkeypatch, tmp_path):
    seed(box)
    monkeypatch.setattr(projects, 'repo_path', lambda *a: tmp_path)
    seen = []
    def run(db, job, rid, binding=None):
        seen.append((job, binding, rows(db)))
        return {'status': 'ok'}
    monkeypatch.setattr(scheduler, '_run_job', run)
    summary = scheduler.run_job(box, {'id': 1, 'project': 'project', 'repo': 'root', 'permission_mode': 'acceptEdits', 'opts': MODE}, 1)
    assert summary['status'] == 'ok'
    assert seen[0][1]['account_id'] == 'claude-b'
    assert seen[0][2][0]['ended_at'] is None
    assert rows(box)[0]['ended_at'] is not None and rows(box)[0]['pool_id'] == 'pool-a'


def test_state_adds_only_nested_compact_list(api, box, monkeypatch):
    seed(box)
    enabled = api.get('/api/state', headers=HEADERS).json()
    assert enabled['pools']['pools'] == [{'id': 'pool-a', 'provider': 'claude', 'name': 'pool-a'}]
    monkeypatch.setattr(settings, 'pools', False)
    disabled = api.get('/api/state', headers=HEADERS).json()
    assert disabled['pools'] is None and set(disabled) == set(enabled)


@pytest.mark.parametrize('mode', ['pool', 'pinned'])
def test_backlog_binding_survives_dispatch(api, box, mode):
    seed(box)
    binding = MODE if mode == 'pool' else {'account_mode': 'pinned', 'account_id': 'claude-b'}
    body = {k: v for k, v in request_body().items() if k not in pools.BINDING_FIELDS}
    response = api.post('/api/tasks', json={**body, **binding, 'when': 'later'}, headers=HEADERS)
    assert response.status_code == 201, response.text
    tid = response.json()['id']
    task = box.task_get(tid)
    assert task['account_mode'] == mode
    assert task['on_limit_policy'] == ('auto' if mode == 'pool' else 'wait')
    if mode == 'pinned':
        assert task['pinned_account_id'] == 'claude-b'
    assert not rows(box)
    response = api.post(f'/api/tasks/{tid}/dispatch', json={}, headers=HEADERS)
    assert response.status_code == 200, response.text
    assert rows(box)[0]['account_mode'] == mode and rows(box)[0]['account_id'] == 'claude-b'
    assert rows(box)[0]['task_id'] == str(tid)


def test_no_account_switch_by_dispatching_pool_task_into_session(api, box):
    seed(box)
    response = api.post('/api/tasks', json={**request_body(), 'when': 'later'}, headers=HEADERS)
    tid = response.json()['id']
    response = api.post(f'/api/tasks/{tid}/dispatch', json={'session': 'project--root--other'}, headers=HEADERS)
    assert response.status_code == 400 and 'new process' in response.text


@pytest.mark.parametrize('kind', ['task', 'session'])
def test_api_p5_reason_before_process(api, box, fake_tmux, kind):
    seed(box)
    body = {**request_body(kind), 'node': 'peer-a'}
    response = api.post('/api/launch/dry-run', json=body, headers=HEADERS)
    assert pools.P5_REASON in response.json()['refused_reason']
    url = '/api/tasks' if kind == 'task' else '/api/projects/project/repos/root/sessions'
    response = api.post(url, json=body, headers=HEADERS)
    assert response.status_code == 400 and pools.P5_REASON in response.text
    assert not rows(box) and not fake_tmux['created']


def test_concurrent_session_launches_reserve_before_tmux(api, box, monkeypatch, fake_tmux):
    seed(box, (10, 75), cap=1)
    barrier = threading.Barrier(2)
    original = profiles.start_attempt
    def synchronize(*a, **kw):
        barrier.wait(timeout=5)
        return original(*a, **kw)
    monkeypatch.setattr(profiles, 'start_attempt', synchronize)
    def run(i):
        try:
            return main._start_session_row(f'project--root--s{i}', 'project', 'root', f's{i}', 'claude', '/tmp',
                cmd_line='claude --model opus', claude_session_id=SID, opts={**MODE, 'model': 'opus'})[0]
        except projects.BadRequest:
            return 'refused'
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, range(2)))
    assert results.count('refused') == 1
    assert len(fake_tmux['created']) == 1 and len(rows(box)) == 1


def test_scheduler_stored_body_and_model(api, box, projects_dir, monkeypatch):
    seed(box)
    (projects_dir / 'project' / '.git').mkdir()  # repo recognition only; no git command runs
    response = api.post('/api/projects/project/repos/root/jobs', json={
        'name': 'Pool job', 'prompt': 'Do it', 'model': 'opus', 'permission_mode': 'acceptEdits', **MODE}, headers=HEADERS)
    assert response.status_code == 201, response.text
    job = box.job_get(response.json()['id'])
    assert scheduler.job_opts(job) == {'model': 'opus', **MODE, 'on_limit_policy': 'auto', 'account_id': pools.ROLLBACK_POOL_ACCOUNT}
    seen = []
    monkeypatch.setattr(scheduler.tasks, 'ensure_excluded', lambda *a: None)
    monkeypatch.setattr(scheduler.tasks, 'unique_slug', lambda *a: 'job')
    monkeypatch.setattr(scheduler, '_finish', lambda db, job, rid, summary, *a: summary)
    monkeypatch.setattr(scheduler, '_run_agent', lambda cmd, **kw: seen.append((cmd, kw)) or subprocess.CompletedProcess(cmd, 0, '{"result":"ok"}', ''))
    result = scheduler.run_job(box, job, 1)
    assert result['status'] == 'ok'
    command, kw = seen[0]
    assert command[command.index('--model')+1] == 'opus'
    assert command[command.index('--permission-mode')+1] == 'acceptEdits'
    assert 'claude-b' in kw['env']['CLAUDE_CONFIG_DIR']
    assert rows(box)[0]['ended_at'] is not None


def test_pool_scheduler_skips_legacy_login_and_quota(api, box, monkeypatch):
    seed(box)
    from app.db import now
    jid = box.job_add(project='project', repo='root', name='Pool job', prompt='Do it', enabled=1,
                     permission_mode='acceptEdits', opts=MODE, next_run_at=now())
    def fail(*a, **kw):
        pytest.fail('pool jobs must use their accounts, not the legacy login or quota')
    monkeypatch.setattr(scheduler, 'login_blocked', fail)
    monkeypatch.setattr(scheduler, 'quota_blocked', fail)
    monkeypatch.setattr(scheduler.Worker, '_run', lambda *a: None)
    worker = scheduler.Worker(box)
    started = worker.tick()
    assert len(started) == 1
    assert box.job_get(jid)['enabled'] == 0
    for thread in worker.running.values():
        thread.join(2)


def test_no_assignment_lock_across_process_io(api, box, monkeypatch):
    seed(box)
    original = main.tmux.new_session
    def check(*a, **kw):
        assert not pools.assignment_lock._is_owned()
        assert not profiles.account_lock('claude-b')._is_owned()
        return original(*a, **kw)
    monkeypatch.setattr(main.tmux, 'new_session', check)
    response = api.post('/api/tasks', json=request_body(), headers=HEADERS)
    assert response.status_code == 201, response.text


def test_usage_read_does_not_follow_group_identity(api, box, monkeypatch):
    seed(box)
    for method in ('ensure_groups', 'identity', 'app_server'):
        monkeypatch.setattr(pool_usage, method, lambda *a, **kw: pytest.fail('assignment read identity or performed a probe'))
    assert pick(box)['account_id'] == 'claude-b'


def test_candidate_cap_does_not_make_unknown_preferable_to_known_eligible(box):
    seed(box, (10, None), cap=1)
    n, result = launch(box)
    assert result['account_id'] == 'claude-a'
    assert launch(box)[1]['account_id'] == 'claude-b'
    assert pick(box)['refused']


def test_delete_guard_includes_task_reference(box):
    seed(box)
    tid = box.task_add(project='p', repo='r', slug='task', title='Task', prompt='Do it', pool_id='pool-a')
    attempt = profiles.start_attempt(box, 'claude', 'claude-b', task_id=tid)
    with pytest.raises(projects.Conflict, match='open attempt'):
        pools.delete_pool(box, 'pool-a')
    profiles.finish_attempt(box, attempt, 'done')
    assert pools.delete_pool(box, 'pool-a') == {'deleted': 'pool-a'}


def test_scheduler_pool_model_keeps_billing_acknowledgement(box, monkeypatch):
    monkeypatch.setattr(scheduler, 'fable_models', lambda parts, env: ['fable'] if 'fable' in parts else [])
    job = {'opts': {**MODE, 'model': 'fable'}, 'max_budget_usd': 1}
    assert scheduler.fable_hold(job, {}) == scheduler.FABLE_HELD
    job['opts']['fable_ack'] = {'at': '2026-01-01', 'models': ['fable'], 'cap': 1}
    assert scheduler.fable_hold(job, {}) is None


@pytest.mark.parametrize('field', ['soft_pct_5h', 'soft_pct_weekly', 'hard_pct_5h', 'hard_pct_weekly', 'concurrency'])
@pytest.mark.parametrize('value', [-1, 1001, True, '20'])
def test_threshold_types_and_bounds(box, field, value):
    with pytest.raises(projects.BadRequest):
        pools.save_pool(box, {'id': 'pool-a', 'provider': 'claude', field: value})


def test_soft_boundary_and_weekly_threshold(box):
    seed(box, (70, 20))
    box.conn.execute('UPDATE usage_sample SET weekly_pct=80 WHERE id=2')
    result = pick(box)
    assert result['refused'] and all(not c['eligible'] for c in result['candidates'])


def test_dry_refusal_lists_all_candidates_without_writes(api, box, monkeypatch):
    seed(box, (75, 81))
    before = list(box.conn.iterdump())
    response = api.post('/api/launch/dry-run', json=request_body(), headers=HEADERS)
    result = response.json()
    assert result['will_start_as'] is None and result['refused_reason']
    assert len(result['candidates']) == 2 and 'claude-a' in result['refused_reason'] and 'claude-b' in result['refused_reason']
    assert list(box.conn.iterdump()) == before


def test_queued_pool_task_inherits_provider(api, box):
    seed(box, provider='codex')
    tid = box.task_add(project='project', repo='root', slug='parent', title='Parent', prompt='First', agent='codex')
    body = request_body()
    body.pop('agent')
    response = api.post('/api/tasks', json={**body, 'after_task_id': tid}, headers=HEADERS)
    assert response.status_code == 201, response.text
    task = box.task_get(response.json()['id'])
    assert task['agent'] == 'codex' and task['account_mode'] == 'pool'


def test_profile_path_checks_are_outside_reservation_locks(box, monkeypatch):
    seed(box)
    original = profiles.profile_dir
    def checked(provider, aid):
        assert not profiles.account_lock(aid)._is_owned()
        assert not pools.assignment_lock._is_owned()
        return original(provider, aid)
    monkeypatch.setattr(profiles, 'profile_dir', checked)
    assert launch(box)[1]['account_id'] == 'claude-b'


def test_explanation_uses_next_reset_not_an_expired_window(box):
    seed(box, (75, 81))
    box.conn.execute('UPDATE usage_sample SET window_5h_reset_at=?,weekly_pct=90 WHERE id=1', (pool_usage.iso(time.time()-1),))
    result = pick(box)
    assert result['refused']
    # The other account's next 5-hour reset precedes this account's next weekly reset.
    assert result['reason'].index('claude-b') < result['reason'].index('claude-a')
