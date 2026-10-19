"""Widening is opt-in, person-only and compared again at the atomic save boundary."""
import importlib.util
import json
from pathlib import Path

import pytest

from app import config, options

URL = '/api/settings/options'
PERSON = {'Tailscale-User-Login': 'alice', 'X-CCBoard': '1', 'Origin': 'http://testserver'}
WORKER = 'CCBOARD_EPIC_WORKER'
WRITES = 'CCBOARD_EPIC_WRITES'
REPOS = 'CCBOARD_EPIC_REPOS'
HOUR = 'CCBOARD_EPIC_WORKER_MAX_PER_HOUR'
MOVED = (HOUR, 'CCBOARD_EPIC_WORKER_MAX_PER_DAY', 'CCBOARD_NODE_LANES', 'CCBOARD_SUBAGENT_MODEL')
# Owner decision: these can never move into page/confirm without changing this test.
FINAL_FILE = {
    'CCBOARD_CODEX_HOOK_TRUST', 'CCBOARD_DEV_BYPASS_USER', 'CCBOARD_RUNTIME', 'CCBOARD_ENV_FILE',
    'CCBOARD_PAGE_WIDEN', 'CCBOARD_ALLOWED_USERS', 'CCBOARD_NODE_AUTH', 'CCBOARD_NODE_CAP',
    'CCBOARD_NODE_TAGS', 'CCBOARD_MCP_NODES', 'CCBOARD_MCP_REMOTE', 'CCBOARD_CLONE_ALLOWED_HOSTS',
    'CCBOARD_HUB_TOKEN', 'CCBOARD_NODES', 'CCBOARD_RESTIC_REPO', 'CCBOARD_RESTIC_PASSWORD_FILE',
    'CCBOARD_BACKUP_EXTRA', 'CCBOARD_PRICE_TABLE',
}


@pytest.fixture
def board(lite_client, monkeypatch, tmp_path):
    from app import main
    file = tmp_path / 'env'
    file.write_text('')
    file.chmod(0o600)
    env = {'CCBOARD_ENV_FILE': str(file), 'CCBOARD_PAGE_FILE': str(tmp_path / 'settings.page.json'),
           'CCBOARD_DATA_DIR': str(tmp_path), 'CCBOARD_ALLOWED_USERS': 'alice'}
    s = config.Settings(env)
    monkeypatch.setattr(main, 'settings', s)
    return lite_client, s, env, file


def trusted(board, **values):
    board[3].write_text('\n'.join(k + '=' + v for k, v in values.items()) + '\n')


def put(board, changes, **extra):
    return board[0].put(URL, headers=PERSON, json={'changes': changes, **extra})


def page_file(board, values, confirmed=()):
    board[1].page_file.write_text(json.dumps({'version': 1, 'set': values, 'confirmed': list(confirmed)}))
    board[1].page_file.chmod(0o600)


def loaded(board):
    return config.Settings(board[2])


def test_switch_off_refuses_save_and_ignores_load(board, monkeypatch, caplog):
    from app import doctor
    response = put(board, {WORKER: '1'})
    assert response.status_code == 403
    assert response.json()['reason'] == 'widen_off' and response.json()['line'] == 'CCBOARD_PAGE_WIDEN=1'
    assert not board[1].page_file.exists()
    page_file(board, {WORKER: '1'}, [WORKER])
    before = board[1].page_file.read_bytes()
    assert put(board, {WORKER: '1'}).status_code == 403
    assert board[1].page_file.read_bytes() == before
    s = loaded(board)
    assert not s.epic_worker and s.page_info['ignored_widening'] == 1
    assert WORKER in caplog.text
    monkeypatch.setattr(doctor, 'settings', s)
    assert '1 saved widening values ignored' in doctor._c_settings_page(None)[1]


def test_confirmation_round_trip_and_audit(board):
    from app import main, nodes
    trusted(board, CCBOARD_PAGE_WIDEN='1', CCBOARD_EPIC_REPOS='sample/project', CCBOARD_EPIC_WRITES='1', CCBOARD_EPIC_AUTHORS='owner')
    changes = {WORKER: '1'}
    answer = put(board, changes)
    assert answer.status_code == 409
    body = answer.json()
    assert body['reason'] == 'needs_confirm' and body['phrase'] == 'ALLOW WORKER'
    assert body['widens'] == [{'key': WORKER, 'from': '0', 'to': '1', 'plain': 'The worker may start epic issues unattended.'}]
    for wrong in ('allow worker', 'ALLOW WORKER ', ' ALLOW WORKER', '', ['ALLOW WORKER']):
        answer = put(board, changes, confirm=wrong)
        assert answer.status_code == 403 and answer.json()['reason'] == 'bad_phrase'
        assert not board[1].page_file.exists()
    assert put(board, changes, confirm='ALLOW WORKER').status_code == 200
    assert json.loads(board[1].page_file.read_text())['set'][WORKER] == '1'
    assert loaded(board).epic_worker
    audit = nodes.audit_list(db=main.db, action='settings_widen')
    assert len(audit) == 1 and audit[0]['user'] == 'alice'
    assert WORKER in audit[0]['detail'] and "'0' -> '1'" in audit[0]['detail']
    assert 'phrase typed' in audit[0]['detail']
    all_rows = json.dumps(nodes.audit_list(100, db=main.db))
    assert 'ALLOW WORKER' not in all_rows and 'allow worker' not in all_rows
    assert any('bad_phrase' in r['detail'] for r in nodes.audit_list(db=main.db, action='settings_refused'))
    trusted(board, CCBOARD_EPIC_REPOS='sample/project', CCBOARD_EPIC_WRITES='1')
    assert not loaded(board).epic_worker
    assert loaded(board).page_info['ignored_widening'] == 1


def test_narrowing_and_raising_from_stored_state(board):
    trusted(board, CCBOARD_PAGE_WIDEN='1', CCBOARD_EPIC_WORKER='1', CCBOARD_EPIC_WRITES='1',
            CCBOARD_EPIC_REPOS='sample/one,sample/two', CCBOARD_EPIC_WORKER_MAX_PER_HOUR='9')
    for changes in ({WORKER: '0'}, {REPOS: 'sample/one'}, {HOUR: '5'}):
        assert put(board, changes).status_code == 200
    assert put(board, {HOUR: '6'}).status_code == 409  # compare to saved 5, not running 9
    assert put(board, {HOUR: '6'}, confirm='ALLOW WORKER').status_code == 200
    assert put(board, {HOUR: '6'}).status_code == 200
    assert put(board, {HOUR: None}).status_code == 409  # reset to file's 9 widens too


@pytest.mark.parametrize('env,missing', [
    ({}, (WRITES, REPOS)), ({WRITES: '1'}, (REPOS,)), ({REPOS: 'sample/project'}, (WRITES,)),
])
def test_worker_requires_saved_or_batched_prerequisites(board, env, missing):
    trusted(board, CCBOARD_PAGE_WIDEN='1', **env)
    answer = put(board, {WORKER: '1'}, confirm='ALLOW WORKER')
    assert answer.status_code == 400
    assert answer.json()['reason'] == 'After the restart the worker will refuse to start: ' + ('epic writes off' if WRITES in missing else 'no repository is listed')
    assert not board[1].page_file.exists()
    changes = {WORKER: '1', WRITES: '1', REPOS: 'sample/project', 'CCBOARD_EPIC_AUTHORS': 'owner'}
    pending = put(board, changes)
    assert pending.status_code == 409
    assert put(board, changes, confirm=pending.json()['phrase']).status_code == 200


def test_separate_writes_phrase_and_mixed_groups(board):
    trusted(board, CCBOARD_PAGE_WIDEN='1')
    assert put(board, {WRITES: '1'}).json()['phrase'] == 'ALLOW GITHUB WRITES'
    changes = {WRITES: '1', REPOS: 'sample/project'}
    assert put(board, changes).json()['phrase'] == 'ALLOW GITHUB WRITES AND ALLOW WORKER'
    assert put(board, changes, confirm='ALLOW WORKER').status_code == 403
    assert put(board, changes, confirm='ALLOW GITHUB WRITES AND ALLOW WORKER').status_code == 200


@pytest.mark.parametrize('key', [o.key for o in options.registry() if o.cls in ('file', 'installer')])
@pytest.mark.parametrize('on', [False, True])
def test_never_from_page_even_with_metadata(board, key, on, caplog):
    trusted(board, CCBOARD_PAGE_WIDEN='1' if on else '0')
    value = 'bypass' if key == 'CCBOARD_CODEX_HOOK_TRUST' else 'fixture-secret'
    answer = put(board, {key: value})
    assert answer.status_code == 403
    assert not board[1].page_file.exists()
    page_file(board, {key: value}, [key])
    s = loaded(board)
    assert key in s.page_info['ignored'] and s.sources[key] != 'page'
    assert s.dev_bypass_user is None and s.codex_hook_trust == 'review'
    assert s.page_widen is on
    assert key in caplog.text and 'fixture-secret' not in caplog.text


def test_final_class_list_and_no_page_master_switch(board):
    reg = {o.key: o.cls for o in options.registry()}
    assert all(reg[k] in ('file', 'installer') for k in FINAL_FILE)
    assert {k for k, cls in reg.items() if cls == 'confirm'} == {*MOVED, WORKER, WRITES, REPOS, 'CCBOARD_EPIC_AUTHORS'}
    assert put(board, {'CCBOARD_PAGE_WIDEN': '1'}).status_code == 403
    page_file(board, {'CCBOARD_PAGE_WIDEN': '1', WORKER: '1'}, ['CCBOARD_PAGE_WIDEN', WORKER])
    assert not loaded(board).page_widen and not loaded(board).epic_worker


@pytest.mark.parametrize('on', [False, True])
def test_legacy_page_entries_stay_ignored(board, on):
    trusted(board, CCBOARD_PAGE_WIDEN='1' if on else '0')
    page_file(board, dict(zip(MOVED, ['60', '500', '999', 'opus'])))
    s = loaded(board)
    assert set(s.page_info['ignored']) == set(MOVED)
    assert (s.epic_worker_max_hour, s.epic_worker_max_day, s.node_lanes, s.subagent_model) == (6, 30, 3, '')


@pytest.mark.parametrize('key,old,new,wider', [
    (WORKER, '0', '1', True), (WORKER, '1', '0', False), (WRITES, '0', '1', True),
    (REPOS, 'sample/one,sample/two', 'sample/one', False), (REPOS, 'SAMPLE/one', 'sample/one', False),
    (REPOS, 'sample/one', 'sample/one sample/two', True),
    ('CCBOARD_EPIC_AUTHORS', 'alice:1', 'alice', True), ('CCBOARD_EPIC_AUTHORS', 'alice', 'alice,bob', True),
    ('CCBOARD_EPIC_AUTHORS', 'bob', '', True), ('CCBOARD_EPIC_AUTHORS', '', '', False), ('CCBOARD_EPIC_AUTHORS', '', 'bob', True),
    ('CCBOARD_EPIC_AUTHORS', 'alice,bob', 'bob', False),
    (HOUR, '6', '7', True), (HOUR, '6', '5', False), ('CCBOARD_EPIC_WORKER_MAX_PER_DAY', '30', '31', True),
    ('CCBOARD_NODE_LANES', '3', '0', True), ('CCBOARD_NODE_LANES', '0', '3', False),
    ('CCBOARD_SUBAGENT_MODEL', '', 'inherit', False), ('CCBOARD_SUBAGENT_MODEL', 'opus', 'haiku', True),
])
def test_widening_direction(key, old, new, wider):
    assert options.widens(key, old, new) is wider


def test_person_gate_and_node_audit(board, unsigned_node_keys):
    from app import main, hooks, nodes, mcp_tokens
    trusted(board, CCBOARD_PAGE_WIDEN='1', CCBOARD_EPIC_WRITES='1', CCBOARD_EPIC_REPOS='sample/project')
    headers = [{}, {'Tailscale-User-Login': 'alice'},
               {**PERSON, hooks.TOKEN_HEADER: hooks.ensure_token()}, {**PERSON, 'X-CCBoard-Hub': 'fixture-secret'},
               {**PERSON, 'Origin': 'https://foreign.invalid'}]
    headers += [{**PERSON, key: ''} for key in ('Authorization', hooks.TOKEN_HEADER, 'X-CCBoard-Hub')]
    token, _ = mcp_tokens.mint('fixture-device')
    headers.append({**PERSON, 'Authorization': 'Bearer ' + token})
    for header in headers:
        assert board[0].put(URL, headers=header, json={'changes': {WORKER: '1'}, 'confirm': 'ALLOW WORKER'}).status_code in (401, 403)
    for scope in nodes.SCOPES:
        _, token = nodes.add_incoming(scopes=[scope], db=main.db)
        header = {k: v for k, v in PERSON.items() if k != 'Origin'}
        header['Authorization'] = 'Bearer ' + token
        assert board[0].put(URL, headers=header, json={'changes': {WORKER: '1'}, 'confirm': 'ALLOW WORKER'}).status_code == 403
    records = nodes.audit_list(100, db=main.db, action='route_refused')
    assert len(records) == len(nodes.SCOPES)
    assert all(r['target'] == 'PUT ' + URL for r in records)
    assert not board[1].page_file.exists()
    assert URL not in str(nodes.NODE_ROUTES)
    assert 'fixture-secret' not in json.dumps(nodes.audit_list(100, db=main.db))


def test_no_mcp_settings_tool():
    from app import mcp
    source = Path(__file__).resolve().parents[1] / 'scripts/ccboard_mcp.py'
    spec = importlib.util.spec_from_file_location('widen_shim', source)
    shim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shim)
    assert 'settings' not in json.dumps(mcp.TOOLS) and 'settings' not in json.dumps(shim.TOOLS)
    assert '/api/settings/' not in source.read_text()


def test_file_opt_in_is_rechecked_and_doctor_informs(board, monkeypatch):
    from app import doctor
    trusted(board, CCBOARD_PAGE_WIDEN='1')
    pending = put(board, {HOUR: '7'})
    assert pending.status_code == 409
    monkeypatch.setattr(doctor, 'settings', loaded(board))
    status, detail, _ = doctor._c_settings_page(None)
    assert status == 'pass' and 'Information: Settings can widen agent permissions' in detail
    trusted(board, CCBOARD_PAGE_WIDEN='0')
    assert put(board, {HOUR: '7'}, confirm=pending.json()['phrase']).status_code == 403
    assert not board[1].page_file.exists()


def test_confirmed_stale_stamp_and_direct_write_race(board, monkeypatch):
    from app import platform as plat
    trusted(board, CCBOARD_PAGE_WIDEN='1')
    stamp = board[0].get(URL, headers=PERSON).json()['stamp']
    assert put(board, {HOUR: '7'}, confirm='ALLOW WORKER', stamp=stamp).status_code == 200
    assert put(board, {HOUR: '8'}, confirm='ALLOW WORKER', stamp=stamp).status_code == 409
    real = plat.secure_write
    def race(path, data, mode, *, still):
        trusted(board, CCBOARD_PAGE_WIDEN='0')
        return real(path, data, mode, still=still)
    monkeypatch.setattr(plat, 'secure_write', race)
    assert put(board, {HOUR: '8'}, confirm='ALLOW WORKER').status_code == 409
    assert json.loads(board[1].page_file.read_text())['set'][HOUR] == '7'


def test_large_widening_audit_keeps_all_keys_and_values(board):
    from app import main, nodes
    trusted(board, CCBOARD_PAGE_WIDEN='1')
    changes = {WORKER: '1', WRITES: '1', REPOS: ','.join('sample/project' + str(n) for n in range(25)),
               'CCBOARD_EPIC_AUTHORS': ','.join('author' + str(n) for n in range(20)), HOUR: '60',
               'CCBOARD_EPIC_WORKER_MAX_PER_DAY': '500', 'CCBOARD_NODE_LANES': '0', 'CCBOARD_SUBAGENT_MODEL': 'opus'}
    phrase = put(board, changes).json()['phrase']
    assert put(board, changes, confirm=phrase).status_code == 200
    row = nodes.audit_list(db=main.db, action='settings_widen')[0]
    assert all(key in row['detail'] and value in row['detail'] for key, value in changes.items())
    assert 'phrase typed' in row['detail'] and phrase not in row['detail']


def test_local_signin_can_confirm(board, monkeypatch):
    from app import hooks, local_signin as local
    trusted(board, CCBOARD_PAGE_WIDEN='1')
    c, settings, _, _ = board
    for target in {id(settings): settings, id(local.settings): local.settings}.values():
        for name, value in (("local_signin", True), ("local_signin_user", "alice"), ("local_signin_hours", 12), ("allowed_users", {"alice"})):
            monkeypatch.setattr(target, name, value)
    monkeypatch.setattr(c._transport, 'client', ('127.0.0.1', 50000))
    c.base_url = f'http://127.0.0.1:{settings.port}'
    local.clear()
    try:
        token = c.post('/auth/local/token', headers={hooks.TOKEN_HEADER: hooks.ensure_token()}).headers[local.TOKEN_HEADER]
        assert c.post('/auth/local/redeem', content=token, headers={'X-CCBoard': '1'}).status_code == 204
        headers = {'X-CCBoard': '1', 'Origin': str(c.base_url).rstrip('/')}
        answer = c.put(URL, headers=headers, json={'changes': {HOUR: '7'}, 'confirm': 'ALLOW WORKER'})
        assert answer.status_code == 200
    finally:
        local.clear()


@pytest.mark.parametrize('key,value', [(HOUR, '61'), ('CCBOARD_EPIC_WORKER_MAX_PER_DAY', '501'),
                                      ('CCBOARD_NODE_LANES', '1000'), ('CCBOARD_SUBAGENT_MODEL', 'bad model')])
def test_confirm_values_still_use_startup_validation(board, key, value):
    trusted(board, CCBOARD_PAGE_WIDEN='1')
    answer = put(board, {key: value}, confirm='ALLOW WORKER')
    assert answer.status_code == 400 and answer.json()['key'] == key
    assert not board[1].page_file.exists()


def test_confirmation_prompt_is_not_a_refusal(board):
    from app import main, nodes
    trusted(board, CCBOARD_PAGE_WIDEN='1')
    assert put(board, {HOUR: '7'}).status_code == 409
    assert nodes.audit_list(db=main.db, action='settings_refused') == []
    assert put(board, {HOUR: '7'}, confirm='ALLOW WORKER').status_code == 200
    assert nodes.audit_list(db=main.db, action='settings_refused') == []
    assert len(nodes.audit_list(db=main.db, action='settings_widen')) == 1


def test_bad_phrase_refusal_names_option(board):
    from app import main, nodes
    trusted(board, CCBOARD_PAGE_WIDEN='1')
    answer = put(board, {HOUR: '7'}, confirm='wrong')
    assert answer.status_code == 403
    assert answer.json()['key'] == HOUR
    assert HOUR in nodes.audit_list(db=main.db, action='settings_refused')[0]['detail']


@pytest.mark.parametrize('on', [False, True])
def test_save_preserves_untouched_raw_entries_and_confirmation_marks(board, on):
    trusted(board, CCBOARD_PAGE_WIDEN='1' if on else '0')
    values = {WORKER: '1', HOUR: '9', 'CCBOARD_UNKNOWN_OPTION': 'future',
              'CCBOARD_NODE_TAGS': 'tag:keep', 'CCBOARD_AUTOCLOSE_GRACE': '5'}
    marks = [WORKER, 'CCBOARD_UNKNOWN_OPTION', 'CCBOARD_NODE_TAGS']
    page_file(board, values, marks)
    assert put(board, {'CCBOARD_AUTOCLOSE_GRACE': '10'}).status_code == 200
    saved = json.loads(board[1].page_file.read_text())
    assert saved['set'] == {**values, 'CCBOARD_AUTOCLOSE_GRACE': '10'}
    assert saved['confirmed'] == marks  # In particular, never arm the unconfirmed hour entry.
    assert put(board, {'CCBOARD_AUTOCLOSE_GRACE': None}).status_code == 200
    assert json.loads(board[1].page_file.read_text())['set'] == {k: v for k, v in values.items() if k != 'CCBOARD_AUTOCLOSE_GRACE'}


@pytest.mark.parametrize('key,value,wider', [
    (WORKER, '1', True), (WRITES, '1', True), (REPOS, 'sample/project', True),
    ('CCBOARD_EPIC_AUTHORS', 'alice', True), (HOUR, '7', True),
    ('CCBOARD_EPIC_WORKER_MAX_PER_DAY', '31', True), ('CCBOARD_NODE_LANES', '0', True),
    ('CCBOARD_SUBAGENT_MODEL', 'opus', True), (HOUR, '5', False), (HOUR, '6', False),
    ('CCBOARD_NODE_LANES', '2', False), ('CCBOARD_SUBAGENT_MODEL', 'inherit', False),
])
def test_live_widening_notes_when_dormant_value_is_armed(board, monkeypatch, key, value, wider):
    from app import doctor
    page_file(board, {key: value}, [key])
    dormant = loaded(board)
    trusted(board, CCBOARD_PAGE_WIDEN='1')
    # A pending wider value is not yet a live one.
    assert not next(r for r in options.listing(dormant)['options'] if r['key'] == key).get('live_widening')
    live = loaded(board)
    row = next(r for r in options.listing(live)['options'] if r['key'] == key)
    assert bool(row.get('live_widening')) is wider
    monkeypatch.setattr(doctor, 'settings', live)
    detail = doctor._c_settings_page(None)[1]
    assert (key in detail) is wider
    if wider:
        assert 'Live page values widen settings file/environment permissions' in detail
        # Still visible when turning off the switch is pending a restart.
        trusted(board, CCBOARD_PAGE_WIDEN='0')
        assert next(r for r in options.listing(live)['options'] if r['key'] == key)['live_widening']
        assert key in doctor._c_settings_page(None)[1]
