// Settings > Box > claude-mem (v0.5.20, issue #10): the write-back switch, off by default, kept on the box through GET / PUT /api/memory/prefs.
// Real core.js, components.js, router.js and pages/*.js on minidom's DOM through tests/js/world.mjs; api() is the recorder from world.mjs.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { calls, fakeState, homeWorld, page, plain, text, tick } from './world.mjs';

const SENDS = { title: 'Task: <title>', text: "the task's result, at most 8 KB, cut at a line break and marked", project: "the repo's claude-mem key",
  metadata: ['source', 'task_id', 'agent'], never: ['the prompt'] };

function memWorld({ writeback = false, available = true, reason = null, put = null } = {}) {
  const { w } = homeWorld({ state: fakeState() });
  w.ctx.__answers['/api/memory/prefs'] = ({ method, body }) => {
    if (method === 'PUT') { if (put) return put(body); return { prefs: { writeback: body.writeback }, available, reason, writeback_sends: SENDS }; }
    return { prefs: { writeback }, available, reason, writeback_sends: SENDS };
  };
  w.run('globalThis.__errors = []; setError = (m) => { if (m) __errors.push(m); }; demoOn = () => false;');
  w.run('settingsMemState.at = 0; settingsMemState.err = null; settingsMemState.available = null; settingsMemState.prefs.writeback = false;');
  w.location.hash = '#/settings?sec=box';
  return w;
}
const block = (w) => page(w).querySelector('.settings-panel[data-sec=box] .set-mem');
const box = (w) => block(w).querySelector('input');
const memCalls = (w, method) => calls(w).filter((c) => c.path === '/api/memory/prefs' && (!method || c.method === method));

test('the switch is off by default, with its sentence under it and what is sent behind a disclosure', async () => {
  const w = memWorld();
  await tick();
  assert.ok(block(w), 'the claude-mem block sits in Settings > Box');
  assert.equal(box(w).checked, false);
  assert.equal(box(w).disabled, false);
  assert.match(text(block(w)), /Save each finished task's result to claude-mem\. Results can contain anything the agent printed\./);
  const what = text(block(w).querySelector('details'));
  assert.match(what, /Never the prompt/);
  assert.match(what, /Memory timeline/);
  assert.equal(memCalls(w, 'GET').length, 1);
  assert.equal(block(w).querySelectorAll('.primary').length, 0, 'not a filled primary');
});

test('turning it on sends writeback true and keeps the answer', async () => {
  const w = memWorld();
  await tick();
  box(w).checked = true;
  box(w).dispatchEvent({ type: 'change' });
  await tick();
  assert.deepEqual(plain(memCalls(w, 'PUT').map((c) => c.body)), [{ writeback: true }]);
  assert.equal(box(w).checked, true);
});

test('a failed PUT puts the switch back and shows the error beside it', async () => {
  const w = memWorld({ put: () => { throw new Error('database is locked'); } });
  await tick();
  box(w).checked = true;
  box(w).dispatchEvent({ type: 'change' });
  await tick();
  assert.equal(box(w).checked, false);
  assert.match(text(block(w).querySelector('.bad')), /Not saved: database is locked/);
});

test('without the plugin the switch is disabled with the box\'s reason', async () => {
  const w = memWorld({ available: false, reason: 'the claude-mem plugin is not installed' });
  await tick();
  assert.equal(box(w).disabled, true);
  assert.match(text(block(w)), /Not available: the claude-mem plugin is not installed\./);
});


test('the claude-mem health tile sits above the switch when the board has claude-mem, and is absent without it (v0.5.20, issue #8)', async () => {
  const { w } = homeWorld({ state: { ...fakeState(), memory: { state: 'down', reason: 'connection refused: the claude-mem worker is not running', observations: null, queue_depth: null } } });
  w.ctx.__answers['/api/memory/prefs'] = { prefs: { writeback: false }, available: true, reason: null, writeback_sends: SENDS };
  w.load('pages/memory.js');
  w.run('demoOn = () => false; settingsMemState.at = 0;');
  w.location.hash = '#/settings?sec=box';
  await tick();
  const tile = block(w).querySelector('.mem-tile');
  assert.ok(tile, 'the tile is in the claude-mem block');
  assert.match(text(tile), /down · connection refused: the claude-mem worker is not running/);
  assert.match(text(tile), /Observationsunknown/);
  assert.equal(block(w).children[0], tile, 'above the switch');
  const none = homeWorld({ state: fakeState() });
  none.w.load('pages/memory.js');
  none.w.run('demoOn = () => false; settingsMemState.at = 0;');
  none.w.location.hash = '#/settings?sec=box';
  await tick();
  assert.equal(block(none.w).querySelector('.mem-tile'), null, 'no state.memory: no tile');
});
