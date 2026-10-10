// A world for the hub view tests (issue #139): the real scripts of index.html in its order (core, nodes, components, keymap, live, shell, router, the pages) plus the lazy
// 'nodeshub' bundle (nodes-hub.js, pages/node.js) and palette.js, on minidom's DOM, with a fake fetch for GET /api/nodes/state and recorders for api, toast and window.open.
// Not a test file (node --test only runs *.test.mjs).
//
//   const w = hubWorld({ state, nodes });     // nodes = records as GET /api/nodes/state answers them; state.nodes_enabled decides whether the hub view is on
//   await poll(w)                             // one Nodes.poll()
//   w.ctx.__fetches                           // every fetch: { url, headers }
import fs from 'node:fs';
import path from 'node:path';
import { STATIC, makeWorld, plain } from './harness.mjs';
import { installDom } from './minidom.mjs';
import { fakeState, projectsOf, sess } from './world.mjs';

export { plain, sess, projectsOf };

export const NOW = Date.now();
export const iso = (secAgo) => new Date(NOW - secAgo * 1000).toISOString();

/** A hub record as GET /api/nodes/state answers it. */
export function rec(handle, over = {}) {
  const base = {
    peer_id: `p-${handle}`, handle, node_id: `ts:n${handle.toUpperCase().replace(/[^A-Z0-9]/g, '')}`, name: handle, url: `https://${handle}.example.ts.net`, status: 'online',
    polled_at: iso(2), last_ok_at: iso(2), age_s: 2, skew_ms: 80, skew_warn: false, error_kind: null, etag: `e-${handle}`, legacy: false, scopes: ['read'],
    card: { app: 'ccboard', api: 1, node_id: `ts:n${handle}`, name: handle, url: `https://${handle}.example.ts.net`, version: '0.5.36', os: { system: 'Linux', release: '6.8', tailscale_os: 'linux' }, runtime: 'docker',
      agents: [{ id: 'claude', installed: true, version: '2.1.0', logged_in: true, hooks: true, login_problem: false }],
      accounts: { supported: true, items: [{ agent: 'claude', label: 'Work', current: true, window: { pct: 40, resets_at: Math.floor(NOW / 1000) + 3600, known: true }, limited: false }] } },
    state: {
      api: 1, node: { id: `ts:n${handle}`, version: '0.5.36', now: iso(0) }, etag_base: 'x',
      projects: [{ name: 'shop', repos: [{ name: 'api', slug: 'example/shop-api', branch: 'main', dirty: true }] }],
      sessions: [
        { tmux: 'shop--api--s1', project: 'shop', repo: 'api', session: 's1', agent: 'claude', state: 'waiting', needs_you: true, kind: 'user', since: iso(300), model: 'Opus 5' },
        { tmux: 'shop--api--s2', project: 'shop', repo: 'api', session: 's2', agent: 'codex', state: 'working', needs_you: false, kind: 'task', since: iso(60), model: 'gpt-5.5' },
      ],
      tasks: [{ id: 7, title: 'Add pagination', phase: 'running', agent: 'claude', project: 'shop', repo: 'api', branch: 'task/p', tmux: 'shop--api--s2', issue_ref: '#12', updated_at: iso(90) }],
      needs_you: { permissions: 0, input: 1, errors: 0 }, usage: { claude: { pct: 42, resets_at: Math.floor(NOW / 1000) + 3600, known: true, limited: false } }, lanes: { cap: 3, running: 1, free: 2 },
      login_problems: [], truncated: false,
    },
  };
  return { ...base, ...over };
}

const PAGES = ['inbox', 'widgets', 'tasks', 'agents', 'home', 'session', 'search'];

export function hubWorld({ state = null, nodes = [], search = '', extra = {}, scripts = true, realApi = false, hash = null } = {}) {
  const w = makeWorld({ matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }), setInterval: () => 0, clearInterval: () => {}, ...extra });   // no real timer: Nodes.start would keep node alive
  const dom = installDom(w);
  w.location.search = search;
  w.ctx.__fetches = []; w.ctx.__calls = []; w.ctx.__toasts = []; w.ctx.__opened = []; w.ctx.__ans = '{}';
  w.ctx.__demoFiles = (name) => fs.readFileSync(path.join(STATIC, 'demo', name), 'utf8');
  w.run(`
    globalThis.fetch = async (url, o) => {
      __fetches.push({ url, headers: Object.assign({}, (o && o.headers) || {}) });
      if (/^\\/static\\/demo\\//.test(url)) return { status: 200, ok: true, statusText: '', headers: { get: () => null }, json: async () => JSON.parse(__demoFiles(url.slice('/static/demo/'.length))) };
      const a = JSON.parse(__ans);
      if (a.throw) throw new Error(a.throw);
      return { status: a.status, ok: a.status >= 200 && a.status < 300, statusText: a.statusText || '', headers: { get: (k) => (String(k).toLowerCase() === 'etag' ? a.etag || null : null) }, json: async () => a.body };
    };`);
  if (scripts) {
    for (const f of ['core.js', 'components.js', 'keymap.js', 'live.js', 'launcher.js', 'palette.js', 'router.js', 'shell.js', ...PAGES.map((p) => `pages/${p}.js`), 'nodes-hub.js', 'pages/node.js']) {
      if (fs.existsSync(path.join(STATIC, f))) w.load(f);
    }
  }
  if (!realApi) w.run('api = async (method, path, body) => { __calls.push({ method, path, body }); return { ok: true }; };');
  w.run(`
    toast = (text, o) => { __toasts.push({ text, kind: o && o.kind }); };
    poll = async () => {};
    window.open = (...a) => { __opened.push(a); };
    if (typeof Live !== 'undefined') { Live.subscribe = () => () => {}; Live.unsubscribe = () => {}; }
  `);
  if (state) setState(w, state);
  answer(w, 200, { nodes, at: iso(0) }, 'W/"1"');
  return { w, dom };
}

/** What the next fetch of /api/nodes/state answers: a status, a body (an object) and an ETag, or {throw: 'message'}. */
export function answer(w, status, body, etag, extra = {}) { w.ctx.__ans = JSON.stringify({ status, body, etag, ...extra }); }

export function setState(w, st) { w.ctx.__st = JSON.stringify(st); w.run('state = JSON.parse(__st)'); }

/** A state of this board with `nodes_enabled` on or off, and a few sessions of its own. */
export function boardState(over = {}) {
  return fakeState({
    nodes_enabled: true, node_name: 'box',
    node: { handle: 'local', name: 'box', sessions: { live: 2, working: 1, needs_you: 1 } },
    usage: { value: { five_hour: { used_percentage: 33, resets_at: Math.floor(NOW / 1000) + 5400 } }, at: iso(5) },
    projects: projectsOf({ ccboard: { ccboard: [sess('ccboard', 'ccboard', 's1', { state: 'working', state_at: iso(60) }), sess('ccboard', 'ccboard', 's2', { state: 'waiting', needs_attention: true, state_at: iso(200) })] } }),
    ...over,
  });
}

export const poll = (w, o) => w.run(`Nodes.poll(${JSON.stringify(o || {})})`);
export const page = (w) => w.document.querySelector('#page');
export const text = (n) => (n ? n.textContent : '');
export const tick = () => new Promise((r) => setImmediate(r));
export const fetched = (w) => plain(w.get('__fetches')).filter((f) => /^\/api\/nodes/.test(f.url));
