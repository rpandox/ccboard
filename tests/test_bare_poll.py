"""Device screen polling and answers: scripted transports only, no real device or home."""
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess

import pytest

from app import bare, main, permissions
from app.agents import codex_pane
from .test_bare_sessions import device  # noqa: F401

FIXTURES = Path(__file__).parent / 'fixtures'
CLAUDE = (FIXTURES / 'agents/claude/approval_dialog.txt').read_text()
CODEX = (FIXTURES / 'agents/codex/approval_dialog.txt').read_text()


@pytest.fixture(autouse=True)
def pushes(monkeypatch):
    out = []
    monkeypatch.setattr(permissions, 'push_request', lambda *args, **kw: out.append((args, kw)))
    return out


def seed(agent='claude', count=1, host='device-1'):
    names = [f'bare-{i:032x}' for i in range(count)]
    for name in names:
        main.db.add_bare_session(host=host, tmux_name=name, repo='repo one', agent=agent, cmd='fixture')
    return names


def capture(names, text=CLAUDE, command='claude', alive=None):
    return (0, '\n'.join(names if alive is None else alive) + '\0' +
            ''.join(name + '\0' + command + '\0' + text + '\0' for name in names), '')


def poll(fake, headers, names, text=CLAUDE, command='claude'):
    fake.answers.append(capture(names, text, command))
    bare.poll_device(main.db, headers['Tailscale-User-Login'], 'device-1')


def item(headers):
    return bare.pending(main.db, headers['Tailscale-User-Login'])[0]


def path(name, verb):
    return '/api/bare/device-1/sessions/' + name + '/' + verb


@pytest.mark.parametrize('agent,screen,state', [
    ('claude', CLAUDE, 'dialog'), ('codex', CODEX, 'dialog'),
    ('codex', (FIXTURES / 'agents/codex/pane_working.txt').read_text(), 'working'),
    ('claude', (FIXTURES / 'agents/claude/pane_working.txt').read_text(), 'working'),
    ('claude', (FIXTURES / 'agents/claude/pane_waiting.txt').read_text(), 'waiting'),
    ('codex', (FIXTURES / 'codex_0161/pane_trust_answered_working.txt').read_text(), 'waiting'),
    ('codex', (FIXTURES / 'codex_0161/pane_trust_answered_footer.txt').read_text(), 'waiting'),
    ('codex', (FIXTURES / 'codex_0161/pane_trust_dialog.txt').read_text(), 'dialog'),
])
def test_screen_states(lite_client, bare_runner, device, agent, screen, state):
    names = seed(agent)
    poll(bare_runner, device[0], names, screen, agent)
    assert main.db.bare_open_rows('device-1')[names[0]]['state'] == state
    assert not main.db.node_audit_list()
    assert main.db.open_rows() == {}


def test_one_batch_socket_and_pending_path(lite_client, bare_runner, device, pushes):
    names = seed(count=3)
    poll(bare_runner, device[0], names)
    assert len(bare_runner.calls) == 1
    words = shlex.split(bare_runner.calls[0][0][-1])
    assert words == ['sh', '-c', bare.POLL, 'bare-poll', bare.session_socket(), *names]
    assert 'list-sessions' in words[2] and "'#{pane_current_command}'" in words[2] and 'capture-pane -p -S -40' in words[2]
    assert bare_runner.calls[0][1] is None
    ids = [p['id'] for p in bare.pending(main.db, device[0]['Tailscale-User-Login'])]
    assert len(ids) == 3 and len(pushes) == 3
    poll(bare_runner, device[0], names)
    assert len(pushes) == 3
    pending = lite_client.get('/api/state', headers=device[0]).json()['pending_permissions']
    assert [p['id'] for p in pending] == ids
    assert all(p['kind'] == 'terminal_prompt' and p['bare_device'] == 'device-1' for p in pending)
    assert not bare.pending(main.db, 'another-person')
    assert not main._pending_permissions()  # never exported through a paired board


@pytest.mark.parametrize('agent,screen,verb,key,enter', [
    ('claude', CLAUDE, 'allow', '2', False), ('claude', CLAUDE, 'deny', '4', False),
    ('codex', CODEX, 'allow', '1', True), ('codex', CODEX, 'deny', '3', True),
])
def test_answers_recapture_consume_and_audit(lite_client, bare_runner, device, agent, screen, verb, key, enter):
    names = seed(agent)
    poll(bare_runner, device[0], names, screen, agent)
    pr = item(device[0])
    bare_runner.answers.extend([capture(names, screen, agent), (0, '', '')])
    r = lite_client.post(path(names[0], verb), headers=device[0], json={'pid': pr['id']})
    assert r.status_code == 200, r.text
    remote = bare_runner.calls[-1][0][-1]
    assert '-l ' + key in remote and ('Enter' in remote) == enter
    assert bare_runner.calls[-1][1] is None
    poll(bare_runner, device[0], names, screen, agent)
    assert not bare.pending(main.db, device[0]['Tailscale-User-Login'])
    bare_runner.answers.append(capture(names, screen, agent))
    assert lite_client.post(path(names[0], verb), headers=device[0], json={'pid': pr['id']}).status_code == 409
    audit = main.db.node_audit_list()[-1]
    assert (audit['action'], audit['detail'], audit['peer'], audit['target'], audit['user']) == (
        'bare_answer', verb, 'device-1', names[0], device[0]['Tailscale-User-Login'])
    assert 'git add' not in str(main.db.node_audit_list())


@pytest.mark.parametrize('screen', [CLAUDE.replace('git add src', 'git add other'), '❯ \n? for shortcuts', ''])
def test_changed_or_gone_prompt_sends_nothing(lite_client, bare_runner, device, screen):
    names = seed()
    poll(bare_runner, device[0], names)
    pid = item(device[0])['id']
    bare_runner.answers.append(capture(names, screen))
    r = lite_client.post(path(names[0], 'allow'), headers=device[0], json={'pid': pid})
    assert r.status_code == 409 and 'nothing was sent' in r.text
    assert len(bare_runner.calls) == 2


def test_reply_stdin_only(lite_client, bare_runner, device):
    names = seed()
    text = "Review this change\nwith 'quotes' and $(literal) text"
    bare_runner.answers.extend([capture(names, '❯ \n? for shortcuts'), (0, '', '')])
    r = lite_client.post(path(names[0], 'reply'), headers=device[0], json={'text': text})
    assert r.status_code == 200, r.text
    argv, stdin = bare_runner.calls[-1]
    assert stdin == text and text not in str(argv)
    sh, flag, script = shlex.split(argv[-1])
    assert (sh, flag) == ('sh', '-c')
    assert 'load-buffer -b' in script and 'paste-buffer -d -p -b' in script and ' && ' in script
    chain, tail = script.split('; rc=$?; ')
    assert chain.endswith('Enter') and tail.endswith(' 2>/dev/null; exit $rc')
    sock = bare.session_socket()
    buffer = shlex.split(chain)[shlex.split(chain).index('-b') + 1]
    assert tail == f'tmux -L {sock} delete-buffer -b {buffer} 2>/dev/null; exit $rc'
    assert re.fullmatch(r'bare-[0-9a-f]{32}', buffer) and buffer != names[0]
    assert 'Review this change' not in str(main.db.node_audit_list())
    assert main.db.node_audit_list()[0]['detail'] == 'reply'


@pytest.mark.parametrize('text', ['bad\x1b[31m', 'bad\u009b', 'bad\u202e', '', 'x' * 16001])
def test_reply_validation_before_transport(lite_client, bare_runner, device, text):
    name, = seed()
    assert lite_client.post(path(name, 'reply'), headers=device[0], json={'text': text}).status_code == 400
    assert not bare_runner.calls


@pytest.mark.parametrize('text,command', [(CLAUDE, 'claude'), ('thinking (esc to interrupt)', 'claude'), ('❯ ', 'sh')])
def test_reply_never_types_into_dialog_work_or_exited_agent(lite_client, bare_runner, device, text, command):
    names = seed()
    bare_runner.answers.append(capture(names, text, command))
    assert lite_client.post(path(names[0], 'reply'), headers=device[0], json={'text': 'continue'}).status_code == 409
    assert len(bare_runner.calls) == 1


@pytest.mark.parametrize('change', [{'Online': False}, {'ShareeNode': True}, {'UserID': 2}])
def test_not_ready_never_dialled(lite_client, bare_runner, device, change):
    name, = seed()
    device[1].update(change)
    worker = bare.Poller(main.db)
    worker.tick(0)
    assert not bare_runner.calls
    assert worker.due['device-1'] == (10, 1)
    assert lite_client.post(path(name, 'reply'), headers=device[0], json={'text': 'continue'}).status_code == 400
    assert not bare_runner.calls


def test_poll_lifecycle_backoff_reconcile(lite_client, bare_runner, device):
    worker = bare.Poller(main.db)
    worker.tick(0)
    assert not bare_runner.calls
    names = seed()
    for now, due in [(0, 10), (10, 40), (40, 100), (100, 220), (220, 520), (520, 820)]:
        bare_runner.answers.append(subprocess.TimeoutExpired(['fake'], 12))
        worker.tick(now)
        assert worker.due['device-1'][0] == due
        before = len(bare_runner.calls)
        worker.tick(now + 1)
        assert len(bare_runner.calls) == before
    assert main.db.bare_open_rows('device-1')[names[0]]['state'] == 'unconfirmed'
    bare_runner.answers.append(capture(names))
    worker.tick(820)
    assert worker.due['device-1'] == (830, 0)
    bare_runner.answers.append(capture([]))
    worker.tick(830)
    assert not main.db.bare_open_rows('device-1')
    assert not bare.pending(main.db, device[0]['Tailscale-User-Login'])
    before = len(bare_runner.calls)
    worker.tick(840)
    assert len(bare_runner.calls) == before and not worker.due
    assert not main.db.node_audit_list()


@pytest.mark.parametrize('answer', [(0, 'truncated', ''), (0, '\0bad\0cmd\0screen\0', ''), (255, '', '')])
def test_partial_poll_preserves_all_rows(lite_client, bare_runner, device, answer):
    seed(count=2)
    before = main.db.bare_open_rows('device-1')
    bare_runner.answers.append(answer)
    bare.Poller(main.db).tick(0)
    assert main.db.bare_open_rows('device-1') == before


@pytest.mark.parametrize('verb,body', [('allow', {'pid': 1}), ('deny', {'pid': 1}), ('reply', {'text': 'continue'})])
def test_person_and_csrf_only(lite_client, bare_runner, device, verb, body):
    name, = seed()
    assert lite_client.post(path(name, verb), json=body).status_code == 403
    assert lite_client.post(path(name, verb), headers={'Tailscale-User-Login': device[0]['Tailscale-User-Login']}, json=body).status_code == 403
    assert not bare_runner.calls


def test_permission_push_route_reuses_device_answer(lite_client, bare_runner, device):
    names = seed()
    poll(bare_runner, device[0], names)
    pid = item(device[0])['id']
    bare_runner.answers.extend([capture(names), (0, '', '')])
    assert lite_client.post(f'/api/permission/{pid}/allow', headers=device[0]).status_code == 200


def test_poll_preserves_stopping(lite_client, bare_runner, device):
    names = seed()
    main.db.bare_set_state('device-1', names[0], 'stopping', 'bare_stop')
    before = main.db.bare_open_rows('device-1')
    poll(bare_runner, device[0], names)
    assert main.db.bare_open_rows('device-1') == before and not bare.pending(main.db, device[0]['Tailscale-User-Login'])


@pytest.mark.parametrize('screen', [CODEX.replace('Yes, proceed (y)', 'Yes, always (y)'), CODEX + '\n› new turn',
                                   CODEX.replace('1. Yes', '3. Yes')])
def test_codex_persistent_or_stale_menus_are_not_answerable(screen):
    assert codex_pane.approval_dialog(screen) is None


@pytest.mark.parametrize('verb', ['allow', 'deny', 'reply'])
def test_tokens_cannot_answer_even_with_person_headers(lite_client, bare_runner, device, verb):
    from app import hooks, mcp_tokens, nodes
    from .test_nodes_auth import mint
    name, = seed()
    _, node_token = mint(main.db, nodes.SCOPES)
    mcp_tokens.set_enabled(True)
    mcp_token, _ = mcp_tokens.mint('test-device')
    headers = [{'X-CCBoard': '1', 'X-CCBoard-Token': hooks.ensure_token()}]
    headers += [{'X-CCBoard': '1', 'Authorization': 'Bearer ' + t} for t in (node_token, mcp_token)]
    headers += [{**device[0], 'Authorization': 'Bearer ' + t} for t in (node_token, mcp_token)]
    for h in headers:
        body = {'text': 'continue'} if verb == 'reply' else {'pid': 1}
        assert lite_client.post(path(name, verb), headers=h, json=body).status_code == 403
    assert not bare_runner.calls


@pytest.mark.parametrize('failure', [(255, '', ''), subprocess.TimeoutExpired(['fake'], 12)])
def test_lost_answer_is_consumed_not_retried(lite_client, bare_runner, device, failure):
    names = seed()
    poll(bare_runner, device[0], names)
    pid = item(device[0])['id']
    bare_runner.answers.extend([capture(names), failure])
    r = lite_client.post(path(names[0], 'allow'), headers=device[0], json={'pid': pid})
    assert r.status_code == 400 and 'unconfirmed' in r.text
    assert main.db.node_audit_list()[0]['status'] == 'unconfirmed'
    bare_runner.answers.append(capture(names))
    assert lite_client.post(path(names[0], 'allow'), headers=device[0], json={'pid': pid}).status_code == 409
    assert sum('send-keys' in c[0][-1] for c in bare_runner.calls) == 1


def test_new_episode_of_identical_menu_can_be_answered(lite_client, bare_runner, device):
    names = seed()
    poll(bare_runner, device[0], names)
    pid = item(device[0])['id']
    bare_runner.answers.extend([capture(names), (0, '', '')])
    lite_client.post(path(names[0], 'deny'), headers=device[0], json={'pid': pid})
    poll(bare_runner, device[0], names, '❯ \n? for shortcuts')
    poll(bare_runner, device[0], names)
    assert item(device[0])['id'] != pid


@pytest.mark.parametrize('change', ['unchecked', 'removed', 'other-owner', 'person-removed'])
def test_poller_skips_unready_records(lite_client, bare_runner, device, monkeypatch, change):
    from app.config import settings
    seed()
    saved = main.db.kv_get(bare.PREFIX + 'device-1')['value']
    if change == 'unchecked':
        saved['check']['ok'] = False
    elif change == 'other-owner':
        saved['owner'] = 'another-person'
    elif change == 'person-removed':
        monkeypatch.setattr(settings, 'allowed_users', set())
    main.db.kv_set(bare.PREFIX + 'device-1', saved)
    if change == 'removed':
        main.db.kv_del(bare.PREFIX + 'device-1')
    bare.Poller(main.db).tick(0)
    assert not bare_runner.calls


def test_poll_batch_script_with_shell_function_fake_only(tmp_path):
    """Exercise quoting/framing with a shell function named tmux, never an executable or socket."""
    name = 'bare-' + '0' * 32
    script = '''tmux() {
      [ "$1" = '-L' ] && [ "$2" = 'ccboard-bare-00000000' ] || return 99
      case "$3" in
        list-sessions) printf '%s\\n%s\\n' "$SESSION" unrelated ;;
        display) [ "$6" = "=$SESSION:" ] || return 98; printf 'claude\\n' ;;
        capture-pane) [ "$8" = "=$SESSION:" ] || return 97; printf '%s\\n' "$SCREEN" ;;
        *) return 96 ;;
      esac
    }
    ''' + bare.POLL
    result = subprocess.run(['/bin/sh', '-c', script, 'fake-poll', 'ccboard-bare-00000000', name],
                            cwd=tmp_path, env={'HOME': str(tmp_path), 'PATH': '', 'SESSION': name, 'SCREEN': CLAUDE},
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout == name + '\nunrelated\0' + name + '\0claude\0' + CLAUDE.rstrip('\n') + '\0'


def test_device_notification_links_to_its_card_not_a_host_or_local_terminal(lite_client, device, monkeypatch):
    from app import notify
    from app.config import settings
    from .test_notify import PUB
    monkeypatch.setattr(settings, 'public_url', PUB)
    notice = notify.build({'tmux_name': 'bare-' + '0' * 32, 'bare_device': 'device-1', 'project': 'Device',
                           'repo': 'repo one', 'agent': 'claude'}, None, 'waiting', 'permission_prompt', 'command',
                          {'id': 276000000001, 'summary': 'command'})
    assert notice.path == '/#/inbox'
    assert notice.actions[-1]['url'].endswith('/#/inbox')
    assert notice.web_actions[-1] == {'action': 'open', 'title': 'Needs you'}
    assert notice.actions[0]['url'].endswith('/api/permission/276000000001/allow')


def test_cached_pending_does_not_wait_for_device_io(lite_client, bare_runner, device):
    from concurrent.futures import ThreadPoolExecutor
    names = seed()
    poll(bare_runner, device[0], names)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with bare.LOCK:  # a device action/poll holds this while awaiting SSH
            future = pool.submit(bare.pending, main.db, device[0]['Tailscale-User-Login'])
            assert len(future.result(timeout=1)) == 1


def test_failed_paste_is_unconfirmed_and_the_command_still_deletes_the_buffer(lite_client, bare_runner, device):
    names = seed()
    bare_runner.answers.extend([capture(names, '❯ \n? for shortcuts'), (1, '', '')])
    r = lite_client.post(path(names[0], 'reply'), headers=device[0], json={'text': 'continue'})
    assert r.status_code == 400 and 'unconfirmed' in r.text
    script = shlex.split(bare_runner.calls[-1][0][-1])[2]
    assert script.index('delete-buffer') > script.index('paste-buffer') and script.endswith('; exit $rc')
    assert main.db.bare_open_rows('device-1')[names[0]]['state'] != 'working'


def test_poller_survives_database_and_record_errors(lite_client, bare_runner, device, monkeypatch):
    seed()
    worker = bare.Poller(main.db)

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError('database is locked')

    monkeypatch.setattr(bare, 'poll_device', locked)
    worker.tick(0)
    assert worker.due['device-1'] == (10, 1)
    worker.tick(10)
    assert worker.due['device-1'] == (40, 2)
    monkeypatch.setattr(bare, 'poll_device', lambda *a: (_ for _ in ()).throw(KeyError('user')))
    worker.tick(40)
    assert worker.due['device-1'] == (100, 3)

    class Stopper:
        def __init__(self):
            self.n = 0

        def is_set(self):
            return self.n >= 3

        def wait(self, seconds):
            self.n += 1

    ticks = []

    def tick(*args):
        ticks.append(1)
        raise sqlite3.OperationalError('database is locked')

    worker.tick = tick
    worker.stop = Stopper()
    worker.run()
    assert len(ticks) == 3
