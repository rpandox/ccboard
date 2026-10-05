// Contract tests for v0.5.18 on the page side: Settings > Notifications (five per-kind switches kept on the box through /api/notify/prefs, and the
// example notice behind each, shown by a segmented control) and the session peek's Task line and mono permission summary. Real core.js, components.js,
// router.js and pages/*.js on minidom's DOM through tests/js/world.mjs; api() is the recorder from world.mjs.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { calls, fakeState, homeWorld, page, plain, sess, text, tick } from './world.mjs';

const SAMPLES = {
  needs: { title: '◆ shop/api · s1: needs you', body: 'Fix login redirect\n› fix the login redirect\n? Bash: npm test', priority: 4, kind: 'permission', buttons: ['Allow', 'Deny'] },
  done: { title: '◆ shop/api · s1: done', body: 'Fix login redirect\n› fix the login redirect\n? Fixed it.', priority: 3, kind: 'done', buttons: ['Terminal', 'Ack'] },
  limit: { title: 'Claude rate limited', body: '◆ shop/api · s1\n? 5-hour limit reached', priority: 5, kind: 'rate_limit', buttons: ['Terminal', 'Ack'] },
  error: { title: '◆ shop/api · s1: error', body: '? API Error: 500', priority: 4, kind: 'error', buttons: ['Terminal', 'Ack'] },
  login: { title: 'Claude login not valid: work', body: 'Please run /login', priority: 4, kind: 'login', buttons: [] },
};
const PREFS = (over = {}) => ({ needs: true, done: true, limit: true, error: true, login: true, ...over });
const KEYS = ['needs', 'done', 'limit', 'error', 'login'];

/** The world on #/settings (the Notifications panel is the default section). `answers` override what api() returns. */
function notifyWorld({ prefs = PREFS(), answers = {}, demo = false } = {}) {
  const { w } = homeWorld({ state: fakeState() });
  w.ctx.__answers['/api/notify/prefs'] = ({ method, body }) => (method === 'PUT' ? { prefs: { ...prefs, ...body }, samples: SAMPLES } : { prefs, samples: SAMPLES });
  for (const [k, v] of Object.entries(answers)) w.ctx.__answers[k] = v;
  w.run(`globalThis.__errors = []; setError = (m) => { if (m) __errors.push(m); }; demoOn = () => ${demo ? 'true' : 'false'};`);
  w.run('settingsNotifyState.at = 0; settingsNotifyState.samples = {}; settingsNotifyState.kind = "needs"; Object.assign(settingsNotifyState.prefs, { needs: true, done: true, limit: true, error: true, login: true });');
  w.location.hash = '#/settings';
  return w;
}
const panel = (w) => page(w).querySelector('.settings-panel[data-sec=notify]');
const boxes = (w) => panel(w).querySelectorAll('.set-prefs input');
const rowLabels = (w) => panel(w).querySelectorAll('.set-pref').map((r) => text(r.querySelector('b')));
const seg = (w, key) => panel(w).querySelector(`.set-kind-seg [data-kind=${key}]`);
const card = (w) => panel(w).querySelector('.set-notif');
const prefCalls = (w, method) => calls(w).filter((c) => c.path === '/api/notify/prefs' && (!method || c.method === method));
const errors = (w) => plain(w.get('__errors'));

test('the panel lists the five kinds, each a switch with a plain-word caption, all on until the box says otherwise', async () => {
  const w = notifyWorld();
  await tick();
  assert.deepEqual(rowLabels(w), ['Needs you', 'Done', 'Rate limit', 'Error or crash', 'Login problem']);
  assert.equal(boxes(w).length, 5);
  assert.ok(boxes(w).every((b) => b.checked));
  for (const r of panel(w).querySelectorAll('.set-pref')) assert.ok(text(r.querySelector('.dim')).length > 20, 'a caption in plain words');
  assert.equal(prefCalls(w, 'GET').length, 1, 'one fetch of the switches and their examples');
});

test('the switches the box reports are shown as they are', async () => {
  const w = notifyWorld({ prefs: PREFS({ done: false, login: false }) });
  await tick();
  assert.deepEqual(boxes(w).map((b) => b.checked), [true, false, true, true, false]);
});

test('switching one off sends only that key and keeps the answer', async () => {
  const w = notifyWorld();
  await tick();
  const done = boxes(w)[1];
  done.checked = false;
  done.dispatchEvent({ type: 'change' });
  await tick();
  assert.deepEqual(plain(prefCalls(w, 'PUT').map((c) => c.body)), [{ done: false }]);
  assert.deepEqual(boxes(w).map((b) => b.checked), [true, false, true, true, true]);
  assert.deepEqual(errors(w), []);
});

test('a switch the box refuses goes back and the reason is shown', async () => {
  const w = notifyWorld({ answers: {} });
  await tick();
  w.ctx.__answers['/api/notify/prefs'] = ({ method }) => { if (method === 'PUT') throw new Error('database is locked'); return { prefs: PREFS(), samples: SAMPLES }; };
  const limit = boxes(w)[2];
  limit.checked = false;
  limit.dispatchEvent({ type: 'change' });
  await tick();
  assert.equal(limit.checked, true, 'rolled back');
  assert.equal(w.get('settingsNotifyState').prefs.limit, true);
  assert.deepEqual(errors(w), ['database is locked']);
});

test('the example card shows the notice the box would send, the question line in mono, and the two buttons a phone shows', async () => {
  const w = notifyWorld();
  await tick();
  assert.equal(seg(w, 'needs').getAttribute('aria-pressed'), 'true');
  assert.equal(text(card(w).querySelector('.set-notif-title')), '◆ shop/api · s1: needs you');
  const lines = card(w).querySelectorAll('.set-notif-line');
  assert.deepEqual(lines.map(text), ['Fix login redirect', '› fix the login redirect', '? Bash: npm test']);
  assert.deepEqual(lines.map((l) => l.classList.contains('mono')), [false, false, true]);
  assert.deepEqual(card(w).querySelectorAll('.set-notif-btn').map(text), ['Allow', 'Deny']);
});

test('one segmented control picks the example (exclusive, aria-pressed), and a kind without buttons shows none', async () => {
  const w = notifyWorld();
  await tick();
  seg(w, 'login').click();
  assert.deepEqual(KEYS.map((k) => seg(w, k).getAttribute('aria-pressed')), ['false', 'false', 'false', 'false', 'true']);
  assert.equal(text(card(w).querySelector('.set-notif-title')), 'Claude login not valid: work');
  assert.equal(card(w).querySelectorAll('.set-notif-btn').length, 0);
  seg(w, 'limit').click();
  assert.equal(text(card(w).querySelector('.set-notif-title')), 'Claude rate limited');
  assert.equal(panel(w).querySelectorAll('.set-kind-seg [aria-pressed=true]').length, 1);
});

test('the arrow keys move along the control', async () => {
  const w = notifyWorld();
  await tick();
  seg(w, 'needs').dispatchEvent({ type: 'keydown', key: 'ArrowRight', preventDefault() {} });
  assert.equal(seg(w, 'done').getAttribute('aria-pressed'), 'true');
  seg(w, 'done').dispatchEvent({ type: 'keydown', key: 'ArrowLeft', preventDefault() {} });
  seg(w, 'needs').dispatchEvent({ type: 'keydown', key: 'ArrowLeft', preventDefault() {} });
  assert.equal(seg(w, 'login').getAttribute('aria-pressed'), 'true', 'wraps round');
});

test('the caption says when the previewed kind is switched off, and says what iOS does without buttons', async () => {
  const w = notifyWorld({ prefs: PREFS({ needs: false }) });
  await tick();
  const cap = text(panel(w).querySelector('.set-notif-cap'));
  assert.match(cap, /switched off/);
  assert.match(cap, /iPhone/);
  seg(w, 'login').click();
  assert.doesNotMatch(text(panel(w).querySelector('.set-notif-cap')), /switched off/);
});

test('the one filled primary of the panel is still the Enable push button: switches and the segmented control add none', async () => {
  const w = notifyWorld();
  await tick();
  const filledBtns = panel(w).querySelectorAll('button').filter((b) => b.classList.contains('primary') || b.classList.contains('bp5-intent-primary'));
  assert.ok(filledBtns.length <= 1, `${filledBtns.length} filled buttons`);
  for (const k of KEYS) assert.ok(!seg(w, k).classList.contains('primary'));
});

test('demo mode shows the built-in examples and never calls the board', async () => {
  const w = notifyWorld({ demo: true });
  await tick();
  assert.equal(prefCalls(w).length, 0);
  assert.equal(text(card(w).querySelector('.set-notif-title')), '◆ shop/api · s1: needs you');
  const done = boxes(w)[1];
  done.checked = false;
  done.dispatchEvent({ type: 'change' });
  await tick();
  assert.equal(prefCalls(w).length, 0);
  assert.equal(w.get('settingsNotifyState').prefs.done, false, 'kept locally');
});

test('a rebuild of the panel paints the fetched examples at once, before any new answer', async () => {
  const w = notifyWorld();
  await tick();
  seg(w, 'error').click();
  w.run('settingsNotify(document.querySelector("#page .settings-panel[data-sec=notify]"))');
  assert.equal(text(card(w).querySelector('.set-notif-title')), '◆ shop/api · s1: error', 'the chosen example survives the minute rebuild');
  assert.equal(prefCalls(w, 'GET').length, 1, 'inside 20 s the examples are not fetched again');
});

// ---------------------------------------------------------------- the peek

function peekFor(w, s, opts = {}) {
  w.ctx.__s = { ...s, project: 'shop', repo: 'api', ...opts };
  w.run('globalThis.__peek = sessionCard(__s, { peek: true, perm: true, showProject: true, link: false }); __peek.ccPatch(__s)');
  return w.get('__peek');
}
const blocks = (card) => card.querySelectorAll('.peek-block');
const shown = (n) => !!n && !n.classList.contains('hidden');

test('the peek shows the task title in a Task block above the last prompt, and hides it for a session without a task', () => {
  const { w } = homeWorld();
  const s = sess('shop', 'api', 's1', { last_prompt: 'fix the login redirect' });
  const withTask = peekFor(w, s, { task: { id: 3, title: 'Fix login redirect', phase: 'running' } });
  const labels = blocks(withTask).map((b) => text(b.querySelector('.k')));
  assert.deepEqual(labels.slice(0, 3), ['Task', 'Last prompt', 'Last message']);
  assert.equal(text(withTask.querySelector('.peek-task')), 'Fix login redirect');
  assert.ok(shown(blocks(withTask)[0]));
  const bare = peekFor(w, s, { task: null });
  assert.equal(shown(blocks(bare)[0]), false);
});

test('the peek shows what is asked as the notification does: the permission summary in mono', () => {
  const { w } = homeWorld({ state: fakeState({ pending_permissions: [{ id: 12, tmux_name: 'shop--api--s1', tool_name: 'Bash', summary: 'Bash: npm test', created_at: new Date().toISOString() }] }) });
  const s = sess('shop', 'api', 's1', { state: 'waiting', needs_attention: true });
  const card = peekFor(w, s);
  const note = card.querySelector('.perm-note');
  assert.equal(text(note), 'Bash: npm test');
  assert.ok(note.classList.contains('mono'));
  assert.deepEqual(card.querySelectorAll('.peek-perm button').map(text), ['Allow', 'Deny']);
});

test('the row of the roster keeps its permission note as it was (no mono class outside the peek)', () => {
  const { w } = homeWorld();
  const s = sess('shop', 'api', 's1');
  w.ctx.__s = { ...s, project: 'shop', repo: 'api' };
  w.run('globalThis.__row = sessionCard(__s, { perm: true })');
  assert.equal(w.get('__row').querySelector('.perm-note').classList.contains('mono'), false);
});
