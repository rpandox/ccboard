"""Page writes are private, atomic, validated and available only to a person."""
import json
import os
import stat
from dataclasses import replace

import pytest

from app import config, options, platform as plat

URL = '/api/settings/options'
PERSON = {'Tailscale-User-Login': 'alice', 'X-CCBoard': '1'}
KEY = 'CCBOARD_NODE_LANES'


def test_a_local_sign_in_person_can_save(board, monkeypatch):
    """The settings write check counts a local sign-in (issue #127) as the person, like the rest of the board."""
    from app import hooks, local_signin as local
    c, settings, _ = board
    for target in {id(settings): settings, id(local.settings): local.settings}.values():     # the board's object and the one local_signin reads
        for name, value in (("local_signin", True), ("local_signin_user", "alice"), ("local_signin_hours", 12), ("allowed_users", {"alice"})):
            monkeypatch.setattr(target, name, value)
    monkeypatch.setattr(c._transport, "client", ("127.0.0.1", 50000))
    c.base_url = f"http://127.0.0.1:{settings.port}"
    local.clear()
    try:
        token = c.post('/auth/local/token', headers={"X-CCBoard-Token": hooks.ensure_token()}).headers[local.TOKEN_HEADER]
        assert c.post('/auth/local/redeem', content=token, headers={"X-CCBoard": "1"}).status_code == 204
        answer = c.put(URL, headers={"X-CCBoard": "1"}, json={'changes': {KEY: '5'}})
        assert answer.status_code == 200, answer.text
    finally:
        local.clear()


@pytest.fixture
def board(lite_client, monkeypatch, tmp_path):
    from app import main
    env = {'CCBOARD_DATA_DIR': str(tmp_path), 'CCBOARD_PAGE_FILE': str(tmp_path / 'settings.page.json'),
           'CCBOARD_ENV_FILE': '', 'CCBOARD_ALLOWED_USERS': 'alice', KEY: '3', 'CCBOARD_HUB_TOKEN': 'fixture-secret-marker'}
    settings = config.Settings(env)
    monkeypatch.setattr(main, 'settings', settings)
    return lite_client, settings, env


def put(board, changes, **extra):
    return board[0].put(URL, headers=PERSON, json={'changes': changes, **extra})


def row(response, key=KEY):
    return next(r for r in response.json()['options'] if r['key'] == key)


def test_memory_viewer_page_save_restart_and_reset(board):
    key = 'CCBOARD_MEM_VIEWER_URL'
    value = 'https://memory.example.ts.net:8446/'
    saved = put(board, {key: value})
    assert saved.status_code == 200, saved.text
    entry = row(saved, key)
    assert entry['group'] == 'memory' and entry['cls'] == 'page'
    assert entry['value'] == value and entry['pending'] and entry['source'] == 'page'
    assert board[1].mem_viewer_url() is None
    assert config.Settings(board[2]).mem_viewer_url() == value
    assert put(board, {key: None}).status_code == 200
    assert config.Settings(board[2]).mem_viewer_url() is None


@pytest.mark.parametrize('value', ['http://example.ts.net/', 'https://user:secret@example.ts.net/', 'junk'])
def test_memory_viewer_page_rejects_invalid_url(board, value):
    answer = put(board, {'CCBOARD_MEM_VIEWER_URL': value})
    assert answer.status_code == 400
    assert 'HTTPS' in answer.text or 'credentials' in answer.text
    assert not board[1].page_file.exists()


@pytest.mark.parametrize('value', ['0.5', '0', '-1', '1e20', '10000', 'nan', 'inf'])
def test_approval_timeout_range(board, value):
    option = next(o for o in options.registry() if o.key == 'CCBOARD_APPROVE_TIMEOUT')
    assert options.validate_value(option, value, {}) == 'Use a whole number from 1 to 9999.'
    assert put(board, {option.key: value}).status_code in (400, 403)
    loaded = config.Settings({**board[2], option.key: value})
    assert loaded.approve_timeout == 90 and loaded.approve_timeout_bad
    assert 'default is in use' in next(r for r in options.view(loaded)['options'] if r['key'] == option.key)['problem']


@pytest.mark.parametrize('value', ['1', '9999'])
def test_approval_timeout_endpoints(board, value):
    loaded = config.Settings({**board[2], 'CCBOARD_APPROVE_TIMEOUT': value})
    assert loaded.approve_timeout == int(value) and not loaded.approve_timeout_bad


def test_state_survives_planted_approval_timeout(board, monkeypatch):
    from app import main
    path = board[1].page_file
    path.write_text('{"version":1,"set":{"CCBOARD_APPROVE_TIMEOUT":"1e20"}}')
    path.chmod(0o600)
    monkeypatch.setattr(main, 'settings', config.Settings(board[2]))
    assert board[0].get('/api/state', headers=PERSON).status_code == 200
    monkeypatch.setattr(main, 'settings', config.Settings({**board[2], 'CCBOARD_APPROVE_TIMEOUT': '1e20'}))
    assert board[0].get('/api/state', headers=PERSON).status_code == 200


def test_numeric_options_are_bounded_and_node_poll_is_safe(board):
    assert all(o.bounds and None not in o.bounds for o in options.registry() if o.kind in ('int', 'float'))
    assert put(board, {'CCBOARD_NODES_POLL': '1e20'}).status_code == 400
    loaded = config.Settings({**board[2], 'CCBOARD_NODES_POLL': '1e20'})
    assert loaded.nodes_poll == 20


@pytest.mark.parametrize('script', ['install.sh', 'scripts/install-macos.sh'])
@pytest.mark.parametrize('value', ['0', '0000', '0.5', '-1', '1e20', '10000', '1', '9999'])
def test_installer_approval_timeout_bounds(script, value):
    import subprocess
    from pathlib import Path
    source = (Path(__file__).resolve().parent.parent / script).read_text()
    check = next(line for line in source.splitlines() if 'CCBOARD_APPROVE_TIMEOUT must be a whole number' in line)
    result = subprocess.run(['bash', '-c', 'die() { exit 1; }; ' + check],
                            env={**os.environ, 'CCBOARD_APPROVE_TIMEOUT': value}, capture_output=True)
    assert (result.returncode == 0) == (value in ('1', '9999'))


def test_approval_timeout_is_installer_only(board):
    key = 'CCBOARD_APPROVE_TIMEOUT'
    answer = put(board, {key: '180'})
    assert answer.status_code == 403 and answer.json()['reason'] == 'installer'
    path = board[1].page_file
    path.write_text(json.dumps({'version': 1, 'set': {key: '180'}}))
    path.chmod(0o600)
    loaded = config.Settings(board[2])
    assert loaded.approve_timeout == 90 and loaded.sources[key] != 'page'
    assert key in loaded.page_info['ignored']
    option = next(o for o in options.registry() if o.key == key)
    assert option.cls == 'installer' and 'hooks' in option.help and 'installer' in option.help


@pytest.mark.parametrize('key,attr,fallback,values', [
    ('NTFY_TOPIC', 'ntfy_topic', 'ccboard', ['', 'a/b', 'a.b', 'a b', 'x' * 65, ' padded ']),
    ('CCBOARD_NODE_NAME', 'node_name', '', ['', '-node', '_node', 'a.b', 'a b', 'x' * 42, ' padded ']),
])
def test_topic_and_node_name_follow_installer_rules(board, key, attr, fallback, values):
    for value in values:
        answer = put(board, {key: value})
        assert answer.status_code == 400, (value, answer.text)
        assert 'letters' in answer.json()['reason']
        loaded = config.Settings({**board[2], key: value})
        assert getattr(loaded, attr) == fallback and getattr(loaded, attr + '_bad') == bool(value.strip())       # empty means unset at start, as in the installer
    for value in (('a_b-9', 'x' * 64) if key == 'NTFY_TOPIC' else ('Node_9-a', 'x' * 41)):
        assert put(board, {key: value}).status_code == 200
        assert getattr(config.Settings(board[2]), attr) == value


@pytest.mark.parametrize('source', ['page', 'environment'])
def test_doctor_node_name_fix_names_its_source(board, monkeypatch, source):
    from app import doctor, nodes
    env = dict(board[2])
    if source == 'page':
        board[1].page_file.write_text('{"version":1,"set":{"CCBOARD_NODE_NAME":"bad.name"}}')
        board[1].page_file.chmod(0o600)
    else:
        env['CCBOARD_NODE_NAME'] = 'bad.name'
    loaded = config.Settings(env)
    monkeypatch.setattr(doctor, 'settings', loaded)
    monkeypatch.setattr(nodes, 'settings', loaded)
    monkeypatch.setattr(nodes, 'id_report', lambda: {'drifted': False})
    result = doctor._c_node(None)
    assert result[0] == 'warn'
    assert ('in Settings > Options' if source == 'page' else 'in /etc/ccboard/env') in str(result[2])


def test_negative_integer_shows_bounds(board):
    answer = put(board, {KEY: '-1'})
    assert answer.status_code == 400
    assert answer.json()['reason'] == 'Use 0 to 999.'


@pytest.mark.parametrize('problem,reason', [
    ('json', 'is not valid JSON'),
    ('version', 'must contain version 1 and string settings'),
    ('mode', 'is readable by group or others (mode 0644); run chmod 600 on it'),
    ('owner', 'is not owned by the user the board runs as'),
    ('symlink', 'is a symbolic link'),
    ('size', 'exceeds 64 KB'),
])
def test_bad_page_save_explains_reset_command(board, monkeypatch, problem, reason):
    import shlex
    path = board[1].page_file.with_name("settings page's.json")
    board[1].page_file = path
    content = '{' if problem == 'json' else '{"version":2,"set":{}}' if problem == 'version' else 'x' * 65537 if problem == 'size' else '{"version":1,"set":{}}'
    target = path.with_name('untouched-target') if problem == 'symlink' else path
    target.write_text(content)
    target.chmod(0o644 if problem == 'mode' else 0o600)
    if problem == 'symlink':
        path.symlink_to(target)
    if problem == 'owner':
        monkeypatch.setattr(plat, 'current_uid', lambda: path.stat().st_uid + 1)
    answer = put(board, {KEY: '5'})
    assert answer.status_code == 400
    assert reason in answer.json()['reason']
    assert 'rm -- ' + shlex.quote(str(path)) in answer.json()['reason']
    assert target.read_text() == content
    assert path.is_symlink() == (problem == 'symlink')


def test_round_trip_precedence_reset_and_pending(board, tmp_path, monkeypatch):
    from app import main
    client, settings, env = board
    file = tmp_path / 'env'
    file.write_text(KEY + '=4\n')
    file.chmod(0o600)
    env['CCBOARD_ENV_FILE'] = str(file)
    settings = config.Settings(env)
    monkeypatch.setattr(main, 'settings', settings)
    board = client, settings, env
    boot = dict(main.BOOT)
    answer = put(board, {KEY: '5'})
    assert answer.status_code == 200, answer.text
    assert answer.json()['saved'] == [KEY]
    assert row(answer)['source'] == 'page'
    assert row(answer)['value'] == 5 and row(answer)['running'] == 3 and row(answer)['pending']
    assert stat.S_IMODE(settings.page_file.stat().st_mode) == 0o600
    assert json.loads(settings.page_file.read_text()) == {'version': 1, 'set': {KEY: '5'}}
    restarted = config.Settings(env)
    assert restarted.node_lanes == 5 and restarted.sources[KEY] == 'page'
    assert options.pending(settings, env) == [KEY]
    assert options.pending_state(settings)['count'] == 1 and main.BOOT == boot
    assert options.pending(restarted, env) == []
    reset = put(board, {KEY: None})
    assert reset.status_code == 200 and row(reset)['value'] == 3
    assert json.loads(settings.page_file.read_text())['set'] == {}
    assert config.Settings(env).node_lanes == 3


def test_stamp_conflict_and_atomic_failure(board, monkeypatch):
    client, settings, _ = board
    stamp = client.get(URL, headers=PERSON).json()['stamp']
    assert put(board, {KEY: '5'}, stamp=stamp).status_code == 200
    old = settings.page_file.read_bytes()
    assert put(board, {KEY: '6'}, stamp=stamp).status_code == 409
    def fail(*args):
        raise OSError('injected replace failure')
    monkeypatch.setattr(plat, 'atomic_replace', fail)
    assert put(board, {KEY: '7'}).status_code == 500
    assert settings.page_file.read_bytes() == old
    assert not list(settings.page_file.parent.glob('.settings.page.json.*.tmp'))


def test_race_during_write_leaves_newer_file(board, monkeypatch):
    settings = board[1]
    real = plat.secure_write
    def raced(path, data, mode, *, still):
        def changed():
            path.write_text('{"version":1,"set":{}}')
            path.chmod(0o600)
            return still()
        real(path, data, mode, still=changed)
    monkeypatch.setattr(plat, 'secure_write', raced)
    assert put(board, {KEY: '5'}).status_code == 409
    assert json.loads(settings.page_file.read_text())['set'] == {}
    assert not list(settings.page_file.parent.glob('.*.tmp'))


@pytest.mark.parametrize('key,value', [
    (KEY, '1000'), (KEY, '-1'), (KEY, 'no'), (KEY, '1.5'),
    ('CCBOARD_AUTOCLOSE_GRACE', '86400'), ('CCBOARD_AUTOCLOSE_GRACE', 'inf'),
    ('CCBOARD_EPIC_LEASE_MIN', '9'), ('CCBOARD_EPIC_MAX_WINDOW_PCT', '101'),
    ('CCBOARD_EPIC_WORKER_POLL', '29'), ('CCBOARD_EPIC_WORKER_MAX_PER_HOUR', '61'),
    ('CCBOARD_EPIC_WORKER_MAX_PER_DAY', '501'), ('CCBOARD_NODES_POLL', '4'),
    ('CCBOARD_NODE_PORTS', '443,bad'), ('CCBOARD_NODE_PORTS', '1,2,3,4,5,6,7,8,9'), ('CCBOARD_NODE_TAGS', 'invalid!tag'),
    ('CCBOARD_SUBAGENT_MODEL', 'bad model'), ('CCBOARD_NODE_NAME', 'x' * 501),
    ('NTFY_TOPIC', 'https://user:fixture-secret-marker@example.invalid/'),
    ('NTFY_TOPIC', 'https://example.invalid/?token=fixture-secret-marker'),
    ('NTFY_TOPIC', 'fixture-secret-marker'), ('NTFY_TOPIC', 'line\nbreak'),
    (KEY, 5), ('UNKNOWN', 'value'),
])
def test_validation_is_all_or_nothing(board, key, value, caplog):
    answer = put(board, {'CCBOARD_EPIC_ALIAS': 'safe', 'CCBOARD_NODE_NAME': 'safe', key: value})
    assert answer.status_code == 400, answer.text
    assert answer.json()['reason'].endswith('.')
    assert answer.json()['key'] == key
    assert not board[1].page_file.exists()
    assert 'fixture-secret-marker' not in answer.text + caplog.text


@pytest.mark.parametrize('kind,choices,value', [('bool', None, 'yes'), ('choice', ('a', 'b'), 'c')])
def test_validation_handles_future_page_kinds(kind, choices, value):
    option = replace(next(o for o in options.registry() if o.key == KEY), kind=kind, choices=choices)
    assert options.validate_value(option, value, {})


@pytest.mark.parametrize('key,reason', [('CCBOARD_HUB_TOKEN', 'file_only'), ('CCBOARD_PORT', 'installer'), ('CCBOARD_AUTO_CONTINUE', 'needs_confirm')])
def test_classes_cannot_write_or_load(board, key, reason, caplog):
    answer = put(board, {key: 'fixture-secret-marker'})
    assert answer.status_code == 403 and answer.json()['reason'] == reason
    assert not board[1].page_file.exists()
    board[1].page_file.write_text(json.dumps({'version': 1, 'set': {key: 'fixture-secret-marker', KEY: '5'}}))
    board[1].page_file.chmod(0o600)
    loaded = config.Settings(board[2])
    assert loaded.sources[key] != 'page' and loaded.node_lanes == 5
    assert key in caplog.text and 'fixture-secret-marker' not in caplog.text


@pytest.mark.parametrize('problem', ['symlink', 'owner', 'mode', 'size', 'directory', 'json', 'schema', 'credential'])
def test_bad_files_start_and_doctor_warns(board, monkeypatch, caplog, problem):
    from app import doctor
    _, settings, env = board
    path = settings.page_file
    data = json.dumps({'version': 1, 'set': {KEY: '5'}})
    if problem == 'directory':
        path.mkdir()
    elif problem == 'symlink':
        target = path.with_name('target')
        target.write_text(data)
        path.symlink_to(target)
    else:
        path.write_text('x' * 65537 if problem == 'size' else '{' if problem == 'json' else
                        '{"version":1,"set":{"CCBOARD_NODE_LANES":5}}' if problem == 'schema' else
                        '{"version":1,"set":{"CCBOARD_NODE_NAME":"https://u:fixture-secret-marker@example.invalid/"}}' if problem == 'credential' else data)
        path.chmod(0o644 if problem == 'mode' else 0o600)
        if problem == 'owner':
            monkeypatch.setattr(plat, 'current_uid', lambda: path.stat().st_uid + 1)
    loaded = config.Settings(env)
    assert loaded.node_lanes == 3
    monkeypatch.setattr(doctor, 'settings', loaded)
    assert doctor._c_settings_page(None)[0] == 'warn'
    count = len(caplog.records)
    config.Settings(env)
    assert len(caplog.records) == count
    assert 'fixture-secret-marker' not in caplog.text


def test_audit_and_secrets(board, caplog):
    from app import main, nodes
    answer = put(board, {KEY: '5'})
    records = nodes.audit_list(db=main.db, action='settings_set')
    assert len(records) == 1 and records[0]['user'] == 'alice'
    assert KEY in records[0]['detail'] and "'3' -> '5'" in records[0]['detail']
    refusal = put(board, {'NTFY_TOPIC': 'fixture-secret-marker'})
    refused = nodes.audit_list(db=main.db, action='settings_refused')
    assert len(refused) == 1 and 'NTFY_TOPIC' in refused[0]['detail']
    for text in [answer.text, refusal.text, board[0].get(URL, headers=PERSON).text,
                 json.dumps(nodes.audit_list(db=main.db)), board[1].page_file.read_text(), caplog.text]:
        assert 'fixture-secret-marker' not in text
    listing = board[0].get(URL, headers=PERSON).json()
    assert len(listing['audit']) == 1 and len(listing['refusals']) == 1


def test_non_person_refusals_do_not_write_audit(board, caplog):
    from app import main, hooks, nodes, mcp_tokens
    headers = [{}, {'Tailscale-User-Login': 'alice'}, {**PERSON, 'Tailscale-User-Login': 'stranger'},
               {**PERSON, hooks.TOKEN_HEADER: hooks.ensure_token()}, {**PERSON, 'X-CCBoard-Hub': 'fixture-secret-marker'}]
    headers.extend({**PERSON, name: ''} for name in ('Authorization', hooks.TOKEN_HEADER, 'X-CCBoard-Hub'))
    for scope in nodes.SCOPES:
        _, token = nodes.add_incoming(scopes=[scope], db=main.db)
        headers.append({**PERSON, 'Authorization': 'Bearer ' + token})
    token, _ = mcp_tokens.mint('fixture-device')
    headers.append({**PERSON, 'Authorization': 'Bearer ' + token})
    with caplog.at_level('DEBUG', logger='ccboard'):
        for header in headers:
            assert board[0].put(URL, headers=header, json={'changes': {KEY: '5'}}).status_code in (401, 403)
    assert not board[1].page_file.exists()
    assert nodes.audit_list(db=main.db, action='settings_refused') == []
    assert 'settings request refused' in caplog.text
    assert board[0].put(URL, headers={**PERSON, 'Origin': 'https://foreign.invalid'}, json={'changes': {KEY: '5'}}).status_code == 403
    assert put(board, {'CCBOARD_PORT': '8001'}).status_code == 403
    records = nodes.audit_list(db=main.db, action='settings_refused')
    assert len(records) == 2 and all(r['user'] == 'alice' for r in records)
    assert URL not in str(nodes.NODE_ROUTES)
    assert board[0].put(URL, headers={**PERSON, 'Origin': 'http://testserver'}, json={'changes': {KEY: '5'}}).status_code == 200


def test_pending_cache_and_stale_doctor(board, monkeypatch):
    from app import doctor
    settings = board[1]
    assert options.pending_state(settings)['count'] == 0
    assert put(board, {KEY: '5'}).status_code == 200
    assert options.pending_state(settings)['count'] == 1
    with monkeypatch.context() as patch:
        patch.setattr(options, 'pending', lambda *args: pytest.fail('cache miss'))
        assert options.pending_state(settings)['count'] == 1
    monkeypatch.setattr(doctor, 'settings', settings)
    os.utime(settings.page_file, (1, 1))
    assert doctor._c_settings_page(None)[0] == 'warn'


def test_missing_file_leaves_existing_values_unchanged(tmp_path):
    baseline = config.Settings({KEY: '7'})
    fresh = config.Settings({KEY: '7', 'CCBOARD_PAGE_FILE': str(tmp_path / 'absent')})
    for key, value in vars(baseline).items():
        if key not in {'_input_env', 'page_file'}:
            assert getattr(fresh, key) == value, key
    # A mapping with a data directory never discovers a real file implicitly.
    (tmp_path / 'settings.page.json').write_text('{"version":1,"set":{"CCBOARD_NODE_LANES":"5"}}')
    assert config.Settings({'CCBOARD_DATA_DIR': str(tmp_path)}).node_lanes == 3


def test_process_default_path_and_disabled_file(tmp_path, monkeypatch):
    monkeypatch.setenv('CCBOARD_DATA_DIR', str(tmp_path))
    monkeypatch.delenv('CCBOARD_PAGE_FILE', raising=False)
    path = tmp_path / 'settings.page.json'
    path.write_text('{"version":1,"set":{"CCBOARD_NODE_LANES":"5"}}')
    path.chmod(0o600)
    assert config.Settings().node_lanes == 5
    monkeypatch.setenv('CCBOARD_PAGE_FILE', '')
    assert config.Settings().node_lanes == 3


def test_body_limits_and_audit_window(board):
    from app import main, nodes
    for body in ({}, {'changes': {}}, {'changes': {str(i): 'x' for i in range(21)}}, {'changes': {KEY: []}}):
        assert board[0].put(URL, headers=PERSON, json=body).status_code == 400
    assert board[0].put(URL, headers=PERSON, content='{').status_code == 400
    for _ in range(22):
        put(board, {KEY: '5'})
    for _ in range(25):
        assert put(board, {'CCBOARD_PORT': '8001'}).status_code == 403
    answer = board[0].get(URL, headers=PERSON).json()
    assert len(answer['audit']) == 20
    assert all(r['action'] == 'settings_set' for r in answer['audit'])
    assert len(answer['refusals']) == 5
    assert all(r['action'] == 'settings_refused' for r in answer['refusals'])
    for records in (answer['audit'], answer['refusals']):
        ids = [r['id'] for r in records]
        assert ids == sorted(ids, reverse=True)


def test_state_reports_pending_and_simulated_boot(board, monkeypatch):
    from app import main
    before = board[0].get('/api/state', headers=PERSON).json()
    assert before['settings_pending']['count'] == 0
    assert put(board, {KEY: '5'}).status_code == 200
    after = board[0].get('/api/state', headers=PERSON).json()
    assert after['settings_pending']['count'] == 1 and after['boot'] == before['boot']
    assert after['settings_pending']['labels'] == ['Task lanes']
    monkeypatch.setattr(main, 'settings', config.Settings(board[2]))
    monkeypatch.setattr(main, 'BOOT', {'id': 'simulated-next-boot', 'at': before['boot']['at']})
    restarted = board[0].get('/api/state', headers=PERSON).json()
    assert restarted['settings_pending']['count'] == 0 and restarted['boot']['id'] != after['boot']['id']


def test_rejected_model_log_once(board, caplog):
    path = board[1].page_file
    path.write_text('{"version":1,"set":{"CCBOARD_SUBAGENT_MODEL":"invalid model"}}')
    path.chmod(0o600)
    config.Settings(board[2])
    config.Settings(board[2])
    assert sum('CCBOARD_SUBAGENT_MODEL' in r.message for r in caplog.records) == 1
    assert 'invalid model' not in caplog.text


def test_unknown_keys_are_logged_by_name_only(board, caplog):
    path = board[1].page_file
    path.write_text('{"version":1,"set":{"UNKNOWN_OPTION":"fixture-secret-marker"}}')
    path.chmod(0o600)
    config.Settings(board[2])
    assert 'UNKNOWN_OPTION' in caplog.text and 'fixture-secret-marker' not in caplog.text


def test_audit_keeps_every_key_in_a_batch(board):
    from app import main, nodes
    changes = {o.key: o.default or 'fixture' for o in options.registry() if o.cls == 'page' and o.kind == 'text'}
    assert put(board, changes).status_code == 200
    detail = nodes.audit_list(db=main.db, action='settings_set')[0]['detail']
    assert all(key in detail for key in changes)


def test_listing_never_pairs_an_old_value_with_a_new_stamp(board, monkeypatch):
    real = options.next_settings
    changed = False
    def during_read(settings, env=None):
        nonlocal changed
        old = real(settings, env)
        if not changed:
            changed = True
            plat.secure_write(settings.page_file, b'{"version":1,"set":{"CCBOARD_NODE_LANES":"8"}}', 0o600)
        return old
    monkeypatch.setattr(options, 'next_settings', during_read)
    listing = board[0].get(URL, headers=PERSON).json()
    assert put(board, {KEY: '5'}, stamp=listing['stamp']).status_code == 409
    assert config.Settings(board[2]).node_lanes == 8


def test_deep_json_is_refused_without_breaking_startup_or_audit(board, caplog):
    path = board[1].page_file
    nested = '[' * 2000 + '0' + ']' * 2000
    path.write_text(nested)
    path.chmod(0o600)
    assert config.Settings(board[2]).node_lanes == 3
    assert 'ignoring settings.page.json' in caplog.text
    assert board[0].put(URL, headers=PERSON, content=nested).status_code == 400


def test_empty_topic_and_node_name_mean_unset():
    from app.config import Settings
    s = Settings({"CCBOARD_NODE_NAME": "", "NTFY_TOPIC": "  "})
    assert not s.node_name_bad and not s.ntfy_topic_bad
    assert s.node_name == "" and s.ntfy_topic == "ccboard"
