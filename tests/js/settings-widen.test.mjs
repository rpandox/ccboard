import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC } from './harness.mjs';
import { calls, fakeState, homeWorld, page, tick, text } from './world.mjs';

const FX = JSON.parse(fs.readFileSync(path.join(STATIC, 'demo/options.json'), 'utf8'));
const settle = async () => { for (let i = 0; i < 5; i++) await tick(); };
const button = (node, label) => node.querySelectorAll('button').find(n => text(n) === label);
const key = 'CCBOARD_EPIC_WORKER_MAX_PER_HOUR';
const row = w => page(w).querySelector('[data-key=' + key + ']');
async function world(on = true) {
  const { w } = homeWorld({ state: fakeState() });
  const fx = structuredClone(FX); fx.page_widen = on;
  w.ctx.__answers['/api/settings/options'] = fx;
  w.location.hash = '#/settings?sec=options';
  await settle();
  return { w, fx };
}
const answer = { reason: 'needs_confirm', phrase: 'ALLOW WORKER', widens: [
  { key, plain: 'The worker may start more issues each hour.' },
  { key: 'CCBOARD_EPIC_REPOS', plain: 'The worker may take issues from additional repositories.' },
] };

for (const on of [false, true]) test(`master switch ${on}: chip and confirm editing follow trusted state`, async () => {
  const { w } = await world(on);
  assert.equal(!!page(w).querySelector('.set-widen-chip'), on);
  assert.equal(!!button(row(w), 'Edit'), on);
  if (on) assert.match(text(page(w)), /Settings can widen agent permissions/);
  else assert.match(text(row(w)), /Allow in settings: CCBOARD_PAGE_WIDEN=1/);
  for (const k of ['CCBOARD_CODEX_HOOK_TRUST', 'CCBOARD_NODE_TAGS', 'CCBOARD_PAGE_WIDEN']) {
    const r = page(w).querySelector('[data-key=' + k + ']');
    assert.equal(button(r, 'Edit'), undefined);
    assert.match(text(r.querySelector('.set-option-line')), new RegExp(k + '='));
  }
});

test('dialog lists every widening sentence and accepts only the exact phrase', async () => {
  const { w, fx } = await world();
  w.ctx.__answers['/api/settings/options'] = ({ body }) => {
    if (body.confirm === answer.phrase) return fx;
    const e = new Error('confirm'); e.status = 409; e.body = answer; throw e;
  };
  button(row(w), 'Edit').click(); row(w).querySelector('input').value = '7';
  button(row(w), 'Save').click(); await settle();
  const dlg = w.document.querySelector('dialog.set-widen');
  assert.ok(dlg && dlg.open);
  assert.deepEqual(dlg.querySelectorAll('li').map(text), answer.widens.map(r => r.plain));
  const input = dlg.querySelector('input'), confirm = button(dlg, 'Confirm save');
  assert.equal(confirm.disabled, true);
  for (const value of ['allow worker', 'ALLOW WORKER ', ' ALLOW WORKER', 'ALLOW  WORKER', 'ALLOW WORKER']) {
    input.value = value; input.dispatchEvent({ type: 'input' });
    assert.equal(confirm.disabled, value !== answer.phrase);
  }
  confirm.click(); await settle();
  assert.equal(w.document.querySelector('dialog.set-widen'), null);
  const writes = calls(w).filter(c => c.method === 'PUT');
  assert.equal(writes.length, 2);
  assert.deepEqual(writes[1].body, { ...writes[0].body, confirm: 'ALLOW WORKER' });
});

test('narrowing saves normally, and cancelling confirmation never repeats the PUT', async () => {
  const { w, fx } = await world();
  button(row(w), 'Edit').click(); row(w).querySelector('input').value = '5';
  button(row(w), 'Save').click(); await settle();
  assert.equal(w.document.querySelector('dialog.set-widen'), null);
  assert.equal(calls(w).filter(c => c.method === 'PUT').length, 1);
  w.ctx.__answers['/api/settings/options'] = () => { const e = new Error('confirm'); e.status = 409; e.body = answer; throw e; };
  button(row(w), 'Edit').click(); row(w).querySelector('input').value = '8';
  button(row(w), 'Save').click(); await settle();
  button(w.document.querySelector('dialog.set-widen'), 'Cancel').click();
  assert.equal(calls(w).filter(c => c.method === 'PUT').length, 2);
  assert.equal(button(row(w), 'Save').disabled, false);
  assert.equal(row(w).querySelector('input').value, '8');
});

test('demo enforces opt-in, typed phrases and narrowing without changing file rows', async () => {
  const { w, fx } = await world();
  w.ctx.__fixture = fx;
  w.run('settingsDemoOptions = __fixture');
  await assert.rejects(w.run(`demoSettingsOptions({changes:{${key}:'7'}})`), e => e.status === 409 && e.body.phrase === 'ALLOW WORKER');
  await assert.rejects(w.run(`demoSettingsOptions({changes:{${key}:'7'},confirm:'wrong'})`), e => e.status === 403);
  await w.run(`demoSettingsOptions({changes:{${key}:'7'},confirm:'ALLOW WORKER'})`);
  await w.run(`demoSettingsOptions({changes:{${key}:'5'}})`);
  await assert.rejects(w.run("demoSettingsOptions({changes:{CCBOARD_PAGE_WIDEN:'1'}})"), e => e.status === 403);
  w.run('settingsDemoOptions.page_widen = false');
  await assert.rejects(w.run(`demoSettingsOptions({changes:{${key}:'7'},confirm:'ALLOW WORKER'})`), e => e.body.reason === 'widen_off');
});

for (const [reason, message] of [
  ['bad_phrase', 'The words you typed do not match; type them exactly as shown.'],
  ['file_only', 'This setting can only be changed in the settings file: '],
]) test(`editor explains ${reason} in plain words`, async () => {
  const { w, fx } = await world();
  w.ctx.__answers['/api/settings/options'] = () => {
    const e = new Error(reason); e.status = 403; e.body = { reason }; throw e;
  };
  button(row(w), 'Edit').click(); row(w).querySelector('input').value = '7';
  button(row(w), 'Save').click(); await settle();
  assert.equal(text(row(w).querySelector('.set-option-error')),
    message + (reason === 'file_only' ? fx.options.find(o => o.key === key).line : ''));
});

test('Options identifies a live widening even with the switch off for next start', async () => {
  const { w, fx } = await world(false);
  fx.options.find(o => o.key === key).live_widening = true;
  w.ctx.__fixture = fx;
  w.ctx.__panel = page(w);
  w.run('settingsOptionsPaint(__panel, __fixture)');
  assert.match(text(row(w)), /Live value is wider than the settings file\/environment alone/);
});
