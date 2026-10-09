// Keyboard reach of the three pointer charts in app/static/charts.js (issue #45): cost bars, session Gantt and the heat grid. Each root is one tab stop (tabindex 0,
// role application, an aria-label and an aria-describedby pointing at the aria-live readout), the arrows / Home / End move a cursor that shows the same readout and mark
// a tap does, Escape leaves, a pointer pick moves the cursor, and the page's plain keys (Keymap, shell.js) stay quiet while a chart has the focus.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { makeWorld, plain } from './harness.mjs';
import { installDom } from './minidom.mjs';

const text = (n) => (n ? n.textContent : '');
const T0 = 1790900000;

function kWorld() {
  const w = makeWorld({ navigator: { platform: 'Linux x86_64', userAgent: 'node-test' }, console: { log() {}, error() {} } });
  installDom(w);
  w.load('core.js');
  w.load('charts.js');
  w.load('keymap.js');
  w.run(`globalThis.ResizeObserver = class { constructor() {} observe() {} disconnect() {} };`);
  return w;
}

function host(w) {
  const h = w.document.createElement('div');
  h.clientWidth = 400;
  w.document.querySelector('#page').append(h);
  return h;
}

/** A keydown on the chart root; `prevented` says whether the chart took the key. */
function press(node, key, mods = {}) {
  const e = { type: 'keydown', key, ctrlKey: false, metaKey: false, altKey: false, shiftKey: false, ...mods, prevented: false, defaultPrevented: false };
  e.preventDefault = () => { e.prevented = true; e.defaultPrevented = true; };
  node.dispatchEvent(e);
  return e;
}
const focusByKey = (node) => { node.focus(); node.dispatchEvent({ type: 'focus' }); };

const DAYS = [
  { day: '2026-10-02', total: 30, zero: false, tokens: 3000, by_project: { A: 10, B: 8, C: 5, D: 4, '(unattributed)': 3 }, by_agent: { claude: 27, codex: 3 } },
  { day: '2026-10-03', total: 0, zero: true, tokens: 0, by_project: {}, by_agent: {} },
  { day: '2026-10-04', total: 12, zero: false, tokens: 900, by_project: { A: 2, E: 1, '(unattributed)': 9 }, by_agent: { claude: 12 } },
];
const ev = (t, key, v, m) => ({ t, key, v, m });
const GEV = [
  ev(T0 + 100, 'c1', 1, { p: 'alpha', r: 'root', s: 's1', a: 'claude' }), ev(T0 + 1000, 'c1', 2, { p: 'alpha', r: 'root', s: 's1', a: 'claude' }), ev(T0 + 2000, 'c1', 3, { p: 'alpha', s: 's1' }),
  ev(T0 + 500, 'c2', 1, { p: 'beta', r: 'web', s: 's2', a: 'claude' }), ev(T0 + 3000, 'c2', 5, { p: 'beta', r: 'web', s: 's2' }),
];

function bars(w, extra = '') {
  const h = host(w);
  w.ctx.__h = h; w.ctx.__days = DAYS; w.ctx.__seen = [];
  w.run(`Charts.stackedBars(__h, __days, { by: "project", top: 2, onHover: (i) => __seen.push(i) ${extra} })`);
  return { h, node: h.querySelector('svg'), read: h.querySelector('.chart-read') };
}

function gantt(w) {
  const h = host(w);
  w.ctx.__h = h; w.ctx.__ev = GEV;
  w.run(`Charts.gantt(__h, __ev, { since: ${T0}, until: ${T0 + 4000}, rowHeight: 24 })`);
  return { h, node: h.querySelector('svg'), read: h.querySelector('.chart-read'), rows: h.querySelectorAll('g.g-row') };
}

function heat(w) {
  const h = host(w);
  const grid = Array.from({ length: 7 }, (_, d) => Array.from({ length: 24 }, (__, hr) => (d === 1 && hr === 21 ? 14 : d === 0 && hr === 1 ? 2 : 0)));
  w.ctx.__h = h; w.ctx.__g = grid;
  const node = w.run('Charts.heatmap(__h, __g, null, {})');
  return { h, node, read: h.querySelector('.chart-read') };
}

// ---------------------------------------------------------------- one tab stop with a name, a role and a spoken readout

test('every chart root is one tab stop with a role, a name and the aria-live readout as its description', () => {
  const w = kWorld();
  const roots = [bars(w), gantt(w), heat(w)];
  const ids = new Set();
  for (const { node, read, h } of roots) {
    assert.equal(node.getAttribute('tabindex'), '0');
    assert.equal(node.getAttribute('role'), 'application');
    assert.equal(node.getAttribute('aria-roledescription'), 'chart');
    assert.match(node.getAttribute('aria-label'), /\. (Left and right|Up and down|Arrow) /, 'the label says which keys move');
    assert.equal(read.getAttribute('aria-live'), 'polite');
    assert.ok(read.getAttribute('id'), 'the readout has an id');
    assert.equal(node.getAttribute('aria-describedby'), read.getAttribute('id'));
    assert.equal(node.getAttribute('data-kbd'), 'chart');
    assert.equal(h.querySelectorAll('[tabindex]').length, 1, 'nothing else inside the chart takes a tab stop');
    ids.add(read.getAttribute('id'));
  }
  assert.equal(ids.size, 3, 'ids are unique per chart');
});

// ---------------------------------------------------------------- cost bars

test('bars: left and right step the days, Home and End jump, each read whole like a tap beside the bars; onHover follows', () => {
  const w = kWorld();
  const { node, read } = bars(w);
  assert.match(text(read), /Hover or tap a day/);
  focusByKey(node);                                                                  // keyboard focus shows the first day
  assert.match(text(read), /^2026-10-02 · day \$30\.00 · 3k tok · /);
  assert.ok(press(node, 'ArrowRight').prevented, 'the chart takes the key');
  assert.match(text(read), /^2026-10-03 · no spend/);
  press(node, 'ArrowRight');
  assert.match(text(read), /^2026-10-04 · day \$12\.00/);
  press(node, 'ArrowRight');
  assert.match(text(read), /^2026-10-04 /, 'clamped at the last day');
  press(node, 'ArrowLeft');
  assert.match(text(read), /^2026-10-03/);
  press(node, 'Home');
  assert.match(text(read), /^2026-10-02/);
  press(node, 'End');
  assert.match(text(read), /^2026-10-04/);
  const marked = node.querySelectorAll('rect').filter((r) => r.classList.contains('day-sel')).map((r) => r.getAttribute('data-i'));
  assert.ok(marked.length > 0 && marked.every((i) => i === '2'), 'the picked day carries the same mark a tap gives');
  const seen = plain(w.get('__seen'));
  assert.equal(seen[seen.length - 1].day, '2026-10-04');
  assert.equal(seen.filter((s) => s.zero).length, 2, 'the zero day (visited twice) reports zero');
});

test('bars: a pointer pick moves the cursor, and a focus that came from a pointer does not overwrite the pick', () => {
  const w = kWorld();
  const { node, read } = bars(w);
  node.dispatchEvent({ type: 'pointerdown' });
  node.focus(); node.dispatchEvent({ type: 'focus' });                               // the click focuses first, then picks
  assert.match(text(read), /Hover or tap a day/, 'a mouse focus announces nothing');
  node.querySelectorAll('rect').find((r) => r.getAttribute('data-i') === '2').click();
  assert.match(text(read), /^2026-10-04/);
  press(node, 'ArrowLeft');
  assert.match(text(read), /^2026-10-03/, 'the arrows continue from the tapped day');
  node.dispatchEvent({ type: 'blur' });
  focusByKey(node);
  assert.match(text(read), /^2026-10-03/, 'tabbing back shows the cursor day');
});

// ---------------------------------------------------------------- Gantt

test('gantt: up and down step the sessions, Home and End jump, each read like a tap on its row', () => {
  const w = kWorld();
  const { node, read, rows } = gantt(w);
  focusByKey(node);
  assert.match(text(read), /^beta\/web · s2 · ended /, 'the most recent row first');
  assert.ok(rows[0].classList.contains('sel'));
  assert.ok(press(node, 'ArrowDown').prevented);
  assert.match(text(read), /^alpha · s1 · /);
  assert.ok(rows[1].classList.contains('sel') && !rows[0].classList.contains('sel'));
  press(node, 'ArrowDown');
  assert.match(text(read), /^alpha · s1 /, 'clamped at the last row');
  press(node, 'Home');
  assert.match(text(read), /^beta\/web/);
  press(node, 'End');
  assert.match(text(read), /^alpha · s1/);
  press(node, 'ArrowUp');
  assert.match(text(read), /^beta\/web/);
  rows[1].querySelector('rect').click();                                              // a tap moves the cursor too
  press(node, 'ArrowUp');
  assert.match(text(read), /^beta\/web/);
});

// ---------------------------------------------------------------- heat grid

test('heat: arrows move over the 7 x 24 grid and the hour profile row, Home and End go to the ends of the row', () => {
  const w = kWorld();
  const { node, read } = heat(w);
  focusByKey(node);
  assert.equal(text(read), 'Mon 00:00 · 0 events');
  press(node, 'ArrowRight');
  assert.equal(text(read), 'Mon 01:00 · 2 events');
  press(node, 'ArrowDown');
  assert.equal(text(read), 'Tue 01:00 · 0 events', 'same hour, next weekday');
  press(node, 'End');
  assert.equal(text(read), 'Tue 23:00 · 0 events');
  press(node, 'ArrowRight');
  assert.equal(text(read), 'Tue 23:00 · 0 events', 'clamped at the last hour');
  press(node, 'Home');
  press(node, 'ArrowUp');
  assert.equal(text(read), 'Mon 00:00 · 0 events', 'clamped at the first row would stay on Mon');
  for (let i = 0; i < 9; i++) press(node, 'ArrowDown');
  assert.equal(text(read), '00:00 · 0 events', 'the 8th row is the hour profile');
  for (let i = 0; i < 21; i++) press(node, 'ArrowRight');
  assert.equal(text(read), '21:00 · 14 events');
  const sel = node.querySelectorAll('.sel');
  assert.equal(sel.length, 1, 'one cell is marked at a time');
  assert.ok(sel[0].classList.contains('hb'));
  // a tap on a cell moves the cursor to it
  node.querySelectorAll('.cell')[24 * 1 + 21].click();
  assert.equal(text(read), 'Tue 21:00 · 14 events');
  press(node, 'ArrowLeft');
  assert.equal(text(read), 'Tue 20:00 · 0 events');
});

// ---------------------------------------------------------------- leaving, modifiers, other keys

test('Escape leaves the chart; Tab, letters and modified arrows are not taken', () => {
  const w = kWorld();
  for (const make of [bars, gantt, heat]) {
    const { node, read } = make(w);
    focusByKey(node);
    assert.equal(w.document.activeElement, node);
    const before = text(read);
    for (const k of ['Tab', 'j', 'g', 'a', 'Enter', ' ']) assert.ok(!press(node, k).prevented, `${k} passes through`);
    assert.ok(!press(node, 'ArrowRight', { ctrlKey: true }).prevented, 'a modified arrow is the browser\'s');
    assert.ok(!press(node, 'ArrowDown', { altKey: true }).prevented);
    assert.ok(!press(node, 'ArrowDown', { metaKey: true }).prevented);
    assert.equal(text(read), before);
    assert.ok(press(node, 'Escape').prevented);
    assert.notEqual(w.document.activeElement, node, 'focus left the chart');
  }
});

test('an empty chart draws no tab stop', () => {
  const w = kWorld();
  const h = host(w);
  w.ctx.__h = h;
  w.run('Charts.stackedBars(__h, [], {})');
  assert.equal(h.querySelectorAll('[tabindex]').length, 0);
  w.run('Charts.gantt(__h, [], {})');
  assert.equal(h.querySelectorAll('[tabindex]').length, 0);
  w.run('Charts.heatmap(__h, [], null, {})');
  assert.equal(h.querySelectorAll('[tabindex]').length, 0);
});

// ---------------------------------------------------------------- the page's own keys

test('Keymap leaves plain keys alone while a chart has the focus, but the palette key still works', () => {
  const w = kWorld();
  w.run(`globalThis.__log = [];
    Keymap.bindKey('j', () => { __log.push('j'); }, {});
    Keymap.bindKey('Esc', () => { __log.push('esc'); }, { dialog: true });
    Keymap.bindKey('g h', () => { __log.push('gh'); }, {});
    Keymap.bindKey('mod+k', () => { __log.push('palette'); }, { dialog: true, input: true });`);
  const { node } = bars(w);
  const handle = (k, mods) => { w.ctx.__e = { key: k, ctrlKey: false, metaKey: false, shiftKey: false, altKey: false, target: node, defaultPrevented: false, preventDefault() {}, ...mods }; w.run('Keymap.handle(__e)'); };
  w.ctx.__n = node;
  assert.equal(w.run('Keymap.inChart(__n)'), true);
  assert.equal(w.run('Keymap.inChart(__n.querySelector("rect"))'), true, 'a bar inside the chart counts');
  handle('j');
  handle('g'); handle('h');
  handle('Escape');
  assert.deepEqual(plain(w.get('__log')), [], 'j, g h and Esc do nothing in a chart');
  handle('k', { ctrlKey: true });
  assert.deepEqual(plain(w.get('__log')), ['palette']);
  // the same keys work again off the chart
  w.ctx.__e = { key: 'j', ctrlKey: false, metaKey: false, shiftKey: false, altKey: false, target: w.document.body, defaultPrevented: false, preventDefault() {} };
  w.run('Keymap.handle(__e)');
  assert.deepEqual(plain(w.get('__log')), ['palette', 'j']);
  assert.equal(w.run('Keymap.inChart(null)'), false);
  assert.equal(w.run('Keymap.inChart({})'), false);
});

test('a chart that handled its key (defaultPrevented) never reaches the page key table', () => {
  const w = kWorld();
  w.run(`globalThis.__log = []; Keymap.bindKey('Esc', () => { __log.push('esc'); }, { dialog: true });`);
  const { node } = bars(w);
  const e = press(node, 'Escape');
  assert.ok(e.defaultPrevented);
  w.ctx.__e = e;
  w.run('Keymap.handle(__e)');
  assert.deepEqual(plain(w.get('__log')), []);
});
