"""Phase 2: every device operation goes through the scripted fake runner."""
import hashlib
import shlex
import subprocess

import pytest

from app import bare, main, nodes, nodes_discovery
from app.config import settings


@pytest.fixture
def device(lite_client, bare_runner, monkeypatch):
    owner = next(iter(settings.allowed_users))
    peer = {'ID': 'device-1', 'DNSName': 'example.ts.net', 'HostName': 'Device', 'UserID': 1,
            'Online': True, 'OS': 'linux', 'TailscaleIPs': ['100.64.0.2']}
    status = {'Self': {'ID': 'board', 'UserID': 1}, 'CurrentTailnet': {'MagicDNSSuffix': 'ts.net'},
              'User': {'1': {'ID': 1, 'LoginName': owner}}, 'Peer': {'one': peer}}
    monkeypatch.setattr(nodes_discovery, 'read_tailscale', lambda fresh=False: (status, {'ok': True}))
    monkeypatch.setattr(nodes, 'node_id', lambda: 'n_0123456789abcdef')
    saved = {'ts_id': 'device-1', 'name': 'Device', 'owner': owner, 'user': 'ccagent', 'folder': 'my projects',
             'check': {'ok': True, 'repos': ['repo one'], 'rows': [{'key': a, 'ok': True} for a in ('claude', 'codex')]}}
    main.db.kv_set(bare.PREFIX + 'device-1', saved)
    timers = []

    class Timer:
        def __init__(self, delay, fn, args):
            timers.append((delay, fn, args))
        def start(self):
            pass

    monkeypatch.setattr(bare.threading, 'Timer', Timer)
    return {'Tailscale-User-Login': owner, 'X-CCBoard': '1'}, peer, timers


def start(client, fake, headers, agent='claude', **fields):
    fake.answers.extend([(0, '1001', ''), (0, 'users staff', ''), (0, '', '')])
    return client.post('/api/bare/device-1/sessions', headers=headers, json={'repo': 'repo one', 'agent': agent, **fields})


@pytest.mark.parametrize('agent,fields,line', [
    ('claude', {}, 'claude --permission-mode default'),
    ('claude', {'mode': 'manual'}, 'claude --permission-mode manual'),
    ('claude', {'mode': 'acceptEdits', 'model': 'opus', 'effort': 'high'}, 'claude --permission-mode acceptEdits --model opus --effort high'),
    ('claude', {'mode': 'plan'}, 'claude --permission-mode plan'),
    ('codex', {}, 'codex -s workspace-write -a on-request -c check_for_update_on_startup=false'),
    ('codex', {'mode': 'read-only', 'model': 'test-model', 'effort': 'high'},
     'codex -s read-only -a on-request -c check_for_update_on_startup=false -m test-model -c \'model_reasoning_effort="high"\''),
    ('shell', {}, 'sh -l'),
])
def test_exact_start_argv(lite_client, bare_runner, device, agent, fields, line):
    headers, _, _ = device
    r = start(lite_client, bare_runner, headers, agent, **fields)
    assert r.status_code == 200, r.text
    row = r.json()['session']
    assert row['host'] == 'device-1' and row['state'] == 'alive' and row['cmd'] == line
    sock = 'ccboard-bare-' + hashlib.sha256(b'n_0123456789abcdef').hexdigest()[:8]
    inner = shlex.join(['sh', '-c', bare.START, 'bare-start', 'my projects/repo one', *shlex.split(line)])
    remote = shlex.join(['tmux', '-L', sock, 'new-session', '-d', '-s', row['tmux_name'], inner])
    directory = settings.data_dir / 'bare-ssh'
    key = hashlib.sha256(b'example.ts.netccagent').hexdigest()[:16]
    opts = ['BatchMode=yes', 'ConnectTimeout=8', 'StrictHostKeyChecking=accept-new',
            'UserKnownHostsFile=' + shlex.quote(str(directory / 'known_hosts')), 'GlobalKnownHostsFile=/dev/null',
            'ControlMaster=auto', 'ControlPersist=30', 'ControlPath=' + shlex.quote(str(directory / key)),
            'ForwardAgent=no', 'ClearAllForwardings=yes', 'RequestTTY=no', 'PubkeyAuthentication=no',
            'PasswordAuthentication=no', 'KbdInteractiveAuthentication=no', 'HostName=100.64.0.2', 'HostKeyAlias=example.ts.net']
    prefix = ['ssh', '-F', '/dev/null', *[p for o in opts for p in ('-o', o)], 'ccagent@example.ts.net']
    assert bare_runner.calls == [(prefix + ['id -u'], None), (prefix + ['id -Gn'], None), (prefix + [remote], None)]
    audit = main.db.node_audit_list()[0]
    assert (audit['action'], audit['user'], audit['status'], audit['detail']) == ('bare_start', headers['Tailscale-User-Login'], 'ok', None)


@pytest.mark.parametrize('agent,mode', [('claude', None), ('claude', ''), ('claude', 'auto'), ('claude', 'bypass'),
                                       ('codex', None), ('codex', ''), ('codex', 'auto'), ('codex', 'full-access'), ('shell', 'manual')])
def test_modes_refused_before_any_argv(lite_client, bare_runner, device, monkeypatch, agent, mode):
    monkeypatch.setattr(bare, 'argv', lambda *a, **k: pytest.fail('argv constructed before refusal'))
    r = lite_client.post('/api/bare/device-1/sessions', headers=device[0], json={'repo': 'repo one', 'agent': agent, 'mode': mode})
    assert r.status_code == 400
    with pytest.raises(ValueError):
        bare.launch_line(agent, mode)
    assert not bare_runner.calls


@pytest.mark.parametrize('extra', [{'prompt': 'private prompt'}, {'args': 'anything'}, {'worktree': True}])
def test_extra_fields_never_sent_or_audited(lite_client, bare_runner, device, extra):
    r = lite_client.post('/api/bare/device-1/sessions', headers=device[0], json={'repo': 'repo one', **extra})
    assert r.status_code == 422 and not bare_runner.calls
    assert 'private prompt' not in str(main.db.node_audit_list())


@pytest.mark.parametrize('field,value', [('repo', '../bad'), ('model', 'bad; touch file'), ('effort', 'high\nexit')])
def test_parts_refused(lite_client, bare_runner, device, field, value):
    r = lite_client.post('/api/bare/device-1/sessions', headers=device[0], json={'repo': 'repo one', field: value})
    assert r.status_code == 400 and not bare_runner.calls


def test_timeout_is_unconfirmed_once_and_reconciles(lite_client, bare_runner, device):
    headers = device[0]
    bare_runner.answers = [(0, '1001', ''), (0, 'staff', ''), subprocess.TimeoutExpired(['fake'], 12)]
    r = lite_client.post('/api/bare/device-1/sessions', headers=headers, json={'repo': 'repo one'})
    assert r.status_code == 200 and r.json()['status'] == 'unconfirmed'
    name = r.json()['session']['tmux_name']
    assert main.db.node_audit_list()[0]['status'] == 'unconfirmed'
    assert len(bare_runner.calls) == 3
    assert main.db.open_rows() == {}
    main.db.reconcile(set(), '9999')  # the local board must not end remote rows
    assert name in main.db.bare_open_rows(host='device-1')
    bare_runner.answers = [(0, name + '\n', '')]
    assert lite_client.post('/api/bare/device-1/sessions/read', headers=headers).json()['sessions'][0]['state'] == 'alive'
    bare_runner.answers = [subprocess.TimeoutExpired(['fake'], 12)]
    assert lite_client.post('/api/bare/device-1/sessions/read', headers=headers).status_code == 400
    assert name in main.db.bare_open_rows(host='device-1')
    bare_runner.answers = [(0, '', '')]
    assert lite_client.post('/api/bare/device-1/sessions/read', headers=headers).json()['sessions'] == []
    assert sum('new-session' in call[0][-1] for call in bare_runner.calls) == 1


@pytest.mark.parametrize('agent,exit_line', [('claude', '/exit'), ('codex', '/quit'), ('shell', 'exit')])
@pytest.mark.parametrize('still_alive', [True, False])
def test_stop_two_taps_and_delayed_kill(lite_client, bare_runner, device, agent, exit_line, still_alive):
    headers, _, timers = device
    row = start(lite_client, bare_runner, headers, agent).json()['session']
    name = row['tmux_name']
    path = '/api/bare/device-1/sessions/' + name + '/stop'
    count = len(bare_runner.calls)
    assert lite_client.post(path, headers=headers, json={}).status_code == 400
    assert len(bare_runner.calls) == count and not timers
    bare_runner.answers = [(0, '', '')]
    assert lite_client.post(path, headers=headers, json={'confirm': True}).json()['status'] == 'stopping'
    prefix = ['tmux', '-L', 'ccboard-bare-' + hashlib.sha256(b'n_0123456789abcdef').hexdigest()[:8]]
    assert bare_runner.calls[-1][0][-1] == shlex.join(prefix + ['send-keys', '-t', '=' + name + ':', '-l', exit_line, ';', 'send-keys', '-t', '=' + name + ':', 'Enter'])
    assert len(timers) == 1 and timers[0][0] == 10
    bare_runner.answers = [(0, name + '\n' if still_alive else '', '')] + ([(0, '', '')] if still_alive else [])
    _, fn, args = timers[0]
    fn(*args)
    assert not main.db.bare_open_rows(host='device-1')
    audit = main.db.node_audit_list()[0]
    assert (audit['action'], audit['user'], audit['status'], audit['detail']) == ('bare_kill', headers['Tailscale-User-Login'], 'ok' if still_alive else 'already_ended', None)
    assert sum('kill-session' in c[0][-1] for c in bare_runner.calls) == int(still_alive)
    if still_alive:
        assert bare_runner.calls[-1][0][-1] == shlex.join(prefix + ['kill-session', '-t', '=' + name])
    assert any(a['action'] == 'bare_stop' and a['user'] == headers['Tailscale-User-Login'] for a in main.db.node_audit_list())


@pytest.mark.parametrize('change', [{'Online': False}, {'ShareeNode': True}, {'UserID': 2}])
def test_start_revalidates_device(lite_client, bare_runner, device, change):
    device[1].update(change)
    r = lite_client.post('/api/bare/device-1/sessions', headers=device[0], json={'repo': 'repo one'})
    assert r.status_code == 400 and not bare_runner.calls


def test_privilege_changed_since_check(lite_client, bare_runner, device):
    bare_runner.answers = [(0, '1001', ''), (0, 'staff sudo', '')]
    r = lite_client.post('/api/bare/device-1/sessions', headers=device[0], json={'repo': 'repo one'})
    assert r.status_code == 400 and len(bare_runner.calls) == 2


def test_person_and_csrf_required(lite_client, bare_runner, device):
    path = '/api/bare/device-1/sessions'
    assert lite_client.post(path, json={'repo': 'repo one'}).status_code == 403
    assert lite_client.post(path, headers={'Tailscale-User-Login': device[0]['Tailscale-User-Login']}, json={'repo': 'repo one'}).status_code == 403
    assert not bare_runner.calls


def test_root_repo_and_local_cached_view(lite_client, bare_runner, device):
    saved = main.db.kv_get(bare.PREFIX + 'device-1')['value']
    saved['check']['repos'] = ['.']
    main.db.kv_set(bare.PREFIX + 'device-1', saved)
    r = start(lite_client, bare_runner, device[0], repo='.')
    assert r.status_code == 200
    remote = shlex.split(bare_runner.calls[-1][0][-1])
    assert shlex.split(remote[-1])[4] == 'my projects'
    calls = len(bare_runner.calls)
    assert len(bare.stored_devices(main.db, device[0]['Tailscale-User-Login'])[0]['sessions']) == 1
    assert len(bare_runner.calls) == calls


def test_stop_failed_read_does_not_kill(lite_client, bare_runner, device):
    headers, _, timers = device
    row = start(lite_client, bare_runner, headers).json()['session']
    bare_runner.answers = [(0, '', '')]
    path = '/api/bare/device-1/sessions/' + row['tmux_name'] + '/stop'
    assert lite_client.post(path, headers=headers, json={'confirm': True}).status_code == 200
    bare_runner.answers = [subprocess.TimeoutExpired(['fake'], 12)]
    _, fn, args = timers[0]
    fn(*args)
    assert row['tmux_name'] in main.db.bare_open_rows(host='device-1')
    assert not any('kill-session' in c[0][-1] for c in bare_runner.calls)


def test_isolation_and_exact_list(lite_client, bare_runner, device):
    row = start(lite_client, bare_runner, device[0]).json()['session']
    main.db.add_session(tmux_name='local', project='p', repo='r', name='shell', launcher='shell')
    main.db.add_bare_session(tmux_name='other', repo='r', agent='shell', cmd='sh -l', host='device-2')
    bare_runner.answers = [(1, '', 'no server running on fake socket')]
    assert lite_client.post('/api/bare/device-1/sessions/read', headers=device[0]).json()['sessions'] == []
    assert 'local' in main.db.open_rows() and 'other' in main.db.bare_open_rows(host='device-2')
    assert row['tmux_name'] not in main.db.bare_open_rows(host='device-1')
    expected = ['tmux', '-L', 'ccboard-bare-' + hashlib.sha256(b'n_0123456789abcdef').hexdigest()[:8], 'list-sessions', '-F', '#{session_name}']
    assert bare_runner.calls[-1][0][-1] == shlex.join(expected)


@pytest.mark.parametrize('answer,arms', [((1, '', ''), False), ((255, '', ''), True),
                                        (subprocess.TimeoutExpired(['fake'], 12), True),
                                        (OSError('unavailable'), False)])
def test_stop_only_arms_on_success_or_lost_ack(lite_client, bare_runner, device, answer, arms):
    headers, _, timers = device
    row = start(lite_client, bare_runner, headers).json()['session']
    bare_runner.answers = [answer]
    path = '/api/bare/device-1/sessions/' + row['tmux_name'] + '/stop'
    response = lite_client.post(path, headers=headers, json={'confirm': True})
    assert response.status_code == 400 and response.json()['error']
    assert len(timers) == int(arms)
    current = main.db.bare_open_rows('device-1')[row['tmux_name']]
    if arms:
        assert current['state'] == 'stopping'
    else:
        assert current == row
    assert main.db.node_audit_list()[0]['status'] == 'failed'


@pytest.mark.parametrize('state', ['alive', 'unconfirmed', 'stopping'])
def test_remove_refuses_open_sessions(lite_client, bare_runner, device, state):
    row = start(lite_client, bare_runner, device[0]).json()['session']
    main.db.bare_set_state('device-1', row['tmux_name'], state, 'test')
    count = len(bare_runner.calls)
    response = lite_client.delete('/api/bare/device-1', headers=device[0])
    assert response.status_code == 409
    assert response.json()['error'] == 'Stop its sessions first, or refresh the list, or remove it and forget its sessions here (they keep running on the device).'
    assert len(bare_runner.calls) == count
    assert main.db.kv_get(bare.PREFIX + 'device-1')
    main.db.bare_end('device-1', row['tmux_name'], 'test')
    bare_runner.answers = [(0, '', '')]
    assert lite_client.delete('/api/bare/device-1', headers=device[0]).status_code == 200


@pytest.mark.parametrize('answers', [[(1, '', '')], [(0, '', ''), (1, '', '')]])
def test_delayed_stop_failure_is_audited(lite_client, bare_runner, device, answers):
    headers, _, timers = device
    row = start(lite_client, bare_runner, headers).json()['session']
    bare_runner.answers = [(0, '', '')]
    lite_client.post('/api/bare/device-1/sessions/' + row['tmux_name'] + '/stop', headers=headers, json={'confirm': True})
    bare_runner.answers = [(rc, row['tmux_name'] if rc == 0 else out, err) for rc, out, err in answers]
    _, fn, args = timers[0]
    fn(*args)
    assert row['tmux_name'] in main.db.bare_open_rows('device-1')
    audit = main.db.node_audit_list()[0]
    assert (audit['action'], audit['user'], audit['status'], audit['detail']) == ('bare_kill', headers['Tailscale-User-Login'], 'failed', None)


def seed_sessions(count, host='device-1'):
    offset = 1000 if host == 'device-2' else 0
    names = [f'bare-{i + offset:032x}' for i in range(count)]
    for name in names:
        main.db.add_bare_session(host=host, tmux_name=name, repo='repo one', agent='shell', cmd='sh -l')
    return names


def test_session_reads_are_capped(lite_client, bare_runner, device):
    names = seed_sessions(60)
    statements = []
    main.db.conn.set_trace_callback(statements.append)
    try:
        state = lite_client.get('/api/state', headers=device[0]).json()
        assert len(state['bare_devices'][0]['sessions']) == 50
        bare_runner.answers = [(0, '\n'.join(names), '')]
        response = lite_client.post('/api/bare/device-1/sessions/read', headers=device[0])
        assert len(response.json()['sessions']) == 50
    finally:
        main.db.conn.set_trace_callback(None)
    selects = [s.upper() for s in statements if s.lstrip().upper().startswith('SELECT') and 'bare_sessions' in s]
    assert selects and all('LIMIT' in s for s in selects)


def test_twentieth_session_allowed_then_start_refused(lite_client, bare_runner, device):
    seed_sessions(19)
    seed_sessions(25, 'device-2')
    assert start(lite_client, bare_runner, device[0], 'shell').status_code == 200
    count = len(bare_runner.calls)
    response = lite_client.post('/api/bare/device-1/sessions', headers=device[0], json={'repo': 'repo one', 'agent': 'shell'})
    assert response.status_code == 409
    assert response.json() == {'error': 'This device already has 20 open sessions; stop some first.'}
    assert len(bare_runner.calls) == count
    assert len(main.db.bare_open_rows('device-1')) == 20


@pytest.mark.parametrize('change', [{'Online': False}, {'ShareeNode': True}, {'UserID': 2}])
def test_confirmed_forget_is_local_and_ends_all_device_rows(lite_client, bare_runner, device, monkeypatch, change):
    names = seed_sessions(60)
    other = seed_sessions(1, 'device-2')
    device[1].update(change)
    monkeypatch.setattr(nodes_discovery, 'read_tailscale', lambda **kw: pytest.fail('forget must be local'))
    response = lite_client.request('DELETE', '/api/bare/device-1', headers=device[0],
                                   json={'confirm': True, 'discard_sessions': True})
    assert response.status_code == 200
    assert not main.db.kv_get(bare.PREFIX + 'device-1')
    assert not main.db.bare_open_rows('device-1') and not bare_runner.calls
    rows = main.db.conn.execute('SELECT ended_reason FROM bare_sessions WHERE host=? LIMIT 100', ('device-1',)).fetchall()
    assert len(rows) == len(names) and all(r['ended_reason'] == 'device_removed' for r in rows)
    assert list(main.db.bare_open_rows('device-2')) == other
    assert [(r['action'], r['status']) for r in main.db.node_audit_list()][:2] == [('bare_remove', 'ok'), ('bare_forget', 'ok')]


@pytest.mark.parametrize('confirm,owned', [(False, True), (True, False)])
def test_forget_requires_confirmation_and_saved_owner(lite_client, bare_runner, device, confirm, owned):
    names = seed_sessions(1)
    if not owned:
        saved = main.db.kv_get(bare.PREFIX + 'device-1')['value']
        saved['owner'] = 'another-person'
        main.db.kv_set(bare.PREFIX + 'device-1', saved)
    response = lite_client.request('DELETE', '/api/bare/device-1', headers=device[0],
                                   json={'confirm': confirm, 'discard_sessions': True})
    assert response.status_code == 400
    assert list(main.db.bare_open_rows('device-1')) == names
    assert main.db.kv_get(bare.PREFIX + 'device-1') and not bare_runner.calls
    assert not any(r['action'] == 'bare_forget' for r in main.db.node_audit_list())


def test_read_preserves_pending_stop_and_its_timer(lite_client, bare_runner, device):
    row = start(lite_client, bare_runner, device[0]).json()['session']
    path = '/api/bare/device-1/sessions/' + row['tmux_name'] + '/stop'
    bare_runner.answers = [(0, '', '')]
    lite_client.post(path, headers=device[0], json={'confirm': True})
    before = main.db.bare_open_rows('device-1')[row['tmux_name']]['state_at']
    bare_runner.answers = [(0, row['tmux_name'], '')]
    response = lite_client.post('/api/bare/device-1/sessions/read', headers=device[0])
    assert response.json()['sessions'][0]['state'] == 'stopping'
    assert response.json()['sessions'][0]['state_at'] == before
    count = len(bare_runner.calls)
    assert lite_client.post(path, headers=device[0], json={'confirm': True}).json()['status'] == 'stopping'
    assert len(bare_runner.calls) == count and len(device[2]) == 1
