// #84: the per-session Auto-continue switch on the page side. core.js setAutoContinue (the one writer: optimistic, settled by the answer's read-back, put
// back with the server's words on a refusal), the row menu item and the "no auto-continue" chip of a rich session row (pages/agents.js sessionCard, which
// Agents, Home and the project page all use), and the read-only board-wide line in Settings > Notifications. The tile menu item and the Tune toggle are in
// termkit.test.mjs. Real scripts on minidom; api() is the recorder from world.mjs, answered here by a gate so the in-flight state can be looked at.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { calls, fakeState, homeWorld, page, plain, projectsOf, rowMenu, rowMenuLabels, sess, text, tick } from './world.mjs';

const shown = (n) => !!n && !n.classList.contains('hidden');
const chip = (row) => row.querySelector('.bdg-noauto');
const flagCalls = (w) => calls(w).filter((c) => c.path.endsWith('/flags'));

/** A rich row for `s` in a world whose POST /flags is held until release(answer) or fail(status, message). */
function rowWorld(s, { config } = {}) {
  const { w } = homeWorld();
  const full = { ...s, project: 'shop', repo: 'api' };
  w.ctx.__s = full;
  w.ctx.__stx = fakeState({ projects: projectsOf({ shop: { api: [s] } }), ...(config ? { config: { ...fakeState().config, ...config } } : {}) });
  w.run(`
    state = __stx;
    globalThis.__row = sessionCard(__s, { rich: true });
    __row.ccPatch(__s);
    globalThis.__held = null;
    __answers['/api/sessions/'] = () => new Promise((res, rej) => { __held = { res, rej }; });
    globalThis.__err = (status, message) => { const e = new Error(message); e.status = status; return e; };
  `);
  return { w, row: w.get('__row') };
}
const release = async (w, answer) => { w.run('__held.res(' + JSON.stringify(answer) + ')'); await tick(); await tick(); };
const refuse = async (w, status, message) => { w.run(`__held.rej(__err(${status}, ${JSON.stringify(message)}))`); await tick(); await tick(); };

test('the chip shows only while the flag is off, with a glyph and words, and goes with the next poll', () => {
  const { w, row } = rowWorld(sess('shop', 'api', 's1'));
  assert.equal(shown(chip(row)), false, 'on: no chip');
  w.ctx.__s2 = { ...sess('shop', 'api', 's1'), project: 'shop', repo: 'api', flags: { no_autoresume: true } };
  w.run('__row.ccPatch(__s2)');
  assert.equal(shown(chip(row)), true);
  assert.equal(text(chip(row)).trim(), '⊘ no auto-continue');
  assert.match(chip(row).getAttribute('title'), /will not type continue after a limit reset or a reboot/);
  w.ctx.__s3 = { ...sess('shop', 'api', 's1'), project: 'shop', repo: 'api', flags: {} };
  w.run('__row.ccPatch(__s3)');
  assert.equal(shown(chip(row)), false, 'switched on elsewhere: the chip goes without a reload');
});

test('a shell row and an ended row have no switch and no chip', () => {
  const sh = rowWorld(sess('shop', 'api', 'sh', { agent: 'shell', launcher: 'shell', flags: { no_autoresume: true } }));
  assert.equal(shown(chip(sh.row)), false);
  assert.equal(rowMenuLabels(sh.w, sh.row).some((l) => l.startsWith('Auto-continue')), false);
  const ended = rowWorld(sess('shop', 'api', 'e1', { state: 'ended', flags: { no_autoresume: true } }));
  assert.equal(shown(chip(ended.row)), false);
  assert.equal(rowMenuLabels(ended.w, ended.row).some((l) => l.startsWith('Auto-continue')), false);
});

test('the menu item names the state, flips it at once (chip and label) and POSTs {no_autoresume}; the answer\'s read-back settles it', async () => {
  const { w, row } = rowWorld(sess('shop', 'api', 's1'));
  assert.ok(rowMenuLabels(w, row).includes('Auto-continue: on'));
  assert.equal(rowMenu(w, row, 'Auto-continue: on'), true);
  assert.equal(shown(chip(row)), true, 'optimistic: the chip is up before the board has answered');
  assert.ok(rowMenuLabels(w, row).includes('Auto-continue: off'), 'and the menu already says off');
  assert.deepEqual(flagCalls(w).map((c) => [c.method, c.path, c.body]), [['POST', '/api/sessions/shop--api--s1/flags', { no_autoresume: true }]]);
  await release(w, { ok: true, flags: { no_autoresume: true } });
  assert.equal(shown(chip(row)), true, 'applied: the read-back shows the flag');
  assert.equal(plain(w.get('__toasts')).length, 0);
  // and back on
  assert.equal(rowMenu(w, row, 'Auto-continue: off'), true);
  assert.equal(shown(chip(row)), false);
  assert.deepEqual(flagCalls(w).map((c) => c.body), [{ no_autoresume: true }, { no_autoresume: false }]);
  await release(w, { ok: true, flags: { no_autoresume: false } });
  assert.equal(shown(chip(row)), false);
});

test('a refusal (4xx) puts the old value back and says why; an answer that does not show the flag does too', async () => {
  const { w, row } = rowWorld(sess('shop', 'api', 's1'));
  rowMenu(w, row, 'Auto-continue: on');
  assert.equal(shown(chip(row)), true);
  await refuse(w, 400, 'internal sessions have no switches');
  assert.equal(shown(chip(row)), false, 'reverted');
  assert.ok(rowMenuLabels(w, row).includes('Auto-continue: on'));
  assert.deepEqual(plain(w.get('__toasts')), [{ text: 'Auto-continue not changed: internal sessions have no switches', kind: 'bad' }]);
  rowMenu(w, row, 'Auto-continue: on');
  await release(w, { ok: true, flags: { no_autoresume: false } });          // the row read back says the flag is not there
  assert.equal(shown(chip(row)), false, 'not Applied: the read-back does not show it');
});

test('a second tap while one write is in flight does nothing', async () => {
  const { w, row } = rowWorld(sess('shop', 'api', 's1'));
  rowMenu(w, row, 'Auto-continue: on');
  rowMenu(w, row, 'Auto-continue: off');
  assert.equal(flagCalls(w).length, 1);
  await release(w, { ok: true, flags: { no_autoresume: true } });
  assert.equal(shown(chip(row)), true);
});

test('the item says what it covers, and when the board setting is off it says so instead of promising an effect', () => {
  const on = rowWorld(sess('shop', 'api', 's1'));
  on.w.run('__row.querySelector(".rr-more").click()');
  const it = on.w.document.querySelectorAll('.menuitem').find((i) => /^Auto-continue/.test(text(i.querySelector('.mi-name') || i)));
  assert.equal(text(it.querySelector('.mi-sub')), 'after a limit reset or a reboot, this session only');
  assert.match(it.getAttribute('title'), /Types continue once after a limit reset or an account switch, and after a reboot if the session was working\. This session only\./);
  assert.doesNotMatch(it.getAttribute('title'), /CCBOARD_AUTO_CONTINUE/);
  on.w.run('__row.querySelector(".rr-more").click()');
  const off = rowWorld(sess('shop', 'api', 's1'), { config: { auto_continue: false } });
  off.w.run('__row.querySelector(".rr-more").click()');
  const it2 = off.w.document.querySelectorAll('.menuitem').find((i) => /^Auto-continue/.test(text(i.querySelector('.mi-name') || i)));
  assert.match(text(it2.querySelector('.mi-sub')), /off for the whole board \(CCBOARD_AUTO_CONTINUE=0\)/);
  assert.match(it2.getAttribute('title'), /changes nothing until the board setting is on again/);
});

test('demo mode keeps the switch where the visitor left it (the fixture has no board to store it)', () => {
  const { w } = homeWorld();
  w.ctx.__st = fakeState({ projects: projectsOf({ shop: { api: [sess('shop', 'api', 's1'), sess('shop', 'api', 's2')], root: [sess('shop', 'root', 'r1')] } }) });
  w.run('demoMake("POST", "/api/sessions/shop--api--s1/flags", { no_autoresume: true }); demoMake("POST", "/api/sessions/shop--root--r1/flags", { no_autoresume: true }); globalThis.__out = demoMadeState(__st)');
  const flat = plain(w.get('__out')).projects[0];
  assert.equal(flat.repos[0].sessions[0].flags.no_autoresume, true);
  assert.equal(flat.repos[0].sessions[1].flags.no_autoresume, undefined, 'only the session that was switched');
  assert.equal(flat.root.sessions[0].flags.no_autoresume, true);
  w.run('demoMake("POST", "/api/sessions/shop--api--s1/flags", { no_autoresume: false }); globalThis.__out = demoMadeState(__st)');
  assert.equal(plain(w.get('__out')).projects[0].repos[0].sessions[0].flags.no_autoresume, undefined, 'switched back on');
});

test('Settings > Notifications shows the board-wide state read-only, with the key and how to change it', async () => {
  for (const [config, word, hint] of [[{}, 'on', /Set it to 0 and restart the board/], [{ auto_continue: false }, 'off', /Set it to 1/]]) {
    const { w } = homeWorld({ state: fakeState({ config: { ...fakeState().config, ...config } }) });
    w.ctx.__answers['/api/notify/prefs'] = { prefs: {}, samples: {} };
    w.run('demoOn = () => true;');
    w.location.hash = '#/settings';
    await tick();
    const panel = page(w).querySelector('.settings-panel[data-sec=notify]');
    const heads = panel.querySelectorAll('.set-h').map(text);
    assert.ok(heads.includes('Auto-continue'));
    const kv = panel.querySelectorAll('.kv').find((r) => text(r.querySelector('.k')) === 'Board-wide');
    assert.equal(text(kv.querySelector('.v')), word);
    assert.match(text(kv), /CCBOARD_AUTO_CONTINUE/);
    assert.match(text(kv), hint);
    assert.equal(kv.querySelectorAll('button, input').length, 0, 'read-only: no control');
  }
});
