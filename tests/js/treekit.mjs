// Shared test kit for the v0.5.6 tree and project page tests (tree.test.mjs, project.test.mjs). Not a test file (node --test only runs *.test.mjs).
//
//   fakeClock()      setTimeout / setInterval / clear* on a manual clock: `await clock.advance(10000)` runs what is due and lets promises settle
//   FakeAbort        an AbortController stand-in that records every controller it makes and whether it was aborted
//   treeServer()     a fake of GET /api/projects/<p>/repos/<r>/tree and /file answered from app/static/demo/tree.json and file.json (the demo
//                    fixtures, so the fixtures and the UI are exercised together); every request lands in server.log whichever way the
//                    script sent it (api(method, path, body, opts) or fetch(url, init)), with its If-None-Match and its abort signal
//   wireNetwork(w, server, clock)   installs the fake api / fetch / timers / AbortController into a world made by makeNetworkWorld()
//   settle()         lets promise chains (api stubs, awaits in the scripts) run to completion
//   treeItems(host) / visibleItems(host) / byPath(host, path) / labelOf(item) / pathOf(item)   readers over the rendered role=tree markup
//   key(w, name)     dispatches a keydown on the focused node (bubbles up like a browser's)
import fs from 'node:fs';
import path from 'node:path';
import { STATIC, makeWorld } from './harness.mjs';
import { installDom } from './minidom.mjs';

/** The script under test; CCB_TREE_JS points the whole kit at a patched copy of tree.js while a fix is pending. */
export const TREE_JS = process.env.CCB_TREE_JS || 'tree.js';

export const tick = () => new Promise((r) => setImmediate(r));
export async function settle(n = 6) { for (let i = 0; i < n; i++) await tick(); }

const readJson = (name) => JSON.parse(fs.readFileSync(path.join(STATIC, 'demo', name), 'utf8'));
export const TREE_FIXTURE = readJson('tree.json');
export const FILE_FIXTURE = readJson('file.json');
export const clone = (v) => JSON.parse(JSON.stringify(v));

// ------------------------------------------------------------------ a manual clock

export function fakeClock() {
  let now = 0;
  let nextId = 1;
  const timers = new Map();
  const add = (fn, ms, every) => { const id = nextId++; timers.set(id, { fn, at: now + Math.max(0, ms || 0), every: every ? Math.max(1, ms) : 0 }); return id; };
  const clock = {
    get now() { return now; },
    setTimeout: (fn, ms) => add(fn, ms, false),
    setInterval: (fn, ms) => add(fn, ms, true),
    clearTimeout: (id) => { timers.delete(id); },
    clearInterval: (id) => { timers.delete(id); },
    get pending() { return timers.size; },
    /** Move time forward by ms, firing due timers in order (an interval re-arms), letting promises settle after each one. */
    async advance(ms) {
      const end = now + ms;
      for (;;) {
        let best = null;
        for (const [id, t] of timers) if (t.at <= end && (!best || t.at < best[1].at)) best = [id, t];
        if (!best) break;
        const [id, t] = best;
        now = Math.max(now, t.at);
        if (t.every) t.at = now + t.every; else timers.delete(id);
        try { t.fn(); } catch (e) { console.error('fake timer threw', e); }
        await settle(3);
      }
      now = end;
      await settle(3);
    },
  };
  return clock;
}

// ------------------------------------------------------------------ AbortController

export function makeAbort() {
  const made = [];
  class FakeAbort {
    constructor() {
      const handlers = [];
      this.signal = {
        aborted: false,
        addEventListener(type, fn) { if (type === 'abort') handlers.push(fn); },
        removeEventListener(type, fn) { const i = handlers.indexOf(fn); if (i >= 0) handlers.splice(i, 1); },
        throwIfAborted() { if (this.aborted) { const e = new Error('aborted'); e.name = 'AbortError'; throw e; } },
        _fire() { for (const fn of [...handlers]) fn({ type: 'abort' }); },
      };
      this.id = made.length;
      made.push(this);
    }
    abort() { if (this.signal.aborted) return; this.signal.aborted = true; this.signal._fire(); }
  }
  return { FakeAbort, made };
}

// ------------------------------------------------------------------ the server

const strip = (etag) => String(etag || '').replace(/^W\//, '').replace(/^"|"$/g, '');
export const weak = (etag) => `W/"${etag}"`;

export function treeServer() {
  const server = {
    log: [],                 // every request: { via, kind: 'tree'|'file'|'other', project, repo, path, reveal, hidden, ignored, ifNoneMatch, signal, status }
    overrides: new Map(),    // 'repo|path' -> payload replacing the fixture (a changed level: a new etag)
    fail: new Map(),         // 'repo|path' -> { status, error } for a tree request
    held: new Set(),         // 'repo|path' of tree requests that wait for release()
    pending: [],             // [{ key, release() }]
    files: FILE_FIXTURE,
    ignoreAbort: false,      // true: a held request is not rejected when its signal aborts (a transport that ignores the signal): the late answer must be discarded by the script
    answers: {},             // api path prefix -> value (or a function of the request, which may throw) for everything that is not tree or file
  };
  const abortError = () => { const e = new Error('The operation was aborted'); e.name = 'AbortError'; return e; };

  server.handle = (via, url, { headers, signal } = {}) => {
    const u = new URL(url, 'https://box');
    const m = /^\/api\/projects\/([^/]+)\/repos\/([^/]+)\/(tree|file)$/.exec(u.pathname);
    const q = u.searchParams;
    const ifNoneMatch = (headers && (headers['If-None-Match'] || headers['if-none-match'])) || null;
    const entry = { via, kind: m ? m[3] : 'other', project: m && decodeURIComponent(m[1]), repo: m && decodeURIComponent(m[2]), path: q.get('path') || '', reveal: q.get('reveal'),
      hidden: q.get('hidden'), ignored: q.get('ignored'), ifNoneMatch, signal: signal || null, url: String(url), status: null };
    server.log.push(entry);
    if (!m) { entry.status = 200; return Promise.resolve({ status: 200, body: { ok: true } }); }
    const key = `${entry.repo}|${entry.path}`;
    const answer = () => {
      if (entry.kind === 'file') {
        const f = server.files[key];
        if (!f) { entry.status = 404; return { status: 404, body: { error: 'not found' } }; }
        const r = f.status === 403 && entry.reveal === '1' ? f.reveal : f;
        entry.status = r.status;
        return r.status === 200 ? { status: 200, body: clone(r.body) } : { status: r.status, body: { error: r.error } };
      }
      if (server.fail.has(key)) { const f = server.fail.get(key); entry.status = f.status; return { status: f.status, body: { error: f.error } }; }
      const payload = server.overrides.get(key) || TREE_FIXTURE[key];
      if (!payload) { entry.status = 404; return { status: 404, body: { error: 'not found' } }; }
      if (ifNoneMatch && strip(ifNoneMatch) === payload.etag) { entry.status = 304; return { status: 304, body: null, etag: payload.etag }; }
      entry.status = 200;
      return { status: 200, body: clone(payload), etag: payload.etag };
    };
    if (entry.kind === 'tree' && server.held.has(key)) {
      return new Promise((resolve, reject) => {
        const done = () => resolve(answer());
        const p = { key, entry, release: done, cancelled: false };
        server.pending.push(p);
        if (signal && signal.addEventListener && !server.ignoreAbort) signal.addEventListener('abort', () => { p.cancelled = true; reject(abortError()); });
      });
    }
    if (signal && signal.aborted) return Promise.reject(abortError());
    return Promise.resolve(answer());
  };
  server.tree = () => server.log.filter((r) => r.kind === 'tree');
  server.treePaths = () => server.tree().map((r) => `${r.repo}|${r.path}`);
  server.release = (key) => { for (const p of server.pending.filter((x) => x.key === key && !x.cancelled)) p.release(); server.pending = server.pending.filter((x) => x.key !== key); };
  server.clear = () => { server.log.length = 0; };
  return server;
}

/** A world with the DOM, core.js, components.js and the fake network (api, fetch, timers, AbortController) wired to `server` and `clock`. */
export function makeNetworkWorld({ server = treeServer(), clock = fakeClock(), state = null, extra = {}, files = [] } = {}) {
  const { FakeAbort, made } = makeAbort();
  const T0 = 1790000000000;
  class FakeDate extends Date { static now() { return T0 + clock.now; } }      // tree.js times its type-ahead buffer and its revalidation with Date.now()
  const hostFetch = async (url, init) => {
    const r = await server.handle('fetch', url, { headers: init && init.headers, signal: init && init.signal });
    return {
      ok: r.status >= 200 && r.status < 300, status: r.status, statusText: String(r.status),
      headers: { get: (k) => (/^etag$/i.test(k) && r.etag ? weak(r.etag) : null) },
      json: async () => r.body, text: async () => JSON.stringify(r.body),
    };
  };
  const w = makeWorld({
    setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout, setInterval: clock.setInterval, clearInterval: clock.clearInterval,
    AbortController: FakeAbort, fetch: hostFetch, Date: FakeDate, ...extra,
  });
  const dom = installDom(w);
  w.load('core.js');
  w.load('components.js');
  w.ctx.__server = server;
  w.ctx.__calls = [];
  // api(method, path, body, opts): the tree may pass { signal, headers } as a fourth argument; a 304 is returned as { __notModified: true }
  w.ctx.__api = async (method, path_, body, opts) => {
    w.ctx.__calls.push({ method, path: path_, body, opts });
    for (const [prefix, v] of Object.entries(server.answers)) if (path_.startsWith(prefix)) return typeof v === 'function' ? v({ method, path: path_, body }) : v;
    const r = await server.handle('api', path_, { headers: opts && opts.headers, signal: opts && opts.signal });
    if (method !== 'GET') return { ok: true };
    if (r.status === 304) return { __notModified: true, status: 304 };
    if (r.status >= 400) { const e = new Error((r.body && r.body.error) || `${r.status}`); e.status = r.status; throw e; }
    return r.body;
  };
  w.run('api = (...a) => __api(...a);');
  for (const f of files) w.load(f);
  if (state) { w.ctx.__st = state; w.run('state = __st'); }
  return { w, dom, server, clock, aborts: made };
}

// ------------------------------------------------------------------ readers over the markup

const kids = (n) => n.querySelectorAll('[role=treeitem]');

export function treeItems(host) { return kids(host); }

/** The label text of one node (its own, not its children's). */
export function labelOf(item) {
  const l = item.querySelector('.treelabel');
  return l ? l.textContent : '';
}

/** The directory path of a node: the labels of its treeitem ancestors plus its own, joined by '/' (a nested repo's children are prefixed by it too). */
export function pathOf(item) {
  const parts = [labelOf(item)];
  for (let n = item.parentNode; n; n = n.parentNode) if (n.nodeType === 1 && n.getAttribute('role') === 'treeitem') parts.unshift(labelOf(n));
  return parts.join('/');
}

function hiddenByAncestor(item) {
  for (let n = item.parentNode; n; n = n.parentNode) {
    if (n.nodeType !== 1) continue;
    if (n.getAttribute('role') === 'treeitem' && n.getAttribute('aria-expanded') !== 'true') return true;
    if (n.getAttribute('hidden') !== null || (n.classList && n.classList.contains('hidden'))) return true;
  }
  return false;
}

/** The nodes a keyboard user can reach, in screen order (children of a collapsed directory are not). */
export function visibleItems(host) { return kids(host).filter((i) => !hiddenByAncestor(i)); }

export function byPath(host, p) { return kids(host).find((i) => pathOf(i) === p) || null; }

export function isDirty(item) {
  if (item.classList.contains('dirty')) return true;
  const c = item.querySelector('.treecontent');
  return !!c && c.classList.contains('dirty');
}

/** The status letter a file shows beside its name ('' when it has none): the secondary label minus the size text that may precede it. */
export function secondary(item) {
  const s = item.querySelector('.treesecondary');
  return s ? s.textContent.replace(/^\s*\d+(?:\.\d+)?\s?(?:B|KB|MB)\s*/, '').trim() : '';
}

/** Dispatch a keydown on the focused node (it bubbles to the tree like a browser's); returns whether the script called preventDefault. */
export function key(w, name, mods = {}) {
  const target = w.document.activeElement;
  if (!target) throw new Error('nothing has the focus');
  const ev = { type: 'keydown', key: name, ctrlKey: false, metaKey: false, altKey: false, shiftKey: false, ...mods, defaultPrevented: false,
    preventDefault() { ev.defaultPrevented = true; }, stopPropagation() { ev._stopped = true; } };
  target.dispatchEvent(ev);
  return ev.defaultPrevented;
}

export const focused = (w) => w.document.activeElement;

/** What a tree looks like for a failure message: indent by level, '>' marks the tabbable node. */
export function dump(host) {
  return visibleItems(host).map((i) => `${'  '.repeat(Number(i.getAttribute('aria-level') || 1) - 1)}${i.getAttribute('tabindex') === '0' ? '>' : ' '}${labelOf(i)}${i.getAttribute('aria-expanded') === null ? '' : i.getAttribute('aria-expanded') === 'true' ? ' [-]' : ' [+]'}`).join('\n');
}

/** The project state the tree and the preview need to build code-server links: abs paths of the project folder and its repos. */
export function pathsState() {
  return {
    config: { code_https_port: 10000, projects_dir: '/srv/projects' },
    projects: [{ name: 'phasezero', path: '/srv/projects/phasezero', root: null, orphan_sessions: [], repos: [
      { name: 'NestJs-Ecommerce-Backend', path: '/srv/projects/phasezero/NestJs-Ecommerce-Backend', state: 'ok', branch: 'develop', dirty: true, sessions: [] },
      { name: 'website', path: '/srv/projects/phasezero/website', state: 'ok', branch: 'main', dirty: false, sessions: [] }] }],
  };
}
