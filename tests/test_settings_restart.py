"""Restart requests never control a real process or service in this suite."""
import asyncio
import json
import signal
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from starlette.requests import Request

from app import config, main, nodes, restart

URL = '/api/settings/restart'
PERSON = {'Tailscale-User-Login': 'alice', 'X-CCBoard': '1'}
LAUNCH = restart._launch


@pytest.fixture
def board(lite_client, fake_tmux, monkeypatch, tmp_path):
    settings = config.Settings({'CCBOARD_DATA_DIR': str(tmp_path), 'CCBOARD_ENV_FILE': '',
                                'CCBOARD_ALLOWED_USERS': 'alice', 'CCBOARD_RUNTIME': 'docker',
                                'CCBOARD_RESTART_POLICY': 'unless-stopped', 'CCBOARD_HUB_TOKEN': 'secret-marker'})
    monkeypatch.setattr(main, 'settings', settings)
    monkeypatch.setattr(restart, 'settings', settings)
    monkeypatch.setattr(restart.plat, 'is_wsl', lambda: False)
    monkeypatch.setattr(restart.backup, 'running', lambda: False)
    monkeypatch.setattr(restart.clonequeue, 'running_clones', lambda: 0)
    monkeypatch.setattr(restart.clonequeue, 'status', lambda: {'queued': []})
    monkeypatch.setattr(restart.clonequeue, '_inflight', 0)
    monkeypatch.setattr(restart.tmux, 'viewers', lambda: {})
    commands, launches = [], []
    monkeypatch.setattr(restart.plat, 'spawn_detached', lambda argv, *a, **kw: commands.append(argv))
    monkeypatch.setattr(restart, '_restart_requested', False)
    monkeypatch.setattr(restart.os, 'kill', lambda *a: pytest.fail('must never signal a process'))
    monkeypatch.setattr(restart.os, '_exit', lambda *a: pytest.fail('must never exit the process'))

    async def launch(via):
        launches.append(via)
    monkeypatch.setattr(restart, '_launch', launch)
    return lite_client, settings, launches, commands


def post(board, force=False, headers=None):
    return board[0].post(URL, json={'force': force}, headers=PERSON if headers is None else headers)


@pytest.mark.parametrize('runtime,expected', [('docker', 'docker'), ('systemd', 'systemd'), ('launchd', 'launchd'), ('host', 'none')])
def test_method(board, runtime, expected):
    board[1].runtime = runtime
    assert restart.method() == expected


def test_unsupervised_container_and_wsl(board, monkeypatch):
    board[1]._input_env.pop('CCBOARD_RESTART_POLICY')
    assert post(board).json()['reason'] == 'no_supervisor'
    assert 'force-recreate' in restart.info()['hint']
    board[1].runtime = 'systemd'
    monkeypatch.setattr(restart.plat, 'is_wsl', lambda: True)
    monkeypatch.setattr(restart.plat, 'service_manager', lambda: 'none')
    assert restart.method() == 'none'
    assert not board[2]


def test_exact_commands_and_delayed_detached_launch(board, monkeypatch):
    monkeypatch.setattr(restart.plat, 'launchd_target', lambda job: 'gui/123/dev.ccboard.' + job)
    expected = {'launchd': ['launchctl', 'kickstart', '-k', 'gui/123/dev.ccboard.board']}
    for via, argv in expected.items():
        assert restart.command(via) == argv
        asyncio.run(LAUNCH(via))
        assert board[3][-1] == ['sh', '-c', 'sleep 1; exec "$@"', 'ccboard-restart', *argv]
    assert restart.command('docker') == []
    assert all(word not in json.dumps(board[3]) for word in ('ccboard-tmux', 'dev.ccboard.tmux', 'ttyd', 'tailscale'))


def test_docker_delays_then_signals_only_own_pid(board, monkeypatch):
    events = []
    monkeypatch.setattr(restart.os, 'getpid', lambda: 2468)
    monkeypatch.setattr(restart.os, 'kill', lambda *args: events.append(args))

    async def sleep(seconds):
        events.append(('sleep', seconds))
    monkeypatch.setattr(restart.asyncio, 'sleep', sleep)
    asyncio.run(LAUNCH('docker'))
    assert events == [('sleep', 1.0), (2468, signal.SIGTERM)]
    assert not board[3]


@pytest.mark.parametrize('runtime', ['docker', 'systemd'])
def test_signal_failure_is_audited(board, monkeypatch, runtime):
    board[1].runtime = runtime
    monkeypatch.setattr(restart, '_launch', LAUNCH)
    monkeypatch.setattr(restart, '_restart_requested', False)

    async def sleep(seconds):
        pass
    def fail_signal(*args):
        raise OSError('secret-marker')
    monkeypatch.setattr(restart.asyncio, 'sleep', sleep)
    monkeypatch.setattr(restart.os, 'kill', fail_signal)
    assert post(board).status_code == 202
    assert len(nodes.audit_list(db=main.db, action='settings_restart_failed')) == 1
    assert not restart._restart_requested



def test_response_is_sent_before_launcher(board):
    sent = []

    async def run():
        async def receive():
            return {'type': 'http.request', 'body': b'{"force":false}'}
        req = Request({'type': 'http', 'method': 'POST', 'path': URL, 'headers': [], 'state': {'user': 'alice'}}, receive)
        response = await main.api_settings_restart(req)
        assert response.status_code == 202 and not board[2]

        async def send(message):
            assert not board[2]
            sent.append(message['type'])
        await response(req.scope, receive, send)
    asyncio.run(run())
    assert sent == ['http.response.start', 'http.response.body']
    assert board[2] == ['docker']


def test_host_refusal_and_systemd_without_sudo(board):
    board[1].runtime = 'host'
    assert post(board).status_code == 409
    assert post(board).json()['reason'] == 'no_supervisor'
    board[1].runtime = 'systemd'
    assert post(board).status_code == 202
    assert board[2] == ['systemd']
    assert board[3] == []
    assert not hasattr(restart, 'sudo_allowed')
    assert restart.command('systemd') == []


def test_systemd_signals_self_then_exits_after_lifespan_cleanup(board, monkeypatch):
    events = []
    monkeypatch.setattr(restart, '_restart_requested', False, raising=False)
    monkeypatch.setattr(restart.os, 'getpid', lambda: 2468)
    monkeypatch.setattr(restart.os, 'kill', lambda pid, sig: events.append(('signal', pid, sig)))
    monkeypatch.setattr(restart.os, '_exit', lambda code: events.append(('exit', code)))
    for name in ("db", "sampler", "indexer", "sched", "taskflow_rt"):
        monkeypatch.setattr(main, name, getattr(main, name))
    # Exercise the real lifespan with inert workers and startup dependencies.
    for name in ('settings', 'DB', 'hooks', 'notify', 'deploy', 'account_store', 'codex_accounts',
                 'push', 'nodes_whois', 'nodes', 'usage', 'samples', 'clonequeue', 'prpoll',
                 'mem_monitor', 'search', 'scheduler', 'health', 'team_github', 'team_presence',
                 'nodes_worker', 'recover', 'taskflow', 'github_write', 'local_signin',
                 'threading', 'nodes_epics', 'nodes_work', 'nodes_stream'):
        monkeypatch.setattr(main, name, MagicMock())
    main.settings.claude_mem = False
    main.health.parse_nodes.return_value = []
    main.github_write.writes_enabled.return_value = False
    main.nodes_worker.enabled.return_value = False
    monkeypatch.setattr(main, 'node_worker', None)
    monkeypatch.setattr(main, 'startup_line', lambda: 'test')
    for name in ('_hub', '_team_poller', '_presence_publisher', '_runner'):
        monkeypatch.setattr(main, name, MagicMock())
    main._hub.return_value.stop.side_effect = lambda: events.append(('cleanup',))

    async def run():
        async with main.lifespan(main.app):
            await LAUNCH('systemd')
            assert restart._restart_requested
            assert events == [('signal', 2468, signal.SIGTERM)]
    asyncio.run(run())
    assert events == [('signal', 2468, signal.SIGTERM), ('cleanup',), ('exit', restart.RESTART_EXIT_CODE)]
    assert restart.RESTART_EXIT_CODE == 75
    assert not board[3]
    assert 'tmux' not in json.dumps(board[3])


def test_systemd_restart_unit_contract():
    unit = Path('systemd/ccboard.service.in').read_text()
    assert 'NoNewPrivileges=yes' in unit
    assert 'Restart=on-failure' in unit and 'RestartSec=2' in unit
    assert 'RestartPreventExitStatus=' not in unit



def test_busy_force_and_audit(board, monkeypatch):
    monkeypatch.setattr(main.db, 'perm_pending', lambda: [{}])
    monkeypatch.setattr(restart.tmux, 'viewers', lambda: {'one': {'full': 2}})
    monkeypatch.setattr(restart.clonequeue, 'running_clones', lambda: 1)
    r = post(board)
    assert r.status_code == 409
    assert r.json()['blockers'] == ['1 permission is waiting for an answer and will be lost',
                                    '2 terminals are open; ttyd and tmux sessions keep running', 'a clone is running']
    assert not board[2]
    monkeypatch.setattr(restart.backup, 'running', lambda: True)
    assert restart.readiness(main.db)[-1] == 'a backup is running'
    assert post(board, True).status_code == 202
    records = nodes.audit_list(db=main.db)
    assert {r['action'] for r in records} == {'settings_restart', 'settings_restart_refused'}
    assert all(r['user'] == 'alice' for r in records)
    assert 'secret-marker' not in json.dumps(records) and 'CCBOARD_' not in json.dumps(records)


def test_idle_rate_limit_persists_through_boot(board, monkeypatch):
    r = post(board)
    assert r.status_code == 202 and r.json()['boot_id'] == main.BOOT['id']
    monkeypatch.setattr(main, 'BOOT', {'id': 'another-process', 'at': main.BOOT['at']})
    r = post(board)
    assert r.status_code == 429 and 1 <= int(r.headers['Retry-After']) <= 60
    assert board[2] == ['docker']


def test_callers_and_no_relay_or_mcp(board):
    from app import hooks, health, mcp_tokens
    headers = [{}, {'Tailscale-User-Login': 'alice'}, {**PERSON, 'Origin': 'https://foreign.invalid'},
               {**PERSON, hooks.TOKEN_HEADER: hooks.ensure_token()}, {**PERSON, health.HUB_HEADER: 'secret-marker'}]
    for scope in nodes.SCOPES:
        _, token = nodes.add_incoming(scopes=[scope], db=main.db)
        headers.append({**PERSON, 'Authorization': 'Bearer ' + token})
    token, _ = mcp_tokens.mint('test')
    headers.append({**PERSON, 'Authorization': 'Bearer ' + token})
    for h in headers:
        assert post(board, headers=h).status_code in (401, 403)
    assert not board[2]
    assert nodes.route_scope('POST', URL) is None
    for file in ('app/mcp.py', 'scripts/ccboard_mcp.py'):
        assert URL not in Path(file).read_text()


def test_local_person(board, monkeypatch):
    from app import hooks, local_signin as local
    client, settings = board[:2]
    for target in {id(settings): settings, id(local.settings): local.settings}.values():
        for name, value in (("local_signin", True), ("local_signin_user", "alice"), ("local_signin_hours", 12), ("allowed_users", {"alice"})):
            monkeypatch.setattr(target, name, value)
    monkeypatch.setattr(client._transport, "client", ("127.0.0.1", 50000))
    client.base_url = f"http://127.0.0.1:{settings.port}"
    local.clear()
    try:
        token = client.post('/auth/local/token', headers={hooks.TOKEN_HEADER: hooks.ensure_token()}).headers[local.TOKEN_HEADER]
        assert client.post('/auth/local/redeem', content=token, headers={'X-CCBoard': '1'}).status_code == 204
        assert post(board, headers={'X-CCBoard': '1'}).status_code == 202
        assert nodes.audit_list(db=main.db, action='settings_restart')[0]['user'] == 'alice'
    finally:
        local.clear()


def test_boot_stable_then_new_process(board, monkeypatch):
    first = main.build_state('alice')['boot']
    assert main.build_state('alice')['boot'] == first
    monkeypatch.setattr(main, 'BOOT', {'id': 'simulated-new-process', 'at': first['at']})
    assert main.build_state('alice')['boot']['id'] != first['id']


@pytest.mark.parametrize('body', [{}, {'force': 'true'}, {'force': True, 'command': 'anything'}, []])
def test_strict_body(board, body):
    assert board[0].post(URL, headers=PERSON, json=body).status_code == 400
    assert not board[2]


def test_readiness_preview_is_read_only(board, monkeypatch):
    monkeypatch.setattr(main.db, 'perm_pending', lambda: [{}])
    answer = board[0].get('/api/settings/options?readiness=1', headers=PERSON)
    assert answer.status_code == 200
    assert answer.json()['restart']['blockers'] == ['1 permission is waiting for an answer and will be lost']
    assert not board[2] and not board[3]


def test_old_docker_hint_refreshes_installed_compose_first(board):
    board[1]._input_env.pop('CCBOARD_RESTART_POLICY')
    info = restart.info()
    response = post(board)
    assert response.status_code == 409 and not board[2]
    assert info['method'] == 'none'
    assert './install.sh' in info['hint'] and 'host' in info['why']
    assert 'deploy/docker-compose.yml' in info['why']
    assert info['hint'].index('./install.sh') < info['hint'].index('force-recreate')
    assert response.json()['hint'] == info['hint']
    readme = Path('README.md').read_text().split('- **Docker:**', 1)[1].split('\n', 1)[0]
    assert 'refresh the installed compose file first' in readme
    assert './install.sh' in readme and 'deploy/docker-compose.yml' in readme


def test_refusals_audit_only_identified_people(board, caplog):
    import logging
    from app import hooks, health
    caplog.set_level(logging.DEBUG, logger='ccboard')
    for headers in ({}, {'Tailscale-User-Login': 'mallory', 'X-CCBoard': '1'},
                    {**PERSON, hooks.TOKEN_HEADER: 'token'},
                    {**PERSON, health.HUB_HEADER: 'secret-marker'},
                    {**PERSON, 'Authorization': 'Bearer token'}):
        assert post(board, headers=headers).status_code in (401, 403)
    assert nodes.audit_list(db=main.db, action='settings_restart_refused') == []
    assert 'restart request refused without an identified allowed person' in caplog.text
    assert post(board, headers={**PERSON, 'Origin': 'https://foreign.invalid'}).status_code == 403
    rows = nodes.audit_list(db=main.db, action='settings_restart_refused')
    assert len(rows) == 1 and rows[0]['user'] == 'alice'


def test_failed_launch_audits_safe_reason_and_releases_spacing(board, monkeypatch):
    board[1].runtime = 'launchd'
    monkeypatch.setattr(restart, '_launch', LAUNCH)

    def fail_spawn(argv, logf):
        raise OSError('secret-marker private launch details')
    monkeypatch.setattr(restart.plat, 'spawn_detached', fail_spawn)
    assert post(board).status_code == 202
    rows = nodes.audit_list(db=main.db, action='settings_restart_failed')
    assert len(rows) == 1 and rows[0]['user'] == 'alice'
    assert 'OSError' in json.dumps(rows) and 'secret-marker' not in json.dumps(rows)
    assert main.db.kv_get('settings_restart_at')['value']['at'] == 0
    assert post(board).status_code == 202


def test_launchd_output_goes_to_advertised_data_log(board, monkeypatch):
    board[1].runtime = 'launchd'
    outputs = []

    def spawn(argv, logf):
        outputs.append(Path(logf.name))
        logf.write(b'launch output\n')
    monkeypatch.setattr(restart.plat, 'spawn_detached', spawn)
    asyncio.run(LAUNCH('launchd'))
    info = restart.info()
    assert outputs == [board[1].data_dir / 'restart.log']
    assert outputs[0].read_text() == 'launch output\n'
    assert info['launch_log'] == str(outputs[0])
    assert str(outputs[0]) in info['logs']


def test_six_accepted_restarts_per_hour_persist_and_expire(board, monkeypatch):
    clock = [10000.0]
    monkeypatch.setattr(main.time, 'time', lambda: clock[0])
    for i in range(6):
        clock[0] = 10000.0 + i * 61
        assert post(board).status_code == 202
        monkeypatch.setattr(main, 'BOOT', {'id': f'boot-{i}', 'at': main.BOOT['at']})
    value = main.db.kv_get('settings_restart_at')['value']
    assert value['accepted'] == [10000.0 + i * 61 for i in range(6)]
    # A fresh DB connection sees the same limit; no in-memory history is needed.
    from app.db import DB
    persisted = DB(config.settings.db_path)
    monkeypatch.setattr(main, 'db', persisted)
    try:
        clock[0] = 10366.0
        response = post(board)
        assert response.status_code == 429 and len(board[2]) == 6
        assert response.json()['message'] == 'At most 6 board restarts are allowed per hour. Try again later.'
        assert int(response.headers['Retry-After']) == 3234
        clock[0] = 13600.0
        assert post(board).status_code == 202
        accepted = main.db.kv_get('settings_restart_at')['value']['accepted']
        assert accepted == [10000.0 + i * 61 for i in range(1, 6)] + [13600.0]
    finally:
        persisted.close()


def test_legacy_restart_stamp_keeps_spacing_and_counts_in_hour(board, monkeypatch):
    monkeypatch.setattr(main.time, 'time', lambda: 10000.0)
    main.db.kv_set('settings_restart_at', {'at': 9980.0})
    assert post(board).status_code == 429
    monkeypatch.setattr(main.time, 'time', lambda: 10041.0)
    assert post(board).status_code == 202
    assert main.db.kv_get('settings_restart_at')['value']['accepted'] == [9980.0, 10041.0]


def test_failed_launch_keeps_hourly_accepted_budget(board, monkeypatch):
    async def fail(via):
        raise OSError('secret-marker')
    monkeypatch.setattr(restart, '_launch', fail)
    for _ in range(6):
        assert post(board).status_code == 202
    assert post(board).status_code == 429
    assert len(main.db.kv_get('settings_restart_at')['value']['accepted']) == 6


def test_a_requested_restart_exits_even_if_a_cleanup_step_fails():
    """The restart exit runs in the lifespan's finally: a failing cleanup step must not leave the board down after Restart."""
    import inspect
    src = inspect.getsource(main.lifespan)
    tail = src[src.index("    yield\n"):]
    assert tail.lstrip().startswith("yield\n    try:") or "\n    try:\n" in tail[:40]
    fin = tail.rindex("    finally:\n")
    assert "restart.finish_shutdown()" in tail[fin:] and "restart.finish_shutdown()" not in tail[:fin]
