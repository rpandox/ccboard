/* ccboard lazy loader (issue #103): the page scripts and the sheets that open from anywhere are not in index.html; they load on first use, once, in order.
   Classic script, definition-only: nothing here touches the DOM, storage, the network or a timer until a bundle is asked for (Lazy.load, Lazy.run, Lazy.later,
   Lazy.warm, Lazy.watchLinks). main.js starts the link watcher; router.js asks for a route's bundle before it mounts the page.

   A bundle is a list of same-origin files under /static (a script is a dynamically created <script src> with async=false, so CSP default-src 'self' covers it; a sheet is a
   <link rel=stylesheet> whose load event is awaited, so a page never paints unstyled), the bundles that must come first (needs), and a probe that says whether the
   globals it defines already exist (a world that loaded every file up front, a test, or a bundle another path loaded). The manifest below is the only list: a file that is
   in no bundle and not in index.html is never served to the page (tests/test_static_perf.py pins that the two lists cover every script under app/static).

   Stylesheets of a page go in just before the first eager sheet that used to follow pages.css (the <link> of /static/charts.css in index.html), ordered by `rank`, so
   the cascade is the order the single pages.css had.

   Entry points that open from anywhere (the launcher sheet, the task dispatch and Fable sheets) are stubbed below under the same global name: the stub loads the real
   file and calls the real function, which replaces the stub in the global scope when the script runs. */
'use strict';

const LAZY_BUNDLES = {
  launcher: { js: ['/static/launcher.js'], probe: () => typeof launcherSchema === 'function' },
  palette: { js: ['/static/palette.js'], probe: () => typeof Palette !== 'undefined' },
  // dnd is for a mouse: a task board asks Lazy.wants('dnd') before it loads it
  dnd: { js: ['/static/dnd.js'], probe: () => typeof Dnd !== 'undefined', when: () => lazyFinePointer() },
  tree: { js: ['/static/tree.js'], probe: () => typeof Tree !== 'undefined' },
  termkit: { js: ['/static/termkit.js'], probe: () => typeof TermKit !== 'undefined' },
  memory: { js: ['/static/pages/memory.js'], css: [{ href: '/static/pages/memory.css', rank: 13 }], probe: () => typeof Memory !== 'undefined' },
  project: { needs: ['tree'], js: ['/static/pages/project.js'], probe: () => typeof projectPage !== 'undefined' },
  settings: { js: ['/static/nodes-pair.js', '/static/pages/doctor.js', '/static/pages/settings.js'], css: [{ href: '/static/pages/settings.css', rank: 10 }], probe: () => typeof settingsPage !== 'undefined' },
  usage: { js: ['/static/charts.js', '/static/pages/usage.js'], probe: () => typeof Usage !== 'undefined' },
  quad: { needs: ['termkit'], js: ['/static/pages/quad.js'], css: [{ href: '/static/pages/quad.css', rank: 12 }], probe: () => typeof Quad !== 'undefined' },
  onboarding: { needs: ['launcher'], js: ['/static/pages/onboarding.js'], css: [{ href: '/static/pages/onboarding.css', rank: 11 }], probe: () => typeof wizPage !== 'undefined' },
};

/* route id -> the bundle that registers its page (the routes not named here are registered by index.html's own scripts) */
const LAZY_ROUTES = { project: 'project', settings: 'settings', usage: 'usage', quad: 'quad', memory: 'memory', onboarding: 'onboarding' };

const lazyPromises = {};      // bundle -> the promise of its load, while it is running or done
const lazyDoneSet = {};       // bundle -> true once its files ran (a probe can say so too, until this loader has touched the bundle)
const lazyStarted = {};       // bundle -> true once this loader began to fetch it: a half-loaded bundle (a script ran, its sheet failed) is not done because a probe sees a global
const lazyScripts = {};       // src -> promise
const lazySheets = {};        // href -> promise
const lazyLinks = [];         // { rank, node } of the sheets this loader put in
const lazyWaiting = {};       // bundle -> callbacks queued by Lazy.later
const LAZY_WAIT_MAX = 300;

/* A mouse: (pointer: fine) and not the html.force-coarse QA switch (the same test dnd.js makes). */
function lazyFinePointer() {
  try { if (document.documentElement && document.documentElement.classList && document.documentElement.classList.contains('force-coarse')) return false; } catch (_) { /* no document element */ }
  try { return !!(window.matchMedia && window.matchMedia('(pointer: fine)').matches); } catch (_) { return false; }
}

function lazyProbe(name) {
  const b = LAZY_BUNDLES[name];
  try { return !!(b && b.probe && b.probe()); } catch (_) { return false; }
}

function lazyDone(name) { return !!lazyDoneSet[name] || (!lazyStarted[name] && lazyProbe(name)); }

function lazyHead() { return document.head || document.documentElement || document.body; }

/* One script, once. Sequential callers get the order they ask in; async=false keeps a dynamically inserted script from jumping the queue. */
function lazyScript(src) {
  if (lazyScripts[src]) return lazyScripts[src];
  const p = new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = src;
    s.async = false;
    s.onload = () => resolve();
    s.onerror = () => { try { if (s.parentNode) s.parentNode.removeChild(s); } catch (_) { /* detached */ } reject(new Error('could not load ' + src)); };
    lazyHead().appendChild(s);
  });
  lazyScripts[src] = p;
  p.catch(() => { if (lazyScripts[src] === p) delete lazyScripts[src]; });
  return p;
}

/* One stylesheet, once, in its rank's place; resolved on the load event so the page is styled before it mounts. */
function lazySheet(sheet) {
  const href = sheet.href;
  if (lazySheets[href]) return lazySheets[href];
  const p = new Promise((resolve, reject) => {
    const link = document.createElement('link');
    link.setAttribute('rel', 'stylesheet');
    link.setAttribute('href', href);
    link.setAttribute('data-lazy', '1');
    link.onload = () => resolve();
    link.onerror = () => {
      try { if (link.parentNode) link.parentNode.removeChild(link); } catch (_) { /* detached */ }
      const i = lazyLinks.findIndex((x) => x.node === link);
      if (i >= 0) lazyLinks.splice(i, 1);
      reject(new Error('could not load ' + href));
    };
    const rank = Number(sheet.rank) || 0;
    const after = lazyLinks.find((x) => x.rank > rank);
    let anchor = after ? after.node : null;
    if (!anchor) { try { anchor = document.querySelector('link[href="/static/charts.css"]'); } catch (_) { anchor = null; } }
    const head = lazyHead();
    if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(link, anchor); else head.appendChild(link);
    lazyLinks.push({ rank, node: link });
    lazyLinks.sort((a, b) => a.rank - b.rank);
  });
  lazySheets[href] = p;
  p.catch(() => { if (lazySheets[href] === p) delete lazySheets[href]; });
  return p;
}

function lazyFlush(name) {
  const q = lazyWaiting[name];
  if (!q) return;
  delete lazyWaiting[name];
  for (const fn of q) { try { fn(); } catch (e) { console.error('ccboard lazy', name, e); } }
}

/* What loading a bundle takes, in order: the bundles it needs (and theirs) that are not here yet, then itself. */
function lazyPlan(name, plan = { names: [], js: [], css: [] }) {
  const b = LAZY_BUNDLES[name];
  if (!b) throw new Error('unknown bundle ' + name);
  if (plan.names.includes(name) || lazyDone(name)) return plan;
  for (const n of b.needs || []) lazyPlan(n, plan);
  plan.names.push(name);
  plan.js.push(...(b.js || []));
  plan.css.push(...(b.css || []));
  return plan;
}

/* Load a bundle: every file it needs is requested at once (the scripts are inserted in order with async=false, so they also run in order) and the promise settles when all of
   them ran and the sheets are in. One promise per bundle; a failure is forgotten so the next ask tries again; a file that did load is not fetched twice. */
function lazyLoad(name) {
  if (!LAZY_BUNDLES[name]) return Promise.reject(new Error('unknown bundle ' + name));
  if (lazyDone(name)) { lazyFlush(name); return Promise.resolve(); }
  if (lazyPromises[name]) return lazyPromises[name];
  const p = (async () => {
    const plan = lazyPlan(name);
    for (const n of plan.names) lazyStarted[n] = true;
    const sheets = Promise.all(plan.css.map(lazySheet));
    const scripts = Promise.all(plan.js.map(lazyScript));
    sheets.catch(() => {});                                             // one failure rejects the whole load below; this keeps the other half from going unhandled
    scripts.catch(() => {});
    await Promise.all([sheets, scripts]);
    for (const n of plan.names) lazyDoneSet[n] = true;
    for (const n of plan.names) lazyFlush(n);
  })();
  lazyPromises[name] = p;
  p.catch(() => { if (lazyPromises[name] === p) delete lazyPromises[name]; });
  return p;
}

/* The bundles a route still waits for: the one that registers its page, when that is not loaded. [] when it can mount at once. */
function lazyPending(routeId) {
  const own = LAZY_ROUTES[routeId];
  return own && !lazyDone(own) ? [own] : [];
}

/* Is it worth loading this bundle now? Not loaded yet, and its `when` (dnd: the pointer is a mouse) agrees. */
function lazyWants(name) {
  const b = LAZY_BUNDLES[name];
  return !!b && !lazyDone(name) && (!b.when || !!b.when());
}

/* Run fn once the bundle is there: now when it is, else after it loads. A load that fails says so in a toast (the opener the person used does nothing else). */
function lazyRun(name, fn, what) {
  if (lazyDone(name)) return fn();
  lazyLoad(name).then(() => fn(), (e) => lazyFail(what || name, e));
  return undefined;
}

/* Run fn once the bundle is there, without asking for it: for work that only matters if something else loads it (Dnd.bind on a row). Bounded. */
function lazyLater(name, fn) {
  if (lazyDone(name)) { lazyFlush(name); fn(); return; }
  const q = (lazyWaiting[name] ||= []);
  q.push(fn);
  if (q.length > LAZY_WAIT_MAX) q.shift();
}

function lazyFail(what, err) {
  console.error('ccboard lazy', what, err);
  if (typeof toast === 'function') toast(`Could not load ${what}. Check the connection and try again.`, { kind: 'bad' });
}

/* Start loading a route's bundle quietly (a pointer over its link, a focus, a touch): the click that follows finds it there. Failures are not reported. */
function lazyWarm(routeId) {
  const need = lazyPending(routeId);
  for (const n of need) lazyLoad(n).catch(() => {});
}

/* One delegated listener set: a link to #/<route> warms that route's bundle. Called once by main.js. */
function lazyWatchLinks() {
  if (typeof document === 'undefined' || typeof document.addEventListener !== 'function') return false;
  const warm = (e) => {
    const t = e && e.target;
    const a = t && typeof t.closest === 'function' ? t.closest('a[href^="#/"]') : null;
    if (!a || typeof parseHash !== 'function') return;
    const r = parseHash(a.getAttribute('href'));
    if (r && !r.unknown) lazyWarm(r.id);
  };
  for (const type of ['pointerover', 'focusin', 'touchstart']) document.addEventListener(type, warm, { capture: true, passive: true });
  return true;
}

const Lazy = {
  bundles: LAZY_BUNDLES, routes: LAZY_ROUTES,
  done: lazyDone, wants: lazyWants, load: lazyLoad, pending: lazyPending, run: lazyRun, later: lazyLater, warm: lazyWarm, watchLinks: lazyWatchLinks, fail: lazyFail,
};

/* ---------- entry points that open from anywhere: the real ones come with launcher.js ---------- */

function lazyStub(name, what) {
  const stub = function (...args) {
    lazyLoad('launcher').then(() => {
      const real = window[name];
      if (typeof real === 'function' && real !== stub) real(...args);
    }, (e) => lazyFail(what, e));
    return true;
  };
  return stub;
}
if (typeof openLauncher !== 'function') window.openLauncher = lazyStub('openLauncher', 'the launcher');
if (typeof taskDispatchSheet !== 'function') window.taskDispatchSheet = lazyStub('taskDispatchSheet', 'the launcher');
if (typeof jobFableSheet !== 'function') window.jobFableSheet = lazyStub('jobFableSheet', 'the launcher');
