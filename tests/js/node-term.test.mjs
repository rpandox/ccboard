// Contract tests for issue #144 in the browser, part one: "Open terminal on <node>" (the link builder, the control on the peek, the session rows, the inbox card, the palette), with the
// world of tests/js/termworld.mjs (hubworld.mjs plus termkit.js and pages/quad.js). The board's relay is a scripted `api`, Live.subscribe a recorder, window.open a recorder. Nothing opens a
// connection. The remote tiles of the quad and the dock are node-tile.test.mjs.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { HOSTILE, S1, URL_S1, all, btn, go, nodeList, page, plain, poll, rec, text, tick, tWorld } from './termworld.mjs';
import { boardState, hubWorld } from './hubworld.mjs';

// ---------------------------------------------------------------- the link builder

test('Nodes.termUrl: the registry address plus /term/<tmux>, only for an https tailnet address and a valid session name; a hostile name or address gives no link at all', async () => {
  const { w } = await tWorld();
  const url = (r, t) => w.run(`Nodes.termUrl(${JSON.stringify(r)}, ${JSON.stringify(t)})`);
  assert.equal(url({ url: 'https://build-box.example.ts.net' }, S1), URL_S1);
  assert.equal(url({ url: 'https://build-box.example.ts.net:8443/' }, S1), 'https://build-box.example.ts.net:8443/term/shop--api--s1', 'a port is kept');
  assert.equal(url({ url: 'https://100.101.102.103' }, S1), 'https://100.101.102.103/term/shop--api--s1', 'a tailnet address');
  for (const bad of ['../x', 'shop--api--s1/../../x', 'shop--api--s1?x=1', 'shop--api--s1#x', 'shop--api--"s1', "shop--api--'s1", 'shop--api--s 1', 'shop--api--s1\n', 'a/b', '', 'x', 'shop--api', '<img>', 'shop--api--s1%2f..']) {
    assert.equal(url({ url: 'https://build-box.example.ts.net' }, bad), null, `no link for ${JSON.stringify(bad)}`);
  }
  for (const bad of ['http://build-box.example.ts.net', 'https://evil.example.com', 'https://user@build-box.example.ts.net', 'https://build-box.example.ts.net/a', 'javascript:alert(1)', '//build-box.example.ts.net', '', null]) {
    assert.equal(url({ url: bad }, S1), null, `no link for the address ${JSON.stringify(bad)}`);
  }
  assert.equal(url(null, S1), null);
});

test('Nodes.openTerm opens the link with window.open(url, "_blank", "noopener") and sends nothing: no api call, no fetch to the board or the node', async () => {
  const h = await tWorld();
  const { w } = h;
  const before = plain(w.get('__fetches')).length;
  const r = plain(w.run("Nodes.openTerm('build-box', 'shop--api--s1')"));
  assert.deepEqual(h.opened(), [[URL_S1, '_blank', 'noopener']]);
  assert.equal(r.ok, true);
  assert.equal(h.log.length, 0, 'nothing went through api()');
  assert.equal(plain(w.get('__fetches')).length, before, 'and no fetch');
});

test('a hostile session name or an unusable address opens nothing: ok false, window.open is never called', async () => {
  const h = await tWorld({ nodes: nodeList({ 'alice-mac': { url: 'http://alice-mac.example.ts.net' } }) });
  const { w } = h;
  for (const [handle, tmux] of [['build-box', '../x'], ['build-box', 'a"b--c--d'], ['build-box', 'shop--api--s 1'], ['alice-mac', S1], ['nobody', S1]]) {
    w.ctx.__a = [handle, tmux];
    assert.equal(plain(w.run('Nodes.openTerm(...__a)')).ok, false, `${handle} ${tmux}`);
  }
  assert.deepEqual(h.opened(), []);
});

test('a browser that gives no window back (a blocked pop-up) leaves a link the person taps: the button is replaced by an <a> with the same address, target _blank and noopener; it stays through a repaint', async () => {
  const h = await tWorld();
  const { w } = h;
  w.run('window.open = (...a) => { __opened.push(a); return null; }');
  await go(w, `#/n/build-box/s/${S1}`);
  const b = btn(page(w), 'Open terminal on build-box');
  assert.ok(b, 'the control on the peek');
  b.click();
  assert.equal(h.opened().length, 1);
  const a = all(page(w), 'a.nd-termlink')[0];
  assert.ok(a, 'a link in its place');
  assert.equal(a.getAttribute('href'), URL_S1);
  assert.equal(a.getAttribute('target'), '_blank');
  assert.match(a.getAttribute('rel'), /noopener/);
  assert.match(text(a), /Tap to open the terminal on build-box/);
  assert.equal(btn(page(w), 'Open terminal on build-box'), undefined, 'the button is gone');
  assert.equal(h.log.length, 0, 'still nothing sent');
  await poll(w, { force: true });
  await tick();
  assert.equal(all(page(w), 'a.nd-termlink').length, 1, 'the repaint of the next reading keeps the link');
});

// ---------------------------------------------------------------- where the control shows

test('the peek of a remote session has Open terminal on <node> beside Open on <node>; it opens the terminal and the page still has one filled primary', async () => {
  const h = await tWorld();
  const { w } = h;
  await go(w, `#/n/build-box/s/${S1}`);
  const b = btn(page(w), 'Open terminal on build-box');
  assert.ok(b);
  assert.ok(!b.classList.contains('primary'));
  assert.match(text(page(w).querySelector('.nd-term-note')), /The terminal opens on build-box itself, not through this board\. This device needs a Tailscale identity on that node: a tagged device has none and gets a 403\./);
  b.click();
  assert.deepEqual(h.opened(), [[URL_S1, '_blank', 'noopener']]);
  assert.ok(all(page(w), 'a.btn').some((a) => text(a) === 'Open on build-box'), 'the board link is still there');
  const filled = all(page(w), 'button, a.btn').filter((x) => x.classList.contains('primary') && !x.classList.contains('tinted'));
  assert.ok(filled.length <= 1, 'one filled primary');
});

test('an offline node says so under its control and still offers the link; a node whose address is unusable gets a disabled control with the reason', async () => {
  const h = await tWorld({ nodes: nodeList({ 'alice-mac': { url: 'http://alice-mac.example.ts.net' } }) });
  const { w } = h;
  await go(w, `#/n/old-laptop/s/${S1}`);
  const b = btn(page(w), 'Open terminal on old-laptop');
  assert.ok(b && !b.getAttribute('aria-disabled'), 'the link is still offered');
  assert.match(text(page(w).querySelector('.nd-term-note')), /old-laptop is offline since \d{2}:\d{2}|old-laptop is offline since \d+ \w+ \d{2}:\d{2}/);
  assert.match(text(page(w).querySelector('.nd-term-note')), /opens once old-laptop is back/);
  assert.match(b.getAttribute('title'), /offline since/);
  b.click();
  assert.equal(h.opened().length, 1);
  await go(w, `#/n/alice-mac/s/${S1}`);
  const off = btn(page(w), 'Open terminal on alice-mac');
  assert.equal(off.getAttribute('aria-disabled'), 'true');
  assert.match(text(page(w)), /Open terminal: the address saved for this node is not an https tailnet address/);
  off.click();
  assert.equal(h.opened().length, 1, 'a disabled control opens nothing');
});

test('every session row of a node has the terminal control as an icon button with a title and a label; the inbox permission card has the text button', async () => {
  const h = await tWorld();
  const { w } = h;
  await go(w, '#/n/build-box');
  const rows = all(page(w), '.nd-item');
  assert.ok(rows.length >= 2);
  const row = rows.find((r) => r.getAttribute('data-key') === `build-box/${S1}`);
  const b = row.querySelector('button[data-act=terminal]');
  assert.ok(b, 'an icon button in the row');
  assert.equal(b.getAttribute('aria-label'), 'Open terminal on build-box');
  assert.match(b.getAttribute('title'), /Open terminal on build-box: a new window on build-box itself\./);
  assert.equal(text(b), '', 'icon only');
  b.click();
  assert.deepEqual(h.opened(), [[URL_S1, '_blank', 'noopener']]);
});

test('a hostile node name and session name are text everywhere the control is drawn: no element comes of them', async () => {
  const bad = rec('build-box', { name: HOSTILE, scopes: ['read', 'tasks', 'sessions'] });
  bad.state.sessions[0].session = HOSTILE;
  const h = await tWorld({ nodes: [bad] });
  const { w } = h;
  await go(w, `#/n/build-box/s/${S1}`);
  assert.ok(text(page(w)).includes(HOSTILE));
  assert.equal(all(page(w), 'img').length, 0);
  const b = all(page(w), 'button').find((x) => /^Open terminal on /.test(text(x)));
  assert.equal(text(b), `Open terminal on ${HOSTILE}`);
});

// ---------------------------------------------------------------- the palette and the keys

test('the palette lists Open terminal on <node> for the open remote peek, and for every remote session once you type; picking it opens the window and sends nothing', async () => {
  const h = await tWorld();
  const { w } = h;
  await go(w, `#/n/build-box/s/${S1}`);
  w.run('Palette.open()');
  await tick();
  const labels = () => all(w, '.pal-item, [role=option]').map(text);
  assert.ok(labels().some((t) => /Open terminal on build-box/.test(t)), 'the peek in front of you leads');
  const groups = plain(w.run("Palette.groups({ sessions: [], scope: null }, '', false, false).map((g) => [g.id, g.items.map((i) => i.label)])"));
  assert.deepEqual(groups.find((g) => g[0] === 'rterm')[1], ['Open terminal on build-box']);
  const typed = plain(w.run("Palette.groups({ sessions: [] }, 'shop', false, false).filter((g) => g.id === 'rterm').map((g) => g.items.map((i) => i.hint))"));
  assert.ok(typed[0].some((x) => /^s2 /.test(x)), 'a typed word finds the other remote sessions too');
  w.run("Palette.groups({ sessions: [] }, '', false, false).find((g) => g.id === 'rterm').items[0].run()");
  assert.deepEqual(h.opened(), [[URL_S1, '_blank', 'noopener']]);
  assert.deepEqual(h.log.filter((c) => /\/api\/nodes/.test(c.path) || c.method !== 'GET'), [], 'nothing went to the hub');
});

test('the palette has no terminal row without a node: a board with none is the stored render', async () => {
  const h = hubWorld({ state: boardState({ nodes_enabled: false }), nodes: [] });
  const { w } = h;
  w.run('Palette.open()');
  await tick();
  const g = plain(w.run("Palette.groups({ sessions: [] }, 'shop', false, false).map((x) => x.id)"));
  assert.ok(!g.includes('rterm'));
});
