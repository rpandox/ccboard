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
    mk('dialog', 'drawer'), mk('dialog', 'sheet'), mk('dialog', 'helpdlg'), mk('div', 'toasts'));
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

const PAGE_FILES = ['home', 'inbox', 'widgets', 'tasks', 'project', 'agents', 'doctor', 'settings', 'search', 'session', 'usage', 'quad', 'onboarding', 'memory', 'placeholders'];       // widgets.js (Widgets, no route) loads right after inbox.js, project.js right after tasks.js

/** A world with the DOM, the real scripts in index.html order (shell.js left out), and recorders for api, toast and registerPage. */
function pagesWorld({ wide = false, extra = {}, state = fakeState(), realPoll = false } = {}) {
  const w = makeWorld({ matchMedia: (q) => ({ matches: wide && /1024/.test(q), addEventListener() {}, removeEventListener() {} }), ...extra });
  const dom = installDom(w);
  for (const f of ['core.js', 'components.js', 'live.js', 'termkit.js', 'launcher.js', 'tree.js', 'router.js']) w.load(f);       // tree.js (Tree, definition-only) follows launcher.js in index.html
  w.ctx.__calls = []; w.ctx.__toasts = []; w.ctx.__registered = []; w.ctx.__mounts = {}; w.ctx.__searchHits = [];
  w.run(`
    api = async (method, path, body) => {
      if (!path.startsWith('/api/series') && !path.startsWith('/api/usage/summary')) __calls.push({ method, path, body });      // the usage card's background reads (Home under a peek) are not what these tests look at
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
/** The visible chips of the Agents state summary as 'count label'. */
const summaryOf = (root) => root.querySelectorAll('.summary .sum-seg').filter((n) => !n.classList.contains('hidden')).map((n) => `${text(n.querySelector('.sum-n'))} ${text(n.querySelector('.sum-l'))}`);
const mounts = (w, id) => w.get('__mounts')[id];
const calls = (w) => plain(w.get('__calls'));
const text = (n) => n.textContent;
/** A session row's `...` menu (v0.5.6d: Ack, Reply, Tail and Kill live there): its labels, and a pick by label (the menu closes itself on a pick). */
const itemLabel = (i) => text(i.querySelector('.mi-name') || i).trim();                 // an item with a caption (Auto-continue) has its name in .mi-name
const rowMenuLabels = (w, row) => { const b = row.querySelector('.rr-more'); b.click(); const l = w.document.querySelectorAll('.menuitem').map(itemLabel); b.click(); return l; };
const rowMenu = (w, row, label) => {
  const b = row.querySelector('.rr-more');
  b.click();
  const hit = w.document.querySelectorAll('.menuitem').find((i) => (label instanceof RegExp ? label.test(itemLabel(i)) : itemLabel(i) === label));
  if (hit) { hit.click(); return true; }
  b.click();
  return false;
};

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
  assert.deepEqual(summaryOf(root), ['1 need you', '1 working', '2 idle', '0 done'], 'the states as the chips Home\'s summary bar uses; error and ended only while there is one');
  assert.ok(root.querySelector('nav.summary.sumbar'));
});

test('#/agents with just a waiting and a working session renders two rows, waiting first', () => {
  const st = fakeState();
  st.projects = [st.projects[0]];
  const { w } = pagesWorld({ state: st });
  w.location.hash = '#/agents';
  assert.deepEqual(rows(page(w)), ['shop--api--s2', 'shop--api--s1']);
});

test('a row shows glyphs, name, repo, age, model chip and context meter, last prompt and message, Open, a ... menu with Ack / Reply / Tail / Kill, and the chips', () => {
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
  assert.equal(text(row.querySelector('.rr-meta')), 'needs you');
  assert.equal(text(row.querySelector('.bdg-model')), 'Opus 5', 'the model is a chip in its hue (the Agents rows are the rich rows)');
  assert.ok(row.querySelector('.bdg-model').classList.contains('hue-blue'));
  assert.equal(text(row.querySelector('.ctx-pct')), '42%');
  assert.match(text(row.querySelector('.rr-last')), /fix the login bug/);
  assert.match(text(row.querySelector('.rr-last')), /done, tests pass/);
  const open = row.querySelector('a[href="/term/shop--api--s2"]');
  assert.ok(open && text(open) === 'Open');
  assert.deepEqual(rowMenuLabels(w, row), ['Acknowledge', 'Hide reply box', 'Tail', 'Auto-continue: on', 'Kill'], 'Ack only for a session that needs attention; the waiting row\'s reply box is already open');
  assert.equal(text(row.querySelector('.slot-kill')), '', 'Kill shows inline only for its second tap');
  assert.deepEqual(row.querySelectorAll('.chips .chip-btn').map(text), ['continue', 'merge', 'push', 'pr', 'add commit push', 'do it']);
  const idle = page(w).querySelector('.rrow[data-tmux=blog--web--s3]');
  assert.deepEqual(rowMenuLabels(w, idle), ['Reply', 'Tail', 'Auto-continue: on', 'Kill'], 'no Acknowledge when nothing needs attention');
  assert.equal(row.classList.contains('open'), true, 'a waiting row keeps its chips and send box open');
  assert.equal(idle.classList.contains('open'), false, 'an idle one folds them behind Reply');
});

test('a row\'s ... menu offers Add to quad from 840 px up (Shell.quadAdd), just before Kill; not on a narrow window, not without the quad, not for the Open-only peek', () => {
  const { w } = pagesWorld();
  w.run('globalThis.__quadAdds = []; globalThis.Shell = { mode: "expanded", wide() { return this.mode === "expanded" || this.mode === "large"; }, quadAdd(t) { __quadAdds.push(t); return true; } }');
  w.location.hash = '#/agents';
  const row = page(w).querySelector('.rrow[data-tmux=shop--api--s2]');
  assert.deepEqual(rowMenuLabels(w, row), ['Acknowledge', 'Hide reply box', 'Tail', 'Add to quad', 'Auto-continue: on', 'Kill'], 'before Kill, the destructive one stays last');
  assert.equal(rowMenu(w, row, 'Add to quad'), true);
  assert.deepEqual(plain(w.get('__quadAdds')), ['shop--api--s2'], 'the row\'s own session');
  w.run('Shell.mode = "medium"');
  assert.equal(rowMenuLabels(w, row).includes('Add to quad'), false, 'under 840 px the quad is the phone chip switcher: no tile to add to');
  w.run('Shell.mode = "large"; delete Shell.quadAdd');
  assert.equal(rowMenuLabels(w, row).includes('Add to quad'), false, 'without Shell.quadAdd (a partial deploy) there is nothing to call');
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
  const chip = s2.querySelector('.chips .chip-btn');
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
  assert.equal(chip.parentNode.parentNode, s2, 'the chip itself was never recreated');
  assert.equal(rowMenuLabels(w, s2).includes('Acknowledge'), false, 'Ack went away with needs_attention');
  assert.ok(rowMenuLabels(w, s1).includes('Acknowledge'));
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
  assert.deepEqual(summaryOf(page(w)), ['0 need you', '0 working', '0 idle', '0 done']);
});

test('the registry placeholder is gone: the outside-threads section stays hidden until a Codex thread is listed (tests/js/agents-external.test.mjs has the rest)', () => {
  const { w } = pagesWorld();
  w.location.hash = '#/agents';
  assert.equal(page(w).querySelectorAll('.nonideal').some((n) => /Background sessions/.test(text(n))), false, 'no v0.5.4 placeholder any more');
  const sec = () => page(w).querySelector('[data-sec=external]');
  assert.ok(sec().classList.contains('hidden'), 'no state.external, no Codex: nothing to list');
  w.ctx.__st = fakeState({ external: { sessions: [] } });                  // the old registry shape: not a Codex list, still nothing
  w.run('state = __st; updateCurrentPage(state)');
  assert.ok(sec().classList.contains('hidden'));
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

test('Kill is a two-tap: the menu item swaps in Confirm and Cancel through the repaint hook, Cancel restores the row, Confirm kills', async () => {
  const { w } = pagesWorld();
  w.location.hash = '#/agents';
  const row = () => page(w).querySelector('.rrow[data-tmux=shop--api--s1]');
  const kill = () => row().querySelector('.slot-kill');
  assert.equal(text(kill()), '', 'nothing inline until the first tap');
  assert.ok(rowMenu(w, row(), 'Kill'));                                 // the menu item is the first tap: it asks the row to show Confirm / Cancel through the repaint hook
  assert.equal(w.get('ui').confirm, 'kill:shop--api--s1');
  assert.match(text(kill()), /Confirm Kill/);
  assert.match(text(kill()), /Cancel/);
  kill().querySelectorAll('button').find((b) => text(b) === 'Cancel').click();
  assert.equal(w.get('ui').confirm, null);
  assert.equal(text(kill()), '', 'Cancel restores the row');
  assert.ok(rowMenu(w, row(), 'Kill'));
  kill().querySelectorAll('button').find((b) => /Confirm Kill/.test(text(b))).click();
  await tick();
  assert.deepEqual(calls(w).pop(), { method: 'DELETE', path: '/api/sessions/shop--api--s1' }, 'the second tap kills');
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
  assert.ok(page(w).querySelector('.pblocks') && page(w).querySelector('.sumbar'), 'home is mounted');
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

test('the memory route is its real page since v0.5.20 (pages/memory.js): a project in the address redraws through onRoute, not a remount', async () => {
  const { w } = pagesWorld();
  w.run(`api = async (method, path) => { if (path.startsWith('/api/memory/')) throw Object.assign(new Error('x'), { status: 503, body: { state: 'down', up: false, reason: 'connection refused' } }); return { ok: true }; }`);
  w.location.hash = '#/memory';
  assert.equal(mounts(w, 'memory'), 1);
  assert.match(text(page(w)), /Memory/);
  assert.doesNotMatch(text(page(w)), /arrives in/, 'v0.5.20 replaced the memory placeholder');
  assert.equal(w.document.title, 'Memory shop · ccboard', 'the first project of the board opens when the address names none');
  w.location.hash = '#/memory/shop?tab=timeline';
  assert.equal(mounts(w, 'memory'), 1, 'the same project and a new tab go through onRoute, not mount');
  await tick();
  w.location.hash = '#/usage';
  assert.equal(mounts(w, 'usage'), 1);
  assert.doesNotMatch(text(page(w)), /Memory/);
});

test('no route is a placeholder any more; the project and quad routes have their real pages', () => {
  const { w } = pagesWorld();
  w.location.hash = '#/quad';
  assert.equal(mounts(w, 'quad'), 1, 'v0.5.9: the quad route is its real page (pages/quad.js), not a placeholder');
  assert.doesNotMatch(text(page(w)), /arrives in/);
  assert.ok(page(w).querySelector('.quad'));
  assert.equal(w.get("typeof PLACEHOLDER_INFO === 'object' && Object.keys(PLACEHOLDER_INFO).length"), 0, 'placeholders.js lists no route');
  w.location.hash = '#/p/shop';
  assert.equal(mounts(w, 'project'), 1);
  assert.doesNotMatch(text(page(w)), /arrives in/, 'v0.5.6 replaced the project placeholder');
  assert.match(text(page(w)), /shop/);
  assert.match(w.document.title, /shop/, 'the title names the project');
});

// ---------------------------------------------------------------- inbox, tasks, home

test('#/inbox lists only what needs attention as cards in kind order, with the permission, chips and legacy selection', async () => {
  const st = fakeState();
  st.projects[1].repos[0].sessions[0] = { ...st.projects[1].repos[0].sessions[0], state: 'done', needs_attention: true, state_at: ISO(90) };
  const { w } = pagesWorld({ state: st });
  w.location.hash = '#/inbox';
  const cards = () => page(w).querySelectorAll('.inbox-card').map((n) => n.getAttribute('data-tmux'));
  assert.equal(w.document.title, '(2) Needs you · ccboard');
  assert.deepEqual(cards(), ['shop--api--s2', 'blog--web--s3'], 'by kind (permission, then done), not by age');
  const waiting = page(w).querySelector('.inbox-card[data-tmux=shop--api--s2]');
  assert.equal(text(waiting.querySelector('.ib-where')), 'shop/api', 'the inbox names project and repo');
  assert.match(text(waiting.querySelector('.ib-ctx')), /Bash: npm test/, 'the permission summary leads');
  const allow = waiting.querySelectorAll('button').find((b) => text(b) === 'Allow');
  allow.click();
  await tick();
  assert.deepEqual(calls(w).pop(), { method: 'POST', path: '/api/permission/7/allow' });
  assert.equal(waiting.querySelectorAll('.chips .chip-btn').filter((b) => !b.classList.contains('chip-more')).length, 6, 'all six chips are on the card; four show, a ... brings the rest');
  assert.ok(waiting.classList.contains('lead'), 'the first card leads: its Allow is the screen\'s filled primary');
  waiting.click();                                                        // selecting a card drives ui.inboxSel (the index in the list the page draws)
  assert.equal(w.get('ui').inboxSel, 0);
  assert.ok(waiting.classList.contains('sel'));
  assert.equal(page(w).querySelector('.inbox-card[data-tmux=blog--web--s3]').classList.contains('sel'), false);
});

test('an empty inbox says so; the title loses its count', () => {
  const st = fakeState();
  st.projects[0].repos[0].sessions[1].needs_attention = false;
  st.pending_permissions = [];                                            // a pending permission would keep its session in the inbox
  const { w } = pagesWorld({ state: st });
  w.location.hash = '#/inbox';
  assert.equal(w.document.title, 'Needs you · ccboard');
  assert.deepEqual(page(w).querySelectorAll('.inbox-card'), []);
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

test('#/ mounts Home: the summary line, the inbox section, one block per project and the usage card host; the v0.4 board sections are gone', () => {
  const { w } = pagesWorld();
  w.location.hash = '#/';
  const root = page(w);
  for (const id of ['inbox-home', 'usage-home']) assert.ok(root.querySelector(`#${id}`), `#${id}`);
  assert.ok(root.querySelector('nav.summary.sumbar') && root.querySelector('.seg-ctl') && root.querySelector('.pblocks'));
  for (const id of ['inbox', 'live', 'tasks', 'jobs', 'new-project', 'importrow', 'queue', 'projects']) assert.equal(root.querySelector(`#${id}`), null, `#${id} moved out of Home (the + menu, #/tasks and the schedules strip)`);
  assert.deepEqual(root.querySelector('.pblocks').children.map((n) => n.getAttribute('data-block')), ['p:shop', 'p:blog'], 'one block per project, the one that needs you first');
  assert.deepEqual(root.querySelectorAll('.sum-seg').map((n) => n.getAttribute('data-f') + ':' + text(n.querySelector('.sum-n'))), ['waiting:1', 'working:1', 'idle:2', 'done:0', 'errored:0']);
  assert.equal(root.querySelector('.sum-seg[data-f=errored]').classList.contains('hidden'), true);
  assert.equal(root.querySelector('#inbox-home').classList.contains('hidden'), false, 'the needs-you cards are shown');
  assert.deepEqual(root.querySelectorAll('#inbox-home .inbox-card').map((n) => n.getAttribute('data-tmux')), ['shop--api--s2']);
  assert.equal(root.querySelectorAll('.pblocks .rrow').length, 4, 'a rich row per session');
  assert.equal(w.document.title, '(1) Home · ccboard');
});

test('Home opens the event stream only while a session\'s tail is expanded: one connection for all of them, closed with the last, never by a poll', () => {
  const opened = [];
  class FakeES { constructor(url) { opened.push(this); this.url = url; this.closed = false; } addEventListener() {} close() { this.closed = true; } }
  const { w } = pagesWorld({ extra: { EventSource: FakeES } });
  w.run('Live.DEBOUNCE_MS = 0');
  w.location.hash = '#/';
  assert.equal(opened.length, 0, 'mounting Home connects to nothing');
  const tail = (tmux) => ({ click: () => assert.ok(rowMenu(w, page(w).querySelector(`.pblocks .rrow[data-tmux="${tmux}"]`), /^(Tail|Hide tail)$/)) });
  tail('shop--api--s1').click();
  assert.equal(opened.length, 1);
  assert.equal(opened[0].url, '/api/stream?names=shop--api--s1&lines=12');
  tail('blog--web--s3').click();
  assert.equal(opened.length, 2, 'the subscriber set changed: one new connection');
  assert.equal(opened[0].closed, true);
  assert.equal(opened[1].url, '/api/stream?names=blog--web--s3,shop--api--s1&lines=12');
  for (let i = 0; i < 4; i++) { w.ctx.__st = fakeState(); w.run('state = __st; updateCurrentPage(state)'); }
  assert.equal(opened.length, 2, 'state polls never touch the stream');
  assert.equal(opened[1].closed, false);
  tail('shop--api--s1').click();
  assert.equal(opened.length, 3, 'one session less: reconnect with the rest');
  assert.equal(opened[2].url, '/api/stream?names=blog--web--s3&lines=12');
  tail('blog--web--s3').click();
  assert.equal(opened[2].closed, true, 'nobody is watching: the stream is closed');
  assert.equal(opened.length, 3);
  assert.equal(w.run('Live.es'), null);
  tail('shop--api--s1').click();
  assert.equal(opened.length, 4);
  w.location.hash = '#/inbox';
  assert.equal(opened[3].closed, true, 'leaving Home lets go of it');
  assert.equal(w.run('Live.subs.size'), 0);
});

test('on #/tasks the first tap of Archive swaps in its Confirm (the kanban repaints through the page hook)', () => {
  const task = { id: 1, title: 'Fix the cart', project: 'shop', repo: 'api', branch: 'worktree-fix', column: 'in_progress', tmux: 'shop--api--t-fix', session: sess('t-fix', { state: 'working' }), ci: null, overlap: [] };
  const { w } = pagesWorld({ state: fakeState({ tasks: [task] }) });
  w.location.hash = '#/tasks';
  const archive = () => page(w).querySelector('#tasks .task .tk-acts').querySelectorAll('button').find((b) => /Archive/.test(text(b)));
  assert.equal(text(archive()), 'Archive');
  archive().click();
  assert.match(text(page(w).querySelector('#tasks')), /Confirm Archive/, 'the tasks section repainted, not just the page behind it');
});

/** A state.tasks row of the shape _tasks_view makes (v0.5.14a: phase, mode, prompt head), `over` on top. */
const taskRow = (id, over = {}) => ({
  ci: null, pr: null, id, project: 'shop', repo: 'api', slug: `t-${id}`, title: `Task ${id}`, branch: '', base: 'main', worktree: '', tmux: '', pr_url: null, pr_number: null, pr_state: null,
  cost_usd: null, overlap: [], created_at: ISO(45), column: 'backlog', session: null, agent: 'claude', mode: 'worktree', phase: 'backlog', session_row: null, prompt: 'do the thing', prompt_len: 12, ...over,
});
const withShell = (w) => w.run("globalThis.__created = []; globalThis.Shell = { openCreate: (k) => { __created.push(k); return true; } };");

test('#/tasks draws the Backlog column first (empty too, with what to do) and a + task button in the head that opens the task sheet', () => {
  const running = taskRow(1, { title: 'Fix the cart', branch: 'worktree-fix', column: 'in_progress', phase: 'running', tmux: 'shop--api--t-fix', prompt: null, session: sess('t-fix', { state: 'working' }) });
  const { w } = pagesWorld({ state: fakeState({ tasks: [running] }) });
  withShell(w);
  w.location.hash = '#/tasks';
  const sec = page(w).querySelector('section#tasks');
  assert.deepEqual(sec.querySelectorAll('.col h2').map((h) => text(h).replace(/\s*\(\d+\)$/, '')), ['Backlog', 'In progress'], 'Backlog first, even with nothing in it; the other empty columns stay out of the way');
  assert.match(text(sec.querySelector('.col[data-col=backlog]')), /nothing queued/);
  const add = sec.querySelectorAll('button').find((b) => /^\+\s*task$/.test(text(b)));
  assert.ok(add, 'a + task button in the head');
  add.click();
  assert.deepEqual(plain(w.get('__created')), ['task'], 'Shell.openCreate(\'task\'): the picker, or the form at once from a project page');
});

test('#/tasks with no task at all shows the empty state with a + task button, never a dead end', () => {
  const { w } = pagesWorld();
  withShell(w);
  w.location.hash = '#/tasks';
  const none = page(w).querySelectorAll('.nonideal').find((n) => /No tasks yet/.test(text(n)));
  assert.ok(none && !none.classList.contains('hidden'));
  const add = none.querySelectorAll('button').find((b) => /^\+\s*task$/.test(text(b)));
  assert.ok(add, 'the next step is in the empty state');
  add.click();
  assert.deepEqual(plain(w.get('__created')), ['task']);
  assert.match(text(none), /Backlog|schedule/i, 'and it says what a task can be: now, later, scheduled');
});

test('#/tasks: a backlog card has Start, Move…, Edit and Delete and no terminal; a started card keeps its Terminal and Archive', () => {
  const backlog = taskRow(2, { title: 'Park this for later', prompt: 'refactor the cart reducer\nkeep the tests green' });
  const running = taskRow(1, { title: 'Fix the cart', branch: 'worktree-fix', column: 'in_progress', phase: 'running', tmux: 'shop--api--t-fix', prompt: null, session: sess('t-fix', { state: 'working' }) });
  const { w } = pagesWorld({ state: fakeState({ tasks: [backlog, running] }) });
  withShell(w);
  w.location.hash = '#/tasks';
  const cardOf = (id) => page(w).querySelector(`#tasks .task[data-task="${id}"]`);
  const labels = (n) => n.querySelectorAll('button').map((b) => text(b).trim());
  assert.deepEqual(labels(cardOf(2)).filter((l) => /^(Start|Move…|Edit|Delete)$/.test(l)), ['Start', 'Move…', 'Edit', 'Delete']);
  assert.equal(cardOf(2).querySelectorAll('a').filter((a) => /\/term\//.test(a.getAttribute('href') || '')).length, 0, 'no terminal before there is a session');
  assert.match(text(cardOf(2)), /refactor the cart reducer/, 'the prompt head');
  assert.ok(labels(cardOf(1)).includes('Archive') && !labels(cardOf(1)).includes('Start'), 'a started card has the old actions');
  assert.ok(cardOf(1).querySelectorAll('a').some((a) => /\/term\/shop--api--t-fix/.test(a.getAttribute('href') || '')));
});

test('on Home the schedules strip opens the schedules list in the sheet, and the first tap of Delete swaps in its Confirm', () => {
  const job = { id: 3, project: 'shop', repo: 'api', name: 'Nightly audit', cron: '0 2 * * *', permission_mode: 'acceptEdits', max_turns: 20, max_budget_usd: null, enabled: 1,
    next_run_at: new Date(Date.now() + 5 * 3600e3).toISOString(), last_status: null, agent: 'claude' };
  const { w } = pagesWorld({ state: fakeState({ jobs: [job] }) });
  w.location.hash = '#/';
  const strip = page(w).querySelector('.sched');
  assert.equal(strip.classList.contains('hidden'), false);
  assert.equal(text(strip.querySelector('.sched-name')), 'Nightly audit');
  assert.equal(w.document.querySelector('#sheet').open, false);
  strip.querySelector('.sched-all').click();
  const sheetEl = w.document.querySelector('#sheet');
  assert.equal(sheetEl.open, true);
  const del = () => sheetEl.querySelector('#jobs').querySelectorAll('button').find((b) => /Delete/.test(text(b)));
  assert.equal(text(del()), 'Delete');
  del().click();
  assert.match(text(sheetEl.querySelector('#jobs')), /Confirm Delete/, 'the schedules list repainted with the two-tap state');
});

test('the schedules list in the Home sheet: an agent glyph on every job and run, Codex shows its model not turns, each agent\'s window is named', () => {
  const at = (h) => new Date(Date.now() + h * 3600e3).toISOString();
  const jobs = [{ id: 3, project: 'shop', repo: 'api', name: 'Nightly audit', cron: '0 2 * * *', permission_mode: 'acceptEdits', max_turns: 20, max_budget_usd: 2, enabled: 1, next_run_at: at(5), last_status: 'ok', agent: 'claude' },
    { id: 4, project: 'shop', repo: 'api', name: 'Nightly review', cron: '30 2 * * *', permission_mode: 'plan', max_turns: 30, max_budget_usd: null, enabled: 1, next_run_at: at(6), last_status: 'ok', agent: 'codex', opts: { model: 'gpt-5.5', reasoning_effort: 'high' } }];
  const runs = [{ id: 9, job_id: 4, started_at: new Date().toISOString(), status: 'ok', result: 'Reviewed.', error: null, session_id: 'x', cost_usd: null, num_turns: 1, task_id: null, agent: 'codex' },
    { id: 8, job_id: 3, started_at: new Date().toISOString(), status: 'ok', result: 'Audited.', error: null, session_id: 'y', cost_usd: 0.4, num_turns: 7, task_id: null, agent: 'claude' }];
  const { w } = pagesWorld({ state: fakeState({ jobs, runs, scheduler: { known: true, pct: 30, backoff_until: null, codex: { known: true, pct: 55, backoff_until: null } },
    agents: { claude: { installed: true, loggedIn: true }, codex: { installed: true, loggedIn: false } } }) });
  w.location.hash = '#/';
  page(w).querySelector('.sched-all').click();
  const sec = w.document.querySelector('#sheet').querySelector('#jobs');
  const rows = sec.querySelectorAll('.sess');
  assert.deepEqual(rows.map((r) => r.querySelector('.main .glyph.agent').textContent), ['◆', '◇']);
  assert.match(text(rows[0].querySelector('.meta')), /≤20 turns · ≤\$2/, 'Claude keeps its limits');
  assert.match(text(rows[1].querySelector('.meta')), /plan · gpt-5\.5 · high reasoning/);
  assert.doesNotMatch(text(rows[1].querySelector('.meta')), /turns/);
  const runLine = (r) => text(r.querySelector('.last .dim'));
  assert.match(runLine(rows[0]), /^◆ run #8 .* · ok · \$0\.40 · 7 turns/);
  assert.match(runLine(rows[1]), /^◇ run #9 .* · ok$/, 'a Codex run has no cost or turn count');
  const head = text(sec.querySelector('.head'));
  assert.match(head, /Claude 5-hour window at 30%/);
  assert.match(head, /Codex usage window at 55%/);
  assert.match(head, /Codex is not logged in on this box: its runs are deferred/);
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
  assert.match(text(panel('nodes')), /Paired nodesAdd node/);
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

test('settings > Agents carries the login button: it leads to Settings > Accounts (the add flow lives there), no modal', async () => {
  const st = fakeState({ claude: { installed: true, loggedIn: false } });
  const { w } = pagesWorld({ state: st });
  w.location.hash = '#/settings?sec=agents';
  const panel = page(w).querySelector('.settings-panel[data-sec=agents]');
  assert.match(text(panel), /Claude: not logged in/);
  assert.ok(!panel.querySelectorAll('button').some((b) => /in progress/.test(text(b))), 'no "Login in progress…" button any more');
  const login = panel.querySelectorAll('button').find((b) => text(b) === 'Log in');
  login.click();
  await tick();
  assert.equal(w.location.hash, '#/settings?sec=accounts');
  assert.ok(!calls(w).some((c) => c.path === '/api/claude/login'), 'the old endpoint is not used by the page any more');
  assert.ok(!w.get('ui.modal'), 'the old ui.modal flag is gone with the #modal');
  // logged in: Log out instead
  w.ctx.__st = fakeState();
  w.run('state = __st; updateCurrentPage(state)');
  w.location.hash = '#/settings?sec=agents';
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
  if (w.run('typeof live') !== 'undefined') {                             // the v0.4 flag shim: main.js still restores it, nothing acts on it any more
  }
  assert.equal(w.run('Live.es'), null);
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

test('every roster row has its own send box: Enter sends the text with enter and clears it; a shell row hides it', async () => {
  const { w } = pagesWorld();
  w.location.hash = '#/agents';
  const root = page(w);
  const row = root.querySelector('.rrow[data-tmux=shop--api--s2]');
  const form = row.querySelector('form.rr-send');
  const ta = form && form.querySelector('textarea.composer');
  assert.ok(ta, 'the row carries a composer textarea');
  assert.match(ta.getAttribute('aria-label'), /^send to /);
  assert.equal(form.classList.contains('hidden'), false, 'a session at its prompt can be written to');
  ta.value = 'run it\nnow';
  ta.dispatchEvent({ type: 'keydown', key: 'Enter', preventDefault() {} });
  await tick();
  assert.deepEqual(calls(w).pop(), { method: 'POST', path: '/api/sessions/shop--api--s2/keys', body: { text: 'run it\nnow', enter: true } });
  assert.equal(ta.value, '', 'the box clears after a send');
  assert.equal(text(row.querySelector('.rr-last .dim')), '› run it', 'the row echoes the sent text at once, before any poll');
  ta.value = 'draft';
  ta.dispatchEvent({ type: 'keydown', key: 'Enter', shiftKey: true, preventDefault() {} });
  assert.equal(ta.value, 'draft\n', 'Shift+Enter adds a line, nothing is sent');
  assert.equal(calls(w).filter((c) => c.path.endsWith('/keys')).length, 1);
  const sh = root.querySelector('.rrow[data-tmux=blog--web--sh] form.rr-send');
  assert.ok(sh && sh.classList.contains('hidden'), 'a shell pane is never typed into from the roster');
});

test('sidebar folders: a nested repo inside a repo keeps the parent repo and extends the path; only the project folder\'s top-level repos are skipped', () => {
  const { w } = pagesWorld();
  w.load('shell.js');                                                   // the pages world does not load the shell by default
  const Shell = w.get('Shell');
  const seed = (key, entries) => Shell.dirs.set(key, { entries });
  const k = (repo, path) => ({ key: Shell.dirKey('shop', repo, path), project: 'shop', repo, path, level: 1 });
  seed(Shell.dirKey('shop', 'api', 'vendor'), [{ name: 'lib', type: 'repo', has_children: true, dirty: true }, { name: 'docs', type: 'dir', has_children: true }]);
  const inside = plain(Shell.dirItems(k('api', 'vendor')).map((x) => [x.kind, x.repo, x.path, x.glyph]));
  assert.deepEqual(inside, [['dir', 'api', 'vendor/lib', 'repo'], ['dir', 'api', 'vendor/docs', 'dir']]);
  seed(Shell.dirKey('shop', 'root', ''), [{ name: 'api', type: 'repo', has_children: true }, { name: 'notes', type: 'dir', has_children: true }]);
  const top = plain(Shell.dirItems(k('root', '')).map((x) => [x.kind, x.repo, x.path]));
  assert.deepEqual(top, [['dir', 'root', 'notes']], 'the project folder\'s own repos are top-level sidebar nodes already');
});

// ---------------------------------------------------------------- v0.5.17b: Settings > Accounts (rows, rename sheet, last seen)

const A1 = '7d3e1c52-9a41-4b6f-8a0e-5c2f19d8a001', A2 = '7d3e1c52-9a41-4b6f-8a0e-5c2f19d8a002';
const secs = (n) => Math.floor(Date.now() / 1000) + n;
const TWO_ACCTS = () => ({ current: A1, list: [
  { key: A1, email: 'demo@example.com', name: 'Demo', label: null, plan: 'max', rl_5h: 42, rl_7d: 71, resets_5h: secs(7200), resets_7d: secs(200000), current: true },
  { key: A2, email: 'work@example.com', name: 'Work', label: 'Work', plan: 'pro', rl_5h: 100, rl_7d: 83, resets_5h: secs(-3600), resets_7d: secs(90000), current: false },
] });
const isPrimary = (b) => b.classList.contains('primary') || b.classList.contains('bp5-intent-primary');

/** The pages world on #/settings?sec=accounts with api() answering GET /api/accounts (full) and PATCH /api/accounts/<key> (patch(method, path, body)). */
function acctWorld(opts = {}) {
  const { full = null, patch = null } = opts;
  const accounts = 'accounts' in opts ? opts.accounts : TWO_ACCTS();               // an explicit undefined means a state without the key
  const { w } = pagesWorld({ state: fakeState({ accounts }) });
  w.ctx.__full = full;
  w.ctx.__patch = patch;
  w.ctx.__shellPaints = 0;
  w.run(`globalThis.Shell = { patchUsage() { __shellPaints++; } };
    { const prev = api; api = async (method, path, body) => {
      if (method === 'GET' && path === '/api/accounts') { __calls.push({ method, path, body }); return __full; }
      if (method === 'PATCH' && path.startsWith('/api/accounts/')) { __calls.push({ method, path, body }); return __patch ? __patch(method, path, body) : { ok: true }; }
      return prev(method, path, body); }; }`);
  w.location.hash = '#/settings?sec=accounts';
  return w;
}
const acctPanel = (w) => page(w).querySelector('.settings-panel[data-sec=accounts]');
const hiddenIn = (n) => { for (let x = n; x && x.nodeType === 1; x = x.parentNode) if (x.classList.contains('hidden')) return true; return false; };
const acctRows = (w) => acctPanel(w).querySelectorAll('.kv.set-acct');
const renameBtn = (row) => row.querySelectorAll('button').find((b) => text(b) === 'Rename');
const openRename = (w, i = 0) => { renameBtn(acctRows(w)[i]).click(); return sheet(w); };
const submitSheet = (w) => sheet(w).querySelector('form').dispatchEvent({ type: 'submit', preventDefault() {} });

test('settings tabs: Doctor sits after Box, Accounts after Agents and before App', () => {
  const w = acctWorld();
  assert.deepEqual(page(w).querySelectorAll('.tab').map((t) => t.getAttribute('data-tab')), ['notify', 'nodes', 'box', 'doctor', 'agents', 'accounts', 'app']);
  assert.equal(page(w).querySelector('.tab[data-tab=accounts]').getAttribute('aria-selected'), 'true');
  assert.equal(acctPanel(w).classList.contains('hidden'), false);
});

test('Settings > Accounts: one row per account with its name in the account\'s hue, plan and current chips, email, readings and Rename; the how-to closes the panel', () => {
  const w = acctWorld();
  assert.deepEqual(acctPanel(w).querySelectorAll('.set-h').map(text), ['Subscription accounts', 'When you switch', 'Add another subscription', 'Codex accounts', 'Add a Codex account'], 'labelled sections (the switch choice is hidden where saved logins are not supported; the Codex section is hidden on a box that sends no codex_accounts)');
  const rows = acctRows(w);
  assert.equal(rows.length, 2);
  const hueOf = (key) => w.run(`chipHue('account', ${JSON.stringify(key)})`);
  assert.deepEqual(rows.map((r) => text(r.querySelector('.k'))), ['Demo', 'Work'], 'label, else the name Claude reports');
  rows.forEach((r, i) => assert.ok(r.querySelector('.k').classList.contains(hueOf([A1, A2][i])), 'the same hue as the chips elsewhere'));
  assert.deepEqual(rows[0].querySelectorAll('.badge').map(text), ['max', 'current']);
  assert.deepEqual(rows[1].querySelectorAll('.badge').map(text), ['pro'], 'only the account in use is marked current');
  assert.ok(rows[0].querySelector('.badge.cur'));
  assert.ok(rows[0].querySelector('.badge').classList.contains(hueOf(A1)), 'the plan chip wears the hue');
  assert.match(text(rows[0].querySelector('.kv-main')), /demo@example\.com/);
  assert.match(text(rows[0].querySelector('.kv-main')), /5H 42% · 7D 71%/);
  assert.match(text(rows[1].querySelector('.kv-main')), /5H 0% \(resets in \d+h\d+m\) · 7D 83%/, 'a window that reset since the last reading counts as empty and counts down to the next reset');
  assert.doesNotMatch(text(rows[1].querySelector('.kv-main')), /rolled over/);
  for (const r of rows) {
    const b = renameBtn(r);
    assert.ok(b && b.closest('.kv-act'), 'Rename lives in the action cell');
    assert.equal(isPrimary(b), false, 'a quiet button: Settings has no filled primary here');
    assert.match(b.getAttribute('aria-label'), /^Rename (Demo|Work)$/);
  }
  assert.match(text(acctPanel(w)), /To use another subscription, run \/login in any terminal; the board notices within a minute and starts a new row for it\./);
  assert.ok(!/undefined|NaN|null/.test(text(acctPanel(w))));
});

test('Settings > Accounts: the account in use shows the topbar pills\' numbers (state.usage), the others their own last reading', () => {
  const { w } = pagesWorld({ state: fakeState({ accounts: TWO_ACCTS(), usage: { value: { five_hour: { used_percentage: 55, resets_at: secs(7000) }, seven_day: { used_percentage: 72.4, resets_at: secs(200000) } }, at: new Date().toISOString() } }) });
  w.location.hash = '#/settings?sec=accounts';
  const rows = acctRows(w);
  assert.match(text(rows[0].querySelector('.kv-main')), /5H 55% · 7D 72%/, 'one figure for the account in use on every screen');
  assert.match(text(rows[1].querySelector('.kv-main')), /5H 0% \(resets in \d+h\d+m\) · 7D 83%/, 'a quiet account: its own reading, its reset window counted as open');
});

test('Settings > Accounts: a rolled window counts down to the next reset in the row, its title says nothing was recorded since and when and where the reading came from; a missed-windows account and a no-reading one read plainly', () => {
  const ago = (s) => new Date((Date.now() - s * 1000) * 1).toISOString();
  const accounts = { current: A1, list: [
    { key: A1, email: 'demo@example.com', name: 'Demo', label: null, plan: 'max', rl_5h: 42, rl_7d: 71, resets_5h: secs(7200), resets_7d: secs(200000), rl_5h_at: ago(300), rl_7d_at: ago(300), source: 'statusline', current: true },
    { key: A2, email: 'work@example.com', name: 'Work', label: 'Work', plan: 'pro', rl_5h: 100, rl_7d: 83, resets_5h: secs(-3600), resets_7d: secs(-3 * 604800 - 3600), rl_5h_at: ago(7200), rl_7d_at: ago(7200), source: 'cache', current: false },
    { key: 'k3', email: 'new@example.com', name: 'New', label: null, plan: 'pro', rl_5h: null, rl_7d: null, resets_5h: null, resets_7d: null, rl_5h_at: null, rl_7d_at: null, source: 'statusline', current: false },
  ] };
  const { w } = pagesWorld({ state: fakeState({ accounts }) });
  w.location.hash = '#/settings?sec=accounts';
  const rows = acctRows(w);
  const use = (r) => r.querySelector('.kv-main .dim');
  assert.match(text(use(rows[0])), /^5H 42% · 7D 71%/, 'a window not yet reset keeps its percentage and adds no countdown to the row');
  assert.equal(use(rows[0]).getAttribute('title'), 'updated 5m ago · from the last session');
  assert.match(text(use(rows[1])), /^5H 0% \(resets in [34]h\d+m\) · 7D 0% \(resets in 6d2[23]h\)/, 'one window and several missed ones');
  assert.equal(use(rows[1]).getAttribute('title'), "5H: no usage recorded since the window reset · 7D: no usage recorded since the window reset · updated 2h ago · from Claude Code's cache");
  assert.match(text(use(rows[2])), /^no usage reading yet/);
  assert.equal(use(rows[2]).getAttribute('title'), 'no reading yet');
  assert.doesNotMatch(text(acctPanel(w)), /rolled over|undefined|NaN/);
});

test('Settings > Accounts empty state: says what is missing and still shows how to add a subscription', () => {
  for (const accounts of [undefined, null, { current: null, list: [] }, { list: 'x' }]) {
    const w = acctWorld({ accounts });
    assert.equal(acctRows(w).length, 0);
    assert.match(text(acctPanel(w)), /No Claude account seen yet/);
    assert.match(text(acctPanel(w)), /run \/login in any terminal/);
    assert.equal(acctPanel(w).querySelectorAll('button').filter((b) => !hiddenIn(b)).length, 0, 'no button shows: this board keeps no saved logins');
  }
});

test('Settings > Accounts names an account by label, name, email or key; one account still lists one row', () => {
  const w = acctWorld({ accounts: { current: A1, list: [{ key: A1, email: 'solo@example.com', name: null, label: null, plan: null, rl_5h: null, rl_7d: null, resets_5h: null, resets_7d: null, current: true }] } });
  const rows = acctRows(w);
  assert.equal(rows.length, 1);
  assert.equal(text(rows[0].querySelector('.k')), 'solo@example.com');
  assert.deepEqual(rows[0].querySelectorAll('.badge').map(text), ['current'], 'no plan, no plan chip');
  assert.match(text(rows[0].querySelector('.kv-main')), /no usage reading yet/);
  assert.equal(text(rows[0].querySelector('.kv-main')).includes('solo@example.com'), false, 'the email is not said twice when it is the name');
});

test('Settings > Accounts asks GET /api/accounts once for last_seen, shows "seen 3h ago", and a refusal or odd answer only costs that word', async () => {
  const w = acctWorld({ full: { current: A1, list: [{ key: A1, last_seen: new Date(Date.now() - 3 * 3600e3).toISOString() }, { key: A2, last_seen: new Date(Date.now() - 2 * 86400e3).toISOString() }] } });
  await tick(); await tick();
  assert.deepEqual(calls(w).filter((c) => c.path === '/api/accounts').map((c) => c.method), ['GET']);
  assert.deepEqual(acctRows(w).map((r) => (/seen \w+ ago/.exec(text(r.querySelector('.kv-main'))) || [])[0]), ['seen 3h ago', 'seen 2d ago']);
  w.run('updateCurrentPage(state)');
  w.location.hash = '#/settings?sec=box';
  w.location.hash = '#/settings?sec=accounts';
  await tick();
  assert.equal(calls(w).filter((c) => c.path === '/api/accounts').length, 1, 'not again within five minutes');
  for (const full of [{ ok: true }, null, { list: 'x' }, { list: [null, {}, { key: 'zz' }] }]) {
    const w2 = acctWorld({ full });
    await tick(); await tick();
    assert.equal(acctRows(w2).length, 2);
    assert.ok(!/seen/.test(text(acctPanel(w2))), JSON.stringify(full));
  }
});

test('Rename: the sheet has one field (the current label, 60 characters) and Save as the one primary; Save paints the label at once, the sheet stays open while PATCH {label} runs and closes when it succeeds', async () => {
  let release;
  const w = acctWorld({ patch: () => new Promise((r) => { release = r; }) });
  const sh = openRename(w, 1);
  assert.equal(sh.open, true);
  assert.equal(text(sh.querySelector('.sheet-title')), 'Rename account · Work');
  const input = sh.querySelector('input');
  assert.equal(input.value, 'Work');
  assert.equal(input.getAttribute('maxlength'), '60');
  assert.deepEqual(sh.querySelectorAll('button').filter(isPrimary).map(text), ['Save'], 'one primary in the sheet');
  assert.ok(sh.querySelectorAll('button').some((b) => text(b) === 'Cancel'));
  input.value = '  Client   work ';
  submitSheet(w);
  assert.equal(sheet(w).open, true, 'open while the box answers: a refusal needs somewhere to say why');
  assert.equal(sheet(w).querySelector('button.primary').disabled, true, 'Save cannot be tapped twice');
  assert.equal(text(acctRows(w)[1].querySelector('.k')), 'Client work', 'painted before the server answers, whitespace tidied');
  assert.equal(w.get('state').accounts.list[1].label, 'Client work');
  assert.ok(w.get('__shellPaints') >= 1, 'the topbar chip repaints too');
  assert.deepEqual(calls(w).filter((c) => c.method === 'PATCH'), [{ method: 'PATCH', path: `/api/accounts/${A2}`, body: { label: 'Client work' } }]);
  release({ ok: true });
  await tick(); await tick();
  assert.equal(sheet(w).open, false, 'closed once the box has it');
  assert.deepEqual(plain(w.get('__toasts')).pop(), { text: 'Renamed to Client work', kind: 'ok' });
  assert.equal(text(acctRows(w)[1].querySelector('.k')), 'Client work');
});

test('Rename: a refusal puts the old label back everywhere and says why in the sheet (and a toast), which stays open for another try; an empty label clears it; an unchanged one sends nothing', async () => {
  const w = acctWorld({ patch: () => { throw new Error('no such account'); } });
  openRename(w, 1).querySelector('input').value = 'Nope';
  submitSheet(w);
  await tick(); await tick();
  assert.equal(text(acctRows(w)[1].querySelector('.k')), 'Work', 'reverted');
  assert.equal(w.get('state').accounts.list[1].label, 'Work');
  assert.equal(sheet(w).open, true, 'the sheet stays open');
  assert.match(text(sheet(w).querySelector('.field-err')), /^Rename failed: no such account$/, 'the reason, under the field');
  assert.equal(sheet(w).querySelector('button.primary').disabled, false, 'Save can be tried again');
  assert.equal(sheet(w).querySelector('input').value, 'Nope', 'and what was typed is still there');
  assert.deepEqual(plain(w.get('__toasts')).pop(), { text: 'Rename failed: no such account', kind: 'bad' });
  // unchanged: close, no PATCH
  const before = calls(w).filter((c) => c.method === 'PATCH').length;
  openRename(w, 1);
  submitSheet(w);
  assert.equal(sheet(w).open, false);
  assert.equal(calls(w).filter((c) => c.method === 'PATCH').length, before);
  // clearing goes back to the name Claude reports
  const w2 = acctWorld();
  openRename(w2, 1).querySelector('input').value = '   ';
  submitSheet(w2);
  assert.deepEqual(calls(w2).filter((c) => c.method === 'PATCH').pop(), { method: 'PATCH', path: `/api/accounts/${A2}`, body: { label: '' } });
  assert.equal(text(acctRows(w2)[1].querySelector('.k')), 'Work', 'its own name again (Claude reports "Work" here)');
  await tick(); await tick();
  assert.equal(sheet(w2).open, false);
  assert.match(plain(w2.get('__toasts')).pop().text, /^Work is back to its own name$/);
});

test('Rename: more than 60 characters is refused in the sheet, which stays open and sends nothing', () => {
  const w = acctWorld();
  const sh = openRename(w, 0);
  sh.querySelector('input').value = 'x'.repeat(61);
  submitSheet(w);
  assert.equal(sheet(w).open, true);
  assert.match(text(sheet(w).querySelector('.field-err')), /At most 60 characters/);
  assert.equal(calls(w).filter((c) => c.method === 'PATCH').length, 0);
});

test('the Accounts panel is rebuilt when an identity or label changes, and not by a new usage reading or an unchanged poll', () => {
  const w = acctWorld();
  const first = acctRows(w)[0];
  w.run('updateCurrentPage(state)');
  assert.equal(acctRows(w)[0], first, 'an unchanged poll leaves the Rename button alone');
  const moved = TWO_ACCTS();
  moved.list[0].rl_5h = 55; moved.list[1].rl_7d = 90;
  w.ctx.__st = fakeState({ accounts: moved });
  w.run('state = __st; updateCurrentPage(state)');
  assert.equal(acctRows(w)[0], first, 'readings move with every statusline: they wait for the minute');
  const renamed = TWO_ACCTS();
  renamed.list[0].label = 'Personal';
  w.ctx.__st = fakeState({ accounts: renamed });
  w.run('state = __st; updateCurrentPage(state)');
  assert.notEqual(acctRows(w)[0], first);
  assert.equal(text(acctRows(w)[0].querySelector('.k')), 'Personal');
  const switched = TWO_ACCTS();
  switched.current = A2; switched.list[0].current = false; switched.list[1].current = true;
  w.ctx.__st = fakeState({ accounts: switched });
  w.run('state = __st; updateCurrentPage(state)');
  assert.deepEqual(acctRows(w).map((r) => !!r.querySelector('.badge.cur')), [false, true], 'a /login switch moves the marker');
});
