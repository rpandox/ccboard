/* Demo-only fixtures and mutations; never needed by a real board. */
'use strict';
/* The fixtures were captured at demo.epoch: shift every timestamp by the elapsed time so ages, countdowns and the 'older' cut stay live. */
const ISO_TS = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/;
const EPOCH_KEYS = new Set(['created', 'resets_at', 'created_at_epoch', 'expires_at', 'resets_5h', 'resets_7d']);   // resets_5h / resets_7d: state.accounts.list[] (v0.5.17b)
function demoRebase(data) {
  const epoch = data && data.demo && data.demo.epoch;
  if (!epoch) return data;
  const dt = Math.floor(Date.now() / 1000 - (typeof epoch === 'number' ? epoch : Date.parse(epoch) / 1000));
  const walk = (v, key) => {
    if (Array.isArray(v)) return v.map((x) => walk(x, key));
    if (v && typeof v === 'object') { const o = {}; for (const [k, x] of Object.entries(v)) o[k] = k === 'demo' ? x : walk(x, k); return o; }
    if (typeof v === 'number' && EPOCH_KEYS.has(key) && v > 1e9) return v + dt;
    if (typeof v === 'string' && ISO_TS.test(v)) { const t = Date.parse(v); if (!Number.isNaN(t)) return new Date(t + dt * 1000).toISOString(); }
    return v;
  };
  return walk(data, '');
}

/* The demo's tailnet list (issue #134): the fixture's ISO times (the answer's at, last_seen, a row's at) follow the clock like state.json's (demoRebase), and a Refresh
   (?refresh=1) is answered as if every probe had just run, so the demo shows a stale row at first and a fresh one after the tap. */
function demoDiscover(data0, refresh) {
  const { variants: vs, ...rest } = demoRebase(data0);
  const mv = vs && ownKey(vs, demoNodeState()) ? vs[demoNodeState()] : null;
  const data = mv && Array.isArray(mv.moved) ? { ...rest, moved: mv.moved } : rest;               // Update address is offered at once in the demo (the board offers it on a Refresh)
  if (!refresh || !data || typeof data !== 'object' || !Array.isArray(data.rows)) return data;
  const iso = new Date().toISOString();
  return { ...data, at: iso, rows: data.rows.map((r) => (r && r.at ? { ...r, at: iso, age: 0, stale: false } : r)) };
}

/* The demo's pairing (issue #135, Settings > Nodes): demo/nodes.json holds the three lists (outgoing pairs, incoming pairs, activity) and, because a POST never reaches a box in demo
   mode, a code, a pair, a rotation and a removal are played here for the life of the page: the code is a made-up one, a new pair is listed as waiting for its first contact, a
   removed one disappears (one of them answers "the other node was not told") and each leaves a row in Activity. Nothing of this is a credential. The writes themselves (demoNodesWrite) are in nodes-pair.js, which loads with Settings, the only page that
   makes them; the state they leave (demoNodesMade) and the three GETs that read it are here. */
const demoNodesMade = { gone: new Set(), added: [], audit: [], seq: 0 };
async function demoNodesFixture() {
  const r = await fetch('/static/demo/nodes.json');
  return r.ok ? demoNodeVariant(demoRebase(await r.json())) : {};
}
/* ?nodestate=<name> (issue #153: unauthorized identity repair incompatible skew offline moved unknown) lays a variant over the demo's build-box: demo/nodes.json `variants` is {name: {hub: {handle: patch}, nodes: {peer_id: patch}}}, nodes-discover.json `variants` {name: {moved: [...]}}. */
function demoNodeState() { try { const m = /[?&]nodestate=([a-z]+)/.exec(location.search); return m ? m[1] : ''; } catch (_) { return ''; } }
function demoNodeVariant(data) {
  const v = data && data.variants && ownKey(data.variants, demoNodeState()) ? data.variants[demoNodeState()] : null;
  const lay = (rows, key, p) => (Array.isArray(rows) && p ? rows.map((r) => (r && ownKey(p, r[key]) ? { ...r, ...p[r[key]] } : r)) : rows);
  return v ? { ...data, hub: lay(data.hub, 'handle', v.hub), nodes: lay(data.nodes, 'peer_id', v.nodes) } : data;
}
/* The three GETs of demo/nodes.json, shaped like the board's ({nodes, pairs, at}, {pairs, at}, {rows, at}): the fixture's rows (times follow the clock like state.json's), minus the ones removed here, plus the ones paired here, plus the activity made here. */
function demoNodes(data0, bare) {
  const data = demoNodeVariant(demoRebase(data0));
  const gone = (r) => demoNodesMade.gone.has(r.peer_id);
  const at = new Date().toISOString();
  const pairs = (data.pairs || []).filter((r) => !gone(r));
  if (/^\/api\/nodes\/[^/]+\//.test(bare)) return typeof demoRelayRead === 'function' ? demoRelayRead(data, bare) : {};   // the relay's reads (issues #141, #143): an agents schema, a tail
  if (bare === '/api/nodes/state') return typeof demoHubState === 'function' ? demoHubState(data, at) : { nodes: [], at };   // the hub view (issue #139): the records of data.hub, built in nodes-hub.js
  if (bare === '/api/mcp/tokens') return data.mcp || {};   // Settings > Agents > Connect from another device (issue #152): the remote endpoint off, no device, and the nodes switch off
  if (bare === '/api/nodes/pairs') return { pairs, at };
  if (bare === '/api/nodes/audit') return { rows: [...demoNodesMade.audit, ...(data.audit || [])], at };
  return { nodes: [...(data.nodes || []).filter((r) => !gone(r)), ...demoNodesMade.added.filter((r) => !gone(r))], pairs, at };
}

/* A demo answer that is an HTTP error: the tree and file previews read err.status (the real endpoints answer 403 / 404 / 415 the same way). */
function demoError(status, message) {
  const e = new Error(message || `${status}`);
  e.status = status;
  return e;
}

/* Demo tree and file fixtures are maps keyed '<repo>|<path>' ('root' is the project folder, path '' a repo's top level); the query's
   hidden / ignored / repos flags are ignored. tree.json holds the /tree payloads; file.json holds {status: 200, body} or
   {status: 403|415, error, reveal?: {status: 200, body}} per file. An absent key answers 404. */
function demoPick(kind, bare, query, data) {
  const m = /^\/api\/projects\/[^/]+\/repos\/([^/]+)\//.exec(bare);
  const q = new URLSearchParams(query || '');
  const key = decodeURIComponent(m ? m[1] : '') + '|' + (q.get('path') || '');
  const hit = data && ownKey(data, key) ? data[key] : null;
  if (!hit) throw demoError(404, 'not found');
  if (kind === 'tree') return hit;
  const r = hit.status === 403 && q.get('reveal') === '1' && hit.reveal ? hit.reveal : hit;
  if (r.status === 200) return r.body;
  throw demoError(r.status, r.error);
}

/* The demo's make-believe usage refresh (v0.5.17f, part 2): in demo mode a POST never reaches a box, so the Usage page's Refresh is played here. A tap is answered
   at once; the reading the state carries holds still for 3 s ('refreshing…') and then moves to that moment, from Claude Code's cache. A query flag picks the state:
     ?refresh=none   no Claude session is live (the button reads 'start a session to refresh'; a POST would be refused with a 409)
     ?refresh=stale  the newest reading is 10 minutes old (opening the Usage page asks once by itself)
     ?refresh=stuck  a tap is answered but the reading never moves (the wait ends with 'no new reading yet')
   Only demoApi calls these, so a real board never sees them. */
const demoRefresh = { tap: 0, base: 0, shown: 0 };       // tap: when the make-believe ask was made (epoch s), base: the reading's time then, shown: the last one handed out
const DEMO_REFRESH_HOLD = 3;                              // seconds the reading holds still after a tap
function demoRefreshFlag() { try { const m = /[?&]refresh=([a-z]+)/.exec(location.search); return m ? m[1] : ''; } catch (_) { return ''; } }
function demoUsageRefresh(st) {
  if (!st || !st.usage || typeof st.usage !== 'object') return st;
  const flag = demoRefreshFlag();
  const now = Date.now() / 1000;
  let at = flag === 'stale' ? now - 600 : Date.parse(st.usage.at) / 1000;
  let cache = false;
  if (demoRefresh.tap) {
    if (flag === 'stuck' || now - demoRefresh.tap < DEMO_REFRESH_HOLD) at = demoRefresh.base;
    else { at = demoRefresh.tap + DEMO_REFRESH_HOLD; cache = true; }
  }
  if (!Number.isFinite(at)) return st;
  demoRefresh.shown = at;
  const usage = { ...st.usage, at: new Date(at * 1000).toISOString(), value: { ...(st.usage.value || {}), ...(cache ? { source: 'cache' } : {}) } };
  if (flag !== 'none') return { ...st, usage };
  const notClaude = (list) => (Array.isArray(list) ? list.filter((x) => !x || x.agent !== 'claude') : list);
  const projects = (st.projects || []).map((p) => ({ ...p, root: p.root ? { ...p.root, sessions: notClaude(p.root.sessions) } : p.root,
    repos: (p.repos || []).map((r) => ({ ...r, sessions: notClaude(r.sessions) })), orphan_sessions: notClaude(p.orphan_sessions) }));
  return { ...st, usage, projects };
}

// Demo creations persist for this page and merge over the fixture.
const demoMade = { projects: [] };
function demoRepoName(url) {
  const tail = String(url || '').trim().replace(/\/+$/, '').split(/[/:]/).pop().replace(/\.git$/, '');
  return tail.replace(/[^A-Za-z0-9_-]+/g, '-').replace(/^[-_]+|[-_]+$/g, '').slice(0, 64);
}
function demoMakeRepo(project, name) {
  let p = demoMade.projects.find((x) => x.name === project);
  if (!p) { p = { linked: false, target: null, name: project, path: `/home/demo/projects/${project}`, root: null, orphan_sessions: [], repos: [] }; demoMade.projects.push(p); }
  if (name && !p.repos.some((r) => r.name === name)) p.repos.push({ linked: false, target: null, name, path: `${p.path}/${name}`, state: 'ok', branch: 'main', dirty: false, devcontainer: false, sessions: [] });
}
const demoNoAuto = {};                                 // #84: tmux name -> the Auto-continue switch as the visitor left it (the fixture has no board to store it)
function demoMake(method, path, body) {
  const b = body && typeof body === 'object' ? body : {};
  let m = null;
  if (method === 'POST' && (m = /^\/api\/sessions\/([^/]+)\/flags$/.exec(path)) && typeof b.no_autoresume === 'boolean') {
    demoNoAuto[decodeURIComponent(m[1])] = b.no_autoresume;
  } else if (method === 'POST' && path === '/api/projects' && typeof b.name === 'string' && b.name) {
    demoMakeRepo(b.name, '');
    if (b.url) demoMakeRepo(b.name, demoRepoName(b.url));
    if (b.link) demoLinkFolder(b.name, null, b.link);
  } else if (method === 'POST' && (m = /^\/api\/projects\/([^/]+)\/repos(\/bulk)?$/.exec(path))) {
    const project = decodeURIComponent(m[1]);
    demoMakeRepo(project, '');
    if (m[2]) for (const r of Array.isArray(b.repos) ? b.repos : []) demoMakeRepo(project, (r && (r.name || demoRepoName(r.url))) || '');
    else {
      const name = b.name || demoRepoName(b.link || b.url);
      demoMakeRepo(project, name);
      if (b.link) demoLinkFolder(project, name, b.link);
    }
  }
}
/* ?taskepic=<name> (running waiting pr failed cancelled lost) lays a variant of the epic record (issue #149) over the demo's task 23: demo/task-epic.json. */
async function demoTaskEpic(st) {
  let q = '';
  try { q = location.search; } catch (_) { q = ''; }
  const m = /[?&]taskepic=([a-z]+)/.exec(q);
  if (!m || !st || !Array.isArray(st.tasks)) return st;
  const r = await fetch('/static/demo/task-epic.json');
  const fx = r.ok ? await r.json() : {};
  const v = m[1] !== '_note' && ownKey(fx, m[1]) ? fx[m[1]] : null;
  return v ? { ...st, tasks: st.tasks.map((t) => (t.id === 23 ? { ...t, ...(v.task || {}), epic: v.epic } : t)) } : st;
}

function demoMadeState(st) {
  if (st && Array.isArray(st.projects) && Object.keys(demoNoAuto).length) {
    const lay = (list) => (Array.isArray(list) ? list.map((x) => (ownKey(demoNoAuto, x.tmux) ? { ...x, flags: { ...(x.flags || {}), no_autoresume: demoNoAuto[x.tmux] || undefined } } : x)) : list);
    st = { ...st, projects: st.projects.map((p) => ({ ...p, root: p.root ? { ...p.root, sessions: lay(p.root.sessions) } : p.root, repos: (p.repos || []).map((r) => ({ ...r, sessions: lay(r.sessions) })) })) };
  }
  if (!demoMade.projects.length || !st || !Array.isArray(st.projects)) return st;
  const made = new Map(demoMade.projects.map((p) => [p.name, p]));
  const merged = st.projects.map((p) => (made.has(p.name) ? { ...p, repos: [...p.repos, ...made.get(p.name).repos.filter((r) => !p.repos.some((x) => x.name === r.name))] } : p));
  for (const p of demoMade.projects) if (!st.projects.some((x) => x.name === p.name)) merged.push(p);
  return { ...st, projects: merged };
}

// Demo memory failure variants.
function demoMemFlag() { try { const m = /[?&]mem=([a-z]+)/.exec(location.search); return m ? m[1] : ''; } catch (_) { return ''; } }
async function demoMemVariant(data0, bare) {
  const pm = /^\/api\/memory\/([^/]+)\//.exec(bare || '');
  let name = null;
  try { name = pm ? decodeURIComponent(pm[1]) : null; } catch (_) { name = null; }
  const data = name && data0 && typeof data0 === 'object' && typeof data0.project === 'string' ? { ...data0, project: name } : data0;   // every demo project answers the same rows, under its own name
  const flag = demoMemFlag();
  if (!flag) return data;
  const vname = 'memory_states';
  const r = await fetch(`/static/demo/${vname}.json`);
  const states = r.ok ? await r.json() : {};
  const v = flag !== '_note' && ownKey(states, flag) ? states[flag] : null;
  if (!v) return data;
  if (v.status !== 200) { const e = demoError(v.status, v.body && v.body.error); e.body = v.body; throw e; }
  return { ...data, ...v.body };
}

// demo/state.json must carry every key of the real /api/state: tests/test_api_v054.py (the demo state test) fails when build_state() gains one.
async function demoApi(method, path, body) {
  if (method === 'POST' && (path === '/api/preflight/folder' || body?.link)) {
    await Lazy.load('launcher');
    if (path === '/api/preflight/folder') return demoFolderPreflight(body);
  }
  if (/^\/api\/bare(\/|$)/.test(path.split('?')[0])) {
    if (typeof Lazy !== 'undefined') await Lazy.load('demobare');
    return demoBare(method, path, body); // demo/bare.json, entirely in memory
  }
  if (path === '/api/settings/options' && typeof demoSettingsOptions === 'function') {
    if (method === 'PUT') return demoSettingsOptions(body);
    if (settingsDemoOptions) return settingsDemoOptions;
  }
  if (path.split('?')[0] === '/api/route/auto' && typeof lxnDemoAuto === 'function') return lxnDemoAuto(path);          // the launcher's Auto (issue #150, launcher-node.js)
  if ((path.split('?')[0] === '/api/epics/new' || (method === 'POST' && /^\/api\/epics(\/preview)?$/.test(path.split('?')[0]))) && typeof demoEpicNew === 'function') return demoEpicNew(method, path, body);   // New epic (issue #158): played in pages/epic-new.js
  if (/^\/api\/(epics(\/|$)|nodes\/[^/]+\/epics\/(claim|work)$)/.test(path.split('?')[0]) && typeof demoEpics === 'function') return demoEpics(method, path, body);   // the epic view (issue #147, demo/epic.json): its answers and pins are played in pages/epic.js
  if (/^\/api\/epic-worker(\/|$)/.test(path.split('?')[0]) && typeof demoWorker === 'function') return demoWorker(method, path, body);   // the epic worker (issue #157, demo/worker.json): played in nodes-worker.js
  if (/^\/api\/team\//.test(path.split('?')[0]) && typeof demoTeam === 'function') return demoTeam(method, path, body);   // the Team view (issue #197, demo/team.json): played in team.js
  if (method !== 'GET') {
    await new Promise((resolve) => setTimeout(resolve, 150));
    demoMake(method, path, body);
    if (/^\/api\/nodes\/[^/]+\/(tasks|sessions|permissions)/.test(path) && typeof demoRelayWrite === 'function') { const made = demoRelayWrite(method, path, body, await demoNodesFixture()); if (made) return made; }   // a start on another node (issue #141): played here
    if (/^\/api\/nodes(\/|$)/.test(path) && typeof demoNodesWrite === 'function') { const made = demoNodesWrite(method, path, body, await demoNodesFixture()); if (made) return made; }   // pairing (issue #135): played here, nothing leaves the page
    if (path === '/api/usage/refresh') {
      if (demoRefreshFlag() === 'none') throw demoError(409, 'no Claude session is at its prompt; start one to refresh');
      demoRefresh.tap = Date.now() / 1000;
      demoRefresh.base = demoRefresh.shown;
      return { ok: true, session: 'ccboard--ccboard--s1', started_at: new Date().toISOString() };
    }
    return { ok: true };
  }
  const bare = path.split('?')[0];
  let name = null;
  if (bare === '/api/state') name = 'state';
  else if (bare === '/api/settings/options') name = 'options';
  else if (bare === '/api/node') name = 'node';   // this board's node card (GET /api/node, issue #133): the same shape the route answers
  else if (bare === '/api/nodes' || bare === '/api/nodes/pairs' || bare === '/api/nodes/audit' || bare === '/api/nodes/state' || bare === '/api/mcp/tokens' || /^\/api\/nodes\/[^/]+\/(agents|audit|permissions|sessions\/[^/]+\/pane)$/.test(bare)) name = 'nodes';   // Settings > Nodes > Paired nodes, Who can control this node and Activity (issue #135): one fixture, three answers
  else if (bare === '/api/nodes/auth') name = 'nodes-auth';   // Settings > Nodes > Device check (issue #136): one fixture, a state per ?authstate=
  else if (bare === '/api/nodes/discover') name = 'nodes-discover';   // Settings > Nodes > Found on your tailnet (issue #134): a found node, a refusing one, an offline one and one with nothing listening
  else if (/^\/api\/sessions\/[^/]+$/.test(bare)) name = 'session';    // the terminal page's own read: the row of state.json with that tmux name (its agent drives Tune and the quick replies)
  else if (bare === '/api/skills') name = 'skills';   // the palette's Skills group (issue #102): the same shape GET /api/skills answers
  else if (bare === '/api/agents') name = 'agents';   // the launcher's option schemas (v0.5.13); the same shape GET /api/agents answers
  else if (bare.startsWith('/api/search')) name = 'search';
  else if (/^\/api\/projects\/[^/]+\/repos\/[^/]+\/tree$/.test(bare)) name = 'tree';
  else if (/^\/api\/projects\/[^/]+\/repos\/[^/]+\/file$/.test(bare)) name = 'file';
  else if (bare.startsWith('/api/series/events')) name = 'series_events';   // before the '/api/series' prefix: the Gantt must not draw the limit series as sessions
  else if (bare.startsWith('/api/series')) name = 'series';
  else if (bare === '/api/usage/summary') name = 'usage_summary';
  else if (bare === '/api/memory/health') name = 'memory_health';   // the Memory proxy (v0.5.20, docs/memory-api.md): one fixture per route, the same for every project
  else if (bare === '/api/memory/prefs') name = 'memory_prefs';
  else if (/^\/api\/memory\/[^/]+\/observations$/.test(bare)) name = 'memory_observations';
  else if (/^\/api\/memory\/[^/]+\/summaries$/.test(bare)) name = 'memory_summaries';
  else if (/^\/api\/memory\/[^/]+\/search$/.test(bare)) name = 'memory_search';
  else if (/^\/api\/memory\/[^/]+\/timeline$/.test(bare)) name = 'memory_timeline';
  else if (/^\/api\/memory\/[^/]+\/palace$/.test(bare)) name = 'memory_palace';
  else if (bare.startsWith('/api/memory/')) name = 'memory';
  else if (/^\/api\/tasks\/[^/]+\/diff$/.test(bare)) name = 'diff';   // the Tasks card's Diff/PR sheet (v0.5.21): one made-up branch, two commits, three files
  else if (bare === '/api/doctor') name = 'doctor';       // the Settings > Doctor checklist (v0.5.19)
  else if (/^\/api\/projects\/[^/]+\/repos\/[^/]+\/issues(\/\d+)?$/.test(bare)) name = 'issues';   // the launcher's "from a GitHub issue" (v0.5.20): one made-up list and its details
  if (!name) return {};
  if (name === 'session') {
    let tmuxName = '';
    try { tmuxName = decodeURIComponent(bare.slice('/api/sessions/'.length)); } catch (_) { tmuxName = ''; }
    const rs = await fetch('/static/demo/state.json');
    if (!rs.ok) throw new Error(`demo fixture state.json: ${rs.status} ${rs.statusText}`);
    const st = demoRebase(await rs.json());
    for (const p of st.projects || []) for (const r of p.repos || []) for (const s of r.sessions || []) if (s && s.tmux === tmuxName) return { ...s, project: p.name, repo: r.name };
    throw demoError(404, 'no such session');
  }
  const r = await fetch(`/static/demo/${name}.json`);
  if (!r.ok) throw new Error(`demo fixture ${name}.json: ${r.status} ${r.statusText}`);
  const data = await r.json();
  if (name === 'issues') {
    const one = /\/issues\/(\d+)$/.exec(bare);
    if (!one) return { issues: data.issues };
    if (!ownKey(data.detail, one[1])) throw demoError(404, 'not found');
    return data.detail[one[1]];
  }
  if (name === 'usage_summary' && /[?&]basis=est(&|$)/.test(path)) return { ...demoRebase(data), basis: 'est' };   // the Estimated basis (issue #95): the demo has nothing to estimate, so the same numbers under the other name (rebased like the reported one, or the windows read as rolled over)
  if (name === 'nodes') return demoNodes(data, bare);
  if (name === 'nodes-discover') return demoDiscover(data, /[?&]refresh=1(&|$)/.test(path));
  if (name === 'nodes-auth') return typeof demoAuth === 'function' ? demoAuth(data, /[?&]check=1(&|$)/.test(path)) : data;   // played in nodes-pair.js (the settings bundle)
  if (name === 'tree' || name === 'file') return demoPick(name, bare, path.slice(bare.length + 1), data);
  if (/^memory_(observations|summaries|search|timeline|palace)$/.test(name)) return demoMemVariant(data, bare);
  if (name === 'series_events' && data && data.demo && data.demo.epoch && Array.isArray(data.events)) {   // keep the fixture's 24 h alive, like state.json
    const dt = Math.floor(Date.now() / 1000 - data.demo.epoch);
    return { ...data, events: data.events.map((e) => ({ ...e, t: e.t + dt })) };
  }
  if (name === 'series' && data && data.demo && data.demo.epoch && data.meta && typeof data.meta === 'object') {   // the window resets of the series follow the clock too: a Codex window that ends after the fixture's moment must not read as 'rolled over' now
    const dt = Math.floor(Date.now() / 1000 - data.demo.epoch);
    const meta = {};
    for (const [k, m] of Object.entries(data.meta)) meta[k] = m && typeof m.resets_at === 'number' ? { ...m, resets_at: m.resets_at + dt } : m;
    return { ...data, meta };
  }
  if (name === 'state') {
    if (typeof Lazy !== 'undefined') await Lazy.load('demobare');
    if (typeof demoBare === 'function') {
      data.bare_devices = (await demoBare('GET', '/api/bare')).devices;
      data.pending_permissions = [...(data.pending_permissions || []), ...demoBarePending(data.bare_devices)];
    }
    const st = demoTaskEpic(demoMadeState(demoUsageRefresh(demoRebase(data))));
    return typeof settingsRestartDemoState === 'function' ? settingsRestartDemoState(st) : st;
  }                 // the usage reading's own time follows the make-believe refresh above
  return name === 'usage_summary' ? demoRebase(data) : data;                         // both carry demo.epoch: the summary's reset times and ISO stamps ride along with the state's
}

