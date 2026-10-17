import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC, makeWorld } from './harness.mjs';
import { calls, fakeState, homeWorld, page, tick, text } from './world.mjs';

const FX = JSON.parse(fs.readFileSync(path.join(STATIC, 'demo/options.json'), 'utf8'));
const panel = (w) => page(w).querySelector('.settings-panel[data-sec=options]');
const settle = async () => { for (let i = 0; i < 4; i++) await tick(); };
function world(answer = FX, route = '#/settings?sec=options') {
  const { w } = homeWorld({ state: fakeState() });
  w.ctx.__answers['/api/settings/options'] = answer;
  w.location.hash = route;
  return w;
}

test('Options follows Box and renders all nine registry groups and every key once', async () => {
  const w = world();
  await settle();
  const ids = w.run('SETTINGS_SECTIONS.map(s => s.id).join()');
  assert.match(ids, /box,options,doctor/);
  assert.deepEqual(panel(w).querySelectorAll('.set-options-group').map(n => n.getAttribute('data-group')), FX.groups.map(g => g.id));
  assert.deepEqual(panel(w).querySelectorAll('.set-options-group .set-h').map(text), FX.groups.map(g => g.label));
  const keys = panel(w).querySelectorAll('.set-option').map(n => n.getAttribute('data-key'));
  assert.equal(keys.length, FX.options.length);
  assert.equal(new Set(keys).size, keys.length);
  assert.equal(panel(w).querySelectorAll('.primary').length, 0);
});

test('secret rows hide even hostile values, defaults and copy lines; words accompany chips', async () => {
  const fixture = structuredClone(FX);
  const secret = fixture.options.find(o => o.secret);
  secret.value = secret.default = secret.line = 'do-not-draw-this';
  const w = world(fixture);
  await settle();
  assert.doesNotMatch(text(panel(w)), /do-not-draw-this/);
  const row = panel(w).querySelector('[data-key=CCBOARD_HUB_TOKEN]');
  assert.match(text(row), /set \(hidden\)/);
  assert.match(text(row), /default not set/);
  assert.match(text(row), /file only/);
  assert.match(text(row), /needs restart/);
  for (const source of ['default', 'file', 'environment']) assert.ok(panel(w).querySelectorAll('.badge').some(n => text(n).endsWith(' ' + source)));
  assert.match(text(panel(w)), /install.sh/);
});

for (const [key, expected] of [
  ['CCBOARD_MCP_REMOTE', 'off'],
  ['CCBOARD_BACKUP_PUSH', 'off'],
  ['CCBOARD_CODEX_HOOK_TRUST', 'review'],
  ['CCBOARD_TAILSCALE_PLACEMENT', 'empty'],
  ['CCBOARD_EPIC_ALIAS', 'empty'],
  ['CCBOARD_MEM_HTTPS_PORT', 'empty'],
  ['CCBOARD_MEM_PORT', 'automatic'],
]) {
  test(`empty ${key} displays ${expected}`, async () => {
    const fixture = structuredClone(FX);
    fixture.options.find(o => o.key === key).value = '';
    const w = world(fixture);
    await settle();
    const row = panel(w).querySelector(`[data-key=${key}]`);
    assert.equal(text(row.querySelector('.v')), expected);
    assert.doesNotMatch(text(panel(w)), /automatic where supported/);
  });
}

test('filter matches keys and labels and a group deep link opens only that group', async () => {
  const w = world(FX, '#/settings?sec=options&group=epics');
  await settle();
  const groups = panel(w).querySelectorAll('.set-options-group');
  assert.deepEqual(groups.filter(n => n.open).map(n => n.getAttribute('data-group')), ['epics']);
  const input = panel(w).querySelector('input');
  for (const term of ['CCBOARD_HUB_TOKEN', 'Legacy hub token']) {
    input.value = term;
    input.dispatchEvent({ type: 'input' });
    const shown = panel(w).querySelectorAll('.set-option').filter(n => !n.classList.contains('hidden'));
    assert.equal(shown.length, 1);
    assert.equal(shown[0].getAttribute('data-key'), 'CCBOARD_HUB_TOKEN');
    assert.equal(groups.find(n => n.getAttribute('data-group') === 'nodes').open, true);
  }
  input.value = '';
  input.dispatchEvent({ type: 'input' });
  assert.deepEqual(groups.filter(n => n.open).map(n => n.getAttribute('data-group')), ['epics']);
});

test('only opening Options fetches it, polls preserve the filter, and returning refreshes', async () => {
  const w = world(FX, '#/settings?sec=box');
  await settle();
  const requests = () => calls(w).filter(c => c.path === '/api/settings/options');
  assert.equal(requests().length, 0);
  w.location.hash = '#/settings?sec=options';
  await settle();
  const input = panel(w).querySelector('input');
  input.value = 'worker';
  w.run('updateCurrentPage(state)');
  assert.equal(panel(w).querySelector('input'), input);
  assert.equal(requests().length, 1);
  w.location.hash = '#/settings?sec=box';
  w.location.hash = '#/settings?sec=options';
  await settle();
  assert.equal(requests().length, 2);
  assert.ok(requests().every(c => c.method === 'GET'));
});

test('unavailable options show an error and Retry can recover', async () => {
  const w = world(() => { throw new Error('offline'); });
  await settle();
  assert.match(text(panel(w)), /Could not load options: offline/);
  w.ctx.__answers['/api/settings/options'] = FX;
  panel(w).querySelector('button').click();
  await settle();
  assert.equal(panel(w).querySelectorAll('.set-options-group').length, 9);
});

test('the real demo API loads the options fixture', async () => {
  const w = makeWorld();
  w.load('core.js');
  w.ctx.fetch = async url => {
    assert.equal(url, '/static/demo/options.json');
    return { ok: true, json: async () => FX };
  };
  const result = await w.run('demoApi("GET", "/api/settings/options")');
  assert.equal(result.groups.length, 9);
});

test('settings renders text without innerHTML or inline style assignments', () => {
  const source = fs.readFileSync(path.join(STATIC, 'pages/settings.js'), 'utf8');
  assert.doesNotMatch(source, /\.innerHTML\s*=|\.outerHTML\s*=|insertAdjacentHTML|setAttribute\(['"]style/);
});
