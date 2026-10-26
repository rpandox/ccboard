"""Round-one P3a regressions: old scheduler, interrupted reservations, and continuation."""
import importlib.util
import json
from pathlib import Path
import time

import pytest

from app import agents, main, pools, profiles, projects, recover, scheduler
from app.config import settings
from tests.test_pools import api, seed, request_body, MODE
from tests.test_profiles import box, rows, HEADERS, SID


@pytest.mark.parametrize('mode', ['pinned', 'pool'])
@pytest.mark.parametrize('batch', [False, True])
def test_rollback_scheduler_guard(api, box, projects_dir, monkeypatch, mode, batch):
    (projects_dir / 'project' / '.git').mkdir()
    binding = MODE if mode == 'pool' else {'account_mode': 'pinned', 'pinned_account_id': 'claude-a'}
    seed(box, (10, 20))
    monkeypatch.setattr(main, 'sched', None)
    payload = {'name': 'Rollback', 'prompt': 'Do it', **binding}
    url = '/api/batch' if batch else '/api/projects/project/repos/root/jobs'
    if batch:
        payload['repos'] = ['project/root']
    response = api.post(url, json=payload, headers=HEADERS)
    assert response.status_code in (200, 201), response.text
    jid = response.json()['jobs'][0] if batch else response.json()['id']
    job = box.job_get(jid)
    spec = importlib.util.spec_from_file_location('app._rollback_fixture', Path(__file__).parent / 'fixtures/pools_rollback_v05115.py')
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    monkeypatch.setattr(profiles, 'require_account', old.require_account)
    calls = []
    old._run_job = lambda db, job, rid, binding=None: calls.append(binding) or {'status': 'ok'}
    if mode == 'pool':
        with pytest.raises(projects.BadRequest, match='invalid account id'):
            old.run_job(box, job, 1)
        assert calls == []
    else:
        old.run_job(box, job, 1)
        assert calls[0] and calls[0]['account_id'] == 'claude-a'


def age(box, attempt, seconds=121):
    from app.pool_usage import iso
    box.conn.execute('UPDATE runtime_attempt SET started_at=? WHERE id=?', (iso(time.time() - seconds), attempt))


@pytest.mark.parametrize('entry', ['startup', 'selection'])
def test_abandoned_reservation_reclaimed(box, entry):
    seed(box, (10,), cap=1)
    attempt, _ = pools.reserve(box, 'claude', MODE)
    age(box, attempt)
    if entry == 'startup':
        profiles.reconcile_attempts(box)
    else:
        pools.reserve(box, 'claude', MODE)
    assert rows(box)[0]['end_reason'] == 'reservation_abandoned'
    assert rows(box)[0]['reservation_owner']


def test_dry_run_ignores_abandoned_reservation_without_writes(api, box):
    seed(box, (10,), cap=1)
    attempt, _ = pools.reserve(box, 'claude', MODE)
    age(box, attempt)
    before = list(box.conn.iterdump())
    result = api.post('/api/launch/dry-run', json=request_body(), headers=HEADERS).json()
    assert result['will_start_as']['account_id'] == 'claude-a'
    assert list(box.conn.iterdump()) == before
    assert api.post('/api/tasks', json=request_body(), headers=HEADERS).status_code == 201
    assert rows(box)[0]['end_reason'] == 'reservation_abandoned'


@pytest.mark.parametrize('owner', ['young', 'task', 'session', 'run'])
def test_live_reservation_not_abandoned(box, fake_tmux, owner):
    seed(box, (10,), cap=1)
    fields = {}
    if owner == 'task':
        fields['task_id'] = box.task_add(project='project', repo='root', slug='a', title='a', prompt='a')
    elif owner == 'run':
        jid = box.job_add(project='project', repo='root', name='a', prompt='a')
        fields['task_id'] = f'run:{box.run_start(jid)}'
    with profiles.inflight_launch(box) as launch_owner:
        attempt, _ = pools.reserve(box, 'claude', MODE, reservation_owner=launch_owner, **fields)
        if owner == 'session':
            name = main.tmux.new_session('ccb-project-root-live', '/tmp')
            row = box.add_session(tmux_name=name, project='project', repo='root', name='live', launcher='claude')
            box.kv_set(f'runtime_session:{row}', {'attempt': attempt})
        if owner != 'young':
            age(box, attempt)
        profiles.reconcile_attempts(box)
        assert rows(box)[0]['ended_at'] is None
        assert rows(box)[0]['reservation_owner']


@pytest.mark.parametrize('binding', [MODE, {'account_mode': 'pinned', 'pinned_account_id': 'codex-a'}])
def test_codex_hook_trust_real_discovery(api, box, fake_tmux, monkeypatch, fake_codex, binding):
    from app.agents.codex import CodexAgent
    ag = agents.get('codex')
    monkeypatch.setattr(ag, 'bin', lambda: str(fake_codex))
    monkeypatch.setattr(ag, '_probe_caps', CodexAgent._probe_caps.__get__(ag))
    monkeypatch.setattr(settings, 'codex_hook_trust', 'bypass')
    assert ag.capabilities()['hook_trust_flag'] is True
    seed(box, (10,), provider='codex')
    body = {**request_body('session', 'codex'), **binding}
    if binding['account_mode'] == 'pinned':
        body.pop('pool_id')
    dry = api.post('/api/launch/dry-run', json=body, headers=HEADERS).json()
    assert dry['will_start_as']['account_id'] == 'codex-a'
    response = api.post('/api/projects/project/repos/root/sessions', json=body, headers=HEADERS)
    assert response.status_code == 201, response.text
    assert '--dangerously-bypass-hook-trust' in fake_tmux['sent'][-1][1]


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_recovery_pins_recorded_pool_account(api, box, fake_tmux, monkeypatch, provider):
    seed(box, (10, 20), provider=provider)
    body = request_body('session', provider)
    assert api.post('/api/projects/project/repos/root/sessions', json=body, headers=HEADERS).status_code == 201
    old = next(iter(box.open_rows().values()))
    box.conn.execute('UPDATE sessions SET claude_session_id=? WHERE id=?', (SID, old['id']))
    fake_tmux['sessions'].clear()
    box.conn.execute('UPDATE usage_sample SET window_5h_pct=1 WHERE id=2')
    monkeypatch.setattr(settings, 'recover', True)
    result = recover.run(box, main._start_session)
    assert result['recovered'] and not result['skipped'], result
    attempt = rows(box)[-1]
    assert attempt['account_id'] == f'{provider}-a' and attempt['account_mode'] == 'pinned'


@pytest.mark.parametrize('ineligible', [False, True])
def test_pool_task_reopen_pins_recorded_account(api, box, fake_tmux, monkeypatch, projects_dir, ineligible):
    seed(box, (10, 20))
    response = api.post('/api/tasks', json=request_body(), headers=HEADERS)
    assert response.status_code == 201, response.text
    tid = response.json()['id']
    task = box.task_get(tid)
    main._end_session(task['tmux_name'], 'done')
    wt = projects_dir / 'retained-worktree'
    wt.mkdir()
    box.task_update(tid, phase='done', mode='worktree', worktree=str(wt), claude_session_id=SID)
    monkeypatch.setattr(main, '_task_transcript', lambda *a: wt / 'transcript')
    monkeypatch.setattr(main, '_stage_transcript', lambda *a: None)
    box.conn.execute('UPDATE usage_sample SET window_5h_pct=? WHERE id=1', (75 if ineligible else 30,))
    response = api.post(f'/api/tasks/{tid}/reopen', json={}, headers=HEADERS)
    if ineligible:
        assert response.status_code == 400 and 'resuming on another account comes in P4' in response.text
        assert len(rows(box)) == 1
    else:
        assert response.status_code == 200, response.text
        assert rows(box)[-1]['account_id'] == 'claude-a'
        assert rows(box)[-1]['account_mode'] == 'pinned'


@pytest.mark.parametrize('kind', ['task', 'session'])
@pytest.mark.parametrize('constraint', ['usage', 'cap'])
@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_legacy_dry_run_matches_launch(api, box, kind, constraint, provider):
    seed(box, (90 if constraint == 'usage' else 10,), provider=provider)
    if constraint == 'cap':
        for _ in range(2):
            profiles.start_attempt(box, provider, f'{provider}-a')
    body = {k: v for k, v in request_body(kind, provider).items() if k not in MODE}
    body['account_id'] = f'{provider}-a'
    result = api.post('/api/launch/dry-run', json=body, headers=HEADERS).json()
    assert result['will_start_as']['account_id'] == f'{provider}-a'
    assert 'pinned' in result['will_start_as']['reason']
    url = '/api/tasks' if kind == 'task' else '/api/projects/project/repos/root/sessions'
    assert api.post(url, json=body, headers=HEADERS).status_code == 201


@pytest.mark.parametrize('opts', [{'mode': 'bypass'}, {'mode': 'bypass', 'permission_mode': 'default'}, {'mode': 'read-only', 'permission_mode': 'bypassPermissions'}])
@pytest.mark.parametrize('provider', ['claude', 'codex'])
@pytest.mark.parametrize('kind', ['task', 'session'])
def test_nested_mode_dry_run_matches_launch(api, box, opts, provider, kind):
    seed(box, provider=provider)
    body = {**request_body(kind, provider), 'opts': opts}
    dry = api.post('/api/launch/dry-run', json=body, headers=HEADERS).json()
    url = '/api/tasks' if kind == 'task' else '/api/projects/project/repos/root/sessions'
    response = api.post(url, json=body, headers=HEADERS)
    if opts['mode'] == 'read-only':
        assert dry['will_start_as'] and response.status_code == 201, (dry, response.text)
    else:
        assert dry['refused_reason'] and response.status_code == 400, (dry, response.text)


@pytest.mark.parametrize('ineligible', [False, True])
def test_pool_run_resume_pins_recorded_account(api, box, fake_tmux, projects_dir, ineligible):
    seed(box, (75 if ineligible else 30, 10))
    wt = projects_dir / 'retained-run'
    wt.mkdir()
    tid = box.task_add(project='project', repo='root', slug='run', title='Run', prompt='Do it',
                       tmux_name='project--root--run', mode='worktree', worktree=str(wt), phase='done', spec=MODE)
    aid = profiles.start_attempt(box, 'claude', 'claude-a', task_id=tid, account_mode='pool', pool_id='pool-a')
    profiles.finish_attempt(box, aid, 'done')
    jid = box.job_add(project='project', repo='root', name='Run', prompt='Do it')
    rid = box.run_start(jid)
    box.run_finish(rid, status='ok', task_id=tid, session_id=SID)
    response = api.post(f'/api/runs/{rid}/resume', json={}, headers=HEADERS)
    if ineligible:
        assert response.status_code == 400 and 'resuming on another account comes in P4' in response.text
        assert not fake_tmux['created']
    else:
        assert response.status_code == 200, response.text
        assert rows(box)[-1]['account_id'] == 'claude-a' and rows(box)[-1]['account_mode'] == 'pinned'
        assert 'claude-a' in fake_tmux['sent'][-1][1]


@pytest.mark.parametrize('args', [
    ['--dangerously-skip-permissions'], ['--allow-dangerously-skip-permissions'],
    ['--permission-mode=bypassPermissions'], ['--permission-mode', 'bypassPermissions'],
    ['--sandbox=danger-full-access'], ['-s', 'danger-full-access'],
    ['--dangerously-bypass-approvals-and-sandbox'], ['--yolo'],
])
def test_exact_permission_bypass_values(args):
    assert main._bypass_requested(args)


@pytest.mark.parametrize('args', [
    ['--dangerously-bypass-hook-trust'], ['--some-dangerously-named-option'],
    ['--append-system-prompt', 'avoid bypassPermissions today'],
])
def test_unrelated_text_is_not_permission_bypass(args):
    assert main._bypass_requested(args) is None


def test_deleted_task_reference_is_abandoned(box):
    seed(box, (10,), cap=1)
    aid, _ = pools.reserve(box, 'claude', MODE, task_id=12345)
    age(box, aid)
    profiles.reconcile_attempts(box)
    assert rows(box)[0]['end_reason'] == 'reservation_abandoned'


def test_scheduler_tick_accepts_persisted_pool_marker(api, box, projects_dir, monkeypatch):
    (projects_dir / 'project' / '.git').mkdir()
    seed(box, (10,))
    response = api.post('/api/projects/project/repos/root/jobs', json={
        'name': 'Scheduled', 'prompt': 'Do it', 'run_now': True, **MODE}, headers=HEADERS)
    assert response.status_code == 201
    monkeypatch.setattr(scheduler.Worker, '_run', lambda *a: None)
    worker = scheduler.Worker(box)
    assert len(worker.tick()) == 1
    for thread in worker.running.values():
        thread.join(2)


@pytest.mark.parametrize('provider', ['claude', 'codex'])
@pytest.mark.parametrize('kind', ['task', 'session'])
def test_nested_mode_uses_provider_option_precedence(api, box, provider, kind):
    seed(box, provider=provider)
    body = {**request_body(kind, provider), 'permission_mode': 'bypassPermissions', 'opts': {'mode': 'read-only'}}
    dry = api.post('/api/launch/dry-run', json=body, headers=HEADERS).json()
    url = '/api/tasks' if kind == 'task' else '/api/projects/project/repos/root/sessions'
    response = api.post(url, json=body, headers=HEADERS)
    # Codex merges opts into its controls; Claude's controls remain flat.
    if provider == 'codex':
        assert dry['will_start_as'] and response.status_code == 201, (dry, response.text)
    else:
        assert dry['refused_reason'] and response.status_code == 400, (dry, response.text)


@pytest.mark.parametrize('mode', ['pool', 'pinned'])
def test_remote_dry_run_refuses_p5_before_local_repo_lookup(api, box, monkeypatch, mode):
    seed(box, (10, 20))
    body = request_body()
    if mode == 'pinned':
        body.pop('pool_id', None)
        body.update(account_mode='pinned', pinned_account_id='claude-a')
    body.update(node='remote-box', project='only-on-peer', repo='peer-repo')
    monkeypatch.setattr(projects, 'repo_path', lambda *args: pytest.fail('remote repository inspected locally'))
    before = list(box.conn.iterdump())
    response = api.post('/api/launch/dry-run', json=body, headers=HEADERS)
    assert response.status_code == 200
    data = response.json()
    assert data['will_start_as'] is None
    assert 'account grants (P5)' in data['refused_reason']
    assert data['candidates'] and all(not c['eligible'] for c in data['candidates'])
    assert list(box.conn.iterdump()) == before


@pytest.mark.parametrize('backlog', [False, True])
def test_preparing_launch_keeps_reservation_past_two_minutes(api, box, monkeypatch, backlog):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    seed(box, (10,), cap=1)
    entered, resume = threading.Event(), threading.Event()
    wrap = profiles.wrap_command
    tid = box.task_add(project='project', repo='root', slug='waiting', title='Waiting', prompt='Do it') if backlog else None
    def paused(*args, **kw):
        entered.set()
        assert resume.wait(10)
        return wrap(*args, **kw)
    monkeypatch.setattr(profiles, 'wrap_command', paused)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(main._start_session_row, 'project--root--slow', 'project', 'root', 'slow', 'claude', '/tmp',
                                 cmd_line='claude', opts=MODE, task_id=tid)
        try:
            assert entered.wait(5)
            age(box, rows(box)[0]['id'], 300)
            profiles.reconcile_attempts(box)
            assert rows(box)[0]['ended_at'] is None
            before = list(box.conn.iterdump())
            dry = api.post('/api/launch/dry-run', json=request_body('session'), headers=HEADERS).json()
            assert dry['refused_reason'] and dry['will_start_as'] is None
            assert list(box.conn.iterdump()) == before
            with pytest.raises(projects.BadRequest, match='concurrency'):
                pools.reserve(box, 'claude', MODE)
        finally:
            resume.set()
            future.result(timeout=5)
    assert not box._inflight_launches


@pytest.mark.parametrize('owner', ['session', 'task', 'run'])
@pytest.mark.parametrize('seconds', [0, 300])
def test_startup_abandons_every_unbound_reservation(box, owner, seconds):
    from app.db import DB
    seed(box, (10,), cap=1)
    fields = {}
    if owner == 'task':
        fields['task_id'] = box.task_add(project='project', repo='root', slug='backlog', title='Backlog', prompt='Do it')
    elif owner == 'run':
        fields['task_id'] = 'run:' + str(box.run_start(box.job_add(project='project', repo='root', name='Job', prompt='Do it')))
    aid, _ = pools.reserve(box, 'claude', MODE, **fields)
    age(box, aid, seconds)
    # A fresh connection represents a new board process, with no live launch owners.
    path = box.conn.execute('PRAGMA database_list').fetchone()[2]
    restarted = DB(Path(path))
    try:
        profiles.reconcile_attempts(restarted)
        assert rows(restarted)[0]['end_reason'] == 'reservation_abandoned'
        assert pools.reserve(restarted, 'claude', MODE)[1]['account_id'] == 'claude-a'
    finally:
        restarted.close()


@pytest.mark.parametrize('args,allowed', [
    ('--append-system-prompt "avoid dangerously deleting files"', True),
    ('--append-system-prompt "avoid bypassPermissions today"', True),
    ('--append-system-prompt "never use --dangerously-skip-permissions"', True),
    ('--dangerously-skip-permissions', False),
    ('--allow-dangerously-skip-permissions', False),
    ('--permission-mode=bypassPermissions', False),
    ('--permission-mode bypassPermissions', False),
    ('--dangerously-bypass-approvals-and-sandbox', False),
    ('--yolo', False),
])
@pytest.mark.parametrize('kind', ['session', 'task'])
def test_quoted_danger_text_dry_run_and_launch_agree(api, box, args, allowed, kind):
    seed(box, (10,))
    body = {**request_body(kind), 'args': args}
    dry = api.post('/api/launch/dry-run', json=body, headers=HEADERS).json()
    response = api.post('/api/tasks' if kind == 'task' else '/api/projects/project/repos/root/sessions', json=body, headers=HEADERS)
    assert bool(dry['will_start_as']) == allowed, dry
    assert (response.status_code == 201) == allowed, response.text


@pytest.mark.parametrize('failed', [False, True])
def test_finished_unbound_backlog_owner_is_abandoned(box, failed):
    seed(box, (10,), cap=1)
    tid = box.task_add(project='project', repo='root', slug='backlog', title='Backlog', prompt='Do it')
    class Interrupted(BaseException):
        pass
    try:
        with profiles.inflight_launch(box) as owner:
            aid, _ = pools.reserve(box, 'claude', MODE, task_id=tid, reservation_owner=owner)
            age(box, aid, 300)
            profiles.reconcile_attempts(box)
            assert rows(box)[0]['ended_at'] is None
            if failed:
                raise Interrupted()
    except Interrupted:
        pass
    assert box.task_get(tid)
    profiles.reconcile_attempts(box)
    assert rows(box)[0]['end_reason'] == 'reservation_abandoned'
    assert not box._inflight_launches


def test_headless_preparation_has_live_owner(box, projects_dir, monkeypatch):
    seed(box, (10,), cap=1)
    (projects_dir / 'project').mkdir(exist_ok=True)
    jid = box.job_add(project='project', repo='root', name='Job', prompt='Do it', opts=MODE)
    rid = box.run_start(jid)
    def preparing(db, job, run_id, binding):
        age(db, binding['attempt'], 300)
        profiles.reconcile_attempts(db)
        assert rows(db)[0]['ended_at'] is None
        assert pools.choose(db, 'claude', pool_id='pool-a')['refused']
        return {'status': 'ok'}
    monkeypatch.setattr(scheduler, '_run_job', preparing)
    assert scheduler.run_job(box, box.job_get(jid), rid) == {'status': 'ok'}
    assert not box._inflight_launches
