// Contract tests for the v0.5.3 pages (app/static/pages/*.js) running on the real core.js, components.js, router.js and
// launcher.js inside the vm harness. The harness' own nodes are too thin for keyed lists (textContent does not clear children,
// append duplicates), so this file installs a small but faithful DOM (tree, selectors, events, dialogs) into the world first.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC, makeWorld, plain } from './harness.mjs';

// ---------------------------------------------------------------- a small DOM

class MText {
  constructor(t) { this.nodeType = 3; this._t = String(t); this.parentNode = null; }
  get textContent() { return this._t; }
  set textContent(v) { this._t = String(v); }
}

const COMPOUND = /#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:([\^$*]?=)"?([^\]"]*)"?)?\]/g;

function parseCompound(s) {
  const m = /^([a-zA-Z*][\w-]*)?(.*)$/.exec(s);
  const c = { tag: m[1] && m[1] !== '*' ? m[1].toUpperCase() : null, id: null, classes: [], attrs: [] };
  for (const x of m[2].matchAll(COMPOUND)) {
    if (x[1]) c.id = x[1];
    else if (x[2]) c.classes.push(x[2]);
    else c.attrs.push({ name: x[3], op: x[4] || null, value: x[5] });
  }
  return c;
}

function matchCompound(n, c) {
  if (n.nodeType !== 1) return false;
  if (c.tag && n.tagName !== c.tag) return false;
  if (c.id && n.getAttribute('id') !== c.id) return false;
  for (const k of c.classes) if (!n.classList.contains(k)) return false;
  for (const a of c.attrs) {
    const v = n.getAttribute(a.name);
    if (v === null) return false;
    if (a.op === '=' && v !== a.value) return false;
    if (a.op === '^=' && !v.startsWith(a.value)) return false;
    if (a.op === '$=' && !v.endsWith(a.value)) return false;
    if (a.op === '*=' && !v.includes(a.value)) return false;
  }
  return true;
}

function matchSelector(n, sel) {                       // one selector with descendant combinators
  const chain = sel.trim().split(/\s+/).map(parseCompound);
  if (!matchCompound(n, chain[chain.length - 1])) return false;
  let at = n.parentNode;
  for (let i = chain.length - 2; i >= 0; i--) {
    while (at && !matchCompound(at, chain[i])) at = at.parentNode;
    if (!at) return false;
    at = at.parentNode;
  }
  return true;
}

function matchesAny(n, list) { return list.split(',').some((s) => matchSelector(n, s)); }

function installDom(w) {
  let active = null;
  class El {
    constructor(tag) {
      this.nodeType = 1; this.tagName = String(tag).toUpperCase(); this.childNodes = []; this.parentNode = null;
      this._attrs = new Map(); this._cls = new Set(); this._on = {}; this.style = {}; this.dataset = {}; this.open = false;
      this.scrollTop = 0; this.disabled = false; this.value = '';
      const self = this;
      this.classList = {
        add(...c) { c.forEach((x) => self._cls.add(x)); },
        remove(...c) { c.forEach((x) => self._cls.delete(x)); },
        toggle(c, force) { const on = force === undefined ? !self._cls.has(c) : !!force; if (on) self._cls.add(c); else self._cls.delete(c); return on; },
        contains(c) { return self._cls.has(c); },
      };
    }
    get className() { return [...this._cls].join(' '); }
    set className(v) { this._cls = new Set(String(v).split(/\s+/).filter(Boolean)); }
    setAttribute(k, v) { if (k === 'class') this.className = v; else this._attrs.set(k, String(v)); }
    getAttribute(k) { if (k === 'class') return this._cls.size ? this.className : null; return this._attrs.has(k) ? this._attrs.get(k) : null; }
    removeAttribute(k) { this._attrs.delete(k); }
    hasAttribute(k) { return this._attrs.has(k); }
    get id() { return this.getAttribute('id') || ''; }
    get children() { return this.childNodes.filter((n) => n.nodeType === 1); }
    get firstChild() { return this.childNodes[0] || null; }
    get firstElementChild() { return this.children[0] || null; }
    get childElementCount() { return this.children.length; }
    get nextSibling() { const s = this.parentNode ? this.parentNode.childNodes : []; return s[s.indexOf(this) + 1] || null; }
    get nextElementSibling() { const s = this.parentNode ? this.parentNode.children : []; return s[s.indexOf(this) + 1] || null; }
    get isConnected() { let n = this; while (n.parentNode) n = n.parentNode; return n === w.document.documentElement; }
    _detach(n) { if (n.parentNode) n.parentNode.childNodes.splice(n.parentNode.childNodes.indexOf(n), 1); n.parentNode = null; }
    append(...kids) {
      for (const k of kids) {
        const n = typeof k === 'string' ? new MText(k) : k;
        this._detach(n); n.parentNode = this; this.childNodes.push(n);
      }
    }
    appendChild(n) { this.append(n); return n; }
    insertBefore(n, ref) {
      this._detach(n);
      if (!ref) { this.append(n); return n; }
      n.parentNode = this; this.childNodes.splice(this.childNodes.indexOf(ref), 0, n);
      return n;
    }
    removeChild(n) { this._detach(n); return n; }
    remove() { this._detach(this); }
    get textContent() { return this.childNodes.map((n) => n.textContent).join(''); }
    set textContent(v) {
      for (const n of this.childNodes) n.parentNode = null;
      this.childNodes = [];
      if (String(v) !== '') this.append(String(v));
    }
    contains(n) { while (n) { if (n === this) return true; n = n.parentNode; } return false; }
    querySelectorAll(sel) {
      const out = [];
      const walk = (n) => { for (const c of n.children) { if (matchesAny(c, sel)) out.push(c); walk(c); } };
      walk(this);
      return out;
    }
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
    matches(sel) { return matchesAny(this, sel); }
    closest(sel) { let n = this; while (n && n.nodeType === 1) { if (matchesAny(n, sel)) return n; n = n.parentNode; } return null; }
    addEventListener(type, fn) { (this._on[type] ||= []).push(fn); }
    removeEventListener(type, fn) { this._on[type] = (this._on[type] || []).filter((f) => f !== fn); }
    dispatchEvent(ev) {
      ev.target = ev.target || this;
      for (let n = this; n && n.nodeType === 1 && !ev._stopped; n = n.parentNode) {
        ev.currentTarget = n;
        for (const fn of [...(n._on[ev.type] || [])]) fn(ev);
        if (ev.type === 'close') break;
      }
      return true;
    }
    click() {
      if (this.disabled) return;
      this.dispatchEvent({ type: 'click', preventDefault() {}, stopPropagation() { this._stopped = true; } });
    }
    focus() { active = this; }
    blur() { if (active === this) active = null; }
    showModal() { this.open = true; }
    close() { if (!this.open) return; this.open = false; this.dispatchEvent({ type: 'close' }); }
    getBoundingClientRect() { return { top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 }; }
  }
  const doc = w.document;
  const html = new El('html');
  const body = new El('body');
  html.append(body);
  Object.assign(doc, {
    createElement: (t) => new El(t), createElementNS: (_ns, t) => new El(t), createTextNode: (t) => new MText(t),
    body, documentElement: html,
    querySelector: (s) => body.querySelector(s), querySelectorAll: (s) => body.querySelectorAll(s),
    getElementById: (id) => body.querySelector('#' + id),
  });
  Object.defineProperty(doc, 'activeElement', { get: () => active, configurable: true });
  const mk = (tag, id, cls) => { const n = new El(tag); n.setAttribute('id', id); if (cls) n.className = cls; return n; };
  const main = mk('main', 'main');
  main.append(mk('div', 'banner'), mk('div', 'page'));
  body.append(mk('header', 'topbar'), mk('aside', 'sidebar'), main, mk('aside', 'dock', 'hidden'), mk('nav', 'bnav'),
    mk('dialog', 'drawer'), mk('dialog', 'sheet'), mk('dialog', 'helpdlg'), mk('div', 'modal', 'hidden'), mk('div', 'toasts'));
  return { El, body, page: () => doc.querySelector('#page') };
}

// ---------------------------------------------------------------- fixtures and the world

const ISO = (minsAgo) => new Date(Date.now() - minsAgo * 60000).toISOString();
const sess = (name, over) => ({
  tmux: `shop--api--${name}`, name, created: Math.floor(Date.now() / 1000) - 7200, attached: 0, command: 'claude', launcher: 'claude',
  state: 'idle', state_at: ISO(30), needs_attention: false, last_prompt: 'fix the login bug', last_message: 'done, tests pass',
  stats: { model: 'Opus 5', context_pct: 42, cost_usd: 1.5 }, ...over,
});

function fakeState(over = {}) {
  return {
    tmux_down: false, user: 'alice', version: 'test',
    config: { code_https_port: 10000, projects_dir: '/srv/projects', ntfy: { enabled: false }, backup: {} },
    claude: { installed: true, loggedIn: true, email: 'a@example.com', subscriptionType: 'max' }, login: { running: false },
    projects: [
      { name: 'shop', path: '/srv/projects/shop', root: null, orphan_sessions: [], repos: [
        { name: 'api', path: '/srv/projects/shop/api', state: 'ok', branch: 'main', dirty: false, devcontainer: false, sessions: [
          sess('s1', { state: 'working', state_at: ISO(2) }),
          sess('s2', { state: 'waiting', needs_attention: true, state_at: ISO(10) }),
        ] }] },
      { name: 'blog', path: '/srv/projects/blog', root: null, orphan_sessions: [], repos: [
        { name: 'web', path: '/srv/projects/blog/web', state: 'ok', branch: 'main', dirty: false, devcontainer: false, sessions: [
          { ...sess('s3', { state: 'idle' }), tmux: 'blog--web--s3' },
          { ...sess('sh', { state: 'idle', launcher: 'shell', command: 'bash', stats: null }), tmux: 'blog--web--sh' },
        ] }] },
    ],
    pending_permissions: [{ id: 7, tmux_name: 'shop--api--s2', tool_name: 'Bash', summary: 'Bash: npm test' }],
    tasks: [], jobs: [], runs: [], nodes: null, health: null, backup: null, ...over,
  };
}

const PAGE_FILES = ['home', 'inbox', 'tasks', 'agents', 'settings', 'search', 'session', 'placeholders'];

/** A world with the DOM, the real scripts in index.html order (shell.js left out), and recorders for api, toast and registerPage. */
function pagesWorld({ wide = false, extra = {}, state = fakeState(), realPoll = false } = {}) {
  const w = makeWorld({ matchMedia: (q) => ({ matches: wide && /1024/.test(q), addEventListener() {}, removeEventListener() {} }), ...extra });
  const dom = installDom(w);
  for (const f of ['core.js', 'components.js', 'live.js', 'launcher.js', 'router.js']) w.load(f);
  w.ctx.__calls = []; w.ctx.__toasts = []; w.ctx.__registered = []; w.ctx.__mounts = {}; w.ctx.__searchHits = [];
  w.run(`
    api = async (method, path, body) => {
      __calls.push({ method, path, body });
      if (path.startsWith('/api/search')) return { results: __searchHits };
      return { ok: true };
    };
    toast = (text, o) => { __toasts.push({ text, kind: o && o.kind }); };
    { const real = registerPage; registerPage = (id, page) => { __registered.push(id); return real(id, page); }; }
  `);
  if (!realPoll) w.run('poll = async () => {};');
  for (const f of PAGE_FILES) w.load(`pages/${f}.js`);
  // count real mounts per page id
  w.run(`for (const id of Object.keys(pages)) { const m = pages[id].mount; __mounts[id] = 0; pages[id].mount = function (...a) { __mounts[id]++; return m.apply(this, a); }; }`);
  w.ctx.__st = state;
  w.run('state = __st');
  return { w, dom };
}

const page = (w) => w.document.querySelector('#page');
const tick = () => new Promise((r) => setImmediate(r));
const rows = (root) => root.querySelectorAll('.rrow').map((n) => n.getAttribute('data-tmux'));
const mounts = (w, id) => w.get('__mounts')[id];
const calls = (w) => plain(w.get('__calls'));
const text = (n) => n.textContent;

// ---------------------------------------------------------------- registration

test('exactly one registerPage per route id, across pages/*.js', () => {
  const { w } = pagesWorld();
  const ids = plain(w.run('ROUTES.map((r) => r.id)'));
  const registered = plain(w.get('__registered'));
  assert.deepEqual([...registered].sort(), [...ids].sort(), 'every ROUTES id registers exactly one page');
  assert.equal(new Set(registered).size, registered.length);
  assert.deepEqual([...ids].sort(), ['agents', 'home', 'inbox', 'memory', 'onboarding', 'project', 'quad', 'search', 'session', 'settings', 'tasks', 'usage']);
  // and the sources agree: one registerPage('<id>' literal per id in pages/*.js
  const seen = [];
  for (const f of fs.readdirSync(path.join(STATIC, 'pages'))) {
    if (!f.endsWith('.js')) continue;
    for (const m of fs.readFileSync(path.join(STATIC, 'pages', f), 'utf8').matchAll(/registerPage\(\s*'([a-z]+)'/g)) seen.push(m[1]);
  }
  assert.deepEqual([...seen].sort(), [...ids].sort());
});

test('every page has mount and update; the peek is an ordinary routed page', () => {
  const { w } = pagesWorld();
  for (const id of plain(w.run('Object.keys(pages)'))) {
    assert.equal(w.run(`typeof pages['${id}'].mount`), 'function', id);
    assert.equal(w.run(`typeof pages['${id}'].update`), 'function', id);
  }
  assert.equal(w.run("typeof pages.session.onRoute"), 'function', 'the peek swaps panels through onRoute');
});

// ---------------------------------------------------------------- the agents roster

test('#/agents mounts into #page, updates with the state and lists the waiting session first', () => {
  const { w } = pagesWorld();
  w.location.hash = '#/agents';
  const root = page(w);
  assert.equal(mounts(w, 'agents'), 1);
  assert.equal(w.document.title, 'Agents · ccboard');
  assert.equal(w.document.body.getAttribute('data-page'), 'agents');
  const list = rows(root);
  assert.equal(list.length, 4);
  assert.deepEqual(list.slice(0, 2), ['shop--api--s2', 'shop--api--s1'], 'waiting first, then working');
  assert.deepEqual(root.querySelectorAll('.rg-name').map(text), ['shop', 'blog'], 'the group with the waiting session leads');
  assert.equal(text(root.querySelector('.summary')), '✻ 1 need you · ✽ 1 working · ∙ 2 idle · ✓ 0 done');
});

test('#/agents with just a waiting and a working session renders two rows, waiting first', () => {
  const st = fakeState();
  st.projects = [st.projects[0]];
  const { w } = pagesWorld({ state: st });
  w.location.hash = '#/agents';
  assert.deepEqual(rows(page(w)), ['shop--api--s2', 'shop--api--s1']);
});

test('a row shows glyphs, name, repo, age, model and context, last prompt and message, Open / Ack / Kill and the chips', () => {
  const { w } = pagesWorld();
  w.location.hash = '#/agents';
  const row = page(w).querySelector('.rrow[data-tmux=shop--api--s2]');
  assert.ok(row.querySelector('.glyph.waiting') && row.querySelector('.glyph.agent'));
  assert.equal(text(row.querySelector('.rr-name')), 's2');
  assert.equal(row.querySelector('.rr-name').getAttribute('href'), '#/s/shop--api--s2', 'the name opens the peek');
  assert.equal(text(row.querySelector('.rr-where')), 'api');
  const age = row.querySelector('time.age');
  assert.match(text(age), /^\d+m$/);
  assert.ok(Number(age.getAttribute('data-epoch')) > 0);
  assert.equal(text(row.querySelector('.rr-meta')), 'needs you · Opus 5 · ctx 42%');
  assert.match(text(row.querySelector('.rr-last')), /fix the login bug/);
  assert.match(text(row.querySelector('.rr-last')), /done, tests pass/);
  const open = row.querySelector('a[href="/term/shop--api--s2"]');
  assert.ok(open && text(open) === 'Open');
  assert.ok(row.querySelector('.slot-ack button'), 'Ack for a session that needs attention');
  assert.equal(text(row.querySelector('.slot-kill')), 'Kill');
  assert.deepEqual(row.querySelectorAll('.chips .chip-btn').map(text), ['continue', 'merge', 'push', 'pr', 'add commit push', 'do it']);
  const idle = page(w).querySelector('.rrow[data-tmux=blog--web--s3]');
  assert.equal(idle.querySelector('.slot-ack button'), null, 'no Ack when nothing needs attention');
});

test('a plain shell session shows no nudge chips (typing "push" into bash would run it)', () => {
  const { w } = pagesWorld();
  w.location.hash = '#/agents';
  const sh = page(w).querySelector('.rrow[data-tmux=blog--web--sh]');
  assert.ok(sh.querySelector('.chips').classList.contains('hidden'));
  assert.equal(page(w).querySelector('.rrow[data-tmux=shop--api--s1] .chips').classList.contains('hidden'), false);
});

test('a project folder session (repo root) shows as the project folder', () => {
  const st = fakeState();
  st.projects[0].root = { name: 'root', path: '/srv/projects/shop', root: true, state: 'project', sessions: [{ ...sess('r1', { state: 'idle' }), tmux: 'shop--root--r1' }] };
  const { w } = pagesWorld({ state: st });
  w.location.hash = '#/agents';
  assert.equal(text(page(w).querySelector('.rrow[data-tmux=shop--root--r1] .rr-where')), 'project folder');
});

test('rows are keyed by tmux and patched in place: a poll keeps the nodes, reorders them, and never moves the row that holds focus', () => {
  const { w } = pagesWorld();
  w.location.hash = '#/agents';
  const root = page(w);
  const s1 = root.querySelector('.rrow[data-tmux=shop--api--s1]');
  const s2 = root.querySelector('.rrow[data-tmux=shop--api--s2]');
  const glyph = s2.querySelector('.glyph.waiting');
  // the user is on the Allow/chips of s2 (a focused button) while the states swap: s1 now waits, s2 works
  const chip = s2.querySelector('.chip-btn');
  chip.focus();
  const st = fakeState();
  st.pending_permissions = [];
  const shop = st.projects[0].repos[0].sessions;
  Object.assign(shop[0], { state: 'waiting', needs_attention: true, state_at: ISO(1) });     // s1
  Object.assign(shop[1], { state: 'working', needs_attention: false, state_at: ISO(1) });    // s2
  w.ctx.__st = st;
  w.run('state = __st; updateCurrentPage(state)');
  assert.deepEqual(rows(root).slice(0, 2), ['shop--api--s1', 'shop--api--s2'], 'waiting first again');
  assert.equal(root.querySelector('.rrow[data-tmux=shop--api--s1]'), s1, 'same node');
  assert.equal(root.querySelector('.rrow[data-tmux=shop--api--s2]'), s2, 'same node');
  assert.ok(s2.contains(w.document.activeElement) && s2.isConnected, 'the focused chip is still in the document');
  assert.equal(s2.querySelector('.glyph.waiting'), null);
  assert.ok(s2.querySelector('.glyph.working'), 'the glyph followed the state');
  assert.notEqual(s2.querySelector('.glyph.working'), glyph);
  assert.equal(s2.querySelector('.slot-ack button'), null, 'Ack went away with needs_attention');
  assert.ok(s1.querySelector('.slot-ack button'));
  assert.equal(chip.parentNode.parentNode, s2, 'the chip itself was never recreated');
});

test('a session that disappears removes its row, and an empty roster shows the empty state', () => {
  const { w } = pagesWorld();
  w.location.hash = '#/agents';
  const st = fakeState();
  st.projects[0].repos[0].sessions.pop();
  w.ctx.__st = st; w.run('state = __st; updateCurrentPage(state)');
  assert.equal(rows(page(w)).includes('shop--api--s2'), false);
  const none = fakeState();
  none.projects.forEach((p) => p.repos.forEach((r) => { r.sessions = []; }));
  w.ctx.__st = none; w.run('state = __st; updateCurrentPage(state)');
  assert.deepEqual(rows(page(w)), []);
  assert.match(text(page(w)), /No live sessions/);
  assert.equal(text(page(w).querySelector('.summary')), '✻ 0 need you · ✽ 0 working · ∙ 0 idle · ✓ 0 done');
});

test('the registry placeholder shows only while state.external is absent', () => {
  const { w } = pagesWorld();
  w.location.hash = '#/agents';
  const find = () => page(w).querySelectorAll('.nonideal').find((n) => /Background sessions/.test(text(n)));
  assert.match(text(find()), /Background sessions from the Claude registry arrive in v0\.5\.4/);
  assert.equal(find().classList.contains('hidden'), false);
  w.ctx.__st = fakeState({ external: { sessions: [] } });
  w.run('state = __st; updateCurrentPage(state)');
  assert.equal(find().classList.contains('hidden'), true);
});

test('nudge chips POST the text with enter and toast the outcome', async () => {
  const { w } = pagesWorld();
  w.location.hash = '#/agents';
  const chip = page(w).querySelectorAll('.rrow[data-tmux=shop--api--s2] .chip-btn').find((b) => text(b) === 'add commit push');
  chip.click();
  await tick();
  const sent = calls(w).filter((c) => c.path.endsWith('/keys'));
  assert.deepEqual(sent, [{ method: 'POST', path: '/api/sessions/shop--api--s2/keys', body: { text: 'add commit push', enter: true } }]);
  assert.deepEqual(plain(w.get('__toasts')), [{ text: 'sent "add commit push" to s2', kind: 'ok' }]);
  assert.equal(chip.disabled, false, 'the chip is usable again');
  // an API error becomes a toast, not a throw
  w.run('api = async () => { throw new Error("no such session"); }');
  chip.click();
  await tick();
  assert.deepEqual(plain(w.get('__toasts')).pop(), { text: 'no such session', kind: 'bad' });
});

test('Kill is the two-tap quiet button: the first tap swaps in Confirm and Cancel through the repaint hook, Cancel restores it', () => {
  const { w } = pagesWorld();
  w.location.hash = '#/agents';
  const row = () => page(w).querySelector('.rrow[data-tmux=shop--api--s1]');
  const kill = () => row().querySelector('.slot-kill');
  assert.equal(text(kill()), 'Kill');
  kill().querySelector('button').click();                               // confirmButton -> renderProjects() -> no #projects -> repaintPage()
  assert.equal(w.get('ui').confirm, 'kill:shop--api--s1');
  assert.match(text(kill()), /Confirm Kill/);
  assert.match(text(kill()), /Cancel/);
  kill().querySelectorAll('button').find((b) => text(b) === 'Cancel').click();
  assert.equal(text(kill()), 'Kill');
});

test('the age ticker starts on mount and stops on unmount', () => {
  const { w } = pagesWorld();
  assert.equal(w.get('agentsTicker.timer'), null);
  w.location.hash = '#/agents';
  assert.notEqual(w.get('agentsTicker.timer'), null);
  w.location.hash = '#/tasks';
  assert.equal(w.get('agentsTicker.timer'), null, 'no interval leaks past the page');
  assert.equal(w.get('agentsTicker.n'), 0);
});

// ---------------------------------------------------------------- the session peek

const sheet = (w) => w.document.querySelector('#sheet');

test('#/s/<tmux> opens the peek in the sheet as an overlay; the page under it stays mounted and is not remounted on return', () => {
  const { w } = pagesWorld();
  w.location.hash = '#/agents';
  w.location.hash = '#/s/shop--api--s2';
  assert.equal(w.document.body.getAttribute('data-page'), 'session');
  assert.equal(sheet(w).open, true);
  const peek = sheet(w).querySelector('.peek[data-tmux=shop--api--s2]');
  assert.ok(peek, 'the peek is in dialog#sheet');
  assert.equal(page(w).children.length, 1, 'the agents page stays mounted under the peek (overlay page)');
  assert.equal(mounts(w, 'agents'), 1);
  assert.equal(w.document.title, 'shop/api · s2 · ccboard');
  // content: card, pending permission with Allow / Deny, actions, chips, the send box
  assert.ok(peek.querySelector('.peek-card .glyph.waiting'));
  assert.match(text(peek.querySelector('.peek-sub')), /needs you · Opus 5 · ctx 42%/);
  assert.match(text(peek), /fix the login bug/);
  assert.match(text(peek), /done, tests pass/);
  const perm = peek.querySelector('.peek-perm');
  assert.equal(perm.classList.contains('hidden'), false);
  assert.match(text(perm), /Bash: npm test/);
  assert.deepEqual(perm.querySelectorAll('button').map(text), ['Allow', 'Deny']);
  assert.ok(peek.querySelector('a[href="/term/shop--api--s2"]'));
  assert.ok(peek.querySelector('.slot-ack button') && peek.querySelector('.slot-kill'));
  assert.equal(peek.querySelectorAll('.chips .chip-btn').length, 6);
  assert.ok(peek.querySelector('form.peek-send textarea.composer[aria-label=send]'));
  // back to the roster: the sheet closes and the roster is still there (no remount, no flash)
  w.location.hash = '#/agents';
  assert.equal(sheet(w).open, false);
  assert.equal(mounts(w, 'agents'), 1, 'closing the peek never remounts the page under it');
  assert.equal(rows(page(w)).length, 4);
  // the sheet's own close event, fired by this very route change, must not start a second navigation
  assert.equal(w.history.calls.length, 0, 'no replaceState from a close event during route()');
  assert.equal(mounts(w, 'home'), 0, 'home was never mounted on the way');
});

test('the peek sends text with enter, answers a permission, and acknowledges', async () => {
  const { w } = pagesWorld();
  w.location.hash = '#/s/shop--api--s2';
  const peek = sheet(w).querySelector('.peek');
  const input = peek.querySelector('form.peek-send textarea');
  input.value = 'run the tests again';
  peek.querySelector('form.peek-send').dispatchEvent({ type: 'submit', preventDefault() {} });
  await tick();
  assert.deepEqual(calls(w).pop(), { method: 'POST', path: '/api/sessions/shop--api--s2/keys', body: { text: 'run the tests again', enter: true } });
  assert.equal(input.value, '', 'the box clears after a send');
  peek.querySelector('.peek-perm button').click();                       // Allow
  await tick();
  assert.deepEqual(calls(w).pop(), { method: 'POST', path: '/api/permission/7/allow' });
  peek.querySelector('.slot-ack button').click();
  await tick();
  assert.deepEqual(calls(w).pop(), { method: 'POST', path: '/api/sessions/shop--api--s2/ack' });
});

test('#/s/<a> to #/s/<b> swaps the panel through onRoute without a remount, and the sheet stays open', () => {
  const { w } = pagesWorld();
  w.location.hash = '#/s/shop--api--s1';
  assert.equal(mounts(w, 'session'), 1);
  w.location.hash = '#/s/shop--api--s2';
  assert.equal(mounts(w, 'session'), 1, 'onRoute, not mount');
  assert.equal(sheet(w).open, true);
  assert.equal(sheet(w).querySelectorAll('.peek').length, 1);
  assert.ok(sheet(w).querySelector('.peek[data-tmux=shop--api--s2] .glyph.waiting'));
});

test('closing the sheet goes back in history when the app opened the peek', () => {
  const { w } = pagesWorld();
  let backs = 0;
  w.history.back = () => { backs++; w.location.hash = '#/agents'; };
  w.location.hash = '#/agents';
  w.location.hash = '#/s/shop--api--s1';
  w.get('closeSheet')();                                                  // Esc, the backdrop and the x all end here
  assert.equal(backs, 1);
  assert.equal(w.location.hash, '#/agents');
  assert.equal(rows(page(w)).length, 4, 'the roster is back');
});

test('closing a peek opened cold (push link) lands on home, not outside the app', () => {
  const { w } = pagesWorld();
  let backs = 0;
  w.history.back = () => { backs++; };
  w.setHash('#/s/shop--api--s1', { silent: true });
  w.get('route')();                                                       // boot: the only route of this document
  assert.equal(sheet(w).open, true);
  w.get('closeSheet')();
  assert.equal(backs, 0);
  const rep = w.history.calls.filter((c) => c.method === 'replaceState').pop();
  assert.equal(rep.args[2], '#/');
  assert.equal(w.location.hash, '#/');
  assert.equal(mounts(w, 'home'), 1);
  assert.ok(page(w).querySelector('#projects'), 'home is mounted');
});

test('in the dock from 1024 px up, and a closed peek hides it again', () => {
  const { w } = pagesWorld({ wide: true });
  w.location.hash = '#/s/shop--api--s1';
  const dock = w.document.querySelector('#dock');
  assert.ok(dock.querySelector('.peek[data-tmux=shop--api--s1]'));
  assert.equal(dock.classList.contains('hidden'), false);
  assert.equal(sheet(w).open, false, 'no sheet on a wide screen');
  assert.equal(dock.querySelector('.peek-head').classList.contains('hidden'), false, 'the dock peek carries its own close button');
  w.location.hash = '#/agents';
  assert.equal(dock.classList.contains('hidden'), true);
  assert.equal(dock.children.length, 0);
});

test('with no sheet surface the peek renders into #page', () => {
  const { w } = pagesWorld();
  sheet(w).showModal = undefined;
  w.location.hash = '#/s/shop--api--s1';
  assert.ok(page(w).querySelector('.peek[data-tmux=shop--api--s1]'));
  assert.equal(sheet(w).open, false);
});

test('a session that is gone says so instead of failing', () => {
  const { w } = pagesWorld();
  w.location.hash = '#/s/shop--api--zz';
  const peek = sheet(w).querySelector('.peek');
  assert.equal(peek.querySelector('.peek-card'), null);
  const gone = peek.querySelectorAll('.dim').find((n) => /not running any more/.test(text(n)));
  assert.equal(gone.classList.contains('hidden'), false);
});

// ---------------------------------------------------------------- placeholders and onRoute

test('placeholder pages take onRoute: #/p/a to #/p/a?tab=x redraws without a remount', () => {
  const { w } = pagesWorld();
  w.location.hash = '#/p/a';
  assert.equal(mounts(w, 'project'), 1);
  assert.match(text(page(w)), /Project a/);
  assert.match(text(page(w)), /arrives in v0\.5\.6/);
  assert.equal(w.document.title, 'Project a · ccboard');
  w.location.hash = '#/p/a?tab=x';
  assert.equal(mounts(w, 'project'), 1, 'onRoute, not mount');
  assert.match(text(page(w)), /Project a · x/);
  assert.equal(w.document.title, 'Project a · x · ccboard');
  w.location.hash = '#/p/b/api';
  assert.equal(mounts(w, 'project'), 1, 'params changing on the same id also go through onRoute');
  assert.match(text(page(w)), /Project b\/api/);
  w.location.hash = '#/usage';
  assert.equal(mounts(w, 'usage'), 1);
  assert.match(text(page(w)), /Usage/);
  assert.match(text(page(w)), /arrives in v0\.5\.17/);
  assert.doesNotMatch(text(page(w)), /Project/);
});

test('every later-phase route has a placeholder that names its phase', () => {
  const { w } = pagesWorld();
  const cases = { '#/quad': 'v0.5.9', '#/memory/shop': 'v0.5.20', '#/onboarding/project': 'v0.5.19', '#/usage': 'v0.5.17', '#/p/shop': 'v0.5.6' };
  for (const [hash, version] of Object.entries(cases)) {
    w.location.hash = hash;
    assert.match(text(page(w)), new RegExp(`arrives in ${version.replace(/\./g, '\\.')}`), hash);
  }
});

// ---------------------------------------------------------------- inbox, tasks, home

test('#/inbox lists only what needs attention, with the permission, chips and legacy selection', async () => {
  const st = fakeState();
  st.projects[1].repos[0].sessions[0] = { ...st.projects[1].repos[0].sessions[0], state: 'done', needs_attention: true, state_at: ISO(90) };
  const { w } = pagesWorld({ state: st });
  w.location.hash = '#/inbox';
  assert.equal(w.document.title, '(2) Needs you · ccboard');
  assert.deepEqual(rows(page(w)), ['blog--web--s3', 'shop--api--s2'], 'oldest first, like the legacy list');
  const waiting = page(w).querySelector('.rrow[data-tmux=shop--api--s2]');
  assert.equal(text(waiting.querySelector('.rr-where')), 'shop/api', 'the inbox names project and repo');
  assert.match(text(waiting.querySelector('.rr-last')), /Bash: npm test/);
  const allow = waiting.querySelectorAll('button').find((b) => text(b) === 'Allow');
  allow.click();
  await tick();
  assert.deepEqual(calls(w).pop(), { method: 'POST', path: '/api/permission/7/allow' });
  assert.equal(waiting.querySelectorAll('.chips .chip-btn').length, 6);
  waiting.click();                                                        // selecting a card drives ui.inboxSel
  assert.equal(w.get('ui').inboxSel, 1);
  assert.ok(waiting.classList.contains('sel'));
  assert.equal(page(w).querySelector('.rrow[data-tmux=blog--web--s3]').classList.contains('sel'), false);
});

test('an empty inbox says so; the title loses its count', () => {
  const st = fakeState();
  st.projects[0].repos[0].sessions[1].needs_attention = false;
  const { w } = pagesWorld({ state: st });
  w.location.hash = '#/inbox';
  assert.equal(w.document.title, 'Needs you · ccboard');
  assert.deepEqual(rows(page(w)), []);
  const none = page(w).querySelectorAll('.nonideal').find((n) => /Nothing needs you/.test(text(n)));
  assert.equal(none.classList.contains('hidden'), false);
});

test('#/tasks renders the kanban into a #tasks section, or an empty state', () => {
  const { w } = pagesWorld();
  w.location.hash = '#/tasks';
  assert.ok(page(w).querySelector('section#tasks').classList.contains('hidden'));
  assert.match(text(page(w)), /No tasks yet/);
  const task = { id: 1, title: 'Fix the cart', project: 'shop', repo: 'api', branch: 'worktree-fix', column: 'in_progress', tmux: 'shop--api--t-fix', session: sess('t-fix', { state: 'working' }), ci: null, overlap: [] };
  w.ctx.__st = fakeState({ tasks: [task] });
  w.run('state = __st; updateCurrentPage(state)');
  const sec = page(w).querySelector('section#tasks');
  assert.equal(sec.classList.contains('hidden'), false);
  assert.match(text(sec), /Fix the cart/);
  assert.match(text(sec), /In progress \(1\)/);
});

test('#/ mounts the board sections with the legacy ids and paints the project cards', () => {
  const { w } = pagesWorld();
  w.location.hash = '#/';
  const root = page(w);
  for (const id of ['inbox', 'live', 'tasks', 'jobs', 'new-project', 'projects']) assert.ok(root.querySelector(`#${id}`), `#${id}`);
  assert.ok(root.querySelector('#importrow') && root.querySelector('#queue'), 'renderNewProject() built its form');
  assert.equal(root.querySelectorAll('#projects .card').length, 2, 'one card per project');
  assert.equal(root.querySelector('#inbox').classList.contains('hidden'), false, 'the needs-attention list is shown');
  assert.equal(w.document.title, '(1) Home · ccboard');
});

test('Home keeps the live grid alive only while mounted: main.js restores the flag, mount starts it, unmount stops it', () => {
  const opened = [];
  class FakeES { constructor(url) { opened.push(this); this.url = url; this.closed = false; } addEventListener() {} close() { this.closed = true; } }
  const { w } = pagesWorld({ extra: { EventSource: FakeES } });
  w.run('live.on = true');
  w.location.hash = '#/';
  assert.equal(opened.length, 1);
  assert.equal(opened[0].url, '/api/stream');
  assert.ok(page(w).querySelector('#live .live-grid'));
  w.location.hash = '#/inbox';
  assert.equal(opened[0].closed, true);
  assert.equal(w.get('live.es'), null);
  assert.equal(w.get('live.on'), true, 'the flag survives so coming back restarts the stream');
  w.location.hash = '#/';
  assert.equal(opened.length, 2);
});

test('on Home the first tap of a task or schedule button (Archive, Delete) swaps in its Confirm', () => {
  const task = { id: 1, title: 'Fix the cart', project: 'shop', repo: 'api', branch: 'worktree-fix', column: 'in_progress', tmux: 'shop--api--t-fix', session: sess('t-fix', { state: 'working' }), ci: null, overlap: [] };
  const { w } = pagesWorld({ state: fakeState({ tasks: [task] }) });
  w.location.hash = '#/';
  const archive = () => page(w).querySelector('#tasks .task .actions').querySelectorAll('button').find((b) => /Archive/.test(text(b)));
  assert.equal(text(archive()), 'Archive');
  archive().click();
  assert.match(text(page(w).querySelector('#tasks')), /Confirm Archive/, 'the tasks section repainted, not just #projects');
});

test('renderProjects off the board repaints the page instead of crashing', () => {
  const { w } = pagesWorld();
  w.location.hash = '#/agents';
  assert.doesNotThrow(() => w.get('renderProjects')());
  assert.doesNotThrow(() => w.get('renderInbox')());
});

// ---------------------------------------------------------------- settings and search

test('#/settings picks its section from ?sec= and switches through onRoute', () => {
  const { w } = pagesWorld();
  w.location.hash = '#/settings?sec=nodes';
  const panel = (id) => page(w).querySelector(`.settings-panel[data-sec=${id}]`);
  assert.equal(panel('nodes').classList.contains('hidden'), false);
  assert.equal(panel('notify').classList.contains('hidden'), true);
  assert.match(text(panel('nodes')), /No other nodes/);
  assert.equal(page(w).querySelector('.tab[data-tab=nodes]').getAttribute('aria-selected'), 'true');
  w.location.hash = '#/settings?sec=box';
  assert.equal(mounts(w, 'settings'), 1, 'onRoute, not mount');
  assert.equal(panel('box').classList.contains('hidden'), false);
  assert.equal(panel('nodes').classList.contains('hidden'), true);
  assert.equal(page(w).querySelector('.tab[data-tab=box]').getAttribute('aria-selected'), 'true');
  w.location.hash = '#/settings?sec=nonsense';
  assert.equal(panel('notify').classList.contains('hidden'), false, 'an unknown section falls back to notify');
});

test('clicking a settings tab navigates with ?sec= (replace, no history entry)', () => {
  const { w } = pagesWorld();
  w.location.hash = '#/settings';
  const before = w.history.calls.length;
  page(w).querySelector('.tab[data-tab=agents]').click();
  assert.equal(w.history.calls.length, before + 1);
  const call = w.history.calls.at(-1);
  assert.deepEqual([call.method, call.args[2]], ['replaceState', '#/settings?sec=agents']);
  assert.equal(page(w).querySelector('.settings-panel[data-sec=agents]').classList.contains('hidden'), false);
});

test('settings > Agents carries the login button, wired to startLogin', async () => {
  const st = fakeState({ claude: { installed: true, loggedIn: false } });
  const { w } = pagesWorld({ state: st });
  w.run('globalThis.__modals = 0; openModal = () => { __modals++; };');
  w.location.hash = '#/settings?sec=agents';
  const panel = page(w).querySelector('.settings-panel[data-sec=agents]');
  assert.match(text(panel), /Claude: not logged in/);
  const login = panel.querySelectorAll('button').find((b) => text(b) === 'Log in');
  login.click();
  await tick();
  assert.deepEqual(calls(w).pop(), { method: 'POST', path: '/api/claude/login' });
  assert.equal(w.get('__modals'), 1, 'the login modal opens');
  // logged in: Log out instead
  w.ctx.__st = fakeState();
  w.run('state = __st; updateCurrentPage(state)');
  assert.match(text(panel), /Claude: a@example\.com \(max\)/);
  assert.ok(panel.querySelectorAll('button').some((b) => text(b) === 'Log out'));
});

test('settings panels are rebuilt only when the state they show changed', () => {
  const { w } = pagesWorld();
  w.location.hash = '#/settings?sec=agents';
  const panel = page(w).querySelector('.settings-panel[data-sec=agents]');
  const first = panel.querySelector('.kv');
  w.run('updateCurrentPage(state)');
  assert.equal(panel.querySelector('.kv'), first, 'an unchanged poll leaves the buttons alone');
  w.ctx.__st = fakeState({ claude: { installed: true, loggedIn: false } });
  w.run('state = __st; updateCurrentPage(state)');
  assert.notEqual(panel.querySelector('.kv'), first);
});

test('renderNotifyPanel() is a global core.js can call: it rebuilds the notify panel when open and is harmless otherwise', () => {
  const { w } = pagesWorld();
  assert.doesNotThrow(() => w.get('renderNotifyPanel')());              // settings not mounted
  w.location.hash = '#/settings';
  assert.doesNotThrow(() => w.get('renderNotifyPanel')());
  assert.match(text(page(w).querySelector('.settings-panel[data-sec=notify]')), /Web Push/);
  assert.match(text(page(w).querySelector('.settings-panel[data-sec=notify]')), /not configured on the box/);
});

test('#/search?q= runs the search into #page; a new q re-runs it without a remount', async () => {
  const { w } = pagesWorld();
  w.ctx.__searchHits = [{ session_id: 'abcdef12-0000', kind: 'user', project: 'shop', repo: 'api', ts: '2026-10-01T10:00:00', tmux: 'shop--api--s1', snippet: 'the [auth] flow' }];
  w.location.hash = '#/search?q=auth';
  await tick();
  assert.equal(calls(w).filter((c) => c.path.startsWith('/api/search')).pop().path, '/api/search?q=auth');
  assert.equal(w.document.title, 'Search: auth · ccboard');
  const sec = page(w).querySelector('section#search');
  assert.match(text(sec), /the \[auth\] flow/);
  assert.match(text(sec), /shop\/api/);
  assert.ok(sec.querySelector('a[href="#/s/shop--api--s1"]'), 'a hit with a live session links to its peek');
  w.location.hash = '#/search?q=cart';
  await tick();
  assert.equal(mounts(w, 'search'), 1);
  assert.equal(calls(w).filter((c) => c.path.startsWith('/api/search')).pop().path, '/api/search?q=cart');
  assert.match(text(sec), /Search: cart/);
  w.location.hash = '#/search';
  assert.match(text(page(w).querySelector('section#search')), /Type a query/);
});

// ---------------------------------------------------------------- router plumbing the pages rely on

test('route(): the drawer closes on every route; the sheet only when a page is remounted', () => {
  const { w } = pagesWorld();
  w.run('globalThis.__drawer = 0; globalThis.__sheetClosed = 0; closeDrawer = () => { __drawer++; }; { const real = closeSheet; closeSheet = () => { __sheetClosed++; return real(); }; }');
  w.location.hash = '#/settings?sec=notify';
  assert.deepEqual([w.get('__drawer'), w.get('__sheetClosed')], [1, 1]);
  w.location.hash = '#/settings?sec=box';                                 // onRoute: the page keeps its sheet
  assert.deepEqual([w.get('__drawer'), w.get('__sheetClosed')], [2, 1]);
  w.location.hash = '#/agents';
  assert.deepEqual([w.get('__drawer'), w.get('__sheetClosed')], [3, 2]);
});

test('route(): aria-current follows the route on topbar, sidebar, bottom nav and drawer links', () => {
  const { w, dom } = pagesWorld();
  const mkLink = (parent, href, attr) => { const a = w.document.createElement('a'); if (href) a.setAttribute('href', href); if (attr) a.setAttribute(attr[0], attr[1]); w.document.querySelector(parent).append(a); return a; };
  const agents = mkLink('#sidebar', '#/agents');
  const inbox = mkLink('#sidebar', '#/inbox');
  const brand = mkLink('#topbar', '#/');
  const proj = mkLink('#sidebar', '#/p/shop');
  const bnav = mkLink('#bnav', null, ['data-route', 'agents']);
  const drawer = mkLink('#drawer', '#/agents');
  const outside = mkLink('#main', '#/agents');
  w.location.hash = '#/agents';
  assert.deepEqual([agents, inbox, brand, bnav, drawer].map((n) => n.getAttribute('aria-current')), ['page', null, null, 'page', 'page']);
  assert.equal(outside.getAttribute('aria-current'), null, 'only shell links are touched');
  w.location.hash = '#/p/shop/api';
  assert.equal(proj.getAttribute('aria-current'), 'page', 'a project link stays current on its repo pages');
  assert.equal(agents.getAttribute('aria-current'), null);
  w.location.hash = '#/';
  assert.equal(brand.getAttribute('aria-current'), 'page');
  void dom;
});

test('route(): scroll positions are saved on leaving a page and restored on return', () => {
  const scrolls = [];
  const { w } = pagesWorld();
  w.run('window.scrollTo = (x, y) => { __scrolls.push([x, y]); }; window.scrollY = 0;');
  w.ctx.__scrolls = scrolls;
  w.location.hash = '#/agents';
  w.run('window.scrollY = 240');
  w.location.hash = '#/tasks';
  assert.equal(w.sessionStorage.getItem('ccboard:scroll:#/agents'), '240:0');
  assert.deepEqual(plain(scrolls.at(-1)), [0, 0]);
  w.location.hash = '#/agents';
  assert.deepEqual(plain(scrolls.at(-1)), [0, 240]);
  w.run('window.scrollY = 0');
  w.location.hash = '#/tasks';
  assert.equal(w.sessionStorage.getItem('ccboard:scroll:#/agents'), null, 'a page left at the top keeps no entry');
});

test('route(): an in-page anchor (the skip link) is not a route; an unknown hash becomes #/', () => {
  const { w } = pagesWorld();
  w.location.hash = '#/agents';
  const before = mounts(w, 'agents');
  w.location.hash = '#main';
  assert.equal(mounts(w, 'agents'), before, 'the roster stays');
  assert.equal(w.history.calls.length, 0);
  w.location.hash = '#/nonsense';
  assert.equal(w.history.calls.at(-1).args[2], '#/');
  assert.equal(w.document.body.getAttribute('data-page'), 'home');
});

test('updateCurrentPage hands the state to the mounted page (what the shell render() calls) and is a no-op without state', () => {
  const { w } = pagesWorld();
  w.location.hash = '#/agents';
  const st = fakeState();
  st.projects[0].repos[0].sessions[0].name = 'renamed';
  w.ctx.__st = st;
  w.run('updateCurrentPage(__st)');
  assert.equal(text(page(w).querySelector('.rrow[data-tmux=shop--api--s1] .rr-name')), 'renamed');
  w.run('state = null');
  assert.doesNotThrow(() => w.run('updateCurrentPage(null)'));
  assert.doesNotThrow(() => w.run('repaintPage()'));
});

// ---------------------------------------------------------------- boot

test('main.js boots in order: legacy hash rewritten, live flag restored, first route, one poll; fallbacks only when the shell is absent', async () => {
  const { w } = pagesWorld({ extra: { setTimeout: () => 0 }, realPoll: true });
  w.run('state = null; api = async (method, path) => { __calls.push({ method, path }); return path === "/api/state" ? __st : {}; };');
  w.setHash('#s=shop--api--s1', { silent: true });
  w.localStorage.setItem('ccboard:live', '1');
  w.load('main.js');
  await tick();
  assert.equal(w.history.calls[0].args[2], '#/s/shop--api--s1', 'the old ntfy link was rewritten first');
  assert.equal(w.get('live.on'), true);
  assert.equal(w.get('live.es'), null, 'the stream waits for the Home page');
  assert.equal(w.get('currentRoute')().id, 'session');
  assert.ok(sheet(w).querySelector('.peek[data-tmux=shop--api--s1]'), 'the first route mounted the peek');
  assert.ok(sheet(w).querySelector('.peek-card'), 'and the first state paints it through the fallback render()');
  for (const name of ['render', 'renderHeader', 'renderUsage']) assert.equal(w.run(`typeof ${name}`), 'function', name);
  assert.equal(calls(w).filter((c) => c.path === '/api/state').length, 1, 'exactly one state poll starts');
});

test('main.js never shadows a render() the shell already defined', () => {
  const { w } = pagesWorld({ extra: { setTimeout: () => 0 }, realPoll: true });
  w.run('globalThis.__shell = 0; globalThis.render = () => { __shell++; }; globalThis.renderHeader = () => {}; globalThis.renderUsage = () => {}; api = async () => { throw new Error("offline"); };');
  w.load('main.js');
  w.run('render(true)');
  assert.equal(w.get('__shell'), 1, 'the shell render is still the one that runs');
});
