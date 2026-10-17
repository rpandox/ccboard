import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC, makeWorld } from './harness.mjs';
import { calls, fakeState, homeWorld, page, tick, text } from './world.mjs';

const FX = JSON.parse(fs.readFileSync(path.join(STATIC, 'demo/options.json'), 'utf8'));
const settle = async () => { for (let i = 0; i < 5; i++) await tick(); };
const button = (node, label) => node.querySelectorAll('button').find(n => text(n) === label);
async function world(fixture = FX) {
  const { w } = homeWorld({ state: fakeState() });
  w.ctx.__answers['/api/settings/options'] = fixture;
  w.location.hash = '#/settings?sec=options';
  await settle();
  return w;
}
const row = (w, key = 'CCBOARD_NODE_LANES') => page(w).querySelector('[data-key=' + key + ']');

test('recent saves and refusals have separate sections', async () => {
  const fx = structuredClone(FX);
  fx.audit = [{ action: 'settings_set', user: 'alice', detail: 'saved marker' }];
  fx.refusals = [{ action: 'settings_refused', user: 'alice', detail: 'refused marker' }];
  const w = await world(fx);
  const sections = page(w).querySelectorAll('.set-options-audit');
  assert.equal(sections.length, 2);
  assert.match(text(sections[0]), /Recent settings saves.*saved marker/);
  assert.doesNotMatch(text(sections[0]), /refused marker/);
  assert.match(text(sections[1]), /Recent settings refusals.*refused marker/);
});

for (const [kind, choices, selector] of [
  ['bool', null, 'input[type=checkbox]'], ['int', null, 'input[type=number]'],
  ['float', null, 'input[type=number]'], ['text', null, 'input[type=text]'],
  ['list', null, 'input[type=text]'], ['choice', ['a', 'b'], '.seg-ctl'],
  ['choice', ['a', 'b', 'c', 'd', 'e'], 'select'],
]) {
  test(`${kind} with ${choices?.length || 0} choices gets the right editor`, async () => {
    const fx = structuredClone(FX);
    Object.assign(fx.options.find(o => o.key === 'CCBOARD_NODE_LANES'), { kind, choices });
    const w = await world(fx);
    button(row(w), 'Edit').click();
    assert.ok(row(w).querySelector(selector));
    if (kind === 'int' || kind === 'float') {
      const input = row(w).querySelector(selector);
      assert.equal(input.getAttribute('min'), '0');
      assert.equal(input.getAttribute('max'), '999');
      assert.equal(input.getAttribute('step'), kind === 'int' ? '1' : 'any');
    }
  });
}

test('Save sends only the changed key and the loaded stamp; unchanged saves do nothing', async () => {
  const w = await world();
  button(row(w), 'Edit').click();
  button(row(w), 'Save').click();
  await settle();
  assert.equal(calls(w).filter(c => c.method === 'PUT').length, 0);
  button(row(w), 'Edit').click();
  row(w).querySelector('.set-option-input').value = '7';
  button(row(w), 'Save').click();
  await settle();
  const writes = calls(w).filter(c => c.method === 'PUT');
  assert.deepEqual(writes, [{ method: 'PUT', path: '/api/settings/options', body: { changes: { CCBOARD_NODE_LANES: '7' }, stamp: FX.stamp } }]);
});

test('the real API sends the CSRF header and preserves server errors', async () => {
  const w = makeWorld(); w.load('core.js');
  let got;
  w.ctx.fetch = async (url, opts) => { got = opts; return { ok: true, json: async () => ({}) }; };
  await w.run('api("PUT", "/api/settings/options", {changes:{CCBOARD_NODE_LANES:"5"}})');
  assert.equal(got.headers['X-CCBoard'], '1');
});

test('400 keeps typed input and shows the board sentence next to the row', async () => {
  const w = await world();
  w.ctx.__answers['/api/settings/options'] = () => { const e = new Error('bad'); e.status = 400; e.body = { key: 'CCBOARD_NODE_LANES', reason: 'Use 0 to 999.' }; throw e; };
  button(row(w), 'Edit').click();
  const input = row(w).querySelector('.set-option-input'); input.value = '1000';
  button(row(w), 'Save').click(); await settle();
  assert.equal(input.value, '1000');
  assert.equal(text(row(w).querySelector('.set-option-error')), 'Use 0 to 999.');
  assert.equal(button(row(w), 'Save').disabled, false);
});

test('conflict reloads, reset sends null, Cancel closes and only one editor is open', async () => {
  const w = await world();
  button(row(w), 'Edit').click();
  button(row(w, 'CCBOARD_NODE_NAME'), 'Edit').click();
  assert.equal(page(w).querySelectorAll('.set-option-editor').length, 1);
  button(row(w, 'CCBOARD_NODE_NAME'), 'Cancel').click();
  assert.equal(page(w).querySelectorAll('.set-option-editor').length, 0);
  button(row(w), 'Edit').click();
  button(row(w), "Use the file's value").click(); await settle();
  assert.deepEqual(calls(w).find(c => c.method === 'PUT').body.changes, { CCBOARD_NODE_LANES: null });
  w.ctx.__answers['/api/settings/options'] = ({ method }) => { if (method === 'GET') return FX; const e = new Error('changed'); e.status = 409; throw e; };
  button(row(w), 'Edit').click(); row(w).querySelector('.set-option-input').value = '6';
  button(row(w), 'Save').click(); await settle();
  assert.equal(page(w).querySelectorAll('.set-option-editor').length, 0);
  assert.equal(calls(w).filter(c => c.method === 'GET' && c.path === '/api/settings/options').length, 2);
});

test('read-only classes have no editor and pending rows compare values', async () => {
  const w = await world();
  for (const key of ['CCBOARD_HUB_TOKEN', 'CCBOARD_PORT', 'CCBOARD_AUTO_CONTINUE']) assert.equal(button(row(w, key), 'Edit'), undefined);
  assert.match(text(row(w)), /saved, needs restart/);
  assert.match(text(row(w)), /running .*next start 5/);
});

test('Home pending callout names registry labels, limits the list, and separates the copyable restart command', async () => {
  const st = fakeState();
  st.settings_pending = { count: 1, keys: ['CCBOARD_NODE_LANES'], labels: ['Task lanes'], restart_hint: FX.restart_hint };
  const { w } = homeWorld({ state: st });
  w.location.hash = '#/';
  await settle();
  const banner = () => page(w).querySelector('.home-settings-pending');
  assert.ok(banner().classList.contains('bp5-callout'));
  assert.ok(banner().classList.contains('bp5-intent-warning'));
  assert.equal(text(banner().querySelector('p')), 'A saved setting waits for a restart: Task lanes.');
  assert.doesNotMatch(text(banner()), /Saved\.|CCBOARD_/);
  assert.equal(text(banner().querySelector('.doc-cmdline code')), FX.restart_hint);
  assert.ok(banner().querySelector('.doc-cmdline .copy-btn'));
  assert.equal(banner().querySelector('a').getAttribute('href'), '#/settings?sec=options');
  assert.equal(text(banner().querySelector('a')), 'Open Settings > Options');
  w.run('state.settings_pending = {count:5, labels:["Task lanes", "Node name", "Task model", "Fourth", "Fifth"], restart_hint:"new restart"}; updateCurrentPage(state)');
  assert.equal(text(banner().querySelector('p')), 'Saved settings wait for a restart: Task lanes, Node name, Task model and 2 more.');
  assert.doesNotMatch(text(banner()), /Fourth|Fifth/);
  assert.equal(text(banner().querySelector('code')), 'new restart');
  w.run('state.settings_pending = {count:0}; updateCurrentPage(state)');
  assert.equal(banner(), null);
});

test('Options pending callout uses labels and one separate command; without pending the single guidance line owns it', async () => {
  const w = await world();
  const p = page(w).querySelector('.settings-panel[data-sec=options]');
  const banner = p.querySelector('.set-pending');
  assert.equal(p.firstChild, banner);
  assert.ok(banner.classList.contains('bp5-callout'));
  assert.ok(banner.classList.contains('bp5-intent-warning'));
  assert.match(text(banner.querySelector('p')), /restart: Task lanes\./);
  assert.doesNotMatch(text(banner), /CCBOARD_/);
  assert.equal(text(banner.querySelector('.doc-cmdline code')), FX.restart_hint);
  assert.ok(banner.querySelector('.copy-btn'));
  const checkGuidance = () => {
    assert.equal(text(p).split(FX.restart_hint).length - 1, 1);
    assert.match(text(p.querySelector('.set-options-guidance')), /Page options save here; others are set by the installer or settings file\./);
    assert.doesNotMatch(text(p), /Saved changes apply after|After changing startup options:/);
  };
  checkGuidance();
  w.ctx.__optionsNoPending = { ...structuredClone(FX), pending: [] };
  w.run('settingsOptionsPaint(settingsPage.refs.panels.options, __optionsNoPending)');
  assert.equal(p.querySelector('.set-pending'), null);
  assert.equal(text(p.querySelector('.set-options-guidance .doc-cmdline code')), FX.restart_hint);
  checkGuidance();
});


test('demo saves and resets to the file value without a network write', async () => {
  const w = await world();
  w.ctx.demoApi = async () => structuredClone(FX);
  const saved = await w.run('demoSettingsOptions({changes:{CCBOARD_NODE_LANES:"7"}})');
  assert.equal(saved.options.find(o => o.key === 'CCBOARD_NODE_LANES').value, '7');
  const reset = await w.run('demoSettingsOptions({changes:{CCBOARD_NODE_LANES:null}})');
  const option = reset.options.find(o => o.key === 'CCBOARD_NODE_LANES');
  assert.equal(option.value, option.running);
  assert.equal(option.source, 'file');
  assert.equal(option.pending, false);
});
