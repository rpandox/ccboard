// Contract tests for issue #82 (the owner's decision of 2026-10-17: "also list Mac sessions on Home"): Home lists the sessions of the paired nodes, read only, in one collapsible
// block "Other nodes" after this board's own projects (app/static/nodes-hub.js SLOTS.sessions and Nodes.homeGroups, hooked in by pages/home.js). Built from the hub's trimmed read model
// (Nodes.M) alone: no new request, no call to a node. Real scripts on minidom's DOM (tests/js/hubworld.mjs); ?demo=1 reads demo/nodes.json.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC } from './harness.mjs';
import { boardState, hubWorld, iso, page, plain, poll, rec, setState, text, tick } from './hubworld.mjs';

const HOSTILE = '<img src=x onerror=alert(1)>';
const three = () => [rec('build-box'), rec('alice-mac', { status: 'stale', age_s: 190 }), rec('old-laptop', { status: 'offline', age_s: 21600, last_ok_at: iso(21600), error_kind: 'refused' })];
const ready = async (nodes = three()) => { const h = hubWorld({ state: boardState(), nodes }); await poll(h.w); return h; };
const go = async (w, hash) => { w.location.hash = hash; await tick(); };
const all = (n, sel) => (n.document || n).querySelectorAll(sel);
const noMarkup = (w) => assert.equal(all(w, 'img, iframe, script').length, 0, 'peer text never becomes markup');
const othersOf = (w) => w.document.querySelector('.nd-others');
const rowsOf = (g) => all(g, '.nd-item').map((r) => [text(r.querySelector('.nd-name')), r.querySelector('a.nd-name') && r.querySelector('a.nd-name').getAttribute('href')]);

const demoHome = async () => {
  const { w } = hubWorld({ search: '?demo=1', realApi: true });
  setState(w, plain(await w.run("api('GET', '/api/state')")));
  await poll(w);
  await go(w, '#/');
  w.run('updateCurrentPage(state)');
  return w;
};

test('Home lists the sessions of the paired nodes in one block "Other nodes" after the projects: a group for each node, rows that do not need you (those stay in the inbox)', async () => {
  const w = await demoHome();
  const sec = othersOf(w);
  assert.ok(sec && !sec.classList.contains('hidden'));
  assert.equal(text(sec.querySelector('h2')), 'Other nodes');
  assert.equal(text(sec.querySelector('.pb-count')), '4 sessions on 3 nodes');
  const kids = page(w).children;
  assert.ok(kids.indexOf(sec) > kids.findIndex((n) => n.classList.contains('pblocks')), 'after this board\'s own projects');
  const groups = all(sec, '.nd-ngrp');
  assert.deepEqual(groups.map((g) => g.getAttribute('data-node')), ['build-box', 'alice-mac', 'old-laptop']);
  assert.deepEqual(groups.map((g) => text(g.querySelector('.nd-ngn'))), ['build-box', 'alice-mac', 'old-laptop']);
  assert.deepEqual(groups.map((g) => g.querySelector('.nd-ngn').getAttribute('href')), ['#/n/build-box', '#/n/alice-mac', '#/n/old-laptop']);
  assert.deepEqual(rowsOf(groups[0]), [['t-cart-page', '#/n/build-box/s/shop--web--t-cart-page'], ['s3', '#/n/build-box/s/shop--api--s3']],'working before idle; the two that need you are in the inbox, not here');
  assert.deepEqual(rowsOf(groups[1]), [['t-rss', '#/n/alice-mac/s/notes--site--t-rss']]);
  assert.deepEqual(rowsOf(groups[2]), [['s1', '#/n/old-laptop/s/scratch--play--s1']]);
  for (const r of all(sec, '.nd-item')) {
    assert.ok(r.querySelector('.nchip-n'), 'the node chip');
    assert.ok(r.querySelector('.glyph'), 'the state glyph');
    assert.ok(text(r.querySelector('.nd-where')).includes('/'), 'project / repo');
    assert.match(text(r.querySelector('.nd-meta')), /\d+[smhd]\b/, 'an age');
    assert.ok(!r.classList.contains('attn'), 'a session that needs you is not listed twice');
  }
  assert.match(text(w.document.querySelector('.nd-inbox')), /s1/, 'the needs-you entries are where they were');
  assert.equal(all(sec, '.primary').length, 0, 'no filled primary');
  noMarkup(w);
});

test('Home: a stale node\'s rows say how old its reading is, an offline node\'s last reading is dimmed with the age; an online node is plain', async () => {
  const w = await demoHome();
  const [online, stale, off] = all(othersOf(w), '.nd-ngrp');
  assert.ok(!online.classList.contains('stale'));
  assert.equal(text(online.querySelector('.nd-ngago')), '');
  assert.equal(text(online.querySelector('.nd-ngw')), 'online');
  assert.ok(stale.classList.contains('stale'));
  assert.equal(text(stale.querySelector('.nd-ngw')), 'stale 3 min');
  assert.equal(text(stale.querySelector('.nd-ngago')), 'last reading 3 min ago');
  assert.match(text(stale.querySelector('.nd-item .nd-meta')), /stale: last reading 3 min ago/);
  assert.equal(off.getAttribute('data-status'), 'offline');
  assert.ok(off.classList.contains('stale'));
  assert.equal(text(off.querySelector('.nd-ngago')), 'last reading 6 h ago');
  assert.match(text(off.querySelector('.nd-item .nd-meta')), /stale: last reading 6 h ago/);
  assert.ok(all(off, '.nd-item').every((r) => r.classList.contains('stale')), 'the rows are the dimmed ones');
});

test('Home: the rows come from the hub model alone; painting the section asks no node and no route', async () => {
  const { w } = await ready();
  await go(w, '#/');
  const before = plain(w.get('__fetches')).length;
  w.run('updateCurrentPage(state)');
  w.run('updateCurrentPage(state)');
  assert.ok(othersOf(w) && all(othersOf(w), '.nd-item').length > 0);
  assert.equal(plain(w.get('__fetches')).length, before, 'no request at all to paint the section');
  assert.ok(plain(w.get('__fetches')).every((f) => f.url === '/api/nodes/state'), 'only the one read of the hub that was there');
});

test('Home: a state chip narrows the node rows to that state, and the block goes when nothing is left', async () => {
  const w = await demoHome();
  await go(w, '#/?f=working');
  w.run('updateCurrentPage(state)');
  assert.deepEqual(all(othersOf(w), '.nd-ngrp').map((g) => g.getAttribute('data-node')), ['build-box', 'alice-mac']);
  assert.deepEqual(all(othersOf(w), '.nd-item').map((r) => text(r.querySelector('.nd-name'))), ['t-cart-page', 't-rss']);
  await go(w, '#/?f=done');
  w.run('updateCurrentPage(state)');
  assert.ok(othersOf(w).classList.contains('hidden'));
  await go(w, '#/');
  w.run('updateCurrentPage(state)');
  assert.equal(all(othersOf(w), '.nd-item').length, 4);
});

test('Home: the block folds like the other blocks and the choice is remembered per viewer in the Home collapsed set', async () => {
  const w = await demoHome();
  assert.equal(othersOf(w).querySelector('.pb-toggle').getAttribute('aria-expanded'), 'true');
  othersOf(w).querySelector('.pb-toggle').click();
  assert.deepEqual(JSON.parse(w.localStorage.getItem('ccboard:home:collapsed')), ['older', 'x:nodes']);
  assert.equal(all(othersOf(w), '.nd-item').length, 0, 'folded: no rows');
  assert.equal(othersOf(w).querySelector('.pb-toggle').getAttribute('aria-expanded'), 'false');
  assert.ok(othersOf(w).classList.contains('collapsed'));
  assert.equal(text(othersOf(w).querySelector('.pb-count')), '4 sessions on 3 nodes', 'the count stays');
  othersOf(w).querySelector('.pb-toggle').click();
  assert.equal(all(othersOf(w), '.nd-item').length, 4);
  assert.deepEqual(JSON.parse(w.localStorage.getItem('ccboard:home:collapsed')), ['older']);
});

test('Home: more than ten sessions on a node show ten and "Show all" to the node page; a node with none, a node of another device and a view that is off add nothing', async () => {
  const many = rec('build-box');
  many.state.sessions = Array.from({ length: 13 }, (_, i) => ({ tmux: `shop--api--n${i}`, project: 'shop', repo: 'api', session: `n${i}`, agent: 'claude', state: 'idle', needs_you: false, kind: 'user', since: iso(60 + i), model: null }));
  const empty = rec('alice-mac');
  empty.state.sessions = [];
  const other = rec('old-laptop', { status: 'unpaired', error_kind: 'identity_changed' });
  const { w } = await ready([many, empty, other]);
  await go(w, '#/');
  w.run('updateCurrentPage(state)');
  const groups = all(othersOf(w), '.nd-ngrp');
  assert.equal(groups.length, 1);
  assert.equal(all(groups[0], '.nd-item').length, 10);
  assert.equal(text(othersOf(w).querySelector('.pb-count')), '13 sessions on 1 node');
  const more = groups[0].querySelector('.nd-ngmore');
  assert.deepEqual([text(more), more.getAttribute('href'), more.classList.contains('hidden')], ['Show all 13', '#/n/build-box', false]);

  const off = hubWorld({ state: boardState({ nodes_enabled: false }), nodes: [many] });
  await poll(off.w);
  await go(off.w, '#/');
  off.w.run('updateCurrentPage(state)');
  assert.equal(othersOf(off.w), null);
});

test('Home: hostile session, project and node names stay text in the node rows', async () => {
  const bad = rec('evil', { name: HOSTILE });
  bad.state.sessions = [{ tmux: 'shop--api--x', project: HOSTILE, repo: 'api', session: HOSTILE, agent: 'claude', state: 'idle', needs_you: false, kind: 'user', since: iso(30), model: null }];
  const { w } = await ready([bad]);
  await go(w, '#/');
  w.run('updateCurrentPage(state)');
  assert.ok(text(othersOf(w)).includes(HOSTILE));
  noMarkup(w);
});

test('the node rows live in the lazy hub bundle: home.js only hands the slot its anchor and the collapse key', () => {
  const hub = fs.readFileSync(path.join(STATIC, 'nodes-hub.js'), 'utf8');
  const home = fs.readFileSync(path.join(STATIC, 'pages', 'home.js'), 'utf8');
  assert.match(hub, /Nodes\.homeGroups = function/);
  assert.match(home, /Nodes\.use\(\(\) => Nodes\.slot\('sessions'/);
  assert.doesNotMatch(home, /homeGroups|nhHomeGroupNode/);
});
