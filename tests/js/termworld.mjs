// A world for the terminal links and the remote tiles of issue #144 (node-term.test.mjs, node-tile.test.mjs): the hub view (tests/js/hubworld.mjs) plus termkit.js and pages/quad.js, a scripted
// `api`, Live.subscribe as a recorder, window.open as a recorder, and a dock world for shell.js. Not a test file (node --test only runs *.test.mjs).
import { makeWorld } from './harness.mjs';
import { installDom } from './minidom.mjs';
import { boardState, hubWorld, iso, page, plain, poll, projectsOf, rec, sess, text, tick } from './hubworld.mjs';

export { boardState, iso, page, plain, poll, projectsOf, rec, sess, text, tick };

export const HOSTILE = '<img src=x onerror=alert(1)>';
export const S1 = 'shop--api--s1';
export const S2 = 'shop--api--s2';
export const L1 = 'ccboard--ccboard--s1';
export const L2 = 'ccboard--ccboard--s2';
export const URL_S1 = 'https://build-box.example.ts.net/term/shop--api--s1';
export const all = (n, sel) => (n.document || n).querySelectorAll(sel);
export const err = (status, body) => Object.assign(new Error((body && body.error) || String(status)), { status, body });
export const flush = async (n = 6) => { for (let i = 0; i < n; i++) await tick(); };
export const btn = (root, label) => all(root, 'button').find((b) => text(b) === label);
export const ev = (key, over = {}) => ({ key, ctrlKey: false, metaKey: false, shiftKey: false, altKey: false, target: null, defaultPrevented: false, preventDefault() {}, ...over });

export class FakeRO {
  constructor(cb) { this.cb = cb; this.nodes = []; }
  observe(n) { this.nodes.push(n); }
  disconnect() { this.nodes = []; }
}

/** The nodes of the fixture: build-box (all scopes, online), alice-mac (read only), old-laptop (offline since a known time). */
export const nodeList = (over = {}) => [
  rec('build-box', { scopes: ['read', 'tasks', 'sessions', 'permissions'], ...(over['build-box'] || {}) }),
  rec('alice-mac', { scopes: ['read'], ...(over['alice-mac'] || {}) }),
  rec('old-laptop', { scopes: ['read', 'tasks', 'sessions'], status: 'offline', last_ok_at: new Date(Date.now() - 6 * 3600 * 1000).toISOString(), age_s: 21600, ...(over['old-laptop'] || {}) }),
];

/** A world with the hub view, the quad page and the terminal kit. h.reply(method, path, body) may return a body or throw err(...). h.feed(ref, lines, meta) hands a tail to the tile that subscribed. */
export async function tWorld(o = {}) {
  const width = o.width || 1280;
  const h = hubWorld({ state: o.state || boardState(), nodes: o.nodes || nodeList(), extra: { innerWidth: width, ResizeObserver: FakeRO, matchMedia: (q) => ({ matches: /840|1024/.test(q) && width >= 1024, addEventListener() {}, removeEventListener() {} }) } });
  const { w } = h;
  w.load('termkit.js');
  w.load('pages/quad.js');
  w.run('TermKit.tileMenu = undefined; TermKit.composer = undefined; TermKit.tune = undefined;');
  h.log = [];
  h.reply = null;
  w.ctx.__subs = [];
  w.ctx.__tags = [];
  w.ctx.__r = async (method, p, body) => {
    h.log.push({ method, path: p, body: body === undefined ? undefined : JSON.parse(JSON.stringify(body)) });
    if (h.reply) { const r = await h.reply(method, p, body); if (r !== undefined) return r; }
    if (/\/prompt$/.test(p)) return { node: 'build-box', age: 0, data: { ok: true, pasted: true, queued: !!(body && body.queue) } };
    return { ok: true };
  };
  w.run(`api = (m, p, b) => __r(m, p, b);
    Live.subscribe = (ref, fn) => { const s = { ref: typeof ref === 'string' ? ref : Ref.text(ref), fn, off: false }; __subs.push(s); return () => { s.off = true; }; };
    Live.isDemo = () => false;
    const __ce = document.createElement.bind(document); document.createElement = (t) => { __tags.push(String(t).toLowerCase()); return __ce(t); };`);
  h.writes = () => h.log.filter((c) => c.method !== 'GET');
  h.opened = () => plain(w.get('__opened'));
  h.subs = () => plain(w.get('__subs.map((s) => ({ ref: s.ref, off: s.off }))'));
  h.feed = (ref, lines, meta) => { w.ctx.__l = lines; w.ctx.__m = meta; w.ctx.__ref = ref; w.run('__subs.filter((s) => s.ref === __ref && !s.off).forEach((s) => s.fn(__l, __m))'); };
  h.tags = () => plain(w.get('__tags'));
  await poll(w);
  await tick();
  return h;
}

export const go = async (w, hash) => { w.location.hash = hash; await tick(); await tick(); };
export const tile = (w, key) => all(page(w), '.qtile').find((t) => t.getAttribute('data-tmux') === key.split('/').pop() && (t.getAttribute('data-node') || '') === (key.includes('/') ? key.split('/')[0] : ''));
export const qrOf = (w, key) => tile(w, key).querySelector('.qr');
export const box = (t) => t.querySelector('textarea');
export const line = (t) => text(t.querySelector('.qr-st-t'));
export const type = (ta, v) => { ta.value = v; ta.dispatchEvent({ type: 'input' }); };
export const enter = (ta, over = {}) => { const e = { type: 'keydown', key: 'Enter', defaultPrevented: false, prevented: 0, preventDefault() { this.prevented += 1; }, ...over }; ta.dispatchEvent(e); return e; };

/** The terminal dock (shell.js) with the hub view loaded and `nodes` read: installShell() on a skeleton with #app. */
export function dockWorld({ width = 1280, nodes = nodeList() } = {}) {
  const w = makeWorld({ innerWidth: width, matchMedia: (q) => { const m = /\(min-width:\s*(\d+)px\)/.exec(q); return { matches: !!m && width >= Number(m[1]), addEventListener() {}, removeEventListener() {} }; }, ResizeObserver: FakeRO, setInterval: () => 0, clearInterval: () => {} });
  const dom = installDom(w);
  const app = w.document.createElement('div');
  app.setAttribute('id', 'app');
  app.props = {};
  app.style = { setProperty(k, v) { app.props[k] = v; }, removeProperty(k) { delete app.props[k]; } };
  dom.body.append(app);
  for (const f of ['core.js', 'components.js', 'live.js', 'termkit.js', 'router.js', 'pages/widgets.js', 'pages/agents.js', 'shell.js', 'pages/session.js', 'nodes-hub.js', 'pages/node.js']) w.load(f);
  w.ctx.__calls = []; w.ctx.__opened = []; w.ctx.__toasts = []; w.ctx.__subs = [];
  w.run(`api = async (method, path, body) => { __calls.push({ method, path, body }); return { node: 'build-box', age: 0, data: { ok: true, pasted: true, queued: false } }; };
    toast = (text, o) => { __toasts.push({ text, kind: o && o.kind }); };
    openPage = (url) => { __opened.push(['page', url]); };
    window.open = (...a) => { __opened.push(a); return {}; };
    poll = async () => {};
    Live.subscribe = (ref, fn) => { const s = { ref: typeof ref === 'string' ? ref : Ref.text(ref), fn, off: false }; __subs.push(s); return () => { s.off = true; }; };
    Live.isDemo = () => false;`);
  w.ctx.__st = boardState();
  w.run('state = __st');
  w.ctx.__n = { nodes };
  w.run('Nodes.apply(__n, null)');
  w.run('installShell()');
  return { w, Shell: w.get('Shell'), dock: () => w.document.querySelector('#dock') };
}
