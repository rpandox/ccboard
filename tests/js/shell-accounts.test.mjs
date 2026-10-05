// Contract tests for the v0.5.17b account chip of the topbar (shell.js: Shell.accountChip / patchAccount / patchUsage): the chip appears before the 5H / 7D pills only
// when the board has more than one subscription account, wears the account's hue (chipHue('account', key), pages/agents.js), names the account in the pills' titles, and
// turns amber ('<label> has N % left') when the current account is at 85 % or more and another one has more room. Real core.js, components.js, launcher.js, router.js,
// widgets.js, pages/agents.js and shell.js on minidom's DOM; the topbar is built by Shell.buildTopbar and painted by Shell.patchUsage.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { makeWorld, plain } from './harness.mjs';
import { installDom } from './minidom.mjs';

const NOW = Math.floor(Date.now() / 1000);
const A1 = '7d3e1c52-9a41-4b6f-8a0e-5c2f19d8a001', A2 = '7d3e1c52-9a41-4b6f-8a0e-5c2f19d8a002', A3 = '7d3e1c52-9a41-4b6f-8a0e-5c2f19d8a003';
const acct = (key, over = {}) => ({ key, email: `${key.slice(-4)}@example.com`, name: null, label: null, plan: 'max', rl_5h: 10, rl_7d: 20, resets_5h: NOW + 7200, resets_7d: NOW + 200000, current: false, ...over });
const stateOf = (list, over = {}) => ({ accounts: { current: (list.find((a) => a.current) || {}).key || null, list }, ...over });
const usage = (p5, p7) => ({ value: { five_hour: { used_percentage: p5, resets_at: NOW + 7200 }, seven_day: { used_percentage: p7, resets_at: NOW + 200000 } }, at: new Date().toISOString() });
const text = (n) => (n ? n.textContent : '');
const POOL = ['hue-blue', 'hue-teal', 'hue-green', 'hue-violet', 'hue-slate', 'hue-amber'];

/** A world with the topbar built; `agents: false` leaves pages/agents.js out (the helpers the chip needs are then missing). */
function sWorld({ agents = true } = {}) {
  const w = makeWorld();
  installDom(w);
  for (const f of ['core.js', 'components.js', 'launcher.js', 'router.js', 'pages/widgets.js', ...(agents ? ['pages/agents.js'] : []), 'shell.js']) w.load(f);
  w.run('Shell.refs = {}; Shell.buildTopbar(document.querySelector("#topbar"))');
  return w;
}
const paint = (w, st) => { w.ctx.__st = st; w.run('state = __st; Shell.patchUsage(__st)'); };
const chip = (w) => w.document.querySelector('#topbar [data-pill=acct]');
const pill = (w, k) => w.document.querySelector(`#topbar [data-pill="${k}"]`);
const shown = (n) => !!n && !n.classList.contains('hidden');
const hues = (n) => n.className.split(/\s+/).filter((c) => c.startsWith('hue-'));
const hueOf = (w, key) => w.run(`chipHue('account', ${JSON.stringify(key)})`);

test('the chip is built into #pills before 5H, hidden, and links to the Usage page', () => {
  const w = sWorld();
  const c = chip(w);
  assert.ok(c);
  assert.equal(c.tagName, 'A');
  assert.equal(c.getAttribute('href'), '#/usage');
  assert.ok(c.classList.contains('pill') && c.classList.contains('acct') && c.classList.contains('hidden'));
  const kids = w.document.querySelector('#pills').children.map((n) => n.getAttribute('data-pill'));
  assert.deepEqual(kids, ['acct', '5h', '7d', 'codex', 'spend']);
});

test('one account, none, or a state without the key: no chip, the pills and their titles are as before', () => {
  const w = sWorld();
  for (const st of [{ usage: usage(42, 71) }, { usage: usage(42, 71), accounts: null }, { usage: usage(42, 71), accounts: { current: null, list: [] } },
    { usage: usage(42, 71), ...stateOf([acct(A1, { current: true })]) }]) {
    paint(w, st);
    assert.equal(shown(chip(w)), false, JSON.stringify(st.accounts));
    assert.match(pill(w, '5h').getAttribute('title'), /^5-hour window: 42% used · resets in /);
    assert.match(pill(w, '7d').getAttribute('title'), /^weekly window: 71% used · resets in /);
  }
});

test('two accounts: the chip shows the first two letters of the label, in the account\'s hue; the pill titles name the account; the numbers stay the current account\'s', () => {
  const w = sWorld();
  paint(w, { usage: usage(42, 71), ...stateOf([acct(A1, { current: true, name: 'Demo', label: 'Personal' }), acct(A2, { label: 'Work', plan: 'pro' })]) });
  const c = chip(w);
  assert.ok(shown(c));
  assert.equal(text(c), 'Pe');
  assert.deepEqual(hues(c), [hueOf(w, A1)], 'exactly the hue of this key');
  assert.equal(c.getAttribute('title'), 'Account: Personal (max) · usage per account');
  assert.equal(c.getAttribute('aria-label'), c.getAttribute('title'));
  assert.match(pill(w, '5h').getAttribute('title'), /^Personal · 5-hour window: 42% used · resets in /);
  assert.match(pill(w, '7d').getAttribute('title'), /^Personal · weekly window: 71% used · resets in /);
  assert.equal(text(pill(w, '5h').pv), '42%');
  assert.equal(text(pill(w, '7d').pv), '71%');
});

test('the letters come from the label, else the name, else the email, else the key; an emoji is not cut in half', () => {
  const w = sWorld();
  const letters = (over) => { paint(w, { usage: usage(10, 10), ...stateOf([acct(A1, { current: true, name: null, email: null, ...over }), acct(A2, { label: 'Work' })]) }); return text(chip(w)); };
  assert.equal(letters({ label: 'work', name: 'Roshan' }), 'Wo');
  assert.equal(letters({ name: 'roshan' }), 'Ro');
  assert.equal(letters({ email: 'zed@example.com' }), 'Ze');
  assert.equal(letters({}), '7d', 'the first characters of the key');
  assert.equal(letters({ label: '\u{1F642}Home' }), '\u{1F642}H');
  assert.equal(letters({ label: ' a b' }), 'Ab', 'spaces are skipped');
  assert.ok(!/undefined|null|NaN/.test(text(chip(w))));
});

test('a rename or a /login switch repaints the chip on the next patch, with its hue following the key', () => {
  const w = sWorld();
  const list = [acct(A1, { current: true, name: 'Demo' }), acct(A2, { label: 'Work' })];
  paint(w, { usage: usage(10, 10), ...stateOf(list) });
  assert.equal(text(chip(w)), 'De');
  const hue1 = hues(chip(w));
  list[0].label = 'Zed';
  paint(w, { usage: usage(10, 10), ...stateOf(list) });
  assert.equal(text(chip(w)), 'Zed'.slice(0, 2));
  assert.deepEqual(hues(chip(w)), hue1, 'same key, same hue');
  list[0].current = false; list[1].current = true;
  paint(w, { usage: usage(10, 10), ...stateOf(list) });
  assert.equal(text(chip(w)), 'Wo');
  assert.deepEqual(hues(chip(w)), [hueOf(w, A2)]);
  assert.match(pill(w, '5h').getAttribute('title'), /^Work · /);
  list.pop();                                                       // the second account is gone: the chip goes with it
  paint(w, { usage: usage(10, 10), ...stateOf(list) });
  assert.equal(shown(chip(w)), false);
  assert.match(pill(w, '5h').getAttribute('title'), /^5-hour window/);
});

test('amber: the current account is at 85 % or more of a window and another has more of it left -> amber tint and "<label> has N % of the <window> window left"', () => {
  const w = sWorld();
  const other = (over) => acct(A2, { label: 'Work', rl_5h: 20, rl_7d: 30, ...over });
  const cur = acct(A1, { current: true, name: 'Demo', rl_5h: 90, rl_7d: 10 });
  paint(w, { usage: usage(90, 10), ...stateOf([cur, other()]) });
  assert.deepEqual(hues(chip(w)), ['hue-amber'], 'amber replaces the account\'s hue, it does not stack');
  assert.equal(chip(w).getAttribute('title'), 'Work has 80 % of the 5-hour window left', 'the title names the window, the same wording as the Usage page\'s most-room line');
  assert.equal(chip(w).getAttribute('aria-label'), 'Demo: Work has 80 % of the 5-hour window left', 'the spoken label keeps whose pills these are');
  assert.equal(text(chip(w)), 'De', 'still the current account\'s letters');
  assert.match(pill(w, '5h').getAttribute('title'), /^Demo · 5-hour window: 90% used/, 'the pills keep naming the account in use');
  // 7D at 85 % counts the same
  paint(w, { usage: usage(10, 85), ...stateOf([acct(A1, { current: true, name: 'Demo', rl_5h: 10, rl_7d: 85 }), other()]) });
  assert.deepEqual(hues(chip(w)), ['hue-amber']);
  assert.equal(chip(w).getAttribute('title'), 'Work has 70 % of the 7-day window left');
  // back under 85 %: its own hue again, normal title
  paint(w, { usage: usage(10, 84.9), ...stateOf([acct(A1, { current: true, name: 'Demo', rl_5h: 10, rl_7d: 84.9 }), other()]) });
  assert.deepEqual(hues(chip(w)), [hueOf(w, A1)]);
  assert.equal(chip(w).getAttribute('title'), 'Account: Demo (max) · usage per account');
});

test('amber needs real room elsewhere, window by window: not when the others are as full, not without readings, and the best of several is named', () => {
  const w = sWorld();
  const cur = (p) => acct(A1, { current: true, name: 'Demo', rl_5h: p, rl_7d: 10 });
  const at = (list, p = 95) => { paint(w, { usage: usage(p, 10), ...stateOf(list) }); return [hues(chip(w))[0] === 'hue-amber', chip(w).getAttribute('title')]; };
  assert.equal(at([cur(95), acct(A2, { label: 'Work', rl_5h: 97, rl_7d: 10 })])[0], false, 'the other is fuller');
  assert.equal(at([cur(95), acct(A2, { label: 'Work', rl_5h: 95, rl_7d: 10 })])[0], false, 'a tie is not more room (strictly more is needed)');
  assert.equal(at([cur(95), acct(A2, { label: 'Work', rl_5h: null, rl_7d: null })])[0], false, 'an account never read is not offered');
  assert.deepEqual(at([cur(95), acct(A2, { label: 'Work', rl_5h: 10, rl_7d: 99 })]), [true, 'Work has 90 % of the 5-hour window left'],
    'the rule is per window (Usage.room): the other account\'s weekly window does not matter for the 5-hour check');
  const best = at([cur(95), acct(A2, { label: 'Work', rl_5h: 50, rl_7d: 10 }), acct(A3, { label: 'Spare', rl_5h: 5, rl_7d: 40 })]);
  assert.deepEqual(best, [true, 'Spare has 95 % of the 5-hour window left']);
  // a window that reset since the other account's last reading counts as empty
  const reset = at([cur(95), acct(A2, { label: 'Work', rl_5h: 100, resets_5h: NOW - 60, rl_7d: 10 })]);
  assert.deepEqual(reset, [true, 'Work has 100 % of the 5-hour window left']);
  // below 85 % nothing is amber however empty the others are
  assert.equal(at([cur(60), acct(A2, { label: 'Work', rl_5h: 0, rl_7d: 0 })], 60)[0], false);
});

test('the chip title follows the Usage.room rule: the 7-day window is checked first, then the 5-hour one, and the first window that trips names itself', () => {
  const w = sWorld();
  const run = (cur, other) => { paint(w, { usage: usage(cur.rl_5h, cur.rl_7d), ...stateOf([acct(A1, { current: true, name: 'Demo', ...cur }), acct(A2, { label: 'Work', ...other })]) }); return [hues(chip(w))[0] === 'hue-amber', chip(w).getAttribute('title')]; };
  assert.deepEqual(run({ rl_5h: 95, rl_7d: 90 }, { rl_5h: 5, rl_7d: 40 }), [true, 'Work has 60 % of the 7-day window left'], 'both are hot: the 7-day window decides, though the 5-hour one has more room');
  assert.deepEqual(run({ rl_5h: 95, rl_7d: 90 }, { rl_5h: 5, rl_7d: 95 }), [true, 'Work has 95 % of the 5-hour window left'], 'the 7-day window offers nothing better: on to the 5-hour one');
  assert.deepEqual(run({ rl_5h: 20, rl_7d: 90 }, { rl_5h: 0, rl_7d: 95 })[0], false, 'the 7-day window is hot, the other is fuller there, and the 5-hour one is nowhere near hot: no move to suggest');
  assert.deepEqual(run({ rl_5h: 20, rl_7d: 90 }, { rl_5h: 0, rl_7d: 50 }), [true, 'Work has 50 % of the 7-day window left']);
});

test('the topbar is marked .has-acct exactly while the chip is shown (the compact shell drops the brand text for it)', () => {
  const w = sWorld();
  const bar = () => w.document.querySelector('#topbar');
  assert.equal(bar().classList.contains('has-acct'), false, 'built without one');
  paint(w, { usage: usage(10, 10), ...stateOf([acct(A1, { current: true, name: 'Demo' }), acct(A2, { label: 'Work' })]) });
  assert.equal(bar().classList.contains('has-acct'), true);
  paint(w, { usage: usage(10, 10), ...stateOf([acct(A1, { current: true, name: 'Demo' })]) });
  assert.equal(bar().classList.contains('has-acct'), false, 'one account: the brand keeps its room');
  paint(w, { usage: usage(10, 10), accounts: { current: null, list: [acct(A1), acct(A2)] } });
  assert.equal(bar().classList.contains('has-acct'), false, 'no current account: no chip, no class');
});

test('without a current account, or without pages/agents.js, the chip stays away and nothing throws', () => {
  const w = sWorld();
  paint(w, { usage: usage(42, 71), accounts: { current: null, list: [acct(A1), acct(A2)] } });
  assert.equal(shown(chip(w)), false, 'two accounts but none known to be in use');
  const bare = sWorld({ agents: false });
  assert.doesNotThrow(() => paint(bare, { usage: usage(42, 71), ...stateOf([acct(A1, { current: true }), acct(A2)]) }));
  assert.equal(shown(chip(bare)), false);
  assert.match(pill(bare, '5h').getAttribute('title'), /^5-hour window: 42% used/);
});

test('with no state.usage the current account\'s own readings stand in for the pills\' numbers in the amber check', () => {
  const w = sWorld();
  paint(w, stateOf([acct(A1, { current: true, name: 'Demo', rl_5h: 92, rl_7d: 10 }), acct(A2, { label: 'Work', rl_5h: 5, rl_7d: 5 })]));
  assert.deepEqual(hues(chip(w)), ['hue-amber']);
  assert.equal(chip(w).getAttribute('title'), 'Work has 95 % of the 5-hour window left');
});

test('Shell.accountChip is pure: it answers null for fewer than two accounts and never throws on odd shapes', () => {
  const w = sWorld();
  for (const st of [null, {}, { accounts: 3 }, { accounts: { list: [null, 7, {}] } }, { accounts: { list: [{ key: 'a' }, { key: 'b' }] } }]) {
    w.ctx.__st = st;
    assert.doesNotThrow(() => w.run('Shell.accountChip(__st)'), JSON.stringify(st));
  }
  w.ctx.__st = { accounts: { list: [{ key: 'a' }] } };
  assert.equal(w.run('Shell.accountChip(__st)'), null);
  w.ctx.__st = stateOf([acct(A1, { current: true, name: 'Demo' }), acct(A2)]);
  const c = plain(w.run('Shell.accountChip(__st)'));
  assert.deepEqual(Object.keys(c).sort(), ['amber', 'href', 'hue', 'key', 'name', 'text', 'title']);
  assert.ok(POOL.includes(c.hue));
});

test('css: the chip is a link as tall as the pills on touch, wears its hue, and the compact bar tightens the pill gap', async () => {
  const fs = await import('node:fs');
  const path = await import('node:path');
  const { STATIC } = await import('./harness.mjs');
  const css = fs.readFileSync(path.join(STATIC, 'shell.css'), 'utf8');
  assert.match(css, /#topbar a\.pill\.acct\s*\{[^}]*color:var\(--hue/);
  assert.match(css, /#topbar a\.pill\.acct\s*\{[^}]*background:var\(--hue-bg/);
  assert.match(css, /@media \(pointer:coarse\)\s*\{\s*#topbar a\.pill\s*\{\s*min-height:var\(--tap\)/, 'every #topbar a.pill, the chip included, is 44 px on coarse pointers');
  assert.match(css, /html\.force-coarse #topbar a\.pill\s*\{\s*min-height:var\(--tap\)/);
});

test('css: with the chip shown the compact shell drops the brand text so the + button stays inside a 390 px viewport; the chip itself is never hidden and is 44 px wide on touch', async () => {
  const fs = await import('node:fs');
  const path = await import('node:path');
  const { STATIC } = await import('./harness.mjs');
  const css = fs.readFileSync(path.join(STATIC, 'shell.css'), 'utf8');
  assert.match(css, /body\[data-shell=compact\] #topbar\.has-acct a\.brand\s*\{\s*display:none;\s*\}/, 'the brand goes, only in the compact shell and only with the chip');
  assert.ok(!/has-acct[^{]*a\.pill\.acct[^{]*\{[^}]*display:\s*none/.test(css), 'and the chip is not the thing that is hidden');
  assert.ok(!/has-acct[^{]*\.agent-dots[^{]*\{[^}]*display:\s*none/.test(css), 'nor are the agent dots (they carry the warn / bad state)');
  assert.match(css, /@media \(pointer:coarse\)\s*\{\s*#topbar a\.pill\.acct\s*\{[^}]*min-width:var\(--tap\)/);
  assert.match(css, /html\.force-coarse #topbar a\.pill\.acct\s*\{[^}]*min-width:var\(--tap\)/);
  const tabs = /\.tablist\s*\{([^}]*)\}/.exec(css);
  assert.ok(tabs && /flex-wrap:\s*nowrap/.test(tabs[1]) && /overflow-x:\s*auto/.test(tabs[1]), 'the tab bar is one row that scrolls sideways (six Settings tabs wrapped at 390 px)');
  assert.match(css, /\.bp5-dark \.tab\s*\{[^}]*flex:none;[^}]*white-space:nowrap/, 'and a tab never shrinks or wraps its label');
});

// ---------------------------------------------------------------- time-aware pills (v0.5.17f)

const ago = (s) => new Date((NOW - s) * 1000).toISOString();
const usageOf = (five, seven, at, source) => ({ value: { ...(five ? { five_hour: five } : {}), ...(seven ? { seven_day: seven } : {}), ...(source ? { source } : {}) }, at });

test('a window whose reset has passed with no newer reading keeps its pill: 0 %, counting down to the next reset, the title says nothing was recorded since and how old the reading is', () => {
  const w = sWorld();
  paint(w, { usage: usageOf({ used_percentage: 91, resets_at: NOW - 3600 }, { used_percentage: 71, resets_at: NOW + 200000 }, ago(3 * 3600)) });
  const p = pill(w, '5h');
  assert.ok(shown(p), 'never a missing pill');
  assert.equal(text(p.pv), '0%');
  assert.match(text(p.pr), /^[34]h\d+m$/, 'the next reset: the one that passed + a whole 5-hour window');
  assert.ok(p.classList.contains('ok') && !p.classList.contains('bad'), 'the old 91 % does not paint it red');
  assert.match(p.getAttribute('title'), /^5-hour window: 0% used · resets in [34]h\d+m · no usage recorded since the window reset · updated 3h ago · from the last session$/);
  assert.equal(text(pill(w, '7d').pv), '71%', 'the weekly window has not reset: its last reading stands');
  assert.match(pill(w, '7d').getAttribute('title'), /^weekly window: 71% used · resets in 2d\d+h · updated 3h ago · from the last session$/);
});

test('several missed windows: the next reset is the first one ahead, for the 5-hour and the weekly pill', () => {
  const w = sWorld();
  paint(w, { usage: usageOf({ used_percentage: 100, resets_at: NOW - 5 * 18000 - 100 }, { used_percentage: 100, resets_at: NOW - 3 * 604800 - 3600 }, ago(30 * 86400)) });
  assert.equal(text(pill(w, '5h').pv), '0%');
  assert.match(text(pill(w, '5h').pr), /^4h5\dm$/);
  assert.equal(text(pill(w, '7d').pv), '0%');
  assert.match(text(pill(w, '7d').pr), /^6d2[23]h$/);
  assert.match(pill(w, '7d').getAttribute('title'), /no usage recorded since the window reset · updated 30d ago · from the last session$/);
});

test('a reading not yet reset keeps its percentage and its own reset; the title ends with when it was read and where it came from (the last session, or Claude Code\'s cache)', () => {
  const w = sWorld();
  paint(w, { usage: usageOf({ used_percentage: 42, resets_at: NOW + 7200 }, { used_percentage: 71, resets_at: NOW + 200000 }, ago(300)) });
  assert.equal(text(pill(w, '5h').pv), '42%');
  assert.match(pill(w, '5h').getAttribute('title'), /^5-hour window: 42% used · resets in 1h5\dm · updated 5m ago · from the last session$|^5-hour window: 42% used · resets in 2h0m · updated 5m ago · from the last session$/);
  assert.doesNotMatch(pill(w, '5h').getAttribute('title'), /no usage recorded/);
  paint(w, { usage: usageOf({ used_percentage: 42, resets_at: NOW + 7200 }, { used_percentage: 71, resets_at: NOW + 200000 }, ago(120), 'cache') });
  assert.match(pill(w, '5h').getAttribute('title'), /· updated 2m ago · from Claude Code's cache$/);
  assert.match(pill(w, '7d').getAttribute('title'), /· updated 2m ago · from Claude Code's cache$/);
});

test('a window the server filled in from an older reading carries its own time and source into the title', () => {
  const w = sWorld();
  paint(w, { usage: usageOf({ used_percentage: 12, resets_at: NOW + 3600 }, { used_percentage: 64, resets_at: NOW - 600, at: ago(8 * 3600), source: 'cache' }, ago(60)) });
  assert.match(pill(w, '5h').getAttribute('title'), /updated 1m ago · from the last session$/);
  assert.equal(text(pill(w, '7d').pv), '0%');
  assert.match(pill(w, '7d').getAttribute('title'), /no usage recorded since the window reset · updated 8h ago · from Claude Code's cache$/);
});

test('an older record with no time or source adds no caption (the pills read as before), and a missing window still hides its pill', () => {
  const w = sWorld();
  paint(w, { usage: { value: { five_hour: { used_percentage: 42, resets_at: NOW + 3600 } } } });
  assert.match(pill(w, '5h').getAttribute('title'), /^5-hour window: 42% used · resets in \d+[hm]/);
  assert.doesNotMatch(pill(w, '5h').getAttribute('title'), /updated|no reading/);
  assert.equal(shown(pill(w, '7d')), false, 'no weekly window in the record at all: nothing to show');
  paint(w, {});
  assert.equal(shown(pill(w, '5h')), false);
});

test('the pills are time-aware without pages/agents.js too (the arithmetic is core.js\'s)', () => {
  const w = sWorld({ agents: false });
  paint(w, { usage: usageOf({ used_percentage: 91, resets_at: NOW - 3600 }, null, ago(7200)) });
  assert.equal(text(pill(w, '5h').pv), '0%');
  assert.match(pill(w, '5h').getAttribute('title'), /no usage recorded since the window reset · updated 2h ago/);
});

test('the account chip does not turn amber for a window that has reset since its 91 % reading', () => {
  const w = sWorld();
  const st = { usage: usageOf({ used_percentage: 91, resets_at: NOW - 3600 }, { used_percentage: 20, resets_at: NOW + 200000 }, ago(3 * 3600)),
    ...stateOf([acct(A1, { current: true, label: 'Personal', rl_5h: 91, resets_5h: NOW - 3600 }), acct(A2, { label: 'Work', rl_5h: 10 })]) };
  paint(w, st);
  assert.equal(chip(w).classList.contains('hue-amber'), false);
  assert.equal(chip(w).getAttribute('title'), 'Account: Personal (max) · usage per account');
  const hot = { usage: usageOf({ used_percentage: 91, resets_at: NOW + 3600 }, { used_percentage: 20, resets_at: NOW + 200000 }, ago(60)),
    ...stateOf([acct(A1, { current: true, label: 'Personal' }), acct(A2, { label: 'Work', rl_5h: 10 })]) };
  paint(w, hot);
  assert.ok(chip(w).classList.contains('hue-amber'), 'a window that is still running at 91 % does');
});
