// A tiny vm-based loader for ccboard's classic scripts (no jsdom, no build step).
//
//   const w = makeWorld();            // a fresh vm context: window, document, location, history, storage stubs
//   w.load('router.js');              // runs app/static/router.js as a classic script inside it
//   const parseHash = w.get('parseHash');   // top-level const/function of any loaded script
//
// Values that come out of the context are objects of another realm: run them through plain() before deepStrictEqual.
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

export const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
export const STATIC = path.join(ROOT, 'app', 'static');

/** Copy a value out of the vm realm (JSON round trip: drops undefined and functions). */
export const plain = (v) => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)));

class MemStorage {
  #m = new Map();
  get length() { return this.#m.size; }
  key(i) { return [...this.#m.keys()][i] ?? null; }
  getItem(k) { return this.#m.has(String(k)) ? this.#m.get(String(k)) : null; }
  setItem(k, v) { this.#m.set(String(k), String(v)); }
  removeItem(k) { this.#m.delete(String(k)); }
  clear() { this.#m.clear(); }
}

function emitter() {
  const listeners = {};
  return {
    listeners,
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
    removeEventListener(type, fn) { listeners[type] = (listeners[type] || []).filter((f) => f !== fn); },
    dispatch(type, ev = {}) { for (const fn of [...(listeners[type] || [])]) fn({ type, ...ev }); },
  };
}

function makeNode(tag) {
  const em = emitter();
  const classes = new Set();
  const node = {
    tagName: String(tag).toUpperCase(), children: [], attrs: {}, style: {}, textContent: '', className: '',
    listeners: em.listeners, addEventListener: em.addEventListener, removeEventListener: em.removeEventListener,
    classList: {
      add(...c) { c.forEach((x) => classes.add(x)); },
      remove(...c) { c.forEach((x) => classes.delete(x)); },
      toggle(c, force) { const on = force === undefined ? !classes.has(c) : !!force; if (on) classes.add(c); else classes.delete(c); return on; },
      contains(c) { return classes.has(c); },
    },
    setAttribute(k, v) { node.attrs[k] = String(v); },
    getAttribute(k) { return k in node.attrs ? node.attrs[k] : null; },
    removeAttribute(k) { delete node.attrs[k]; },
    append(...kids) { node.children.push(...kids); },
    appendChild(kid) { node.children.push(kid); return kid; },
    dispatch: em.dispatch,
  };
  return node;
}

/**
 * A fresh browser-ish world. `extra` globals are merged over the defaults.
 *   world.history.calls  every replaceState/pushState call: { method, args }
 *   world.location.hash  assigning fires 'hashchange' like a browser; setHash(h, {silent:true}) does not
 *   world.document.nodes selector -> node map used by document.querySelector (default: nothing, so querySelector returns null)
 */
export function makeWorld(extra0 = {}) {
  const { companions = true, ...extra } = extra0;            // companions: false keeps the lazy halves out (lazy.test.mjs makes the loader fetch them)
  const win = emitter();
  const doc = emitter();
  let hash = '';
  const location = {
    hostname: 'box', host: 'box', protocol: 'https:', pathname: '/', search: '',
    get hash() { return hash; },
    set hash(v) {
      const next = v === '' || v === '#' ? '' : (String(v).startsWith('#') ? String(v) : '#' + v);
      if (next === hash) return;
      hash = next;
      win.dispatch('hashchange', {});
    },
  };
  const history = {
    calls: [],
    replaceState(state, title, url) { history.calls.push({ method: 'replaceState', args: [state, title, url] }); if (typeof url === 'string' && url.startsWith('#')) hash = url; },
    pushState(state, title, url) { history.calls.push({ method: 'pushState', args: [state, title, url] }); if (typeof url === 'string' && url.startsWith('#')) hash = url; },
  };
  const document = {
    title: '', nodes: {}, listeners: doc.listeners, hidden: false, visibilityState: 'visible', readyState: 'complete',
    addEventListener: doc.addEventListener, removeEventListener: doc.removeEventListener, dispatch: doc.dispatch,
    querySelector(sel) { return document.nodes[sel] ?? null; },
    querySelectorAll() { return []; },
    getElementById(id) { return document.nodes['#' + id] ?? null; },
    createElement(tag) { return makeNode(tag); },
    createElementNS(_ns, tag) { return makeNode(tag); },
    createTextNode(text) { return { nodeType: 3, textContent: String(text) }; },
    body: makeNode('body'),
  };
  const sandbox = {
    document, location, history,
    localStorage: new MemStorage(), sessionStorage: new MemStorage(),
    URLSearchParams, URL, console, setTimeout, clearTimeout, setInterval, clearInterval,
    navigator: { userAgent: 'node-test', onLine: true },
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
    addEventListener: win.addEventListener, removeEventListener: win.removeEventListener,
    ...extra,
  };
  const ctx = vm.createContext(sandbox);
  if (!companions) NO_COMPANIONS.add(ctx);
  vm.runInContext('globalThis.window = globalThis;', ctx);
  loadScript(ctx, 'nodes.js');       // index.html loads nodes.js (the Ref helpers) right after core.js; it is definition-only and needs nothing, so every world has it (never load it again)
  loadScript(ctx, 'nodes-pair.js');  // the pairing half (lazy.js loads it with the settings bundle, not index.html); definition-only too, so a world that needs the pair words and the demo's writes has it up front
  return {
    ctx, document, location, history, window: { listeners: win.listeners },
    localStorage: sandbox.localStorage, sessionStorage: sandbox.sessionStorage,
    load: (file) => loadScript(ctx, file),
    run: (code) => vm.runInContext(code, ctx),
    get: (name) => vm.runInContext(name, ctx),
    fire: (type, ev) => win.dispatch(type, ev),
    setHash(h, { silent = false } = {}) { if (silent) hash = h; else location.hash = h; },
  };
}

/**
 * Lazy halves (issue #103 and later): code that index.html does not load up front lives in a file of its own that lazy.js fetches on first use. A test world is not a
 * browser, so by default loading the file a half was cut from also runs the half right after it, once per world: the world is then the one it was before the cut and
 * every test that loads its scripts by a list keeps working. makeWorld({ companions: false }) skips this, for the tests that drive the real loader (lazy.test.mjs).
 * A companion that is not in the directory the parent came from (an older static tree) is skipped.
 */
export const COMPANIONS = {
  'pages/agents.js': ['pages/agents-page.js'],
  'shell.js': ['shell-create.js'],
  'components.js': ['task-sheets.js'],
};
const NO_COMPANIONS = new WeakSet();
const COMPANIONS_RUN = new WeakMap();

function runCompanions(ctx, abs) {
  if (NO_COMPANIONS.has(ctx)) return;
  const posix = abs.split(path.sep).join('/');
  for (const [parent, list] of Object.entries(COMPANIONS)) {
    if (!posix.endsWith('/' + parent)) continue;
    const root = abs.slice(0, abs.length - parent.length);
    const done = COMPANIONS_RUN.get(ctx) || new Set();
    COMPANIONS_RUN.set(ctx, done);
    for (const c of list) {
      const target = path.join(root, c);
      if (done.has(c) || !fs.existsSync(target)) continue;
      done.add(c);
      new vm.Script(fs.readFileSync(target, 'utf8'), { filename: target }).runInContext(ctx);
    }
  }
}

/** Run a classic script (path relative to app/static, or absolute) inside a context. A missing file throws '<name> missing: ...'. */
export function loadScript(ctx, file) {
  const abs = path.isAbsolute(file) ? file : path.join(STATIC, file);
  if (!fs.existsSync(abs)) throw new Error(`${path.basename(abs)} missing: expected ${abs}`);
  if (abs === path.join(STATIC, 'nodes-pair.js') && vm.runInContext("typeof NodeView !== 'undefined' && typeof NodeView.peerRow", ctx) === 'function') return;     // makeWorld already ran it
  if (abs === path.join(STATIC, 'nodes.js') && vm.runInContext('typeof Ref', ctx) !== 'undefined') return;     // makeWorld already ran it (a `const Ref` cannot be declared twice); index.html's list still names it
  new vm.Script(fs.readFileSync(abs, 'utf8'), { filename: abs }).runInContext(ctx);
  runCompanions(ctx, abs);
}
