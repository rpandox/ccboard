/* ccboard lazy loader (issue #103): the page scripts and the sheets that open from anywhere are not in index.html; they load on first use, once, in order.
   Classic script, definition-only: nothing here touches the DOM, storage, the network or a timer until a bundle is asked for (Lazy.load, Lazy.run, Lazy.later,
   Lazy.warm, Lazy.watchLinks). main.js starts the link watcher; router.js asks for a route's bundle before it mounts the page.

   A bundle is a list of same-origin files under /static (a script is a dynamically created <script src> with async=false, so CSP default-src 'self' covers it; a sheet is a
   <link rel=stylesheet> whose load event is awaited, so a page never paints unstyled), the bundles that must come first (needs), and a probe that says whether the
   globals it defines already exist (a world that loaded every file up front, a test, or a bundle another path loaded). The manifest below is the only list: a file that is
   in no bundle and not in index.html is never served to the page (tests/test_static_perf.py pins that the two lists cover every script under app/static).

   All application stylesheets are inserted in `rank` order, including the shared sheets and charts/termkit, so
   the cascade is the order the single pages.css had.

   Entry points that open from anywhere (the launcher sheet, the task dispatch and Fable sheets, the task card's own sheets) are stubbed below under the same global name: the stub loads the real
   file and calls the real function, which replaces the stub in the global scope when the script runs. */
'use strict';

const LAZY_BUNDLES = {
  loader: { js: ['/static/lazy.js'] },
  demo: { js: ['/static/demo-api.js'], probe: () => typeof demoApi === 'function' },
  accountactions: { js: ['/static/account-actions.js'] },
  restartnotice: { js: ['/static/settings-restart.js'], css: [{ href: '/static/pages/restart.css', rank: 105 }], probe: () => typeof settingsRestart !== 'undefined' },
  restartactions: { needs: ['restartnotice'], js: ['/static/settings-restart-actions.js'] },
  tabs: { js: ['/static/tabs.js'], probe: () => typeof tabs === 'function' },
  quickeditor: { js: ['/static/quick-editor.js'] },
  app: { js: ['/static/core.js', '/static/refs.js', '/static/components.js', '/static/session-model.js', '/static/accounts.js', '/static/inbox-model.js', '/static/page-actions.js', '/static/banner.js', '/static/widgets-core.js', '/static/live.js', '/static/shell.js', '/static/router.js'], css: [{ href: '/static/vendor/blueprint/blueprint-used.css', rank: 0 }, { href: '/static/vendor/blueprint/blueprint-icons.css', rank: 1 }, { href: '/static/tokens.css', rank: 2 }, { href: '/static/style.css', rank: 3 }, { href: '/static/shell.css', rank: 4 }, { href: '/static/pages.css', rank: 5 }] },
  main: { js: ['/static/main.js'] },
  home: { needs: ['sessionrows', 'inboxcards', 'restartnotice'], js: ['/static/pages/home.js'], css: [{ href: '/static/pages/home.css', rank: 40 }], probe: () => typeof homePage !== 'undefined' },
  sessionrows: { js: ['/static/pages/agents.js', '/static/page-selection.js', '/static/session-groups.js'], css: [{ href: '/static/pages/session-row.css', rank: 41 }], probe: () => typeof sessionCard === 'function' },
  inboxcards: { needs: ['sessionrows'], js: ['/static/inbox-cards.js'], probe: () => typeof inboxCard === 'function' },
  inbox: { needs: ['inboxcards'], js: ['/static/pages/inbox.js'], probe: () => typeof inboxPage !== 'undefined' },
  widgets: { css: [{ href: '/static/pages/widgets.css', rank: 30 }], js: ['/static/pages/widgets.js'], probe: () => typeof Widgets !== 'undefined' && typeof Widgets.usageCard === 'function' },
  taskui: { js: ['/static/components-tasks.js', '/static/task-views.js'], css: [{ href: '/static/pages/tasks.css', rank: 60 }], probe: () => typeof taskCard === 'function' },
  tasks: { needs: ['taskui'], js: ['/static/pages/tasks.js'], probe: () => typeof tasksPage !== 'undefined' },
  search: { needs: ['sessionrows'], js: ['/static/pages/search.js'], probe: () => typeof searchPage !== 'undefined' },
  session: { needs: ['home', 'shelltree'], js: ['/static/pages/session.js'], probe: () => typeof sessionPeek !== 'undefined' },
  placeholders: { js: ['/static/pages/placeholders.js'], probe: () => typeof PLACEHOLDER_INFO !== 'undefined' },
  nodes: { js: ['/static/nodes.js'], css: [{ href: '/static/pages/node-base.css', rank: 90 }], probe: () => typeof NodeView !== 'undefined' },
  keyboard: { needs: ['sessionrows', 'inboxcards'], js: ['/static/page-keys.js', '/static/keymap.js'], probe: () => typeof Keymap !== 'undefined' },
  shelltree: { js: ['/static/shell-tree.js'], css: [{ href: '/static/pages/project.css', rank: 50 }], probe: () => typeof Shell.dirKey === 'function' },
  shelldock: { css: [{ href: '/static/shell-dock.css', rank: 6 }], needs: ['termkit'], js: ['/static/shell-dock.js'], probe: () => typeof Shell.dockMount === 'function' },
  pools: { js: ['/static/pool-usage.js'], css: [{ href: '/static/pool-usage.css', rank: 119 }], probe: () => typeof paintPoolStrip === 'function' },
  // the create flow behind Shell.openCreate (+ session / + task / Schedule): the place from the route, the repo picker, the launcher form in a sheet; shell.js asks for it on a tap
  demobare: { js: ['/static/bare-play.js'], probe: () => typeof demoBare === 'function' },
  bare: { js: ['/static/bare.js'], probe: () => typeof Shell !== 'undefined' && typeof Shell.bareDevice === 'function' },
  shellcreate: { needs: ['taskui'], css: [{ href: '/static/pages/create.css', rank: 80 }], js: ['/static/shell-create.js'], probe: () => typeof Shell !== 'undefined' && typeof Shell.pickRepo === 'function' },
  // the sheets a task card opens on a tap (Move, Edit, the preview port, the diff and pull request): components.js and home.js call them by name, lazyStub below loads this first
  tasksheets: { needs: ['taskui'], css: [{ href: '/static/pages/task-sheets.css', rank: 70 }], js: ['/static/task-sheets.js'], probe: () => typeof diffSideControl === 'function' },
  launcher: { needs: ['taskui'], js: ['/static/launcher.js', '/static/launcher-node.js'], css: [{ href: '/static/pages/create.css', rank: 80 }, { href: '/static/launcher.css', rank: 109 }], probe: () => typeof launcherSchema === 'function' && typeof lxnKit === 'function' },
  palette: { needs: ['keyboard', 'taskui'], css: [{ href: '/static/pages/palette.css', rank: 20 }], js: ['/static/palette.js'], probe: () => typeof Palette !== 'undefined' },
  // dnd is for a mouse: a task board asks Lazy.wants('dnd') before it loads it
  dnd: { js: ['/static/dnd.js'], probe: () => typeof Dnd !== 'undefined', when: () => lazyFinePointer() },
  tree: { js: ['/static/tree.js'], probe: () => typeof Tree !== 'undefined' },
  termkit: { css: [{ href: '/static/termkit.css', rank: 201 }], js: ['/static/termkit.js'], probe: () => typeof TermKit !== 'undefined' },
  memory: { needs: ['tabs'], js: ['/static/pages/memory.js'], css: [{ href: '/static/pages/memory.css', rank: 113 }], probe: () => typeof Memory !== 'undefined' },
  project: { css: [{ href: '/static/pages/home.css', rank: 40 }], needs: ['tree', 'sessionrows', 'taskui', 'shelltree', 'tabs'], js: ['/static/pages/project.js'], probe: () => typeof projectPage !== 'undefined' },
  // the Agents page (#/agents): the roster half of pages/agents.js; Settings > Agents reads the Codex threads list from it, so settings needs it
  agents: { needs: ['sessionrows'], js: ['/static/pages/agents-page.js'], probe: () => typeof agentsPage !== 'undefined' },
  settings: { needs: ['agents', 'nodes', 'widgets', 'taskui', 'restartnotice', 'tabs'], js: ['/static/nodes-pair.js', '/static/pages/doctor.js', '/static/pages/settings.js'], css: [{ href: '/static/pages/settings.css', rank: 110 }], probe: () => typeof settingsPage !== 'undefined' },
  // the hub view (issue #139): loads only once state.nodes_enabled is true (nodes.js Nodes.use), or for a #/n/ address
  nodeshub: { needs: ['nodes', 'sessionrows'], js: ['/static/nodes-hub.js', '/static/pages/node.js'], css: [{ href: '/static/pages/nodes.css', rank: 114 }], probe: () => typeof Nodes !== 'undefined' && Nodes.ready },
  // the epic view (issue #147): the page #/epic/<owner>/<repo>/<n>, the Epics list of the Tasks page, the palette's epic rows and the demo's answers for /api/epics; with it the New epic sheet (issue #158, epic-new.js: epicNewOpen)
  epic: { js: ['/static/pages/epic.js', '/static/pages/epic-new.js'], css: [{ href: '/static/pages/epic.css', rank: 115 }], probe: () => typeof epicPage !== 'undefined' },
  // the Team view (issue #197): the project page's Team tab, Settings > Team and the demo's answers for /api/team; both pages load it on first use, never at first paint
  team: { js: ['/static/team.js'], css: [{ href: '/static/pages/team.css', rank: 116 }], probe: () => typeof Team !== 'undefined' },
  // the Team page (issue #200): #/team, and what the sidebar group, the Home line and the palette draw once the board shares a repository (core.js teamUse); never at first paint
  teamview: { needs: ['team'], js: ['/static/pages/team.js'], css: [{ href: '/static/pages/team-view.css', rank: 117 }], probe: () => typeof TeamView !== 'undefined' },
  // the epic worker's panel (issue #157): Settings > Nodes and a paired node's page ask for it on first use (and the demo's answers for /api/epic-worker); never at first paint
  worker: { js: ['/static/nodes-worker.js'], css: [{ href: '/static/pages/worker.css', rank: 118 }], probe: () => typeof EpicWorker !== 'undefined' },
  usage: { needs: ['widgets'], css: [{ href: '/static/charts.css', rank: 200 }], js: ['/static/charts.js', '/static/pages/usage.js'], probe: () => typeof Usage !== 'undefined' },
  quad: { needs: ['termkit', 'sessionrows', 'shelldock'], js: ['/static/pages/quad.js'], css: [{ href: '/static/pages/quad.css', rank: 112 }], probe: () => typeof Quad !== 'undefined' },
  onboarding: { needs: ['launcher'], js: ['/static/pages/onboarding.js'], css: [{ href: '/static/pages/onboarding.css', rank: 111 }], probe: () => typeof wizPage !== 'undefined' },
};

/* route id -> the bundle that registers its page (the routes not named here are registered by index.html's own scripts) */
const LAZY_ROUTES = { home: 'home', inbox: 'inbox', tasks: 'tasks', search: 'search', session: 'session', agents: 'agents', project: 'project', settings: 'settings', usage: 'usage', quad: 'quad', memory: 'memory', onboarding: 'onboarding', node: 'nodeshub', 'node-session': 'nodeshub', 'node-task': 'nodeshub', epic: 'epic', team: 'teamview' };

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
    s.defer = true;
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
  done: lazyDone, wants: lazyWants, load: lazyLoad, pending: lazyPending, run: lazyRun, later: lazyLater, warm: lazyWarm, watchLinks: lazyWatchLinks, watchKeys: lazyWatchKeys, fail: lazyFail,
};

/* ---------- entry points that open from anywhere: the real ones come with launcher.js (the launcher bundle) or task-sheets.js (the tasksheets bundle) ---------- */

function lazyStub(name, what, bundle = 'launcher') {
  const stub = function (...args) {
    lazyLoad(bundle).then(() => {
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
// the task card's sheets (task-sheets.js, bundle tasksheets): a click handler calls the name, the stub loads the file and calls the real function that replaced it
for (const name of ['taskMoveSheet', 'taskEditSheet', 'taskPortSheet', 'openTaskModal']) if (typeof window[name] !== 'function') window[name] = lazyStub(name, 'the task sheet', 'tasksheets');

/* Install a tiny capture gate before the shell's fallback key listener. On the
   first shortcut, keep the event (and a following chord key) until keymap loads.
   Text entry, browser shortcuts, IME and already-handled events stay untouched. */
function lazyWatchKeys() {
  const queue = [];
  let loading = false;
  const gate = (e) => {
    if (typeof Keymap !== 'undefined') return;
    const t = e.target;
    const typing = t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
    const mac = /Mac|iP(?:hone|ad|od)/.test(navigator.platform || navigator.userAgent || '');
    if (mac ? e.ctrlKey : e.metaKey) return;
    const mod = mac ? !!e.metaKey : !!e.ctrlKey;
    const modified = mod && !e.altKey && !e.shiftKey && /^[1-9kj\\]$/i.test(e.key);
    const plain = !mod && !e.altKey && !typing && /^(?:[gcjkoaydrfz?\/\[]|Enter|Escape)$/.test(e.key);
    if (e.defaultPrevented || e.isComposing || e.repeat || (!modified && !plain && !(loading && !mod && !e.altKey && /^[hitqumsp]$/.test(e.key))) || (typing && !(mod && e.key.toLowerCase() === 'k'))) return;
    const palette = mod && e.key.toLowerCase() === 'k';
    if (t?.tagName === 'IFRAME' || document.activeElement?.tagName === 'IFRAME') return;
    if (t?.closest?.('[data-kbd=chart]') && !palette) return;
    if (e.key === 'Enter' && t?.closest?.('button, a[href], summary, [role=button], [role=link], [role=tab], [role=menuitem], [role=treeitem], [role=option]')) return;
    // Escape must still close native dialogs; unbound control keys retain their defaults.
    if (e.key !== 'Escape') { e.preventDefault(); e.stopImmediatePropagation(); }
    // A plain record: native KeyboardEvent properties are not enumerable.
    queue.push({ key: e.key, code: e.code, ctrlKey: e.ctrlKey, metaKey: e.metaKey, shiftKey: e.shiftKey, altKey: e.altKey,
      target: t, repeat: false, isComposing: false, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, stopPropagation() {} });
    if (loading) return;
    loading = true;
    lazyLoad('keyboard').then(() => {
      Keymap.install();
      document.removeEventListener('keydown', gate, true);
      for (const event of queue.splice(0)) Keymap.handle(event);
    }, (err) => { loading = false; queue.length = 0; lazyFail('keyboard shortcuts', err); });
  };
  document.addEventListener('keydown', gate, true);
}

// Actions keep their promise (callers await mutations); the real declaration replaces the stub.
for (const [name, bundle] of [['acctProblemBack', 'accountactions'], ['acctProblemDismiss', 'accountactions'], ['accountSwitch', 'accountactions'], ['accountLogin', 'accountactions'], ['cxSwitch', 'accountactions'], ['quickReplyEditor', 'quickeditor']]) {
  if (typeof window[name] !== 'function') window[name] = (...args) => lazyLoad(bundle).then(() => window[name](...args));
}

function settingsRestartArm(...args) { return Lazy.load('restartactions').then(() => settingsRestartArm(...args)); }
function settingsRestartSend(...args) { return Lazy.load('restartactions').then(() => settingsRestartSend(...args)); }
