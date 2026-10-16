// Contract tests for the New epic sheet of issue #158 (app/static/pages/epic-new.js, its rules in pages/epic.css, the entry points in the Tasks Epics tab, the project page and the palette, and the demo's
// three answers). Real scripts on minidom's DOM through tests/js/world.mjs; api() is the recorder, and its answers are the board's shapes (GET /api/epics/new, POST /api/epics/preview, POST /api/epics)
// built from app/static/demo/epic.json (`new`), which tests/test_epic_new_demo.py holds to the real board.
// (The issue names tests/js/epic.test.mjs; that file already holds the epic view's tests of #147, so these are a file of their own.)
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { afterEach, test } from 'node:test';
import { STATIC } from './harness.mjs';
import { calls, fakeState, homeWorld, plain, text, tick } from './world.mjs';

const FX = JSON.parse(fs.readFileSync(path.join(STATIC, 'demo', 'epic.json'), 'utf8'));
const NEW = FX.new;
const clone = (v) => JSON.parse(JSON.stringify(v));
const LAZY_STUB = "globalThis.Lazy = { run: (n, fn) => fn(), wants: () => false, load: () => Promise.resolve(), pending: () => [], later() {}, warm() {} };";

const made = [];
afterEach(() => { for (const w of made.splice(0)) { try { w.run('if (epicNew.S) { epnStopTimer(epicNew.S); epicNew.S = null; } epUnmount(); Epics.stopList()'); } catch (_) { /* never loaded */ } } });
const later = (ms) => new Promise((r) => setTimeout(r, ms));
const settle = async (n = 5) => { for (let i = 0; i < n; i++) await tick(); };

const OPTIONS = { repos: [{ repo: 'example/website', project: 'website' }, { repo: 'example/board', project: 'ccboard' }], writes_on: true, limits: { children: 20, title: 120, body: 10000 } };
const previewOf = (over = {}) => ({ ...clone(NEW.preview), private: false, writes_on: true, ...over });
const created = (n = 3, over = {}) => ({
  ok: true, repo: 'example/website', key: 'aaaaaaaaaaaa', resumed: false, writes: 2 * n + 4, parent: { number: 301, url: 'https://github.com/example/website/issues/301', state: 'created' },
  children: Array.from({ length: n }, (_, i) => ({ index: i, row: i + 1, slug: `aaaaaaaaaaaa-0${i + 1}`, number: 302 + i, url: `https://github.com/example/website/issues/${302 + i}`, state: 'created', attached: true })),
  done: true, notes: [], error: null, reason: null, unknown: false, step: null, ...over,
});
const fail = (status, body, message) => Object.assign(new Error(message || (body && body.error) || 'x'), { status, body });

/** A world with the real scripts and the board's three answers. Each answer is a value or a function of the request; a function may throw fail(...). */
async function newWorld({ opts = OPTIONS, preview = () => previewOf(), create = () => created() } = {}) {
  const { w } = homeWorld({ state: fakeState() });
  made.push(w);
  w.load('pages/epic.js');
  w.load('pages/epic-new.js');
  w.ctx.__opts = opts; w.ctx.__preview = preview; w.ctx.__create = create; w.ctx.__list = clone(FX.list);
  w.run(`__answers['/api/epics'] = (req) => {
    const call = (v) => (typeof v === 'function' ? v(req) : v);
    if (req.path.startsWith('/api/epics/new')) return call(__opts);
    if (req.path === '/api/epics/preview') return call(__preview);
    if (req.path === '/api/epics' && req.method === 'POST') return call(__create);
    return __list;
  }`);
  return w;
}

const sheet = (w) => w.document.getElementById('sheet');
const open = async (w, opts = {}) => { w.ctx.__o = opts; w.run('epicNewOpen(__o)'); await settle(); return sheet(w); };
const rowsOf = (w) => sheet(w).querySelectorAll('.epn-row');
const q = (w, sel) => sheet(w).querySelector(sel);
const type = (n, v) => { n.value = v; n.dispatchEvent({ type: 'input' }); };
const keydown = (n, k, extra = {}) => { const ev = { type: 'keydown', key: k, preventDefault() { this.prevented = true; }, ...extra }; n.dispatchEvent(ev); return ev; };
const act = (row, name) => row.querySelector(`[data-act=${name}]`);
const create = (w) => q(w, '.epn-create');
const previewBtn = (w) => q(w, '.epn-prev');
const words = (w) => text(q(w, '.epn-words'));
const posts = (w, p) => calls(w).filter((c) => c.method === 'POST' && c.path === p);
const state = (w) => w.run('epicNew.S');
const toasts = (w) => plain(w.get('__toasts'));

/** Fill the first row, add two more, so the epic has three typed issues. */
async function typed(w, { title = 'Checkout rework' } = {}) {
  type(q(w, '[data-ctl=epic-title]'), title);
  type(rowsOf(w)[0].querySelector('[data-ctl=title]'), 'Tax rules table');
  q(w, '.epn-add').click();
  q(w, '.epn-add').click();
  const rows = rowsOf(w);
  type(rows[1].querySelector('[data-ctl=title]'), 'Payment step');
  type(rows[2].querySelector('[data-ctl=title]'), 'Receipt email');
  return rows;
}

// ---------------------------------------------------------------- the sheet opens

test('it opens as a native dialog sheet called New epic, asks the board for its repositories once and starts on the first one with one empty issue', async () => {
  const w = await newWorld();
  const dlg = await open(w);
  assert.equal(dlg.open, true);
  assert.equal(text(dlg.querySelector('.sheet-title')), 'New epic');
  assert.deepEqual(calls(w).map((c) => `${c.method} ${c.path}`), ['GET /api/epics/new']);
  assert.deepEqual(q(w, '[data-ctl=repo]').querySelectorAll('option').map((o) => text(o)), ['example/website', 'example/board']);
  assert.equal(q(w, '[data-ctl=repo]').value, 'example/website');
  assert.equal(rowsOf(w).length, 1);
  assert.deepEqual(calls(w).filter((c) => c.method !== 'GET'), [], 'opening writes nothing and previews nothing');
});

test('a project name is passed on so its repositories come first, and a repository named by the caller is the one the sheet starts on', async () => {
  const w = await newWorld();
  await open(w, { project: 'ccboard', repo: 'example/board' });
  assert.equal(calls(w)[0].path, '/api/epics/new?project=ccboard');
  assert.equal(q(w, '[data-ctl=repo]').value, 'example/board');
});

test('a board with no GitHub remote says so and offers no form, no Preview and no Create', async () => {
  const w = await newWorld({ opts: { repos: [], writes_on: true } });
  await open(w);
  assert.ok(!q(w, '.epn-empty').classList.contains('hidden'));
  assert.match(text(q(w, '.epn-empty')), /No GitHub repository here/);
  assert.ok(q(w, '.epn-fields').classList.contains('hidden'));
  assert.ok(q(w, '.epn-actions').classList.contains('hidden'));
});

test('a board that cannot be read says so beside a Try again that asks again', async () => {
  let n = 0;
  const w = await newWorld({ opts: () => { if (++n === 1) throw fail(503, null, 'The board did not answer'); return OPTIONS; } });
  await open(w);
  assert.ok(!q(w, '.epn-failed').classList.contains('hidden'));
  assert.match(text(q(w, '.epn-failed')), /The board did not answer/);
  q(w, '.epn-failed button').click();
  await settle();
  assert.ok(q(w, '.epn-failed').classList.contains('hidden'));
  assert.equal(rowsOf(w).length, 1);
});

test('the repository last used is remembered, but only when the board still has it', async () => {
  const w = await newWorld();
  w.ctx.__store = { 'ccboard:epic-new-repo': 'example/board' };
  w.run(`localStorage.getItem = (k) => (k in __store ? __store[k] : null); localStorage.setItem = (k, v) => { __store[k] = v; };`);
  await open(w);
  assert.equal(q(w, '[data-ctl=repo]').value, 'example/board');
  w.run(`__store['ccboard:epic-new-repo'] = 'gone/repo'`);
  w.run('closeSheet()');
  await open(w);
  assert.equal(q(w, '[data-ctl=repo]').value, 'example/website');
});

// ---------------------------------------------------------------- rows: add, move, remove, with buttons and keys

const titlesOf = (w) => rowsOf(w).map((r) => r.querySelector('[data-ctl=title]').value);

test('Add issue adds a row at the end, numbers them and focuses the new title; the 20th row is the last and says why', async () => {
  const w = await newWorld();
  await open(w);
  q(w, '.epn-add').click();
  assert.equal(rowsOf(w).length, 2);
  assert.deepEqual(rowsOf(w).map((r) => text(r.querySelector('.epn-rowk'))), ['Issue 1', 'Issue 2']);
  assert.equal(w.document.activeElement, rowsOf(w)[1].querySelector('[data-ctl=title]'));
  for (let i = 0; i < 30; i++) q(w, '.epn-add').click();
  assert.equal(rowsOf(w).length, 20);
  assert.equal(q(w, '.epn-add').disabled, true);
  assert.match(text(q(w, '.epn-addnote')), /20 issues is the most/);
  assert.match(text(q(w, '.epn-n')), /20 of 20/);
});

test('Move up and Move down reorder rows, keep what was typed, disable the ends and keep the focus on the button that was used', async () => {
  const w = await newWorld();
  await open(w);
  await typed(w);
  assert.equal(act(rowsOf(w)[0], 'up').disabled, true);
  assert.equal(act(rowsOf(w)[2], 'down').disabled, true);
  act(rowsOf(w)[2], 'up').click();
  assert.deepEqual(titlesOf(w), ['Tax rules table', 'Receipt email', 'Payment step']);
  assert.equal(w.document.activeElement, act(rowsOf(w)[1], 'up'), 'the moved row keeps the focus on the same kind of control');
  act(rowsOf(w)[0], 'down').click();
  assert.deepEqual(titlesOf(w), ['Receipt email', 'Tax rules table', 'Payment step']);
  assert.deepEqual(rowsOf(w).map((r) => text(r.querySelector('.epn-rowk'))), ['Issue 1', 'Issue 2', 'Issue 3']);
});

test('Alt+Up and Alt+Down in a title move the row; Ctrl+Enter adds one below; Delete on Remove removes the row', async () => {
  const w = await newWorld();
  await open(w);
  await typed(w);
  const t = (i) => rowsOf(w)[i].querySelector('[data-ctl=title]');
  const ev = keydown(t(2), 'ArrowUp', { altKey: true });
  assert.equal(ev.prevented, true);
  assert.deepEqual(titlesOf(w), ['Tax rules table', 'Receipt email', 'Payment step']);
  assert.equal(w.document.activeElement, t(1), 'the title of the moved row is still the focus');
  keydown(t(1), 'ArrowDown', { altKey: true });
  assert.deepEqual(titlesOf(w), ['Tax rules table', 'Payment step', 'Receipt email']);
  keydown(t(0), 'Enter', { ctrlKey: true });
  assert.equal(rowsOf(w).length, 4);
  assert.equal(t(1).value, '', 'the new row is below the one it was added from');
  assert.equal(w.document.activeElement, t(1));
  keydown(t(0), 'Enter', { metaKey: true });
  assert.equal(rowsOf(w).length, 5);
  const rm = act(rowsOf(w)[1], 'remove');
  keydown(rm, 'Delete');
  assert.equal(rowsOf(w).length, 4);
  keydown(act(rowsOf(w)[0], 'remove'), 'Backspace');
  assert.equal(rowsOf(w).length, 3);
  assert.ok(!titlesOf(w).includes('Tax rules table') || titlesOf(w).length === 3);
});

test('Remove drops a row, offers Undo that puts it back where it was, and the last row cannot be removed', async () => {
  const w = await newWorld();
  await open(w);
  await typed(w);
  act(rowsOf(w)[1], 'remove').click();
  assert.deepEqual(titlesOf(w), ['Tax rules table', 'Receipt email']);
  assert.match(text(q(w, '.epn-undo')), /Issue 2 removed/);
  q(w, '.epn-undo-b').click();
  assert.deepEqual(titlesOf(w), ['Tax rules table', 'Payment step', 'Receipt email']);
  assert.equal(text(q(w, '.epn-undo')), '');
  act(rowsOf(w)[0], 'remove').click(); act(rowsOf(w)[0], 'remove').click();
  assert.equal(rowsOf(w).length, 1);
  assert.equal(act(rowsOf(w)[0], 'remove').disabled, true, 'an epic keeps at least one issue');
});

test('Depends on lists only the rows above, is sent as the number of that row, and is cleared in words when a move or a removal would make it point down or nowhere', async () => {
  const w = await newWorld();
  await open(w);
  const rows = await typed(w);
  assert.equal(rows[0].querySelector('[data-ctl=dep]').parentNode.classList.contains('hidden'), true, 'the first row has nothing above it');
  assert.deepEqual(rows[2].querySelector('[data-ctl=dep]').querySelectorAll('option').map((o) => text(o)), ['none', 'Issue 1: Tax rules table', 'Issue 2: Payment step']);
  const dep = rows[2].querySelector('[data-ctl=dep]');
  dep.value = String(state(w).rows[1].id);
  dep.dispatchEvent({ type: 'change' });
  previewBtn(w).click(); await settle();
  assert.deepEqual(posts(w, '/api/epics/preview')[0].body.children.map((c) => c.depends_on), [null, null, 2]);
  act(rowsOf(w)[2], 'up').click();                                   // the row that depends on Payment step now sits above it
  assert.deepEqual(titlesOf(w), ['Tax rules table', 'Receipt email', 'Payment step']);
  assert.match(text(rowsOf(w)[1].querySelector('.epn-rownote')), /Depends on was cleared/);
  previewBtn(w).click(); await settle();
  assert.deepEqual(posts(w, '/api/epics/preview')[1].body.children.map((c) => c.depends_on), [null, null, null]);
  // a removal clears the rows that depended on the removed one, and says how many
  const d2 = rowsOf(w)[2].querySelector('[data-ctl=dep]');
  d2.value = String(state(w).rows[1].id);
  d2.dispatchEvent({ type: 'change' });
  act(rowsOf(w)[1], 'remove').click();
  assert.match(text(q(w, '.epn-undo')), /1 issue depended on it; that was cleared/);
});

// ---------------------------------------------------------------- Preview, and Create only after it

test('Create is disabled before a preview, with Preview as the way forward; Preview sends the typed epic and writes nothing', async () => {
  const w = await newWorld();
  await open(w);
  assert.equal(create(w).disabled, true);
  assert.equal(words(w), 'Preview first. Nothing is written until you press Create.');
  await typed(w);
  assert.equal(create(w).disabled, true, 'typing is not a preview');
  previewBtn(w).click(); await settle();
  const sent = posts(w, '/api/epics/preview');
  assert.equal(sent.length, 1);
  assert.deepEqual(sent[0].body, { repo: 'example/website', title: 'Checkout rework', goal: '', children: [
    { title: 'Tax rules table', description: '', tier: 'B', size: 'M', depends_on: null }, { title: 'Payment step', description: '', tier: 'B', size: 'M', depends_on: null },
    { title: 'Receipt email', description: '', tier: 'B', size: 'M', depends_on: null }] });
  assert.deepEqual(posts(w, '/api/epics'), [], 'a preview creates nothing');
  assert.equal(create(w).disabled, false);
});

test('the preview shows the call count, whether the repository is public, every title with its labels, tier and size, and the bodies collapsed', async () => {
  const w = await newWorld({ preview: () => previewOf({ private: false }) });
  await open(w);
  await typed(w);
  previewBtn(w).click(); await settle();
  const host = q(w, '.epn-preview');
  assert.ok(!host.classList.contains('hidden'));
  assert.match(text(q(w, '.epn-calls')), /12 GitHub calls: 2 labels, 5 issues, 4 attachments and 1 edit/);
  assert.match(text(q(w, '.epn-vis')), /example\/website is public: anyone can read what is written/);
  const items = host.querySelectorAll('.epn-pitem');
  assert.equal(items.length, 5, 'the epic and its four issues');
  assert.match(text(items[0]), /Epic.*Checkout  rework|Epic.*Checkout rework/);
  assert.match(text(items[0]), /ccboard:epic/);
  assert.match(text(items[2]), /Payment step.*ccboard:epic-child.*Tier A.*Size L.*after issue 1/);
  assert.match(text(items[2]), /claude --model opus --effort high --permission-mode acceptEdits/);
  assert.equal(host.querySelectorAll('details').length, 1 * 2 + 4, 'the epic has two bodies and each issue one, all collapsed');
  assert.ok(host.querySelectorAll('details').every((d) => !d.hasAttribute('open')));
  assert.match(text(host.querySelector('pre')), /## Goal/);
  assert.match(text(host), /#\[row 2\] stands for the number GitHub gives/);
});

test('a private repository and one whose visibility is not known are said in words, with a glyph', async () => {
  for (const [priv, glyph, want] of [[true, '○', /example\/website is private/], [null, '?', /Whether example\/website is public is not known/]]) {
    const w = await newWorld({ preview: () => previewOf({ private: priv }) });
    await open(w);
    await typed(w);
    previewBtn(w).click(); await settle();
    assert.equal(text(q(w, '.epn-glyph')), glyph);
    assert.match(text(q(w, '.epn-vis')), want);
  }
});

test('beside Create the sheet says how many issues will be written, in which repository, whether it is public, and that the board cannot delete them', async () => {
  const w = await newWorld({ preview: () => previewOf({ private: false }) });
  await open(w);
  await typed(w);
  previewBtn(w).click(); await settle();
  assert.equal(words(w), 'Create writes 5 issues on GitHub in example/website (public). The board cannot delete them.');
  const priv = await newWorld({ preview: () => previewOf({ private: true }) });
  await open(priv); await typed(priv);
  previewBtn(priv).click(); await settle();
  assert.match(words(priv), /in example\/website \(private\)\. The board cannot delete them\./);
  const unknown = await newWorld({ preview: () => previewOf({ private: null }) });
  await open(unknown); await typed(unknown);
  previewBtn(unknown).click(); await settle();
  assert.match(words(unknown), /\(not known to be public or private\)/);
});

test('any change after a preview disables Create again: a title, a description, a tier, a size, a dependency, the goal, the repository, an added, moved or removed row', async () => {
  const w = await newWorld();
  await open(w);
  await typed(w);
  const edits = [
    () => type(q(w, '[data-ctl=epic-title]'), 'Checkout rework 2'),
    () => type(q(w, '[data-ctl=goal]'), 'Pay in two steps'),
    () => type(rowsOf(w)[0].querySelector('[data-ctl=title]'), 'Tax rules'),
    () => type(rowsOf(w)[0].querySelector('[data-ctl=desc]'), 'A table'),
    () => rowsOf(w)[0].querySelector('.epn-tier [data-v=A]').click(),
    () => rowsOf(w)[0].querySelector('.epn-size [data-v=L]').click(),
    () => { const d = rowsOf(w)[1].querySelector('[data-ctl=dep]'); d.value = String(state(w).rows[0].id); d.dispatchEvent({ type: 'change' }); },
    () => { const r = q(w, '[data-ctl=repo]'); r.value = 'example/board'; r.dispatchEvent({ type: 'change' }); },
    () => q(w, '.epn-add').click(),
    () => act(rowsOf(w)[1], 'up').click(),
    () => act(rowsOf(w)[0], 'remove').click(),
  ];
  for (const [i, edit] of edits.entries()) {
    previewBtn(w).click(); await settle();
    assert.equal(create(w).disabled, false, `step ${i}: a fresh preview enables Create`);
    edit();
    assert.equal(create(w).disabled, true, `step ${i}: the change disables it`);
    assert.match(words(w), /You changed something after the preview/);
    assert.ok(q(w, '.epn-preview').classList.contains('stale'));
  }
});

test('putting an input back the way it was does not make the old preview valid by accident: only the payload that was previewed enables Create', async () => {
  const w = await newWorld();
  await open(w);
  await typed(w);
  previewBtn(w).click(); await settle();
  type(q(w, '[data-ctl=goal]'), 'x');
  assert.equal(create(w).disabled, true);
  type(q(w, '[data-ctl=goal]'), '');
  assert.equal(create(w).disabled, false, 'identical to the previewed text again');
});

// ---------------------------------------------------------------- errors beside the field, every value kept

test('the board\'s refusals appear beside the field they belong to, and every typed value stays', async () => {
  const errors = [
    { field: 'title', index: null, message: 'give the epic a title' }, { field: 'goal', index: null, message: 'this text looks like a secret or a token' },
    { field: 'child_title', index: 0, message: 'the title is 121 characters; the most is 120' }, { field: 'child_description', index: 1, message: 'this text holds a host name or an address' },
    { field: 'child_tier', index: 1, message: 'the tier is A, B, C or check' }, { field: 'child_size', index: 2, message: 'the size is S, M or L' },
    { field: 'depends_on', index: 2, message: 'depends on must be the number of an earlier row' }, { field: 'children', index: null, message: 'an epic has at most 20 issues; this one has 21' },
  ];
  const w = await newWorld({ preview: () => { throw fail(400, { error: errors[0].message, errors }); } });
  await open(w);
  await typed(w);
  type(q(w, '[data-ctl=goal]'), 'keep this goal');
  previewBtn(w).click(); await settle();
  const err = (node) => text(node.querySelector('.field-err'));
  const fTitle = q(w, '[data-ctl=epic-title]').parentNode;
  const fGoal = q(w, '[data-ctl=goal]').parentNode;
  assert.equal(err(fTitle), 'give the epic a title');
  assert.equal(err(fGoal), 'this text looks like a secret or a token');
  const rows = rowsOf(w);
  assert.equal(err(rows[0].querySelector('[data-ctl=title]').parentNode), 'the title is 121 characters; the most is 120');
  assert.equal(err(rows[1].querySelector('[data-ctl=desc]').parentNode), 'this text holds a host name or an address');
  assert.equal(err(rows[1].querySelector('.epn-tier').parentNode), 'the tier is A, B, C or check');
  assert.equal(err(rows[2].querySelector('.epn-size').parentNode), 'the size is S, M or L');
  assert.equal(err(rows[2].querySelector('[data-ctl=dep]').parentNode), 'depends on must be the number of an earlier row');
  assert.equal(text(q(w, '.epn-rowserr')), 'an epic has at most 20 issues; this one has 21');
  assert.deepEqual(titlesOf(w), ['Tax rules table', 'Payment step', 'Receipt email']);
  assert.equal(q(w, '[data-ctl=goal]').value, 'keep this goal');
  assert.equal(create(w).disabled, true);
  assert.equal(previewBtn(w).disabled, false, 'fix it and preview again');
  type(q(w, '[data-ctl=goal]'), 'a new goal');
  w.ctx.__preview = () => previewOf();
  previewBtn(w).click(); await settle();
  assert.equal(err(fGoal), '', 'a new preview clears the old messages');
});

test('a title over 120 characters is said beside the field while typing, before any request', async () => {
  const w = await newWorld();
  await open(w);
  const t = rowsOf(w)[0].querySelector('[data-ctl=title]');
  type(t, 'x'.repeat(121));
  assert.match(text(t.parentNode.querySelector('.field-err')), /the title is 121 characters; the most is 120/);
  type(t, 'x'.repeat(120));
  assert.equal(text(t.parentNode.querySelector('.field-err')), '');
  type(q(w, '[data-ctl=epic-title]'), 'y'.repeat(130));
  assert.match(text(q(w, '[data-ctl=epic-title]').parentNode.querySelector('.field-err')), /the title is 130 characters/);
  assert.deepEqual(calls(w).filter((c) => c.method === 'POST'), []);
});

// ---------------------------------------------------------------- GitHub writes off

test('with GitHub writes off, Preview works, Create is disabled and the reason is in words beside it and above the form', async () => {
  const w = await newWorld({ opts: { ...OPTIONS, writes_on: false }, preview: () => previewOf({ writes_on: false }) });
  await open(w);
  await typed(w);
  assert.ok(!q(w, '.epn-writesoff').classList.contains('hidden'));
  assert.match(text(q(w, '.epn-writesoff')), /CCBOARD_EPIC_WRITES=1/);
  assert.equal(previewBtn(w).disabled, false);
  previewBtn(w).click(); await settle();
  assert.equal(posts(w, '/api/epics/preview').length, 1);
  assert.ok(!q(w, '.epn-preview').classList.contains('hidden'), 'the preview is shown');
  assert.equal(create(w).disabled, true);
  assert.match(words(w), /GitHub writes are off on this board\. Set CCBOARD_EPIC_WRITES=1 on the box to allow Create\. Preview still works and writes nothing\./);
  assert.match(create(w).getAttribute('title'), /CCBOARD_EPIC_WRITES/);
  create(w).click();
  await settle();
  assert.deepEqual(posts(w, '/api/epics'), [], 'a disabled Create sends nothing');
});

test('a board that turned writes off after the sheet opened says so when Create is pressed and leaves every value alone', async () => {
  const w = await newWorld({ create: () => { throw fail(409, { error: 'this board does not write to GitHub (CCBOARD_EPIC_WRITES is off). Preview still works and writes nothing.', reason: 'writes_off' }); } });
  await open(w);
  await typed(w);
  previewBtn(w).click(); await settle();
  create(w).click(); await settle();
  assert.match(words(w), /GitHub writes are off on this board\. Set CCBOARD_EPIC_WRITES=1/);
  assert.equal(create(w).disabled, true);
  assert.deepEqual(titlesOf(w), ['Tax rules table', 'Payment step', 'Receipt email']);
  assert.equal(q(w, '.epn-form').classList.contains('epn-locked'), false);
});

// ---------------------------------------------------------------- Create

test('Create sends the epic that was previewed, shows progress with the count and the time, locks the form, and on success closes the sheet, opens the epic page and says Run is there', async () => {
  let release;
  const gate = new Promise((r) => { release = r; });
  const w = await newWorld({ create: () => gate.then(() => created(3)), preview: () => previewOf({ calls: { writes: 10, labels: 2, issues: 4, attachments: 3, edits: 1, min_gap_s: 1.1, min_seconds: 9.9, est_seconds: 9.9 } }) });
  await open(w);
  await typed(w);
  previewBtn(w).click(); await settle();
  const previewed = posts(w, '/api/epics/preview')[0].body;
  create(w).click(); await settle();
  assert.deepEqual(posts(w, '/api/epics')[0].body, previewed, 'what is written is what was previewed');
  assert.match(words(w), /Writing 4 issues to example\/website, about 10 s/);
  assert.equal(create(w).disabled, true);
  assert.equal(q(w, '[data-ctl=epic-title]').disabled, true, 'the form is locked while it writes');
  assert.equal(rowsOf(w)[0].querySelector('.epn-tier [data-v=A]').disabled, true);
  assert.equal(act(rowsOf(w)[0], 'down').disabled, true);
  assert.equal(q(w, '.epn-add').disabled, true);
  release(); await settle();
  assert.equal(sheet(w).open, false, 'the sheet is closed');
  assert.equal(w.location.hash, '#/epic/example/website/301');
  assert.ok(toasts(w).some((t) => /Created example\/website#301 with 3 issues\. Run is on this page\./.test(t.text) && t.kind === 'ok'));
});

test('a create that stops midway says where, lists what exists with links, offers Resume and Edit as a new epic, and keeps every value', async () => {
  const stopped = created(3, { ok: false, done: false, step: 'child 2', reason: 'rate_limited', error: 'GitHub asked us to wait', children: created(3).children.slice(0, 1) });
  const w = await newWorld({ create: () => stopped });
  await open(w);
  await typed(w);
  previewBtn(w).click(); await settle();
  create(w).click(); await settle();
  assert.equal(sheet(w).open, true, 'it stays open');
  assert.match(words(w), /Stopped while writing issue 2: GitHub asked us to wait\. Resume writes only what is missing\./);
  assert.equal(text(create(w)), 'Resume');
  assert.equal(create(w).disabled, false);
  assert.ok(previewBtn(w).classList.contains('hidden'));
  const ex = q(w, '.epn-exists');
  assert.ok(!ex.classList.contains('hidden'));
  assert.deepEqual(ex.querySelectorAll('.epn-ex').map((n) => text(n).replace(/\s+/g, ' ')), ['Epic #301 written', 'Issue 1 #302 written, attached']);
  assert.equal(ex.querySelector('a').getAttribute('href'), 'https://github.com/example/website/issues/301');
  assert.equal(q(w, '[data-ctl=epic-title]').disabled, true, 'locked: a changed title would be a different epic');
  assert.deepEqual(titlesOf(w), ['Tax rules table', 'Payment step', 'Receipt email']);
  assert.ok(!q(w, '.epn-again').classList.contains('hidden'));
});

test('Resume sends the very same epic again and, when it finishes, goes to the epic page', async () => {
  let n = 0;
  const w = await newWorld({ create: () => (++n === 1 ? created(3, { ok: false, done: false, step: 'child 2', reason: 'rate_limited', error: 'GitHub asked us to wait', children: [] }) : created(3, { resumed: true })) });
  await open(w);
  await typed(w);
  previewBtn(w).click(); await settle();
  create(w).click(); await settle();
  create(w).click(); await settle();
  const sent = posts(w, '/api/epics');
  assert.equal(sent.length, 2);
  assert.deepEqual(sent[1].body, sent[0].body);
  assert.equal(w.location.hash, '#/epic/example/website/301');
  assert.ok(toasts(w).some((t) => /some were already there/.test(t.text)));
});

test('an outcome that is not known is said in words and Resume is the only way on (the board looks for the marker first)', async () => {
  const w = await newWorld({ create: () => created(3, { ok: false, done: false, step: 'child 2', reason: 'timeout', unknown: true, error: 'gh did not answer in time, so the outcome of the write is not known', children: [] }) });
  await open(w);
  await typed(w);
  previewBtn(w).click(); await settle();
  create(w).click(); await settle();
  assert.match(words(w), /GitHub did not say whether the last write happened\. Resume looks on GitHub first, so that write is not made twice\./);
  assert.equal(text(create(w)), 'Resume');
});

test('an issue from an earlier try that now has other text is named, nothing is offered to resume, and the form stays editable', async () => {
  const note = 'Issue 1 (#301) is already on GitHub with different text: edit it there or start a new epic.';
  const w = await newWorld({ create: () => created(3, { ok: false, done: false, step: 'read', reason: 'differs', error: '1 of the issues from an earlier try is already on GitHub with different text, so the board wrote nothing more', notes: [note], children: [] }) });
  await open(w);
  await typed(w);
  previewBtn(w).click(); await settle();
  create(w).click(); await settle();
  assert.ok(toasts(w).some((t) => t.text.includes(note) && !/Resume writes only what is missing/.test(t.text) && t.kind === 'bad'));
  assert.doesNotMatch(words(w), /Resume writes only what is missing/);
  assert.notEqual(text(create(w)), 'Resume');
  assert.deepEqual(titlesOf(w), ['Tax rules table', 'Payment step', 'Receipt email']);
});

test('a connection that breaks while writing keeps every value and offers Resume, never a blind second Create', async () => {
  let n = 0;
  const w = await newWorld({ create: () => { if (++n === 1) throw new Error('Failed to fetch'); return created(3); } });
  await open(w);
  await typed(w);
  previewBtn(w).click(); await settle();
  create(w).click(); await settle();
  assert.match(words(w), /The connection broke while the board was writing\. Some issues may already be on GitHub\. Resume looks on GitHub first/);
  assert.equal(text(create(w)), 'Resume');
  assert.deepEqual(titlesOf(w), ['Tax rules table', 'Payment step', 'Receipt email']);
  create(w).click(); await settle();
  assert.equal(posts(w, '/api/epics').length, 2);
  assert.equal(w.location.hash, '#/epic/example/website/301');
});

test('Edit as a new epic gives the form back, forgets the stop and needs a new Preview before Create', async () => {
  const w = await newWorld({ create: () => { throw new Error('Failed to fetch'); } });
  await open(w);
  await typed(w);
  previewBtn(w).click(); await settle();
  create(w).click(); await settle();
  q(w, '.epn-again').click();
  assert.equal(q(w, '[data-ctl=epic-title]').disabled, false);
  assert.equal(text(create(w)), 'Create');
  assert.equal(create(w).disabled, true, 'a new preview first');
  assert.ok(q(w, '.epn-exists').classList.contains('hidden'));
  assert.equal(act(rowsOf(w)[0], 'up').disabled, true, 'the first row still has nothing to move up to');
  assert.equal(act(rowsOf(w)[1], 'up').disabled, false);
});

test('a create already running for the same epic is waited for, not doubled', async () => {
  const w = await newWorld({ create: () => { throw fail(409, { error: 'this epic is being created right now; wait for it to finish', reason: 'in_progress' }); } });
  await open(w);
  await typed(w);
  previewBtn(w).click(); await settle();
  create(w).click(); await settle();
  assert.match(words(w), /being created right now.*Wait for it to finish, then press Resume\./);
  assert.equal(text(create(w)), 'Resume');
});

test('a refusal of the text at Create time (a 400) goes beside the fields and writes nothing', async () => {
  const w = await newWorld({ create: () => { throw fail(400, { error: 'x', errors: [{ field: 'goal', index: null, message: 'this text names a permission bypass, which the board never writes into an issue' }] }); } });
  await open(w);
  await typed(w);
  previewBtn(w).click(); await settle();
  create(w).click(); await settle();
  assert.match(text(q(w, '[data-ctl=goal]').parentNode.querySelector('.field-err')), /permission bypass/);
  assert.equal(create(w).disabled, true);
  assert.equal(q(w, '[data-ctl=epic-title]').disabled, false, 'nothing was written, so the form is free');
});

test('closing the sheet while the board writes says how it ended in a toast, and does not move the page when it worked', async () => {
  let release;
  const gate = new Promise((r) => { release = r; });
  const w = await newWorld({ create: () => gate.then(() => created(3)) });
  await open(w);
  await typed(w);
  previewBtn(w).click(); await settle();
  create(w).click(); await settle();
  w.run('closeSheet()');
  release(); await settle();
  assert.ok(toasts(w).some((t) => /Created example\/website#301 with 3 issues\./.test(t.text)));
  assert.notEqual(w.location.hash, '#/epic/example/website/301');
});

test('the sheet returns the focus to the control that opened it', async () => {
  const w = await newWorld();
  const opener = w.document.createElement('button');
  w.document.getElementById('page').append(opener);
  opener.focus();
  await open(w);
  assert.notEqual(w.document.activeElement, opener);
  w.run('closeSheet()');
  assert.equal(w.document.activeElement, opener);
});

// ---------------------------------------------------------------- entry points

test('the Tasks Epics tab has New epic as its one filled primary, and tapping it opens the sheet', async () => {
  const w = await newWorld();
  const host = w.document.createElement('section');
  w.document.getElementById('page').append(host);
  w.run(LAZY_STUB);
  w.ctx.__host = host;
  w.run('Epics.paintList(__host)');
  await settle();
  const btn = host.querySelector('.ep-new');
  assert.ok(btn, 'a New epic button in the list head');
  assert.match(text(btn), /New epic/);
  assert.ok(btn.classList.contains('primary'));
  assert.equal(host.querySelectorAll('.primary').length, 1, 'one filled primary');
  btn.click();
  await settle();
  assert.equal(sheet(w).open, true);
  assert.equal(text(sheet(w).querySelector('.sheet-title')), 'New epic');
  w.run('Epics.stopList()');
});

test('#/tasks?tab=epics&new=1 opens the sheet once the list is drawn, and the plain address is put back', async () => {
  const w = await newWorld();
  w.run(LAZY_STUB);
  w.ctx.__replaced = [];
  w.run(`globalThis.history = { replaceState: (a, b, url) => { __replaced.push(url); } };`);
  w.location.hash = '#/tasks?tab=epics&new=1';
  const host = w.document.createElement('section');
  w.document.getElementById('page').append(host);
  w.ctx.__host = host;
  w.run('currentRouteValue = null; Epics.paintList(__host)');
  await settle();
  assert.equal(sheet(w).open, true);
  await later(10);
  const put = plain(w.get('__replaced'));
  assert.ok(put.length >= 1 && put.every((u) => u === '#/tasks?tab=epics'), 'the address goes back to the plain one');
  w.run('closeSheet(); Epics.stopList()');
  w.location.hash = '#/tasks?tab=epics';
  w.run('currentRouteValue = null; Epics.paintList(__host)');
  await settle();
  assert.equal(sheet(w).open, false, 'the plain address does not open it');
  w.run('Epics.stopList()');
});

test('the project page\'s More menu has New epic and passes the project, and the palette finds it with "new epic" and with "epic"', async () => {
  const src = fs.readFileSync(path.join(STATIC, 'pages', 'project.js'), 'utf8');
  assert.match(src, /label: 'New epic', icon: 'git-branch', onClick: \(\) => \{ if \(typeof Lazy !== 'undefined'\) Lazy\.run\('epic', \(\) => epicNewOpen\(\{ project: P\.project \}\)/);
  const { w } = homeWorld({ state: fakeState() });
  made.push(w);
  w.load('pages/epic.js'); w.load('pages/epic-new.js'); w.load('keymap.js'); w.load('palette.js');
  w.run(LAZY_STUB);
  w.ctx.__list = clone(FX.list); w.ctx.__opts = OPTIONS;
  w.run(`__answers['/api/epics'] = (req) => (req.path.startsWith('/api/epics/new') ? __opts : __list); __answers['/api/skills'] = { agent: 'claude', skills: [] };`);
  w.run('Palette.open()');
  const type2 = async (v) => { w.run(`Palette.ui.input.value = ${JSON.stringify(v)}; Palette.render()`); await settle(); };
  const rows = () => w.document.querySelectorAll('#helpdlg .pal-item').map((n) => text(n.querySelector('.pal-label')));
  await type2('');
  assert.ok(!rows().includes('New epic'), 'the empty palette keeps its short list');
  for (const v of ['epic', 'ep', 'new epic', 'new ep']) { await type2(v); assert.ok(rows().includes('New epic'), v); }
  await type2('new epic');
  w.run('Palette.ui.sel = 0; Palette.runSelected({})');
  await settle();
  assert.equal(sheet(w).open, true);
  assert.deepEqual(calls(w).filter((c) => c.path.startsWith('/api/epics/new')).map((c) => c.path), ['/api/epics/new']);
});

test('nothing here uses innerHTML, a style attribute or cssText, and the sheet is the shell\'s native dialog', () => {
  const src = fs.readFileSync(path.join(STATIC, 'pages', 'epic-new.js'), 'utf8');
  assert.doesNotMatch(src, /innerHTML|insertAdjacentHTML|cssText|\.style\b|'style'/);
  assert.match(src, /openSheet\(\{ title: 'New epic'/);
  const css = fs.readFileSync(path.join(STATIC, 'pages', 'epic.css'), 'utf8');
  const mine = css.slice(css.indexOf('dialog#sheet.right.epn-wide'));
  assert.ok(mine.length > 500);
  for (const line of mine.split('\n')) if (/^[#.a-z]/i.test(line) && !/^(@media|html\.force-coarse)/.test(line)) assert.match(line, /^(dialog)?#sheet/, line.slice(0, 60));
  assert.match(mine, /@media \(pointer:coarse\)[\s\S]*var\(--tap\)/);
  assert.match(mine, /html\.force-coarse #sheet \.epn-desc/);
});

// ---------------------------------------------------------------- the demo

async function demoWorld(search = '') {
  const { w } = homeWorld({ state: fakeState() });
  made.push(w);
  w.load('pages/epic.js');
  w.load('pages/epic-new.js');
  w.location.search = search;
  w.ctx.__fx = clone(FX);
  w.ctx.crypto = globalThis.crypto;                     // the key of an epic is a SHA-256, as the board makes it
  w.ctx.TextEncoder = TextEncoder;
  w.run(`globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(__fx)) });`);
  return w;
}
const demo = (w, method, p, body) => { w.ctx.__a = [method, p, body]; return w.run('demoApi(...__a)').then(plain); };
const sample = () => clone(NEW.sample);

test('the demo\'s own preview builder gives the board\'s preview for the sample, byte for byte (the fixture is the board\'s: tests/test_epic_new_demo.py)', async () => {
  const w = await demoWorld();
  const got = await demo(w, 'POST', '/api/epics/preview', sample());
  const want = clone(NEW.preview);
  assert.equal(got.private, false);
  assert.equal(got.writes_on, true);
  delete got.private; delete got.writes_on;
  assert.deepEqual(got, want);
});

test('the demo\'s three answers: the repositories (the named project first), a refused text with every message, a private repository, and writes off', async () => {
  const w = await demoWorld();
  const o = await demo(w, 'GET', '/api/epics/new?project=ccboard');
  assert.deepEqual(o.repos.map((r) => r.repo), ['example/board', 'example/website']);
  assert.equal(o.writes_on, true);
  const bad = { repo: 'example/website', title: '', goal: 'use bypassPermissions', children: [{ title: 'x'.repeat(121), description: '', tier: 'Z', size: 'M', depends_on: 1 }] };
  await assert.rejects(demo(w, 'POST', '/api/epics/preview', bad), (e) => {
    const fields = e.body.errors.map((x) => `${x.field}:${x.index}`).sort().join(' ');
    assert.equal(fields, 'child_tier:0 child_title:0 depends_on:0 goal:null title:null');
    return e.status === 400;
  });
  const priv = await demoWorld('?newepic=private');
  assert.equal((await demo(priv, 'POST', '/api/epics/preview', sample())).private, true);
  const off = await demoWorld('?newepic=writesoff');
  assert.equal((await demo(off, 'GET', '/api/epics/new')).writes_on, false);
  assert.equal((await demo(off, 'POST', '/api/epics/preview', sample())).writes_on, false);
  await assert.rejects(demo(off, 'POST', '/api/epics', sample()), (e) => e.status === 409 && e.body.reason === 'writes_off');
  const none = await demoWorld('?newepic=norepo');
  assert.deepEqual((await demo(none, 'GET', '/api/epics/new')).repos, []);
});

test('the demo\'s create writes the epic, which then has its own page and a place at the top of the Epics list; ?newepic=fail stops at issue 2 and Resume finishes with no duplicate', async () => {
  const w = await demoWorld('?newepic=fail');
  const first = await demo(w, 'POST', '/api/epics', sample());
  assert.equal(first.ok, false);
  assert.equal(first.step, 'child 2');
  assert.deepEqual(first.children.map((c) => c.row), [1]);
  assert.equal(first.parent.state, 'created');
  const second = await demo(w, 'POST', '/api/epics', sample());
  assert.equal(second.ok, true);
  assert.equal(second.resumed, true);
  assert.deepEqual(second.children.map((c) => `${c.row}:${c.state}`), ['1:existing', '2:created', '3:created', '4:created']);
  assert.equal(second.parent.number, first.parent.number, 'the same epic, not a second one');
  assert.equal(new Set([second.parent.number, ...second.children.map((c) => c.number)]).size, 5, 'no number twice');
  const n = second.parent.number;
  const page = await demo(w, 'GET', `/api/epics/example/website/${n}`);
  assert.equal(page.epic.title, 'Checkout rework');
  assert.equal(page.children.length, 4);
  assert.ok(page.children.every((c) => c.state === 'open' && c.claim_state === 'none'));
  const list = await demo(w, 'GET', '/api/epics');
  assert.equal(list.epics[0].number, n);
  assert.deepEqual(list.epics[0].children, { open: 4, closed: 0, total: 4, more: false });
});

test('?newepic=unknown stops with an outcome that is not known, and Resume finds the issue it wrote rather than writing it again', async () => {
  const w = await demoWorld('?newepic=unknown');
  const first = await demo(w, 'POST', '/api/epics', sample());
  assert.equal(first.ok, false);
  assert.equal(first.unknown, true);
  assert.equal(first.reason, 'timeout');
  const second = await demo(w, 'POST', '/api/epics', sample());
  assert.equal(second.ok, true);
  assert.deepEqual(second.children.map((c) => c.state), ['existing', 'existing', 'created', 'created']);
  assert.equal(new Set(second.children.map((c) => c.number)).size, 4);
});

test('the whole sheet runs on the demo\'s answers: open, type, Preview, Create, and ?newepic=fail then Resume ends on the new epic\'s page', async () => {
  const w = await demoWorld('?newepic=fail');
  w.run(`api = async (method, path, body) => { __calls.push({ method, path, body }); return demoApi(method, path, body); }`);
  await open(w);
  await new Promise((r) => setTimeout(r, 120));
  await settle();
  type(q(w, '[data-ctl=epic-title]'), 'Demo epic');
  type(rowsOf(w)[0].querySelector('[data-ctl=title]'), 'First issue');
  q(w, '.epn-add').click();
  type(rowsOf(w)[1].querySelector('[data-ctl=title]'), 'Second issue');
  q(w, '.epn-add').click();
  type(rowsOf(w)[2].querySelector('[data-ctl=title]'), 'Third issue');
  previewBtn(w).click();
  await new Promise((r) => setTimeout(r, 300));
  await settle();
  assert.match(words(w), /Create writes 4 issues on GitHub in example\/website \(public\)\. The board cannot delete them\./);
  create(w).click();
  await new Promise((r) => setTimeout(r, 1300));
  await settle();
  assert.match(words(w), /Stopped while writing issue 2: GitHub asked us to wait\./);
  assert.match(text(q(w, '.epn-exists')), /Epic #301 written/);
  create(w).click();
  await new Promise((r) => setTimeout(r, 1300));
  await settle();
  assert.equal(sheet(w).open, false);
  assert.equal(w.location.hash, '#/epic/example/website/301');
});
