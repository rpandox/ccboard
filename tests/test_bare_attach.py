"""Device terminal credentials: temp data, fake discovery and fake executables only."""
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app import bare, hooks, main, mcp_tokens, nodes
from app.config import settings
from tests.test_bare_sessions import device, seed_sessions

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def ticket(lite_client, device, bare_runner):
    name = seed_sessions(1)[0]
    path = '/api/bare/device-1/sessions/' + name + '/attach'
    response = lite_client.post(path, headers=device[0])
    assert response.status_code == 200, response.text
    ident = response.json()['attach_url'].removeprefix('/term/')
    file = settings.data_dir / 'bare-tickets' / ident
    assert not bare_runner.calls
    return ident, file, json.loads(file.read_text()), name, path


def test_issue_private_reusable_ticket_and_audit(lite_client, device, ticket, monkeypatch):
    ident, file, data, name, path = ticket
    assert bare.ticket_id_ok(ident) and len(ident) == len('bare-ticket-') + 64
    assert file.stat().st_mode & 0o7777 == 0o600
    assert file.stat().st_uid == os.getuid()
    assert file.parent.stat().st_mode & 0o777 == 0o700
    assert data['session'] == name and data['user'] == 'ccagent'
    assert data['expires_at'] - data['issued_at'] == 600
    first = bare.read_ticket(ident)
    assert bare.read_ticket(ident) == first
    monkeypatch.setattr(bare.time, 'time', lambda: data['expires_at'] - 1)
    assert bare.read_ticket(ident) == first
    monkeypatch.setattr(bare.time, 'time', lambda: data['expires_at'])
    with pytest.raises(ValueError, match='expired'):
        bare.read_ticket(ident)
    audit = main.db.node_audit_list()[0]
    assert (audit['action'], audit['user'], audit['peer'], audit['target'], audit['status']) == (
        'bare_attach', device[0]['Tailscale-User-Login'], 'device-1', name, 'ok')
    assert ident not in json.dumps(main.db.node_audit_list()) and audit['detail'] is None
    response = lite_client.get('/term/' + ident, headers=device[0])
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    assert response.headers['referrer-policy'] == 'no-referrer'
    assert lite_client.get('/term/' + ident).status_code == 403


def test_a_ticket_write_failure_names_the_board_not_the_device(lite_client, device, bare_runner, monkeypatch):
    name = seed_sessions(1)[0]
    def fail(*_args, **_kwargs):
        raise OSError(28, 'No space left on device')
    monkeypatch.setattr(bare, 'issue_ticket', fail)
    response = lite_client.post('/api/bare/device-1/sessions/' + name + '/attach', headers=device[0])
    assert response.status_code == 400
    assert 'could not prepare the terminal ticket' in response.text and 'did not answer' not in response.text
    assert not bare_runner.calls


def test_exact_shared_ssh_options_and_remote_words(ticket):
    ident, _, data, name, _ = ticket
    check = bare.argv(data['device'], data['user'], 'tmux')[:-1]
    expected = [v.replace('StrictHostKeyChecking=accept-new', 'StrictHostKeyChecking=yes').replace('RequestTTY=no', 'RequestTTY=yes') for v in check[:-1]]
    expected += ['-t', check[-1], 'tmux', '-L', data['socket'], 'attach-session', '-t', '=' + name]
    assert bare.read_ticket(ident) == expected
    assert shlex.split(shlex.join(expected)) == expected


@pytest.mark.parametrize('change', ['unchecked', 'foreign', 'missing', 'ended', 'other-device', 'offline', 'shared', 'no-longer-owned'])
def test_only_an_open_row_on_the_persons_checked_device(lite_client, device, bare_runner, change):
    name = seed_sessions(1)[0]
    saved = main.db.kv_get(bare.PREFIX + 'device-1')['value']
    if change == 'unchecked':
        saved['check']['ok'] = False
    if change == 'foreign':
        saved['owner'] = 'another-person'
    main.db.kv_set(bare.PREFIX + 'device-1', saved)
    if change == 'missing':
        main.db.kv_del(bare.PREFIX + 'device-1')
    if change in ('ended', 'other-device'):
        main.db.bare_end('device-1', name, 'test')
    if change == 'other-device':
        name = seed_sessions(1, host='device-2')[0]
    if change == 'offline':
        device[1]['Online'] = False
    if change == 'shared':
        device[1]['ShareeNode'] = True
    if change == 'no-longer-owned':
        device[1]['UserID'] = 2
    response = lite_client.post('/api/bare/device-1/sessions/' + name + '/attach', headers=device[0])
    assert response.status_code == 400
    assert not (settings.data_dir / 'bare-tickets').exists() and not bare_runner.calls
    assert main.db.node_audit_list()[0]['action'] == 'bare_attach'
    assert main.db.node_audit_list()[0]['status'] == 'failed'


def test_only_person_with_csrf_not_hook_node_or_mcp(lite_client, device, bare_runner):
    from tests.test_nodes_auth import mint
    name = seed_sessions(1)[0]
    _, node_token = mint(main.db, nodes.SCOPES)
    mcp_tokens.set_enabled(True)
    mcp_token, _ = mcp_tokens.mint('test-device')
    headers = [{}, {'Tailscale-User-Login': device[0]['Tailscale-User-Login']},
               {'X-CCBoard': '1', 'X-CCBoard-Token': hooks.ensure_token()}]
    headers += [{'X-CCBoard': '1', 'Authorization': 'Bearer ' + t} for t in (node_token, mcp_token)]
    headers += [{**device[0], 'Authorization': 'Bearer ' + t} for t in (node_token, mcp_token)]
    for h in headers:
        assert lite_client.post('/api/bare/device-1/sessions/' + name + '/attach', headers=h).status_code == 403
    assert not (settings.data_dir / 'bare-tickets').exists() and not bare_runner.calls


@pytest.mark.parametrize('value', ['', 'device', 'user@device', 'device.example', '../ticket', '/ticket', 'device:22', 'two words', '-o', '\n', 'bare-ticket-' + 'a' * 63, 'bare-ticket-' + 'G' * 64])
def test_invalid_id_refused_before_file_access(projects_dir, monkeypatch, value):
    monkeypatch.setattr(bare, '_ticket_directory', lambda *a: pytest.fail('invalid ID touched disk'))
    with pytest.raises(ValueError, match='Invalid terminal ticket'):
        bare.read_ticket(value)


@pytest.mark.parametrize('mode', [0o644, 0o400, 0o660, 0o1600, 0o700])
def test_wrong_mode(ticket, mode):
    ident, file, *_ = ticket
    file.chmod(mode)
    with pytest.raises(ValueError, match='private file'):
        bare.read_ticket(ident)


def test_foreign_owner(ticket, monkeypatch):
    ident, *_ = ticket
    original = bare.os.fstat
    def foreign(fd):
        values = list(original(fd))
        values[4] += 1
        return os.stat_result(values)
    monkeypatch.setattr(bare.os, 'fstat', foreign)
    with pytest.raises(ValueError, match='owned by the board user'):
        bare.read_ticket(ident)


@pytest.mark.parametrize('kind', ['missing', 'symlink', 'fifo', 'directory', 'large', 'json', 'utf8', 'deep-json'])
def test_bad_file(ticket, kind):
    ident, file, *_ = ticket
    if kind in ('missing', 'symlink', 'fifo', 'directory'):
        data = file.read_bytes()
        file.unlink()
        if kind == 'symlink':
            other = file.parent / 'other'
            other.write_bytes(data)
            other.chmod(0o600)
            file.symlink_to(other)
        elif kind == 'fifo':
            os.mkfifo(file, 0o600)
        elif kind == 'directory':
            file.mkdir(mode=0o600)
    else:
        file.write_bytes({'large': b' ' * 4097, 'json': b'{', 'utf8': b'\xff', 'deep-json': b'[' * 1500 + b']' * 1500}[kind])
    with pytest.raises(ValueError):
        bare.read_ticket(ident)


@pytest.mark.parametrize('field,value', [
    ('device', None), ('device', {'dns_name': 'bad;command', 'ips': ['100.64.0.2']}),
    ('device', {'dns_name': '-option', 'ips': ['100.64.0.2']}),
    ('device', {'dns_name': 'device', 'ips': ['127.0.0.1']}),
    ('device', {'dns_name': 'device', 'ips': ['100.64.0.2', '100.64.0.3']}),
    ('device', {'dns_name': 'device', 'ips': ['fd7a:115c:a1e0::1%zone']}),
    ('user', 'root'), ('user', '-option'), ('user', 'ccagent;exit'), ('user', None),
    ('socket', None), ('socket', 'ccboard'), ('socket', '../socket'),
    ('session', None), ('session', 'normal--local--session'), ('session', 'bare-' + 'a' * 32 + ';exit'),
    ('issued_at', 10 ** 3000), ('expires_at', 10 ** 3000), ('issued_at', True), ('issued_at', float('nan')), ('expires_at', float('inf')), ('expires_at', 'later'),
])
def test_invalid_fields_never_build_ssh(ticket, monkeypatch, field, value):
    ident, file, data, *_ = ticket
    data[field] = value
    file.write_text(json.dumps(data))
    # All invalid values must fail before executing anything; the runner remains the autouse refusal fake.
    with pytest.raises(ValueError):
        bare.read_ticket(ident)


def test_lifetime_schema_and_unique_ids(lite_client, device, ticket):
    ident, file, data, _, route = ticket
    assert lite_client.post(route, headers=device[0]).json()['attach_url'] != '/term/' + ident
    for patch in ({'expires_at': data['issued_at'] + 601}, {'issued_at': data['expires_at'] + 1}, {'extra': 'refuse'}):
        file.write_text(json.dumps({**data, **patch}))
        with pytest.raises(ValueError):
            bare.read_ticket(ident)


@pytest.fixture
def wrapper_env(tmp_path):
    fakes = tmp_path / 'fakes'
    fakes.mkdir()
    record = tmp_path / 'argv.json'
    fake = fakes / 'ssh'
    fake.write_text('#!' + sys.executable + '\nimport json, os, sys\nopen(os.environ["RECORD"], "w").write(json.dumps(sys.argv[1:]))\n')
    fake.chmod(0o755)
    (fakes / 'python3').symlink_to(sys.executable)
    # Other processes cannot accidentally reach a real tmux/tailscale.
    for name in ('tmux', 'tailscale'):
        f = fakes / name
        f.write_text('#!/bin/sh\nexit 99\n')
        f.chmod(0o755)
    return {'PATH': str(fakes) + ':/usr/bin:/bin', 'HOME': str(tmp_path), 'CCBOARD_DATA_DIR': str(settings.data_dir), 'RECORD': str(record)}, record


def test_wrapper_and_dispatcher_execute_only_ticket_argv(ticket, wrapper_env, tmp_path):
    ident, _, data, name, _ = ticket
    env, record = wrapper_env
    expected = bare.read_ticket(ident)[1:]
    # Run from a copy with only the stdlib module, as on a Docker host (no board venv/package).
    app = tmp_path / 'host app'
    (app / 'bin').mkdir(parents=True)
    (app / 'app').mkdir()
    shutil.copy(ROOT / 'app/bare.py', app / 'app/bare.py')
    for script in ('ccboard-attach', 'ccboard-attach-bare'):
        shutil.copy(ROOT / 'bin' / script, app / 'bin' / script)
    for script in ('ccboard-attach', 'ccboard-attach-bare'):
        result = subprocess.run([str(app / 'bin' / script), ident], env=env, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(record.read_text()) == expected
        record.unlink()


@pytest.mark.parametrize('args', [[], ['one', 'two'], ['user@device'], ['../ticket']])
def test_wrapper_plain_failures(projects_dir, wrapper_env, args):
    env, record = wrapper_env
    result = subprocess.run([str(ROOT / 'bin/ccboard-attach-bare'), *args], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 1 and result.stdout.startswith('ccboard: ')
    assert not result.stderr and not record.exists()


def test_normal_ttyd_service_argv_is_pinned():
    import plistlib
    systemd = (ROOT / 'systemd/ccboard-ttyd.service.in').read_text()
    line = next(line for line in systemd.splitlines() if line.startswith('ExecStart='))
    assert line == 'ExecStart=__TTYD_BIN__ -i lo -p ${TTYD_PORT} -W -O -a -t titleFixed=ccboard -t disableLeaveAlert=true __APP_BIN__/ccboard-attach'
    mac = plistlib.loads((ROOT / 'launchd/dev.ccboard.ttyd.plist.in').read_bytes())
    assert mac['ProgramArguments'] == ['__TTYD_BIN__', '-i', '__TTYD_IFACE__', '-p', '__TTYD_PORT__', '-W', '-O', '-a', '-t',
                                       'titleFixed=ccboard', '-t', 'disableLeaveAlert=true', '__APP_BIN__/ccboard-attach']


def test_doctor_checks_host_helper_and_module(projects_dir, monkeypatch):
    from app import doctor
    monkeypatch.setattr(settings, 'runtime', 'docker')
    path = settings.data_dir / 'app/bin/ccboard-attach-bare'
    path.parent.mkdir(parents=True)
    assert doctor._c_attach_bare_wrapper(None).status == 'fail'
    shutil.copy(ROOT / 'bin/ccboard-attach-bare', path)
    assert doctor._c_attach_bare_wrapper(None).status == 'fail'
    (path.parent.parent / 'app').mkdir()
    shutil.copy(ROOT / 'app/bare.py', path.parent.parent / 'app/bare.py')
    assert doctor._c_attach_bare_wrapper(None).status == 'pass'


def test_wrapper_default_data_directory_is_in_the_temp_home(ticket, wrapper_env):
    ident, file, *_ = ticket
    env, record = wrapper_env
    env.pop('CCBOARD_DATA_DIR')
    directory = Path(env['HOME']) / '.local/share/ccboard/bare-tickets'
    directory.mkdir(mode=0o700, parents=True)
    shutil.copy(file, directory / ident)
    response = subprocess.run([str(ROOT / 'bin/ccboard-attach-bare'), ident], env=env, capture_output=True, text=True, timeout=10)
    assert response.returncode == 0, response.stdout + response.stderr
    assert json.loads(record.read_text())[-1] == '=' + ticket[3]


def test_dispatcher_refuses_ticket_plus_mode(ticket, wrapper_env):
    env, record = wrapper_env
    response = subprocess.run([str(ROOT / 'bin/ccboard-attach'), ticket[0], 'grid'], env=env, capture_output=True, text=True, timeout=10)
    assert response.returncode == 1 and response.stdout.startswith('ccboard: usage:') and not record.exists()
