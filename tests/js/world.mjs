// A shared world for the v0.5.5 page tests (home.test.mjs, inbox.test.mjs): the real scripts in index.html order (shell.js left out) on
// minidom's DOM, with recorders for api(), toast and Live, plus state fixtures shaped like /api/state. Not a test file (node --test only
// runs *.test.mjs).
//
//   const { w } = homeWorld({ state });   // w.location.hash = '#/' mounts Home; w.ctx.__calls / __toasts / __live record what happened
//   fixtureState()                        // the same sessions as app/static/demo/state.json (the cases Home and the inbox draw), times relative to now
import fs from 'node:fs';
import path from 'node:path';
import { STATIC, makeWorld, plain } from './harness.mjs';
import { installDom } from './minidom.mjs';

export { plain };

export const NOW = Date.now();
export const ISO = (minsAgo) => new Date(NOW - minsAgo * 60000).toISOString();
export const EPOCH = (minsAgo) => Math.floor(NOW / 1000) - minsAgo * 60;

/** One live session as a state payload carries it (statusline stats, hook state, flags). */
export function sess(project, repo, name, over = {}) {
  const tmux = `${project}--${repo}--${name}`;
  return {
    tmux, name, created: EPOCH(120), attached: 0, command: 'claude', launcher: 'claude', agent: 'claude', path: `/srv/projects/${project}/${repo}`,
    state: 'idle', state_at: ISO(30), needs_attention: false, last_prompt: 'fix the login bug', last_message: 'done, tests pass',
    stats: { model: 'Opus 5', context_pct: 42, context_size: 200000, cost_usd: 1.5 }, flags: {}, task: null,
    viewers: { full: 0, grid: 0, ro: 0 }, ...over,
  };
}

/** Project blocks from { project: { repo: [sessions] } }; a repo named 'root' becomes the project folder. */
export function projectsOf(spec) {
  return Object.entries(spec).map(([name, repos]) => {
    const p = { name, path: `/srv/projects/${name}`, root: null, orphan_sessions: [], repos: [] };
    for (const [repo, sessions] of Object.entries(repos)) {
      if (repo === 'root') p.root = { name: 'root', path: p.path, root: true, state: 'project', branch: null, dirty: null, devcontainer: false, sessions };
      else p.repos.push({ name: repo, path: `${p.path}/${repo}`, state: 'ok', branch: 'main', dirty: false, devcontainer: false, sessions });
    }
    return p;
  });
}

/** The cases of the demo fixture, relative to now: a permission, a question, a limit episode, a blocked bg job, a done session, a worktree. */
export function fixtureState(over = {}) {
  const sessions = {
    s1: sess('ccboard', 'ccboard', 's1', { state: 'idle', state_at: ISO(300), stats: { model: 'Opus 5', context_pct: 23, context_size: 200000, cost_usd: 12.5 },
      flags: { subagents: 2, registry: { status: 'idle', name: 'push deep links', name_source: 'auto', pid: 41207, bridge: true, kind: 'cli',
        job: { state: 'blocked', tempo: 'blocked', needs: 'approve the plan', suggested_reply: 'approve' } } } }),
    cx1: sess('ccboard', 'ccboard', 'cx1', { state: 'working', agent: 'codex', launcher: 'codex', command: 'codex', state_at: ISO(1), stats: { model: 'gpt-5.5', context_pct: 31, cost_usd: null } }),
    p2: sess('petroit', 'api', 's2', { state: 'errored', needs_attention: true, state_at: ISO(170), last_message: '5-hour limit reached · resets 6:25am', stats: { model: 'Opus 5', context_pct: 74, cost_usd: 9.8 } }),
    p1: sess('petroit', 'api', 's1', { state: 'done', needs_attention: true, state_at: ISO(20), last_message: 'Pushed 3 commits to feature/devices-pagination. The branch is ready for a PR.', stats: { model: 'Fable 5.1', context_pct: 47, cost_usd: 6.2 } }),
    p3: sess('petroit', 'api', 's3', { state: 'waiting', needs_attention: true, state_at: ISO(19), last_message: 'Both paginations work. Which one should GET /devices expose?', stats: { model: 'Fable 5.1', context_pct: 36, cost_usd: 2.4 } }),
    root: sess('phasezero', 'root', 's1', { state: 'ended', state_at: ISO(4000), path: '/srv/projects/phasezero', stats: { model: 'Opus 5', context_pct: 12, cost_usd: 140.0 } }),
    wt: sess('phasezero', 'NestJs-Ecommerce-Backend', 't-stock-sync', { state: 'working', state_at: ISO(1), path: '/srv/projects/phasezero/NestJs-Ecommerce-Backend/.claude/worktrees/stock-sync',
      stats: { model: 'Opus 5', context_pct: 88, context_size: 200000, cost_usd: 3.42 }, task: { id: 5, title: 'Sync variant stock when an order is cancelled', phase: 'running', auto_close: false } }),
    ck: sess('phasezero', 'website', 't-checkout-redesign', { state: 'waiting', needs_attention: true, state_at: ISO(3), last_message: 'Claude needs your permission to use Bash',
      path: '/srv/projects/phasezero/website/.claude/worktrees/checkout-redesign', stats: { model: 'Opus 5', context_pct: 61, context_size: 200000, cost_usd: 1.87 },
      task: { id: 4, title: 'Redesign checkout step 2 for mobile', phase: 'running', auto_close: false } }),
  };
  return fakeState({
    projects: projectsOf({
      ccboard: { ccboard: [sessions.s1, sessions.cx1] },
      petroit: { api: [sessions.p2, sessions.p1, sessions.p3] },
      phasezero: { root: [sessions.root], 'NestJs-Ecommerce-Backend': [sessions.wt], website: [sessions.ck] },
      internalSystem: { server: [] }, mailgate: { mailgate: [] },
    }),
    pending_permissions: [{ id: 12, tmux_name: 'phasezero--website--t-checkout-redesign', tool_name: 'Bash', summary: 'Bash: npm test', created_at: ISO(3) }],
    rate_limited: { value: { session: 'petroit--api--s2', message: '5-hour limit reached · resets 6:25am', kind: '5h', resets_at: EPOCH(-160) }, at: ISO(170) },
    tasks: [
      { id: 5, slug: 'stock-sync', title: 'Sync variant stock when an order is cancelled', project: 'phasezero', repo: 'NestJs-Ecommerce-Backend', tmux: sessions.wt.tmux, column: 'in_progress', mode: 'worktree', pr_number: null, pr_url: null, pr_state: null },
      { id: 4, slug: 'checkout-redesign', title: 'Redesign checkout step 2 for mobile', project: 'phasezero', repo: 'website', tmux: sessions.ck.tmux, column: 'needs_you', mode: 'worktree', pr_number: 88, pr_url: 'https://example.invalid/pull/88', pr_state: 'OPEN' },
    ],
    jobs: [
      { id: 4, project: 'petroit', repo: 'api', name: 'Prune stale worktrees', cron: '30 6 * * *', enabled: 1, next_run_at: new Date(NOW + 3 * 3600e3).toISOString(), last_run_at: ISO(1300), last_status: 'ok', agent: 'claude' },
      { id: 3, project: 'phasezero', repo: 'website', name: 'Weekly changelog digest', cron: '0 9 * * 1', enabled: 1, next_run_at: new Date(NOW + 30 * 3600e3).toISOString(), last_run_at: null, last_status: null, agent: 'claude' },
      { id: 2, project: 'ccboard', repo: 'ccboard', name: 'Nightly dependency audit', cron: '0 2 * * *', enabled: 1, next_run_at: new Date(NOW + 20 * 3600e3).toISOString(), last_run_at: ISO(900), last_status: 'ok', agent: 'claude' },
      { id: 1, project: 'petroit', repo: 'api', name: 'Draft the release notes', cron: null, enabled: 0, next_run_at: null, last_run_at: ISO(5000), last_status: 'ok', agent: 'claude' },
    ],
    ...over,
  });
}

export function fakeState(over = {}) {
  return {
    tmux_down: false, user: 'alice', version: 'test',
    config: { code_https_port: 10000, projects_dir: '/srv/projects', ntfy: { enabled: false }, backup: {} },
    claude: { installed: true, loggedIn: true, email: 'a@example.com', subscriptionType: 'max' }, login: { running: false },
    projects: [], pending_permissions: [], tasks: [], jobs: [], runs: [], nodes: null, health: null, backup: null, cost: null, usage: null, block: null,
    rate_limited: null, clone_queue: null, scheduler: { known: false }, agents: {}, setup: { first_run: false, ok: true }, ...over,
  };
}

const PAGE_FILES = ['home', 'inbox', 'widgets', 'tasks', 'agents', 'settings', 'search', 'session', 'placeholders'];

/**
 * A world with the DOM, the real scripts (core, components, live, launcher, router, the pages in index.html order) and recorders:
 *   __calls   every api() call {method, path, body};  __toasts   toast() calls;  __created   Shell.openCreate(kind) calls
 *   __live    {subscribed: [tmux...], unsubscribed: [tmux...], fns: {tmux: fn}} from the stubbed Live.subscribe / unsubscribe
 *   __answers path-prefix -> value api() answers with (default {ok: true}); a function value is called with the request
 * `realLive: true` keeps live.js untouched. `wide` makes the 1024 px media query match (dock layout).
 */
export function homeWorld({ state = fixtureState(), extra = {}, wide = false, realLive = false, withState = true } = {}) {
  const w = makeWorld({ matchMedia: (q) => ({ matches: wide && /1024/.test(q), addEventListener() {}, removeEventListener() {} }), ...extra });
  const dom = installDom(w);
  for (const f of ['core.js', 'components.js', 'live.js', 'launcher.js', 'router.js']) w.load(f);
  w.ctx.__calls = []; w.ctx.__toasts = []; w.ctx.__created = []; w.ctx.__answers = {};
  w.ctx.__live = { subscribed: [], unsubscribed: [], fns: {} };
  w.run(`
    api = async (method, path, body) => {
      __calls.push({ method, path, body });
      for (const [prefix, v] of Object.entries(__answers)) if (path.startsWith(prefix)) return typeof v === 'function' ? v({ method, path, body }) : v;
      return { ok: true };
    };
    toast = (text, o) => { __toasts.push({ text, kind: o && o.kind }); };
    poll = async () => {};
    globalThis.Shell = { openCreate: (kind) => { __created.push(kind); return true; } };
  `);
  if (!realLive) {
    w.run(`
      Live.subscribe = (tmux, fn) => { __live.subscribed.push(tmux); __live.fns[tmux] = fn; return () => Live.unsubscribe(tmux, fn); };
      Live.unsubscribe = (tmux, fn) => { __live.unsubscribed.push(tmux); if (__live.fns[tmux] === fn) delete __live.fns[tmux]; };
    `);
  }
  for (const f of PAGE_FILES) {
    if (!fs.existsSync(path.join(STATIC, 'pages', `${f}.js`))) continue;       // widgets.js may not have landed yet: Home then shows no usage card
    w.load(`pages/${f}.js`);
  }
  w.ctx.__st = state;
  if (withState) w.run('state = __st');
  return { w, dom };
}

export const page = (w) => w.document.querySelector('#page');
export const tick = () => new Promise((r) => setImmediate(r));
export const calls = (w) => plain(w.get('__calls'));
export const text = (n) => (n ? n.textContent : '');
export const setState = (w, st) => { w.ctx.__st = st; w.run('state = __st; updateCurrentPage(state)'); };
