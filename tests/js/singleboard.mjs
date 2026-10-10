// The single-board contract of issue #139 (not a test file): render Home, Tasks, Agents, the inbox, the sidebar and the command palette of one fixed state on a clock that does not
// move, and dump each as text. tests/js/snapshots/single-board.json holds the dumps the code made BEFORE the hub view existed (taken from the v0.5.36 sources); with
// state.nodes_enabled false (or absent) the new code must produce the same bytes and ask nothing at /api/nodes*.
//
// To refresh the snapshot after a change that legitimately moves a byte of these screens: check out the tree BEFORE the change (archive the revision's app/static into an empty folder), call
// renderBoard({ dir: '<that folder>/app/static', nodesEnabled: undefined }), and write every key but `w` and `requests` to tests/js/snapshots/single-board.json (JSON, 1-space indent).
//
//   renderBoard({ dir, nodesEnabled })   dir: the folder of the scripts (app/static, or a copy of the old ones); nodesEnabled: undefined (absent) | false | true
import path from 'node:path';
import { STATIC, loadScript, makeWorld } from './harness.mjs';
import { installDom } from './minidom.mjs';

const T = Date.parse('2026-10-10T12:00:00.000Z');
const iso = (minsAgo) => new Date(T - minsAgo * 60000).toISOString();
const epoch = (minsAgo) => Math.floor(T / 1000) - minsAgo * 60;

/** An element and everything under it as text: tag.classes[attributes] and the text nodes, one per line, with the nesting as indentation. */
export function dump(n, depth = 0) {
  if (!n) return '';
  const pad = '  '.repeat(depth);
  if (n.nodeType === 3) return n.textContent.trim() ? `${pad}"${n.textContent}"\n` : '';
  if (n.nodeType !== 1) return '';
  const attrs = [...n._attrs.entries()].filter(([k]) => k !== 'id' || true).sort(([a], [b]) => (a < b ? -1 : 1)).map(([k, v]) => `${k}=${v}`).join(' ');
  const cls = [...n._cls].sort().join('.');
  let out = `${pad}<${n.tagName.toLowerCase()}${cls ? '.' + cls : ''}${attrs ? ' ' + attrs : ''}>\n`;
  for (const c of n.childNodes) out += dump(c, depth + 1);
  return out;
}

function sess(project, repo, name, over = {}) {
  const tmux = `${project}--${repo}--${name}`;
  return { tmux, name, created: epoch(600), attached: 0, command: 'claude', launcher: 'claude', agent: 'claude', path: `/srv/projects/${project}/${repo}`, state: 'idle', state_at: iso(30), needs_attention: false,
    last_prompt: 'fix the login bug', last_message: 'done, tests pass', stats: { model: 'Opus 5', context_pct: 42, context_size: 200000, cost_usd: 1.5 }, flags: {}, task: null, viewers: { full: 0, grid: 0, ro: 0 }, ...over };
}

export function fixedState(nodesEnabled) {
  const repo = (project, name, sessions) => ({ name, path: `/srv/projects/${project}/${name}`, state: 'ok', branch: 'main', dirty: false, devcontainer: false, sessions });
  const st = {
    tmux_down: false, user: 'alice', version: 'test', node_name: 'box',
    config: { code_https_port: 10000, projects_dir: '/srv/projects', ntfy: { enabled: false }, backup: {} },
    claude: { installed: true, loggedIn: true, email: 'a@example.com', subscriptionType: 'max' }, login: { running: false },
    projects: [
      { name: 'ccboard', path: '/srv/projects/ccboard', root: null, orphan_sessions: [], repos: [repo('ccboard', 'ccboard', [sess('ccboard', 'ccboard', 's1', { state: 'working', state_at: iso(1) }),
        sess('ccboard', 'ccboard', 's2', { state: 'waiting', needs_attention: true, state_at: iso(12), last_message: 'Which one should GET /devices expose?' })])] },
      { name: 'petroit', path: '/srv/projects/petroit', root: null, orphan_sessions: [], repos: [repo('petroit', 'api', [sess('petroit', 'api', 's1', { state: 'done', needs_attention: true, state_at: iso(20) })])] },
    ],
    pending_permissions: [{ id: 12, tmux_name: 'ccboard--ccboard--s2', tool_name: 'Bash', summary: 'Bash: npm test', created_at: iso(3) }],
    tasks: [{ id: 5, slug: 'stock-sync', title: 'Sync variant stock', project: 'petroit', repo: 'api', tmux: 'petroit--api--s1', column: 'in_progress', mode: 'worktree', pr_number: null, pr_url: null, pr_state: null },
      { id: 6, slug: 'later', title: 'Write the changelog', project: 'ccboard', repo: 'ccboard', tmux: null, column: 'backlog', mode: 'worktree', pr_number: null, pr_url: null, pr_state: null, phase: 'backlog' }],
    jobs: [], runs: [], nodes: null, health: null, backup: null, cost: null, usage: null, block: null, rate_limited: null, clone_queue: null, scheduler: { known: false }, agents: {}, setup: { first_run: false, ok: true },
  };
  if (nodesEnabled !== undefined) st.nodes_enabled = nodesEnabled;
  return st;
}

const tick = () => new Promise((r) => setImmediate(r));

/** Load the scripts of `dir` in index.html's order (lazy.js and the lazy bundles left out: the pages and the palette are loaded here the way their routes load them). */
export async function renderBoard({ dir = STATIC, nodesEnabled, scripts = null } = {}) {
  class FixedDate extends Date {
    constructor(...a) { if (a.length) super(...a); else super(T); }
    static now() { return T; }
  }
  const w = makeWorld({ Date: FixedDate, matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) });
  installDom(w);
  w.ctx.__fetches = []; w.ctx.__calls = [];
  w.run("globalThis.fetch = async (url) => { __fetches.push({ url }); throw new Error('no network in this test'); };");
  const list = scripts || ['core.js', 'components.js', 'keymap.js', 'live.js', 'launcher.js', 'palette.js', 'router.js', 'shell.js', 'pages/inbox.js', 'pages/widgets.js', 'pages/tasks.js', 'pages/agents.js',
    'pages/home.js', 'pages/session.js', 'pages/search.js'];
  for (const f of list) loadScript(w.ctx, path.join(dir, f));
  w.run(`
    api = async (method, path, body) => { __calls.push({ method, path }); return { ok: true }; };
    toast = () => {}; poll = async () => {};
    Live.subscribe = () => () => {}; Live.unsubscribe = () => {};
    state = ${JSON.stringify(fixedState(nodesEnabled))};
  `);
  const out = {};
  const grab = async (name, hash, sel) => { w.location.hash = hash; await tick(); w.run('updateCurrentPage(state)'); await tick(); out[name] = dump(w.document.querySelector(sel)); };
  await grab('home', '#/', '#page');
  await grab('tasks', '#/tasks', '#page');
  await grab('agents', '#/agents', '#page');
  await grab('inbox', '#/inbox', '#page');
  w.run('Shell.refs = {}; Shell.buildSide(document.querySelector("#sidebar"), false); Shell.patchTrees()');
  out.sidebar = dump(w.document.querySelector('#sidebar'));
  w.run('Palette.open()');
  out.palette = dump(w.document.querySelector('#helpdlg'));
  w.run('Palette.ui.input.value = "ccb"; Palette.render()');
  out.paletteQuery = dump(w.document.querySelector('#helpdlg'));
  out.requests = JSON.parse(JSON.stringify(w.get('__fetches'))).map((f) => f.url).concat(JSON.parse(JSON.stringify(w.get('__calls'))).map((c) => `${c.method} ${c.path}`)).filter((u) => /\/api\/nodes/.test(u));
  out.w = w;
  return out;
}
