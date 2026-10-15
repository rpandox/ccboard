// Contract tests for the Team view of issue #197 (app/static/team.js, pages/team.css, the project page's Team tab, Settings > Team and the demo's answers for /api/team).
// Real scripts on minidom's DOM through tests/js/world.mjs; api() is the recorder, and its answers are the demo fixture (app/static/demo/team.json) or a changed copy of it.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { afterEach, test } from 'node:test';
import { STATIC, makeWorld, plain } from './harness.mjs';
import { calls, fakeState, fixtureState, homeWorld, page, text, tick } from './world.mjs';

const FX = JSON.parse(fs.readFileSync(path.join(STATIC, 'demo', 'team.json'), 'utf8'));
const clone = (v) => JSON.parse(JSON.stringify(v));
const KEY = 'github.com/example/website';
const STATES = ['off', 'loading', 'ok', 'sso', 'no_access', 'not_found', 'rate_limited', 'low_budget', 'no_login', 'no_gh', 'error'];

/** The body /api/team/state answers for a demo variant (the same lay-over demoTeam makes), with the times as they are in the fixture. */
function body(name = '') {
  const v = name ? FX.variants[name] : null;
  const repos = clone(FX.base.repos).map((b) => (v && v.repo && b.repo_slug === KEY ? { ...b, ...v.repo } : b));
  return { ...clone(FX.base), ...((v && v.body) || {}), repos, enabled: repos.some((b) => b.shared) };
}
const block = (name = '') => body(name).repos.find((b) => b.repo_slug === KEY);

const made = [];
afterEach(() => { for (const w of made.splice(0)) { try { w.run('for (const t of teamStore.soon.splice(0)) clearTimeout(t); clearTimeout(teamStore.timer); teamStore.subs.clear();'); } catch (_) { /* never loaded */ } } });
const settle = async (n = 5) => { for (let i = 0; i < n; i++) await tick(); };

/** A world with team.js loaded. GET /api/team/state answers __team (a body or a function of the request); PUT /api/team/repos/ answers __put. */
function teamWorld({ team = body(), state = fakeState(), put = null, project = false } = {}) {
  const { w } = homeWorld({ state });
  made.push(w);
  if (project) { w.load('tree.js'); w.load('pages/project.js'); }
  w.ctx.__team = team;
  w.ctx.__put = put;
  w.run(`__answers['/api/team/state'] = (req) => (typeof __team === 'function' ? __team(req) : __team);
    __answers['/api/team/repos/'] = (req) => { if (typeof __put === 'function') return __put(req); if (__put) return __put; throw new Error('no answer for ' + req.path); };
    __answers['/api/team/refresh'] = () => ({ queued: true });`);
  w.load('team.js');
  return w;
}

const T = (w, code) => w.run(code);
const node = (w, b) => { w.ctx.__b = b; return w.run('teamBlockNode(__b)'); };
const words = (n) => text(n).replace(/\s+/g, ' ');
const hasEl = (n, tag) => n.querySelectorAll(tag).length > 0;

// ---------------------------------------------------------------- the fixture itself (demo/team.json is what ?demo=1 shows)

test('the demo shares one repo with alice (you) and bob, keeps two off, and has a variant for every state word', () => {
  const b = block();
  assert.equal(b.shared, true);
  assert.deepEqual(b.people.map((p) => [p.login, p.you]), [['alice', true], ['bob', false]]);
  assert.deepEqual(FX.base.repos.filter((r) => !r.shared).map((r) => r.state), ['off', 'off']);
  const seen = new Set(Object.keys(FX.variants).map((n) => (FX.variants[n].repo || {}).state).filter(Boolean).concat(['ok']));
  for (const s of STATES) assert.ok(seen.has(s) || s === 'off', `no demo variant for the state ${s}`);
  for (const n of Object.keys(FX.variants)) assert.ok(STATES.includes(block(n).state), `variant ${n} carries a state word`);
  const kinds = new Set();
  for (const p of b.people) {
    if (p.issues.length) kinds.add('issue');
    for (const r of p.prs) kinds.add(r.draft ? 'draft' : 'pr');
    for (const x of p.branches) kinds.add(x.source);
  }
  assert.deepEqual([...kinds].sort(), ['draft', 'issue', 'linked', 'pr', 'pushed']);
  assert.ok(b.people.some((p) => p.prs.some((r) => r.from_fork)), 'a pull request from a fork is listed under the shared repo');
});

test('the fixture names no real host, person or path', () => {
  const raw = fs.readFileSync(path.join(STATIC, 'demo', 'team.json'), 'utf8');
  for (const bad of ['/Us' + 'ers/', '/ho' + 'me/', 'gmail' + '.com', 'ts' + '.net']) assert.ok(!raw.includes(bad), bad);
  assert.deepEqual([...new Set((raw.match(/"login": "([^"]+)"/g) || []).map((x) => x.slice(10, -1)))].sort(), ['../x', 'alice', 'bob']);
});

// ---------------------------------------------------------------- every state in words, and "no teammates" only when it is true

test('every state draws its own words and glyph, never as "no teammates"', () => {
  const w = teamWorld();
  const wants = { loading: 'Reading GitHub for the first time', sso: 'Authorise this token for the organization', forbidden: 'This login cannot read this repository', notfound: 'GitHub does not show this repository',
    rate: 'GitHub asked the board to wait', low: 'Waiting for the request limit', stale: 'This may be out of date', nologin: 'gh is not logged in', nogh: 'gh is not installed', error: 'GitHub could not be read' };
  for (const [name, title] of Object.entries(wants)) {
    const n = node(w, block(name));
    assert.ok(words(n).includes(title), `${name}: ${title}`);
    assert.ok(!words(n).includes('No teammates found here'), `${name} must not read as no teammates`);
    assert.ok(n.querySelector('.tm-state'), `${name}: a state chip`);
    assert.ok(text(n.querySelector('.tm-sg')).length > 0, `${name}: a glyph beside the words`);
    assert.ok(n.querySelector('.tm-note-n'), `${name}: one next step`);
  }
  for (const name of ['empty', 'alone']) {
    const n = node(w, block(name));
    assert.ok(words(n).includes('No teammates found here'), name);
    assert.equal(n.getAttribute('data-state'), 'ok');
  }
  const fine = node(w, block());
  assert.ok(!words(fine).includes('No teammates') && !fine.querySelector('.tm-note'), 'a fresh read with teammates has no notice');
});

test('each state has a distinct chip text and next step', () => {
  const w = teamWorld();
  const chips = new Set();
  const nexts = new Set();
  for (const name of ['', 'loading', 'sso', 'forbidden', 'notfound', 'rate', 'low', 'nologin', 'nogh', 'error']) {
    const n = node(w, block(name));
    chips.add(text(n.querySelector('.tm-state')));
    const nx = n.querySelector('.tm-note-n');
    if (nx) nexts.add(text(nx));
  }
  assert.equal(chips.size, 10);
  assert.equal(nexts.size, 9);
});

test('authorise-for-the-organization and no access are told apart by title and by next step', () => {
  const w = teamWorld();
  const sso = words(node(w, block('sso')));
  const none = words(node(w, block('forbidden')));
  assert.match(sso, /Configure SSO/);
  assert.doesNotMatch(none, /Configure SSO/);
  assert.match(none, /Ask the owner of the repository for access/);
});

test('data kept from an earlier read is shown beside a failed state with its age, and the old data is marked', () => {
  const w = teamWorld();
  const b = { ...block(), state: 'rate_limited', message: 'GitHub asked this board to wait 540 s.', wait_s: 540, stale: true, read_at: new Date(Date.now() - 12 * 60000).toISOString() };
  const n = node(w, b);
  assert.match(words(n), /Showing the last good read, 12 min ago/);
  assert.ok(n.querySelector('.tm-people.stale'));
  assert.equal(n.querySelectorAll('.tm-person').length, 2);
  assert.match(words(n), /Read 12 min ago/);
});

test('a stale read says how old it is; the age comes from read_at, not from the server age_s', () => {
  const w = teamWorld();
  const b = { ...block(), stale: true, age_s: 5, read_at: new Date(Date.now() - 47 * 60000).toISOString() };
  const n = node(w, b);
  assert.match(words(n), /The last read was 47 min ago/);
  assert.match(words(n), /Read 47 min ago/);
  assert.doesNotMatch(words(n), /5 s|just now/);
});

test('the slow GraphQL parts, cut lists and unattributed branches are each said in words', () => {
  const w = teamWorld();
  assert.match(words(node(w, block('pending'))), /read slowly \(every 10 minutes\); they are not here yet/);
  const cut = words(node(w, block('cut')));
  assert.match(cut, /only the first part of the issues, branches/);
  assert.match(cut, /3 recent branches with no GitHub author are not listed/);
});

test('a GraphQL read that failed is said in words, the rest of the block stays', () => {
  const w = teamWorld();
  const n = node(w, { ...block(), graph_failed: true, graph_pending: true, gql_at: null });
  assert.match(words(n), /could not be read just now; the board will try again soon/);
  assert.doesNotMatch(words(n), /they are not here yet/);
  assert.equal(n.querySelectorAll('.tm-person').length, 2);
  assert.doesNotMatch(words(node(w, block())), /could not be read just now/);
});

// ---------------------------------------------------------------- people, meaning and links

test('assigned, linked branch, pushed branch, draft PR and open PR are different words with their own glyphs; the owner is you', () => {
  const w = teamWorld();
  const n = node(w, block());
  const by = (k) => n.querySelectorAll(`.tm-item[data-kind=${k}]`);
  assert.deepEqual(['issue', 'linked', 'pushed', 'draft', 'pr'].map((k) => by(k).length > 0), [true, true, true, true, true]);
  assert.equal(text(by('issue')[0].querySelector('.tm-kind')), 'Assigned');
  assert.equal(text(by('linked')[0].querySelector('.tm-kind')), 'Linked branch');
  assert.equal(text(by('pushed')[0].querySelector('.tm-kind')), 'Pushed branch');
  assert.equal(text(by('draft')[0].querySelector('.tm-kind')), 'Draft PR');
  assert.equal(text(by('pr')[0].querySelector('.tm-kind')), 'Open PR');
  assert.equal(new Set(['issue', 'linked', 'pushed', 'draft', 'pr'].map((k) => text(by(k)[0].querySelector('.tm-g')))).size, 5, 'five glyphs');
  const people = n.querySelectorAll('.tm-person');
  assert.equal(people[0].getAttribute('data-login'), 'alice');
  assert.equal(people[0].getAttribute('data-you'), 'yes');
  assert.equal(text(people[0].querySelector('.tm-you')), 'you');
  assert.equal(people[1].querySelector('.tm-you'), null);
  assert.match(words(n), /from the fork bob\/website/);
  assert.match(words(n), /closes #152/);
  assert.match(words(n), /claimed, live on worktree-issue-154/);
  assert.match(words(people[1]), /2 issues, 2 branches, 2 pull requests/);
  for (const it of n.querySelectorAll('.tm-item')) {                            // a row's title (a branch: its name) is its only link: no extra "Open on GitHub" button per row
    const links = it.querySelectorAll('a');
    assert.equal(links.length, 1, text(it));
    assert.ok(links[0].classList.contains('tm-tl'));
    assert.notEqual(text(links[0]), 'Open on GitHub');
  }
});

test('the block has no control: no button, input, select or form, and every anchor is a https://github.com link that opens without the opener', () => {
  const w = teamWorld();
  for (const name of ['', 'sso', 'rate', 'stale', 'cut', 'hostile']) {
    const n = node(w, block(name));
    for (const tag of ['button', 'input', 'select', 'textarea', 'form']) assert.equal(hasEl(n, tag), false, `${name}: no ${tag}`);
    for (const a of n.querySelectorAll('a')) {
      const href = a.getAttribute('href');
      if (a.classList.contains('tm-set')) { assert.match(href, /^#\/settings\?sec=team$/); continue; }
      assert.match(href, /^https:\/\/github\.com\/example\/website(\/(issues|pull)\/[0-9]+|\/tree\/[A-Za-z0-9._\/%-]+)?$/, `${name}: ${href}`);
      assert.equal(a.getAttribute('target'), '_blank');
      assert.match(a.getAttribute('rel'), /noopener/);
      assert.match(a.getAttribute('aria-label'), /on GitHub$/);
    }
  }
});

test('a link is built only from a https://github.com address of the right shape', () => {
  const w = teamWorld();
  const link = (u) => { w.ctx.__u = u; return w.run("teamLink(__u, 'x', 'y')"); };
  for (const ok of ['https://github.com/example/website', 'https://github.com/example/website/issues/7', 'https://github.com/example/website/pull/12', 'https://github.com/example/website/tree/feat/a-b.c']) {
    assert.ok(link(ok), ok);
  }
  for (const bad of ['http://github.com/example/website', 'https://evil.example/example/website', 'https://github.com.evil.example/a/b', 'https://github.com@evil.example/a/b', 'javascript:alert(1)',
    'https://github.com/example/website/issues/0', 'https://github.com/example/website/issues/7/../../x', 'https://github.com/example/website/tree/../x', 'https://github.com/example/website?x=1',
    'https://github.com/example/website/blob/main/a', 'https://github.com/-bad/website', 'https://github.com/example/.hidden', '', null, 7, 'https://github.com/example/website/tree/a b']) {
    assert.equal(link(bad), null, String(bad));
  }
});

test('hostile text from GitHub is capped plain text: no markup, no control or bidi characters, a bad login is not named, a bad link is not an anchor', () => {
  const w = teamWorld();
  const b = block('hostile');
  const n = node(w, b);
  assert.equal(hasEl(n, 'img'), false, 'an <img> in a title is text');
  assert.equal(hasEl(n, 'b') && n.querySelectorAll('b').some((x) => text(x) === 'bold'), false, 'a <b> in a label is text');
  const title = text(n.querySelector('.tm-title'));
  assert.ok(title.startsWith('<img src=x onerror=alert(1)>'), 'shown as the characters it was');
  assert.ok(title.length <= 200, `capped (${title.length})`);
  assert.equal(text(n.querySelector('.tm-login')), 'unknown user');
  assert.equal(n.querySelector('.tm-person').getAttribute('data-login'), null);
  assert.equal(n.querySelectorAll('a').filter((a) => /^javascript/i.test(a.getAttribute('href') || '')).length, 0);
  assert.equal(n.querySelector('.tm-item a.tm-open'), null, 'the item with a javascript: address has no link');
  const bidi = String.fromCharCode(0x202e);
  const zw = String.fromCharCode(0x200b);
  const bel = String.fromCharCode(7);
  w.ctx.__s = `a${bidi}b${zw}c${bel}d\n\te` + 'x'.repeat(10240);
  const cut = w.run('teamText(__s, 200)');
  assert.ok(cut.length <= 200, 'a 10 KB title is cut');
  assert.equal([...cut].some((ch) => { const c = ch.charCodeAt(0); return c < 32 || (c >= 0x7f && c <= 0x9f) || (c >= 0x200b && c <= 0x200f) || (c >= 0x202a && c <= 0x202e); }), false);
  assert.ok(cut.startsWith('a b c d e'));
  assert.equal(w.run("teamText({ toString() { throw new Error('x'); } }, 20)"), '', 'a non-text value is nothing');
});

// ---------------------------------------------------------------- the repo row and its switch

test('the switch is off by default, says in one sentence that it only reads, and a click sends one PUT and nothing else', async () => {
  const off = clone(block());
  off.shared = false; off.state = 'off'; off.people = []; off.read_at = null;
  const on = { ...clone(block()), read_at: null, state: 'loading', people: [], message: 'Reading GitHub for the first time.' };
  const w = teamWorld({ team: { ...body(), repos: [off] }, put: on });
  w.run('teamAdopt(__team)');
  const row = w.run('teamShareRow(teamBlocks()[0])');
  const box = row.querySelector('input');
  assert.equal(box.checked, false);
  assert.equal(box.getAttribute('role'), 'switch');
  assert.match(box.getAttribute('aria-label'), /Shared with my team: example\/website/);
  assert.match(words(row), /Off\. Turn on to see who else works in example\/website.*read only\. Nothing is written to GitHub\./);
  box.checked = true;
  box.dispatchEvent({ type: 'change' });
  await settle();
  const c = calls(w);
  assert.deepEqual(c.map((x) => [x.method, x.path, x.body]), [['PUT', '/api/team/repos/example/website', { shared: true }]]);
  assert.equal(w.run('teamBlocks()[0].shared'), true);
  assert.equal(w.run('teamBlocks()[0].state'), 'loading');
});

test('a switch shows its new state at once and goes back with a reason when the board refuses', async () => {
  const w = teamWorld({ put: () => { throw new Error('this board has no project for example/website'); } });
  w.run('teamAdopt(__team)');
  const row = w.run('teamShareRow(teamBlocks()[0])');
  const box = row.querySelector('input');
  assert.equal(box.checked, true);
  box.checked = false;
  box.dispatchEvent({ type: 'change' });
  assert.equal(w.run('teamBlocks().find((b) => b.repo_slug === "' + KEY + '").shared'), false, 'optimistic');
  await settle();
  assert.equal(w.run('teamBlocks().find((b) => b.repo_slug === "' + KEY + '").shared'), true, 'put back');
  assert.match(w.run('teamStore.errors["' + KEY + '"]'), /^Not saved: this board has no project/);
  const again = w.run('teamShareRow(teamBlocks().find((b) => b.repo_slug === "' + KEY + '"))');
  assert.match(text(again.querySelector('.tm-err')), /Not saved/);
});

test('the switch takes a slug that is a GitHub name only: nothing else reaches the address', async () => {
  const w = teamWorld();
  w.run('teamAdopt(__team)');
  for (const slug of ['../etc/passwd', 'a/b/c', 'a b/c', '', 'o/..', '-x/y', 'x/y?z']) {
    w.ctx.__b = { ...block(), slug };
    assert.equal(await w.run('teamShare(__b, true)'), false, slug);
  }
  assert.deepEqual(calls(w), []);
});

// ---------------------------------------------------------------- the project page: the Team tab

const phasezero = () => ({ ...body(), repos: body().repos });

async function projectTab(w, hash = '#/p/phasezero?tab=team') {
  w.location.hash = hash;
  await settle(8);
  return page(w);
}

test('the project page has a Team tab; the sessions tab fetches nothing of the team', async () => {
  const w = teamWorld({ state: fixtureState(), project: true });
  w.location.hash = '#/p/phasezero';
  await settle();
  const ids = page(w).querySelectorAll('.tab').map((t) => t.getAttribute('data-tab'));
  assert.ok(ids.includes('team'), ids.join());
  assert.deepEqual(calls(w).filter((c) => /team/.test(c.path)), [], 'no team request until the tab opens');
});

test('the Team tab draws a switch per repo of the project with a GitHub remote and the block of the shared one', async () => {
  const w = teamWorld({ state: fixtureState(), project: true });
  const p = await projectTab(w);
  const rows = p.querySelectorAll('.tm-repo');
  assert.deepEqual(rows.map((r) => r.getAttribute('data-repo')), [KEY, 'github.com/example/api']);
  const [shared, off] = rows;
  assert.equal(shared.querySelector('input').checked, true);
  assert.equal(off.querySelector('input').checked, false);
  assert.ok(shared.querySelector('.tm-block'), 'the shared repo has its block');
  assert.equal(off.querySelector('.tm-block'), null, 'a repo that is off has none');
  assert.equal(shared.querySelectorAll('.tm-person').length, 2);
  assert.match(words(p), /Read only: nothing is written to GitHub/);
  assert.equal(calls(w).filter((c) => c.path === '/api/team/state').length, 1, 'one read of the board for the tab');
  assert.equal(calls(w).filter((c) => c.method !== 'GET').length, 0, 'opening the tab writes nothing');
});

test('a repo route narrows the tab to that repo, and a project without a GitHub remote says so', async () => {
  const w = teamWorld({ state: fixtureState(), project: true });
  let p = await projectTab(w, '#/p/phasezero/website?tab=team');
  assert.deepEqual(p.querySelectorAll('.tm-repo').map((r) => r.getAttribute('data-repo')), [KEY]);
  p = await projectTab(w, '#/p/petroit?tab=team');
  assert.equal(p.querySelectorAll('.tm-repo').length, 0);
  assert.match(words(p), /No GitHub repository in this project/);
});

test('the Team tab asks for the lazy bundle when it opens, and says so when it cannot load', async () => {
  const { w } = homeWorld({ state: fixtureState() });
  made.push(w);
  w.load('tree.js'); w.load('pages/project.js');
  w.run(`globalThis.__asked = [];
    globalThis.Lazy = { bundles: { team: {} }, pending: () => [], load: (n) => { __asked.push(n); return Promise.reject(new Error('could not load')); } };`);
  w.location.hash = '#/p/phasezero?tab=team';
  await settle();
  assert.deepEqual(plain(w.get('__asked')), ['team']);
  assert.match(words(page(w)), /The team view did not load/);
  assert.deepEqual(calls(w).filter((c) => /team/.test(c.path)), [], 'nothing is asked of the board without the bundle');
});

// ---------------------------------------------------------------- Settings > Team

test('Settings > Team names the login, shows the switches and the Doctor rows, and Check again asks the board to read', async () => {
  const w = teamWorld();
  w.run(`doctorStore.data = { checks: [
    { id: 'gh-login', group: 'box', label: 'GitHub login of this board', status: 'pass', detail: 'this board acts as alice (id 1001)', fix: null },
    { id: 'team-gh', group: 'box', label: 'Team GitHub access', status: 'warn', detail: 'authorise this token for the organization: GitHub will not let it read example/website', fix: { text: 'Configure SSO' } },
    { id: 'git', group: 'box', label: 'git', status: 'pass', detail: 'git 2.43', fix: null }] }; doctorStore.at = Date.now();`);
  w.run('teamAdopt(__team)');
  w.location.hash = '#/settings?sec=team';
  await settle(8);
  const p = page(w).querySelector('.settings-panel[data-sec=team]');
  assert.ok(p && !p.classList.contains('hidden'));
  assert.equal(text(p.querySelector('.kv .v')), 'alice');
  assert.match(words(p), /GitHub user id 1001/);
  assert.deepEqual(p.querySelectorAll('.doc-row').map((r) => r.getAttribute('data-check')), ['gh-login', 'team-gh']);
  assert.match(words(p), /authorise this token for the organization/);
  assert.deepEqual(p.querySelectorAll('.tm-srow').map((r) => r.getAttribute('data-repo')), [KEY, 'github.com/example/api', 'github.com/example/ccboard']);
  assert.equal(p.querySelectorAll('.tm-srow input').filter((i) => i.checked).length, 1);
  assert.match(words(p), /at most 600 an hour in all/);
  const again = p.querySelectorAll('button').find((b) => text(b) === 'Check again');
  again.click();
  await settle();
  assert.ok(calls(w).some((c) => c.method === 'POST' && c.path === '/api/team/refresh'));
});

test('Settings > Team says "not known yet", not "nobody", when gh has not told the board its login', async () => {
  const w = teamWorld({ team: { ...body('nologin') } });
  w.run('teamAdopt(__team)');
  w.location.hash = '#/settings?sec=team';
  await settle(8);
  const p = page(w).querySelector('.settings-panel[data-sec=team]');
  assert.match(words(p), /not known yet/);
  assert.match(words(p), /gh is not logged in/);
});

test('leaving the Team tab of Settings stops the watch of the board', async () => {
  const w = teamWorld();
  w.location.hash = '#/settings?sec=team';
  await settle(8);
  assert.equal(w.run('teamStore.subs.size'), 1);
  w.location.hash = '#/settings?sec=box';
  await settle();
  assert.equal(w.run('teamStore.subs.size'), 0);
  assert.equal(w.run('teamStore.timer'), null);
});

// ---------------------------------------------------------------- the demo (?demo=1)

function demoWorld(search) {
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
  return w;
}

test('?demo=1 answers /api/team/state from the fixture with the ages worked out now, and every ?team= variant is one state', async () => {
  const d0 = plain(await demoWorld('?demo=1').run("api('GET', '/api/team/state')"));
  assert.equal(d0.enabled, true);
  assert.deepEqual(d0.me, { login: 'alice', id: 1001 });
  assert.deepEqual(d0.repos.map((r) => [r.slug, r.shared, r.state]), [['example/website', true, 'ok'], ['example/api', false, 'off'], ['example/ccboard', false, 'off']]);
  assert.ok(d0.repos[0].age_s >= 0 && d0.repos[0].age_s < 3600 * 24 * 3, 'age_s follows the clock');
  assert.ok(Date.now() - Date.parse(d0.repos[0].read_at) < 120000, 'read_at is minutes before now (the fixture is rebased)');
  for (const name of Object.keys(FX.variants)) {
    const d = plain(await demoWorld(`?demo=1&team=${name}`).run("api('GET', '/api/team/state')"));
    const web = d.repos.find((r) => r.repo_slug === KEY);
    assert.equal(web.state, block(name).state, name);
    assert.equal(web.shared, block(name).shared, name);
  }
  const unknown = plain(await demoWorld('?demo=1&team=nonsense').run("api('GET', '/api/team/state')"));
  assert.equal(unknown.repos[0].state, 'ok');
});

test('in the demo a switch is played for the life of the page: on gives an empty ok read, off gives the off block, a stranger is refused', async () => {
  const w = demoWorld('?demo=1');
  const on = plain(await w.run("api('PUT', '/api/team/repos/example/api', { shared: true })"));
  assert.equal(on.shared, true);
  assert.equal(on.state, 'ok');
  assert.deepEqual(on.people, []);
  assert.equal(plain(await w.run("api('GET', '/api/team/state')")).repos.find((r) => r.slug === 'example/api').shared, true);
  const off = plain(await w.run("api('PUT', '/api/team/repos/example/website', { shared: false })"));
  assert.equal(off.state, 'off');
  assert.equal(plain(await w.run("api('GET', '/api/team/state')")).repos.find((r) => r.slug === 'example/website').people.length, 0);
  await assert.rejects(w.run("api('PUT', '/api/team/repos/other/thing', { shared: true })"), /no project/);
  await assert.rejects(w.run("api('PUT', '/api/team/repos/example/api', { shared: 'yes' })"), /true or false/);
  assert.deepEqual(plain(await w.run("api('POST', '/api/team/refresh')")), { queued: true });
});

test('team.js is a lazy bundle: not in index.html, in lazy.js with its own sheet, and the project and settings bundles do not pull it in', () => {
  const idx = fs.readFileSync(path.join(STATIC, 'index.html'), 'utf8');
  assert.ok(!idx.includes('team.js') && !idx.includes('team.css'));
  const lazy = fs.readFileSync(path.join(STATIC, 'lazy.js'), 'utf8');
  assert.match(lazy, /team: \{ js: \['\/static\/team\.js'\], css: \[\{ href: '\/static\/pages\/team\.css', rank: 16 \}\]/);
  assert.doesNotMatch(lazy, /needs: \[[^\]]*'team'/);
  const src = fs.readFileSync(path.join(STATIC, 'team.js'), 'utf8');
  assert.doesNotMatch(src, /innerHTML|insertAdjacentHTML|cssText|\.style\b|style:/);
  assert.equal(src.includes(String.fromCharCode(0x2014)), false, 'no em-dash in UI text');
});

test('the Team tab loads the bundle on first use, draws, and stops watching the board when another tab opens', async () => {
  const { w } = homeWorld({ state: fixtureState() });
  made.push(w);
  w.load('tree.js'); w.load('pages/project.js');
  w.ctx.__team = body();
  w.ctx.__loadTeam = () => w.load('team.js');
  w.run(`__answers['/api/team/state'] = () => __team; globalThis.__asked = [];
    globalThis.Lazy = { bundles: { team: {} }, pending: () => [], wants: () => false, load: (n) => { __asked.push(n); __loadTeam(); return Promise.resolve(); } };`);
  w.location.hash = '#/p/phasezero?tab=team';
  await settle(8);
  assert.deepEqual(plain(w.get('__asked')), ['team']);
  assert.equal(page(w).querySelectorAll('.tm-repo').length, 2);
  assert.equal(w.run('teamStore.subs.size'), 1);
  w.location.hash = '#/p/phasezero?tab=tasks';
  await settle(4);
  assert.equal(w.run('teamStore.subs.size'), 0, 'the watch ends with the tab');
  assert.equal(w.run('teamStore.timer'), null);
});

test('the board is asked again every 15 seconds while a surface is open, and not while the page is hidden', async () => {
  const w = teamWorld();
  w.run('globalThis.__q = []; setTimeout = (fn, ms) => { __q.push({ fn, ms }); return __q.length; }; clearTimeout = () => {};');
  const reads = () => calls(w).filter((c) => c.path === '/api/team/state').length;
  const stop = w.run('teamWatch(() => {})');
  await settle(4);
  assert.equal(reads(), 1, 'asked at once');
  assert.deepEqual(plain(w.get('__q.map((x) => x.ms)')), [15000]);
  w.document.hidden = true;
  await w.run('__q[0].fn()');
  assert.equal(reads(), 1, 'a hidden page asks for nothing');
  assert.deepEqual(plain(w.get('__q.map((x) => x.ms)')), [15000, 15000], 'but it keeps its schedule');
  w.document.hidden = false;
  await w.run('__q[1].fn()');
  assert.equal(reads(), 2, 'asked again when the page is back');
  stop();
});
