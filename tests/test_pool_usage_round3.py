"""Verification regressions for P2 round 3; only scratch profiles and fake CLIs."""
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

from app import pool_usage as pu, profiles, usage_refresh
from app.agents import claude_pane, codex_rollout as cr
from .test_pool_usage import box, account, attempt, cache  # noqa: F401

# Pane shape follows test_esc_dismissed_permission and the Claude pane fixtures.
PROMPT = (Path(__file__).parent / 'fixtures/agents/claude/pane_waiting.txt').read_text()
USAGE = (Path(__file__).parent / 'fixtures/agents/claude/pane_usage.txt').read_text()


@pytest.mark.parametrize('screen,returns,escape', [
    (USAGE, True, True), (USAGE, False, True),
    ('Settings: Config\nSelect a model\n Esc to cancel', True, False),
    (USAGE + '\nDo you want to proceed?\n❯ 1. Yes\n 2. No\n Esc to cancel', True, False),
    (USAGE + '\nSelect a model\n Esc to cancel', True, False),
    (PROMPT, True, False),
], ids=['usage', 'stuck', 'other-dialog', 'permission', 'later-dialog', 'prompt'])
def test_usage_panel_cleanup(box, monkeypatch, screen, returns, escape):
    from app import main
    now = int(time.time())
    path = account(box)
    attempt(box, at=now - 60)
    box.set_state('pane-one', 'idle', 'Stop')
    monkeypatch.setattr(main, 'db', box)
    monkeypatch.setattr(usage_refresh.tmux, 'list_sessions', lambda: {'pane-one': {'command': 'claude', 'attached': 0}})
    monkeypatch.setattr(usage_refresh.tmux, 'viewers', lambda: {})
    monkeypatch.setattr(usage_refresh.tmux, 'has_session', lambda _: True)
    pane, timers, keys = [PROMPT], [], []
    monkeypatch.setattr(usage_refresh.tmux, 'capture', lambda *a, **kw: pane[0])
    monkeypatch.setattr(usage_refresh.tmux, 'send_text', lambda *a, **kw: None)
    def send_keys(*args):
        keys.append(args)
        if returns:
            pane[0] = PROMPT
    monkeypatch.setattr(usage_refresh.tmux, 'send_keys', send_keys)
    monkeypatch.setattr(usage_refresh, 'later', lambda delay, fn: timers.append(fn))
    usage_refresh.release()
    c = pu.collector(box)
    c.tick(now)
    assert len(timers) == 1
    pane[0] = screen
    cache(path, now)
    assert claude_pane.screen_state(USAGE) == 'dialog'
    timers.pop()()
    assert keys == ([('pane-one', ['Escape'])] if escape else [])
    rec = box.kv_get('pool_refresh:one')['value']
    if escape and returns:
        assert pu.groups_view(box, now)[0]['source'] == 'cli'
        assert rec['misses'] == 0
    else:
        assert rec['misses'] >= 1
        assert pu.groups_view(box, now)[0]['source'] is None
        if escape:
            assert rec['next_at'] >= now + 3600
            assert not c.active_due('one', now + 3599)
    usage_refresh.release()


def test_same_account_launch_cancels_slow_child(box, monkeypatch, tmp_path):
    account(box, provider='codex')
    n = attempt(box, provider='codex')
    profiles.finish_attempt(box, n, 'done')
    exe, pidfile = tmp_path / 'slow-codex', tmp_path / 'pid'
    exe.write_text(f'#!{sys.executable}\nimport os, time\nopen({str(pidfile)!r}, "w").write(str(os.getpid()))\ntime.sleep(15)\n')
    exe.chmod(0o700)
    monkeypatch.setattr(pu.shutil, 'which', lambda _: str(exe))
    killed, killpg = [], os.killpg
    monkeypatch.setattr(pu.os, 'killpg', lambda pid, sig: (killed.append((pid, sig)), killpg(pid, sig))[-1])
    c = pu.collector(box)
    worker = threading.Thread(target=c.tick, daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 3
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert pidfile.exists()
        before = time.monotonic()
        attempt(box, provider='codex')
        elapsed = time.monotonic() - before
        worker.join(2)
        assert elapsed < 1.3, f'launch took {elapsed:.2f}s'
        assert not worker.is_alive()
        assert (int(pidfile.read_text()), signal.SIGKILL) in killed
        assert pu.groups_view(box)[0]['source'] is None
    finally:
        if pidfile.exists():
            try:
                killpg(int(pidfile.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
        worker.join(2)


@pytest.mark.parametrize('grouped', [False, True])
def test_busy_identity_does_not_block_next_account(box, grouped):
    now = int(time.time())
    account(box)
    path = account(box, 'two', seat='two')
    cache(path, now)
    if grouped:
        pu.ensure_groups(box)
    entered, unblock = threading.Event(), threading.Event()
    def hold():
        with profiles.account_lock('one'):
            entered.set()
            unblock.wait(3)
    worker = threading.Thread(target=hold)
    worker.start()
    assert entered.wait(1)
    try:
        before = time.monotonic()
        pu.collector(box).tick(now, active=False)
        assert time.monotonic() - before < .5
        assert next(g for g in pu.groups_view(box) if g['accounts'] == ['two'])['state'] == 'ok'
    finally:
        unblock.set()
        worker.join()


@pytest.mark.parametrize('age,source', [(200, 'statusline'), (5, 'cache')])
def test_common_freshness_ranking(box, age, source):
    now = int(time.time())
    path = account(box)
    attempt(box, at=now - 900)
    box.set_state('pane-one', 'idle', 'Stop')
    box.conn.execute('UPDATE sessions SET state_at=?', (pu.iso(now - 2),))
    cache(path, now - age)
    pu.observe_statusline(box, 'pane-one', {'five_hour': {'used_percentage': 42}}, now)
    pu.collector(box).tick(now, active=False)
    assert pu.groups_view(box, now)[0]['source'] == source


def test_group_exception_is_unknown_and_other_group_collects(box, monkeypatch):
    now = int(time.time())
    account(box)
    path = account(box, 'two', seat='two')
    cache(path, now)
    c = pu.collector(box)
    original = c.sample
    def sample(a, *args, **kw):
        if a['id'] == 'one':
            raise RuntimeError('must not expose profile data')
        return original(a, *args, **kw)
    monkeypatch.setattr(c, 'sample', sample)
    c.tick(now, active=False)
    groups = {g['accounts'][0]: g for g in pu.groups_view(box, now)}
    assert groups['one']['state'] == 'unknown' and groups['one']['reason']
    assert 'profile data' not in groups['one']['reason']
    assert groups['two']['state'] == 'ok'


def test_ensure_failure_backs_off_due_zero(box, monkeypatch):
    c = pu.collector(box)
    c.due[1] = 0
    monkeypatch.setattr(pu, 'ensure_groups', lambda *a: (_ for _ in ()).throw(ValueError('bad')))
    c.tick(1000)
    assert c.wait_delay(1000) >= 60


@pytest.mark.parametrize('payload', ['[' * 2000 + ']' * 2000, '{"n":' + '1' * 5000 + '}'])
def test_unreadable_identity_and_transcript(box, monkeypatch, tmp_path, payload):
    now = int(time.time())
    path = account(box)
    (path / '.claude.json').write_text(payload)
    pu.collector(box).tick(now, active=False)
    assert pu.groups_view(box, now)[0]['reason'] == 'identity file unavailable'
    file = tmp_path / 'transcript.jsonl'
    file.write_text(payload + '\n')
    monkeypatch.setattr(pu, 'attempt_path', lambda *a: file)
    assert pu.transcript_tokens({'started_at': pu.iso(now - 1), 'ended_at': None}, now) is None


def test_codex_expired_reset_dropped_before_save(box):
    now = int(time.time())
    account(box, provider='codex')
    raw = {'primary': {'used_percent': 90, 'window_minutes': 300, 'resets_at': now - 1},
           'secondary': {'used_percent': 20, 'window_minutes': 10080, 'resets_at': now + 600}}
    reading = pu.codex_windows(raw)
    assert reading == {'weekly_pct': 20, 'weekly_reset_at': pu.iso(now + 600)}


@pytest.mark.parametrize('field,period', [('window_5h_pct', 18000), ('weekly_pct', 7 * 86400)])
def test_no_reset_window_expires_from_sample(box, field, period):
    now = int(time.time())
    account(box)
    group = pu.ensure_groups(box)[0]['quota_group_id']
    pu.save(box, group, {field: 50}, 'cache', 1, now)
    assert pu.groups_view(box, now + period - 1)[0][field] == 50
    assert pu.groups_view(box, now + period)[0][field] is None


def test_partial_transcript_eof_retried(box, monkeypatch, tmp_path):
    now = int(time.time())
    file = tmp_path / 'partial.jsonl'
    line = json.dumps({'timestamp': pu.iso(now), 'type': 'assistant', 'message': {'id': 'm', 'usage': {'input_tokens': 123}}})
    file.write_text(line[:40])
    monkeypatch.setattr(pu, 'attempt_path', lambda *a: file)
    a = {'started_at': pu.iso(now - 10), 'ended_at': None}
    assert pu.transcript_tokens(a, now) is None
    assert pu._TRANSCRIPTS[str(file)]['offset'] == 0
    with file.open('a') as f:
        f.write(line[40:] + '\n')
    assert pu.transcript_tokens(a, now) == 123


def test_codex_excludes_only_recent_or_open_ids(box, monkeypatch):
    now = int(time.time())
    account(box, provider='codex')
    ids = ['11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222', '33333333-3333-4333-8333-333333333333']
    for sid, ended in zip(ids, [None, pu.iso(now - 6 * 86400), pu.iso(now - 8 * 86400)]):
        box.conn.execute("INSERT INTO runtime_attempt(n,account_id,provider,profile_dir,started_at,ended_at,provider_session_id) VALUES (1,'one','codex','unused',?,?,?)", (pu.iso(now - 9 * 86400), ended, sid))
    captured = []
    monkeypatch.setattr(cr, '_readings', lambda now, exclude=None: (captured.append(exclude), [])[1])
    cr.Tailer().rate_limits(box, now)
    assert captured == [set(ids[:2])]


def test_codex_exclusion_uses_id_lookup(monkeypatch):
    sid = '11111111-1111-4111-8111-111111111111'
    class LookupOnly(set):
        def __iter__(self):
            pytest.fail('must not scan every excluded id per rollout')
    monkeypatch.setattr(cr, 'recent_rollouts', lambda _: [(1, 1, Path('rollout-test-' + sid + '.jsonl'))])
    monkeypatch.setattr(cr, '_rollout_reading', lambda *a: pytest.fail('excluded rollout read'))
    assert cr._readings(time.time(), exclude=LookupOnly([sid])) == []


@pytest.mark.parametrize('error', [PermissionError, ProcessLookupError])
def test_killpg_errors_still_reap_and_close(box, monkeypatch, error):
    import subprocess
    account(box, provider='codex')
    monkeypatch.setattr(pu.shutil, 'which', lambda _: 'fake')
    processes, popen = [], subprocess.Popen
    def child(*args, **kw):
        proc = popen([sys.executable, '-c', 'pass'], **kw)
        processes.append(proc)
        return proc
    monkeypatch.setattr(pu.subprocess, 'Popen', child)
    monkeypatch.setattr(pu.os, 'killpg', lambda *a: (_ for _ in ()).throw(error()))
    assert pu.app_server({'id': 'one'}) is None
    proc = processes[0]
    assert proc.returncode is not None
    assert proc.stdin.closed and proc.stdout.closed


def test_estimate_and_local_reads_do_not_hold_launch_lock(box, monkeypatch):
    account(box)
    c = pu.collector(box)
    checked = []
    def check(*a, **kw):
        acquired = []
        def other_thread():
            lock = profiles.account_lock('one')
            if lock.acquire(blocking=False):
                acquired.append(True)
                lock.release()
        t = threading.Thread(target=other_thread)
        t.start(); t.join(1)
        assert acquired, 'file parsing holds account_lock'
        checked.append(True)
    monkeypatch.setattr(c, 'sample', check)
    monkeypatch.setattr(c, 'estimate', check)
    c.tick(active=False)
    assert len(checked) == 2


@pytest.mark.parametrize('age,source', [(200, 'statusline'), (5, 'cache')])
def test_best_uses_effective_confidence_for_every_source(age, source):
    now = int(time.time())
    candidates = [({'window_5h_pct': 80}, now - age, 'cache', 1),
                  ({'window_5h_pct': 42}, now - 2, 'statusline', .95)]
    assert pu.best(candidates)[2] == source


@pytest.mark.parametrize('error', [RecursionError, ValueError])
def test_json_decode_failure_is_unreadable(box, monkeypatch, tmp_path, error):
    now = int(time.time())
    profile = account(box)
    file = tmp_path / 'bad.jsonl'
    payload = '{"broken-test-json":1}'
    (profile / '.claude.json').write_text(payload)
    file.write_text(payload + '\n')
    original = json.loads
    def loads(data, *a, **kw):
        if b'broken-test-json' in (data.encode() if isinstance(data, str) else data):
            raise error('private data')
        return original(data, *a, **kw)
    monkeypatch.setattr(pu.json, 'loads', loads)
    pu.ensure_groups(box, now)
    assert box._pool_identity_errors['one'] == 'identity file unavailable'
    monkeypatch.setattr(pu, 'attempt_path', lambda *a: file)
    assert pu.transcript_tokens({'started_at': pu.iso(now - 1), 'ended_at': None}, now) is None


def test_release_cancels_sampling_before_moving_credentials(box, monkeypatch):
    account(box, provider='codex')
    n = attempt(box, provider='codex')
    profiles.finish_attempt(box, n, 'done')
    monkeypatch.setattr(profiles, '_check_liveness', lambda db: None)
    monkeypatch.setattr(pu.shutil, 'which', lambda _: 'fake')
    entered, cancelled = threading.Event(), threading.Event()
    def probe(a):
        entered.set()
        if a['_sampling_cancel'].wait(2):
            cancelled.set()
    monkeypatch.setattr(pu, 'app_server', probe)
    worker = threading.Thread(target=pu.collector(box).tick)
    worker.start()
    try:
        assert entered.wait(1)
        with pytest.raises(profiles.BadRequest, match='no unique handover slot'):
            profiles.release(box, 'one')
        assert cancelled.is_set(), 'release reached credential work before cancelling sampler'
    finally:
        worker.join(3)
    assert not worker.is_alive()
