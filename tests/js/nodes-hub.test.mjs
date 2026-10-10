// Contract tests for the hub view of issue #139: the model and its poll (app/static/nodes-hub.js), the node chip (nodes.js), the Home strip, the node page and its read only
// peeks (pages/node.js), the inbox and Tasks sections, the sidebar groups, the palette rows, the @node prefix and g n. Real scripts on minidom's DOM (tests/js/hubworld.mjs);
// the board's answers are a fake fetch, and ?demo=1 reads the fixture (demo/nodes.json, key hub) through core.js's demo handler. Nothing here opens a network connection.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC } from './harness.mjs';
import { answer, boardState, fetched, hubWorld, iso, page, plain, poll, rec, setState, text, tick } from './hubworld.mjs';

const HOSTILE = '<img src=x onerror=alert(1)>';
const three = () => [rec('build-box'), rec('alice-mac', { status: 'stale', age_s: 190, skew_warn: true, skew_ms: 7400, error_kind: 'timeout' }),
  rec('old-laptop', { status: 'offline', age_s: 21600, last_ok_at: iso(21600), error_kind: 'refused' })];
const ready = async (nodes = three(), over = {}) => { const h = hubWorld({ state: boardState(), nodes, ...over }); await poll(h.w); return h; };
const go = async (w, hash) => { w.location.hash = hash; await tick(); };
const all = (n, sel) => (n.document || n).querySelectorAll(sel);
const noMarkup = (w) => assert.equal(all(w, 'img, iframe, script').length, 0, 'peer text never becomes markup');

// ---------------------------------------------------------------- the chip

test('the chip: nothing while the hub view is off, a dim one for this board once a node is paired, a plain one for a node; the name is text', async () => {
  const off = hubWorld({ state: boardState({ nodes_enabled: false }), nodes: three() });
  assert.equal(off.w.run('Nodes.chip(null)'), null);
  assert.equal(off.w.run("Nodes.chip('build-box')"), null);

  const { w } = await ready();
  const local = w.run('Nodes.chip(null)');
  assert.deepEqual([local.className, local.getAttribute('data-node')], ['nchip local', 'local']);
  assert.equal(text(local), 'BObox');
  const remote = w.run("Nodes.chip('build-box')");
  assert.equal(remote.className, 'nchip');
  assert.equal(text(remote), 'BUbuild-box');
  assert.equal(remote.querySelector('.nchip-m').getAttribute('aria-hidden'), 'true', 'the monogram is decoration; the name is the label');
  assert.doesNotMatch(remote.className, /hue-|agent|state/, 'a neutral chip: no agent or state hue');
});

test('the chip of a node that is not online carries a glyph and its word in the title; amber and rose are not used to hash a node', async () => {
  const { w } = await ready();
  const stale = w.run("Nodes.chip('alice-mac')");
  assert.equal(stale.querySelector('.nchip-g').textContent, '◐');
  assert.match(stale.getAttribute('title'), /alice-mac: stale 3 min/);
  assert.equal(w.run("Nodes.chip('build-box')").querySelector('.nchip-g'), null, 'an online node needs no glyph');
  assert.equal(w.run("Nodes.chip('old-laptop')").querySelector('.nchip-g').textContent, '○');
});

test('a hostile node name renders as text in the chip, the strip, the node page, the sidebar and the palette; no element is made from it', async () => {
  const { w } = await ready([rec('evil', { name: HOSTILE }), rec('build-box')]);
  assert.equal(text(w.run("Nodes.chip('evil')")), 'IM' + HOSTILE);
  await go(w, '#/');
  w.run('updateCurrentPage(state)');
  assert.ok(text(all(w, '.nd-cell')[1]).includes(HOSTILE));
  await go(w, '#/n/evil');
  assert.equal(text(page(w).querySelector('h1')), HOSTILE);
  w.run('Shell.refs = {}; Shell.buildSide(document.querySelector("#sidebar"), false); Shell.patchTrees()');
  assert.ok(text(w.document.querySelector('#sidebar')).includes(HOSTILE));
  w.run('Palette.open()');
  assert.ok(text(w.document.querySelector('#helpdlg')).includes(`Go to ${HOSTILE}`));
  noMarkup(w);
});

// ---------------------------------------------------------------- the poll

test('Nodes.poll: GET /api/nodes/state on this origin with the ETag it holds as If-None-Match; a 304 keeps the model; a failure keeps the model and says how old it is', async () => {
  const { w } = hubWorld({ state: boardState(), nodes: [rec('build-box')] });
  answer(w, 200, { nodes: [rec('build-box')], at: iso(0) }, 'W/"abc"');
  assert.equal(await poll(w), true);
  assert.deepEqual(fetched(w), [{ url: '/api/nodes/state', headers: { 'X-CCBoard': '1' } }], 'the first ask has no ETag to send');
  assert.deepEqual(plain(w.run('Nodes.list().map((r) => r.handle)')), ['build-box']);
  assert.equal(w.run('Nodes.M.etag'), 'W/"abc"');

  answer(w, 304, null, null);
  assert.equal(await poll(w), false, 'unchanged: nothing repaints');
  assert.equal(fetched(w)[1].headers['If-None-Match'], 'W/"abc"');
  assert.deepEqual(plain(w.run('Nodes.list().map((r) => r.handle)')), ['build-box'], 'a 304 keeps the model');
  assert.equal(w.run('Nodes.M.err'), null);

  answer(w, 200, null, null, { throw: 'the board did not answer' });
  await poll(w);
  assert.equal(w.run('Nodes.M.err'), 'the board did not answer');
  assert.deepEqual(plain(w.run('Nodes.list().map((r) => r.handle)')), ['build-box'], 'a failure keeps the last model');
  assert.equal(w.run("Nodes.get('build-box').status"), 'online');
  w.run('Nodes.M.recvAt = Date.now() - 95000');
  assert.ok(w.run("Nodes.ageOf(Nodes.get('build-box'))") >= 96, 'the age keeps counting from the last answer');
  await go(w, '#/n/build-box');
  assert.match(text(page(w)), /could not refresh \(the board did not answer\)\. Showing the answer from 1 min ago/);
  assert.ok(page(w).querySelector('h1'), 'the page is still there, not a blank');

  answer(w, 500, null, null, { statusText: 'Internal Server Error' });
  await poll(w);
  assert.match(w.run('Nodes.M.err'), /^500/, 'a 5xx is a failure too');
  answer(w, 200, { nodes: [rec('build-box', { name: 'renamed' })], at: iso(0) }, 'W/"def"');
  await poll(w);
  assert.equal(w.run('Nodes.M.err'), null);
  assert.equal(w.run("Nodes.get('build-box').name"), 'renamed');
});

test('Nodes.poll asks nothing while the tab is hidden, nothing while the view is off, and one request at a time; force asks anyway', async () => {
  const { w } = await ready([rec('build-box')]);
  const before = fetched(w).length;
  w.document.hidden = true;
  assert.equal(await poll(w), null);
  assert.equal(fetched(w).length, before, 'no request while document.hidden');
  assert.equal((await poll(w, { force: true })) !== null, true);
  assert.equal(fetched(w).length, before + 1);
  w.document.hidden = false;
  setState(w, boardState({ nodes_enabled: false }));
  assert.equal(await poll(w), null);
  assert.equal(fetched(w).length, before + 1, 'no request while state.nodes_enabled is false');
  setState(w, boardState());
  const p1 = poll(w);
  const p2 = poll(w);
  assert.equal(await p2, null, 'a second ask while one is out does nothing');
  await p1;
});

test('the poll runs every 6 s from Nodes.start, once, and a hidden tab skips the beat; Nodes.stop ends it', async () => {
  const { w } = hubWorld({ state: boardState(), nodes: [rec('build-box')] });
  const calls = [];
  w.run(`globalThis.__ticks = []; setInterval = (fn, ms) => { __ticks.push(ms); globalThis.__beat = fn; return 77; }; clearInterval = (id) => { __ticks.push('clear ' + id); };`);
  w.run('Nodes.start(); Nodes.start();');
  await tick();
  assert.deepEqual(plain(w.get('__ticks')), [6000], 'one timer of 6000 ms, however often start is called');
  const n = fetched(w).length;
  w.document.hidden = true;
  w.run('__beat()');
  await tick();
  assert.equal(fetched(w).length, n, 'the beat of a hidden tab asks nothing');
  w.document.hidden = false;
  w.run('__beat()');
  await tick();
  assert.equal(fetched(w).length, n + 1);
  w.run('Nodes.stop()');
  assert.deepEqual(plain(w.get('__ticks')), [6000, 'clear 77']);
  assert.deepEqual(plain(w.run('Nodes.list()')), [], 'stopped: the model is dropped');
  assert.ok(calls);
});

test('a tab that becomes visible asks at once', async () => {
  const { w } = hubWorld({ state: boardState(), nodes: [rec('build-box')] });
  w.run('Nodes.start()');
  await tick();
  const n = fetched(w).length;
  w.document.hidden = false;
  w.document.dispatch('visibilitychange');
  await tick();
  assert.equal(fetched(w).length, n + 1);
  w.run('Nodes.stop()');
});

test('demo mode reads the fixture through core.js and nothing else: three nodes, online, stale with a skew warning, offline; no request leaves the page', async () => {
  const { w } = hubWorld({ search: '?demo=1', realApi: true });
  const st = plain(await w.run("api('GET', '/api/state')"));
  assert.equal(st.nodes_enabled, true);
  setState(w, st);
  await poll(w);
  const list = plain(w.run('Nodes.list()'));
  assert.deepEqual(list.map((r) => [r.handle, r.status, r.skew_warn]), [['build-box', 'online', false], ['alice-mac', 'stale', true], ['old-laptop', 'offline', false]]);
  assert.equal(w.run("Nodes.view(Nodes.get('alice-mac')).word"), 'stale 3 min');
  assert.match(w.run("Nodes.view(Nodes.get('old-laptop')).word"), /^offline since \d\d:\d\d$/);
  const urls = plain(w.get('__fetches')).map((f) => f.url);
  assert.ok(urls.every((u) => u.startsWith('/static/demo/')), `only fixtures: ${urls}`);
  assert.ok(!urls.some((u) => /^\/api\//.test(u)));
});

test('the demo fixture has three hub nodes whose handles are the paired ones and every record has the shape of GET /api/nodes/state', () => {
  const d = JSON.parse(fs.readFileSync(path.join(STATIC, 'demo', 'nodes.json'), 'utf8'));
  assert.deepEqual(d.hub.map((r) => r.handle), ['build-box', 'alice-mac', 'old-laptop']);
  assert.deepEqual(d.hub.map((r) => r.status), ['online', 'stale', 'offline']);
  assert.equal(d.hub[1].skew_warn, true);
  assert.ok(Math.abs(d.hub[1].skew_ms) > 5000);
  for (const r of d.hub) {
    for (const k of ['peer_id', 'handle', 'node_id', 'name', 'url', 'status', 'polled_at', 'last_ok_at', 'age_s', 'skew_ms', 'skew_warn', 'error_kind', 'card', 'state', 'etag', 'legacy', 'scopes']) assert.ok(k in r, `${r.handle}.${k}`);
    for (const k of ['api', 'node', 'projects', 'sessions', 'tasks', 'needs_you', 'usage', 'lanes', 'login_problems', 'truncated']) assert.ok(k in r.state, `${r.handle}.state.${k}`);
    assert.match(r.url, /^https:\/\/[a-z-]+\.example\.ts\.net/);
  }
  assert.ok(!JSON.stringify(d.hub).match(/@|\/Users\/|\/home\//), 'no e-mail address or personal path in the fixture');
});

// ---------------------------------------------------------------- Home

test('Home strip from the demo fixture: this node first, then three cells in the hub order, each a link to its page with status as glyph and word, live sessions, who needs you, the 5-hour pill', async () => {
  const { w } = hubWorld({ search: '?demo=1', realApi: true });
  setState(w, plain(await w.run("api('GET', '/api/state')")));
  await poll(w);
  await go(w, '#/');
  w.run('updateCurrentPage(state)');
  const cells = all(w, '.nd-cell');
  assert.equal(cells.length, 4, 'this node and three');
  assert.deepEqual(cells.map((c) => c.getAttribute('href')), ['#/agents', '#/n/build-box', '#/n/alice-mac', '#/n/old-laptop']);
  assert.deepEqual(cells.map((c) => c.querySelector('.nd-cell-s').textContent), ['online', 'online', 'stale 3 min', w.run("Nodes.view(Nodes.get('old-laptop')).word")]);
  assert.deepEqual(cells.map((c) => c.querySelector('.nd-sg').textContent), ['●', '●', '◐', '○']);
  assert.match(text(cells[1]), /4 live · 2 need you/);
  assert.match(text(cells[1]), /5h 42%/);
  assert.match(text(cells[2]), /last reading 3 min ago/);
  assert.ok(cells[3].classList.contains('dim') && !cells[1].classList.contains('dim'), 'an offline cell is dim');
  assert.match(text(cells[3]), /last reading 6 h ago/);
  assert.match(cells[3].getAttribute('aria-label'), /^old-laptop: offline since \d\d:\d\d, /);
  const strip = w.document.querySelector('.nd-strip');
  assert.match(text(strip), /The clock of alice-mac differs from this board's by more than 5 s/);
  assert.match(text(w.document.querySelector('.nd-attn')), /2 need you on build-box/);
  assert.equal(all(w, '.nd-strip .primary').length, 0, 'the strip adds no filled primary');
  noMarkup(w);
});

test('Home strip sits after the away line and before the inbox, and the arrow keys walk the cells', async () => {
  const { w } = await ready();
  await go(w, '#/');
  w.run('updateCurrentPage(state)');
  const kids = page(w).children;
  const at = (cls) => kids.findIndex((n) => n.classList.contains(cls));
  assert.ok(at('nd-strip') > at('home-away') || at('home-away') < 0);
  assert.ok(at('nd-strip') < at('home-inbox'), 'before the inbox');
  const cells = all(w, 'a.nd-cell');
  cells[0].focus();
  const key = (k) => { const e = { key: k, target: cells[0], prevented: false, preventDefault() { this.prevented = true; } }; w.document.querySelector('.nd-cells').dispatchEvent({ type: 'keydown', ...e, target: w.document.activeElement }); };
  key('ArrowRight');
  assert.equal(w.document.activeElement, cells[1]);
  key('End');
  assert.equal(w.document.activeElement, cells[3]);
  key('ArrowLeft');
  assert.equal(w.document.activeElement, cells[2]);
  key('Home');
  assert.equal(w.document.activeElement, cells[0]);
});

test('the strip says so when the hub cannot be read, and keeps the last cells with their age', async () => {
  const { w } = await ready([rec('build-box')]);
  await go(w, '#/');
  w.run('updateCurrentPage(state)');
  answer(w, 200, null, null, { throw: 'timeout' });
  await poll(w);
  assert.match(text(w.document.querySelector('.nd-strip')), /Could not refresh the nodes \(timeout\)\. Showing the last reading from \d+ s ago\./);
  assert.equal(all(w, '.nd-cell').length, 2, 'the cells stay');
});

test('no strip while the model is empty or not read yet; no node, no section', async () => {
  const { w } = hubWorld({ state: boardState(), nodes: [] });
  await go(w, '#/');
  w.run('updateCurrentPage(state)');
  const strip = w.document.querySelector('.nd-strip');
  assert.ok(!strip || strip.classList.contains('hidden'));
});

// ---------------------------------------------------------------- the node page

test('the node page: header (name, status with age, OS, version, Open board), then Needs you, Sessions, Tasks, Repos and Account windows; every row has the chip', async () => {
  const { w } = await ready();
  await go(w, '#/n/build-box');
  const p = page(w);
  assert.equal(text(p.querySelector('h1')), 'build-box');
  assert.match(text(p.querySelector('.nd-status')), /^●online$/);
  assert.match(text(p.querySelector('.nd-facts')), /Linux · v0\.5\.36 · docker · handle build-box/);
  const open = p.querySelector('a.btn.primary');
  assert.equal(text(open), 'Open board');
  assert.deepEqual([open.getAttribute('href'), open.getAttribute('target'), open.getAttribute('rel')], ['https://build-box.example.ts.net/', '_blank', 'noopener noreferrer']);
  assert.equal(all(w, 'iframe').length, 0, 'no iframe');
  assert.deepEqual(all(p, 'h2.nd-h').map((h) => h.childNodes[0].textContent), ['Needs you', 'Sessions', 'Tasks', 'Repos', 'Account windows']);
  const sec = (id) => p.querySelector(`[data-sec=${id}]`);
  assert.equal(all(sec('needs'), '.nd-item').length, 1);
  assert.equal(all(sec('sessions'), '.nd-item').length, 2);
  assert.equal(all(sec('tasks'), '.nd-item').length, 1);
  assert.match(text(sec('repos')), /shop\/api.*example\/shop-api.*main · uncommitted changes/);
  assert.match(text(sec('windows')), /Claude window.*5-hour window.*42%/);
  assert.match(text(sec('windows')), /Work.*in use.*40%/);
  for (const id of ['needs', 'sessions', 'tasks']) for (const row of all(sec(id), '.nd-item')) assert.ok(row.querySelector('.nchip'), `${id}: a chip on the row`);
  assert.deepEqual(all(sec('needs'), 'a.nd-name').map((a) => a.getAttribute('href')), ['#/n/build-box/s/shop--api--s1']);
  assert.deepEqual(all(sec('tasks'), 'a.nd-name').map((a) => a.getAttribute('href')), ['#/n/build-box/t/7']);
  assert.match(text(p), /Read only here/);
  assert.doesNotMatch(text(p), /—/, 'no em-dash in UI text');
});

test('the node page of an offline node shows its last state with the age; a stale one says so; the skew above 5 s is a warning', async () => {
  const { w } = await ready();
  await go(w, '#/n/old-laptop');
  const p = page(w);
  assert.match(text(p.querySelector('.nd-status')), /^○offline since \d\d:\d\d$/);
  assert.match(text(p.querySelector('.nd-banner')), /Showing the last reading, from 6 h ago\. old-laptop refused the connection\./);
  assert.equal(all(p, '[data-sec=sessions] .nd-item').length, 2, 'the last state is still listed');
  await go(w, '#/n/alice-mac');
  const banners = all(page(w), '.nd-banner').map(text);
  assert.ok(banners.some((b) => /Showing the last reading, from 3 min ago\. alice-mac did not answer in time\./.test(b)));
  assert.ok(banners.some((b) => /Its clock differs from this board's by 7 s, so ages from alice-mac may be off\./.test(b)));
});

test('a node that never answered: no state, a next step in every section, nothing blank', async () => {
  const { w } = await ready([rec('fresh', { status: 'offline', state: null, card: null, last_ok_at: null, age_s: null, error_kind: 'timeout' })]);
  await go(w, '#/n/fresh');
  const p = page(w);
  assert.match(text(p.querySelector('.nd-banner')), /Nothing has been read from fresh yet: it did not answer in time\. It may be asleep or off the tailnet\./);
  assert.match(text(p.querySelector('.nd-status')), /offline, never read/);
  for (const e of all(p, '.nd-empty')) assert.ok(!e.classList.contains('hidden') && text(e).length > 20, 'an empty section says what to do');
});

test('an unauthorized node offers Re-pair to Settings, Nodes as the page\'s one filled primary; Open board stays plain', async () => {
  const { w } = await ready([rec('revoked', { status: 'unauthorized', error_kind: null })]);
  await go(w, '#/n/revoked');
  const p = page(w);
  assert.match(text(p.querySelector('.nd-status')), /re-pair/);
  const repair = all(p, 'a.btn').find((a) => text(a) === 'Re-pair');
  assert.equal(repair.getAttribute('href'), '#/settings?sec=nodes');
  assert.ok(repair.classList.contains('primary'));
  assert.equal(all(p, '.primary').length, 1);
  assert.equal(all(p, 'a.btn').find((a) => text(a) === 'Open board').classList.contains('primary'), false);
  assert.match(text(p.querySelector('.nd-banner')), /no longer accepts the token this board saved/);
  const strip = rec('revoked', { status: 'unauthorized' });
  assert.ok(strip);
});

test('controls that need a relay action are present, aria-disabled, and carry their reason; a missing scope is named', async () => {
  const { w } = await ready([rec('build-box', { scopes: ['read'] }), rec('wide', { scopes: ['read', 'tasks', 'sessions'] })]);
  await go(w, '#/n/build-box');
  const acts = page(w).querySelector('.nd-acts');
  assert.deepEqual(acts.children.map(text), ['Open board', 'New task here', 'New session here'], 'one grid of equal cells: Open board first, the disabled ones after');
  assert.ok(acts.children[0].classList.contains('primary'));
  const offs = all(acts, 'button.nd-off');
  const note = page(w).querySelector('.nd-whys');
  assert.equal(text(note), 'New task: needs the tasks scope on build-box. New session: needs the sessions scope on build-box.', 'the reasons in one short note under the grid');
  assert.equal(all(page(w), '.nd-whys').length, 1);
  for (const b of offs) {
    assert.equal(b.getAttribute('aria-disabled'), 'true');
    const why = all(note, 'span').find((x) => x.getAttribute('id') === b.getAttribute('aria-describedby'));
    assert.ok(why, 'each control is described by its own reason');
    assert.equal(b.disabled, false, 'it stays focusable, so keyboard and touch can read the reason');
    b.click();
  }
  assert.match(text(all(note, 'span')[0]), /^New task:/);
  assert.match(plain(w.get('__toasts'))[0].text, /New task here: needs the tasks scope on build-box/);
  await go(w, '#/n/wide');
  assert.equal(text(page(w).querySelector('.nd-whys')), 'New task: read only in this version. New session: read only in this version.', 'with the scope the reason is the version, not a permission');
});

test('the session peek is read only: its fields, an Open on <node> link to that node\'s own page in a new tab, no iframe, and the disabled controls with their reasons', async () => {
  const { w } = await ready();
  await go(w, '#/n/build-box/s/shop--api--s1');
  const p = page(w);
  assert.equal(text(p.querySelector('h1')), 's1');
  assert.ok(p.querySelector('.nchip'));
  const facts = text(p.querySelector('.nd-facts-dl'));
  for (const piece of ['needs you', 'claude', 'Opus 5', 'shop/api', 'user', 'shop--api--s1']) assert.ok(facts.includes(piece), piece);
  const open = all(p, 'a.btn').find((a) => text(a) === 'Open on build-box');
  assert.deepEqual([open.getAttribute('href'), open.getAttribute('target'), open.getAttribute('rel')], ['https://build-box.example.ts.net/#/s/shop--api--s1', '_blank', 'noopener noreferrer']);
  assert.equal(all(w, 'iframe').length, 0);
  assert.deepEqual(all(p, '.nd-acts button.nd-off').map(text), ['Send a prompt', 'Answer', 'Close session']);
  assert.equal(text(p.querySelector('.nd-whys')), 'Send a prompt: needs the sessions scope on build-box. Answer: needs the sessions scope on build-box. Close session: needs the sessions scope on build-box.');
  assert.equal(all(p, 'a.btn').find((a) => /^Back to/.test(text(a))).getAttribute('href'), '#/n/build-box');
  assert.match(text(p), /Read only: the last reading of build-box, \d+ s ago\./);
});

test('the task peek: fields, a link to the node\'s session peek, Open on <node> to its tasks page, disabled Run again and Cancel', async () => {
  const { w } = await ready();
  await go(w, '#/n/build-box/t/7');
  const p = page(w);
  assert.equal(text(p.querySelector('h1')), 'Add pagination');
  const facts = text(p.querySelector('.nd-facts-dl'));
  for (const piece of ['running', 'claude', 'shop/api', 'task/p', '#12']) assert.ok(facts.includes(piece), piece);
  assert.equal(p.querySelector('.nd-facts-dl a').getAttribute('href'), '#/n/build-box/s/shop--api--s2');
  assert.equal(all(p, 'a.btn').find((a) => text(a) === 'Open on build-box').getAttribute('href'), 'https://build-box.example.ts.net/#/tasks');
  assert.deepEqual(all(p, '.nd-acts button.nd-off').map(text), ['Run again', 'Cancel task']);
  assert.match(text(p.querySelector('.nd-whys')), /^Run again: needs the tasks scope on build-box\. Cancel task: needs the tasks scope on build-box\.$/);
});

test('a peek of something that left the last reading says so with a way back; an unknown node, or any #/n/ address while the view is off, is the plain not-paired page', async () => {
  const { w } = await ready();
  await go(w, '#/n/build-box/s/shop--api--gone');
  assert.match(text(page(w)), /That session is not here.*may have ended/);
  assert.equal(all(page(w), 'a.btn')[0].getAttribute('href'), '#/n/build-box');
  await go(w, '#/n/build-box/t/999');
  assert.match(text(page(w)), /That task is not here/);
  await go(w, '#/n/nobody');
  assert.match(text(page(w)), /This node is not paired.*"nobody"/);
  assert.equal(page(w).querySelector('a').getAttribute('href'), '#/settings?sec=nodes');
  setState(w, boardState({ nodes_enabled: false }));
  await go(w, '#/n/build-box');
  assert.match(text(page(w)), /This node is not paired/);
  const asked = fetched(w).length;
  await go(w, '#/n/other');
  assert.equal(fetched(w).length, asked, 'no request for an address while the view is off');
});

test('a direct load of #/n/<handle> before the first state shows "Reading the nodes", not "not paired"; the sections come back after the nodes went and came again', async () => {
  const { w } = hubWorld({ state: null, nodes: [rec('build-box')] });
  await go(w, '#/n/build-box');
  assert.match(text(page(w)), /Reading the nodes/);
  assert.doesNotMatch(text(page(w)), /not paired/);
  setState(w, boardState());
  await poll(w);
  assert.equal(text(page(w).querySelector('h1')), 'build-box', 'the page fills in when the state and the reading are there');
  await go(w, '#/');
  w.run('updateCurrentPage(state)');
  assert.equal(all(w, '.nd-strip').length, 1);
  setState(w, boardState({ nodes_enabled: false }));
  w.run('Nodes.sync(state)');
  assert.equal(all(w, '.nd-strip').length, 0, 'the view went off: its sections go with it');
  setState(w, boardState());
  answer(w, 200, { nodes: [rec('build-box')], at: iso(0) }, 'W/"9"');
  w.run('Nodes.sync(state)');
  await tick();
  w.run('updateCurrentPage(state)');
  assert.equal(all(w, '.nd-strip').length, 1, 'and they are built again when a node is back');
});

test('the node page repaints in place with every reading: a row keeps its element, and a new reading changes only what changed', async () => {
  const { w } = await ready();
  await go(w, '#/n/build-box');
  const row = all(page(w), '[data-sec=sessions] .nd-item')[0];
  const sig = row.ccSig;
  answer(w, 200, { nodes: [rec('build-box', { etag: 'new' })], at: iso(0) }, 'W/"2"');
  await poll(w);
  assert.equal(all(page(w), '[data-sec=sessions] .nd-item')[0], row, 'the same element');
  assert.equal(row.ccSig, sig);
  const next = rec('build-box');
  next.state.sessions[0] = { ...next.state.sessions[0], state: 'idle', needs_you: false };
  answer(w, 200, { nodes: [next], at: iso(0) }, 'W/"3"');
  await poll(w);
  assert.equal(all(page(w), '[data-sec=needs] .nd-item').length, 0);
  assert.match(text(page(w).querySelector('[data-sec=needs] .nd-empty')), /Nothing needs you on build-box/);
});

test('Nodes.boardUrl: https and a tailnet name or address only; no user info, no path, no other scheme; nothing else becomes an href', async () => {
  const { w } = await ready();
  const url = (u) => w.run(`Nodes.boardUrl(${JSON.stringify(u)})`);
  assert.equal(url('https://box.example.ts.net'), 'https://box.example.ts.net/');
  assert.equal(url('https://Box.Tail-1.ts.net:8443/'), 'https://box.tail-1.ts.net:8443/');
  assert.equal(url('https://100.64.0.21'), 'https://100.64.0.21/');
  assert.equal(url('https://100.127.9.9:10000'), 'https://100.127.9.9:10000/');
  assert.equal(url('https://[fd7a:115c:a1e0:ab12::1]:8443'), 'https://[fd7a:115c:a1e0:ab12::1]:8443/');
  for (const bad of ['http://box.example.ts.net', 'javascript:alert(1)', 'https://example.com', 'https://box.example.ts.net.evil.com', 'https://evil.com/box.ts.net', 'https://user@box.example.ts.net',
    'https://box.example.ts.net/path', 'https://box.example.ts.net:99999', 'https://100.63.0.1', 'https://100.128.0.1', 'https://192.168.1.5', 'https://[::1]', 'https://127.0.0.1:8443', '//box.example.ts.net', '',
    'https://box.example.ts.net?x=1', 'https://box.example.ts.net#x', 'https://.ts.net', 'https://box example.ts.net', null, 7]) assert.equal(url(bad), null, String(bad));
});

test('a node whose saved address is not a tailnet https address gets no link: Open board is a disabled control with the reason, and the palette row is off', async () => {
  const { w } = await ready([rec('odd', { url: 'http://odd.example.com' })]);
  await go(w, '#/n/odd');
  const p = page(w);
  assert.equal(all(p, 'a[target=_blank]').length, 0, 'no external link at all');
  assert.ok(all(p, '.nd-acts button.nd-off').some((b) => text(b) === 'Open board'));
  assert.match(text(p.querySelector('.nd-whys')), /Open board: the address saved for this node is not an https tailnet address\./);
  w.run('Palette.open()');
  const row = all(w, '.pal-item').find((n) => text(n).startsWith('Open board on odd'));
  assert.equal(row.getAttribute('aria-disabled'), 'true');
  w.run('Palette.ui.sel = Palette.ui.shown.findIndex((i) => i.id === "nb:odd"); Palette.runSelected({})');
  assert.deepEqual(plain(w.get('__opened')), [], 'nothing is opened');
});

// ---------------------------------------------------------------- inbox and Tasks

test('the inbox: remote items that need you sit in "On other nodes" under the local cards, read only, each with its chip; "Nothing needs you" is not shown while they wait', async () => {
  const { w } = await ready([rec('build-box'), rec('alice-mac', { status: 'stale', age_s: 190 })]);
  setState(w, boardState({ projects: [] }));
  await go(w, '#/inbox');
  const sec = w.document.querySelector('.nd-inbox');
  assert.match(text(sec.querySelector('h2')), /^On other nodes \(2\)$/);
  assert.deepEqual(all(sec, '.nd-item').map((r) => r.querySelector('.nchip-n').textContent), ['build-box', 'alice-mac']);
  assert.deepEqual(all(sec, 'a.nd-name').map((a) => a.getAttribute('href')), ['#/n/build-box/s/shop--api--s1', '#/n/alice-mac/s/shop--api--s1']);
  assert.match(text(sec), /Read only here/);
  const sib = sec.parentNode.children;
  assert.ok(sib[sib.indexOf(sec) - 1].classList.contains('inbox-list'), 'right under the local list');
  const none = all(page(w), '.empty').find((n) => /Nothing needs you/.test(text(n)));
  assert.ok(none.classList.contains('hidden'));
});

test('Home keeps its inbox and adds the remote items below it, at most the Home limit, with a link to all of them', async () => {
  const many = rec('build-box');
  many.state.sessions = Array.from({ length: 11 }, (_, i) => ({ tmux: `shop--api--n${i}`, project: 'shop', repo: 'api', session: `n${i}`, agent: 'claude', state: 'waiting', needs_you: true, kind: 'user', since: iso(60 + i), model: null }));
  const { w } = await ready([many]);
  await go(w, '#/');
  w.run('updateCurrentPage(state)');
  const sec = w.document.querySelector('.nd-inbox');
  assert.equal(all(sec, '.nd-item').length, w.run('HOME_INBOX_LIMIT'));
  assert.match(text(sec.querySelector('h2')), /\(11\)$/);
  const more = sec.querySelector('a.btn');
  assert.deepEqual([text(more), more.getAttribute('href'), more.classList.contains('hidden')], ['Show all 11', '#/inbox', false]);
  const kids = page(w).children;
  assert.ok(kids.indexOf(sec) === kids.findIndex((n) => n.id === 'inbox-home') + 1, 'right after the local inbox');
});

test('Tasks: the filter All nodes / This node / <each> (only when nodes exist), tasks of other nodes read only with the chip, this board\'s cards wear the dim chip', async () => {
  const { w } = await ready([rec('build-box'), rec('alice-mac')]);
  setState(w, boardState({ tasks: [{ id: 5, slug: 's', title: 'Local task', project: 'ccboard', repo: 'ccboard', tmux: null, column: 'backlog', mode: 'worktree', pr_number: null, pr_url: null, pr_state: null, phase: 'backlog' }] }));
  await go(w, '#/tasks');
  const bar = w.document.querySelector('.nd-tfilter');
  assert.ok(!bar.classList.contains('hidden'));
  assert.deepEqual(all(bar, '.seg-btn').map(text), ['All nodes', 'This node', 'build-box', 'alice-mac']);
  assert.deepEqual(all(bar, '.seg-btn').map((b) => b.getAttribute('aria-pressed')), ['true', 'false', 'false', 'false']);
  const board = w.document.querySelector('#tasks');
  assert.ok(board.querySelector('.task .meta .nchip.local'), 'this board\'s card carries the dim chip in its meta line, beside where the task lives');
  const rows = () => all(w.document.querySelector('.nd-tasks'), '.nd-item');
  assert.equal(rows().length, 2, 'all nodes: the other nodes\' tasks under the board');
  assert.ok(rows().every((r) => r.querySelector('.nchip') && r.querySelector('a.nd-name')));
  const click = (label) => all(w.document.querySelector('.nd-tfilter'), '.seg-btn').find((b) => text(b) === label).click();
  click('This node');
  assert.ok(w.document.querySelector('.nd-tasks').classList.contains('hidden'));
  assert.ok(!board.classList.contains('nd-hide'));
  click('alice-mac');
  assert.equal(rows().length, 1);
  assert.equal(text(rows()[0].querySelector('.nchip-n')), 'alice-mac');
  assert.match(text(w.document.querySelector('.nd-tasks h2')), /^Tasks on alice-mac \(1\)$/);
  assert.ok(board.classList.contains('nd-hide'), 'this board\'s columns step aside for another node\'s list');
  assert.equal(w.document.querySelector('.nd-tfilter [aria-pressed=true]').textContent, 'alice-mac');
  assert.equal(w.localStorage.getItem('ccboard:nodes:tfilter'), 'alice-mac');
  click('All nodes');
  assert.ok(!board.classList.contains('nd-hide'));
  assert.equal(rows().length, 2);
});

test('Tasks filter of a node without tasks says what to do; a remembered node that is gone falls back to All nodes', async () => {
  const quiet = rec('quiet');
  quiet.state.tasks = [];
  const { w } = await ready([quiet]);
  w.localStorage.setItem('ccboard:nodes:tfilter', 'quiet');
  await go(w, '#/tasks');
  assert.match(text(w.document.querySelector('.nd-tasks .nd-empty')), /No task in the last reading of quiet\. Open its board to add one\./);
  answer(w, 200, { nodes: [], at: iso(0) }, 'W/"x"');
  await poll(w);
  assert.equal(w.document.querySelector('.nd-tfilter').classList.contains('hidden'), true);
});

// ---------------------------------------------------------------- local rows

test('rows of this board wear the dim chip (Agents, Home, the peek) once nodes exist, and the chip follows the name of the board', async () => {
  const { w } = await ready();
  await go(w, '#/agents');
  w.run('updateCurrentPage(state)');
  const chips = all(page(w), '.rrow .nchip');
  assert.equal(chips.length, 2);
  assert.ok(chips.every((c) => c.classList.contains('local') && text(c) === 'BObox'));
  setState(w, boardState({ node_name: 'studio' }));
  w.run('updateCurrentPage(state)');
  assert.deepEqual(all(page(w), '.rrow .nchip').map(text), ['STstudio', 'STstudio'], 'a renamed board repaints its chips');
  setState(w, boardState({ nodes_enabled: false }));
  w.run('updateCurrentPage(state)');
  assert.equal(all(page(w), '.nchip').length, 0, 'and they go with the view');
});

// ---------------------------------------------------------------- the sidebar

test('the sidebar: "This node" as today, then one collapsed group per paired node with its needs-you count and its sessions under Ref.key keys', async () => {
  const { w } = await ready();
  w.run('Shell.refs = {}; Shell.buildSide(document.querySelector("#sidebar"), false); Shell.patchTrees()');
  const side = w.document.querySelector('#sidebar');
  const tree = side.querySelector('.tree');
  const keys = tree.children.map((n) => n.getAttribute('data-key'));
  assert.deepEqual(keys, ['nodes-this', 'p:ccboard', 'nodes-other', 'n:build-box', 'n:alice-mac', 'n:old-laptop']);
  assert.deepEqual(all(tree, '.nd-sbh').map(text), ['This node', 'Other nodes']);
  const groups = all(tree, '.nd-group');
  assert.deepEqual(groups.map((g) => g._r.row.getAttribute('aria-expanded')), ['false', 'false', 'false'], 'collapsed');
  assert.deepEqual(groups.map((g) => text(g._r.name)), ['build-box', 'alice-mac', 'old-laptop']);
  assert.deepEqual(groups.map((g) => text(g._r.cnt)), ['1', '1', '1'], 'the needs-you badge');
  assert.deepEqual(groups.map((g) => text(g._r.sg)), ['●', '◐', '○']);
  assert.equal(groups[0].querySelector('.tn-go').getAttribute('href'), '#/n/build-box');
  assert.equal(groups[0]._r.kids.children.length, 0, 'nothing under a closed group');
  w.run('Shell.toggle("n:build-box")');
  const kids = all(tree, '.nd-group')[0]._r.kids.children;
  assert.deepEqual(kids.map((k) => k.getAttribute('data-key')), ['s:build-box/shop--api--s1', 's:build-box/shop--api--s2']);
  assert.deepEqual(kids.map((k) => k.getAttribute('href')), ['#/n/build-box/s/shop--api--s1', '#/n/build-box/s/shop--api--s2']);
  assert.deepEqual(kids.map((k) => k.getAttribute('data-node')), ['build-box', 'build-box']);
  assert.equal(kids[0].classList.contains('attn'), true, 'the session that needs you comes first and is marked');
});

test('the sidebar lists at most 20 sessions of a node and then "more" to its page; a node with none says so', async () => {
  const big = rec('big');
  big.state.sessions = Array.from({ length: 23 }, (_, i) => ({ tmux: `shop--api--m${i}`, project: 'shop', repo: 'api', session: `m${i}`, agent: 'claude', state: 'idle', needs_you: false, kind: 'user', since: iso(i * 10), model: null }));
  const empty = rec('empty');
  empty.state.sessions = [];
  const { w } = await ready([big, empty]);
  w.run('Shell.refs = {}; Shell.buildSide(document.querySelector("#sidebar"), false); Shell.open.add("n:big"); Shell.open.add("n:empty"); Shell.patchTrees()');
  const groups = all(w.document.querySelector('#sidebar'), '.nd-group');
  const kids = groups[0]._r.kids.children;
  assert.equal(kids.length, 21);
  assert.equal(all(groups[0]._r.kids, 'a[data-node]').length, 20);
  const more = kids[20];
  assert.deepEqual([text(more), more.getAttribute('href')], ['more (3)', '#/n/big']);
  assert.equal(text(groups[1]._r.kids), 'no live session');
});

test('the same session name on this board and on a node is two sidebar rows with two keys', async () => {
  const { w } = await ready([rec('build-box')]);
  setState(w, boardState({ projects: w.run('state.projects') }));
  w.run('Shell.refs = {}; Shell.buildSide(document.querySelector("#sidebar"), false); Shell.open.add("n:build-box"); Shell.open.add("p:ccboard"); Shell.patchTrees()');
  const keys = all(w.document.querySelector('#sidebar'), 'a.s-row').map((n) => n.getAttribute('data-key'));
  assert.ok(keys.includes('s:ccboard--ccboard--s1') && keys.includes('s:build-box/shop--api--s1'));
  assert.equal(new Set(keys).size, keys.length);
});

test('the sidebar of a board whose view is off, or whose nodes are all gone, is the tree as before', async () => {
  const { w } = await ready();
  setState(w, boardState({ nodes_enabled: false }));
  w.run('Shell.refs = {}; Shell.buildSide(document.querySelector("#sidebar"), false); Shell.patchTrees()');
  assert.deepEqual(w.document.querySelector('#sidebar .tree').children.map((n) => n.getAttribute('data-key')), ['p:ccboard']);
  assert.equal(all(w, '.nd-sbh, .nd-group').length, 0);
  assert.equal(w.run('Nodes.sbItems(["x"]).length'), 1);
});

// ---------------------------------------------------------------- the palette and g n

const palRows = (w) => all(w.document.querySelector('#helpdlg'), '.pal-group, .pal-item').map((n) => (n.classList.contains('pal-group') ? `## ${text(n)}` : text(n)));
const typeIn = (w, q) => { w.ctx.__q = q; w.run('Palette.ui.input.value = __q; Palette.render()'); };

test('the palette: a Nodes group (Go to, Open board on), remote sessions with their chip after this board\'s, remote tasks while searching', async () => {
  const { w } = await ready();
  w.run('Palette.open()');
  const rows = palRows(w);
  assert.ok(rows.includes('## Nodes'));
  const nodes = rows.slice(rows.indexOf('## Nodes') + 1);
  assert.ok(nodes[0].startsWith('Go to build-box') && nodes[0].endsWith('onlineg n'), nodes[0]);
  assert.ok(nodes[1].startsWith('Open board on build-box') && nodes[1].includes('new tab'));
  assert.ok(nodes.some((r) => r.startsWith('Go to alice-mac') && r.includes('stale 3 min')));
  const sess = all(w, '.pal-item').filter((n) => n.querySelector('.nchip'));
  assert.ok(sess.length >= 6, 'the sessions of three nodes carry their chip');
  assert.ok(!rows.includes('## Tasks on other nodes'), 'tasks of other nodes only while searching');
  typeIn(w, 'pagination');
  assert.ok(palRows(w).includes('## Tasks on other nodes'));
  w.run('Palette.ui.sel = Palette.ui.shown.findIndex((i) => i.id.startsWith("t:build-box")); Palette.runSelected({})');
  assert.equal(w.location.hash, '#/n/build-box/t/7');
});

test('the palette reaches a node or a remote session in two keystrokes after it opens: a few letters, Enter', async () => {
  const zeta = rec('alice-mac');
  zeta.state.sessions = [{ ...zeta.state.sessions[0], tmux: 'shop--web--zeta', session: 'zeta', repo: 'web' }];
  const { w } = await ready([rec('build-box'), zeta]);
  w.run('Palette.open()');
  const key = (k) => w.run(`Palette.key({ key: ${JSON.stringify(k)}, preventDefault() {} })`);
  typeIn(w, 'alice');
  key('Enter');
  assert.equal(w.location.hash, '#/n/alice-mac', 'a node: its name, Enter');
  w.run('Palette.open()');
  typeIn(w, 'zeta');
  assert.equal(w.run('Palette.ui.shown[Palette.ui.sel].id'), 's:alice-mac/shop--web--zeta');
  key('Enter');
  assert.equal(w.location.hash, '#/n/alice-mac/s/shop--web--zeta');
});

test('@box narrows Sessions and the tasks of other nodes to that node and filters by the rest; a handle, a name or a unique start work; @local is this board; an unknown @word is searched as typed', async () => {
  const { w } = await ready();
  w.run('Palette.open()');
  typeIn(w, '@build-box');
  assert.deepEqual(palRows(w).filter((r) => r.startsWith('##')), ['## Sessions', '## Tasks on other nodes']);
  assert.ok(all(w, '.pal-item').every((n) => /build-box/.test(text(n))));
  typeIn(w, '@build s2');
  assert.deepEqual(all(w, '.pal-item').map((n) => text(n).replace(/\s+/g, ' ')).filter((t) => /s2/.test(t)).length > 0, true);
  assert.ok(all(w, '.pal-item').every((n) => text(n).includes('build-box')), 'a unique start of the handle names the node');
  assert.equal(all(w, '.pal-item').length, 1, 'only the session that matches s2');
  typeIn(w, '@local');
  assert.ok(all(w, '.pal-item').length > 0 && all(w, '.pal-item').every((n) => !n.querySelector('.nchip')), 'this board\'s own sessions');
  typeIn(w, '@a');
  assert.ok(all(w, '.pal-item').every((n) => /alice-mac/.test(text(n))), '@a is alice-mac');
  typeIn(w, '@zzz fix');
  assert.ok(palRows(w).some((r) => r.startsWith('Search transcripts for "@zzz fix"')), 'not a node: the whole text is searched');
  assert.equal(w.run('Palette.nodePrefix("@")'), null);
  assert.equal(w.run('Palette.nodePrefix("a @box")'), null);
  assert.deepEqual(plain(w.run('Palette.nodePrefix("@build-box   fix the login")')), { node: 'build-box', rest: 'fix the login' });
});

test('a node cannot take another node\'s @handle by naming itself after it; a shared name names nobody; a start matches handles only', async () => {
  const { w } = await ready([rec('evil', { name: 'build-box' }), rec('build-box'), rec('twin-a', { name: 'twin' }), rec('twin-b', { name: 'twin' })]);
  assert.deepEqual(plain(w.run('Palette.nodePrefix("@build-box fix")')), { node: 'build-box', rest: 'fix' }, 'the exact handle wins over an earlier node that calls itself build-box');
  assert.equal(w.run('Palette.nodePrefix("@twin")'), null, 'two nodes share the name: it names neither');
  assert.deepEqual(plain(w.run('Palette.nodePrefix("@twin-b")')), { node: 'twin-b', rest: '' });
  assert.deepEqual(plain(w.run('Palette.nodePrefix("@build")')), { node: 'build-box', rest: '' }, 'a start matches handles only: evil\'s name build-box does not make @build ambiguous');
});

test('a remote session\'s palette row uses Palette.sessionHash through Ref; Enter on the Open board row opens a plain https link with no opener', async () => {
  const { w } = await ready([rec('build-box')]);
  assert.equal(w.run("Palette.sessionHash({ tmux: 'shop--api--s1', node: 'build-box' })"), '#/n/build-box/s/shop--api--s1');
  assert.equal(w.run("Palette.sessionHash('shop--api--s1')"), '#/s/shop--api--s1');
  w.run('Palette.open()');
  w.run('Palette.ui.sel = Palette.ui.shown.findIndex((i) => i.id === "nb:build-box"); Palette.runSelected({})');
  assert.deepEqual(plain(w.get('__opened')), [['https://build-box.example.ts.net/', '_blank', 'noopener,noreferrer']]);
});

test('the palette of a board with the view off has no Nodes group, no @ prefix and no remote rows', async () => {
  const { w } = hubWorld({ state: boardState({ nodes_enabled: false }), nodes: three() });
  w.run('Palette.open()');
  assert.ok(!palRows(w).includes('## Nodes'));
  assert.equal(w.run('Palette.nodePrefix("@build-box")'), null);
  assert.equal(all(w, '.nchip').length, 0);
});

test('g n goes to the first node page; it needs the hub view, ignores typing in a field, and is in the shortcut help', async () => {
  const { w } = await ready();
  w.run('Keymap.install()');
  w.run('Keymap.now = () => 1000');
  const ev = (key, over = {}) => ({ key, ctrlKey: false, metaKey: false, shiftKey: false, altKey: false, target: null, defaultPrevented: false, preventDefault() {}, ...over });
  const K = w.get('Keymap');
  K.handle(ev('g'));
  assert.ok(K.handle(ev('n')));
  assert.equal(w.location.hash, '#/n/build-box');
  w.location.hash = '#/';
  K.handle(ev('g'));
  assert.equal(K.handle(ev('n', { target: { tagName: 'INPUT' } })), null, 'not inside a text field');
  assert.equal(w.location.hash, '#/');
  K.handle(ev('g', { target: { tagName: 'TEXTAREA' } }));
  assert.equal(K.handle(ev('n', { target: { tagName: 'TEXTAREA' } })), null);
  assert.ok(plain(K.help()).some((h) => h.keys.join(' ') === 'g n' && /first paired node/.test(h.help)));
  setState(w, boardState({ nodes_enabled: false }));
  K.handle(ev('g'));
  assert.equal(K.handle(ev('n')), null, 'with the view off the chord does nothing');
});

test('g n with a model that has no node yet says so instead of navigating', async () => {
  const { w } = hubWorld({ state: boardState(), nodes: [] });
  await poll(w);
  w.run('Keymap.install(); Keymap.now = () => 1000');
  const K = w.get('Keymap');
  K.handle({ key: 'g', preventDefault() {} });
  K.handle({ key: 'n', preventDefault() {} });
  assert.equal(w.location.hash, '');
  assert.match(plain(w.get('__toasts'))[0].text, /No node is paired yet/);
});

// ---------------------------------------------------------------- Settings

test('Settings, Nodes: the read model\'s line under a paired node: status with its age, the skew warning above 5 s, the agents and the accounts with their windows', async () => {
  const { w } = await ready();
  const line = (h) => w.run(`Nodes.settingsLine(${JSON.stringify(h)})`);
  assert.equal(line('nobody'), null);
  const a = line('alice-mac');
  assert.match(text(a), /^◐stale 3 minlast reading 3 min agoclock differs by 7 sclaude ready1 account: claude Work 40%$/);
  const b = line('build-box');
  assert.doesNotMatch(text(b), /clock differs/, 'no warning for an honest clock');
  assert.match(text(b), /^●onlineclaude ready1 account/);
  assert.match(text(line('old-laptop')), /○offline since \d\d:\d\d.*last reading 6 h ago/);
});

// ---------------------------------------------------------------- the bundle

test('the hub view is a lazy bundle: nodes-hub.js, pages/node.js and their sheet, loaded for the three node routes; index.html carries none of it', () => {
  const lazy = fs.readFileSync(path.join(STATIC, 'lazy.js'), 'utf8');
  assert.match(lazy, /nodeshub: \{ js: \['\/static\/nodes-hub\.js', '\/static\/pages\/node\.js'\], css: \[\{ href: '\/static\/pages\/nodes\.css', rank: 14 \}\]/);
  assert.match(lazy, /node: 'nodeshub', 'node-session': 'nodeshub', 'node-task': 'nodeshub'/);
  const html = fs.readFileSync(path.join(STATIC, 'index.html'), 'utf8');
  assert.ok(!/nodes-hub|pages\/node\.js/.test(html));
  const eager = fs.readFileSync(path.join(STATIC, 'nodes.js'), 'utf8');
  for (const lazyOnly of ['Nodes.poll', 'Nodes.slot', 'Nodes.sbItems', 'nhFetch']) assert.ok(!eager.includes(lazyOnly + ' ='), `${lazyOnly} lives in nodes-hub.js`);
});

test('the lazy loader loads the bundle once for a node address and the eager hook asks for it only with the view on', async () => {
  const h = hubWorld({ state: boardState(), nodes: three(), scripts: false });
  const { w } = h;
  for (const f of ['core.js', 'components.js', 'keymap.js', 'lazy.js']) w.load(f);
  w.run('globalThis.__loads = []; Lazy.load = (n) => { __loads.push(n); return Promise.resolve(); };');
  setState(w, boardState({ nodes_enabled: false }));
  assert.equal(w.run('Nodes.use(() => 1)'), false);
  assert.deepEqual(plain(w.get('__loads')), [], 'view off: the bundle is not even asked for');
  setState(w, boardState());
  assert.equal(w.run('Nodes.use(() => 1)'), true);
  assert.deepEqual(plain(w.get('__loads')), ['nodeshub']);
  assert.deepEqual(plain(w.run("['node', 'node-session', 'node-task'].map((id) => Lazy.pending(id))")), [['nodeshub'], ['nodeshub'], ['nodeshub']]);
});

test('Tasks filter with more than four options is a native select labelled Node; up to four stays a segmented control; the choice is remembered', async () => {
  const { w } = await ready([rec('build-box'), rec('alice-mac'), rec('old-laptop')]);
  await go(w, '#/tasks');
  const bar = w.document.querySelector('.nd-tfilter');
  assert.equal(all(bar, '.seg-btn').length, 0, 'five options: no segmented control, so no label wraps');
  const sel = bar.querySelector('select');
  assert.equal(sel.getAttribute('aria-label'), 'Node');
  assert.equal(text(bar.querySelector('label')).startsWith('Node'), true);
  assert.deepEqual(all(sel, 'option').map((o) => [o.getAttribute('value'), text(o)]), [['all', 'All nodes'], ['local', 'This node'], ['build-box', 'build-box'], ['alice-mac', 'alice-mac'], ['old-laptop', 'old-laptop']]);
  assert.equal(sel.value, 'all');
  const board = w.document.querySelector('#tasks');
  sel.value = 'alice-mac';
  sel.dispatchEvent({ type: 'change' });
  assert.equal(w.localStorage.getItem('ccboard:nodes:tfilter'), 'alice-mac');
  assert.match(text(w.document.querySelector('.nd-tasks h2')), /^Tasks on alice-mac \(1\)$/);
  assert.ok(board.classList.contains('nd-hide'));
  assert.equal(bar.querySelector('select'), sel, 'the same select stays (focus is not lost)');
  assert.equal(sel.value, 'alice-mac');
  answer(w, 200, { nodes: [rec('build-box'), rec('alice-mac')], at: iso(0) }, 'W/"4"');
  await poll(w);
  assert.equal(all(bar, 'select').length, 0, 'four options again: segmented');
  assert.deepEqual(all(bar, '.seg-btn').map(text), ['All nodes', 'This node', 'build-box', 'alice-mac']);
  assert.equal(bar.querySelector('[aria-pressed=true]').textContent, 'alice-mac');
});
