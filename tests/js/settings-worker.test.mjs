import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC } from './harness.mjs';
import { calls, fakeState, homeWorld, page, tick, text } from './world.mjs';

const FX = JSON.parse(fs.readFileSync(path.join(STATIC, 'demo/options.json'), 'utf8'));
const SAFETY = "If on, an issue's text starts an agent on that machine with nobody pressing Run. Only for repos and authors you list.";
const settle = async () => { for (let i = 0; i < 6; i++) await tick(); };
const button = (node, label) => node.querySelectorAll('button').find(n => text(n) === label);
const form = w => page(w).querySelector('.set-worker');
const field = (w, key) => form(w).querySelector('[data-key=CCBOARD_EPIC_' + key + ']');
const input = (w, key) => field(w, key).querySelector('input, textarea');
async function world(on = true) {
  const { w } = homeWorld({ state: fakeState() });
  w.load('nodes-worker.js');
  const fx = structuredClone(FX); fx.page_widen = on;
  w.ctx.__answers['/api/settings/options'] = fx;
  w.location.hash = '#/settings?sec=options&group=epics';
  await settle();
  return { w, fx };
}
const worker = { repos: ['owner/name'], authors: ['owner:1'], writes: true, poll: 90, hour: 8, day: 30 };
const confirmation = { reason: 'needs_confirm', phrase: 'ALLOW WORKER', safety: SAFETY, worker,
  widens: [{ key: 'CCBOARD_EPIC_WORKER_MAX_PER_HOUR', plain: 'The worker may start more issues each hour.' }] };

test('all fields render once, safety is above the switch, master off is read only', async () => {
  for (const on of [false, true]) {
    const { w } = await world(on);
    const root = form(w);
    assert.ok(root);
    assert.equal(text(root.querySelector('.set-worker-safety')), SAFETY);
    assert.ok(text(root).indexOf(SAFETY) < text(root).indexOf('Worker mode') && text(root).includes('acceptEdits'));
    for (const suffix of ['WORKER', 'REPOS', 'AUTHORS', 'WRITES', 'WORKER_POLL', 'WORKER_MAX_PER_HOUR', 'WORKER_MAX_PER_DAY']) {
      assert.equal(input(w, suffix).disabled, !on);
      assert.equal(page(w).querySelectorAll('[data-key=CCBOARD_EPIC_' + suffix + ']').length, 1);
      if (!on) assert.ok(field(w, suffix).querySelector('.set-option-line'));
    }
    assert.equal(button(root, 'Save').disabled, !on);
    assert.match(text(root), /An id pins the account/);
    assert.match(text(root), /30 to 3600 seconds; default 90/);
    assert.equal(page(w).querySelectorAll('.primary').filter(n => !n.classList.contains('tinted')).length, 1);
  }
});

test('shared dialog repeats exact safety, trust lists and limits and gates the phrase', async () => {
  const { w, fx } = await world();
  w.ctx.__answers['/api/settings/options'] = ({ body }) => {
    if (body.confirm === 'ALLOW WORKER') return fx;
    const e = new Error('confirm'); e.status = 409; e.body = confirmation; throw e;
  };
  input(w, 'WORKER_MAX_PER_HOUR').value = '8';
  button(form(w), 'Save').click(); await settle();
  const dlg = w.document.querySelector('dialog.set-widen');
  assert.ok(dlg?.open);
  assert.ok(button(form(w), 'Save').classList.contains('tinted'));
  assert.equal(text(dlg.querySelector('.set-worker-safety')), SAFETY);
  assert.match(text(dlg), /owner\/name/); assert.match(text(dlg), /owner:1/);
  assert.match(text(dlg), /90 seconds.*8 per hour and 30 per day/);
  const typed = dlg.querySelector('input'), confirm = button(dlg, 'Confirm save');
  assert.equal(confirm.disabled, true);
  for (const value of ['allow worker', 'ALLOW WORKER ', 'ALLOW WORKER']) {
    typed.value = value; typed.dispatchEvent({ type: 'input' });
    assert.equal(confirm.disabled, value !== 'ALLOW WORKER');
  }
  confirm.click(); await settle();
  const writes = calls(w).filter(c => c.method === 'PUT');
  assert.equal(writes.length, 2);
  assert.deepEqual(writes[1].body, { ...writes[0].body, confirm: 'ALLOW WORKER' });
});

test('a missing prerequisite shows the board sentence and keeps all typed fields', async () => {
  const { w } = await world();
  const sentence = 'After the restart the worker will refuse to start: no allowed author known';
  w.ctx.__answers['/api/settings/options'] = () => { const e = new Error('refused'); e.status = 400; e.body = { reason: sentence }; throw e; };
  input(w, 'WORKER').checked = true;
  input(w, 'REPOS').value = 'owner/one\nowner/two';
  input(w, 'AUTHORS').value = '';
  button(form(w), 'Save').click(); await settle();
  assert.equal(text(form(w).querySelector('.set-worker-error')), sentence);
  assert.equal(input(w, 'WORKER').checked, true);
  assert.equal(input(w, 'REPOS').value, 'owner/one\nowner/two');
  assert.equal(calls(w).filter(c => c.method === 'PUT')[0].body.changes.CCBOARD_EPIC_REPOS, 'owner/one,owner/two');
});

for (const [key, value] of [['WORKER_POLL', '29'], ['WORKER_MAX_PER_HOUR', '61'], ['WORKER_MAX_PER_DAY', '0'], ['REPOS', 'invalid'], ['AUTHORS', 'two words']]) test(`${key} validates before Save`, async () => {
  const { w } = await world();
  const control = input(w, key);
  control.value = value; control.dispatchEvent({ type: 'input' });
  assert.equal(control.getAttribute('aria-invalid'), 'true');
  button(form(w), 'Save').click(); await settle();
  assert.equal(calls(w).filter(c => c.method === 'PUT').length, 0);
});

test('pending off keeps live Stop available, and polls preserve unsaved inputs', async () => {
  const { w, fx } = await world();
  fx.worker_live = { on: true, state: 'idle', running: true };
  const option = fx.options.find(o => o.key === 'CCBOARD_EPIC_WORKER');
  Object.assign(option, { edit_value: '0', pending: true, running: true, value: '0' });
  w.ctx.__fx = fx; w.ctx.__panel = page(w).querySelector('[data-sec=options]');
  w.run('settingsOptionsPaint(__panel, __fx)');
  const stop = button(form(w), 'Stop worker');
  assert.ok(stop && !stop.classList.contains('hidden'));
  assert.match(text(field(w, 'WORKER')), /saved, needs restart/);
  const control = input(w, 'REPOS'); control.value = 'owner/typed';
  w.run('updateCurrentPage(state)');
  assert.equal(input(w, 'REPOS'), control); assert.equal(control.value, 'owner/typed');
});

test('the Options form has no setup link to itself, and its file lines follow page_widen', async () => {
  const { w } = await world();
  const off = form(w).querySelector('.ew-off');
  assert.ok(off.querySelector('.ew-file').classList.contains('hidden'));
  assert.equal(form(w).querySelector('.ew-setup'), null);
  assert.doesNotMatch(text(form(w)), /Nothing on this page turns it on/);
  const closed = await world(false);
  assert.ok(!form(closed.w).querySelector('.ew-off .ew-file').classList.contains('hidden'));
  assert.equal(form(closed.w).querySelector('.ew-setup'), null);
});

test('worker panel file lines show only when page_widen is known to be false', async () => {
  const { w } = await world();
  w.run('globalThis.__panel = EpicWorker.build({ mode: "settings" })');
  const lines = () => w.ctx.__panel.root.querySelector('.ew-file').classList.contains('hidden');
  for (const [pageWiden, ctx, hidden] of [[undefined, {}, true], [undefined, { page_widen: undefined }, true], [true, {}, true], [false, {}, false],
                                          [undefined, { page_widen: false }, false], [false, { page_widen: true }, true], [true, { page_widen: false }, false]]) {
    w.ctx.__pw = pageWiden; w.ctx.__c = ctx;
    w.run('EpicWorker.pageWiden = __pw; __panel.paint(null, __c)');
    assert.equal(lines(), hidden, JSON.stringify([pageWiden, ctx]));
  }
});

test('every option key can wrap after an underscore and never inside a word', async () => {
  const { w, fx } = await world();
  const keys = page(w).querySelectorAll('.set-option-key');
  assert.ok(keys.length > 10);
  const pass = new Set(keys.map(k => text(k)));
  for (const k of keys) {
    const key = text(k);
    assert.equal(k.querySelectorAll('wbr').length, key.split('_').length - 1, key);
    const nodes = k.childNodes.map(n => n.nodeType === 3 ? n.textContent : n.tagName);
    assert.equal(nodes.join(''), key.split('_').slice(0, -1).map(x => x + '_WBR').join('') + key.split('_').pop(), key);
    for (let i = 0; i < nodes.length; i++) if (nodes[i] === 'WBR') assert.ok(String(nodes[i - 1]).endsWith('_'), key);
  }
  assert.ok(pass.has('CCBOARD_EPIC_WORKER') && fx.options.length >= pass.size);
});

for (const flag of ['off', 'widenoff', 'ready', 'refusing']) test(`demo form state ${flag}`, async () => {
  const { w, fx } = await world();
  w.location.search = '?demo=1&workersettings=' + flag;
  w.ctx.__fx = fx;
  const result = w.run('settingsDemoWorkerState(__fx)');
  assert.equal(result.page_widen, flag !== 'widenoff');
  assert.equal(result.worker.on, ['ready', 'refusing'].includes(flag));
  assert.equal(result.worker.reason, flag === 'refusing' ? 'repos_empty' : null);
  for (const [key, value] of Object.entries(fx.worker_states[flag].values)) assert.equal(result.options.find(o => o.key === key).line, key + '=' + value);
});


test('draft prerequisite warning appears before Save, and a cached login counts', async () => {
  const { w, fx } = await world();
  input(w, 'WORKER').checked = true;
  input(w, 'WRITES').checked = true;
  input(w, 'REPOS').value = 'owner/name';
  input(w, 'AUTHORS').value = '';
  input(w, 'WORKER').dispatchEvent({ type: 'input' });
  assert.equal(text(form(w).querySelector('.set-worker-error')), 'After the restart the worker will refuse to start: no allowed author known');
  assert.equal(calls(w).filter(c => c.method === 'PUT').length, 0);
  fx.worker.known_author = 'owner';
  input(w, 'WORKER').dispatchEvent({ type: 'input' });
  assert.equal(text(form(w).querySelector('.set-worker-error')), '');
});
