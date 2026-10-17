"""P5: saved worker setup, pure refusal checks, person gates and no live side effects."""
import json
import threading

import pytest

from app import config, github_write, main, nodes, nodes_epics, nodes_worker, options, prpoll

URL = '/api/settings/options'
PERSON = {'Tailscale-User-Login': 'owner', 'X-CCBoard': '1', 'Origin': 'http://testserver'}
WORKER = 'CCBOARD_EPIC_WORKER'
REPOS = 'CCBOARD_EPIC_REPOS'
AUTHORS = 'CCBOARD_EPIC_AUTHORS'
WRITES = 'CCBOARD_EPIC_WRITES'
POLL = 'CCBOARD_EPIC_WORKER_POLL'
HOUR = 'CCBOARD_EPIC_WORKER_MAX_PER_HOUR'
DAY = 'CCBOARD_EPIC_WORKER_MAX_PER_DAY'
READY = {WORKER: '1', REPOS: 'owner/name', AUTHORS: 'owner:1', WRITES: '1'}


@pytest.fixture
def board(lite_client, tmp_path, monkeypatch):
    file = tmp_path / 'env'
    file.write_text('CCBOARD_PAGE_WIDEN=1\n')
    file.chmod(0o600)
    env = {'CCBOARD_ENV_FILE': str(file), 'CCBOARD_PAGE_FILE': str(tmp_path / 'settings.page.json'),
           'CCBOARD_DATA_DIR': str(tmp_path), 'CCBOARD_ALLOWED_USERS': 'owner'}
    s = config.Settings(env)
    for module in (main, nodes_worker, nodes_epics, github_write):
        monkeypatch.setattr(module, 'settings', s)
    monkeypatch.setattr(main, 'node_worker', None)
    gh = tmp_path / 'bin'
    gh.mkdir()
    (gh / 'gh').write_text('#!/bin/sh\nexit 91\n')
    (gh / 'gh').chmod(0o700)
    monkeypatch.setenv('PATH', str(gh))
    def no_gh(*args, **kwargs):
        pytest.fail('settings must not probe GitHub')
    monkeypatch.setattr(nodes_epics, '_gh_login', no_gh)
    return lite_client, s, env, file


def put(board, changes, **extra):
    return board[0].put(URL, headers=PERSON, json={'changes': changes, **extra})


def trust(board, **values):
    board[3].write_text('\n'.join(k + '=' + v for k, v in {'CCBOARD_PAGE_WIDEN': '1', **values}.items()) + '\n')


@pytest.mark.parametrize('missing,reason', [(REPOS, 'repos_empty'), (AUTHORS, 'authors_empty'), (WRITES, 'writes_off'), (None, None)])
def test_pure_check_agrees_with_real_refusal(board, monkeypatch, missing, reason):
    values = {**READY, **({missing: '' if missing != WRITES else '0'} if missing else {})}
    s = config.Settings({**board[2], **values})
    for module in (nodes_worker, nodes_epics, github_write):
        monkeypatch.setattr(module, 'settings', s)
    worker = nodes_worker.Worker(main.db)
    worker._probed = True
    assert worker.refusal(probe=False) == options.worker_next(s)['reason'] == reason
    assert worker._thread is None
    assert main.db.kv_get(nodes_worker.KV) is None


def test_cached_login_is_accepted_without_probe(board, monkeypatch):
    trust(board, **{WORKER: '1', REPOS: 'owner/name', WRITES: '1'})
    monkeypatch.setattr(prpoll, 'gh_identity_cached', lambda: ('owner', 1))
    s = config.Settings(board[2])
    assert options.worker_next(s, 'owner')['reason'] is None
    result = board[0].get(URL, headers=PERSON).json()
    assert result['worker']['authors'] == ['owner']
    assert result['worker']['text'] == 'After the restart the worker will start'
    assert put(board, {WORKER: '1'}, confirm='ALLOW WORKER').status_code == 200


@pytest.mark.parametrize('key', sorted(options.WORKER_KEYS))
def test_master_off_refuses_every_worker_key(board, key):
    trust(board, CCBOARD_PAGE_WIDEN='0')
    value = {**READY, POLL: '90', HOUR: '6', DAY: '30'}[key]
    answer = put(board, {key: value})
    assert answer.status_code == 403
    assert answer.json()['reason'] == 'widen_off'
    assert not board[1].page_file.exists()
    assert put(board, {key: 'invalid'}).status_code == 403


def test_typed_roundtrip_loads_all_values_and_pending_badges(board):
    trust(board, **{WRITES: '1'})
    changes = {**READY, REPOS: 'OWNER/name,owner/other', AUTHORS: 'owner:1,other', POLL: '120', HOUR: '8', DAY: '40'}
    response = put(board, changes)
    assert response.status_code == 409
    detail = response.json()
    assert detail['phrase'] == 'ALLOW WORKER'
    assert detail['safety'] == options.WORKER_SAFETY
    assert detail['worker']['repos'] == ['owner/name', 'owner/other']
    assert detail['worker']['authors'] == ['owner:1', 'other']
    assert put(board, changes, confirm='ALLOW WORKER ').status_code == 403
    answer = put(board, changes, confirm='ALLOW WORKER')
    assert answer.status_code == 200
    s = config.Settings(board[2])
    assert s.epic_worker and s.epic_repos == ('owner/name', 'owner/other')
    assert s.epic_author_ids == {'owner': 1}
    assert (s.epic_worker_poll, s.epic_worker_max_hour, s.epic_worker_max_day) == (120, 8, 40)
    assert set(answer.json()['pending']) >= set(changes)
    assert not board[1].epic_worker


@pytest.mark.parametrize('key,word', [(REPOS, 'no repository is listed'), (AUTHORS, 'no allowed author known'), (WRITES, 'epic writes off')])
def test_missing_prerequisite_and_atomic_batch(board, key, word):
    values = {**READY, key: '0' if key == WRITES else ''}
    answer = put(board, values, confirm='ALLOW GITHUB WRITES AND ALLOW WORKER')
    assert answer.status_code == 400
    assert answer.json()['reason'] == (options.AUTHORS_PENDING if key == AUTHORS else 'After the restart the worker will refuse to start: ' + word)
    assert not board[1].page_file.exists()
    confirm = put(board, READY)
    assert confirm.status_code == 409
    assert confirm.json()['phrase'] == 'ALLOW GITHUB WRITES AND ALLOW WORKER'
    assert put(board, READY, confirm=confirm.json()['phrase']).status_code == 200
    # Removing the last prerequisite while still on must not save a refusal either.
    assert put(board, {key: values[key]}).status_code == 400
    assert config.Settings(board[2]).epic_worker


@pytest.mark.parametrize('key,value', [(POLL, '29'), (POLL, '3601'), (HOUR, '0'), (HOUR, '61'), (DAY, '0'), (DAY, '501'),
                                      (REPOS, 'invalid'), (REPOS, ','.join('owner/r' + str(i) for i in range(26))),
                                      (AUTHORS, 'two words')])
def test_bounds_and_lists_name_the_field(board, key, value):
    answer = put(board, {key: value})
    assert answer.status_code == 400 and answer.json()['key'] == key
    assert not board[1].page_file.exists()


def test_narrowing_needs_no_phrase_raising_does(board):
    trust(board, **{**READY, REPOS: 'owner/name,owner/other', AUTHORS: 'owner:1,other', HOUR: '9', DAY: '40'})
    for changes in ({REPOS: 'owner/name'}, {AUTHORS: 'owner:1'}, {HOUR: '5'}, {DAY: '20'}, {WORKER: '0'}):
        assert put(board, changes).status_code == 200
    for key in (HOUR, DAY):
        assert put(board, {key: '30'}).status_code == 409
    assert put(board, {WORKER: '1'}).status_code == 409


def test_saving_off_leaves_running_worker_and_live_controls(board, monkeypatch):
    trust(board, **READY)
    running = config.Settings(board[2])
    for module in (main, nodes_worker, nodes_epics, github_write):
        monkeypatch.setattr(module, 'settings', running)
    worker = nodes_worker.Worker(main.db)
    monkeypatch.setattr(main, 'node_worker', worker)
    monkeypatch.setattr(worker, 'running', lambda: True)
    before = board[0].get('/api/epic-worker', headers=PERSON).json()
    answer = put(board, {WORKER: '0'})
    assert answer.status_code == 200
    assert answer.json()['worker']['on'] is False
    assert answer.json()['worker_live']['on'] is True
    assert answer.json()['worker_live']['state'] == before['state'] == 'idle'
    assert running.epic_worker and not config.Settings(board[2]).epic_worker
    assert worker.running()
    assert board[0].get('/api/epic-worker', headers=PERSON).json()['on'] is True


def test_off_creates_no_worker_thread_row_or_state_key(board):
    before = set(threading.enumerate())
    assert put(board, {WORKER: '0'}).status_code == 200
    result = board[0].get('/api/state', headers=PERSON).json()
    assert 'epic_worker' not in result and 'epic_worker' not in result.get('node', {})
    assert main.node_worker is None and main.db.kv_get(nodes_worker.KV) is None
    assert not [t for t in threading.enumerate() if t not in before and t.name == 'epic-worker']


@pytest.mark.parametrize('key', sorted(options.WORKER_KEYS))
def test_only_person_can_save_worker_keys(board, key):
    from app import hooks, mcp_tokens
    value = {**READY, POLL: '90', HOUR: '6', DAY: '30'}[key]
    token, _ = mcp_tokens.mint('fixture-device')
    callers = [{}, {**PERSON, hooks.TOKEN_HEADER: hooks.ensure_token()},
               {**PERSON, 'X-CCBoard-Hub': 'fixture'}, {**PERSON, 'Authorization': 'Bearer ' + token},
               {**PERSON, 'Origin': 'null'}]
    for scope in nodes.SCOPES:
        _, node_token = nodes.add_incoming(scopes=[scope], db=main.db)
        callers.append({**PERSON, 'Authorization': 'Bearer ' + node_token})
    for headers in callers:
        answer = board[0].put(URL, headers=headers, json={'changes': {key: value}, 'confirm': 'ALLOW WORKER'})
        assert answer.status_code in (401, 403)
    assert not board[1].page_file.exists()
    assert URL not in str(nodes.NODE_ROUTES)


def test_existing_phase_four_space_separated_authors_still_load(board):
    board[1].page_file.write_text(json.dumps({'version': 1, 'set': {AUTHORS: 'owner other'}, 'confirmed': [AUTHORS]}))
    board[1].page_file.chmod(0o600)
    assert config.Settings(board[2]).epic_authors == ('owner', 'other')


def test_worker_keeps_probe_order_and_pending_login_state(board, monkeypatch):
    worker = nodes_worker.Worker(main.db)
    monkeypatch.setattr(board[1], 'epic_writes', False)
    monkeypatch.setattr(board[1], 'epic_repos', ())
    assert worker.refusal() == 'writes_off'
    monkeypatch.setattr(board[1], 'epic_writes', True)
    assert worker.refusal() == 'repos_empty'
    monkeypatch.setattr(board[1], 'epic_repos', ('owner/name',))
    assert worker.refusal(probe=False) == 'login_pending'
    worker._probed = True
    assert worker.refusal(probe=False) == 'authors_empty'


AUTHORS_SENTENCE = "No author is listed and this board's gh login is not known yet. Type your GitHub login in Allowed authors."


def test_cold_login_cache_only_blocks_changes_that_need_an_author(board, monkeypatch):
    monkeypatch.setattr(prpoll, 'gh_identity_cached', lambda: (None, None))
    # Saved state: worker on, writes on, repository listed, no author listed, nothing cached.
    trust(board, **{WORKER: '1', REPOS: 'owner/name', WRITES: '1'})
    assert options.AUTHORS_PENDING == AUTHORS_SENTENCE
    # Unrelated edits save: the real worker probes gh at start.
    for changes in ({POLL: '120'}, {HOUR: '5'}, {DAY: '20'}):
        assert put(board, changes).status_code == 200, changes
    # Touching what the worker needs while it is on is refused with the plain sentence.
    for changes in ({AUTHORS: ''}, {REPOS: 'owner/name,owner/other'}, {WRITES: '1'}, {WORKER: '1'}):
        answer = put(board, changes, confirm='ALLOW WORKER')
        assert answer.status_code == 400, changes
        assert answer.json()['reason'] == AUTHORS_SENTENCE
    # Typing the login makes the same worker savable.
    assert put(board, {AUTHORS: 'owner'}, confirm='ALLOW WORKER').status_code == 200


def test_cold_login_cache_refuses_turning_the_worker_on(board, monkeypatch):
    monkeypatch.setattr(prpoll, 'gh_identity_cached', lambda: (None, None))
    trust(board, **{REPOS: 'owner/name', WRITES: '1'})
    answer = put(board, {WORKER: '1'}, confirm='ALLOW WORKER')
    assert answer.status_code == 400 and answer.json()['reason'] == AUTHORS_SENTENCE
    assert not board[1].page_file.exists()
    # Other edits while the worker stays off are never blocked.
    assert put(board, {POLL: '120'}).status_code == 200
    assert put(board, {WORKER: '1', AUTHORS: 'owner'}, confirm='ALLOW WORKER').status_code == 200


def test_clearing_authors_hands_trust_to_gh_login_and_needs_the_phrase(board, monkeypatch):
    monkeypatch.setattr(prpoll, 'gh_identity_cached', lambda: ('owner', 1))
    trust(board, **{WORKER: '1', REPOS: 'owner/name', WRITES: '1', AUTHORS: 'bob'})
    answer = put(board, {AUTHORS: ''})
    assert answer.status_code == 409 and answer.json()['phrase'] == 'ALLOW WORKER'
    assert answer.json()['widens'][0]['key'] == AUTHORS
    assert put(board, {AUTHORS: ''}, confirm='ALLOW WORKER').status_code == 200


def test_next_start_line_says_a_kept_stop_stays_stopped(board, monkeypatch):
    monkeypatch.setattr(prpoll, 'gh_identity_cached', lambda: ('owner', 1))
    trust(board, **READY)
    s = config.Settings(board[2])
    assert options.worker_next(s, 'owner')['text'] == 'After the restart the worker will start'
    assert options.worker_next(s, 'owner', True)['text'] == 'After the restart the worker will start, but it stays stopped until you press Start worker'
    assert options.worker_saved(s, main.db)['text'] == 'After the restart the worker will start'
    main.db.kv_set(nodes_worker.KV, {'stopped': True, 'stopped_at': 1.0})
    before = main.db.kv_get(nodes_worker.KV)
    listing = board[0].get(URL, headers=PERSON).json()['worker']
    assert listing['text'] == 'After the restart the worker will start, but it stays stopped until you press Start worker'
    assert listing['stopped'] is True
    assert main.db.kv_get(nodes_worker.KV) == before and main.node_worker is None
    # A refusal wins over the stopped note, and an off worker says nothing about it.
    off = config.Settings({**board[2], **READY, WRITES: '0'})
    assert 'refuse to start' in options.worker_next(off, 'owner', True)['text']
    assert options.worker_next(config.Settings({**board[2], WORKER: '0'}), 'owner', True)['text'] == 'After the restart the worker will be off'
