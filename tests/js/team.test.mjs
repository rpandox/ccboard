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
  const lines = (b) => {                                                   // a variant sets the live line of a person by login, as demoTeam does
    if (!v || !v.lines || b.repo_slug !== KEY) return b;
    const people = b.people.map((x) => (Object.hasOwn(v.lines, x.login) ? { ...x, presence: v.lines[x.login] } : x));
    return { ...b, people, counts: { ...b.counts, live: people.filter((x) => !x.you && x.presence && ['live', 'idle'].includes(x.presence.live)).length } };
  };
  const repos = clone(FX.base.repos).map((b) => (v && v.repo && b.repo_slug === KEY ? { ...b, ...v.repo } : b)).map(lines);
  return { ...clone(FX.base), ...((v && v.body) || {}), repos, enabled: repos.some((b) => b.shared) };
}
const block = (name = '') => body(name).repos.find((b) => b.repo_slug === KEY);
/** The same block read just now (the fixture's own times are those of its epoch, and a live line ages from the read). */
const fresh = (b, agoS = 0) => ({ ...clone(b), read_at: new Date(Date.now() - agoS * 1000).toISOString() });
/** The publishing view GET /api/team/presence answers for a demo variant (the lay-over demoPresBuild makes). */
function presView(name = '') {
  const v = name ? FX.presence.variants[name] : null;
  const view = clone(FX.presence.base);
  const repos = view.repos.map((b) => (v && v.repo && b.repo_slug === KEY ? { ...b, ...clone(v.repo) } : b));
  return { ...view, ...(v && v.body ? clone(v.body) : {}), repos };
}

const made = [];
afterEach(() => { for (const w of made.splice(0)) { try { w.run('for (const t of teamStore.soon.splice(0)) clearTimeout(t); clearTimeout(teamStore.timer); teamStore.subs.clear();'); } catch (_) { /* never loaded */ } } });
const settle = async (n = 5) => { for (let i = 0; i < n; i++) await tick(); };

/** A world with team.js loaded. GET /api/team/state answers __team (a body or a function of the request); PUT /api/team/repos/ answers __put. */
function teamWorld({ team = body(), state = fakeState(), put = null, project = false, pres = presView() } = {}) {
  const { w } = homeWorld({ state });
  made.push(w);
  if (project) { w.load('tree.js'); w.load('pages/project.js'); }
  w.ctx.__team = team;
  w.ctx.__put = put;
  w.ctx.__pres = pres;
  w.run(`__answers['/api/team/state'] = (req) => (typeof __team === 'function' ? __team(req) : __team);
    __answers['/api/team/repos/'] = (req) => { if (typeof __put === 'function') return __put(req); if (__put) return __put; throw new Error('no answer for ' + req.path); };
    __answers['/api/team/presence'] = (req) => (typeof __pres === 'function' ? __pres(req) : __pres);
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
  assert.match(words(people[1]), /2 issues, 4 branches, 2 pull requests/);
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
  assert.match(words(row), /Off\. Turn on to see who else works in example\/website.*read only\. Reading writes nothing to GitHub\./);
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
  assert.match(words(p), /Reading is off until you switch a repository on, and it writes nothing/);
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
    globalThis.Lazy = { done: () => false, bundles: { team: {} }, pending: () => [], load: (n) => { __asked.push(n); return Promise.reject(new Error('could not load')); } };`);
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
  assert.match(lazy, /team: \{ js: \['\/static\/team\.js'\], css: \[\{ href: '\/static\/pages\/team\.css', rank: 116 \}\]/);
  assert.deepEqual([...lazy.matchAll(/^  (\w+): \{ needs: \[[^\]]*'team'/gm)].map((m) => m[1]), ['teamview'], 'only the Team page (issue #200) builds on the team bundle');
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
    globalThis.Lazy = { done: () => false, bundles: { team: {} }, pending: () => [], wants: () => false, load: (n) => { __asked.push(n); __loadTeam(); return Promise.resolve(); } };`);
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

// ================================================================ issue #198, Team P2: the live line, and Settings > Team: Publish my status

const bob = (n) => n.querySelector('.tm-person[data-login=bob]');
const rowsOf = (n) => n.querySelectorAll('.tm-pl');
const withBoards = (boards, readAgoS = 0) => { const b = fresh(block(), readAgoS); b.people[1].presence = { live: 'live', last_seen_s: 5, boards }; return b; };
const aBoard = (over = {}) => ({ node: 'willow-3f', live: 'live', last_seen_s: 60, seq: 3, state: 'running', agent: 'claude', issue: 152, issue_url: 'https://github.com/example/website/issues/152',
  branch: 'bob/coupon-total', branch_url: 'https://github.com/example/website/tree/bob/coupon-total', phase: 'running', since: '2026-10-03T01:30:00Z', last: '2026-10-03T02:45:00Z', updated_at: '2026-10-03T02:56:00Z', ...over });

test('the demo has a live line for alice (you) and two boards for bob, and a variant for every live word, session state and presence state', () => {
  const b = block();
  assert.deepEqual(b.people.map((p) => p.presence.boards.length), [1, 2]);
  assert.equal(b.counts.live, 1);
  const words = new Set();
  const states = new Set();
  for (const v of Object.values(FX.variants)) for (const pr of Object.values(v.lines || {})) for (const x of (pr ? pr.boards : [])) { words.add(x.live); states.add(x.state); }
  for (const x of b.people.flatMap((p) => p.presence.boards)) { words.add(x.live); states.add(x.state); }
  assert.deepEqual([...words].sort(), ['idle', 'live', 'stale', 'unconfirmed', 'unknown']);
  assert.deepEqual([...states].sort(), ['ended', 'idle', 'needs_you', 'running']);
  assert.deepEqual(Object.values(FX.variants).map((v) => (v.repo && v.repo.presence ? v.repo.presence.state : null)).filter(Boolean).sort(), ['closed', 'error', 'none', 'ok']);
  assert.equal(Object.keys(FX.presence.variants).length, 19, 'one variant per publishing state besides the base');
});

test('a teammate who publishes shows a live line per board: liveness, agent, state, issue and branch as links, since, last active, last seen', () => {
  const w = teamWorld();
  const n = node(w, fresh(block()));
  const rows = rowsOf(bob(n));
  assert.equal(rows.length, 2);
  const first = words(rows[0]);
  assert.match(first, /^● Live ◆ claude ✽ Running #152 bob\/coupon-total on willow-3f /);
  assert.match(first, /task running · since (\d\d:\d\d|[A-Z][a-z]{2} \d{1,2} \d\d:\d\d) · last active (\d+ (min|h|d) ago|just now) · last seen/);
  assert.match(first, /last seen 4 min ago$/);
  assert.equal(rows[0].getAttribute('data-live'), 'live');
  const second = words(rows[1]);
  assert.match(second, /^◐ Idle ◇ codex on ash-91 /);
  assert.doesNotMatch(second, /Running|Needs you/, 'an idle board does not say idle twice');
  assert.equal(rows[1].getAttribute('data-live'), 'idle');
  const links = rows[0].querySelectorAll('a');
  assert.deepEqual(links.map((a) => a.getAttribute('href')), ['https://github.com/example/website/issues/152', 'https://github.com/example/website/tree/bob/coupon-total']);
  assert.ok(links.every((a) => a.getAttribute('target') === '_blank' && /noopener/.test(a.getAttribute('rel')) && /on GitHub$/.test(a.getAttribute('aria-label'))));
  const mine = rowsOf(n.querySelector('.tm-person[data-login=alice]'));
  assert.equal(mine.length, 1);
  assert.doesNotMatch(words(mine[0]), / on node-a/, 'one board: no board name');
  assert.match(words(n), /1 live now/);
  assert.match(words(n), /nothing on GitHub yet|2 issues, 4 branches, 2 pull requests/);
});

test('every live word and every session state has its own glyph and words, never colour alone', () => {
  const w = teamWorld();
  const wants = { lineneeds: ['● Live', '✻ Needs you'], lineended: ['◐ Idle', '○ Ended'], lineunconfirmed: ['◌ Not confirmed yet', '✽ Running'], linestale: ['! Stale', '✽ Running'], lineunknown: ['? Unknown', '✽ Running'] };
  const glyphs = new Set();
  for (const [name, parts] of Object.entries(wants)) {
    const row = rowsOf(bob(node(w, fresh(block(name))))) [0];
    for (const part of parts) assert.ok(words(row).includes(part), `${name}: ${part} in ${words(row)}`);
    glyphs.add(text(row.querySelector('.tm-lv .tm-sg')));
    assert.equal(row.querySelector('.tm-lv .tm-sg').getAttribute('aria-hidden'), 'true');
  }
  assert.equal(glyphs.size, 5, 'live, idle, not confirmed, stale and unknown do not share a glyph');
  assert.match(words(rowsOf(bob(node(w, fresh(block('linestale')))))[0]), /last seen 2 h ago/);
  assert.match(words(rowsOf(bob(node(w, fresh(block('lineunknown')))))[0]), /last seen unknown/);
  assert.equal(rowsOf(bob(node(w, fresh(block('linegone'))))).length, 0, 'a gone line is not drawn');
});

test('a line that outlived its lease since the last read reads stale on this page, one older than a day is dropped, and nothing is ever made live', () => {
  const w = teamWorld();
  const n1 = node(w, withBoards([aBoard({ last_seen_s: 1700 })], 300));                    // 1700 s at the read + 300 s since: past the 30 minute lease
  assert.match(words(rowsOf(bob(n1))[0]), /^! Stale /);
  assert.match(words(rowsOf(bob(n1))[0]), /last seen 33 min ago/);
  const n2 = node(w, withBoards([aBoard({ last_seen_s: 86000 }), aBoard({ node: 'ash-91', last_seen_s: 100 })], 600));
  assert.equal(rowsOf(bob(n2)).length, 1, 'the day-old board is not drawn');
  assert.match(words(rowsOf(bob(n2))[0]), /on ash-91/);
  const n3 = node(w, withBoards([aBoard({ live: 'stale', last_seen_s: 10 }), aBoard({ live: 'banana', node: 'x' }), aBoard({ live: 'unknown', last_seen_s: null, node: 'y' })]));
  assert.deepEqual(rowsOf(bob(n3)).map((r) => r.getAttribute('data-live')), ['stale', 'unknown', 'unknown'], 'the board\'s word is kept; a word it does not know is unknown');
  assert.equal(w.run('teamLiveWord({ live: "unknown", last_seen_s: 1 }, null)'), 'unknown');
  assert.equal(w.run('teamLiveWord({ live: "live", last_seen_s: -5 }, null)'), 'live', 'a negative age is no age at all: the board\'s word stands');
});

test('hostile text in a live line is plain capped text: no markup, no bad link, no unknown state or agent', () => {
  const w = teamWorld();
  const evil = String.fromCharCode(0x202e) + String.fromCharCode(7);
  const n = node(w, withBoards([aBoard({ node: `<img src=x onerror=alert(1)>${evil}` + 'n'.repeat(200), issue_url: 'javascript:alert(1)', branch: `a b<script>${evil}`, branch_url: 'https://evil.example/x', state: 'rm -rf', agent: 'bash; rm', phase: '<b>', since: 'nope', last: 'nope' }),
    aBoard({ node: 'second', issue: '7;ls', branch: null, branch_url: null })]));
  const rows = rowsOf(bob(n));
  assert.equal(rows.length, 2);
  assert.equal(hasEl(n, 'img') || hasEl(n, 'script'), false);
  assert.equal(n.querySelectorAll('a').filter((a) => !/^https:\/\/github\.com\//.test(a.getAttribute('href') || '') && !a.classList.contains('tm-set')).length, 0);
  const t = words(rows[0]);
  assert.ok(t.includes('<img src=x onerror=alert(1)>'), 'shown as the characters it was');
  assert.ok(!t.includes(String.fromCharCode(0x202e)) && !t.includes(String.fromCharCode(7)));
  assert.ok(text(rows[0].querySelector('.tm-nd')).length <= 44, 'the board name is capped');
  assert.equal(rows[0].querySelector('.tm-ps'), null, 'a state the board does not use is not drawn');
  assert.equal(rows[0].querySelector('.tm-ag'), null, 'an agent the board does not use is not drawn');
  assert.doesNotMatch(t, /task <b>/);
  assert.equal(rows[1].querySelectorAll('a').length, 0, 'an issue that is not a number is no link');
});

test('what the board found of the presence issue is said in words under the people, never as "no teammates"', () => {
  const w = teamWorld();
  const base = words(node(w, fresh(block())));
  assert.match(base, /Live status is read from the presence issue #5\./);
  assert.equal(node(w, fresh(block())).querySelector('.tm-pnote a').getAttribute('href'), 'https://github.com/example/website/issues/5');
  assert.match(words(node(w, fresh(block('linenone')))), /Nobody publishes a live status in this repository yet\. A teammate can switch on Publish my status in Settings > Team\./);
  assert.match(words(node(w, fresh(block('lineclosed')))), /! The presence issue is closed, so no live status is shown here\. presence issue #5\./);
  assert.match(words(node(w, fresh(block('lineerror')))), /! The live status could not be read just now/);
  const cut = words(node(w, fresh(block('linecut'))));
  assert.match(cut, /! Some live lines are left out: GitHub would not say who last edited them\./);
  assert.match(cut, /Only the first part of the live status comments was read/);
  assert.doesNotMatch(words(node(w, fresh({ ...block('linenone'), state: 'sso' }))), /Nobody publishes a live status/, 'a failed read says its own words');
});

test('the Team tab shows the live line of a teammate, reads no publishing settings, and the read switch no longer claims to write nothing at all', async () => {
  const w = teamWorld({ state: fixtureState(), project: true, team: { ...body(), repos: body().repos.map((r) => (r.read_at ? { ...r, read_at: new Date().toISOString() } : r)) } });
  const p = await projectTab(w);
  assert.equal(rowsOf(p).length, 3);
  assert.ok(words(p).includes('● Live'));
  assert.equal(calls(w).filter((c) => /presence/.test(c.path)).length, 0, 'the Team tab does not ask for the publishing settings');
  assert.match(words(p), /Sharing your own status is a separate switch in Settings > Team, also off/);
});

// ---------------------------------------------------------------- Settings > Team > Publish my status

async function presPanel(w) {
  w.run('teamAdopt(__team)');
  w.location.hash = '#/settings?sec=team';
  await settle(10);
  return page(w).querySelector('.settings-panel[data-sec=team]');
}
const prow = (p, slug) => p.querySelector(`.tm-prow[data-repo="github.com/example/${slug}"]`);
const pcalls = (w) => calls(w).filter((c) => /^\/api\/team\/presence/.test(c.path));
const fireChange = (box, on) => { box.checked = on; box.dispatchEvent({ type: 'change' }); };
const enough = async () => { for (let i = 0; i < 6; i++) await tick(); };

test('Settings > Team shows Publish my status: the opt-in text, the pause, one row per repository, the budget and what it never publishes', async () => {
  const w = teamWorld();
  const p = await presPanel(w);
  assert.deepEqual(pcalls(w).map((c) => [c.method, c.path]), [['GET', '/api/team/presence']]);
  assert.ok(p.querySelectorAll('h2').some((h) => text(h) === 'Publish my status'));
  const optin = words(p.querySelector('.tm-optin'));
  assert.match(optin, /GitHub keeps the edit history of a comment/);
  assert.match(optin, /everyone who can read the repository can open it while the comment exists/);
  assert.match(optin, /Some organizations do not allow tools to write to their issues/);
  assert.match(words(p), /Off for every repository until you switch it on here, and separate from Shared with my team\. Never published: titles, prompts, replies, transcripts, terminal text, paths, host names, account names, usage or cost\./);
  assert.deepEqual(p.querySelectorAll('.tm-prow').map((r) => r.getAttribute('data-repo')), [KEY, 'github.com/example/api', 'github.com/example/ccboard']);
  const pause = p.querySelector('[data-pause=all]');
  assert.equal(pause.getAttribute('role'), 'switch');
  assert.equal(pause.checked, false);
  assert.equal(p.querySelectorAll('[data-pub]').map((b) => b.checked).join(), 'true,false,false');
  assert.equal(words(prow(p, 'website').querySelector('.tm-repohead')), 'example/website ✓ Publishing private repository presence issue #5');
  assert.equal(words(prow(p, 'api').querySelector('.tm-repohead')), 'example/api ○ Not published not known to be private');
  assert.equal(words(prow(p, 'ccboard').querySelector('.tm-repohead')), 'example/ccboard ○ Not published public repository');
  assert.match(words(p.querySelector('.tm-budget')), /^Writes this hour: 14 of 240 for this login \(this board may use 240\)\. A line is rewritten when your state changes and every 10 to 15 minutes to show you are still here; it counts as stale after 30 minutes\.$/);
  assert.match(words(p), /Publishing is counted apart: at most 240 writes an hour for the whole login/);
  assert.ok(prow(p, 'website').querySelector('[data-priv]'), 'Private is offered where publishing is on');
  assert.equal(prow(p, 'api').querySelector('[data-priv]'), null, 'and not where it is off');
});

test('the publish switch sends one PUT, shows its new state at once and goes back with a reason when the board refuses', async () => {
  const w = teamWorld({ pres: (req) => { if (req.method === 'PUT') { const e = new Error('this board has no project for example/website'); e.status = 404; throw e; } return presView(); } });
  const p = await presPanel(w);
  fireChange(prow(p, 'website').querySelector('[data-pub]'), false);
  assert.equal(w.run('teamPresBlocks().find((b) => b.repo_slug === "' + KEY + '").publish'), false, 'optimistic');
  await enough();
  assert.equal(w.run('teamPresBlocks().find((b) => b.repo_slug === "' + KEY + '").publish'), true, 'put back');
  assert.deepEqual(pcalls(w).filter((c) => c.method === 'PUT').map((c) => [c.path, c.body]), [['/api/team/presence/example/website', { publish: false }]]);
  const again = page(w).querySelector('.settings-panel[data-sec=team]');
  assert.match(text(prow(again, 'website').querySelector('.tm-err')), /^Not saved: this board has no project/);
  assert.equal(prow(again, 'website').querySelector('[data-pub]').checked, true);
});

test('a public repository, or one GitHub has not said is private, asks the second confirm with exactly what becomes public, and sends nothing until it is given', async () => {
  const w = teamWorld({ pres: (req) => {
    if (req.method === 'GET') return presView();
    if (req.body.confirm_public === true) return { ...presView().repos[2], publish: true, public_confirmed: true, state: 'publishing', message: '', published: true, issue: 1, issue_url: 'https://github.com/example/ccboard/issues/1', last_body: 'x', last_at: new Date().toISOString() };
    const e = new Error('This repository is public, or GitHub has not said it is private. Confirm to publish.');
    e.status = 409;
    e.body = { error: e.message, needs_confirm: true, public_lists: presView().public_lists, next_body: '<!-- ccboard:presence v1 -->\nccboard presence: node=node-a nid=1a2b3c4d repo=example/ccboard seq=1 at=2026-10-03T02:57:00Z lease=30m state=idle agent=claude', opt_in_text: 'x' };
    throw e;
  } });
  const p = await presPanel(w);
  fireChange(prow(p, 'ccboard').querySelector('[data-pub]'), true);
  await enough();
  assert.deepEqual(pcalls(w).filter((c) => c.method === 'PUT').map((c) => c.body), [{ publish: true }]);
  const q = page(w).querySelector('.settings-panel[data-sec=team]');
  const box = prow(q, 'ccboard').querySelector('.tm-confirm');
  assert.ok(box, 'the confirm is open');
  assert.equal(prow(q, 'ccboard').querySelector('[data-pub]').checked, false, 'nothing is switched on yet');
  assert.match(words(box), /Confirm before publishing in example\/ccboard/);
  assert.deepEqual(box.querySelectorAll('li').map((l) => text(l)), ['your GitHub login', 'the issue number you work on', 'the state and agent of your session', 'when it started and when you were last active (to 15 minutes)']);
  assert.match(words(box), /No branch name is published here\./);
  assert.match(text(box.querySelector('pre')), /^<!-- ccboard:presence v1 -->\nccboard presence: node=node-a /);
  const buttons = box.querySelectorAll('button');
  assert.deepEqual(buttons.map((b) => text(b)), ['Publish here', 'Cancel']);
  assert.equal(buttons.filter((b) => b.classList.contains('primary')).length, 1);
  buttons[0].click();
  await enough();
  assert.deepEqual(pcalls(w).filter((c) => c.method === 'PUT').map((c) => c.body), [{ publish: true }, { publish: true, confirm_public: true }]);
  const r = page(w).querySelector('.settings-panel[data-sec=team]');
  assert.equal(prow(r, 'ccboard').querySelector('.tm-confirm'), null);
  assert.equal(prow(r, 'ccboard').querySelector('[data-pub]').checked, true);
  assert.match(words(prow(r, 'ccboard')), /✓ Publishing/);
});

test('Cancel closes the second confirm and writes nothing; a second 409 after the confirm is an error, not a loop', async () => {
  const refuse = (req) => { if (req.method === 'GET') return presView(); const e = new Error('Confirm to publish.'); e.status = 409; e.body = { error: 'Confirm to publish.', needs_confirm: true, public_lists: ['a'], next_body: 'b' }; throw e; };
  const w = teamWorld({ pres: refuse });
  const p = await presPanel(w);
  fireChange(prow(p, 'api').querySelector('[data-pub]'), true);
  await enough();
  let q = page(w).querySelector('.settings-panel[data-sec=team]');
  prow(q, 'api').querySelector('.tm-confirm').querySelectorAll('button').find((b) => text(b) === 'Cancel').click();
  await enough();
  q = page(w).querySelector('.settings-panel[data-sec=team]');
  assert.equal(prow(q, 'api').querySelector('.tm-confirm'), null);
  assert.equal(pcalls(w).filter((c) => c.method === 'PUT').length, 1, 'cancel sends nothing');
  fireChange(prow(q, 'api').querySelector('[data-pub]'), true);
  await enough();
  q = page(w).querySelector('.settings-panel[data-sec=team]');
  prow(q, 'api').querySelector('.tm-go').click();
  await enough();
  q = page(w).querySelector('.settings-panel[data-sec=team]');
  assert.equal(prow(q, 'api').querySelector('.tm-confirm'), null, 'no second box');
  assert.match(text(prow(q, 'api').querySelector('.tm-err')), /^Not saved: Confirm to publish\./);
});

test('Pause team presence sends {paused} once and shows the pause in words; Private sends {private} and is offered only where publishing is on', async () => {
  const seen = [];
  const w = teamWorld({ pres: (req) => {
    seen.push([req.method, req.path, req.body]);
    if (req.method === 'GET') return presView();
    if (req.path === '/api/team/presence') return presView('paused');
    return { ...presView().repos[0], private: true, state: 'private', message: 'Private: this board does not publish here and has taken its line down.', published: false, last_body: null, last_at: null };
  } });
  const p = await presPanel(w);
  fireChange(p.querySelector('[data-pause=all]'), true);
  assert.equal(w.run('teamStore.pres.paused'), true, 'optimistic');
  await enough();
  let q = page(w).querySelector('.settings-panel[data-sec=team]');
  assert.deepEqual(plain(seen.filter((x) => x[0] === 'PUT')), [['PUT', '/api/team/presence', { paused: true }]]);
  assert.equal(q.querySelector('[data-pause=all]').checked, true);
  assert.match(words(q), /On\. This board writes nothing and takes its lines down within a minute\. Your switches stay as they are\./);
  assert.match(words(prow(q, 'website')), /∙ Paused/);
  assert.match(words(prow(q, 'website')), /Team presence is paused on this board\. Nothing is written\./);
  fireChange(prow(q, 'website').querySelector('[data-priv]'), true);
  await enough();
  assert.deepEqual(plain(seen.filter((x) => x[0] === 'PUT').slice(1)), [['PUT', '/api/team/presence/example/website', { private: true }]]);
  q = page(w).querySelector('.settings-panel[data-sec=team]');
  assert.match(words(prow(q, 'website')), /○ Private/);
  assert.match(words(prow(q, 'website')), /On\. This board has taken its line down and writes nothing here\./);
});

test('"What I published" is the literal last body as text, kept open across a repaint; hostile text in it is cut, plain and without control characters', async () => {
  const w = teamWorld();
  const p = await presPanel(w);
  const det = prow(p, 'website').querySelector('details.tm-pub');
  assert.equal(text(det.querySelector('summary')), 'What I published');
  assert.equal(text(det.querySelector('pre')), FX.presence.base.repos[0].last_body, 'the literal body, line break kept');
  assert.match(words(det), /Written \d+ \w+ ago\. This is the text on GitHub\. Its edit history stays readable while the comment exists\./);
  assert.equal(prow(p, 'api').querySelector('details'), null, 'nothing published, nothing to show');
  det.setAttribute('open', '');
  det.dispatchEvent({ type: 'toggle' });
  w.run('teamNotify()');
  await enough();
  assert.ok(prow(page(w).querySelector('.settings-panel[data-sec=team]'), 'website').querySelector('details.tm-pub').hasAttribute('open'));
  const evil = '<img src=x onerror=alert(1)>\nline two' + String.fromCharCode(0x202e) + String.fromCharCode(7) + 'Z'.repeat(6000);
  const hostile = presView();
  hostile.repos[0].last_body = evil;
  const w2 = teamWorld({ pres: hostile });
  const p2 = await presPanel(w2);
  const pre = prow(p2, 'website').querySelector('details pre');
  assert.equal(hasEl(prow(p2, 'website'), 'img'), false);
  assert.ok(text(pre).startsWith('<img src=x onerror=alert(1)>\nline two'), 'the line break stays, the markup is text');
  assert.ok(text(pre).length <= 3200, 'cut');
  assert.ok(!text(pre).includes(String.fromCharCode(0x202e)) && !text(pre).includes(String.fromCharCode(7)));
});

test('every publishing state has its own glyph and words, its own sentence, and a next step where the sentence does not give one', () => {
  const w = teamWorld();
  const states = plain(w.run('Object.keys(TEAM_PUB)'));
  const chips = new Set();
  for (const name of ['', ...Object.keys(FX.presence.variants)]) {
    const r = presView(name).repos[0];
    w.ctx.__r = r;
    const row = w.run('teamPubRow(__r, false)');
    const chip = text(row.querySelector('.tm-pstate'));
    chips.add(chip);
    assert.equal(row.querySelector('.tm-pstate').getAttribute('data-pstate'), r.state);
    if (!['publishing', 'off'].includes(r.state)) assert.ok(words(row).includes(r.message), `${r.state}: the board's sentence`);
  }
  assert.equal(chips.size, 20, [...chips].join(' | '));
  assert.deepEqual(states.sort(), ['archived', 'budget', 'closed', 'deleted', 'error', 'forbidden', 'issues_disabled', 'limited', 'locked', 'needs_confirm', 'no_create', 'no_label', 'off', 'paused', 'private',
    'publishing', 'rate_limited', 'removing', 'too_long', 'waiting']);
  const word = (name) => words(w.run((w.ctx.__r = presView(name).repos[0], 'teamPubRow(__r, false)')));
  assert.match(word('issuesoff'), /Issues are turned off in this repository, so there is nowhere to publish\. Next: Turn Issues on in the repository settings on GitHub, or switch publishing off\./);
  assert.match(word('archived'), /The repository is archived.*Next: Nothing can be written to an archived repository/);
  assert.match(word('locked'), /The presence issue is locked.*Next: Unlock the presence issue on GitHub/);
  assert.match(word('closed'), /The presence issue is closed, so this board stopped publishing\. Reopen it on GitHub to go on\./);
  assert.match(word('nocreate'), /may not open issues here.*Next: Ask the owner of the repository to let this login open issues/);
  assert.match(word('nolabel'), /triage rights.*Next: Ask the owner to give this login triage rights/);
  assert.match(word('forbidden'), /GitHub refused the write for this login\. Next: Ask the owner of the repository for write access.*Some organizations do not let tools write to their issues/);
  assert.match(word('limited'), /interaction limits/);
  assert.match(word('budget'), /hourly presence budget is spent/);
  assert.match(word('confirm'), /Next: Read the box below and confirm/);
  assert.equal(/[\u2013\u2014]/.test(JSON.stringify(plain(w.run('[TEAM_PUB, TEAM_PUB_NEXT, TEAM_AUDIT, TEAM_OPT_IN]')))), false, 'no dash in UI text');
});

test('the publishing controls are 28 px with a mouse and 44 px on touch, and a long body cannot make the page scroll sideways', () => {
  const css = fs.readFileSync(path.join(STATIC, 'pages', 'team.css'), 'utf8');
  assert.match(css, /@media \(pointer:coarse\) \{[^}]*#page \.tm-confirm-a button \{ min-height:var\(--tap\)/s);
  assert.match(css, /html\.force-coarse #page[^{]*\.tm-confirm-a button[^{]*\{ min-height:var\(--tap\)/);
  assert.match(css, /\.tm-body \{[^}]*white-space:pre-wrap; overflow-wrap:anywhere;/);
  assert.match(css, /\.tm-pl \{[^}]*flex-wrap:wrap/);
  assert.doesNotMatch(css, /style=|cssText|innerHTML/);
});

test('Settings > Team reads the publishing settings only while it is the open tab, and the project page never does', async () => {
  const w = teamWorld();
  await presPanel(w);
  assert.equal(pcalls(w).filter((c) => c.method === 'GET').length, 1);
  w.location.hash = '#/settings?sec=box';
  await settle();
  assert.equal(w.run('teamStore.presSubs.size'), 0);
  const q = teamWorld();
  q.run('teamWatch(() => {})');
  await settle();
  assert.equal(pcalls(q).length, 0, 'a watcher that did not ask for it');
});

// ---------------------------------------------------------------- the demo: the publishing settings and their variants

test('?demo=1 answers /api/team/presence from the fixture, and every ?presence= variant is one state', async () => {
  const d0 = plain(await demoWorld('?demo=1').run("api('GET', '/api/team/presence')"));
  assert.deepEqual(d0.repos.map((r) => [r.slug, r.publish, r.state, r.public]), [['example/website', true, 'publishing', false], ['example/api', false, 'off', null], ['example/ccboard', false, 'off', true]]);
  assert.equal(d0.paused, false);
  const at = Date.parse(/ at=(\S+)/.exec(d0.repos[0].last_body)[1]);
  assert.ok(Math.abs(Date.now() - at) < 10 * 60000, 'the times inside the body follow the clock');
  assert.ok(Math.abs(Date.now() - Date.parse(d0.repos[0].last_at)) < 10 * 60000);
  const seen = new Set();
  for (const name of Object.keys(FX.presence.variants)) {
    const d = plain(await demoWorld(`?demo=1&presence=${name}`).run("api('GET', '/api/team/presence')"));
    seen.add(d.repos[0].state);
    assert.equal(d.repos[0].state, FX.presence.variants[name].repo.state, name);
  }
  assert.equal(seen.size, 19);
  assert.equal(plain(await demoWorld('?demo=1&presence=paused').run("api('GET', '/api/team/presence')")).paused, true);
  assert.equal(plain(await demoWorld('?demo=1&presence=nonsense').run("api('GET', '/api/team/presence')")).repos[0].state, 'publishing');
  const live = plain(await demoWorld('?demo=1&team=lineneeds').run("api('GET', '/api/team/state')"));
  const bobs = live.repos[0].people.find((p) => p.login === 'bob');
  assert.deepEqual([bobs.presence.live, bobs.presence.boards[0].state, live.repos[0].counts.live], ['live', 'needs_you', 1]);
  assert.equal(plain(await demoWorld('?demo=1&team=linestale').run("api('GET', '/api/team/state')")).repos[0].counts.live, 0);
});

test('in the demo the switches are played for the life of the page: the public repository asks for the confirm, then publishes; off, private and pause follow', async () => {
  const w = demoWorld('?demo=1');
  const err = await w.run("api('PUT', '/api/team/presence/example/ccboard', { publish: true }).catch((e) => ({ status: e.status, body: e.body }))");
  assert.equal(err.status, 409);
  assert.equal(err.body.needs_confirm, true);
  assert.equal(err.body.public_lists.length, 4);
  assert.match(err.body.next_body, /^<!-- ccboard:presence v1 -->\nccboard presence: node=node-a nid=1a2b3c4d repo=example\/ccboard seq=1 at=/);
  assert.equal(plain(await w.run("api('GET', '/api/team/presence')")).repos[2].publish, false, 'a refused switch changes nothing');
  const on = plain(await w.run("api('PUT', '/api/team/presence/example/ccboard', { publish: true, confirm_public: true })"));
  assert.deepEqual([on.publish, on.state, on.published, on.public_confirmed, on.issue], [true, 'publishing', true, true, 1]);
  assert.match(on.last_body, /repo=example\/ccboard/);
  const priv = plain(await w.run("api('PUT', '/api/team/presence/example/ccboard', { private: true })"));
  assert.deepEqual([priv.state, priv.published, priv.last_body, priv.private], ['private', false, null, true]);
  const off = plain(await w.run("api('PUT', '/api/team/presence/example/ccboard', { publish: false })"));
  assert.deepEqual([off.state, off.publish, off.private, off.public_confirmed], ['off', false, false, false]);
  assert.equal((await w.run("api('PUT', '/api/team/presence/example/ccboard', { publish: true }).catch((e) => e.status)")), 409, 'the confirm is asked again after an off');
  await assert.rejects(w.run("api('PUT', '/api/team/presence/example/api', { private: true })"), /switch it on first/);
  const ok = plain(await w.run("api('PUT', '/api/team/presence/example/website', { private: true })"));
  assert.equal(ok.state, 'private');
  const paused = plain(await w.run("api('PUT', '/api/team/presence', { paused: true })"));
  assert.equal(paused.paused, true);
  assert.deepEqual(paused.repos.map((r) => r.state), ['paused', 'off', 'off'], 'the pause comes before Private, as it does on the board');
  const again = plain(await w.run("api('PUT', '/api/team/presence/example/website', { private: false })"));
  assert.equal(again.state, 'paused');
  assert.equal(again.private, false);
  assert.equal(plain(await w.run("api('PUT', '/api/team/presence', { paused: false })")).repos[0].state, 'publishing');
  await assert.rejects(w.run("api('PUT', '/api/team/presence/other/thing', { publish: true })"), /no project/);
  await assert.rejects(w.run("api('PUT', '/api/team/presence/example/api', {})"), /say publish or private/);
  await assert.rejects(w.run("api('PUT', '/api/team/presence', { paused: 'yes' })"), /true or false/);
});

test('the presence issue is named as a plain link inside its sentence, not as a button', () => {
  const w = teamWorld();
  const n = node(w, block());
  const a = [...n.querySelectorAll('a')].find((x) => /presence issue #/.test(text(x)));
  assert.ok(a, 'the block links the presence issue');
  assert.ok(a.classList.contains('tm-tl'));
});

// ================================================================ issue #199, Team P3: conflict warnings (the builders), the marks in the project page's Team block, and the demo

test('teamBranchFor makes the name git gives the branch of a task with this title (app/tasks.py slugify, then worktree-<slug>)', () => {
  const w = teamWorld();
  const f = (t) => w.run(`teamBranchFor(${JSON.stringify(t)})`);
  assert.equal(f('Fix the login redirect'), 'worktree-fix-the-login-redirect');
  assert.equal(f('#154 Add a gift wrap option'), 'worktree-154-add-a-gift-wrap-option');
  assert.equal(f('  --Hello,   World!!  '), 'worktree-hello-world');
  assert.equal(f(''), 'worktree-task');
  assert.equal(f('!!!'), 'worktree-task');
  assert.equal(f(null), 'worktree-task');
  const long = f('word '.repeat(30));
  assert.ok(long.length <= 'worktree-'.length + 40 && !long.endsWith('-'), long);
  assert.equal(f('word '.repeat(30)), 'worktree-' + 'word-'.repeat(8).slice(0, 40).replace(/-+$/, ''), 'cut at 40 characters, then the trailing dash goes');
});

test('teamWho is @login, or "a teammate" when the login is missing or fails the rule; never "@null"', () => {
  const w = teamWorld();
  const who = (p) => w.run(`teamWho(${JSON.stringify(p)})`);
  assert.equal(who({ id: 1, login: 'bob' }), '@bob');
  assert.equal(who({ id: 1, login: 'dependabot[bot]' }), '@dependabot[bot]');
  for (const p of [{ id: 1, login: null }, { id: 1 }, null, 'bob', { id: 1, login: '<img src=x onerror=alert(1)>' }, { id: 1, login: '../x' }, { id: 1, login: 'a'.repeat(80) }]) assert.equal(who(p), 'a teammate');
});

test('the issue chip and the lines work from the cached block: taken, recent and stale each have a glyph and words, and a read that moved on adds to the age', () => {
  const w = teamWorld();
  const view = (b) => { w.ctx.__b = b; return w.run("teamStore.data = { repos: [__b] }; teamLaunchView('phasezero', 'website')"); };
  const v = view(fresh(block()));
  const chip = (n) => { w.ctx.__v = v; return w.run(`teamIssueChip(__v, ${n})`); };
  assert.equal(chip(152), '! taken by @bob');
  assert.equal(chip(156), '◌ @bob, seen 7 min ago');
  assert.equal(chip(160), '◌ @bob, last seen 3 h ago');
  assert.equal(chip(162), '! taken by a teammate');
  assert.equal(chip(164), '! taken by @bob, +1');
  assert.equal(chip(47), '');
  assert.equal(chip(0), '');
  const later = view(fresh(block(), 3600));                                  // read an hour ago: the stale line is an hour older, never younger
  w.ctx.__v = later;
  assert.equal(w.run('teamIssueChip(__v, 160)'), '◌ @bob, last seen 4 h ago');
  assert.equal(view(fresh({ ...block(), warnings: null })).warnings, null);
  assert.equal(w.run("teamIssueChip(teamLaunchView('phasezero', 'website'), 152)"), '', 'no warnings, no chip');
});

test('the lines for a picked issue name the person and the signs; since is shown only when a live status line gave it', () => {
  const w = teamWorld();
  w.ctx.__b = fresh(block());
  w.run('teamStore.data = { repos: [__b] }');
  const L = (ctx) => { w.ctx.__c = ctx; return plain(w.run("teamLaunchLines(teamLaunchView('phasezero', 'website'), __c).map((l) => ({ kind: l.kind, status: l.status, glyph: l.glyph, detail: l.detail, head: l.head.map((h) => (typeof h === 'string' ? h : h.textContent)) }))")); };
  const flat = (l) => `${l.glyph} ${l.head.join('')} ${l.detail}`;
  const a = L({ issue: 152 });
  assert.equal(a.length, 1);
  assert.match(flat(a[0]), /^! #152 is being worked on by @bob since .*\d\d:\d\d assigned on GitHub · live status: running · branch bob\/coupon-total · draft PR #158$/);
  assert.match(flat(L({ issue: 154 })[0]), /^! #154 is being worked on by @bob assigned on GitHub · holds a claim \(running\)$/);
  assert.deepEqual(L({ issue: 47 }), []);
  assert.deepEqual(L({}), []);
  assert.deepEqual(L(null), []);
  assert.deepEqual(L({ issue: 'x' }), []);
  const t7 = L({ task: 7 });
  assert.deepEqual(t7.map((l) => l.kind), ['files', 'files']);
  assert.deepEqual(L({ task: 99 }), []);
  const t4 = L({ task: 4 });
  assert.deepEqual(t4.map((l) => l.kind), ['issue', 'branch', 'files']);
  assert.equal(L({ task: 4, issue: 152 }).filter((l) => l.kind === 'issue').length, 1, 'the same issue is said once');
});

test('a taken issue whose status line is stale says "last seen" and never "since"; a live line says since and no age', () => {
  const w = teamWorld();
  const b = fresh(block());
  const taken = b.warnings.issues['154'][0];
  b.warnings.issues['154'] = [{ ...taken, since: null, since_from: null, stale_age_s: 10800 }];
  w.ctx.__b = b;
  w.run('teamStore.data = { repos: [__b] }');
  const head = (n) => w.run(`teamLaunchLines(teamLaunchView('phasezero', 'website'), { issue: ${n} })[0].head.join('')`);
  assert.equal(head(154), '#154 is being worked on by @bob, last seen 3 h ago');
  b.warnings.issues['154'][0].stale_age_s = null;
  assert.equal(head(154), '#154 is being worked on by @bob', 'no age known: nothing is invented');
  b.warnings.issues['154'][0].stale_age_s = '10800';
  assert.equal(head(154), '#154 is being worked on by @bob', 'an age that is not a whole number is ignored');
  assert.match(head(152), /^#152 is being worked on by @bob since .*\d\d:\d\d$/, 'a live line gives since and no last seen');
  const later = fresh(block(), 3600);                                       // read an hour ago: the age grows with it
  later.warnings.issues['154'] = [{ ...taken, since: null, since_from: null, stale_age_s: 10800 }];
  w.ctx.__b = later;
  w.run('teamStore.data = { repos: [__b] }');
  assert.equal(head(154), '#154 is being worked on by @bob, last seen 4 h ago');
});

test('more than four people on one issue say how many more, a warning that is not sealed as advice is dropped, and a task or branch name that is not an own key is not read', () => {
  const w = teamWorld();
  const b = fresh(block());
  const bob = b.warnings.issues['152'][0];
  b.warnings.issues['152'] = Array.from({ length: 7 }, (_, i) => ({ ...bob, person: { id: 2000 + i, login: `user${i}` } }));
  b.warnings.issues['154'][0].advice = false;
  b.warnings.branches['__proto__'] = [{ ...b.warnings.branches['bob/coupon-total'][0], name: 'x' }];
  w.ctx.__b = b;
  w.run('teamStore.data = { repos: [__b] }');
  const L = (ctx) => { w.ctx.__c = ctx; return plain(w.run("teamLaunchLines(teamLaunchView('phasezero', 'website'), __c).map((l) => ({ kind: l.kind, head: l.head.map(String) }))")); };
  const l = L({ issue: 152 });
  assert.equal(l.length, 5);
  assert.equal(l[4].head.join(''), 'and 3 more');
  assert.deepEqual(L({ issue: 154 }), [], 'not sealed as advice: not shown');
  assert.deepEqual(L({ title: '__proto__' }), []);
  assert.deepEqual(L({ task: 'constructor' }), []);
});

test('the quiet note says why "no warning" may mean "not known": a first read, a failed read with the last good one, an old read, an unknown login; and nothing for a fresh clean read', () => {
  const w = teamWorld();
  const note = (b) => { w.ctx.__b = b; return w.run('teamWarnNote(__b, __b.warnings)'); };
  assert.equal(note(fresh(block())), '');
  assert.equal(note(fresh(block('off'))), '');
  assert.match(note(fresh(block('loading'))), /being read for the first time\. Nothing is checked yet\./);
  assert.match(note(fresh(block('rate'))), /^Team: waiting for github\. Warnings are from the last read/i);
  assert.match(note(fresh(block('sso'))), /^Team: authorise the token\. /i);
  assert.match(note(fresh({ ...block('stale') }, 2800)), /^Team: last read 47 min ago, so warnings may be out of date\.$/);
  assert.match(note(fresh(block('warnnoid'))), /who you are on GitHub is not known yet/);
  assert.equal(note(fresh(block('warnnone'))), '');
  assert.equal(note(fresh(block('warnempty'))), '');
});

test('every warning is a glyph plus words, in the warn colour and never a red error: no line is drawn with the bad class, and the block of lines carries no role=alert', () => {
  const w = teamWorld();
  w.ctx.__b = fresh(block());
  w.run('teamStore.data = { repos: [__b] }');
  const host = w.run("document.createElement('div')");
  w.ctx.__h = host;
  const n = w.run("teamLaunchPaint(__h, teamLaunchView('phasezero', 'website'), { issue: 164, task: 7, title: '#154 Add a gift wrap option' })");
  assert.ok(n >= 5);
  for (const l of host.querySelectorAll('.lx-team-l')) {
    assert.ok(text(l.querySelector('.lx-team-g')).length >= 1, 'a glyph');
    assert.ok(words(l).length > 12, 'and words');
    assert.ok(!l.classList.contains('bad'));
  }
  assert.ok(!host.querySelector('[role=alert]'));
  const css = fs.readFileSync(path.join(STATIC, 'pages', 'team.css'), 'utf8');
  assert.match(css, /\.lx-team \{[^}]*border-left:3px solid var\(--warn\)/);
  assert.ok(!/\.lx-team[^{]*\{[^}]*var\(--bad/.test(css), 'never the error colour');
  assert.match(css, /\.lx-team\.quiet \{/);
});

test('a row of the project page Team block that collides with one of the owner\'s open tasks is marked with a glyph and words; other rows and the owner\'s own are not', () => {
  const w = teamWorld();
  const n = node(w, fresh(block()));
  const marks = n.querySelectorAll('.tm-collide');
  assert.equal(marks.length, 3);
  assert.ok(marks.every((m) => text(m.querySelector('.tm-sg')) === '!' && m.getAttribute('role') === 'note'));
  const rowOf = (re) => n.querySelectorAll('.tm-item').find((i) => re.test(words(i)));
  assert.match(words(rowOf(/Coupon codes are not applied/).querySelector('.tm-collide')), /^! Collides: same issue as your task “Redesign checkout step 2 for mobile”$/);
  assert.match(words(rowOf(/Apply coupon codes before tax/).querySelector('.tm-collide')),
    /^! Collides: same issue as your task “Redesign checkout step 2 for mobile”; changes 1 file that your task “Redesign checkout step 2 for mobile” changes: src\/pages\/checkout\.tsx$/);
  assert.match(words(rowOf(/worktree-checkout-redesign/).querySelector('.tm-collide')), /^! Collides: same branch as your task/);
  assert.equal(rowOf(/Add a gift wrap option/).querySelector('.tm-collide'), null, 'no collision, no mark');
  assert.equal(n.querySelector('.tm-person[data-you=yes]').querySelectorAll('.tm-collide').length, 0);
  const plainBlock = fresh(block());
  for (const p of plainBlock.people) for (const k of ['issues', 'branches', 'prs']) for (const r of p[k]) delete r.collides;
  assert.equal(node(w, plainBlock).querySelectorAll('.tm-collide').length, 0, 'a block without collisions is what it was');
});

test('a mark is text: a task title with markup, a file with control characters, and a list of many files are cut; an unknown kind is dropped', () => {
  const w = teamWorld();
  const b = fresh(block());
  const row = b.people[1].prs[0];
  row.collides = [{ task: 4, title: '<img src=x onerror=alert(1)>\u202e', kind: 'files', files: ['<b>a</b>.py', 'b\u0007.py', 'c.py', 'd.py'], count: 40, more: true }, { task: 5, title: 't', kind: 'weird' }, null];
  const n = node(w, b);
  assert.equal(n.querySelectorAll('img').length, 0);
  const m = words(n.querySelectorAll('.tm-item').find((i) => /Apply coupon/.test(words(i))).querySelector('.tm-collide'));
  assert.match(m, /changes 40 files that your task “<img src=x onerror=alert\(1\)>” changes: <b>a<\/b>\.py, b \.py, c\.py, …$/);
});

test('the Team block repaints when the warnings change (the signature carries them)', () => {
  const w = teamWorld();
  w.ctx.__b = fresh(block());
  w.run('teamStore.data = { repos: [__b] }');
  const s1 = w.run('teamSig()');
  w.ctx.__b = { ...fresh(block()), warnings: null };
  w.run('teamStore.data = { repos: [__b] }');
  assert.notEqual(w.run('teamSig()'), s1);
});

test('?demo=1 answers every block with warnings: the base block has every form, the variants change what is known, a switched-on repository has an empty set and an off one none', async () => {
  const d0 = plain(await demoWorld('?demo=1').run("api('GET', '/api/team/state')"));
  const web = d0.repos[0].warnings;
  assert.equal(web.api, 1);
  assert.deepEqual(Object.keys(web.issues), ['152', '154', '156', '160', '162', '164']);
  assert.deepEqual(d0.repos.slice(1).map((r) => r.warnings), [null, null]);
  for (const [name, check] of [['warnnone', (x) => x === null], ['warnnoid', (x) => x.identity_known === false], ['warnempty', (x) => Object.keys(x.issues).length === 0], ['off', (x) => x === null],
    ['loading', (x) => x === null], ['warnfiles', (x) => x.tasks['7'].files[0].more === true]]) {
    const d = plain(await demoWorld(`?demo=1&team=${name}`).run("api('GET', '/api/team/state')"));
    assert.ok(check(d.repos[0].warnings), name);
  }
  const w = demoWorld('?demo=1');
  const on = plain(await w.run("api('PUT', '/api/team/repos/example/api', { shared: true })"));
  assert.deepEqual([on.shared, on.warnings.api, Object.keys(on.warnings.issues)], [true, 1, []]);
  const off = plain(await w.run("api('PUT', '/api/team/repos/example/website', { shared: false })"));
  assert.equal(off.warnings, null);
});

test('in the demo the launcher reads the warnings: state.json says a repository is shared, and the issues it lists include the ones the warnings talk about', () => {
  const st = JSON.parse(fs.readFileSync(path.join(STATIC, 'demo', 'state.json'), 'utf8'));
  assert.deepEqual(st.team, { shared: true });
  const issues = JSON.parse(fs.readFileSync(path.join(STATIC, 'demo', 'issues.json'), 'utf8'));
  const listed = new Set(issues.issues.map((i) => String(i.number)));
  for (const n of Object.keys(block().warnings.issues)) assert.ok(listed.has(n) && issues.detail[n], `issue ${n} can be picked in the demo`);
});
