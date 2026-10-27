// Contract tests for the Team page of issue #200 (app/static/pages/team.js with pages/team-view.css, the eager hook in core.js, and the hooks in shell.js, home.js, palette.js and
// pages/settings.js): #/team with its grouping and filters, the free issues in words, the sidebar group, the Home line, the palette rows, the notification toggle and the demo.
// Real scripts on minidom's DOM (tests/js/world.mjs). The answers of /api/team/state are the demo fixture (app/static/demo/team.json) as the demo itself makes them.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { afterEach, test } from 'node:test';
import { STATIC, makeWorld, plain } from './harness.mjs';
import { installDom } from './minidom.mjs';
import { calls, fakeState, fixtureState, homeWorld, page, text, tick } from './world.mjs';

const FX = JSON.parse(fs.readFileSync(path.join(STATIC, 'demo', 'team.json'), 'utf8'));
const OVERVIEWS = JSON.parse(fs.readFileSync(path.join(path.dirname(new URL(import.meta.url).pathname), '..', 'fixtures', 'team_demo_overviews.json'), 'utf8'));
const clone = (v) => JSON.parse(JSON.stringify(v));
const html = fs.readFileSync(path.join(STATIC, 'index.html'), 'utf8');
const INDEX_SCRIPTS = [...html.matchAll(/<script (?:defer )?src="([^"]+)"/g)].map((m) => m[1]);
const settle = async (n = 8) => { for (let i = 0; i < n; i++) await tick(); };
const made = [];
afterEach(() => { for (const w of made.splice(0)) { try { w.run('if (typeof TeamView !== "undefined") TeamView.stop(); for (const t of teamStore.soon.splice(0)) clearTimeout(t); clearTimeout(teamStore.timer); teamStore.subs.clear();'); } catch (_) { /* never loaded */ } } });

/** What the demo answers for /api/team/state at `search` (the same code the visitor runs), as plain data with the times of now. */
async function demoBody(search = '?demo=1') {
  const w = makeWorld({
    fetch: async (url) => {
      const m = /^\/static\/demo\/(\w+)\.json$/.exec(String(url));
      if (!m) throw new Error(`demo mode asked for ${url}`);
      const raw = fs.readFileSync(path.join(STATIC, 'demo', `${m[1]}.json`), 'utf8');
      return { ok: true, status: 200, statusText: 'OK', json: async () => JSON.parse(raw) };
    },
  });
  w.location.search = search;
  w.load('core.js');
  w.load('team.js');
  return plain(await w.run("api('GET', '/api/team/state')"));
}

/** A world on the Team page: home, router and the pages, team.js and pages/team.js loaded the way their bundles load; GET /api/team/state answers __team. */
function pageWorld({ team = null, shared = true } = {}) {
  const { w } = homeWorld({ state: fakeState(shared ? { team: { shared: true } } : {}) });
  made.push(w);
  w.ctx.__team = team;
  w.run("__answers['/api/team/state'] = () => JSON.parse(JSON.stringify(__team));");
  w.load('team.js');
  w.load('pages/team.js');
  return w;
}

const rowsOf = (w) => page(w).querySelectorAll('.tv-row');
const words = (n) => text(n).replace(/\s+/g, ' ').trim();
const groupNames = (w) => page(w).querySelectorAll('.tv-work .tv-gname').map(text);
const choose = (sel, v) => { sel.value = v; sel.dispatchEvent({ type: 'change' }); };
const filterSel = (w, label) => page(w).querySelector(`.tv-filters select[aria-label="${label}"]`);

// ---------------------------------------------------------------- the address

test('#/team parses and builds like the other lazy address: ROUTES keeps its twelve ids and the page is a lazy route', () => {
  const { w } = homeWorld({ state: fakeState() });
  assert.equal(w.run("parseHash('#/team').id"), 'team');
  assert.deepEqual(plain(w.run("parseHash('#/team?by=repo&person=id%3A1002&state=working').query")), { by: 'repo', person: 'id:1002', state: 'working' });
  assert.equal(w.run("buildHash('team', {}, { by: 'repo', person: null })"), '#/team?by=repo');
  assert.equal(w.run("buildHash('team')"), '#/team');
  assert.equal(w.run("parseHash('#/team/x').unknown"), true, 'only the bare address');
  assert.deepEqual(plain(w.run('ROUTES.map((r) => r.id)')), ['home', 'inbox', 'tasks', 'agents', 'project', 'quad', 'usage', 'memory', 'onboarding', 'settings', 'search', 'session']);
  const lazy = fs.readFileSync(path.join(STATIC, 'lazy.js'), 'utf8');
  assert.match(lazy, /teamview: \{ needs: \['team'\], js: \['\/static\/pages\/team\.js'\], css: \[\{ href: '\/static\/pages\/team-view\.css', rank: 117 \}\]/);
  assert.match(lazy, /team: 'teamview' \}/);
});

// ---------------------------------------------------------------- the demo: its list is the model's list

test('the overview the demo makes of every variant is what app/team.py makes of it (tests/fixtures/team_demo_overviews.json)', async () => {
  const norm = (o) => ({ ...o, rows: o.rows.map((r) => ({ ...r, since: '', last: '', read_at: '' })) });      // the demo moves every time with the clock; the order does not change
  assert.deepEqual(Object.keys(OVERVIEWS).sort(), ['base', ...Object.keys(FX.variants)].sort());
  for (const name of ['base', ...Object.keys(FX.variants)]) {
    const body = await demoBody(name === 'base' ? '?demo=1' : `?demo=1&team=${name}`);
    assert.deepEqual(norm(body.overview), norm(OVERVIEWS[name]), `variant ${name}`);
  }
});

test('in the demo a repository switched on or off changes the list: the counts follow, the rows of an unshared repository go', async () => {
  const w = makeWorld({ fetch: async (url) => ({ ok: true, status: 200, statusText: 'OK', json: async () => JSON.parse(fs.readFileSync(path.join(STATIC, 'demo', `${/(\w+)\.json$/.exec(url)[1]}.json`), 'utf8')) }) });
  w.location.search = '?demo=1';
  w.load('core.js');
  w.load('team.js');
  const on = plain(await w.run("api('PUT', '/api/team/repos/example/api', { shared: true }).then(() => api('GET', '/api/team/state'))"));
  assert.equal(on.overview.counts.shared, 2);
  assert.deepEqual(on.overview.counts.free, { count: 0, ok: 0, unread: 2, unknown: 0 });
  assert.equal(on.overview.rows.length, 8, 'the new repository has nobody in it');
  const off = plain(await w.run("api('PUT', '/api/team/repos/example/website', { shared: false }).then(() => api('GET', '/api/team/state'))"));
  assert.equal(off.overview.counts.shared, 1);
  assert.deepEqual(off.overview.rows, []);
  assert.equal(off.overview.counts.live, 0);
});

// ---------------------------------------------------------------- the page

test('#/team groups by person, the owner first, with every row in words: state, what it is, repo, branch, agent, since, last seen, and the age of the read', async () => {
  const w = pageWorld({ team: await demoBody() });
  w.location.hash = '#/team';
  await settle();
  assert.equal(text(page(w).querySelector('h1')), 'Team');
  assert.deepEqual(groupNames(w), ['@alice', '@bob']);
  const rows = rowsOf(w);
  assert.equal(rows.length, 8);
  const alice = rows[0];
  assert.equal(alice.getAttribute('data-state'), 'working');
  assert.match(words(alice), /Working/);
  assert.match(words(alice), /#148/);
  assert.match(words(alice), /Checkout: show the shipping total before payment/);
  assert.match(words(alice), /Repo example\/website/);
  assert.match(words(alice), /Branch alice\/shipping-total/);
  assert.match(words(alice), /Agent .* claude/);
  assert.match(words(alice), /Since \d\d:\d\d|Since \w{3} \d+ \d\d:\d\d/);
  assert.match(words(alice), /Last seen \d+ min ago|Last seen just now/);
  assert.match(words(alice), /Read (just now|\d+ (min|h) ago)/);
  const gh = rows.filter((r) => r.getAttribute('data-state') === 'github');
  assert.equal(gh.length, 5, 'work that GitHub shows, from a person who publishes no live status for it');
  assert.match(words(gh[0]), /On GitHub/);
  const heads = page(w).querySelectorAll('.tv-work .tv-gh').map(words);
  assert.match(heads[0], /@alice you .*Live/);
  assert.match(heads[1], /@bob .*Live .*1 repo · 7 rows/);
  assert.equal(page(w).querySelectorAll('.tv-by .seg-btn').filter((b) => b.getAttribute('aria-pressed') === 'true').map(text).join(), 'Person');
  assert.equal(page(w).querySelector('.tv-count').textContent.trim(), '8 rows from 2 people in 1 repo');
});

test('group by repo is a segmented control that the address carries; the filters narrow the rows, say how many are left, and clear', async () => {
  const w = pageWorld({ team: await demoBody() });
  w.location.hash = '#/team';
  await settle();
  const repoBtn = page(w).querySelectorAll('.tv-by .seg-btn').find((b) => text(b) === 'Repo');
  repoBtn.click();
  await settle();
  assert.equal(w.location.hash, '#/team?by=repo');
  assert.deepEqual(groupNames(w), ['example/website']);
  assert.match(words(page(w).querySelector('.tv-work .tv-gh')), /Read .* 8 rows|read .* · 8 rows/);
  assert.equal(repoBtn.getAttribute('aria-pressed'), 'true');
  assert.match(words(rowsOf(w)[0]), /Person @alice \(you\)/, 'by repo the person is the cell');
  choose(filterSel(w, 'Person'), 'id:1002');
  await settle();
  assert.equal(w.location.hash, '#/team?by=repo&person=id%3A1002');
  assert.equal(rowsOf(w).length, 7);
  assert.equal(page(w).querySelector('.tv-count').textContent.trim(), '7 rows of 8 from 1 person in 1 repo');
  choose(filterSel(w, 'State'), 'working');
  await settle();
  assert.equal(rowsOf(w).length, 1);
  assert.match(words(rowsOf(w)[0]), /#152/);
  choose(filterSel(w, 'State'), 'ended');
  await settle();
  assert.equal(rowsOf(w).length, 0);
  assert.match(words(page(w).querySelector('.tv-work')), /No rows match these filters/);
  page(w).querySelector('.tv-work button').click();
  await settle();
  assert.equal(w.location.hash, '#/team?by=repo');
  assert.equal(rowsOf(w).length, 8);
  w.location.hash = '#/team?person=id%3A1001&repo=example%2Fwebsite';
  await settle();
  assert.equal(rowsOf(w).length, 1, 'a link with filters shows the same screen');
  assert.equal(filterSel(w, 'Person').value, 'id:1001');
});

test('every state has a row in words and a glyph, never colour alone', async () => {
  const body = await demoBody();
  const template = body.overview.rows[0];
  const states = ['working', 'waiting', 'idle', 'unconfirmed', 'stale', 'ended', 'unknown', 'github'];
  body.overview.rows = states.map((s, i) => ({ ...template, person: 'id:1002', login: 'bob', you: false, kind: 'issue', issue: 100 + i, issue_url: `https://github.com/example/website/issues/${100 + i}`, title: `Row ${s}`, state: s, live: null, last_seen_s: null }));
  const w = pageWorld({ team: body });
  w.location.hash = '#/team';
  await settle();
  const chips = page(w).querySelectorAll('.tv-row .tv-state');
  assert.deepEqual(chips.map((c) => c.getAttribute('data-state')), states);
  assert.deepEqual(chips.map((c) => words(c)), ['✽ Working', '✻ Waiting', '∙ Idle', '◌ Not confirmed yet', '! Stale', '○ Ended', '? Unknown', '◦ On GitHub']);
  assert.deepEqual(page(w).querySelector('.tv-state[data-state=stale]').getAttribute('title'), 'The last live status or read is old, so this may have changed.');
});

test('a line that outlived its lease since the read is stale on the page, never live', async () => {
  const body = await demoBody();
  for (const b of body.repos) b.read_at = new Date(Date.now() - 3 * 3600 * 1000).toISOString();
  for (const r of body.overview.rows) r.read_at = body.repos[0].read_at;
  const w = pageWorld({ team: body });
  w.location.hash = '#/team';
  await settle();
  const states = rowsOf(w).map((r) => r.getAttribute('data-state'));
  assert.ok(!states.includes('working') && !states.includes('idle') && states.includes('stale'), states.join());
  assert.equal(w.run('TeamView.live(TeamView.data()).people'), 0, 'nobody is counted live from an old read');
});

test('the rows, the live count and the live people are worked out once for the same data, however many times the shell, Home and the palette ask', async () => {
  const { w } = lazyWorld({ state: fixtureState({ team: { shared: true } }), team: await demoBody() });
  w.location.hash = '#/';
  paint(w);
  await settle(30);
  w.run('globalThis.__built = 0; (() => { const real = tvRowsMake; tvRowsMake = (o) => { globalThis.__built++; return real(o); }; })();');
  w.run('teamStore.data = JSON.parse(JSON.stringify(teamStore.data));');         // an answer the poll just brought
  const paints = () => w.run('Shell.patchTrees(); Shell.patchTrees(); TeamView.homePaint(homePage.refs); TeamView.homePaint(homePage.refs); TeamView.live(TeamView.data()); TeamView.homeMessage();');
  paints();
  assert.equal(w.run('__built'), 1, 'two paints of the same data built the rows once');
  assert.equal(w.run('TeamView.rows(TeamView.data()) === TeamView.rows(TeamView.data())'), true);
  assert.equal(w.run('tvLivePeople(TeamView.data()) === tvLivePeople(TeamView.data())'), true);
  assert.equal(w.run('TeamView.live(TeamView.data()) === TeamView.live(TeamView.data())'), true);
  assert.equal(w.run('__built'), 1);
  w.run('teamStore.data = JSON.parse(JSON.stringify(teamStore.data));');
  paints();
  assert.equal(w.run('__built'), 2, 'another answer is worked out again, once');
});

test('the free issues are never guessed: unread says so and names what is held, unknown says why, ok lists them; nothing is listed as free when it is not known', async () => {
  let w = pageWorld({ team: await demoBody() });
  w.location.hash = '#/team';
  await settle();
  assert.match(words(page(w).querySelector('.tv-freewrap .tv-lede')), /an unassigned issue with a linked branch and no status line can show as free/);
  let sec = page(w).querySelector('.tv-free');
  assert.equal(sec.getAttribute('data-state'), 'unread');
  assert.match(words(sec), /The open issues of this repository have not been read yet, so free issues are not listed\./);
  assert.match(words(sec), /Held by somebody: #148, #152, #154, #156, #160, #162, #164/);
  assert.match(words(sec), /Next: Open the issue list in the launcher/);
  assert.equal(sec.querySelectorAll('.tv-issue').length, 0);
  w = pageWorld({ team: await demoBody('?demo=1&team=freeok') });
  w.location.hash = '#/team';
  await settle();
  sec = page(w).querySelector('.tv-free');
  assert.equal(sec.getAttribute('data-state'), 'ok');
  assert.deepEqual(sec.querySelectorAll('.tv-issue').map((i) => i.getAttribute('data-n')), ['140', '141', '143', '146']);
  assert.match(words(sec), /4 free issues, newest first/);
  assert.match(words(sec.querySelector('.tv-issue')), /#140 Footer links open in the same tab/);
  const cut = await demoBody('?demo=1&team=freeok');                      // the live reader's full page of 100: all of them listed, and the list may be longer
  cut.repos.find((r) => r.free && r.free.state === 'ok').free.truncated = true;
  w = pageWorld({ team: cut });
  w.location.hash = '#/team';
  await settle();
  assert.match(words(page(w).querySelector('.tv-free')), /4 free issues, newest first\. There may be more: the board reads only the first 100 open issues that have no assignee\./);
  w = pageWorld({ team: await demoBody('?demo=1&team=freecut') });
  w.location.hash = '#/team';
  await settle();
  assert.match(words(page(w).querySelector('.tv-free')), /4 of 63 free issues/);
  w = pageWorld({ team: await demoBody('?demo=1&team=freeempty') });
  w.location.hash = '#/team';
  await settle();
  assert.match(words(page(w).querySelector('.tv-free')), /No free issues: every open issue is held by somebody or has an assignee/);
  for (const [name, why] of [['pending', /Linked branches and pull request links are not read yet, so nobody can say which issues are free/], ['stale', /The last read of GitHub is old/], ['cut', /more issues, pull requests or branches than the read keeps/],
    ['sso', /needs this token authorised for the organization|Authorise/], ['loading', /Reading GitHub for the first time/], ['warnnone', /does not know its GitHub login yet/]]) {
    w = pageWorld({ team: await demoBody(`?demo=1&team=${name}`) });
    w.location.hash = '#/team';
    await settle();
    sec = page(w).querySelector('.tv-free');
    assert.ok(sec, `${name}: the section`);
    assert.equal(sec.getAttribute('data-state'), 'unknown', name);
    assert.match(words(sec), why, name);
    assert.equal(sec.querySelectorAll('.tv-issue').length, 0, `${name}: nothing is listed as free`);
  }
});

test('a repository that cannot be read is said in words with its one next step, and its rows are stale, not "no teammates"', async () => {
  const w = pageWorld({ team: await demoBody('?demo=1&team=sso') });
  w.location.hash = '#/team';
  await settle();
  assert.match(words(page(w).querySelector('.tv-notes')), /example\/website\s*Authorise this token for the organization/);
  assert.match(words(page(w).querySelector('.tv-notes')), /Next: .*Configure SSO/);
  assert.ok(rowsOf(w).every((r) => r.getAttribute('data-state') === 'stale' || r.getAttribute('data-state') === 'github'));
  assert.doesNotMatch(words(page(w)), /Nobody else shows up/);
});

test('with nothing shared the page is one sentence and one filled link to Settings, and it asks the board for nothing', async () => {
  const w = pageWorld({ team: await demoBody(), shared: false });
  w.location.hash = '#/team';
  await settle();
  assert.match(words(page(w)), /No repository is shared with your team/);
  const go = page(w).querySelector('a.primary');
  assert.equal(text(go), 'Open Team settings');
  assert.equal(go.getAttribute('href'), '#/settings?sec=team');
  assert.equal(page(w).querySelectorAll('.btn.primary, button.primary, a.primary').length, 1, 'one filled primary');
  assert.deepEqual(calls(w).filter((c) => /api\/team/.test(c.path)), []);
});

test('hostile text: a login, title, branch and address from GitHub are capped plain text, a bad link is not an anchor, and nothing is markup', async () => {
  const w = pageWorld({ team: await demoBody('?demo=1&team=hostile') });
  w.location.hash = '#/team';
  await settle();
  const root = page(w);
  assert.deepEqual(groupNames(w), ['a teammate'], 'a login that is not a GitHub name is "a teammate"');
  assert.equal(root.querySelectorAll('img, b, script').length, 0);
  for (const a of root.querySelectorAll('a')) assert.doesNotMatch(a.getAttribute('href') || '', /^javascript:/i);
  const row = rowsOf(w)[0];
  assert.ok(words(row).length < 700, 'the title is cut');
  assert.match(words(row), /<img src=x onerror=alert\(1\)>/, 'shown as text');
});

// ---------------------------------------------------------------- the sidebar, Home and the palette, in the lazy world

/** The first-paint set of index.html in a world whose head fetches lazy files from app/static, like tests/js/lazy.test.mjs. */
function lazyWorld({ state, team = null }) {
  const w = makeWorld({ companions: false, matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) });
  made.push(w);
  const dom = installDom(w);
  const doc = w.document;
  const head = new dom.El('head');
  doc.documentElement.insertBefore(head, doc.body);
  doc.head = head;
  const charts = new dom.El('link');
  charts.setAttribute('rel', 'stylesheet');
  charts.setAttribute('href', '/static/charts.css');
  head.append(charts);
  const find = doc.querySelector;
  doc.querySelector = (s) => find(s) || head.querySelector(s);
  const net = { scripts: [], sheets: [] };
  const onInsert = (n) => {
    if (n.tagName === 'SCRIPT') { net.scripts.push(n.src); setImmediate(() => { w.load(n.src.slice('/static/'.length)); n.onload(); }); }
    else if (n.tagName === 'LINK' && n.getAttribute('data-lazy')) { net.sheets.push(n.getAttribute('href')); setImmediate(() => n.onload()); }
  };
  const append = head.appendChild.bind(head);
  const insert = head.insertBefore.bind(head);
  head.appendChild = (n) => { const r = append(n); onInsert(n); return r; };
  head.insertBefore = (n, ref) => { const r = insert(n, ref); onInsert(n); return r; };
  w.load('lazy.js');
  for (const src of plain(w.get('LAZY_BUNDLES.app.js'))) w.load(src.slice('/static/'.length));
  for (const file of ['pages/agents.js', 'page-selection.js', 'page-keys.js', 'session-groups.js', 'inbox-cards.js', 'pages/inbox.js', 'pages/widgets.js', 'pages/home.js', 'components-tasks.js', 'task-views.js', 'pages/tasks.js', 'pages/search.js', 'pages/session.js', 'keymap.js']) w.load(file);
  w.ctx.__calls = [];
  w.ctx.__team = team;
  w.run(`api = async (method, path, body) => { __calls.push({ method, path, body }); if (path === '/api/team/state') return JSON.parse(JSON.stringify(__team)); return { ok: true }; }; poll = async () => {};`);
  w.ctx.__st = state;
  w.run('state = __st');
  w.run('Shell.refs = {}; Shell.buildSide(document.querySelector("#sidebar"), false); Shell.openCreate = () => true');
  return { w, net };
}

const sidebar = (w) => w.document.querySelector('#sidebar');
/** What the shell does with every state, without the topbar: the team watch, the sidebar trees and the page. */
const paint = (w) => w.run('teamSync(state); Shell.patchTrees(); updateCurrentPage(state)');

test('nothing shared: no script of the team page is fetched, no request is made, and Home, the sidebar and the palette have no team node', async () => {
  const { w, net } = lazyWorld({ state: fixtureState(), team: null });
  w.location.hash = '#/';
  paint(w);
  await settle(20);
  await w.run('new Promise((r) => Keymap.withPalette(() => { Palette.open(); r(); }))');
  assert.deepEqual(net.scripts.filter((s) => /team/.test(s)), []);
  assert.deepEqual(calls(w).filter((c) => /team/.test(c.path)), []);
  assert.equal(w.document.querySelectorAll('.tm-group, .tm-homeline, .tm-grp').length, 0);
  assert.equal(w.run('typeof TeamView'), 'undefined');
  const groups = plain(w.run("Palette.groups(Palette.context(), 'team', false, false).map((g) => g.id)"));
  assert.ok(!groups.includes('team'));
  assert.equal(w.run('teamUse(() => {})'), false);
});

test('a shared repository: the bundle loads once, the sidebar has one collapsed Team group with the live count, Home has the line, the palette has the rows, and one request is made', async () => {
  const team = await demoBody();
  const { w, net } = lazyWorld({ state: fixtureState({ team: { shared: true } }), team });
  w.location.hash = '#/';
  paint(w);
  await settle(30);
  assert.deepEqual(net.scripts.filter((s) => /team/.test(s)), ['/static/team.js', '/static/pages/team.js']);
  assert.deepEqual(net.sheets.filter((s) => /team/.test(s)), ['/static/pages/team.css', '/static/pages/team-view.css']);
  assert.deepEqual(calls(w).filter((c) => /api\/team/.test(c.path)).map((c) => c.path), ['/api/team/state']);
  const group = sidebar(w).querySelector('.tm-group');
  assert.ok(group, 'the group is there');
  const label = sidebar(w).querySelector('.sb-h[data-key=team-sbh]');
  assert.equal(text(label), 'Team', 'a label of its own: the group is not read as a project or a node');
  const order = sidebar(w).querySelectorAll('.sb-h[data-key=team-sbh], .tm-group').map((n) => n.getAttribute('data-key'));
  assert.deepEqual(order, ['team-sbh', 'team'], 'the label comes first');
  assert.equal(group.querySelector('.tn-row').getAttribute('aria-expanded'), 'false', 'collapsed by default');
  assert.equal(text(group.querySelector('.tn-name')), 'Teammates', 'the group under the Team label names the people, not the section again');
  assert.equal(text(group.querySelector('.tn-cnt')), '1');
  assert.equal(group.querySelector('.tn-go').getAttribute('href'), '#/team');
  assert.equal(group.querySelectorAll('.tm-prow').length, 0, 'closed: no rows');
  w.run("Shell.toggle('team')");
  const kids = sidebar(w).querySelectorAll('.tm-prow');
  assert.equal(kids.length, 1);
  assert.equal(kids[0].getAttribute('href'), '#/team?person=id%3A1002');
  assert.match(words(kids[0]), /●\s*@bob\s*example\/website/);
  assert.equal(kids[0].getAttribute('data-drop'), null, 'a person is not a drop target');
  assert.equal(words(page(w).querySelector('.tm-homeline')), '● 1 teammate active in 1 repo');
  assert.equal(page(w).querySelector('.tm-homeline a').getAttribute('href'), '#/team');
  await w.run('new Promise((r) => Keymap.withPalette(() => { Palette.open(); r(); }))');
  const g = plain(w.run("Palette.groups(Palette.context(), 'team', false, false).find((g) => g.id === 'team')"));
  assert.deepEqual(g.items.map((i) => i.label), ['Team', 'Team: example/website']);
  const empty = plain(w.run("Palette.groups(Palette.context(), '', false, false).find((g) => g.id === 'team')"));
  assert.deepEqual(empty.items.map((i) => i.label), ['Team'], 'the empty palette keeps its short list');
  w.run("Palette.groups(Palette.context(), 'team', false, false).find((g) => g.id === 'team').items[1].run()");
  assert.equal(w.location.hash, '#/p/phasezero/website?tab=team');
});

test('the line on Home says teammates active; with a repository that cannot be read it says so; with nothing to say it has no node; and a count that is not live stays out', async () => {
  const { w } = lazyWorld({ state: fixtureState({ team: { shared: true } }), team: await demoBody('?demo=1&team=sso') });
  w.location.hash = '#/';
  paint(w);
  await settle(30);
  assert.equal(words(page(w).querySelector('.tm-homeline')), '! Team: 1 repo could not be read. See why');
  assert.equal(text(sidebar(w).querySelector('.tm-group .tn-cnt')), '');
  const q = lazyWorld({ state: fixtureState({ team: { shared: true } }), team: await demoBody('?demo=1&team=alone') });
  q.w.location.hash = '#/';
  paint(q.w);
  await settle(30);
  assert.equal(page(q.w).querySelector('.tm-homeline'), null, 'nobody else is here: a quiet board says nothing');
  const l = lazyWorld({ state: fixtureState({ team: { shared: true } }), team: await demoBody('?demo=1&team=loading') });
  l.w.location.hash = '#/';
  paint(l.w);
  await settle(30);
  assert.equal(words(page(l.w).querySelector('.tm-homeline')), '∙ Team: reading GitHub for the first time');
});

test('when the board stops sharing, the group and the line go and the watch stops', async () => {
  const { w } = lazyWorld({ state: fixtureState({ team: { shared: true } }), team: await demoBody() });
  w.location.hash = '#/';
  paint(w);
  await settle(30);
  assert.ok(sidebar(w).querySelector('.tm-group') && page(w).querySelector('.tm-homeline'));
  w.run('delete state.team'); paint(w);
  await settle(5);
  assert.equal(sidebar(w).querySelectorAll('.tm-group').length, 0);
  assert.equal(page(w).querySelectorAll('.tm-homeline').length, 0);
  assert.equal(w.run('TeamView.stopWatch'), null);
});

test('#/team is a lazy route: the bundle loads before the page mounts, and the Team page draws from the one request', async () => {
  const { w, net } = lazyWorld({ state: fixtureState({ team: { shared: true } }), team: await demoBody() });
  w.location.hash = '#/team';
  await settle(40);
  assert.equal(w.get('mountedId'), 'team');
  assert.equal(w.document.title, 'Team · ccboard');
  assert.deepEqual(net.scripts.filter((s) => /team/.test(s)), ['/static/team.js', '/static/pages/team.js']);
  assert.equal(rowsOf(w).length, 8);
});

// ---------------------------------------------------------------- Settings > Notifications

test('Settings > Notifications lists "A teammate started on an issue I hold" only while a repository is shared, off until switched on, with its example', async () => {
  const team = { prefs: { needs: true, done: true, limit: true, error: true, login: true, team: false }, samples: {
    team: { title: 'bob started on #12', body: 'acme/shop\nbob started on issue #12, which you hold.\nOpen Team to see who is working on what.', priority: 3, kind: 'team_started', buttons: [] },
    needs: { title: 'x', body: 'y', buttons: [] } } };
  const { w } = homeWorld({ state: fixtureState({ team: { shared: true } }) });
  made.push(w);
  w.load('team.js');
  w.run("__answers['/api/notify/prefs'] = (req) => (req.method === 'PUT' ? { prefs: { ...__np.prefs, ...req.body }, samples: __np.samples } : __np)");
  w.ctx.__np = team;
  w.run('settingsNotifyAdopt(__np)');
  const block = w.run('settingsNotifyBlock()');
  const labels = block.querySelectorAll('.set-pref b').map(text);
  assert.deepEqual(labels, ['Needs you', 'Done', 'Rate limit', 'Error or crash', 'Login problem', 'A teammate started on an issue I hold']);
  const boxes = block.querySelectorAll('.set-pref input');
  assert.deepEqual(boxes.map((b) => b.checked), [true, true, true, true, true, false], 'the five are on, the team kind is off');
  assert.match(text(block.querySelectorAll('.set-pref')[5]), /Off until you switch it on/);
  const seg = block.querySelectorAll('.set-kind-seg .seg-btn').find((b) => text(b) === 'Team');
  seg.click();
  assert.match(words(block.querySelector('.set-notif')), /bob started on #12/);
  assert.match(words(block.querySelector('.set-notif-cap')), /switched off/);
  boxes[5].checked = true;
  boxes[5].dispatchEvent({ type: 'change' });
  await settle();
  const put = calls(w).find((c) => c.method === 'PUT' && c.path === '/api/notify/prefs');
  assert.deepEqual(put.body, { team: true });
  assert.equal(w.run('settingsNotifyOn("team")'), true);
  // the team kind counts as on only for the boolean true (the other five default on)
  for (const odd of ["'yes'", 'null', '1']) { w.run(`settingsNotifyState.prefs.team = ${odd}`); assert.equal(w.run('settingsNotifyOn("team")'), false, odd); }
  assert.equal(w.run('settingsNotifyState.prefs.team = true; settingsNotifyOn("team")'), true);
  // not shared: the key is not listed, whatever the board answered earlier
  w.run('delete state.team');
  const none = w.run('settingsNotifyBlock()');
  assert.deepEqual(none.querySelectorAll('.set-pref b').map(text), ['Needs you', 'Done', 'Rate limit', 'Error or crash', 'Login problem']);
  // and a board that does not answer the key leaves the five alone
  w.run('state.team = { shared: true }; settingsNotifyAdopt({ prefs: { needs: true, done: true, limit: true, error: true, login: true } })');
  assert.equal(w.run('settingsNotifyBlock().querySelectorAll(".set-pref").length'), 5);
});

test('the demo lists the team kind while the demo board shares a repository, off, with bob\'s example', () => {
  const { w } = homeWorld({ state: fixtureState({ team: { shared: true } }) });
  made.push(w);
  const d = plain(w.run('settingsNotifyDemo()'));
  assert.equal(d.prefs.team, false);
  assert.equal(d.samples.team.title, 'bob started on #12');
  w.run('delete state.team');
  assert.equal(Object.hasOwn(plain(w.run('settingsNotifyDemo()')).prefs, 'team'), false);
});

// ---------------------------------------------------------------- the rules of the frontend

test('the team page and its sheet keep the rules: no innerHTML, no inline style, no em-dash, classic definition-only script', () => {
  for (const f of ['pages/team.js', 'pages/team-view.css']) {
    const src = fs.readFileSync(path.join(STATIC, f), 'utf8');
    assert.doesNotMatch(src, f.endsWith('.js') ? /innerHTML|insertAdjacentHTML|cssText|\.style\b|style:|style=/ : /style=|url\(\s*['"]?data:|!important/, f);
    assert.equal(src.includes(String.fromCharCode(0x2014)), false, `${f}: no em-dash`);
    if (f.endsWith('.js')) assert.doesNotMatch(src, /localStorage|sessionStorage|fetch\(/, f);
  }
  const src = fs.readFileSync(path.join(STATIC, 'pages/team.js'), 'utf8');
  assert.doesNotMatch(src, /^(?!\s*(\/\/|\*|\/\*)).*\bdocument\.(querySelector|getElementById)\b.*=\s*/m);
});
