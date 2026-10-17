import assert from 'node:assert/strict';
import { test } from 'node:test';
import { calls, homeWorld, page, tick, text } from './world.mjs';

const settle = async () => { for (let i = 0; i < 8; i++) await tick(); };
const candidate = { ts_id: 'device-1', name: 'Device', os: 'linux', tags: ['tag:lab'] };
const check = { ok: false, rows: [
  { key: 'ssh', ok: true, message: 'SSH works for ccagent on Device.', fix: '' },
  { key: 'tmux', ok: false, message: 'tmux is not installed for ccagent on Device.', fix: 'Install it, then Check again.' },
], repos: ['group/repo'], login_url: null };
const device = { ...candidate, user: 'ccagent', folder: 'projects', check };
const section = (w) => page(w).querySelector('.bare-section');
const button = (w, word) => section(w).querySelectorAll('button').find((b) => text(b) === word);
function world(over = {}) {
  const { w } = homeWorld();
  let saved = [];
  w.ctx.__answers['/api/bare'] = (r) => {
    if (r.method === 'GET') return { candidates: [candidate], devices: saved, tailscale: { ok: true } };
    if (r.method === 'DELETE') { saved = []; return { ok: true }; }
    const result = { added: true, device, check, ...over };
    if (result.added) saved = [result.device];
    return result;
  };
  w.location.hash = '#/settings?sec=nodes';
  return w;
}

test('explicit add/check/remove renders outcomes, fixes and repos; board polls do not run checks', async () => {
  const w = world();
  await settle();
  assert.match(text(section(w)), /Devices without a board/);
  assert.match(text(section(w)), /linux · online · tag:lab/);
  assert.equal(calls(w).filter((c) => c.path === '/api/bare').length, 1);
  section(w).querySelector('select').value = 'device-1';
  button(w, 'Add and check').click();
  await settle();
  assert.deepEqual(calls(w).find((c) => c.method === 'POST' && c.path === '/api/bare').body,
    { ts_id: 'device-1', user: 'ccagent', folder: 'projects' });
  assert.match(text(section(w)), /✓SSH works/);
  assert.match(text(section(w)), /✕tmux is not installed/);
  assert.match(text(section(w)), /Install it, then Check again/);
  assert.match(text(section(w)), /group\/repo/);
  button(w, 'Check again').click();
  await settle();
  assert.equal(calls(w).filter((c) => c.path === '/api/bare/device-1/check').length, 1);
  const before = calls(w).filter((c) => c.path.startsWith('/api/bare')).length;
  w.run('updateCurrentPage(state)');
  await settle();
  assert.equal(calls(w).filter((c) => c.path.startsWith('/api/bare')).length, before);
  button(w, 'Remove device').click();
  await settle();
  assert.equal(section(w).querySelectorAll('.bare-device').length, 0);
  assert.ok(calls(w).some((c) => c.method === 'DELETE' && c.path === '/api/bare/device-1'));
});

test('refusal stays visible, remote text is text, and only a Tailscale check link is offered', async () => {
  const unsafe = { ...check, login_url: 'https://untrusted.test/check', rows: [{ key: 'groups', ok: false, message: '<img src=x> Account refused.', fix: 'Use a non-admin user.' }] };
  const w = world({ added: false, check: unsafe, device: { ...device, check: unsafe } });
  await settle();
  section(w).querySelector('select').value = 'device-1';
  button(w, 'Add and check').click();
  await settle();
  assert.match(text(section(w)), /was refused and was not added/);
  assert.match(text(section(w)), /<img src=x>/);
  assert.equal(section(w).querySelector('img'), null);
  assert.equal(section(w).querySelector('a'), null);
  assert.equal(button(w, 'Remove device'), undefined);
});

test('Tailscale prompt offers Retry, failures retain form values and explain the error', async () => {
  const w = world({ added: false, check: { ...check, login_url: 'https://login.tailscale.com/a/check' } });
  await settle();
  section(w).querySelector('select').value = 'device-1';
  button(w, 'Add and check').click();
  await settle();
  assert.ok(button(w, 'Retry'));
  assert.equal(section(w).querySelector('a').getAttribute('href'), 'https://login.tailscale.com/a/check');
  section(w).querySelectorAll('input')[0].value = 'worker';
  w.ctx.__answers['/api/bare'] = () => { throw new Error('Wake the device and try again.'); };
  button(w, 'Refresh devices').click();
  await settle();
  assert.match(text(section(w)), /Wake the device and try again/);
  assert.equal(section(w).querySelectorAll('input')[0].value, 'worker');
});

test('bare audit actions name devices and their actual outcome', async () => {
  const w = world();
  await settle();
  assert.equal(w.run("NodeView.auditWord('bare_check', false)"), 'Device check failed');
  assert.equal(w.run("NodeView.auditWord('bare_add', true)"), 'Device added');
  assert.equal(w.run("NodeView.auditWord('bare_remove', true)"), 'Device removed');
  assert.equal(w.run("NodeView.auditWord('bare_remove', false)"), 'Device not removed');
});

test('a pending request cannot duplicate an action or paint into a disposed page', async () => {
  const w = world();
  await settle();
  let finish;
  w.ctx.__answers['/api/bare'] = () => new Promise((resolve) => { finish = resolve; });
  section(w).querySelector('select').value = 'device-1';
  button(w, 'Add and check').click();
  await tick();
  button(w, 'Add and check').click();
  await tick();
  assert.equal(calls(w).filter((c) => c.method === 'POST' && c.path === '/api/bare').length, 1);
  w.run('settingsNdDispose()');
  finish({ added: true, device, check });
  await settle();
  assert.equal(w.run('settingsBare.result'), null);
  assert.equal(w.run('settingsBare.ui'), null);
});

test('changed host key offers an explicit Forget host key action', async () => {
  const failed = { ...check, rows: [{ key: 'ssh', ok: false, message: 'The host key changed.', fix: 'Verify it first.', action: 'forget_host_key' }] };
  const w = world({ added: false, check: failed, device: { ...device, check: failed } });
  await settle();
  section(w).querySelector('select').value = 'device-1';
  button(w, 'Add and check').click();
  await settle();
  assert.ok(button(w, 'Forget host key'));
  button(w, 'Forget host key').click();
  await settle();
  assert.ok(calls(w).some((c) => c.method === 'POST' && c.path === '/api/bare/device-1/forget-host-key' && c.body.user === 'ccagent'));
});

for (const key of ['ssh', 'user', 'groups', 'tmux']) {
  test(`not-added wording distinguishes an actual ${key} failure from skipped checks`, async () => {
    const failed = { ...check, rows: [{ key, ok: false, message: 'Failed.', fix: 'Check again.' },
      ...['user', 'groups'].filter((k) => k !== key).map((k) => ({ key: k, ok: false, skipped: true, message: 'Did not run.' }))] };
    const w = world({ added: false, check: failed, device: { ...device, check: failed } });
    await settle();
    section(w).querySelector('select').value = 'device-1';
    button(w, 'Add and check').click();
    await settle();
    const content = text(section(w));
    if (key === 'user' || key === 'groups') assert.match(content, /Device was refused and was not added\./);
    else {
      assert.match(content, /Not added yet: fix the rows marked ✕ and check again\./);
      assert.doesNotMatch(content, /Device was refused/);
    }
  });
}

test('device copy describes later sessions without phase wording', async () => {
  const w = world();
  await settle();
  assert.doesNotMatch(text(section(w)), /This phase checks devices/);
  assert.match(text(section(w)), /Sessions on this device come in a later version\./);
});

for (const seen of [new Date(Date.now() - 12 * 60000).toISOString(), 'invalid timestamp']) {
  test(`offline last seen renders a relative age or unknown: ${seen}`, async () => {
    const offline = { ...check, rows: [{ key: 'ssh', ok: false, last_seen: seen, message: `Device is offline (last seen ${seen}). Nothing was started.`, fix: 'Wake it.' }] };
    const w = world({ check: offline, device: { ...device, check: offline } });
    await settle();
    section(w).querySelector('select').value = 'device-1';
    button(w, 'Add and check').click();
    await settle();
    assert.match(text(section(w)), seen === 'invalid timestamp' ? /last seen unknown/ : /last seen 12 min ago/);
    assert.ok(!text(section(w)).includes(seen));
  });
}
