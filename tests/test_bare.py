"""Phase 1: scripted SSH only; ownership, argv, checks and audit use temporary board data."""
import hashlib
import shlex
import subprocess

import pytest

from app import bare, main, nodes_discovery
from app.config import settings

PROCESS_RUNNER = bare.runner


@pytest.fixture
def tailnet(monkeypatch, projects_dir):
    owner = next(iter(settings.allowed_users))
    suffix = "example.test"
    peer = {"ID": "device-1", "DNSName": "device." + suffix, "HostName": "Device", "UserID": 1,
            "OS": "linux", "Online": True, "TailscaleIPs": ["100.64.0.2"]}
    status = {"Self": {"ID": "board", "UserID": 1}, "CurrentTailnet": {"MagicDNSSuffix": suffix},
              "User": {"1": {"ID": 1, "LoginName": owner}}, "Peer": {"one": peer}}
    monkeypatch.setattr(nodes_discovery, "read_tailscale", lambda fresh=False: (status, {"ok": True, "reason": ""}))
    return status, owner, {"Tailscale-User-Login": owner, "X-CCBoard": "1"}


def good(fake):
    fake.answers.extend([(0, "", ""), (0, "1001\n", ""), (0, "users staff\n", ""), (0, "Linux\n", ""),
                         (0, "tmux 3.4\n", ""), (0, "/bin/claude\n/bin/codex\n", ""),
                         (0, "./repo one/.git\n./group/repo/.git\n", "")])


def test_candidates_owner_tagged_and_never_sharee(tailnet):
    status, owner, _ = tailnet
    peers = status["Peer"]
    for name, over in {"tagged": {"UserID": 2, "Tags": ["tag:anything"]}, "other": {"UserID": 2},
                       "shared": {"ShareeNode": True, "Tags": ["tag:anything"]},
                       "foreign": {"DNSName": "device.other.test", "Tags": ["tag:anything"]},
                       "asleep": {"Online": False}, "expired": {"Expired": True}}.items():
        peers[name] = {**peers["one"], "ID": name, **over}
    assert {r["ts_id"] for r in bare.candidates(status, owner)} == {"device-1", "tagged"}
    status["Self"]["Tags"] = ["tag:board"]
    status["Self"]["UserID"] = 2
    assert {r["ts_id"] for r in bare.candidates(status, owner)} == {"device-1", "tagged"}
    status.pop("User")
    assert {r["ts_id"] for r in bare.candidates(status, owner)} == {"tagged"}


def test_add_check_remove_exact_argv_and_audit(lite_client, tailnet, bare_runner):
    status, owner, headers = tailnet
    assert lite_client.get('/api/bare', headers=headers).json()["candidates"][0]["ts_id"] == "device-1"
    assert bare_runner.calls == []
    good(bare_runner)
    r = lite_client.post('/api/bare', headers=headers, json={"ts_id": "device-1", "folder": "my projects"})
    assert r.status_code == 200
    result = r.json()
    assert result["added"] and result["check"]["ok"]
    assert result["check"]["repos"] == ["group/repo", "repo one"]
    assert {r["key"] for r in result["check"]["rows"]} == set(bare.CHECKS)
    assert all(r["message"].endswith('.') for r in result["check"]["rows"])
    host = status["Peer"]["one"]["DNSName"]
    directory = settings.data_dir / 'bare-ssh'
    key = hashlib.sha256((host + 'ccagent').encode()).hexdigest()[:16]
    opts = ['BatchMode=yes', 'ConnectTimeout=8', 'StrictHostKeyChecking=accept-new',
            'UserKnownHostsFile=' + shlex.quote(str(directory / 'known_hosts')), 'GlobalKnownHostsFile=/dev/null',
            'ControlMaster=auto', 'ControlPersist=30', 'ControlPath=' + shlex.quote(str(directory / key)),
            'ForwardAgent=no', 'ClearAllForwardings=yes', 'RequestTTY=no', 'PubkeyAuthentication=no',
            'PasswordAuthentication=no', 'KbdInteractiveAuthentication=no', 'HostName=100.64.0.2', 'HostKeyAlias=' + host]
    prefix = ['ssh', '-F', '/dev/null', *[p for o in opts for p in ('-o', o)], 'ccagent@' + host]
    commands = ['true', 'id -u', 'id -Gn', 'uname', 'tmux -V', 'command -v claude codex',
                shlex.join(['sh', '-c', bare.REPOS, 'bare-repos', 'my projects'])]
    assert bare_runner.calls == [(prefix + [cmd], None) for cmd in commands]
    good(bare_runner)
    assert lite_client.post('/api/bare/device-1/check', headers=headers, json={}).json()['check']['ok']
    bare_runner.answers = [(0, '', '')]
    assert lite_client.delete('/api/bare/device-1', headers=headers).json()['ok']
    assert lite_client.get('/api/bare', headers=headers).json()['devices'] == []
    audit = main.db.node_audit_list()
    assert [r['action'] for r in audit] == ['bare_remove', 'bare_check', 'bare_add', 'bare_check']
    assert all(r['user'] == owner and r['status'] == 'ok' and r['detail'] is None for r in audit)


@pytest.mark.parametrize('groups', ['users sudo', 'wheel', 'staff admin', '', 'bad\x1bgroup'])
def test_privileged_or_unreadable_groups_refused(lite_client, tailnet, bare_runner, groups):
    _, _, headers = tailnet
    bare_runner.answers = [(0, '', ''), (0, '1001', ''), (0, groups, '')]
    answer = lite_client.post('/api/bare', headers=headers, json={'ts_id': 'device-1'}).json()
    assert not answer['added'] and not answer['check']['ok']
    assert len(bare_runner.calls) == 3
    assert all(row['fix'] for row in answer['check']['rows'] if not row['ok'])
    assert 'sudo, wheel or admin' in answer['check']['rows'][2]['message']
    assert main.db.kv_list_prefix(bare.PREFIX) == []
    assert [a['action'] for a in main.db.node_audit_list()] == ['bare_add', 'bare_check']


@pytest.mark.parametrize('body', [{'user': 'root'}, {'user': '-oProxyCommand=oops'}, {'user': 'user;true'},
                                  {'folder': '../projects'}, {'folder': '/projects'}, {'folder': 'x;true'}, {'folder': 'x\ntrue'}])
def test_invalid_parts_never_reach_runner(lite_client, tailnet, bare_runner, body):
    r = lite_client.post('/api/bare', headers=tailnet[2], json={'ts_id': 'device-1', **body})
    assert r.status_code == 400
    assert not bare_runner.calls
    assert main.db.node_audit_list()[0]['action'] == 'bare_add'


def test_uid_zero_refused(lite_client, tailnet, bare_runner):
    bare_runner.answers = [(0, '', ''), (0, '0', '')]
    answer = lite_client.post('/api/bare', headers=tailnet[2], json={'ts_id': 'device-1'}).json()
    assert not answer['added']
    assert len(bare_runner.calls) == 2


@pytest.mark.parametrize('change', [{'Online': False}, {'ShareeNode': True}, {'UserID': 2}, {'Expired': True}])
def test_revalidate_before_each_check(lite_client, tailnet, bare_runner, change):
    good(bare_runner)
    lite_client.post('/api/bare', headers=tailnet[2], json={'ts_id': 'device-1'})
    bare_runner.calls.clear()
    tailnet[0]['Peer']['one'].update(change)
    r = lite_client.post('/api/bare/device-1/check', headers=tailnet[2])
    assert not bare_runner.calls
    if change.get('ShareeNode') or change.get('UserID'):
        assert r.status_code == 400
    else:
        assert 'offline' in r.json()['check']['rows'][0]['message']
    assert main.db.node_audit_list()[0]['action'] == 'bare_check'
    assert main.db.node_audit_list()[0]['status'] == 'failed'
    bare_runner.answers = [(0, '', '')]
    assert lite_client.delete('/api/bare/device-1', headers=tailnet[2]).status_code == 200


def test_missing_tools_and_folder_have_fixes(lite_client, tailnet, bare_runner):
    good(bare_runner)
    bare_runner.answers[4:] = [(1, '', ''), (1, '/bin/claude\n', ''), (1, '', 'private error')]
    answer = lite_client.post('/api/bare', headers=tailnet[2], json={'ts_id': 'device-1'}).json()
    rows = {r['key']: r for r in answer['check']['rows']}
    assert answer['added'] and not answer['check']['ok']
    assert rows['claude']['ok'] and not rows['codex']['ok']
    assert 'not installed' in rows['tmux']['message']
    assert 'not on' in rows['codex']['message']
    assert 'could not be read' in rows['repos']['message']
    assert all(r['fix'] for r in rows.values() if not r['ok'])
    assert 'private error' not in str(answer) + str(main.db.node_audit_list())


@pytest.mark.parametrize('error', [OSError('private error'), subprocess.TimeoutExpired(['unused'], 12),
                                   (255, '', 'REMOTE HOST IDENTIFICATION HAS CHANGED')])
def test_transport_failures_have_plain_rows(lite_client, tailnet, bare_runner, error):
    bare_runner.answers = [error]
    answer = lite_client.post('/api/bare', headers=tailnet[2], json={'ts_id': 'device-1'}).json()
    assert not answer['added']
    assert len(answer['check']['rows']) == len(bare.CHECKS)
    assert all(not r['ok'] and r['message'] and r['fix'] for r in answer['check']['rows'])


def test_check_link_ephemeral_and_only_tailscale(lite_client, tailnet, bare_runner):
    link = 'https://login.tailscale.com/a/check'
    bare_runner.answers = [(255, '', 'Open ' + link)]
    answer = lite_client.post('/api/bare', headers=tailnet[2], json={'ts_id': 'device-1'}).json()
    assert answer['check']['login_url'] == link
    assert 'press Retry' in answer['check']['rows'][0]['fix']
    assert link not in str(main.db.kv_list_prefix(bare.PREFIX)) + str(main.db.node_audit_list())
    for bad in ['https://login.tailscale.com.evil.test/x', 'https://login.tailscale.com@evil.test/x', 'http://login.tailscale.com/x']:
        assert bare.login_link(bad) is None


def test_person_csrf_and_extra_fields(lite_client, tailnet, bare_runner):
    headers = tailnet[2]
    assert lite_client.post('/api/bare', headers={'Tailscale-User-Login': tailnet[1]}, json={'ts_id': 'device-1'}).status_code == 403
    assert lite_client.get('/api/bare').status_code == 403
    assert lite_client.post('/api/bare', headers=headers, json={'ts_id': 'device-1', 'prompt': 'not accepted'}).status_code == 422
    from app import hooks
    assert lite_client.get('/api/bare', headers={'Authorization': 'Bearer ' + hooks._token}).status_code == 403
    assert bare_runner.calls == []


@pytest.mark.parametrize('field,value', [('dns_name', '-oProxyCommand=x'), ('dns_name', 'bad;true'),
                                        ('dns_name', 'bad..test'), ('ips', ['127.0.0.1'])])
def test_destination_validation_before_runner(tailnet, bare_runner, field, value):
    device = bare.candidates(tailnet[0], tailnet[1])[0]
    device[field] = value
    with pytest.raises(ValueError):
        bare.argv(device, 'ccagent', 'ssh')
    assert not bare_runner.calls


def test_discovery_failure_and_no_magicdns_fail_closed(lite_client, tailnet, bare_runner, monkeypatch):
    tailnet[0].pop('CurrentTailnet')
    assert lite_client.get('/api/bare', headers=tailnet[2]).json()['candidates'] == []
    monkeypatch.setattr(nodes_discovery, 'read_tailscale', lambda fresh=False: (None, {'ok': False, 'reason': 'Start Tailscale, then try again.'}))
    listing = lite_client.get('/api/bare', headers=tailnet[2]).json()
    assert listing['candidates'] == [] and not listing['tailscale']['ok']
    response = lite_client.post('/api/bare', headers=tailnet[2], json={'ts_id': 'device-1'})
    assert response.status_code == 400 and 'Start Tailscale' in response.json()['error']
    assert not bare_runner.calls


@pytest.mark.parametrize('stream,limit', [('stdout', 32768), ('stderr', 8192)])
def test_runner_stops_stream_at_byte_cap(monkeypatch, stream, limit):
    import io
    import selectors
    from types import SimpleNamespace

    class Stream(io.BytesIO):
        read_sizes = None

        def read(self, size=-1):
            assert 0 < size <= 4096
            self.read_sizes.append(size)
            return super().read(size)

    class Process:
        returncode = 0
        killed = False
        waited = False
        stdin = None

        def kill(self):
            self.killed = True
            self.returncode = -9

        def wait(self, timeout=None):
            self.waited = True
            return self.returncode

    process = Process()
    process.stdout, process.stderr = Stream(), Stream()
    target = Stream(b'\xff' * (limit + 100000))
    target.read_sizes = []
    setattr(process, stream, target)
    getattr(process, 'stderr' if stream == 'stdout' else 'stdout').read_sizes = []

    class Selector:
        def __enter__(self):
            self.keys = {}
            return self

        def __exit__(self, *args):
            pass

        def register(self, fileobj, events, data=None):
            self.keys[fileobj] = SimpleNamespace(fileobj=fileobj, data=data)

        def unregister(self, fileobj):
            del self.keys[fileobj]

        def get_map(self):
            return self.keys

        def select(self, timeout=None):
            return [(key, selectors.EVENT_READ) for key in list(self.keys.values())]

    monkeypatch.setattr(selectors, 'DefaultSelector', Selector)
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **kw: process)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: pytest.fail('must stream instead of subprocess.run'))
    result = PROCESS_RUNNER(['fake-device-command'])
    assert process.killed and process.waited
    assert result.returncode != 0
    assert getattr(result, stream) == '\ufffd' * limit
    assert sum(target.read_sizes) <= limit + 1
    assert process.stdout.closed and process.stderr.closed


def test_remove_forgets_only_device_key_and_closes_master(lite_client, tailnet, bare_runner):
    good(bare_runner)
    lite_client.post('/api/bare', headers=tailnet[2], json={'ts_id': 'device-1'})
    original = bare_runner.calls[0][0]
    known = settings.data_dir / 'bare-ssh' / 'known_hosts'
    known.write_text('device.example.test ssh-ed25519 old\n'
                     'device.example.test ssh-rsa old2\n'
                     'other.example.test ssh-ed25519 keep\n'
                     'device.example.test.extra ssh-ed25519 keep2\n')
    tailnet[0]['Peer']['one']['Online'] = False
    bare_runner.answers = [(255, '', 'Control socket does not exist')]
    result = lite_client.delete('/api/bare/device-1', headers=tailnet[2])
    assert result.status_code == 200 and result.json()['ok']
    assert known.read_text() == ('other.example.test ssh-ed25519 keep\n'
                                 'device.example.test.extra ssh-ed25519 keep2\n')
    close = bare_runner.calls[-1][0]
    assert close[0] == 'ssh' and close[-3:] == ['-O', 'exit', 'ccagent@device.example.test']
    assert next(x for x in close if x.startswith('ControlPath=')) == next(x for x in original if x.startswith('ControlPath='))
    assert main.db.kv_list_prefix(bare.PREFIX) == []


def test_changed_key_offers_forget_and_retry(lite_client, tailnet, bare_runner):
    bare_runner.answers = [(255, '', 'WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!')]
    failed = lite_client.post('/api/bare', headers=tailnet[2], json={'ts_id': 'device-1'}).json()
    assert failed['check']['rows'][0]['action'] == 'forget_host_key'
    known = settings.data_dir / 'bare-ssh' / 'known_hosts'
    known.write_text('device.example.test ssh-ed25519 old\n')
    bare_runner.answers = [(0, '', '')]
    response = lite_client.post('/api/bare/device-1/forget-host-key', headers=tailnet[2], json={})
    assert response.status_code == 200 and response.json()['ok']
    assert known.read_text() == ''
    assert main.db.node_audit_list()[0]['action'] == 'bare_forget_host_key'
    good(bare_runner)
    assert lite_client.post('/api/bare', headers=tailnet[2], json={'ts_id': 'device-1'}).json()['added']


def test_forget_key_revalidates_ownership_and_csrf(lite_client, tailnet, bare_runner):
    path = '/api/bare/device-1/forget-host-key'
    assert lite_client.post(path, headers={'Tailscale-User-Login': tailnet[1]}, json={}).status_code == 403
    tailnet[0]['Peer']['one']['ShareeNode'] = True
    assert lite_client.post(path, headers=tailnet[2], json={}).status_code == 400
    assert bare_runner.calls == []


def test_repo_scan_has_depth_limit_and_prunes_dependencies(tailnet, bare_runner):
    device = bare.candidates(tailnet[0], tailnet[1])[0]
    command = shlex.split(bare.argv(device, 'ccagent', 'repos')[-1])
    assert command[:2] == ['sh', '-c']
    scan = shlex.split(command[2])
    assert scan[scan.index('-maxdepth') + 1] == '4'
    assert r'\( -name node_modules -o -name .venv \) -prune' in command[2]
    assert '-name .git -prune -print' in command[2]


def test_repo_scan_accepts_partial_results(lite_client, tailnet, bare_runner):
    good(bare_runner)
    bare_runner.answers[-1] = (1, './visible/.git\n', 'find: ./private: Permission denied')
    answer = lite_client.post('/api/bare', headers=tailnet[2], json={'ts_id': 'device-1'}).json()
    assert answer['check']['repos'] == ['visible']
    assert answer['check']['rows'][-1]['ok']
    assert answer['check']['ok']


def test_repo_scan_timeout_explains_stopped_listing(lite_client, tailnet, bare_runner):
    good(bare_runner)
    bare_runner.answers[-1] = subprocess.TimeoutExpired(['unused'], 12)
    answer = lite_client.post('/api/bare', headers=tailnet[2], json={'ts_id': 'device-1'}).json()
    row = answer['check']['rows'][-1]
    assert not row['ok']
    assert 'listing the projects folder took too long; it was stopped' in row['message'].lower()
    assert 'could not be read' not in row['message']
    assert 'Create that folder' not in row['fix']


@pytest.mark.parametrize('error,message,fix,action', [
    ((255, '', '@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@\nHost key verification failed.'),
     'host key has changed', 'Verify', 'forget_host_key'),
    ((255, '', 'ccagent@device: Permission denied (tailscale).'),
     'tailnet rule denied', 'docs/nodes/device-check.md', None),
    ((255, '', 'tailscale: access denied by ACL'),
     'tailnet rule denied', 'SSH ACL', None),
    ((255, '', 'ssh: connect to host 100.64.0.2 port 22: Connection timed out'),
     'SSH connection timed out', 'Wake', None),
    (subprocess.TimeoutExpired(['unused'], 12), 'SSH connection timed out', 'Wake', None),
    (FileNotFoundError(2, 'No such file or directory', 'ssh'), 'SSH is not installed on the board host', 'Install', None),
    ((255, '', 'unknown private diagnostic'), 'SSH could not reach', 'Tailscale SSH', None),
])
def test_ssh_failure_sentences(lite_client, tailnet, bare_runner, error, message, fix, action):
    bare_runner.answers = [error]
    answer = lite_client.post('/api/bare', headers=tailnet[2], json={'ts_id': 'device-1'}).json()
    row = answer['check']['rows'][0]
    assert message in row['message']
    assert fix in row['fix']
    assert row.get('action') == action
    assert 'unknown private diagnostic' not in str(answer)
    assert not answer['added'] and len(bare_runner.calls) == 1


def test_unrun_identity_rows_are_skipped(lite_client, tailnet, bare_runner):
    bare_runner.answers = [(255, '', 'Connection timed out')]
    answer = lite_client.post('/api/bare', headers=tailnet[2], json={'ts_id': 'device-1'}).json()
    for row in answer['check']['rows'][1:]:
        assert row['skipped'] is True
        assert 'refused' not in row['fix']


@pytest.mark.parametrize('seen,expected', [('2026-10-17T12:00:00Z', 'last seen 12 min ago'),
                                         ('invalid timestamp', 'last seen unknown')])
def test_offline_last_seen_is_relative(tailnet, bare_runner, monkeypatch, seen, expected):
    from datetime import datetime
    monkeypatch.setattr(bare.time, 'time', lambda: datetime.fromisoformat('2026-10-17T12:12:00+00:00').timestamp())
    device = bare.candidates(tailnet[0], tailnet[1])[0]
    device.update(online=False, last_seen=seen)
    row = bare.check(device, 'ccagent', 'projects')['rows'][0]
    assert expected in row['message']
    assert seen not in row['message']
    assert row['last_seen'] == seen
    assert bare_runner.calls == []
