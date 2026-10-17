import assert from 'node:assert/strict';
import { test } from 'node:test';
import fs from 'node:fs';
import vm from 'node:vm';
import { randomBytes } from 'node:crypto';
import { homeWorld, page, text, tick, calls } from './world.mjs';

const source = fs.readFileSync(new URL('../../app/static/local-signin.js', import.meta.url), 'utf8');

for (const ok of [true, false]) test(`fragment removed before redemption; success=${ok}`, async () => {
  const token = randomBytes(32).toString('base64url');
  const events = [];
  const status = { textContent: '' };
  const context = {
    document: { getElementById: () => status },
    window: { location: { hash: '#' + token, replace: path => events.push(['navigate', path]) },
      history: { replaceState: (_, __, path) => events.push(['erase', path]) } },
    fetch: async (path, options) => { events.push(['fetch', path, options]); return { ok }; },
  };
  await vm.runInNewContext(source, context);
  assert.deepEqual(events[0], ['erase', '/auth/local']);
  assert.equal(events[1][1], '/auth/local/redeem');
  assert.equal(events[1][2].body, token);
  assert.equal(events[1][2].headers['X-CCBoard'], '1');
  assert.equal(events[1][2].cache, 'no-store');
  if (ok) assert.deepEqual(events[2], ['navigate', '/']);
  else assert.match(status.textContent, /Run bin\/ccboard-open/);
  assert.ok(!status.textContent.includes(token));
});

async function settingsWorld(enabled, sessions = [{ created: 100, last_seen: 200 }]) {
  const { w } = homeWorld();
  w.ctx.__local = { enabled, sessions };
  w.run(`__answers['/api/auth/local/sessions'] = req => req.method === 'GET' ? __local : {ok: true};`);
  w.location.hash = '#/settings?sec=app';
  await tick(); await tick();
  return w;
}
const button = (w, label) => page(w).querySelectorAll('button').find(b => text(b).trim() === label);
const localPanel = w => page(w).querySelectorAll('section').find(node => node.getAttribute('aria-label') === 'Local sign-in');

test('App labels the current session as This browser and others as Browser', async () => {
  const w = await settingsWorld(true, [
    { created: 100, last_seen: 200, current: true },
    { created: 300, last_seen: 400, current: false },
  ]);
  const panel = localPanel(w);
  const labels = panel.querySelectorAll('.k').map(node => text(node));
  assert.deepEqual(labels, ['This browser', 'Browser', 'Sign out']);
});

for (const empty of [false, true]) test(`App explains local sign-in above rows; empty=${empty}`, async () => {
  const w = await settingsWorld(true, empty ? [] : [{ created: 100, last_seen: 200 }]);
  const panelText = text(localPanel(w));
  const instructions = 'On this machine, run bin/ccboard-open from the ccboard folder; the link works once, for 60 seconds';
  assert.ok(panelText.includes(instructions));
  const rowText = empty ? 'No browser is signed in locally.' : 'Browser';
  assert.ok(panelText.indexOf(instructions) < panelText.indexOf(rowText));
  assert.equal(panelText.includes('No browser is signed in locally.'), empty);
  assert.ok(button(w, 'Sign out all local sessions'));
});

test('App lists timestamps only when enabled and sign out requires confirmation', async () => {
  const w = await settingsWorld(true);
  assert.match(text(page(w)), /Created .*last seen/);
  assert.ok(button(w, 'Sign out all local sessions'));
  button(w, 'Sign out all local sessions').click();
  await tick(); await tick();
  assert.equal(calls(w).filter(c => c.method === 'DELETE').length, 0);
  button(w, 'Confirm Sign out all local sessions').click();
  await tick(); await tick();
  assert.equal(calls(w).filter(c => c.method === 'DELETE' && c.path === '/api/auth/local/sessions').length, 1);
  assert.match(text(page(w)), /All local sessions and pending links signed out/);
});

test('App hides local sign-in when disabled', async () => {
  const w = await settingsWorld(false);
  assert.equal(button(w, 'Sign out all local sessions'), undefined);
  assert.ok(!text(page(w)).includes('Local sign-in'));
});

test('failed sign-out leaves the sessions and reports the error', async () => {
  const w = await settingsWorld(true);
  w.run(`__localErrors = []; setError = value => __localErrors.push(value);
    __answers['/api/auth/local/sessions'] = req => { if (req.method === 'DELETE') throw new Error('refused'); return __local; };`);
  button(w, 'Sign out all local sessions').click();
  await tick(); await tick();
  button(w, 'Confirm Sign out all local sessions').click();
  await tick(); await tick();
  assert.equal(w.get('__localErrors').at(-1), 'refused');
  assert.match(text(page(w)), /Created .*last seen/);
});
