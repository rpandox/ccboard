import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC } from './harness.mjs';
import { calls, fakeState, homeWorld, page, tick, text } from './world.mjs';

const FX = JSON.parse(fs.readFileSync(path.join(STATIC, 'demo/options.json'), 'utf8'));
const INFO = { method: 'docker', expect_seconds: 10, hint: 'docker compose up -d --force-recreate ccboard', logs: 'docker compose logs ccboard', blockers: [] };
const settle = async () => { for (let i = 0; i < 5; i++) await tick(); };
const button = (w, label) => page(w).querySelectorAll('button').find(n => text(n) === label);
async function world(t, info = INFO) {
  const st = fakeState({ boot: { id: 'before' }, restart: structuredClone(info), settings_pending: { count: 1, labels: ['Task lanes'] } });
  const { w } = homeWorld({ state: st });
  w.ctx.__answers['/api/settings/options'] = { ...structuredClone(FX), restart: structuredClone(info) };
  w.ctx.__answers['/api/settings/restart'] = { ok: true, method: info.method, boot_id: 'before', expect_seconds: 10 };
  w.location.hash = '#/settings?sec=options';
  await settle();
  t.after(() => w.run('clearTimeout(settingsRestart.timer); settingsRestart.active = false'));
  return w;
}

test('first tap arms, second confirms; busy warnings require a further forced confirmation', async t => {
  const w = await world(t);
  let requests = 0;
  w.ctx.__answers['/api/settings/restart'] = () => {
    if (requests++ === 0) { const e = new Error('busy'); e.body = { reason: 'busy', blockers: ['1 permission is waiting for an answer and will be lost'] }; throw e; }
    return { boot_id: 'before', expect_seconds: 10 };
  };
  button(w, 'Restart board').click();
  assert.ok(button(w, 'Confirm Restart board').classList.contains('confirm'));
  assert.equal(calls(w).filter(c => c.method === 'POST').length, 0);
  button(w, 'Confirm Restart board').click(); await settle();
  assert.match(text(page(w)), /1 permission is waiting for an answer and will be lost/);
  assert.ok(button(w, 'Cancel'));
  button(w, 'Confirm Restart board').click(); await settle();
  assert.deepEqual(calls(w).filter(c => c.method === 'POST').map(c => c.body), [{ force: false }, { force: true }]);
  assert.match(text(page(w)), /Restarting\. Back in about 10 seconds/);
});

test('new boot ends polling, reloads state and clears pending banner', async t => {
  const w = await world(t);
  button(w, 'Restart board').click(); button(w, 'Confirm Restart board').click(); await settle();
  w.ctx.__answers['/api/settings/options'] = { ...structuredClone(FX), pending: [], restart: INFO };
  w.ctx.__answers['/api/state'] = fakeState({ boot: { id: 'after' }, restart: INFO, settings_pending: { count: 0 } });
  await w.run('settingsRestartPoll()'); await settle();
  assert.equal(w.run('settingsRestart.active'), false);
  assert.equal(page(w).querySelector('.set-pending'), null);
  assert.match(JSON.stringify(w.ctx.__toasts), /Board restarted/);
  const before = calls(w).length;
  await w.run('settingsRestartPoll()');
  assert.equal(calls(w).length, before);
});

test('offline retries, then 90 second timeout gives logs, hand command and another attempt', async t => {
  const w = await world(t);
  button(w, 'Restart board').click(); button(w, 'Confirm Restart board').click(); await settle();
  w.ctx.__answers['/api/state'] = () => { throw new Error('offline'); };
  await w.run('settingsRestartPoll()');
  assert.match(text(page(w)), /Offline, retrying/);
  w.run('settingsRestart.at = Date.now() - 90001');
  await w.run('settingsRestartPoll()');
  assert.match(text(page(w)), /No new board process after 90 seconds/);
  assert.match(text(page(w)), /docker compose logs ccboard/);
  assert.match(text(page(w)), /docker compose up -d --force-recreate ccboard/);
  assert.ok(button(w, 'Restart board'));
});

test('unsupported runtime shows a reason and hand command without a button', async t => {
  const w = await world(t, { method: 'none', why: 'No supervisor runs this board.', hint: 'uvicorn app.main:app' });
  assert.equal(button(w, 'Restart board'), undefined);
  assert.match(text(page(w)), /No supervisor runs this board/);
  assert.match(text(page(w)), /uvicorn app.main:app/);
});

test('rate refusal and lost response always offer the next step', async t => {
  const w = await world(t);
  w.ctx.__answers['/api/settings/restart'] = () => { const e = new Error('rate'); e.body = { reason: 'rate', retry_after: 42 }; throw e; };
  button(w, 'Restart board').click(); button(w, 'Confirm Restart board').click(); await settle();
  assert.match(text(page(w)), /Wait 42 seconds, then try again/);
  assert.ok(button(w, 'Restart board'));
  w.ctx.__answers['/api/settings/restart'] = () => { throw new Error('offline'); };
  button(w, 'Restart board').click(); button(w, 'Confirm Restart board').click(); await settle();
  assert.match(text(page(w)), /Restart was not confirmed/);
  assert.match(text(page(w)), /docker compose logs ccboard/);
});

test('demo simulates a restart with no request', async t => {
  const w = await world(t);
  w.ctx.demoOn = () => true;
  await w.run('settingsRestartSend()');
  await w.run('settingsRestartPoll(true)');
  assert.equal(calls(w).filter(c => c.method === 'POST').length, 0);
  assert.equal(w.run('settingsRestart.active'), false);
  assert.match(JSON.stringify(w.ctx.__toasts), /Board restarted/);
});


test('arming previews readiness without posting a restart', async t => {
  const w = await world(t);
  w.ctx.__answers['/api/settings/options'] = ({ path }) => path.includes('readiness=1')
    ? { restart: { ...INFO, blockers: ['a backup is running'] } } : FX;
  button(w, 'Restart board').click(); await settle();
  assert.match(text(page(w)), /a backup is running/);
  assert.ok(button(w, 'Confirm Restart board'));
  assert.equal(calls(w).filter(c => c.method === 'POST').length, 0);
});

test('launchd timeout names the detached launcher output log', async t => {
  const launch_log = '/data/ccboard/restart.log';
  const w = await world(t, { ...INFO, method: 'launchd', launch_log, logs: `tail -n 100 ${launch_log}` });
  await w.run('settingsRestartSend()');
  w.run('settingsRestart.at = Date.now() - 90001');
  await w.run('settingsRestartPoll()');
  assert.match(w.run('settingsRestart.message'), /90 seconds.*\/data\/ccboard\/restart\.log/);
});

test('hourly limit displays the server sentence', async t => {
  const w = await world(t);
  w.ctx.__answers['/api/settings/restart'] = () => {
    const e = new Error('rate');
    e.body = { reason: 'rate', retry_after: 3200, message: 'At most 6 board restarts are allowed per hour. Try again later.' };
    throw e;
  };
  await w.run('settingsRestartSend()');
  assert.match(text(page(w)), /At most 6 board restarts are allowed per hour\. Try again later\./);
});
