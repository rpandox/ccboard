/* ccboard lazy loader (issue #103): the page scripts and the sheets that open from anywhere are not in index.html; they load on first use, once, in order.
   Classic script, definition-only: nothing here touches the DOM, storage, the network or a timer until a bundle is asked for (Lazy.load, Lazy.run, Lazy.later,
   Lazy.warm, Lazy.watchLinks). main.js starts the link watcher; router.js asks for a route's bundle before it mounts the page.

   A bundle is a list of same-origin files under /static (a script is a dynamically created <script src> with async=false, so CSP default-src 'self' covers it; a sheet is a
   <link rel=stylesheet> whose load event is awaited, so a page never paints unstyled), and the bundles that must come first (needs). LAZY_EXPORTS below probes the
   registrations each module must provide (a world that loaded every file up front, a test, or a bundle another path loaded). The manifest below is the only list: a file that is
   in no bundle and not in index.html is never served to the page (tests/test_static_perf.py pins that the two lists cover every script under app/static).

   All application stylesheets are inserted in `rank` order, including the shared sheets and charts/termkit, so
   the cascade is the order the single pages.css had.

   Entry points that open from anywhere (the launcher sheet, the task dispatch and Fable sheets, the task card's own sheets) are stubbed below under the same global name: the stub loads the real
   file and calls the real function, which replaces the stub in the global scope when the script runs. */
'use strict';

const LAZY_BUNDLES = {
  loader: { js: ['/static/lazy.js'] },
  demo: { js: ['/static/demo-api.js'] },
  accountactions: { js: ['/static/account-actions.js'] },
  restartnotice: { js: ['/static/settings-restart.js'], css: [{ href: '/static/pages/restart.css', rank: 105 }] },
  restartactions: { needs: ['restartnotice'], js: ['/static/settings-restart-actions.js'] },
  tabs: { js: ['/static/tabs.js'] },
  quickeditor: { js: ['/static/quick-editor.js'] },
  app: { js: ['/static/core.js', '/static/refs.js', '/static/components.js', '/static/session-model.js', '/static/accounts.js', '/static/inbox-model.js', '/static/page-actions.js', '/static/banner.js', '/static/widgets-core.js', '/static/live.js', '/static/shell.js', '/static/router.js'], css: [{ href: '/static/vendor/blueprint/blueprint-used.css', rank: 0 }, { href: '/static/vendor/blueprint/blueprint-icons.css', rank: 1 }, { href: '/static/tokens.css', rank: 2 }, { href: '/static/style.css', rank: 3 }, { href: '/static/shell.css', rank: 4 }, { href: '/static/pages.css', rank: 5 }] },
  main: { needs: ['app'], js: ['/static/main.js'] },
  home: { needs: ['sessionrows', 'inboxcards', 'restartnotice'], js: ['/static/pages/home.js'], css: [{ href: '/static/pages/home.css', rank: 40 }] },
  sessionrows: { js: ['/static/pages/agents.js', '/static/page-selection.js', '/static/session-groups.js'], css: [{ href: '/static/pages/session-row.css', rank: 41 }] },
  inboxcards: { needs: ['sessionrows'], js: ['/static/inbox-cards.js'] },
  inbox: { needs: ['inboxcards'], js: ['/static/pages/inbox.js'] },
  widgets: { css: [{ href: '/static/pages/widgets.css', rank: 30 }], js: ['/static/pages/widgets.js'] },
  taskui: { js: ['/static/components-tasks.js', '/static/task-views.js'], css: [{ href: '/static/pages/tasks.css', rank: 60 }] },
  tasks: { needs: ['taskui'], js: ['/static/pages/tasks.js'] },
  search: { needs: ['sessionrows'], js: ['/static/pages/search.js'] },
  session: { needs: ['home', 'shelltree'], js: ['/static/pages/session.js'] },
  placeholders: { js: ['/static/pages/placeholders.js'] },
  nodes: { js: ['/static/nodes.js'], css: [{ href: '/static/pages/node-base.css', rank: 90 }] },
  keyboard: { needs: ['sessionrows', 'inboxcards'], js: ['/static/page-keys.js', '/static/keymap.js'] },
  shelltree: { js: ['/static/shell-tree.js'], css: [{ href: '/static/pages/project.css', rank: 50 }] },
  shelldock: { css: [{ href: '/static/shell-dock.css', rank: 6 }], needs: ['termkit'], js: ['/static/shell-dock.js'] },
  pools: { js: ['/static/pool-usage.js'], css: [{ href: '/static/pool-usage.css', rank: 119 }] },
  // the create flow behind Shell.openCreate (+ session / + task / Schedule): the place from the route, the repo picker, the launcher form in a sheet; shell.js asks for it on a tap
  demobare: { js: ['/static/bare-play.js'] },
  bare: { js: ['/static/bare.js'] },
  shellcreate: { needs: ['taskui'], css: [{ href: '/static/pages/create.css', rank: 80 }], js: ['/static/shell-create.js'] },
  // the sheets a task card opens on a tap (Move, Edit, the preview port, the diff and pull request): components.js and home.js call them by name, lazyStub below loads this first
  tasksheets: { needs: ['taskui'], css: [{ href: '/static/pages/task-sheets.css', rank: 70 }], js: ['/static/task-sheets.js'] },
  launcher: { needs: ['taskui'], js: ['/static/launcher.js', '/static/launcher-node.js'], css: [{ href: '/static/pages/create.css', rank: 80 }, { href: '/static/launcher.css', rank: 109 }] },
  palette: { needs: ['keyboard', 'taskui'], css: [{ href: '/static/pages/palette.css', rank: 20 }], js: ['/static/palette.js'] },
  // dnd is for a mouse: a task board asks Lazy.wants('dnd') before it loads it
  dnd: { js: ['/static/dnd.js'], when: () => lazyFinePointer() },
  tree: { js: ['/static/tree.js'] },
  termkit: { css: [{ href: '/static/termkit.css', rank: 201 }], js: ['/static/termkit.js'] },
  memory: { needs: ['tabs'], js: ['/static/pages/memory.js'], css: [{ href: '/static/pages/memory.css', rank: 113 }] },
  project: { css: [{ href: '/static/pages/home.css', rank: 40 }], needs: ['tree', 'sessionrows', 'taskui', 'shelltree', 'tabs'], js: ['/static/pages/project.js'] },
  // the Agents page (#/agents): the roster half of pages/agents.js; Settings > Agents reads the Codex threads list from it, so settings needs it
  agents: { needs: ['sessionrows'], js: ['/static/pages/agents-page.js'] },
  settings: { needs: ['agents', 'nodes', 'widgets', 'taskui', 'restartnotice', 'tabs'], js: ['/static/nodes-pair.js', '/static/pages/doctor.js', '/static/pages/settings.js'], css: [{ href: '/static/pages/settings.css', rank: 110 }] },
  // the hub view (issue #139): loads only once state.nodes_enabled is true (nodes.js Nodes.use), or for a #/n/ address
  nodeshub: { needs: ['nodes', 'sessionrows'], js: ['/static/nodes-hub.js', '/static/pages/node.js'], css: [{ href: '/static/pages/nodes.css', rank: 114 }] },
  // the epic view (issue #147): the page #/epic/<owner>/<repo>/<n>, the Epics list of the Tasks page, the palette's epic rows and the demo's answers for /api/epics; with it the New epic sheet (issue #158, epic-new.js: epicNewOpen)
  epic: { js: ['/static/pages/epic.js', '/static/pages/epic-new.js'], css: [{ href: '/static/pages/epic.css', rank: 115 }] },
  // the Team view (issue #197): the project page's Team tab, Settings > Team and the demo's answers for /api/team; both pages load it on first use, never at first paint
  team: { js: ['/static/team.js'], css: [{ href: '/static/pages/team.css', rank: 116 }] },
  // the Team page (issue #200): #/team, and what the sidebar group, the Home line and the palette draw once the board shares a repository (core.js teamUse); never at first paint
  teamview: { needs: ['team'], js: ['/static/pages/team.js'], css: [{ href: '/static/pages/team-view.css', rank: 117 }] },
  // the epic worker's panel (issue #157): Settings > Nodes and a paired node's page ask for it on first use (and the demo's answers for /api/epic-worker); never at first paint
  worker: { js: ['/static/nodes-worker.js'], css: [{ href: '/static/pages/worker.css', rank: 118 }] },
  usage: { needs: ['widgets'], css: [{ href: '/static/charts.css', rank: 200 }], js: ['/static/charts.js', '/static/pages/usage.js'] },
  quad: { needs: ['termkit', 'sessionrows', 'shelldock'], js: ['/static/pages/quad.js'], css: [{ href: '/static/pages/quad.css', rank: 112 }] },
  onboarding: { needs: ['launcher'], js: ['/static/pages/onboarding.js'], css: [{ href: '/static/pages/onboarding.css', rank: 111 }] },
};

/* The consumer-facing registration/export from EVERY script, including the secondary members of a
   bundle. load fires even after a runtime exception, so it is not a success probe.
   No eval/Function: these checks remain compatible with default-src 'self'. */
const LAZY_EXPORTS = {
  '/static/lazy.js': () => Lazy !== undefined,
  '/static/demo-api.js': () => demoApi !== undefined,
  '/static/account-actions.js': () => accountSwitch !== undefined && !accountSwitch.lazyStub,
  '/static/settings-restart.js': () => settingsRestart !== undefined,
  '/static/settings-restart-actions.js': () => settingsRestartPoll !== undefined,
  '/static/tabs.js': () => tabs !== undefined,
  '/static/quick-editor.js': () => quickReplyEditor !== undefined && !quickReplyEditor.lazyStub,
  '/static/core.js': () => api !== undefined,
  '/static/refs.js': () => Ref !== undefined && typeof KeyBindings.bind === 'function',
  '/static/components.js': () => toast !== undefined,
  '/static/session-model.js': () => SESSION_RANK !== undefined,
  '/static/accounts.js': () => acctStore !== undefined,
  '/static/inbox-model.js': () => Inbox !== undefined,
  '/static/page-actions.js': () => deployNow !== undefined,
  '/static/banner.js': () => homeAway !== undefined,
  '/static/widgets-core.js': () => typeof Widgets.limitBanner === 'function',
  '/static/live.js': () => typeof Live.subscribe === 'function' && typeof Live.ended === 'function',
  '/static/shell.js': () => typeof Shell.buildTopbar === 'function' && typeof Shell.limit === 'function',
  '/static/router.js': () => route !== undefined,
  '/static/main.js': () => firstStateReady !== undefined,
  '/static/pages/home.js': () => typeof pages.home?.mount === 'function',
  '/static/pages/agents.js': () => sessionCard !== undefined,
  '/static/page-selection.js': () => typeof Pages.dropSkeleton === 'function',
  '/static/session-groups.js': () => homeGroupNode !== undefined,
  '/static/inbox-cards.js': () => inboxCard !== undefined,
  '/static/pages/inbox.js': () => typeof pages.inbox?.mount === 'function',
  '/static/pages/widgets.js': () => Widgets.usageCard !== undefined,
  '/static/components-tasks.js': () => taskCard !== undefined,
  '/static/task-views.js': () => renderJobs !== undefined,
  '/static/pages/tasks.js': () => typeof pages.tasks?.mount === 'function',
  '/static/pages/search.js': () => typeof pages.search?.mount === 'function',
  '/static/pages/session.js': () => typeof pages.session?.mount === 'function',
  '/static/pages/placeholders.js': () => PLACEHOLDER_INFO !== undefined,
  '/static/nodes.js': () => typeof NodeView.row === 'function',
  '/static/page-keys.js': () => Pages.act !== undefined,
  '/static/keymap.js': () => typeof Keymap.install === 'function' && Keymap.installed,
  '/static/shell-tree.js': () => Shell.dirKey !== undefined,
  '/static/shell-dock.js': () => Shell.dockMount !== undefined,
  '/static/pool-usage.js': () => paintPoolStrip !== undefined,
  '/static/bare-play.js': () => demoBare !== undefined,
  '/static/bare.js': () => Shell.bareDevice !== undefined,
  '/static/shell-create.js': () => Shell.pickRepo !== undefined,
  '/static/task-sheets.js': () => diffSideControl !== undefined,
  '/static/launcher.js': () => launcherSchema !== undefined,
  '/static/launcher-node.js': () => lxnKit !== undefined,
  '/static/palette.js': () => typeof Palette.open === 'function' && typeof Palette.openHelp === 'function',
  '/static/dnd.js': () => typeof Dnd.bind === 'function',
  '/static/tree.js': () => typeof Tree.mount === 'function',
  '/static/termkit.js': () => TermKit !== undefined,
  '/static/pages/memory.js': () => typeof pages.memory?.mount === 'function',
  '/static/pages/project.js': () => typeof pages.project?.mount === 'function',
  '/static/pages/agents-page.js': () => typeof pages.agents?.mount === 'function',
  '/static/nodes-pair.js': () => { const view = NodeView; return typeof view.auditFilter === 'function' && typeof view.peer === 'function' && Array.isArray(view.SCOPES); },
  '/static/pages/doctor.js': () => doctorStore !== undefined,
  '/static/pages/settings.js': () => typeof pages.settings?.mount === 'function',
  '/static/nodes-hub.js': () => Nodes.ready === true,
  '/static/pages/node.js': () => ['node', 'node-session', 'node-task'].every(id => typeof pages[id]?.mount === 'function'),
  '/static/pages/epic.js': () => typeof pages.epic?.mount === 'function',
  '/static/pages/epic-new.js': () => epicNewOpen !== undefined,
  '/static/team.js': () => typeof Team.projectSection === 'function' && typeof Team.settingsPanel === 'function',
  '/static/pages/team.js': () => typeof pages.team?.mount === 'function',
  '/static/nodes-worker.js': () => typeof EpicWorker.build === 'function',
  '/static/charts.js': () => typeof Charts.ready === 'function' && typeof Charts.line === 'function' && typeof Charts.spark === 'function',
  '/static/pages/usage.js': () => typeof pages.usage?.mount === 'function',
  '/static/pages/quad.js': () => typeof pages.quad?.mount === 'function',
  '/static/pages/onboarding.js': () => typeof pages.onboarding?.mount === 'function',
};

function lazyUrl(path) {
  const release = typeof BOOT_RELEASE !== 'undefined' ? BOOT_RELEASE : document.documentElement?.getAttribute('data-release');
  return release && release !== '__ASSET_VERSION__' ? path + '?v=' + encodeURIComponent(release) : path;
}
function lazyExport(src) {
  try { return !!(LAZY_EXPORTS[src] && LAZY_EXPORTS[src]()); } catch (_) { return false; }
}

/* route id -> the bundle that registers its page (the routes not named here are registered by index.html's own scripts) */
const LAZY_ROUTES = { home: 'home', inbox: 'inbox', tasks: 'tasks', search: 'search', session: 'session', agents: 'agents', project: 'project', settings: 'settings', usage: 'usage', quad: 'quad', memory: 'memory', onboarding: 'onboarding', node: 'nodeshub', 'node-session': 'nodeshub', 'node-task': 'nodeshub', epic: 'epic', team: 'teamview' };

const lazyPromises = {};      // bundle -> the promise of its load, while it is running or done
const lazyDoneSet = {};       // bundle -> true once its files ran (a probe can say so too, until this loader has touched the bundle)
const lazyStarted = {};       // bundle -> true once this loader began to fetch it: a half-loaded bundle (a script ran, its sheet failed) is not done because a probe sees a global
const lazyScripts = {};       // src -> promise
const lazyScriptDone = {};    // fulfilled scripts can only be reused with their exports
const lazySheets = {};        // href -> promise
const lazyLinks = [];         // { rank, node } of the sheets this loader put in
const lazyWaiting = {};       // bundle -> callbacks queued by Lazy.later
const LAZY_WAIT_MAX = 300;

/* A mouse: (pointer: fine) and not the html.force-coarse QA switch (the same test dnd.js makes). */
function lazyFinePointer() {
  try { if (document.documentElement && document.documentElement.classList && document.documentElement.classList.contains('force-coarse')) return false; } catch (_) { /* no document element */ }
  try { return !!(window.matchMedia && window.matchMedia('(pointer: fine)').matches); } catch (_) { return false; }
}

function lazyDone(name) {
  const b = LAZY_BUNDLES[name];
  return !!b && (b.js || []).every(lazyExport) && (b.needs || []).every(lazyDone)
    && (!!lazyDoneSet[name] || !lazyStarted[name]);
}

function lazyHead() { return document.head || document.documentElement || document.body; }

/* Fetch a dependent's bytes while its prerequisites load, without executing it
   against missing globals. Match script priority so dependents cannot crowd out
   prerequisites. The eventual script uses the same release URL. */
function lazyPreload(src) {
  if (lazyScripts[src] || lazyExport(src)) return;
  const link = document.createElement('link');
  link.setAttribute('rel', 'preload');
  link.setAttribute('as', 'script');
  link.setAttribute('fetchpriority', 'low');
  link.setAttribute('href', lazyUrl(src));
  link.onload = link.onerror = () => { if (link.parentNode) link.parentNode.removeChild(link); };
  lazyHead().appendChild(link);
}

/* One script, once. Preloads fetch the batch in parallel; insertion waits for
   the preceding script to register successfully, including on Retry. */
function lazyScript(src) {
  if (lazyScriptDone[src] && !lazyExport(src)) { delete lazyScripts[src]; delete lazyScriptDone[src]; }
  if (lazyScripts[src]) return lazyScripts[src];
  if (lazyExport(src)) return Promise.resolve();
  const p = new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = lazyUrl(src);
    s.async = false;
    s.defer = true;
    s.onload = () => {
      if (!lazyExport(src)) { reject(new Error('missing export from ' + src)); return; }
      lazyScriptDone[src] = true; resolve();
    };
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
    link.setAttribute('href', lazyUrl(href));
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

/* Wait for every in-flight member before exposing Retry. Otherwise a late load
   from the failed attempt could race with its replacement. */
async function lazySettled(promises) {
  const results = await Promise.allSettled(promises);
  const failed = results.find((r) => r.status === 'rejected');
  if (failed) throw failed.reason;
}

/* Dependencies settle before dependents execute. Retry traverses the whole
   failed bundle again, reusing only scripts whose declared exports exist. */
function lazyLoad(name) {
  const b = LAZY_BUNDLES[name];
  if (!b) return Promise.reject(new Error('unknown bundle ' + name));
  if (lazyDone(name)) { lazyFlush(name); return Promise.resolve(); }
  if (lazyDoneSet[name]) { delete lazyDoneSet[name]; delete lazyPromises[name]; }
  if (lazyPromises[name]) return lazyPromises[name];
  lazyStarted[name] = true;
  const p = (async () => {
    const dependencies = (b.needs || []).map(lazyLoad);
    (b.js || []).forEach(lazyPreload);
    await lazySettled([
      ...(b.css || []).map(lazySheet),
      lazySettled(dependencies).then(async () => {
        for (const src of b.js || []) await lazyScript(src);
      }),
    ]);
    if (!(b.js || []).every(lazyExport)) throw new Error('incomplete bundle ' + name);
    lazyDoneSet[name] = true;
    lazyFlush(name);
  })();
  lazyPromises[name] = p;
  p.catch(() => {
    // Invalidate every transitive dependent, including a previously memoized
    // success whose prerequisite has lost an export. In-flight failures settle
    // before the Retry handler is available.
    const invalid = new Set([name]);
    let changed = true;
    while (changed) {
      changed = false;
      for (const [n, spec] of Object.entries(LAZY_BUNDLES)) {
        if (!invalid.has(n) && (spec.needs || []).some((d) => invalid.has(d))) { invalid.add(n); changed = true; }
      }
    }
    for (const n of invalid) {
      delete lazyPromises[n]; delete lazyDoneSet[n];
      for (const src of LAZY_BUNDLES[n].js || []) if (!lazyExport(src)) delete lazyScripts[src];
    }
  });
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
  lazyLoad(name).then(() => fn()).catch((e) => Lazy.fail(what || name, e, () => lazyRun(name, fn, what)));
  return undefined;
}

/* Run fn once the bundle is there, without asking for it: for work that only matters if something else loads it (Dnd.bind on a row). Bounded. */
function lazyLater(name, fn) {
  if (lazyDone(name)) { lazyFlush(name); fn(); return; }
  const q = (lazyWaiting[name] ||= []);
  q.push(fn);
  if (q.length > LAZY_WAIT_MAX) q.shift();
}

function lazyFail(what, err, retry = () => location.reload()) {
  console.error('ccboard lazy', what, err);
  if (typeof toast !== 'function') return;
  const node = toast(`Could not load ${what}. Check the connection and try again.`, { kind: 'bad', ttl: retry ? 0 : 7000 });
  if (node && retry) node.append(el('button', { type: 'button', class: 'btn small', text: 'Retry', onclick: (e) => {
    e.stopPropagation(); node.remove(); Promise.resolve().then(retry).catch((error) => lazyFail(what, error, retry));
  } }));
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
  done: lazyDone, wants: lazyWants, load: lazyLoad, pending: lazyPending, run: lazyRun, later: lazyLater, warm: lazyWarm, watchLinks: lazyWatchLinks, watchKeys: lazyWatchKeys, fail: lazyFail, url: lazyUrl,
};

/* ---------- entry points that open from anywhere: the real ones come with launcher.js (the launcher bundle) or task-sheets.js (the tasksheets bundle) ---------- */

function lazyStub(name, what, bundle = 'launcher') {
  const stub = function (...args) {
    const invoke = () => lazyLoad(bundle).then(() => {
      const real = window[name];
      if (typeof real !== 'function' || real === stub) throw new Error('missing action ' + name);
      return real(...args);
    });
    // Click handlers may ignore the return value; never leave a rejection
    // unhandled. Awaiting callers still receive the real result on success.
    return invoke().catch((e) => { Lazy.fail(what, e, () => stub(...args)); return false; });
  };
  stub.lazyStub = true;
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
    if (!loading && typeof Keymap !== 'undefined') { Keymap.install(); return; }
    const record = () => queue.push({ key: e.key, code: e.code, ctrlKey: e.ctrlKey, metaKey: e.metaKey, shiftKey: e.shiftKey, altKey: e.altKey,
      target: e.target, repeat: e.repeat, isComposing: e.isComposing, defaultPrevented: e.defaultPrevented, at: Date.now(),
      preventDefault() { this.defaultPrevented = true; }, stopPropagation() {} });
    // Even an unbound key (Tab, x, a modifier, etc.) can break a chord.
    if (loading) record();
    const t = e.target;
    const typing = t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
    const mac = /Mac|iP(?:hone|ad|od)/.test(navigator.platform || navigator.userAgent || '');
    if (mac ? e.ctrlKey : e.metaKey) return;
    const mod = mac ? !!e.metaKey : !!e.ctrlKey;
    const modified = mod && !e.altKey && !e.shiftKey && /^[1-9kj\\]$/i.test(e.key);
    const plain = !mod && !e.altKey && !typing && /^(?:[gcjkoaydrfz?\/\[]|Enter|Escape)$/.test(e.key);
    if (e.defaultPrevented || e.isComposing || e.repeat || (!modified && !plain) || (typing && !(mod && e.key.toLowerCase() === 'k'))) return;
    const palette = mod && e.key.toLowerCase() === 'k';
    if (t?.tagName === 'IFRAME' || document.activeElement?.tagName === 'IFRAME') return;
    if (t?.closest?.('[data-kbd=chart]') && !palette) return;
    if (e.key === 'Enter' && t?.closest?.('button, a[href], summary, [role=button], [role=link], [role=tab], [role=menuitem], [role=treeitem], [role=option]')) return;
    if (!loading) record();
    // Escape must still close native dialogs; unbound control keys retain their defaults.
    if (e.key !== 'Escape') { e.preventDefault(); e.stopImmediatePropagation(); }
    if (loading) return;
    loading = true;
    lazyLoad('keyboard').then(() => {
      Keymap.install();
      document.removeEventListener('keydown', gate, true);
      for (const event of queue.splice(0)) Keymap.handle(event, event.at);
      if (Keymap.pending && Date.now() - Keymap.pending.at > Keymap.CHORD_MS) Keymap.pending = null;
      loading = false;
    }, (err) => { loading = false; queue.length = 0; lazyFail('keyboard shortcuts', err); });
  };
  document.addEventListener('keydown', gate, true);
}

// Actions keep their promise (callers await mutations); the real declaration replaces the stub.
for (const [name, bundle] of [['acctProblemBack', 'accountactions'], ['acctProblemDismiss', 'accountactions'], ['accountSwitch', 'accountactions'], ['accountLogin', 'accountactions'], ['cxSwitch', 'accountactions'], ['quickReplyEditor', 'quickeditor']]) {
  if (typeof window[name] !== 'function') window[name] = lazyStub(name, name === 'quickReplyEditor' ? 'the quick reply editor' : 'account actions', bundle);
}

for (const name of ['settingsRestartArm', 'settingsRestartSend']) if (typeof window[name] !== 'function') window[name] = lazyStub(name, 'restart actions', 'restartactions');
