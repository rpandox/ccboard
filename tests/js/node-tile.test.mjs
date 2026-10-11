// Contract tests for issue #144 in the browser, part two: a session of another node as a tail tile in the quad (the pick list grouped by node, the tile's chip, 12 lines of text, the prompt
// box that sends through the prompt relay row and shows every refusal sentence, the offline and re-pair states, no framed page anywhere, the o key, the store) and in the terminal dock
// (shell.js), with the world of tests/js/termworld.mjs. Live.subscribe is a recorder that hands the test the callback; window.open is a recorder. Nothing opens a connection.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC, makeWorld } from './harness.mjs';
import { HOSTILE, L1, L2, S1, S2, URL_S1, all, box, boardState, btn, dockWorld, enter, err, ev, flush, go, iso, line, nodeList, page, plain, poll, projectsOf, qrOf, sess, text, tick, tile, tWorld, type } from './termworld.mjs';

const Q = (extra = '') => `#/quad?l=4${extra}`;
const SLOTS = `&s=${L1},build-box/${S1},alice-mac/${S1},old-laptop/${S1}`;

// ---------------------------------------------------------------- the quad: the pick list

test('Quad.remoteGroups (pure): one group per node, only sessions that can show, the ones that need you first, the ones on screen and the ended left out, the project respected, four per node', () => {
  const w = makeWorld();
  w.load('core.js');
  w.load('router.js');
  w.load('pages/quad.js');
  const recs = [{ handle: 'a-box', name: 'A box', status: 'online' }, { handle: 'b-box', name: '', status: 'offline' }, { handle: 'c-box', status: 'online' }, { handle: '../x', status: 'online' }];
  const row = (node, tmux, over = {}) => ({ node, tmux, state: 'idle', project: tmux.split('--')[0], ...over });
  const rows = [row('a-box', 'shop--api--s1', { state: 'working' }), row('a-box', 'shop--api--s2', { needs_you: true, state: 'waiting' }), row('a-box', 'shop--api--s3', { state: 'ended' }), row('a-box', 'shop--api--s4'),
    row('a-box', 'bad name'), row('a-box', 'shop--api--s5'), row('a-box', 'shop--api--s6'), row('a-box', 'shop--api--s7'), row('b-box', 'notes--site--s1'), row('zzz', 'x--y--z'), row('../x', 'shop--api--s1')];
  w.ctx.__a = [recs, { sessions: rows, project: '', shown: ['a-box/shop--api--s4'] }];
  const g = plain(w.run('Quad.remoteGroups(...__a)'));
  assert.deepEqual(g.map((x) => [x.handle, x.name, x.status]), [['a-box', 'A box', 'online'], ['b-box', 'b-box', 'offline']], 'c-box has nothing; the bad handle and the unknown node have no group');
  assert.deepEqual(g[0].items.map((i) => i.key), ['a-box/shop--api--s2', 'a-box/shop--api--s1', 'a-box/shop--api--s5', 'a-box/shop--api--s6'], 'needs you, working, then the rest; ended, shown and invalid left out; four');
  w.ctx.__a = [recs, { sessions: rows, project: 'notes' }];
  assert.deepEqual(plain(w.run('Quad.remoteGroups(...__a)')).map((x) => x.handle), ['b-box'], 'a project quad offers that project only');
  assert.deepEqual(plain(w.run('Quad.remoteGroups(null, null)')), []);
});

test('an empty tile\'s pick list groups the sessions by node: This node first, then one group per paired node with its status word when it is not online; a pick fills the tile with the node\'s session', async () => {
  const h = await tWorld();
  const { w } = h;
  await go(w, `#/quad?l=2&s=${L1},${L2}`);
  tile(w, L2).querySelector('.qt-close').click();                          // an emptied tile (Close): this node's L2 is free again
  await tick();
  const empty = all(page(w), '.qempty')[0];
  const groups = all(empty, '.qe-group');
  assert.deepEqual(groups.map((g) => g.getAttribute('aria-label')), ['This node', 'On build-box', 'On alice-mac', 'On old-laptop']);
  assert.deepEqual(groups.slice(0, 3).map((g) => text(g.querySelector('.qe-gh'))), ['This node', 'build-box', 'alice-mac']);
  assert.match(text(groups[3].querySelector('.qe-gh')), /^old-laptopoffline since /, 'the offline node says since when');
  assert.deepEqual(all(groups[1], '.qe-pick').map((b) => b.getAttribute('data-tmux')), [`build-box/${S1}`, `build-box/${S2}`]);
  assert.deepEqual(all(groups[0], '.qe-pick').map((b) => b.getAttribute('data-tmux')), [L2], 'this node: what is not on screen, as a bare name');
  all(groups[1], '.qe-pick')[0].click();
  await tick();
  const t = tile(w, `build-box/${S1}`);
  assert.ok(t, 'the tile of the node\'s session');
  assert.equal(t.getAttribute('data-node'), 'build-box');
  assert.deepEqual(plain(JSON.parse(w.localStorage.getItem('ccboard:quad:all')).slots.slice(0, 2)), [L1, { node: 'build-box', tmux: S1 }], 'the store holds {node, tmux}; this board\'s stays a bare name');
});

test('with no paired node the pick list is the flat list it always was: no group, no header', async () => {
  const h = await tWorld({ state: boardState({ nodes_enabled: false }), nodes: [] });
  const { w } = h;
  await go(w, `#/quad?l=2&s=${L1},${L2}`);
  tile(w, L2).querySelector('.qt-close').click();
  await tick();
  const empty = all(page(w), '.qempty')[0];
  assert.equal(all(empty, '.qe-group').length, 0);
  assert.ok(all(empty, '.qe-pick').length >= 1);
  assert.equal(all(empty, '.qe-pick').filter((b) => b.getAttribute('data-node')).length, 0);
});

// ---------------------------------------------------------------- the quad: a tile of another node

test('a tile of another node: the node chip, the session and its state, the word Tail, a link to the terminal; its body is the tail and a prompt box, and not one iframe is ever created for it', async () => {
  const h = await tWorld();
  const { w } = h;
  await go(w, Q(SLOTS));
  const t = tile(w, `build-box/${S1}`);
  assert.ok(t);
  assert.equal(text(t.querySelector('.nchip-n')), 'build-box');
  assert.equal(text(t.querySelector('.qt-sess')), 's1');
  assert.equal(text(t.querySelector('.qt-tailword')), 'Tail');
  assert.ok(t.querySelector('.qt-glyphs .glyph'), 'a state glyph');
  assert.equal(t.getAttribute('data-state'), 'waiting');
  assert.equal(t.querySelector('iframe'), null, 'no framed page in a remote tile');
  assert.ok(qrOf(w, `build-box/${S1}`).querySelector('pre.qr-tail'));
  assert.equal(all(t, '.qt-mode, .qt-modes, .qt-modemenu').length, 0, 'no terminal view modes on a tail tile');
  assert.equal(t.getAttribute('data-mode'), 'tail');
  assert.deepEqual(h.subs().filter((s) => s.ref.includes('/')).map((s) => s.ref), [`build-box/${S1}`, `old-laptop/${S1}`], 'the stream of each node with the sessions scope; none for the read only node');
  // the local tile next to it is what it was: an iframe on this board's terminal
  assert.equal(tile(w, L1).querySelectorAll('iframe').length, 1);
  // not one iframe was created for a remote tile: the only one in the page is the local one, and the only iframe element ever made is that one
  assert.equal(all(page(w), 'iframe').length, 1);
  assert.equal(h.tags().filter((x) => x === 'iframe').length, 1, 'one iframe element created in all, the local tile\'s');
  const term = t.querySelector('[data-act=terminal]');
  assert.ok(term, 'the Open terminal control in the header');
  term.click();
  assert.deepEqual(h.opened(), [[URL_S1, '_blank', 'noopener']]);
  assert.equal(h.writes().length, 0);
});

test('the tail shows the last 12 lines as text, scrolled to the bottom, and nothing from a hostile line becomes an element', async () => {
  const h = await tWorld();
  const { w } = h;
  await go(w, Q(SLOTS));
  const pre = qrOf(w, `build-box/${S1}`).querySelector('pre');
  pre.scrollHeight = 4321;
  const lines = Array.from({ length: 20 }, (_, i) => `line ${i + 1}`);
  lines[19] = HOSTILE;
  h.feed(`build-box/${S1}`, lines);
  assert.equal(text(pre), lines.slice(8).join('\n'));
  assert.equal(text(pre).split('\n').length, 12);
  assert.equal(pre.scrollTop, 4321, 'scrolled to the bottom');
  assert.equal(pre.children.length, 0, 'text only');
  assert.ok(text(pre).includes(HOSTILE));
  assert.equal(pre.getAttribute('role'), 'log');
  assert.equal(line(tile(w, `build-box/${S1}`)), 'Live: updates as the session prints.');
});

test('the composer: Enter sends through the prompt relay row, "Sending to" first, then "Sent through the relay", the box emptied; Shift+Enter is refused and sends nothing', async () => {
  const h = await tWorld();
  const { w } = h;
  await go(w, Q(SLOTS));
  const t = tile(w, `build-box/${S1}`);
  const ta = box(t);
  type(ta, 'run the tests');
  let release;
  h.reply = (m, p) => (/\/prompt$/.test(p) ? new Promise((r) => { release = r; }) : undefined);
  const sh = enter(ta, { shiftKey: true });
  assert.equal(sh.prevented, 1, 'Shift+Enter is swallowed: this box shows one line');
  const alt = enter(ta, { altKey: true });
  assert.equal(alt.prevented, 1);
  const comp = enter(ta, { isComposing: true });
  assert.equal(comp.prevented, 0, 'an Enter that confirms an IME candidate is left alone');
  assert.equal(h.writes().length, 0, 'none of the three sent');
  assert.equal(ta.value, 'run the tests', 'and the draft is untouched');
  enter(ta);
  await flush();
  assert.deepEqual(h.writes().map((c) => [c.method, c.path, c.body]), [['POST', `/api/nodes/build-box/sessions/${S1}/prompt`, { text: 'run the tests', queue: false }]]);
  assert.equal(line(t), 'Sending to build-box…');
  assert.equal(ta.value, 'run the tests', 'the draft stays until the node confirmed');
  assert.equal(ta.disabled, true);
  release({ node: 'build-box', age: 0, data: { ok: true, pasted: true, queued: false } });
  await flush();
  assert.equal(line(t), 'Sent through the relay to build-box.');
  assert.equal(ta.value, '');
  assert.equal(ta.disabled, false);
  assert.ok(t.querySelector('.qr-st').classList.contains('ok'));
});

test('every refusal of the steer rules shows as its plain sentence in the tile and keeps the draft: scope, the session is asking a question, wider permissions, cannot start with / ! or #, busy, offline, unconfirmed', async () => {
  const typed = 'a remote prompt cannot start with / ! or # (those are commands in the terminal); start it from its own board';
  const wide = 'that session runs with wider permissions than another node may use, or its mode cannot be read; start it from its own board';
  const cases = [
    [err(409, { error: 'needs the sessions scope on build-box', reason: 'scope' }), 'needs the sessions scope on build-box'],
    [err(409, { error: 'this session is asking a question in its terminal: answer it with Allow or Deny, not with a key', reason: 'busy', code: 'asking', retry: null }), 'This session is asking a question in its terminal: answer it with Allow or Deny, not with a key. Waiting will not help.'],
    [err(409, { error: 'needs the permissions scope on build-box: this session is asking a question in its terminal', reason: 'scope' }), 'needs the permissions scope on build-box: this session is asking a question in its terminal'],
    [err(409, { error: wide, reason: 'refused' }), 'That session runs with wider permissions than another node may use, or its mode cannot be read; start it from its own board.'],
    [err(422, { error: typed, reason: 'invalid' }), 'A remote prompt cannot start with / ! or # (those are commands in the terminal); start it from its own board.'],
    [err(409, { error: 'the session is working', reason: 'busy', code: 'working', retry: 8 }), 'The session is working. Try again in 8 seconds.'],
    [err(503, { error: 'build-box is offline', reason: 'offline', age: 190 }), 'build-box is offline, last seen 3 minutes ago'],
    [err(504, { error: 'build-box did not answer in 8 s', reason: 'unconfirmed' }), 'Could not confirm whether build-box got this. Check the terminal before answering again.'],
    [Object.assign(new Error('Failed to fetch'), {}), 'Could not confirm whether build-box got this. Check the terminal before answering again.'],
  ];
  for (const [e, want] of cases) {
    const h = await tWorld();
    const { w } = h;
    await go(w, Q(SLOTS));
    const t = tile(w, `build-box/${S1}`);
    const ta = box(t);
    type(ta, 'try this');
    h.reply = (m, p) => { if (/\/prompt$/.test(p)) throw e; return undefined; };
    enter(ta);
    await flush();
    assert.equal(line(t), want, want);
    assert.equal(ta.value, 'try this', `the draft survives: ${want}`);
    assert.equal(ta.disabled, false, 'ready for another try');
    assert.equal(h.writes().length, 1, 'nothing was retried');
    assert.ok(/warn|bad/.test(t.querySelector('.qr-st').className), 'a refusal is not painted as a success');
  }
});

test('a slash command is refused before anything is sent, and a busy working session offers Queue it, which sends once more with queue true', async () => {
  const h = await tWorld();
  const { w } = h;
  await go(w, Q(SLOTS));
  const t = tile(w, `build-box/${S1}`);
  type(box(t), '/compact');
  enter(box(t));
  await flush();
  assert.match(line(t), /^Slash commands are not sent to another node\./);
  assert.equal(h.writes().length, 0);
  type(box(t), 'after the tests');
  h.reply = (m, p, b) => { if (/\/prompt$/.test(p) && !(b && b.queue)) throw err(409, { error: 'the session is working', reason: 'busy', code: 'working', retry: 8 }); return undefined; };
  enter(box(t));
  await flush();
  assert.equal(line(t), 'The session is working. Try again in 8 seconds.');
  const q = btn(t, 'Queue it');
  assert.ok(q && !q.classList.contains('hidden'));
  q.click();
  await flush();
  assert.deepEqual(h.writes().map((c) => c.body), [{ text: 'after the tests', queue: false }, { text: 'after the tests', queue: true }]);
  assert.equal(line(t), 'Queued on build-box. It is typed when the session is ready.');
});

test('a node without the sessions scope: the tile has no stream and no usable box, and says why in the status line and the placeholder', async () => {
  const h = await tWorld();
  const { w } = h;
  await go(w, Q(SLOTS));
  const t = tile(w, `alice-mac/${S1}`);
  assert.ok(t);
  assert.equal(box(t).disabled, true);
  assert.equal(box(t).getAttribute('placeholder'), 'needs the sessions scope on alice-mac');
  assert.equal(line(t), 'needs the sessions scope on alice-mac');
  assert.equal(h.subs().filter((s) => s.ref === `alice-mac/${S1}`).length, 0, 'no stream');
  enter(box(t));
  assert.equal(h.writes().length, 0);
  assert.ok(t.querySelector('[data-act=terminal]'), 'the terminal link is still offered');
});

test('an offline node keeps the last lines, says "offline since HH:MM", disables the composer and keeps the Open terminal link; a re-pair says "re-pair"', async () => {
  const h = await tWorld();
  const { w } = h;
  await go(w, Q(SLOTS));
  const key = `old-laptop/${S1}`;
  const t = tile(w, key);
  h.feed(key, ['kept one', 'kept two']);
  const word = plain(w.run("Nodes.view(Nodes.get('old-laptop')).word"));
  assert.match(word, /^offline since /);
  assert.equal(line(t), word);
  assert.match(line(t), /^offline since (\d{2}:\d{2}|\d+ \w+ \d{2}:\d{2})$/);
  assert.equal(text(qrOf(w, key).querySelector('pre')), 'kept one\nkept two', 'the last lines stay');
  assert.equal(box(t).disabled, true);
  assert.match(box(t).getAttribute('placeholder'), /old-laptop is offline/);
  const term = t.querySelector('[data-act=terminal]');
  assert.ok(term && !term.getAttribute('aria-disabled'));
  assert.match(term.getAttribute('title'), /old-laptop is offline since/);
  enter(box(t));
  assert.equal(h.writes().length, 0);
  // the node was revoked: the next reading says unauthorized
  w.ctx.__ans = JSON.stringify({ status: 200, body: { nodes: nodeList({ 'old-laptop': { status: 'unauthorized' } }), at: iso(0) }, etag: 'W/"9"' });
  await poll(w, { force: true });
  await tick();
  assert.equal(line(tile(w, key)), 're-pair');
  assert.equal(text(qrOf(w, key).querySelector('pre')), 'kept one\nkept two');
  assert.equal(box(tile(w, key)).disabled, true);
  assert.ok(tile(w, key).querySelector('[data-act=terminal]'), 'the link stays');
});

test('a stream that ended keeps the lines and says why; the next lines bring it back', async () => {
  const h = await tWorld();
  const { w } = h;
  await go(w, Q(SLOTS));
  const key = `build-box/${S1}`;
  h.feed(key, ['still here']);
  const at = new Date(2026, 9, 11, 9, 14).getTime();
  h.feed(key, ['still here'], { gone: { reason: 'upstream_error', message: '', final: false, seen: at } });
  assert.equal(line(tile(w, key)), 'build-box: could not be reached, last seen 09:14');
  assert.equal(text(qrOf(w, key).querySelector('pre')), 'still here');
  assert.ok(qrOf(w, key).classList.contains('gone'));
  h.feed(key, ['alive again']);
  assert.equal(line(tile(w, key)), 'Live: updates as the session prints.');
  assert.ok(!qrOf(w, key).classList.contains('gone'));
});

test('closing a remote tile removes the view and stops its stream; the session is untouched (no call of any kind)', async () => {
  const h = await tWorld();
  const { w } = h;
  await go(w, Q(SLOTS));
  const key = `build-box/${S1}`;
  tile(w, key).querySelector('.qt-close').click();
  await tick();
  assert.equal(tile(w, key), undefined);
  assert.ok(h.subs().filter((s) => s.ref === key).every((s) => s.off), 'unsubscribed');
  assert.equal(h.writes().length, 0);
});

test('the tile menu has the terminal, the session page, a reload, close and one row that says why there is no Tune or slash command; it opens the terminal on that node', async () => {
  const h = await tWorld();
  const { w } = h;
  await go(w, Q(SLOTS));
  const t = tile(w, `build-box/${S1}`);
  t.querySelector('.qt-title').click();
  await tick();
  const rows = all(w, '.menuitem');
  const labels = rows.map((r) => text(r));
  assert.ok(labels.some((l) => /^Open terminal on build-box/.test(l)));
  assert.ok(labels.some((l) => /^Close tile/.test(l) && /the session keeps running on its node/.test(l)));
  assert.ok(labels.some((l) => /^Tune and slash commands/.test(l) && /Not offered on a tile of another node/.test(l)));
  assert.ok(!labels.some((l) => /^(Compact|Kill|Rename)/.test(l)));
  rows.find((r) => /^Open terminal on build-box/.test(text(r))).click();
  assert.deepEqual(h.opened(), [[URL_S1, '_blank', 'noopener']]);
});

test('a tile of a node whose saved address is not an https tailnet address has a disabled terminal icon that says why and opens nothing; o on it opens nothing but says so', async () => {
  const h = await tWorld({ nodes: nodeList({ 'build-box': { url: 'http://build-box.example.ts.net' } }) });
  const { w } = h;
  await go(w, Q(SLOTS));
  const t = tile(w, `build-box/${S1}`);
  const b = t.querySelector('[data-act=terminal]');
  assert.equal(b.getAttribute('aria-disabled'), 'true');
  assert.match(b.getAttribute('title'), /Open terminal on build-box: the address saved for this node is not an https tailnet address/);
  assert.equal(text(b), '', 'an icon, not a long label in a narrow header');
  b.click();
  assert.deepEqual(h.opened(), []);
  assert.match(plain(w.get('__toasts')).map((x) => x.text).join('|'), /not an https tailnet address/);
  w.run('Keymap.install()');
  w.ctx.__k = `build-box/${S1}`;
  w.run('Quad.current.setActive(__k)');
  w.run('Quad.current.openRemote()');
  assert.deepEqual(h.opened(), [], 'no window');
  assert.match(line(t), /The address saved for build-box is not one this board opens\./);
});

// ---------------------------------------------------------------- the quad: keys and the store

test('o on a focused tile of another node opens its terminal; it is quiet in a field, on a local tile and during IME composition; the key is listed in the help only on a board with nodes', async () => {
  const h = await tWorld();
  const { w } = h;
  await go(w, Q(SLOTS));
  w.run('Keymap.install()');
  const K = w.get('Keymap');
  const cur = () => w.get('Quad').current;
  assert.ok(plain(w.run("Keymap.help().filter((x) => x.group === 'Quad').map((x) => x.keys[0])")).includes('O'));
  w.ctx.__k = L1;
  w.run('Quad.current.setActive(__k)');
  assert.equal(K.handle(ev('o')), null, 'a local tile active: not handled');
  assert.deepEqual(h.opened(), []);
  w.ctx.__k = `build-box/${S1}`;
  w.run('Quad.current.setActive(__k)');
  assert.ok(K.handle(ev('o')), 'handled');
  assert.deepEqual(h.opened(), [[URL_S1, '_blank', 'noopener']]);
  assert.equal(K.handle(ev('o', { target: { tagName: 'TEXTAREA' } })), null, 'inside a field the key types');
  assert.equal(K.handle(ev('o', { isComposing: true })), null, 'not during IME composition');
  assert.equal(h.opened().length, 1);
  assert.ok(cur().remoteTarget(), 'the focused remote tile');
  // a single-board quad has no such key
  const solo = await tWorld({ state: boardState({ nodes_enabled: false }), nodes: [] });
  await go(solo.w, Q(`&s=${L1}`));
  assert.ok(!plain(solo.w.run("Keymap.help().filter((x) => x.group === 'Quad').map((x) => x.keys[0])")).includes('O'));
});

test('the store round trip keeps {node, tmux}; an old layout of bare names loads as it was; a saved remote tile comes back after a reload', async () => {
  const h = await tWorld();
  const { w } = h;
  await go(w, Q(SLOTS));
  const saved = JSON.parse(w.localStorage.getItem('ccboard:quad:all'));
  assert.deepEqual(plain(saved.slots.slice(0, 4)), [L1, { node: 'build-box', tmux: S1 }, { node: 'alice-mac', tmux: S1 }, { node: 'old-laptop', tmux: S1 }]);
  assert.deepEqual(plain(w.run("Quad.load('')")).slots.slice(0, 4), [L1, `build-box/${S1}`, `alice-mac/${S1}`, `old-laptop/${S1}`], 'back to keys in memory');
  const fresh = await tWorld();                                           // a new visit: only the saved layout, no address
  fresh.w.localStorage.setItem('ccboard:quad:all', JSON.stringify(saved));
  await go(fresh.w, '#/quad');
  assert.ok(tile(fresh.w, `build-box/${S1}`) && tile(fresh.w, `old-laptop/${S1}`), 'the remote tiles are back from the store');
  assert.ok(tile(fresh.w, L1), 'and the local one');
  const old = await tWorld();
  old.w.localStorage.setItem('ccboard:quad:all', JSON.stringify({ layout: 2, slots: [L1, L2, '', '', '', '', '', '', '', ''], modes: {}, zoom: null }));
  await go(old.w, '#/quad');
  assert.ok(tile(old.w, L1) && tile(old.w, L2));
  assert.equal(all(page(old.w), '.qtile[data-node]').length, 0, 'no node on an old layout');
});

test('with the hub view off a saved remote tile says its node is not paired, shows no tail and no box, and opens nothing', async () => {
  const h = await tWorld({ state: boardState({ nodes_enabled: false }), nodes: [] });
  const { w } = h;
  await go(w, Q(`&s=${L1},build-box/${S1}`));
  const t = tile(w, `build-box/${S1}`);
  assert.ok(t);
  assert.match(text(t), /This session runs on another node that is not paired on this board\./);
  assert.equal(t.querySelector('iframe'), null);
  assert.equal(t.querySelector('textarea'), null);
  assert.equal(h.subs().filter((s) => s.ref.includes('/')).length, 0);
});

// ---------------------------------------------------------------- the dock

test('the terminal dock holds a session of another node as the same tail tile: a header with the chip, the session, Tail and the terminal link; the tail; the prompt box; no iframe; not remembered', async () => {
  const d = dockWorld();
  const { w, Shell } = d;
  const key = `build-box/${S1}`;
  assert.equal(Shell.openDock(key, { focus: false }), true);
  const dock = d.dock();
  assert.ok(dock.classList.contains('has-term'));
  assert.equal(dock.querySelectorAll('iframe').length, 0, 'a tail tile, not a terminal');
  assert.equal(dock.querySelectorAll('.tpane').length, 0);
  const head = dock.querySelector('.qr-head');
  assert.ok(head);
  assert.equal(text(head.querySelector('.nchip-n')), 'build-box');
  assert.equal(text(head.querySelector('.qr-hn')), 's1');
  assert.equal(text(head.querySelector('.qr-mode')), 'Tail');
  assert.ok(dock.querySelector('textarea.tk-ta'), 'the same one-line box as a docked local tile');
  assert.equal(Shell.dockOpen(), true);
  assert.equal(Shell.dock.tmux, key);
  assert.equal(w.localStorage.getItem('ccboard:dock'), null, 'a remote tile is not restored by key');
  w.ctx.__key = key;
  w.run('__subs.filter((s) => s.ref === __key).forEach((s) => s.fn(["dock line one", "dock line two"]))');
  assert.equal(text(dock.querySelector('pre')), 'dock line one\ndock line two');
  // Enter sends through the relay; Shift+Enter is refused
  const ta = dock.querySelector('textarea');
  ta.value = 'hello';
  const sh = { type: 'keydown', key: 'Enter', shiftKey: true, prevented: 0, preventDefault() { this.prevented += 1; } };
  ta.dispatchEvent(sh);
  assert.equal(sh.prevented, 1);
  assert.equal(plain(w.get('__calls')).length, 0);
  ta.dispatchEvent({ type: 'keydown', key: 'Enter', preventDefault() {} });
  await flush();
  assert.deepEqual(plain(w.get('__calls')).map((c) => [c.method, c.path, c.body]), [['POST', `/api/nodes/build-box/sessions/${S1}/prompt`, { text: 'hello', queue: false }]]);
  // the terminal link and Close
  head.querySelector('[data-act=terminal]').click();
  assert.deepEqual(plain(w.get('__opened')), [[URL_S1, '_blank', 'noopener']]);
  head.querySelector('.qr-close').click();
  assert.equal(Shell.dockOpen(), false);
  assert.equal(plain(w.get('__subs')).filter((s) => s.ref === key).every((s) => s.off), true, 'the stream is let go');
});

test('Shell.openTerm and the dock\'s pop-out never build /term/<handle>/<name> on this board for a remote key: they open the node\'s own terminal', () => {
  const d = dockWorld();
  const { w, Shell } = d;
  assert.equal(Shell.openTerm(`build-box/${S1}`), false);
  assert.deepEqual(plain(w.get('__opened')), [[URL_S1, '_blank', 'noopener']]);
  Shell.openDock(`build-box/${S1}`, {});
  assert.equal(Shell.popOutDock(), true);
  assert.deepEqual(plain(w.get('__opened')).slice(1), [[URL_S1, '_blank', 'noopener']]);
  assert.ok(!plain(w.get('__opened')).some((o) => o[0] === 'page' || /%2F/.test(String(o[1]))));
});

test('a remote key for a node this board does not know opens no dock tile', () => {
  const d = dockWorld({ nodes: [] });
  assert.equal(d.Shell.openDock(`nobody/${S1}`, {}), false);
  assert.equal(d.Shell.dockOpen(), false);
  assert.equal(d.dock().querySelectorAll('iframe').length, 0);
});

// ---------------------------------------------------------------- the rules in the sources

test('the sources hold the rules: no innerHTML, no inline style, no iframe in the files that draw a remote tile, the terminal address built only from Nodes.boardUrl', () => {
  for (const f of ['nodes-hub.js', 'pages/node.js']) {
    const src = fs.readFileSync(path.join(STATIC, f), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
    assert.ok(!/innerHTML|insertAdjacentHTML|cssText|\.style\b|style\s*:/.test(src), f);
    assert.ok(!/iframe/i.test(src), `${f}: no iframe`);
  }
  const hub = fs.readFileSync(path.join(STATIC, 'nodes-hub.js'), 'utf8');
  assert.match(hub, /Nodes\.termUrl = function \(rec, tmux\) \{\s*const base = Nodes\.boardUrl\(rec && rec\.url\);\s*return base && Ref\.isTmux\(tmux\)/, 'the address and the name are checked before a link is built');
  assert.match(hub, /window\.open\(url, '_blank', 'noopener'\)/);
  const quad = fs.readFileSync(path.join(STATIC, 'pages', 'quad.js'), 'utf8');
  assert.ok(!/innerHTML|cssText/.test(quad));
  const css = fs.readFileSync(path.join(STATIC, 'pages', 'nodes.css'), 'utf8') + fs.readFileSync(path.join(STATIC, 'pages', 'quad.css'), 'utf8');
  assert.ok(!/—/.test(css), 'no em-dash');
  for (const f of ['nodes-hub.js', 'pages/node.js', 'pages/quad.js', 'palette.js']) {
    const src = fs.readFileSync(path.join(STATIC, f), 'utf8');
    const strings = src.match(/(['"`])(?:(?!\1)[^\\\n]|\\.)*\1/g) || [];
    assert.ok(!strings.some((s) => /Open terminal|Tail|offline since|re-pair/.test(s) && /—/.test(s)), `${f}: no em-dash in UI text`);
  }
});
