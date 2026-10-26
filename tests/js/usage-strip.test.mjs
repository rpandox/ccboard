import assert from 'node:assert/strict';
import { test } from 'node:test';
import { makeWorld } from './harness.mjs';
import { installDom } from './minidom.mjs';
function world() {
  const w = makeWorld(); installDom(w); w.load('core.js');
  w.load('pool-usage.js');
  return w;
}
const group = (pct, extra = {}) => ({ id: 1, provider: 'claude', accounts: ['one', 'two'], window_5h_pct: pct, weekly_pct: 70, state: 'ok', age_s: 20, source: 'cache', confidence: 1, ...extra });
test('headroom thresholds include both boundaries', () => {
  const kind = world().get('poolKind');
  for (const [pct, want] of [[0, 'filled'], [49.9, 'filled'], [50, 'half'], [85, 'half'], [85.1, 'hollow'], [100, 'hollow']]) assert.equal(kind(group(pct, { weekly_pct: 0 })), want);
});
test('unknown is hatched with age and never a substitute percentage', () => {
  const w = world();
  const strip = w.get('poolStrip')({ groups: [group(null, { state: 'unknown', age_s: 420 })] });
  assert.match(strip.textContent, /unknown, 7 min old/);
  assert.ok(strip.querySelector('.pool-hatched'));
  assert.doesNotMatch(strip.textContent, /(?:0|50)%/);
});
test('one group with two accounts draws one button and lists both in the sheet', () => {
  const w = world(), groups = [group(22)];
  const strip = w.get('poolStrip')({ groups });
  assert.equal(strip.querySelectorAll('button').length, 1);
  assert.match(w.get('poolSheetBody')(groups).textContent, /Accounts: one, two/);
  assert.equal(w.get('poolSheetBody')(groups).querySelectorAll('svg').length, 2);
});
test('pools off draws nothing', () => { assert.equal(world().get('poolStrip')(null), null); });
test('weekly-only group is known while missing five-hour window remains unknown', () => {
  const w = world();
  assert.equal(w.get('poolKind')(group(null)), 'half');
  assert.match(w.get('poolSheetBody')([group(null)]).textContent, /5 h: unknown/);
});

test('weekly exhaustion controls the symbol and names the limiting window', () => {
  const w = world(), g = group(10, { weekly_pct: 100, account_labels: { one: 'work', two: 'work-copy' } });
  assert.equal(w.get('poolKind')(g), 'hollow');
  const strip = w.get('poolStrip')({ groups: [g] });
  assert.match(strip.textContent, /work, work-copy · weekly 100%/);
  assert.match(strip.querySelector('button').getAttribute('aria-label'), /weekly 100%/);
  assert.equal(w.get('poolKind')(group(null, { weekly_pct: 100 })), 'hollow');
  const sheet = w.get('poolSheetBody')([g]);
  assert.match(sheet.querySelector('h3').textContent, /work, work-copy/);
  assert.doesNotMatch(sheet.querySelector('h3').textContent, /Group 1/);
  assert.equal(w.get('poolStrip')({ groups: [group(10)] }).querySelectorAll('button .dim').length, 2);
});

test('poll updates keyed buttons in place, preserves focus, and opens current data', () => {
  const w = world();
  if (!w.document.getElementById('banner')) w.document.body.append(w.get('el')('div', { id: 'banner' }));
  const paint = w.get('paintPoolStrip');
  paint({ pools: { groups: [group(10)] } });
  const before = w.document.querySelector('.pool-group');
  before.focus();
  paint({ pools: { groups: [group(12, { weekly_pct: 100, account_labels: { one: 'work' } })] } });
  assert.equal(w.document.querySelector('.pool-group'), before);
  assert.equal(w.document.activeElement, before);
  assert.equal(before.isConnected, true);
  assert.match(before.textContent, /work, two · weekly 100%/);
  w.run('globalThis.openSheet = (options) => { globalThis.openedPool = options; };');
  before.onclick();
  assert.match(w.get('openedPool').body.textContent, /Weekly: 100%/);
  paint({ pools: { groups: [] } });
  assert.equal(w.document.querySelector('.pool-group'), null);
});

test('pool lazy load remembers failures, backs off 1/5/15 minutes and toasts once', async () => {
  const w = makeWorld({ companions: false }); installDom(w);
  w.run(`
    globalThis.clock = 1000; Date.now = () => clock;
    globalThis.loads = 0; globalThis.toasts = 0;
    globalThis.Lazy = { load: () => { loads++; return Promise.reject(new Error('offline')); }, fail: () => { toasts++; }, watchLinks() {} };
    globalThis.registerServiceWorker = () => {};
    globalThis.installLifecycleListeners = () => {};
    globalThis.rewriteLegacyHash = () => {};
    globalThis.route = () => {};
    globalThis.startStatePolling = () => Promise.resolve();
    globalThis.state = { pools: { groups: [] } };
  `);
  w.load('main.js');
  const render = w.get('renderPoolStrip'), state = w.get('state');
  render(state); render(state);
  await Promise.resolve();
  assert.equal(w.get('loads'), 1); assert.equal(w.get('toasts'), 1);
  for (const [wait, expected] of [[60000, 2], [300000, 3], [900000, 4]]) {
    w.run(`clock += ${wait - 1}`); render(state);
    assert.equal(w.get('loads'), expected - 1);
    w.run('clock++'); render(state); await Promise.resolve();
    assert.equal(w.get('loads'), expected);
    assert.equal(w.get('toasts'), 1);
  }
  w.run('clock += 900000; Lazy.load = () => { loads++; globalThis.paintPoolStrip = (st) => { globalThis.paintedPools = st; }; return Promise.resolve(); };');
  render(state); await Promise.resolve();
  assert.equal(w.get('paintedPools'), state);
});

test('value changes and reordered input never move focused group nodes', () => {
  const w = world(), paint = w.get('paintPoolStrip');
  w.document.body.append(w.get('el')('div', { id: 'banner' }));
  paint({ pools: { groups: [group(10), group(80, { id: 2 })] } });
  const items = w.document.querySelector('.pool-groups'), button = items.children[1];
  button.focus();
  const insert = items.insertBefore.bind(items);
  items.insertBefore = (node, before) => {
    // Browsers lose focus when a connected node is reinserted; minidom does not.
    if (node.isConnected && w.document.activeElement === node) node.blur();
    return insert(node, before);
  };
  paint({ pools: { groups: [group(5, { id: 2 }), group(90)] } });
  assert.equal(w.document.activeElement, button);
  assert.deepEqual([...items.children].map((b) => b.getAttribute('data-pool-key')), ['claude:1', 'claude:2']);
  paint({ pools: { groups: [group(5, { id: 2 })] } });
  assert.equal(w.document.activeElement, button, 'removing a preceding group also preserves focus');
  paint({ pools: { groups: [group(10), group(5, { id: 2 })] } });
  assert.equal(w.document.activeElement, button, 'inserting a preceding group preserves focus');
});
