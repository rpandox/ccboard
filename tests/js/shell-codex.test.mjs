// Contract tests for the v0.5.12 CX pill of the topbar (shell.js: Shell.codexWindow / windowName / patchCodex): ONE window of state.usage_codex, labelled from its
// window_minutes ('CX 7D' for the weekly 10080, 'CX 5H' for 300), the longest window when the plan reports two, red when the limit was reached, 0 % once the window rolled over,
// the account named in the title when the box has several Codex accounts, and a link to the Usage page's Codex tab. Real core.js, components.js, launcher.js, router.js,
// widgets.js, pages/agents.js and shell.js on minidom's DOM (the same world as shell-accounts.test.mjs).
import assert from 'node:assert/strict';
import { test } from 'node:test';
import fs from 'node:fs';
import path from 'node:path';
import { makeWorld, STATIC } from './harness.mjs';
import { installDom } from './minidom.mjs';

const NOW = Math.floor(Date.now() / 1000);
const K1 = '3b9f1c2a7d4e5f6a8b0c1d2e', K2 = '7c8d9e0f1a2b3c4d5e6f7a8b';
const text = (n) => (n ? n.textContent : '');
const win = (pct, minutes, resets = NOW + 4 * 86400) => ({ used_percent: pct, window_minutes: minutes, resets_at: resets });
const cx = (value, over = {}) => ({ usage_codex: { value: { limit_id: 'codex', plan_type: 'plus', secondary: null, credits: null, reached: false, observed_at: new Date().toISOString(), ...value }, at: new Date().toISOString() }, ...over });
const accts = (current = K1, labels = ['Codex main', 'Codex work']) => ({ codex_accounts: { current, list: [{ key: K1, label: labels[0], current: current === K1 }, { key: K2, label: labels[1], current: current === K2 }] } });

function sWorld({ agents = true } = {}) {
  const w = makeWorld();
  installDom(w);
  for (const f of ['core.js', 'components.js', 'launcher.js', 'router.js', 'pages/widgets.js', ...(agents ? ['pages/agents.js'] : []), 'shell.js']) w.load(f);
  w.run('Shell.refs = {}; Shell.buildTopbar(document.querySelector("#topbar"))');
  return w;
}
const paint = (w, st) => { w.ctx.__st = st; w.run('state = __st; Shell.patchUsage(__st)'); };
const pill = (w) => w.document.querySelector('#topbar [data-pill="codex"]');
const shown = (n) => !!n && !n.classList.contains('hidden');

test('the pill is a link to the Usage page\'s Codex tab, hidden until a reading arrives', () => {
  const w = sWorld();
  const p = pill(w);
  assert.equal(p.tagName, 'A');
  assert.equal(p.getAttribute('href'), '#/usage?agent=codex');
  assert.ok(p.classList.contains('hidden'));
  assert.equal(w.document.querySelector('#topbar [data-pill="5h"]').getAttribute('href'), '#/usage', 'the Claude pills still go to the plain page');
});

test('a weekly window of 10080 minutes reads CX 7D with the weekly title, the plan and the reset', () => {
  const w = sWorld();
  paint(w, cx({ primary: win(17, 10080) }));
  assert.ok(shown(pill(w)));
  assert.equal(text(pill(w).pl), 'CX 7D');
  assert.equal(text(pill(w).pv), '17%');
  assert.match(pill(w).getAttribute('title'), /^Codex weekly window: 17% used · resets in .* · plus plan$/);
  assert.ok(pill(w).classList.contains('ok'));
});

test('a window of 300 minutes reads CX 5H; an odd window is named from its minutes', () => {
  const w = sWorld();
  paint(w, cx({ primary: win(42, 300, NOW + 3600) }));
  assert.equal(text(pill(w).pl), 'CX 5H');
  assert.match(pill(w).getAttribute('title'), /^Codex 5-hour window: 42% used/);
  paint(w, cx({ primary: win(10, 4320) }));
  assert.equal(text(pill(w).pl), 'CX 3D');
  assert.match(pill(w).getAttribute('title'), /^Codex 3-day window: /);
  paint(w, cx({ primary: win(10, 90, NOW + 600) }));
  assert.equal(text(pill(w).pl), 'CX 90m');
  assert.match(pill(w).getAttribute('title'), /^Codex 90-minute window: /);
});

test('with a 5-hour and a weekly window the pill shows the fullest by percentage, whichever of primary / secondary it is; a tie goes to the longer', () => {
  const w = sWorld();
  paint(w, cx({ primary: win(80, 300, NOW + 3600), secondary: win(23, 10080) }));
  assert.equal(text(pill(w).pl), 'CX 5H');
  assert.equal(text(pill(w).pv), '80%');
  paint(w, cx({ primary: win(23, 10080), secondary: win(80, 300, NOW + 3600) }));
  assert.equal(text(pill(w).pl), 'CX 5H');
  assert.equal(text(pill(w).pv), '80%');
  paint(w, cx({ primary: win(10, 300, NOW + 3600), secondary: win(40, 10080) }));
  assert.equal(text(pill(w).pl), 'CX 7D');
  assert.equal(text(pill(w).pv), '40%');
  paint(w, cx({ primary: win(30, 300, NOW + 3600), secondary: win(30, 10080) }));
  assert.equal(text(pill(w).pl), 'CX 7D', 'equal percentages: the longer window');
  paint(w, cx({ primary: win(90, 300, NOW - 60), secondary: win(20, 10080) }));
  assert.equal(text(pill(w).pl), 'CX 7D', 'a 5-hour window whose reset has passed counts as 0 %');
});

test('a reached 5-hour window shows CX 5H, red, with the glyph; it is never shown as the weekly percentage (#32 e)', () => {
  const w = sWorld();
  paint(w, cx({ primary: win(100, 300, NOW + 1800), secondary: win(41, 10080), reached: true }));
  assert.equal(text(pill(w).pl), 'CX 5H');
  assert.equal(text(pill(w).pv), '100%');
  assert.ok(pill(w).classList.contains('bad') && pill(w).classList.contains('reached'));
  assert.equal(pill(w).gl.classList.contains('hidden'), false, 'the glyph says limit reached, not red alone');
  assert.match(pill(w).getAttribute('title'), /^Codex 5-hour window: 100% used .*limit reached$/);
  paint(w, cx({ primary: win(60, 300, NOW + 1800), secondary: win(100, 10080), reached: true }));
  assert.equal(text(pill(w).pl), 'CX 7D');
  paint(w, cx({ primary: win(100, 300, NOW + 1800), secondary: win(100, 10080), reached: true }));
  assert.equal(text(pill(w).pl), 'CX 7D', 'both windows reached: the longer one blocks longer');
  paint(w, cx({ primary: win(99, 300, NOW + 1800), secondary: win(41, 10080), reached: true }));
  assert.equal(text(pill(w).pl), 'CX 5H', 'reached with no window at 100 %: the fullest');
  paint(w, cx({ primary: win(30, 10080) }));
  assert.equal(pill(w).classList.contains('reached'), false);
  assert.ok(pill(w).gl.classList.contains('hidden'));
});

test('the compact shell hides the CX pill only while no limit is reached', () => {
  const css = fs.readFileSync(path.join(STATIC, 'shell.css'), 'utf8');
  assert.match(css, /body\[data-shell=compact\] #pills \[data-pill=codex\]:not\(\.reached\) \{ display:none; \}/);
  assert.doesNotMatch(css, /\[data-pill=codex\] \{ display:none; \}/, 'no unconditional hide of the CX pill');
  assert.match(css, /\.pill \.pg\.hidden \{ display:none; \}/);
});

test('no reading, a null record or a window without a number: the pill stays hidden and nothing throws', () => {
  const w = sWorld();
  for (const st of [{}, { usage_codex: null }, { usage_codex: { value: null, at: null } }, cx({ primary: null }), cx({ primary: { window_minutes: 10080 } })]) {
    paint(w, st);
    assert.equal(shown(pill(w)), false, JSON.stringify(st));
  }
  paint(w, cx({ primary: win(17, 10080) }));
  assert.ok(shown(pill(w)));
  paint(w, {});
  assert.equal(shown(pill(w)), false, 'a reading that goes away hides the pill again');
});

test('the tone follows the percentage; a reached limit is red whatever the percentage says', () => {
  const w = sWorld();
  const tone = () => ['ok', 'warn', 'bad'].filter((c) => pill(w).classList.contains(c));
  paint(w, cx({ primary: win(30, 10080) }));
  assert.deepEqual(tone(), ['ok']);
  paint(w, cx({ primary: win(70, 10080) }));
  assert.deepEqual(tone(), ['warn']);
  paint(w, cx({ primary: win(90, 10080) }));
  assert.deepEqual(tone(), ['bad']);
  paint(w, cx({ primary: win(30, 10080), reached: true }));
  assert.deepEqual(tone(), ['bad']);
  assert.match(pill(w).getAttribute('title'), /limit reached$/);
});

test('a window that rolled over since the reading shows 0 %, says so and no countdown', () => {
  const w = sWorld();
  paint(w, cx({ primary: win(64, 10080, NOW - 3600) }));
  assert.equal(text(pill(w).pv), '0%');
  assert.equal(text(pill(w).pr), '');
  assert.match(pill(w).getAttribute('title'), /the window rolled over since the last reading/);
  assert.ok(pill(w).classList.contains('ok'));
});

test('with several Codex accounts the title names the one the reading belongs to (the account in use when it names none); one account adds no name', () => {
  const w = sWorld();
  paint(w, { ...cx({ primary: win(17, 10080) }), ...accts(K1) });
  assert.match(pill(w).getAttribute('title'), /^Codex main · Codex weekly window: 17% used/);
  paint(w, { ...cx({ primary: win(17, 10080), account: K2 }), ...accts(K1) });
  assert.match(pill(w).getAttribute('title'), /^Codex work · Codex weekly window/, 'the reading names its account');
  paint(w, { ...cx({ primary: win(17, 10080) }), codex_accounts: { current: K1, list: [{ key: K1, label: 'Codex main', current: true }] } });
  assert.match(pill(w).getAttribute('title'), /^Codex weekly window/);
});

test('a board without pages/agents.js (no account helpers) still paints the pill', () => {
  const w = sWorld({ agents: false });
  paint(w, { ...cx({ primary: win(17, 10080) }), ...accts(K1) });
  assert.equal(text(pill(w).pl), 'CX 7D');
  assert.match(pill(w).getAttribute('title'), /^Codex weekly window/);
});

test('Shell.codexWindow still takes a bare window, a list and an object of windows', () => {
  const w = sWorld();
  const run = (v) => JSON.parse(JSON.stringify(w.run(`Shell.codexWindow(${JSON.stringify(v)})`)));
  assert.equal(run({ used_percent: 5, window_minutes: 300 }).minutes, 300);
  assert.equal(run([{ used_percent: 5, window_minutes: 300 }, { used_percentage: 9, window_minutes: 10080 }]).minutes, 10080);
  assert.equal(run({ five: { used_percent: 5, window_minutes: 300 }, week: { used_percent: 9, window_minutes: 10080 } }).used_percentage, 9);
  assert.equal(run(null), null);
});
