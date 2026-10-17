"""Issue #277: the optional state log on a device. Scripted fake runner only: no ssh, tailscale or tmux; the shell scripts that
would run on the device run here against a temp HOME (and a fake `tmux` file in a temp PATH), never the real home."""
import json
import os
import shlex
import subprocess
import time
from types import SimpleNamespace

import pytest

from app import bare, main, permissions
from .test_bare_poll import CLAUDE, capture, item, path as route, poll, seed  # noqa: F401
from .test_bare_sessions import device  # noqa: F401

HOME = '/home/ccagent'
SESSION = 'bare-' + 'a' * 32
OTHER = 'bare-' + 'b' * 32
THEIRS = {'type': 'command', 'command': '/usr/local/bin/their-hook', 'timeout': 9}


@pytest.fixture(autouse=True)
def pushes(monkeypatch):
    out = []
    monkeypatch.setattr(permissions, 'push_request', lambda *args, **kw: out.append((args, kw)))
    return out


def merged(text, agent='claude', *, install=True, home=HOME):
    return bare.hook_merge(text, '~/' + bare.HOOK_FILES[agent], agent, home, install=install)


def ours(data):
    return [h['command'] for groups in data.get('hooks', {}).values() for g in groups for h in g['hooks'] if bare.HOOK_REL in h['command']]


# ---- settings merge ----

def test_install_keeps_theirs_and_adds_ours_once():
    theirs = {'model': 'x', 'hooks': {'Stop': [{'matcher': 'a', 'hooks': [THEIRS]}], 'PreToolUse': [{'hooks': [THEIRS]}]}}
    new = json.loads(merged(json.dumps(theirs)))
    assert new['model'] == 'x' and new['hooks']['PreToolUse'] == [{'hooks': [THEIRS]}]
    assert new['hooks']['Stop'][0] == {'matcher': 'a', 'hooks': [THEIRS]}          # their group is first and untouched
    assert sorted(new['hooks']) == sorted(['PreToolUse', *bare.HOOK_EVENTS])
    assert len(ours(new)) == 4 and len(set(ours(new))) == 4
    assert all(c.startswith(HOME + '/.ccboard-bare/hook claude ') for c in ours(new))
    sync = new['hooks']['PermissionRequest'][0]['hooks'][0]
    assert 'async' not in sync and new['hooks']['Stop'][1]['hooks'][0]['async'] is True
    assert merged(json.dumps(new)) is None                                           # idempotent: a second install changes nothing
    assert merged(json.dumps(new, indent=2) + '\n') is None


def test_install_into_empty_or_missing_settings_and_codex_shape():
    for text in ('', '{}', '\n'):
        new = json.loads(merged(text, 'codex'))
        assert list(new) == ['hooks'] and len(ours(new)) == 4 and ours(new)[0].endswith(' codex SessionStart')


def test_install_replaces_a_stale_entry_in_place_without_a_duplicate():
    old = {'hooks': {'Stop': [{'hooks': [THEIRS, {'type': 'command', 'command': '/old/home/.ccboard-bare/hook claude Stop', 'async': True}]}]}}
    new = json.loads(merged(json.dumps(old)))
    assert new['hooks']['Stop'] == [{'hooks': [THEIRS, {'type': 'command', 'command': HOME + '/.ccboard-bare/hook claude Stop',
                                                       'timeout': 5, 'async': True}]}]


def test_remove_takes_only_ours():
    start = {'keep': [1], 'hooks': {'Stop': [{'hooks': [THEIRS]}], 'Notification': [{'hooks': [THEIRS]}]}}
    installed = merged(json.dumps(start))
    removed = json.loads(merged(installed, install=False))
    assert removed == start
    # Our entry sharing a group with theirs: theirs stay, the group stays.
    mixed = json.loads(installed)
    mixed['hooks']['Stop'][0]['hooks'].append(mixed['hooks']['Stop'][1]['hooks'][0])
    del mixed['hooks']['Stop'][1]
    assert json.loads(merged(json.dumps(mixed), install=False))['hooks']['Stop'] == [{'hooks': [THEIRS]}]
    assert merged(json.dumps(start), install=False) is None                          # nothing of ours: nothing to write
    assert merged('{}', install=False) is None and merged('', install=False) is None
    only = json.loads(merged('{}'))
    assert json.loads(merged(json.dumps(only), install=False)) == {}                # an emptied hooks object goes too
    assert merged(json.dumps({'hooks': {}}), install=False) is None


@pytest.mark.parametrize('text,fragment', [
    ('{not json', 'is not valid JSON'), ('[1]', 'is not a JSON object'), ('"x"', 'is not a JSON object'),
    ('{"hooks": []}', 'hooks entry that is not an object'), ('{"hooks": {"Stop": {}}}', 'hooks.Stop that is not a list'),
    ('{"hooks": {"PermissionRequest": "x"}}', 'hooks.PermissionRequest that is not a list'), ('[' * 5000, 'is not valid JSON'),
])
@pytest.mark.parametrize('install', [True, False])
def test_malformed_settings_are_refused_with_a_plain_message(text, fragment, install):
    with pytest.raises(bare.HookRefused) as e:
        merged(text, install=install)
    assert fragment in str(e.value) and 'nothing was changed' in str(e.value) and '~/.claude/settings.json' in str(e.value)


# ---- the device calls (scripted) ----

def frame(claude=None, codex=None, home=HOME, link=()):
    def part(agent, text):
        if agent in link:
            return 'link\0\0\0'
        return 'missing\0missing\0\0' if text is None else f'file\0{len(text) * 7} {len(text)}\0{text}\0'
    return (0, home + '\0' + part('claude', claude) + part('codex', codex), '')


def post(client, headers, verb, **body):
    return client.post(f'/api/bare/device-1/state-log/{verb}', headers=headers, json=body)


def audits():
    return [(r['action'], r['status'], r['user']) for r in main.db.node_audit_list()]


def argv_words(call):
    return shlex.split(call[0][-1])


def test_install_argv_stdin_record_and_audit(lite_client, bare_runner, device):
    headers = device[0]
    theirs = json.dumps({'hooks': {'Stop': [{'hooks': [THEIRS]}]}})
    bare_runner.answers.extend([frame(claude=theirs), (0, '', ''), (0, '', ''), (0, '', '')])
    r = post(lite_client, headers, 'install', confirm=True)
    assert r.status_code == 200, r.text
    assert r.json()['changed'] == ['claude', 'codex']
    assert [argv_words(c)[:2] for c in bare_runner.calls] == [['sh', '-c']] * 4
    read, script, claude, codex = bare_runner.calls
    assert argv_words(read) == ['sh', '-c', bare.HOOK_READ, 'bare-hook-read'] and read[1] is None
    assert argv_words(script) == ['sh', '-c', bare.HOOK_WRITE, 'bare-hook-write', '.ccboard-bare/hook', 'any', '700', str(len(bare.HOOK_SCRIPT.encode()))]
    assert script[1] == bare.HOOK_SCRIPT
    assert argv_words(claude) == ['sh', '-c', bare.HOOK_WRITE, 'bare-hook-write', '.claude/settings.json', f'{len(theirs) * 7} {len(theirs)}', '600', str(len(claude[1].encode()))]
    assert argv_words(codex) == ['sh', '-c', bare.HOOK_WRITE, 'bare-hook-write', '.codex/hooks.json', 'missing', '600', str(len(codex[1].encode()))]
    sent = json.loads(claude[1])
    assert sent['hooks']['Stop'][0] == {'hooks': [THEIRS]} and len(ours(sent)) == 4
    assert json.loads(codex[1])['hooks']['Stop'][0]['hooks'][0]['command'] == HOME + '/.ccboard-bare/hook codex Stop'
    for call in bare_runner.calls:                                                   # every argv is the pinned transport plus one quoted command
        assert call[0][0] == 'ssh' and call[0][-2] == 'ccagent@example.ts.net' and len(call[0]) == len(bare_runner.calls[0][0])
    saved = main.db.kv_get(bare.PREFIX + 'device-1')['value']
    assert saved['state_log']['installed'] is True and saved['state_log']['agents'] == ['claude', 'codex']
    assert audits() == [('bare_hook_install', 'ok', headers['Tailscale-User-Login'])]
    assert 'Stop' not in bare.HOOK_SCRIPT.split('\n')[0]                              # the script is fixed text, not built from anything


def test_install_twice_writes_nothing_new_to_settings(lite_client, bare_runner, device):
    headers = device[0]
    bare_runner.answers.extend([frame(), (0, '', ''), (0, '', ''), (0, '', '')])
    assert post(lite_client, headers, 'install', confirm=True).status_code == 200
    claude, codex = bare_runner.calls[2][1], bare_runner.calls[3][1]
    bare_runner.calls.clear()
    bare_runner.answers.extend([frame(claude=claude, codex=codex), (0, '', '')])
    r = post(lite_client, headers, 'install', confirm=True)
    assert r.status_code == 200 and r.json()['changed'] == []
    assert [argv_words(c)[3] for c in bare_runner.calls] == ['bare-hook-read', 'bare-hook-write']
    assert argv_words(bare_runner.calls[1])[4] == '.ccboard-bare/hook'


def test_install_only_for_agents_the_check_found(lite_client, bare_runner, device):
    headers = device[0]
    saved = main.db.kv_get(bare.PREFIX + 'device-1')['value']
    saved['check']['rows'][1]['ok'] = False                                          # codex missing
    main.db.kv_set(bare.PREFIX + 'device-1', saved)
    bare_runner.answers.extend([frame(), (0, '', ''), (0, '', '')])
    assert post(lite_client, headers, 'install', confirm=True).json()['changed'] == ['claude']
    assert [argv_words(c)[4] for c in bare_runner.calls[1:]] == ['.ccboard-bare/hook', '.claude/settings.json']
    saved['check']['rows'][0]['ok'] = False
    main.db.kv_set(bare.PREFIX + 'device-1', saved)
    bare_runner.calls.clear()
    assert post(lite_client, headers, 'install', confirm=True).status_code == 400 and not bare_runner.calls


@pytest.mark.parametrize('claude,codex,file', [('{broken', None, 'settings.json'), (None, '[]', 'hooks.json'), ('{"hooks": 3}', None, 'settings.json')])
def test_malformed_settings_write_nothing_at_all(lite_client, bare_runner, device, claude, codex, file):
    headers = device[0]
    bare_runner.answers.append(frame(claude=claude, codex=codex))
    r = post(lite_client, headers, 'install', confirm=True)
    assert r.status_code == 400 and file in r.json()['error'] and 'nothing was changed' in r.json()['error']
    assert len(bare_runner.calls) == 1                                               # only the read; not even the script was written
    assert 'state_log' not in main.db.kv_get(bare.PREFIX + 'device-1')['value']
    assert audits() == [('bare_hook_install', 'failed', headers['Tailscale-User-Login'])]


def test_links_unreadable_replies_and_a_changed_file_are_refused(lite_client, bare_runner, device):
    headers = device[0]
    bare_runner.answers.append(frame(link=('codex',)))
    r = post(lite_client, headers, 'install', confirm=True)
    assert r.status_code == 400 and 'is a link' in r.json()['error'] and len(bare_runner.calls) == 1
    for answer in [(255, '', ''), (0, 'truncated', ''), (0, '/home/x\0bad\0\0\0missing\0\0\0', ''), (125, '', '')]:
        bare_runner.answers.append(answer)
        assert post(lite_client, headers, 'install', confirm=True).status_code == 400
    bare_runner.answers.extend([frame(), (0, '', ''), (3, '', '')])
    r = post(lite_client, headers, 'install', confirm=True)
    assert r.status_code == 400 and 'changed on the device while the board worked' in r.json()['error']
    assert 'state_log' not in main.db.kv_get(bare.PREFIX + 'device-1')['value']      # no settings file was written, so nothing is claimed
    bare_runner.answers.extend([subprocess.TimeoutExpired(['fake'], 12)])
    r = post(lite_client, headers, 'install', confirm=True)
    assert r.status_code == 400 and 'safe to repeat' in r.json()['error']


def test_two_taps_person_and_tokens(lite_client, bare_runner, device):
    headers = device[0]
    for verb in ('install', 'remove'):
        assert post(lite_client, headers, verb).status_code == 400
        assert post(lite_client, headers, verb, confirm=False).status_code == 400
        assert post(lite_client, headers, verb, confirm='yes').status_code == 422
        assert post(lite_client, headers, verb, confirm=True, extra=1).status_code == 422
        assert lite_client.post(f'/api/bare/device-1/state-log/{verb}', json={'confirm': True}).status_code in (400, 401, 403)
    assert post(lite_client, headers, 'sideways', confirm=True).status_code == 404
    assert not bare_runner.calls
    assert lite_client.post('/api/bare/nope/state-log/install', headers=headers, json={'confirm': True}).status_code == 400


def test_remove_takes_only_ours_cleans_up_and_audits(lite_client, bare_runner, device):
    headers = device[0]
    start = {'hooks': {'Stop': [{'hooks': [THEIRS]}]}}
    both = merged(json.dumps(start))
    main.db.kv_set(bare.PREFIX + 'device-1', {**main.db.kv_get(bare.PREFIX + 'device-1')['value'],
                                              'state_log': {'installed': True, 'agents': ['claude'], 'at': 'x'}})
    bare_runner.answers.extend([frame(claude=both, codex=merged('{}', 'codex')), (0, '', ''), (0, '', ''), (0, '', '')])
    r = post(lite_client, headers, 'remove', confirm=True)
    assert r.status_code == 200 and r.json()['changed'] == ['claude', 'codex']
    assert [argv_words(c)[3] for c in bare_runner.calls] == ['bare-hook-read', 'bare-hook-write', 'bare-hook-write', 'bare-hook-clean']
    assert json.loads(bare_runner.calls[1][1]) == start and json.loads(bare_runner.calls[2][1]) == {}
    assert argv_words(bare_runner.calls[3]) == ['sh', '-c', bare.HOOK_CLEAN, 'bare-hook-clean']
    assert 'state_log' not in main.db.kv_get(bare.PREFIX + 'device-1')['value']
    assert audits() == [('bare_hook_remove', 'ok', headers['Tailscale-User-Login'])]
    bare_runner.calls.clear()
    bare_runner.answers.extend([frame(claude=json.dumps(start)), (0, '', '')])      # nothing of ours left: only the clean-up runs
    assert post(lite_client, headers, 'remove', confirm=True).json()['changed'] == []
    assert [argv_words(c)[3] for c in bare_runner.calls] == ['bare-hook-read', 'bare-hook-clean']
    bare_runner.answers.append(frame(claude='{broken'))
    assert post(lite_client, headers, 'remove', confirm=True).status_code == 400
    assert audits()[0][1] == 'failed'


def test_hook_argv_validation_and_fixed_scripts():
    dev = {'dns_name': 'example.ts.net', 'ips': ['100.64.0.2']}
    ok = {'rel': bare.HOOK_REL, 'expected': 'any', 'mode': '600', 'size': 10}
    for step, kw in [('write', {**ok, 'rel': '../x'}), ('write', {**ok, 'expected': 'a b'}), ('write', {**ok, 'mode': '6000'}),
                     ('write', {**ok, 'expected': 'any;id'}), ('write', {**ok, 'size': '10;id'}), ('write', {**ok, 'size': -1}),
                     ('write', {**ok, 'size': None}), ('write', {**ok, 'size': True}), ('delete', {})]:
        with pytest.raises(ValueError):
            bare.hook_argv(dev, 'ccagent', step, **kw)
    with pytest.raises(ValueError):
        bare.hook_argv(dev, 'root', 'read')
    with pytest.raises(ValueError):
        bare.hook_command('/home/x', 'claude', 'PreToolUse')
    assert shlex.split(bare.hook_argv(dev, 'ccagent', 'write', **ok)[-1])[-5:] == ['bare-hook-write', bare.HOOK_REL, 'any', '600', '10']
    assert bare.hook_argv(dev, 'ccagent', 'read')[-1] == shlex.join(['sh', '-c', bare.HOOK_READ, 'bare-hook-read'])
    assert 'ccboard-bare' in bare.HOOK_SCRIPT and 'prompt' in bare.HOOK_SCRIPT                      # it says what it does not read


def test_state_log_survives_a_check(lite_client, bare_runner, device):
    headers = device[0]
    saved = main.db.kv_get(bare.PREFIX + 'device-1')['value']
    saved['state_log'] = {'installed': True, 'agents': ['claude'], 'at': 'x'}
    main.db.kv_set(bare.PREFIX + 'device-1', saved)
    bare_runner.answers.extend([(0, '', ''), (0, '1001', ''), (0, 'users staff', ''), (0, 'Linux', ''), (0, 'tmux 3.4', ''),
                                (0, '/bin/claude\n/bin/codex', ''), (0, './repo one/.git\n', '')])
    assert lite_client.post('/api/bare/device-1/check', headers=headers).status_code == 200
    assert main.db.kv_get(bare.PREFIX + 'device-1')['value']['state_log']['installed'] is True
    assert main.db.kv_get(bare.PREFIX + 'device-1')['value']['check']['ok'] is True


# ---- the log lines ----

def line(state='working', session=SESSION, time_=1000, **extra):
    return json.dumps({'state': state, 'session': session, 'time': time_, **extra})


def test_parse_log_skips_bad_lines_and_newest_wins():
    text = '\n'.join([
        '"ing": "cut off first line"}',                 # the tail -c cut
        line('waiting', SESSION, 900), line('working', SESSION, 1000), line('waiting', OTHER, 950),
        line('dialog', SESSION, 990),                   # older than the line before it: not the newest
        'not json', '[]', '', 'null', '{"state": "working"}',
        line('sleeping', SESSION, 1005), line('working', 'bare-short', 1005), line('working', 'bare-' + 'G' * 32, 1005),
        line('working', SESSION, 99999), line('working', SESSION, -5), line('working', SESSION, 0),
        json.dumps({'state': 'working', 'session': SESSION, 'time': True}), json.dumps({'state': 'working', 'session': SESSION, 'time': 1001.5}),
        json.dumps({'state': 'working', 'session': SESSION, 'time': '1001'}), line(prompt='secret words'),
        '\x00\x01' + line('working', SESSION, 1002),
    ])
    assert bare.parse_log(1100, text) == {SESSION: ('working', 1000), OTHER: ('waiting', 950)}
    assert bare.parse_log(1100, '') == {} and bare.parse_log(1100, 'x' * 50000) == {}
    assert bare.parse_log(1100, line('waiting', SESSION, 1000) + '\n' + line('working', SESSION, 1000)) == {SESSION: ('working', 1000)}
    many = '\n'.join(line('working', OTHER, 100 + i) for i in range(2000))
    assert bare.parse_log(5000, many) == {OTHER: ('working', 2099)}


def test_merged_state_rules():
    mem, key = {}, ('d', SESSION)
    # no line, a stale line, a visible dialog: the screen alone
    assert bare.merged_state(mem, key, None, 'waiting', 5000) == ('waiting', 'bare_screen')
    assert bare.merged_state(mem, key, ('working', 100), 'waiting', 5000) == ('waiting', 'bare_screen')
    assert bare.merged_state(mem, key, ('working', 4990), 'dialog', 5000) == ('dialog', 'bare_screen')
    mem.clear()
    # a new event wins over the screen, and holds while the screen stays as it was
    assert bare.merged_state(mem, key, ('working', 4990), 'waiting', 5000) == ('working', 'bare_log')
    assert bare.merged_state(mem, key, ('working', 4990), 'waiting', 5010) == ('working', 'bare_log')
    # the screen changed by itself (Esc sends no hook): the screen decides again, and keeps deciding
    assert bare.merged_state(mem, key, ('working', 4990), 'unknown', 5020) == ('unknown', 'bare_screen')
    assert bare.merged_state(mem, key, ('working', 4990), 'unknown', 5030) == ('unknown', 'bare_screen')
    # the next event wins again
    assert bare.merged_state(mem, key, ('waiting', 5035), 'unknown', 5040) == ('waiting', 'bare_log')
    # after a restart (no memory) only a young line is believed
    assert bare.merged_state({}, key, ('working', 4000), 'waiting', 5000) == ('waiting', 'bare_screen')
    assert bare.merged_state({}, key, ('working', 4990), 'waiting', 5000) == ('working', 'bare_log')
    # agreeing evidence is not a log decision
    assert bare.merged_state({}, key, ('waiting', 4990), 'waiting', 5000) == ('waiting', 'bare_screen')


def test_an_answered_dialog_line_is_never_brought_back():
    mem, key = {}, ('d', SESSION)
    # the dialog shows: the screen wins and the line's time stays as a floor
    assert bare.merged_state(mem, key, ('dialog', 995), 'dialog', 1000) == ('dialog', 'bare_screen')
    assert mem[key]['at'] == 995
    # answered with no hook: the same line must not return, on any later poll
    for now, screen in ((1010, 'working'), (1020, 'working'), (1100, 'working'), (1110, 'waiting'), (1200, 'working')):
        assert bare.merged_state(mem, key, ('dialog', 995), screen, now) == (screen, 'bare_screen'), now
    # a line older than the floor cannot revive it either, and a restart-fresh memory believes no dialog line over a screen without one
    mem2 = {key: {'at': 995, 'state': 'dialog', 'screen': 'dialog'}}
    assert bare.merged_state(mem2, key, ('working', 990), 'waiting', 1005) == ('waiting', 'bare_screen')
    assert bare.merged_state({}, key, ('dialog', 995), 'working', 1000) == ('working', 'bare_screen')
    assert bare.merged_state({}, key, ('dialog', 995), 'unknown', 1000) == ('unknown', 'bare_screen')
    assert bare.merged_state({}, key, ('dialog', 995), 'waiting', 1100) == ('waiting', 'bare_screen')
    # a newer working or waiting line still wins as before, after the dialog was answered
    assert bare.merged_state(mem, key, ('working', 1300), 'waiting', 1302) == ('working', 'bare_log')
    assert bare.merged_state(mem, key, ('waiting', 1310), 'working', 1312) == ('waiting', 'bare_log')
    mem3 = {}
    assert bare.merged_state(mem3, key, ('dialog', 995), 'dialog', 1000) == ('dialog', 'bare_screen')
    assert bare.merged_state(mem3, key, ('working', 1005), 'waiting', 1006) == ('working', 'bare_log')
    # a real dialog on screen still wins over any line, and a dialog line then a fresh one keep the newest floor
    assert bare.merged_state(mem3, key, ('waiting', 1008), 'dialog', 1010) == ('dialog', 'bare_screen')
    assert mem3[key]['at'] == 1008
    assert bare.merged_state({}, key, None, 'dialog', 1010) == ('dialog', 'bare_screen')


# ---- the poll batch ----

def installed(value=True):
    saved = main.db.kv_get(bare.PREFIX + 'device-1')['value']
    saved['state_log'] = {'installed': value, 'agents': ['claude'], 'at': 'x'}
    main.db.kv_set(bare.PREFIX + 'device-1', saved)


def row(name):
    return main.db.bare_open_rows('device-1')[name]


def test_batch_argv_asks_for_the_log_only_when_installed(lite_client, bare_runner, device):
    names = seed()
    poll(bare_runner, device[0], names)
    assert shlex.split(bare_runner.calls[-1][0][-1]) == ['sh', '-c', bare.POLL, 'bare-poll', bare.session_socket(), '0', *names]
    installed()
    poll(bare_runner, device[0], names)
    assert shlex.split(bare_runner.calls[-1][0][-1]) == ['sh', '-c', bare.POLL, 'bare-poll', bare.session_socket(), '1', *names]
    assert len(bare_runner.calls) == 2 and 'state.log' in bare.POLL and 'tail -c 16384' in bare.POLL      # one SSH call per device, capped read


def test_log_state_preferred_then_screen_takes_over_when_it_changes(lite_client, bare_runner, device):
    installed()
    names = seed()
    now = 1_800_000_000
    waiting = (__import__('pathlib').Path(__file__).parent / 'fixtures/agents/claude/pane_waiting.txt').read_text()
    working = (__import__('pathlib').Path(__file__).parent / 'fixtures/agents/claude/pane_working.txt').read_text()

    def tick(text, log, at):
        bare_runner.answers.append(capture(names, text, now=str(at), log=log))
        bare.poll_device(main.db, device[0]['Tailscale-User-Login'], 'device-1')
        return row(names[0])

    r = tick(waiting, line('working', names[0], now - 2) + '\n', now)                  # the log says working, the screen says waiting
    assert (r['state'], r['last_event']) == ('working', 'bare_log')
    r = tick(waiting, line('working', names[0], now - 2) + '\n', now + 10)             # nothing new, screen unchanged: the log stands
    assert (r['state'], r['last_event']) == ('working', 'bare_log')
    r = tick('nothing recognisable', line('working', names[0], now - 2) + '\n', now + 20)   # the screen moved by itself: the screen decides
    assert (r['state'], r['last_event']) == ('unknown', 'bare_screen')
    r = tick('nothing recognisable', line('working', names[0], now - 2) + '\n', now + 25)   # and keeps deciding until a new event
    assert (r['state'], r['last_event']) == ('unknown', 'bare_screen')
    r = tick(working, line('working', names[0], now - 2) + '\n' + line('waiting', names[0], now + 27) + '\n', now + 30)
    assert (r['state'], r['last_event']) == ('waiting', 'bare_log')
    assert not main.db.node_audit_list()


@pytest.mark.parametrize('log', ['', 'garbage\n{"truncated', line('working', OTHER, 5) + '\n', line('working', 'x', 5) + '\n'])
def test_missing_stale_or_unreadable_log_falls_back_to_the_screen(lite_client, bare_runner, device, log):
    installed()
    names = seed()
    waiting = (__import__('pathlib').Path(__file__).parent / 'fixtures/agents/claude/pane_waiting.txt').read_text()
    bare_runner.answers.append(capture(names, waiting, now='1800000000', log=log))
    bare.poll_device(main.db, device[0]['Tailscale-User-Login'], 'device-1')
    assert (row(names[0])['state'], row(names[0])['last_event']) == ('waiting', 'bare_screen')


def test_stale_line_and_unreadable_clock_fall_back_and_a_dialog_is_never_hidden(lite_client, bare_runner, device, pushes):
    installed()
    names = seed()
    waiting = (__import__('pathlib').Path(__file__).parent / 'fixtures/agents/claude/pane_waiting.txt').read_text()
    for now, log in [('1800000000', line('working', names[0], 1_799_990_000) + '\n'), ('later', line('working', names[0], 1_799_999_999) + '\n')]:
        bare_runner.answers.append(capture(names, waiting, now=now, log=log))
        bare.poll_device(main.db, device[0]['Tailscale-User-Login'], 'device-1')
        assert row(names[0])['state'] == 'waiting'
    bare_runner.answers.append(capture(names, CLAUDE, now='1800000000', log=line('working', names[0], 1_799_999_999) + '\n'))
    bare.poll_device(main.db, device[0]['Tailscale-User-Login'], 'device-1')
    assert row(names[0])['state'] == 'dialog' and len(pushes) == 1 and item(device[0])['kind'] == 'terminal_prompt'


def test_shells_ignore_the_log(lite_client, bare_runner, device):
    installed()
    names = seed('shell')
    bare_runner.answers.append(capture(names, '$ ', 'sh', now='1800000000', log=line('working', names[0], 1_799_999_999) + '\n'))
    bare.poll_device(main.db, device[0]['Tailscale-User-Login'], 'device-1')
    assert row(names[0])['state'] == 'waiting' and row(names[0])['last_event'] == 'bare_screen'


def test_a_reply_recaptures_the_screen_without_asking_for_the_log(lite_client, bare_runner, device):
    installed()
    names = seed()
    waiting = (__import__('pathlib').Path(__file__).parent / 'fixtures/agents/claude/pane_waiting.txt').read_text()
    bare_runner.answers.append(capture(names, waiting))
    bare.poll_device(main.db, device[0]['Tailscale-User-Login'], 'device-1')
    bare_runner.calls.clear()
    bare_runner.answers.extend([capture(names, waiting), (0, '', '')])
    assert lite_client.post(route(names[0], 'reply'), headers=device[0], json={'text': 'go'}).status_code == 200
    assert shlex.split(bare_runner.calls[0][0][-1])[5] == '0'


# ---- the device scripts, run here against a temp HOME ----

def local(home, command, stdin=None, env=None):
    words = shlex.split(command)
    assert words[:2] == ['sh', '-c']
    return subprocess.run(['/bin/sh', '-c', words[2], *words[3:]], input=stdin, text=True, capture_output=True, cwd=home,
                          env={'HOME': str(home), 'PATH': '/usr/bin:/bin', **(env or {})})


@pytest.fixture
def local_device(lite_client, bare_runner, device, tmp_path):
    """The fake runner executes the fixed device scripts here, in a temp HOME. Nothing leaves the process but `sh`."""
    home = tmp_path / 'home'
    home.mkdir()

    def run(argv, *, stdin=None, stdout_limit=32768):
        assert argv[0] == 'ssh'
        bare_runner.calls.append((list(argv), stdin))
        done = local(home, argv[-1], stdin)
        return SimpleNamespace(returncode=done.returncode, stdout=done.stdout, stderr=done.stderr)

    bare_runner.home = home
    import app.bare as module
    yield bare_runner, device[0], home, run, module


def test_install_and_remove_end_to_end_in_a_temp_home(lite_client, local_device, monkeypatch):
    fake, headers, home, run, module = local_device
    monkeypatch.setattr(module, 'runner', run)
    (home / '.claude').mkdir()
    original = json.dumps({'theme': 'dark', 'hooks': {'Stop': [{'hooks': [THEIRS]}]}}, indent=4)
    (home / '.claude/settings.json').write_text(original)
    r = post(lite_client, headers, 'install', confirm=True)
    assert r.status_code == 200, r.text
    settings = json.loads((home / '.claude/settings.json').read_text())
    assert settings['theme'] == 'dark' and settings['hooks']['Stop'][0] == {'hooks': [THEIRS]} and len(ours(settings)) == 4
    assert (home / '.claude/settings.json.ccboard-bak').read_text() == original            # one backup of the previous version
    assert (home / '.ccboard-bare/hook').read_text() == bare.HOOK_SCRIPT and os.access(home / '.ccboard-bare/hook', os.X_OK)
    assert oct((home / '.ccboard-bare/hook').stat().st_mode & 0o777) == '0o700'
    assert oct((home / '.codex/hooks.json').stat().st_mode & 0o777) == '0o600'
    assert not list(home.rglob('*.ccboard-tmp'))
    again = post(lite_client, headers, 'install', confirm=True)
    assert again.status_code == 200 and again.json()['changed'] == []
    assert json.loads((home / '.claude/settings.json').read_text()) == settings
    removed = post(lite_client, headers, 'remove', confirm=True)
    assert removed.status_code == 200
    assert json.loads((home / '.claude/settings.json').read_text()) == {'theme': 'dark', 'hooks': {'Stop': [{'hooks': [THEIRS]}]}}
    assert json.loads((home / '.codex/hooks.json').read_text()) == {}
    assert not (home / '.ccboard-bare').exists()


def test_write_script_guards(tmp_path):
    home = tmp_path / 'h'
    home.mkdir()
    write = lambda *a, stdin='new': local(home, shlex.join(['sh', '-c', bare.HOOK_WRITE, 'w', *a, str(len(stdin.encode()))]), stdin)
    assert write('.claude/settings.json', 'missing', '600').returncode == 0
    target = home / '.claude/settings.json'
    assert target.read_text() == 'new' and oct(target.stat().st_mode & 0o777) == '0o600'
    assert write('.claude/settings.json', 'missing', '600').returncode == 3          # it exists now: the board's reading was stale
    assert write('.claude/settings.json', '1 2', '600').returncode == 3              # wrong checksum
    assert target.read_text() == 'new' and not (home / '.claude/settings.json.ccboard-bak').exists()
    done = local(home, 'sh -c ' + shlex.quote('cksum < "$HOME/.claude/settings.json"') + ' x')
    assert write('.claude/settings.json', done.stdout.strip(), '600', stdin='newer').returncode == 0
    assert target.read_text() == 'newer' and (home / '.claude/settings.json.ccboard-bak').read_text() == 'new'
    (home / '.codex').mkdir()
    (home / '.codex/hooks.json').symlink_to(target)
    assert write('.codex/hooks.json', 'any', '600').returncode == 4 and target.read_text() == 'newer'


def test_write_script_refuses_a_short_stream_and_keeps_the_original(tmp_path):
    home = tmp_path / 'h'
    (home / '.claude').mkdir(parents=True)
    target = home / '.claude/settings.json'
    target.write_text('original text')
    before = target.stat()
    sum_ = local(home, 'sh -c ' + shlex.quote('cksum < "$HOME/.claude/settings.json"') + ' x').stdout.strip()
    full = 'a much longer replacement\n'
    run = lambda sum_, size, stdin: local(home, shlex.join(['sh', '-c', bare.HOOK_WRITE, 'w', '.claude/settings.json', sum_, '600', str(size)]), stdin)
    short = run(sum_, len(full.encode()), full[:7])                                  # the stream ended early: a clean EOF, too few bytes
    assert short.returncode == 5
    assert target.read_bytes() == b'original text' and target.stat().st_ino == before.st_ino
    assert not list(home.rglob('*.ccboard-tmp'))                                     # no temporary file behind
    assert run(sum_, len(full.encode()), '').returncode == 5 and target.read_text() == 'original text'
    assert run(sum_, 3, full).returncode == 5 and target.read_text() == 'original text' and not list(home.rglob('*.ccboard-tmp'))
    assert run('missing', 3, 'abc').returncode == 3
    # a new file, too: nothing is installed from a short stream
    created = local(home, shlex.join(['sh', '-c', bare.HOOK_WRITE, 'w', '.ccboard-bare/hook', 'any', '700', '50']), '#!/bin/sh\n')
    assert created.returncode == 5 and not (home / '.ccboard-bare/hook').exists() and not list(home.rglob('*.ccboard-tmp'))
    assert run(sum_, len(full.encode()), full).returncode == 0 and target.read_text() == full      # the full stream still installs
    multibyte = 'caf\u00e9\n'
    assert run(local(home, 'sh -c ' + shlex.quote('cksum < "$HOME/.claude/settings.json"') + ' x').stdout.strip(), len(multibyte.encode()), multibyte).returncode == 0


def test_a_short_write_is_reported_plainly_and_claims_nothing(lite_client, bare_runner, device):
    headers = device[0]
    bare_runner.answers.extend([frame(), (5, '', '')])
    r = post(lite_client, headers, 'install', confirm=True)
    assert r.status_code == 400 and 'received only part of' in r.json()['error'] and '.ccboard-bare/hook' in r.json()['error']
    assert len(bare_runner.calls) == 2                                               # nothing after the refused script
    assert 'state_log' not in main.db.kv_get(bare.PREFIX + 'device-1')['value']


def test_read_script_frames_files_missing_and_links(tmp_path):
    home = tmp_path / 'h'
    (home / '.claude').mkdir(parents=True)
    (home / '.claude/settings.json').write_text('{"a": 1}\n')
    (home / '.codex').mkdir()
    (home / '.codex/real.json').write_text('{}')
    (home / '.codex/hooks.json').symlink_to(home / '.codex/real.json')
    out = local(home, shlex.join(['sh', '-c', bare.HOOK_READ, 'r'])).stdout
    parts = out.split('\0')
    assert parts[0] == str(home) and parts[1] == 'file' and parts[3] == '{"a": 1}' and parts[4] == 'link' and parts[7] == ''
    (home / '.codex/hooks.json').unlink()
    assert local(home, shlex.join(['sh', '-c', bare.HOOK_READ, 'r'])).stdout.split('\0')[4:7] == ['missing', 'missing', '']


# ---- the hook script itself ----

@pytest.fixture
def hookenv(tmp_path):
    home = tmp_path / 'home'
    fakebin = tmp_path / 'bin'
    home.mkdir()
    fakebin.mkdir()
    (fakebin / 'tmux').write_text('#!/bin/sh\n[ "$1" = display-message ] && [ "$4" = "%3" ] || exit 9\nprintf "%s\\n" "$FAKE_SESSION"\n')
    (fakebin / 'tmux').chmod(0o755)
    script = tmp_path / 'hook'
    script.write_text(bare.HOOK_SCRIPT)

    def fire(event, *, session=SESSION, tmux='/tmp/tmux-1000/ccboard-bare-00ab12cd,321,0', stdin='{"prompt": "SECRET-PROMPT-TEXT /private/path"}', agent='claude'):
        env = {'HOME': str(home), 'PATH': f'{fakebin}:/usr/bin:/bin', 'FAKE_SESSION': session, 'TMUX_PANE': '%3'}
        if tmux is not None:
            env['TMUX'] = tmux
        return subprocess.run(['/bin/sh', str(script), agent, event], input=stdin, text=True, capture_output=True, env=env, cwd=tmp_path)

    return SimpleNamespace(home=home, fire=fire, log=home / '.ccboard-bare/state.log')


def test_hook_script_writes_only_state_session_time(hookenv):
    assert hookenv.fire('UserPromptSubmit').returncode == 0
    (entry,) = [json.loads(l) for l in hookenv.log.read_text().splitlines()]
    assert set(entry) == {'state', 'session', 'time'} and entry['state'] == 'working' and entry['session'] == SESSION
    assert abs(entry['time'] - time.time()) < 60
    assert 'SECRET' not in hookenv.log.read_text() and '/private' not in hookenv.log.read_text()
    assert oct(hookenv.log.stat().st_mode & 0o777) == '0o600' and oct(hookenv.log.parent.stat().st_mode & 0o777) == '0o700'
    for event, state in [('Stop', 'waiting'), ('PermissionRequest', 'dialog'), ('Stop', 'waiting'), ('SessionStart', 'waiting'), ('UserPromptSubmit', 'working')]:
        hookenv.fire(event)
    states = [json.loads(l)['state'] for l in hookenv.log.read_text().splitlines()]
    assert states == ['working', 'waiting', 'dialog', 'waiting', 'working']        # a repeated state adds no line


@pytest.mark.parametrize('kw', [
    {'tmux': None}, {'tmux': '/tmp/tmux-1000/default,321,0'}, {'session': 'my-own-session'}, {'session': 'bare-' + 'a' * 31},
    {'session': 'bare-' + 'A' * 32}, {'session': 'bare-' + 'a' * 32 + 'x'}, {'session': ''}, {'event': 'PreToolUse'}, {'event': 'Notification'},
])
def test_hook_script_ignores_everything_else(hookenv, kw):
    event = kw.pop('event', 'Stop')
    assert hookenv.fire(event, **kw).returncode == 0
    assert not hookenv.log.exists()


def test_hook_script_tracks_sessions_separately_and_rotates(hookenv):
    hookenv.fire('Stop')
    hookenv.fire('UserPromptSubmit', session=OTHER)
    hookenv.fire('UserPromptSubmit')
    assert [(json.loads(l)['session'][-3:], json.loads(l)['state']) for l in hookenv.log.read_text().splitlines()] == [
        ('aaa', 'waiting'), ('bbb', 'working'), ('aaa', 'working')]
    filler = ''.join(line('working', 'bare-' + format(i, '032x'), 1_700_000_000 + i) + '\n' for i in range(900))
    hookenv.log.write_text(filler)
    assert hookenv.log.stat().st_size > 65536
    hookenv.fire('Stop')
    rotated = hookenv.log.parent / 'state.log.1'
    assert rotated.read_text() == filler and hookenv.log.stat().st_size < 8000
    lines = hookenv.log.read_text().splitlines()
    assert len(lines) == 51 and json.loads(lines[-1])['state'] == 'waiting' and lines[0] == filler.splitlines()[-50]
    assert not list(hookenv.log.parent.glob('*.tmp'))


def test_hook_script_survives_a_missing_tmux_and_unwritable_home(hookenv, tmp_path):
    (tmp_path / 'bin/tmux').write_text('#!/bin/sh\nexit 127\n')                        # a failing fake: PATH may hold a real tmux on CI
    (tmp_path / 'bin/tmux').chmod(0o755)
    assert hookenv.fire('Stop').returncode == 0 and not hookenv.log.exists()
    (tmp_path / 'bin/tmux').write_text('#!/bin/sh\nprintf "%s\\n" "$FAKE_SESSION"\n')
    (tmp_path / 'bin/tmux').chmod(0o755)
    hookenv.home.chmod(0o500)
    try:
        assert hookenv.fire('Stop').returncode == 0
    finally:
        hookenv.home.chmod(0o700)


def test_readding_with_another_user_drops_the_state_log(lite_client, bare_runner, device):
    headers = device[0]
    saved = main.db.kv_get(bare.PREFIX + 'device-1')['value']
    saved['state_log'] = {'installed': True, 'agents': ['claude'], 'at': 'x'}
    main.db.kv_set(bare.PREFIX + 'device-1', saved)
    answers = [(0, '', ''), (0, '1001', ''), (0, 'users staff', ''), (0, 'Linux', ''), (0, 'tmux 3.4', ''),
               (0, '/bin/claude\n/bin/codex', ''), (0, './repo one/.git\n', '')]
    bare_runner.answers.extend(answers)
    r = lite_client.post('/api/bare', headers=headers, json={'ts_id': 'device-1', 'user': 'ccother', 'folder': 'my projects'})
    assert r.status_code == 200, r.text
    record = main.db.kv_get(bare.PREFIX + 'device-1')['value']
    assert record['user'] == 'ccother' and 'state_log' not in record                 # the hooks sit in the other user's home
    saved['user'] = 'ccother'
    saved['state_log'] = {'installed': True, 'agents': ['claude'], 'at': 'x'}
    main.db.kv_set(bare.PREFIX + 'device-1', saved)
    bare_runner.answers.extend(answers)
    assert lite_client.post('/api/bare', headers=headers, json={'ts_id': 'device-1', 'user': 'ccother', 'folder': 'my projects'}).status_code == 200
    assert main.db.kv_get(bare.PREFIX + 'device-1')['value']['state_log']['installed'] is True      # the same user keeps it
