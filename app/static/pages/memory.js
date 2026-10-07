/* ccboard Memory page (v0.5.20, #/memory[/<project>][?tab=timeline|summaries|search&q=&type=&repo=&agent=&session=&range=]): what past sessions learned, read
   from claude-mem through the board's proxy (docs/memory-api.md). The browser never talks to the worker: every call here is GET /api/memory/<project>/...
   (palace, observations, summaries, search) or GET /api/memory/health, plus the one write the page owns: POST /api/sessions/{tmux}/prompt from a
   Timeline row's "Send to session..." menu (after a confirm step; nothing is sent when a session is picked).

   Header: a project select, the worker pill (from state.memory, the 3 s poll, and the health route), a search box (300 ms debounce, kept in ?q=), the
   optional "Open claude-mem viewer" link (state.config.mem_viewer_url, hidden with an install hint when null), the health tile, then the four tabs
   (Palace by default, Timeline, Summaries, Search) as tabs() and a filter bar. Filters (type, repo, agent, session, range) live in
   localStorage ccboard:memory:<project> (inside try/catch) and are mirrored in the hash query, so a link reproduces the view.

   The pieces other screens reuse are top-level functions: memoryPalaceView (the Palace tab and the project page's Memory tab), memoryHealthTile (this page,
   Usage and Settings), memoryGotchasMount / memoryGotchasStrip (the project header and the launcher sheet), memEnvNotes / memDegradedNode (the states).

   Everything is text: titles, narratives, facts, tags and paths go through textContent (the narrative with white-space: pre-wrap), the page builds nodes with
   el() only, and writes no style of any kind (the stacked bar is a ladder of width classes). Definition only at load, apart from the registerPage call. */
'use strict';

const MEM_TABS = [['palace', 'Palace'], ['timeline', 'Timeline'], ['summaries', 'Summaries'], ['search', 'Search']];
const MEM_RANGES = [['24h', '24 h'], ['7d', '7 d'], ['30d', '30 d'], ['all', 'All']];
const MEM_RANGE_MS = { '24h': 86400000, '7d': 7 * 86400000, '30d': 30 * 86400000 };
const MEM_AGENTS = [['', 'Any'], ['claude', 'Claude'], ['codex', 'Codex']];
const MEM_KNOWN_TYPES = ['bugfix', 'feature', 'refactor', 'change', 'discovery', 'decision'];
const MEM_PAGE = 30;                                  // rows per Timeline / Summaries page
const MEM_Q_MAX = 500;
const MEM_DEBOUNCE = 300;
const MEM_STORE = (project) => `ccboard:memory:${project}`;
const MEM_LAST = 'ccboard:memory-last';               // the project the page opened with last
const MEM_PROMPT_MAX = 4000;                          // chars of a note pasted into a session
const MEM_VIEWER_WARNING = 'every device on your tailnet can open and change the claude-mem worker';

const Memory = { cur: null, ids: 0 };

/* ---------- pure helpers (tested without a page) ---------- */

function memStoreGet(key) { try { return localStorage.getItem(key); } catch (_) { return null; } }
function memStorePut(key, value) { try { localStorage.setItem(key, value); } catch (_) { /* storage may be unavailable */ } }

/* Filters from anything: junk is ignored, so a hand-edited address or a stale stored value never reaches the proxy. */
function memFilterClean(raw) {
  const f = { type: '', repo: '', agent: '', session: '', range: '' };
  if (!raw || typeof raw !== 'object') return f;
  const s = (v) => (typeof v === 'string' ? v : '');
  if (/^[a-z][a-z-]{0,23}(,[a-z][a-z-]{0,23}){0,7}$/.test(s(raw.type))) f.type = raw.type;
  if (/^[A-Za-z0-9_.-]{1,80}$/.test(s(raw.repo))) f.repo = raw.repo;
  if (raw.agent === 'claude' || raw.agent === 'codex') f.agent = raw.agent;
  if (/^[A-Za-z0-9_-]{6,64}$/.test(s(raw.session))) f.session = raw.session;
  if (['24h', '7d', '30d', 'all'].includes(raw.range)) f.range = raw.range;
  return f;
}

function memStoredFilters(project) {
  const raw = memStoreGet(MEM_STORE(project));
  if (!raw) return memFilterClean(null);
  try { return memFilterClean(JSON.parse(raw)); } catch (_) { return memFilterClean(null); }
}

function memSaveFilters(project, f) { memStorePut(MEM_STORE(project), JSON.stringify(memFilterClean(f))); }

/* The address wins over what was stored, key by key; a bad value in the address falls back to the stored one. */
function memFiltersFrom(query, stored) {
  const base = memFilterClean(stored);
  const fromUrl = memFilterClean(query);
  const out = { ...base };
  for (const k of Object.keys(out)) if (query && typeof query === 'object' && Object.prototype.hasOwnProperty.call(query, k) && fromUrl[k]) out[k] = fromUrl[k];
  return out;
}

function memHasFilters(f) { return !!(f && (f.type || f.repo || f.agent || f.session || (f.range && f.range !== 'all'))); }

/* What the proxy is asked: only the filters that are set; the range becomes `since` in epoch milliseconds. */
function memFilterParams(f, now) {
  const out = {};
  if (!f) return out;
  if (f.type) out.type = f.type;
  if (f.repo) out.repo = f.repo;
  if (f.agent) out.agent = f.agent;
  if (f.session) out.session = f.session;
  if (f.range && Object.prototype.hasOwnProperty.call(MEM_RANGE_MS, f.range)) out.since = String((typeof now === 'number' ? now : Date.now()) - MEM_RANGE_MS[f.range]);
  return out;
}

/* The hash query that reproduces a view: the tab (not Palace), ?q=, and every filter that is set. */
function memHashQuery(tab, q, f) {
  const out = {};
  if (tab && tab !== 'palace') out.tab = tab;
  if (q) out.q = q;
  for (const k of ['type', 'repo', 'agent', 'session']) if (f && f[k]) out[k] = f[k];
  if (f && f.range) out.range = f.range;
  return out;
}

/* The projects the page can open: a name the router cannot put in an address (#/memory/<project> takes letters, digits, _ and -) is not offered. */
function memProjectNames(st) { return ((st && st.projects) || []).map((p) => p.name).filter((n) => typeof n === 'string' && /^[A-Za-z0-9_-]+$/.test(n)); }

/* What the route says. The project is the route's, else the last one opened, else the first the board has. */
function memParseRoute(route, st) {
  const p = (route && route.params) || {};
  const q = (route && route.query) || {};
  const names = memProjectNames(st);
  let project = p.project || '';
  let auto = false;
  if (!project) {
    const last = memStoreGet(MEM_LAST);
    auto = !(last && names.includes(last)) && names.length > 0;       // no route project, nothing remembered: the page looks for the first project that has memory
    project = last && names.includes(last) ? last : (names[0] || '');
  }
  const text = typeof q.q === 'string' ? q.q.slice(0, MEM_Q_MAX) : '';
  const tab = MEM_TABS.some((t) => t[0] === q.tab) ? q.tab : (text.trim() ? 'search' : 'palace');
  return { project, auto, tab, q: text, filters: memFiltersFrom(q, project ? memStoredFilters(project) : null) };
}

function memUrl(project, what, params) {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params || {})) if (v !== null && v !== undefined && v !== '') qs.set(k, String(v));
  const s = qs.toString();
  return `/api/memory/${encodeURIComponent(project)}/${what}${s ? '?' + s : ''}`;
}

function memGet(project, what, params) { return api('GET', memUrl(project, what, params)); }

function memTypeClass(t) { return MEM_KNOWN_TYPES.includes(t) ? t : 'other'; }
function memNum(n) { return typeof n === 'number' && Number.isFinite(n) ? n : null; }
function memCount(n) { return memNum(n) === null ? 'unknown' : Math.round(n).toLocaleString('en-US'); }
function memOneLine(text, max) {
  const t = String(text === null || text === undefined ? '' : text).replace(/\s+/g, ' ').trim();
  return t.length > max ? t.slice(0, max - 1) + '…' : t;
}

function memClock(v) {
  const ms = typeof v === 'number' ? v : Date.parse(v);
  if (!Number.isFinite(ms)) return '';
  const d = new Date(ms);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

function memDayKey(ms) { const d = new Date(ms); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`; }

function memDayLabel(ms) {
  const key = memDayKey(ms);
  const now = Date.now();
  if (key === memDayKey(now)) return 'Today';
  if (key === memDayKey(now - 86400000)) return 'Yesterday';
  try { return new Date(ms).toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' }); } catch (_) { return key; }
}

function memAgo(ms) { const a = memNum(ms) === null ? '' : fmtAge(ms / 1000); return a ? `${a} ago` : ''; }

/* The rows of a list grouped by local day, in the order given. */
function memGroupDays(items) {
  const out = [];
  for (const it of items) {
    const ms = memNum(it && it.created_at_epoch);
    const key = ms === null ? 'unknown' : memDayKey(ms);
    const last = out[out.length - 1];
    if (last && last.key === key) last.rows.push(it); else out.push({ key, ms, rows: [it] });
  }
  return out;
}

/* A bar segment's width in 5 % steps (a ladder of classes: no style is ever written). */
function memStep(frac) { return Math.max(5, Math.min(100, Math.round(frac * 20) * 5)); }

/* Files of an item, read and modified, once each. */
function memFilesOf(o) {
  if (!o) return [];
  const all = Array.isArray(o.files) ? o.files : [...(o.files_modified || []), ...(o.files_read || [])];
  return [...new Set(all.filter((f) => typeof f === 'string' && f))];
}

/* A file path inside the repo of the wing, as an absolute path, or null: a path that climbs out with .. or an absolute path elsewhere never links. */
function memResolveFile(path, repoPath) {
  if (!repoPath || typeof path !== 'string' || !path || path.indexOf('\0') >= 0) return null;
  if (path.split('/').includes('..')) return null;
  const base = String(repoPath).replace(/\/+$/, '');
  if (path.charAt(0) === '/') return path.startsWith(base + '/') ? path : null;
  return `${base}/${path.replace(/^\.\//, '')}`;
}

function memRepoPath(st, project, repoName) {
  const p = ((st && st.projects) || []).find((x) => x.name === project);
  if (!p) return '';
  if (!repoName || repoName === 'root') return (p.root && p.root.path) || p.path || '';
  const r = (p.repos || []).find((x) => x.name === repoName);
  return (r && r.path) || '';
}

/* Idle sessions of a project that can take a pasted prompt (an agent, not a plain shell). */
function memIdleSessions(st, project) {
  const p = ((st && st.projects) || []).find((x) => x.name === project);
  if (!p) return [];
  const out = [];
  const add = (repo, label) => {
    for (const s of (repo && repo.sessions) || []) {
      const agent = typeof sessionAgent === 'function' ? sessionAgent(s) : (s.agent || 'claude');
      if (s.state === 'idle' && agent !== 'shell' && s.tmux) out.push({ tmux: s.tmux, label: `${label} / ${s.name || s.tmux}`, agent });
    }
  };
  if (p.root) add(p.root, 'project folder');
  for (const r of (p.repos || [])) add(r, r.name);
  return out;
}

/* The note pasted into a session: what claude-mem kept, as plain text, cut to a size the prompt route takes. */
function memPromptFor(o) {
  const bits = [`Context from claude-mem (observation ${o.id}, ${o.type || 'note'}): ${o.title || ''}`];
  if (o.subtitle) bits.push(o.subtitle);
  if (o.narrative) bits.push('', o.narrative);
  if (Array.isArray(o.facts) && o.facts.length) bits.push('', 'Facts:', ...o.facts.map((f) => `- ${f}`));
  const files = memFilesOf(o);
  if (files.length) bits.push('', `Files: ${files.join(', ')}`);
  const text = bits.join('\n');
  return text.length > MEM_PROMPT_MAX ? text.slice(0, MEM_PROMPT_MAX - 1) + '…' : text;
}

/* An error thrown by api() as the envelope the notes and the degraded panel read: the proxy's 503 body when there is one, else what the failure said. */
function memErrorEnv(e) {
  const b = e && e.body && typeof e.body === 'object' ? e.body : {};
  const offline = !e || e.status === undefined;
  return {
    state: typeof b.state === 'string' ? b.state : (offline ? 'offline' : 'degraded'), up: false,
    reason: String(b.reason || b.error || (e && e.message) || 'no answer'), reason_code: b.reason_code || (offline ? 'offline' : 'http'),
    compat: b.compat || null, worker_version: b.worker_version || null, tested_worker: b.tested_worker || null, status: e && e.status,
  };
}

/* The quiet lines under the header for an answer: each has its own glyph and words, never colour alone. */
function memEnvNotes(env) {
  const out = [];
  if (!env || typeof env !== 'object') return out;
  const reason = typeof env.reason === 'string' && env.reason ? env.reason : '';
  if (env.state === 'incompatible' || env.shape_error) out.push({ id: 'incompatible', kind: 'warn', glyph: '⚠', text: `The Memory page may be wrong until the board is updated; report it.${reason ? ' ' + reason : ''}` });
  if (env.stale) out.push({ id: 'stale', kind: 'warn', glyph: '✻', text: `Showing data from ${memClock(env.stale_at) || 'earlier'}.${reason ? ' ' + reason : ''}`, retry: true });
  if (env.compat === 'untested') out.push({ id: 'untested', kind: 'quiet', glyph: '○', text: `${env.worker_version ? 'claude-mem ' + env.worker_version : 'This claude-mem'} is newer than the one this board was tested with (${env.tested_worker || 'unknown'}); the Memory page may be off.` });
  else if (env.compat === 'unknown' && env.worker_version) out.push({ id: 'unknown-version', kind: 'quiet', glyph: '○', text: `This board has not been tested with claude-mem ${env.worker_version}; the Memory page may be off.` });
  if (Array.isArray(env.partial) && env.partial.length) out.push({ id: 'partial', kind: 'quiet', glyph: '○', text: `Some of this project's memory did not answer: ${env.partial.join(', ')}. The rest is shown.` });
  if (Array.isArray(env.ambiguous) && env.ambiguous.length) {
    const keys = env.ambiguous.map((a) => a && a.key).filter(Boolean).join(', ');
    out.push({ id: 'ambiguous', kind: 'quiet', glyph: '○', text: `Another project shares the folder name ${keys}: its rows are filtered by file path, best effort.` });
  }
  return out;
}

/* The worker pill: what the monitor last saw, or "stale" when the answer on screen is an old one. */
function memPillInfo(h, env) {
  if (env && env.stale) return { cls: 'stale', glyph: '✻', text: `stale ${memClock(env.stale_at) || ''}`.trim(), title: env.reason || 'showing the last good answer' };
  if (!h || typeof h !== 'object') return { cls: 'unknown', glyph: '○', text: 'checking', title: 'the board has not asked the worker yet' };
  const s = h.state;
  if (s === 'up') {
    const v = h.version ? `v${h.version}` : 'version unknown';
    return { cls: 'up', glyph: '✓', text: `up ${v} · ${memCount(h.observations)} obs`, title: h.reason || 'the claude-mem worker answers' };
  }
  if (s === 'degraded') return { cls: 'degraded', glyph: '✻', text: `degraded${h.reason ? ' · ' + memOneLine(h.reason, 80) : ''}`, title: h.reason || 'the worker answers badly' };
  if (s === 'down') return { cls: 'down', glyph: '✕', text: `down${h.reason ? ' · ' + memOneLine(h.reason, 80) : ''}`, title: h.reason || 'the worker does not answer' };
  if (s === 'incompatible') return { cls: 'degraded', glyph: '⚠', text: `incompatible${h.reason ? ' · ' + memOneLine(h.reason, 80) : ''}`, title: h.reason || 'the worker answered in a shape this board does not know' };
  if (s === 'off') return { cls: 'off', glyph: '○', text: 'off', title: 'claude-mem is turned off on this box' };
  return { cls: 'unknown', glyph: '○', text: 'checking', title: h.reason || 'the board has not asked the worker yet' };
}

/* ---------- small builders ---------- */

function memSeg(items, value, onPick, label) {
  const node = el('div', { class: 'seg-ctl mem-seg', role: 'group', 'aria-label': label });
  const btns = new Map();
  let cur = value;
  const set = (v, focus) => {
    cur = v;
    for (const [k, b] of btns) b.setAttribute('aria-pressed', k === cur ? 'true' : 'false');
    if (focus) btns.get(v).focus();
    onPick(v);
  };
  for (const [v, text] of items) {
    btns.set(v, el('button', { class: 'seg-btn', type: 'button', 'data-v': v, 'aria-pressed': v === cur ? 'true' : 'false', text, onclick: () => set(v), onkeydown: (e) => {
      const i = items.findIndex(([x]) => x === cur);
      const to = e.key === 'ArrowRight' ? items[(i + 1) % items.length][0] : e.key === 'ArrowLeft' ? items[(i + items.length - 1) % items.length][0] : null;
      if (to === null) return;
      e.preventDefault();
      set(to, true);
    } }));
    node.append(btns.get(v));
  }
  node.setValue = (v) => { cur = v; for (const [k, b] of btns) b.setAttribute('aria-pressed', k === cur ? 'true' : 'false'); };
  return node;
}

function memTypeTag(t) {
  const type = typeof t === 'string' && t ? t : 'note';
  return el('span', { class: `mem-type mem-type-${memTypeClass(type)}`, text: type });
}

function memFileChip(path, repoName, project) {
  const st = typeof state !== 'undefined' ? state : null;
  const repoPath = memRepoPath(st, project, repoName);
  const abs = memResolveFile(path, repoPath);
  if (abs && st && st.config && st.config.code_https_port && typeof codeServerFileUrl === 'function') {
    return el('a', { class: 'mem-file mono', href: codeServerFileUrl(abs, null, repoPath), target: '_blank', rel: 'noopener', title: `open ${path} in code-server`, text: path });
  }
  return el('span', { class: 'mem-file mono', title: path, text: path });
}

/* The expanded part of an observation or a drawer: narrative (pre-wrap text), facts, concept tags and file chips. */
function memItemBody(o, ctx) {
  const body = el('div', { class: 'mem-body' });
  if (o.narrative) body.append(el('p', { class: 'mem-narr', text: o.narrative }));
  if (Array.isArray(o.facts) && o.facts.length) body.append(el('ul', { class: 'mem-facts' }, ...o.facts.map((f) => el('li', { class: 'mem-fact', text: String(f) }))));
  if (Array.isArray(o.concepts) && o.concepts.length) body.append(el('div', { class: 'mem-tags', 'aria-label': 'Concepts' }, ...o.concepts.map((c) => el('span', { class: 'mem-tag', text: String(c) }))));
  const files = memFilesOf(o);
  if (files.length) body.append(el('div', { class: 'mem-files', 'aria-label': 'Files' }, ...files.map((f) => memFileChip(f, (ctx && ctx.repo) || o.repo || '', ctx && ctx.project))));
  if (!body.children.length) body.append(el('p', { class: 'dim mem-nodetail', text: 'No more detail was kept for this one.' }));
  return body;
}

/* One observation or drawer: a button head (type tag, title, subtitle) that expands the body in place. ctx: {project, repo, open: Set, meta, send}. */
function memItem(o, ctx) {
  const c = ctx || {};
  const body = memItemBody(o, c);
  const isOpen = !!(c.open && c.open.has(o.id));
  body.classList.toggle('hidden', !isOpen);
  const head = el('button', { class: 'mem-item-head mem-row', type: 'button', 'aria-expanded': isOpen ? 'true' : 'false', onclick: () => {
    const open = head.getAttribute('aria-expanded') !== 'true';
    head.setAttribute('aria-expanded', open ? 'true' : 'false');
    body.classList.toggle('hidden', !open);
    if (c.open) { if (open) c.open.add(o.id); else c.open.delete(o.id); }
  } }, memTypeTag(o.type), el('span', { class: 'mem-title', text: o.title || '(untitled)' }), o.subtitle ? el('span', { class: 'mem-sub dim', text: o.subtitle }) : null);
  const node = el('article', { class: 'mem-item', 'data-id': String(o.id) }, head);
  if (c.meta) node.append(memItemMeta(o, c, node));
  else if (memNum(o.created_at_epoch) !== null) node.append(el('div', { class: 'mem-meta' }, el('span', { class: 'mem-time dim', title: new Date(o.created_at_epoch).toISOString(), text: memAgo(o.created_at_epoch) })));
  node.append(body);
  return node;
}

function memItemMeta(o, c, node) {
  const meta = el('div', { class: 'mem-meta' });
  const ms = memNum(o.created_at_epoch);
  if (ms !== null) meta.append(el('span', { class: 'mem-time mono', title: new Date(ms).toISOString(), text: memClock(ms) }));
  if (o.repo) meta.append(el('span', { class: 'mem-repo mono', title: o.key && o.key !== o.repo ? `claude-mem key ${o.key}` : null, text: o.repo }));
  if (o.platform_source === 'claude' || o.platform_source === 'codex') meta.append(agentGlyph(o.platform_source));
  const sid = o.content_session_id || o.memory_session_id;
  if (sid && typeof c.onSession === 'function') {
    meta.append(el('button', { class: 'chip-btn mem-sess mono', type: 'button', title: 'Show only this session', 'aria-label': `Show only session ${String(sid).slice(0, 8)}`, text: String(sid).slice(0, 8), onclick: () => c.onSession(sid) }));
  }
  const tokens = memNum(o.discovery_tokens);
  if (tokens !== null) meta.append(el('span', { class: 'mem-tok dim', title: 'tokens spent discovering this', text: `${memCount(tokens)} tok` }));
  if (c.send) meta.append(memSendButton(o, c, node));
  return meta;
}

/* Send to session...: a menu of the project's idle sessions. Choosing one only opens a confirm panel with the text; Send in the panel is what pastes it. */
function memSendButton(o, c, node) {
  const btn = el('button', { class: 'minimal small mem-send', type: 'button', text: 'Send to session…', title: 'Paste this note into an idle session of this project' });
  menu(btn, () => {
    const idle = memIdleSessions(typeof state !== 'undefined' ? state : null, c.project);
    if (!idle.length) return [{ label: 'No idle session in this project', icon: 'info-sign' }];
    return idle.map((s) => ({ label: s.label, icon: 'send-message', onClick: () => memSendPanel(o, s, node) }));
  });
  return btn;
}

function memSendPanel(o, sess, node) {
  const old = node.querySelector('.mem-send-panel');
  if (old) old.remove();
  const status = el('div', { class: 'mem-send-status dim', role: 'status', 'aria-live': 'polite' });
  const text = memPromptFor(o);
  let busy = false;
  const send = el('button', { class: 'small', type: 'button', text: 'Send', onclick: async () => {
    if (busy) return;
    busy = true;
    send.disabled = true;
    status.classList.remove('bad');
    status.textContent = 'Sending…';
    try {
      await api('POST', `/api/sessions/${encodeURIComponent(sess.tmux)}/prompt`, { text });
      status.textContent = `Sent to ${sess.label}. The session may not have acted on it yet.`;
      if (typeof pageToast === 'function') pageToast(`sent to ${sess.label}`, 'ok');
      send.classList.add('hidden');
    } catch (e) {
      status.textContent = (e && e.body && e.body.error) || (e && e.message) || 'The board could not send it.';
      status.classList.add('bad');
      send.disabled = false;
    }
    busy = false;
  } });
  const panel = el('div', { class: 'mem-send-panel', role: 'group', 'aria-label': 'Send to session' },
    el('div', { class: 'mem-k', text: `Paste into ${sess.label}?` }), el('pre', { class: 'mem-send-text', text }),
    el('div', { class: 'mem-send-actions' }, send, el('button', { class: 'minimal small', type: 'button', text: 'Cancel', onclick: () => panel.remove() })), status);
  node.append(panel);
  return panel;
}

/* ---------- states ---------- */

function memNotesNode(notes, retry) {
  const host = el('div', { class: 'mem-notes' });
  for (const n of notes) {
    host.append(el('div', { class: `mem-note ${n.kind}`, 'data-note': n.id, role: n.kind === 'warn' ? 'status' : null },
      el('span', { class: 'mem-note-g', 'aria-hidden': 'true', text: n.glyph }), el('span', { class: 'mem-note-t', text: n.text }),
      n.retry && typeof retry === 'function' ? el('button', { class: 'minimal small mem-retry', type: 'button', text: 'Retry', title: 'Ask the worker again', onclick: retry }) : null));
  }
  return host;
}

/* The page when the worker gave nothing: the real reason, and Retry saying what it re-asks. */
function memDegradedNode(env, retry, what) {
  const e = env || memErrorEnv(null);
  const off = e.state === 'off';
  const title = off ? 'claude-mem is turned off on this box'
    : e.state === 'offline' ? 'The board cannot be reached, retrying'
      : e.state === 'down' ? 'claude-mem is not running'
        : e.reason_code === 'timeout' ? 'claude-mem is slow'
          : e.state === 'incompatible' ? 'claude-mem answered in a shape this board does not know' : 'claude-mem could not answer';
  const node = el('div', { class: 'mem-degraded', role: 'alert', 'data-state': e.state },
    el('span', { class: 'mem-degraded-g', 'aria-hidden': 'true', text: e.state === 'down' || off ? '✕' : '✻' }),
    el('div', { class: 'mem-degraded-b' }, el('b', { text: title }), el('p', { class: 'mem-reason', text: e.reason || 'no reason was given' }),
      off ? null : el('button', { class: 'primary', type: 'button', text: 'Retry', title: `Ask the worker again for ${what || 'this view'}`, onclick: () => { if (typeof retry === 'function') retry(); } }),
      off ? null : el('p', { class: 'dim mem-retry-note', text: `Retry asks the worker again for ${what || 'this view'}.` })));
  return node;
}

function memSkeleton() {
  return typeof Pages !== 'undefined' && Pages && typeof Pages.skeleton === 'function' ? Pages.skeleton(3) : el('div', { class: 'dim', text: 'Loading…' });
}

function memEmptyNoMemory(project) {
  return pageEmpty('database', `No memory for ${project || 'this project'} yet`, `Next step: run a session in one of ${project ? project + '\'s' : 'its'} repos. claude-mem records what it learns there and it shows up here.`);
}

/* ---------- the palace ---------- */

function memBarNode(types, total) {
  const entries = Object.entries(types || {}).filter(([, n]) => memNum(n) !== null && n > 0).sort((a, b) => b[1] - a[1]);
  const sum = entries.reduce((n, [, c]) => n + c, 0) || total || 1;
  const label = entries.map(([t, n]) => `${t} ${n}`).join(', ');
  const bar = el('div', { class: 'mem-bar', role: 'img', 'aria-label': `Observations by type: ${label || 'none'}` });
  entries.forEach(([t, n], i) => bar.append(el('span', { class: `mem-seg mem-type-${memTypeClass(t)} ${i === entries.length - 1 ? 'mem-seg-rest' : 'mem-w' + memStep(n / sum)}` })));
  const legend = el('ul', { class: 'mem-legend' }, ...entries.map(([t, n]) => el('li', {}, el('span', { class: `mem-swatch mem-type-${memTypeClass(t)}`, 'aria-hidden': 'true' }), `${t} ${n}`)));
  return el('div', { class: 'mem-bargroup' }, bar, legend);
}

/* One wing (a repo of the project): its type bar, room chips (they filter the drawers shown) and the drawers. */
function memWingCard(w, ctx) {
  let room = null;
  const rooms = el('div', { class: 'mem-rooms', role: 'group', 'aria-label': `Rooms in ${w.name}` });
  const drawers = el('div', { class: 'mem-drawers' });
  const fits = (d) => !room || (room.kind === 'type' ? d.type === room.name : (d.concepts || []).includes(room.name));
  const paint = () => {
    for (const b of rooms.children) b.setAttribute('aria-pressed', room && b.getAttribute('data-room') === `${room.kind}:${room.name}` ? 'true' : 'false');
    drawers.textContent = '';
    const list = (w.drawers || []).filter(fits);
    for (const d of list) drawers.append(memItem(d, { ...ctx, repo: w.name, meta: false }));
    if (!list.length) drawers.append(el('p', { class: 'dim mem-nodrawer', text: room ? `None of the ${(w.drawers || []).length} newest drawers is in ${room.name}. Pick the room again to clear it.` : 'No drawers were kept for this wing.' }));
  };
  for (const r of (w.rooms || [])) {
    rooms.append(el('button', { class: 'chip-btn mem-room', type: 'button', 'data-room': `${r.kind}:${r.name}`, 'aria-pressed': 'false', title: `${r.kind === 'type' ? 'Type' : 'Concept'} ${r.name}`, onclick: () => {
      room = room && room.name === r.name && room.kind === r.kind ? null : r;
      paint();
    } }, r.name, el('span', { class: 'mem-room-n mono', text: String(r.count) })));
  }
  paint();
  const keys = Array.isArray(w.keys) && w.keys.length > 1 ? el('span', { class: 'mem-wing-keys dim mono', title: 'claude-mem keys folded into this wing', text: w.keys.join(', ') }) : null;
  return el('section', { class: 'mem-wing', 'data-wing': w.name },
    el('div', { class: 'mem-wing-head' }, el('h3', { class: 'mem-wing-name mono', text: w.name }), el('span', { class: 'mem-wing-n dim', text: `${w.total} observation${w.total === 1 ? '' : 's'}` }), keys),
    memBarNode(w.types, w.total), el('div', { class: 'mem-k', text: 'Rooms' }), rooms, el('div', { class: 'mem-k', text: 'Newest drawers' }), drawers);
}

/* The palace of a project as nodes: counts line, then a card per wing. repo limits it to one wing. Reused by the Memory page and the project page. */
function memPalaceRender(d, ctx) {
  const host = el('div', { class: 'mem-palace' });
  if (!d || d.state === 'incompatible' || d.shape_error) return host;
  const wings = (d.wings || []).filter((w) => !ctx.repo || w.name === ctx.repo);
  if (!wings.length) {
    host.append(d.keys_checked && !(d.keys || []).length ? memEmptyNoMemory(ctx.project)
      : pageEmpty('database', ctx.repo ? `Nothing in ${ctx.repo} yet` : `No memory for ${ctx.project || 'this project'} yet`, ctx.repo ? 'Clear the repo filter to see the other wings.' : `Next step: run a session in one of ${ctx.project ? ctx.project + '\'s' : 'its'} repos; claude-mem records them while they run.`));
    return host;
  }
  const bits = [`${d.total} observation${d.total === 1 ? '' : 's'}${d.truncated ? ` (of the newest ${d.scanned})` : ''}`, `${wings.length} wing${wings.length === 1 ? '' : 's'}`];
  if (memNum(d.summaries) !== null) bits.push(`${memCount(d.summaries)} summaries on the whole worker`);
  if (d.subagent_filter === 'unavailable') bits.push('subagent notes cannot be told apart here');
  else if (memNum(d.excluded_subagents) && d.excluded_subagents > 0) bits.push(`${d.excluded_subagents} subagent note${d.excluded_subagents === 1 ? '' : 's'} left out`);
  host.append(el('p', { class: 'mem-counts dim', text: bits.join(' · ') }));
  for (const w of wings) host.append(memWingCard(w, ctx));
  return host;
}

/* The Palace tab as a self-contained view: fetches /palace, keeps the last good answer, shows the states. o: {repo, onData}. */
function memoryPalaceView(project, o) {
  const opt = o || {};
  const notes = el('div', { class: 'mem-notes-host' });
  const body = el('div', { class: 'mem-palace-host' }, memSkeleton());
  const node = el('div', { class: 'mem-palace-view', 'data-project': project }, notes, body);
  const open = new Set();
  let seq = 0;
  let dead = false;
  let last = null;
  const view = { node, open };
  const paint = (d, errEnv, at) => {
    notes.textContent = '';
    const env = errEnv ? { ...d, stale: true, stale_at: at, reason: errEnv.reason } : d;
    notes.append(memNotesNode(memEnvNotes(env), () => view.load()));
    body.textContent = '';
    body.append(memPalaceRender(d, { project, repo: opt.repo || '', open }));
  };
  view.load = async () => {
    const my = ++seq;
    if (!last) { body.textContent = ''; body.append(memSkeleton()); }
    try {
      const d = await memGet(project, 'palace', {});
      if (dead || my !== seq) return d;
      last = { d, at: Date.now() };
      paint(d, null, 0);
      if (typeof opt.onData === 'function') opt.onData(d);
      return d;
    } catch (e) {
      if (dead || my !== seq) return null;
      const env = memErrorEnv(e);
      if (typeof opt.onData === 'function') opt.onData(env);
      if (last) { paint(last.d, env, last.at); return last.d; }
      notes.textContent = '';
      body.textContent = '';
      body.append(memDegradedNode(env, () => view.load(), 'the palace'));
      return null;
    }
  };
  view.setRepo = (repo) => { opt.repo = repo || ''; if (last) paint(last.d, null, 0); };
  view.destroy = () => { dead = true; seq += 1; };
  return view;
}

/* ---------- gotchas strip (project header and launcher sheet) ---------- */

/* The strip from a palace answer, or null when there is nothing honest to show: no gotchas, or an answer that is stale, partial or not ok. */
function memoryGotchasStrip(d, o) {
  const opt = o || {};
  if (!d || d.state !== 'ok' || d.stale || !Array.isArray(d.gotchas) || !d.gotchas.length) return null;
  const list = d.gotchas.slice(0, 3);
  const panel = el('div', { class: 'mem-gotcha-panel hidden' });
  const title = el('div', { class: 'mem-k mem-gotchas-k', text: 'Newest gotchas in this project, from claude-mem' });
  const rows = el('div', { class: 'mem-gotcha-rows' });
  const node = el('div', { class: 'mem-gotchas', 'data-count': String(list.length), 'data-open': narrowViewport() ? 'false' : 'true' }, title, rows, panel);
  let openId = null;
  let opener = null;
  const closePanel = () => { panel.classList.add('hidden'); panel.textContent = ''; openId = null; };
  const show = (g, btn) => {
    if (typeof coarsePointer === 'function' && coarsePointer() && typeof openSheet === 'function') {
      opener = btn;
      openSheet({ title: g.title || 'Gotcha', body: [el('div', { class: 'mem-sheet-meta' }, memTypeTag(g.type), el('span', { class: 'dim', text: memAgo(g.created_at_epoch) })), memItemBody(g, { project: opt.project, repo: opt.repo })],
        onClose: () => { if (opener && typeof opener.focus === 'function') opener.focus(); opener = null; } });
      return;
    }
    if (openId === g.id) { closePanel(); return; }
    openId = g.id;
    panel.textContent = '';
    panel.append(el('div', { class: 'mem-sheet-meta' }, memTypeTag(g.type), el('span', { class: 'dim', text: memAgo(g.created_at_epoch) })), memItemBody(g, { project: opt.project, repo: opt.repo }));
    panel.classList.remove('hidden');
  };
  list.forEach((g) => {
    const btn = el('button', { class: 'mem-gotcha mem-row', type: 'button', 'data-id': String(g.id), title: g.subtitle ? `${g.title}: ${g.subtitle}` : g.title, onclick: () => show(g, btn) },
      el('span', { class: 'mem-gotcha-g', 'aria-hidden': 'true', text: '⚠' }), el('span', { class: 'mem-gotcha-t', text: g.title || '(untitled)' }),
      g.subtitle ? el('span', { class: 'mem-gotcha-s dim', text: g.subtitle }) : null, el('span', { class: 'mem-gotcha-a dim mono', text: memAgo(g.created_at_epoch) }));
    rows.append(btn);
  });
  if (list.length > 1) {
    const more = el('button', { class: 'minimal small mem-gotcha-more', type: 'button', 'aria-expanded': node.getAttribute('data-open') === 'true' ? 'true' : 'false', text: `${list.length - 1} more`, onclick: () => {
      const on = node.getAttribute('data-open') !== 'true';
      node.setAttribute('data-open', on ? 'true' : 'false');
      more.setAttribute('aria-expanded', on ? 'true' : 'false');
      setText(more, on ? 'fewer' : `${list.length - 1} more`);
    } });
    node.insertBefore(more, panel);
  }
  return node;
}

/* Fill `host` with the strip for a project, once: the fetch starts after the caller has painted, a late answer is dropped when isCurrent() says no,
   and nothing (no error, no toast, no focus) happens when the worker is down, slow or has no gotchas. Resolves to the strip node or null. */
function memoryGotchasMount(host, project, o) {
  const opt = o || {};
  if (!host || !project) return Promise.resolve(null);
  return memGet(project, 'palace', {}).then((d) => {
    if (typeof opt.isCurrent === 'function' && !opt.isCurrent()) return null;
    const strip = memoryGotchasStrip(d, { project });
    if (strip) { host.textContent = ''; host.append(strip); host.classList.remove('hidden'); }
    return strip;
  }, () => null);
}

/* ---------- health tile (Memory header, Usage, Settings) ---------- */

function memRateText(r) { return memNum(r) === null ? 'collecting' : `${(Math.round(r * 10) / 10).toLocaleString('en-US')} per day`; }

function memTileRow(label, ...kids) {
  return el('div', { class: 'mem-krow' }, el('span', { class: 'mem-kl', text: label }), el('span', { class: 'mem-kv' }, ...kids));
}

/* One reusable tile. h: state.memory merged with GET /api/memory/health. o: {compact, link}. null when claude-mem is off or not sampled. */
function memoryHealthTile(h, o) {
  const opt = o || {};
  if (!h || typeof h !== 'object' || h.state === 'off') return null;
  const down = h.state === 'down';
  const pill = memPillInfo(h, null);
  const node = el('section', { class: 'mem-tile' + (opt.compact ? ' compact' : ''), 'data-state': h.state || 'unknown', 'aria-label': 'claude-mem health' });
  if (!opt.bare) {
    node.append(el('div', { class: 'mem-tile-head' }, el('h3', { class: 'mem-k', text: 'claude-mem health' }),
      el('span', { class: `mem-pill ${pill.cls}`, title: pill.title }, el('span', { class: 'mem-pill-g', 'aria-hidden': 'true', text: pill.glyph }), el('span', { class: 'mem-pill-t', text: pill.text }))));
  }
  if (h.state !== 'up' && h.reason) node.append(el('p', { class: 'mem-reason', text: h.reason }));
  if (opt.bare && down) {                                              // a stopped worker has no figures: the reason and the footnote are the whole tile
    node.append(el('p', { class: 'mem-tile-foot dim', text: 'The counts come back once the worker answers. Per-day figures come from the board\'s own samples, taken every 5 minutes.' }));
    return node;
  }
  const rate = (r) => (down ? 'unknown' : `${memRateText(r && r.d1)} over 24 h, ${memRateText(r && r.d7)} over 7 days`);
  const count = (n) => (down ? 'unknown' : memCount(n));
  const rows = el('div', { class: 'mem-krows' });
  rows.append(memTileRow('Observations', el('b', { text: count(h.observations) }), el('span', { class: 'dim', text: ` ${rate(h.rates && h.rates.obs)}` })));
  if (!opt.compact) rows.append(memTileRow('Summaries', el('b', { text: count(h.summaries) }), el('span', { class: 'dim', text: ` ${rate(h.rates && h.rates.sum)}` })));
  const q = down ? null : memNum(h.queue_depth);
  rows.append(memTileRow('Observer queue', el('b', { text: q === null ? 'unknown' : String(q) }), el('span', { class: 'dim', text: q === null ? '' : ` waiting for the observer · ` }),
    q === null ? null : el('span', { class: 'mem-proc' }, el('span', { 'aria-hidden': 'true', text: h.processing ? '✽ ' : '∙ ' }), h.processing ? 'processing now' : 'not processing')));
  if (!opt.compact) {
    const a = down ? null : memNum(h.active_sessions);
    const stale = down ? null : memNum(h.stale_sessions);
    rows.append(memTileRow('Active sessions', el('b', { text: a === null ? 'unknown' : String(a) }),
      stale !== null && stale > 0 ? el('span', { class: 'dim', text: ` · about ${stale} possibly stale (an estimate: the worker's count minus the board's live Claude sessions)` }) : null));
  }
  rows.append(memErrorRow(h));
  node.append(rows);
  const foot = [];
  if (h.plugin_version && h.version && h.plugin_version !== h.version) foot.push(`Plugin ${h.plugin_version} is installed; the running worker is ${h.version} until it restarts.`);
  if (h.compat === 'untested') foot.push(`claude-mem ${h.worker_version || h.version || ''} is newer than the one this board was tested with (${h.tested_worker}).`);
  foot.push('Per-day figures come from the board\'s own samples, taken every 5 minutes.');
  node.append(el('p', { class: 'mem-tile-foot dim', text: foot.join(' ') }));
  if (opt.link) node.append(el('a', { class: 'btn small mem-tile-link', href: '#/memory', text: 'Open Memory' }));
  return node;
}

/* 'none', 'recovered' or 'failing' for the worker's last provider error. */
function memErrLevel(h) {
  const e = h && h.last_error;
  if (!e || typeof e !== 'object') return 'none';
  const at = Date.parse(e.at);
  const ok = Date.parse(e.last_success_at);
  return Number.isFinite(at) && Number.isFinite(ok) && ok > at && !(e.failures > 0) ? 'recovered' : 'failing';
}

/* The one line a collapsed Health disclosure shows: "up · 12 queued · last error recovered", or the state and its reason. */
function memHealthLine(h) {
  const s = h.state || 'unknown';
  if (s === 'down' || s === 'degraded' || s === 'incompatible') return `${s}${h.reason ? ' · ' + memOneLine(h.reason, 120) : ''}`;
  const bits = [s === 'up' ? 'up' : 'checking'];
  const q = memNum(h.queue_depth);
  if (q !== null) bits.push(`${q} queued`);
  const lv = memErrLevel(h);
  if (lv !== 'none') bits.push(lv === 'recovered' ? 'last error recovered' : 'observer failing');
  return bits.join(' · ');
}

/* The Memory page's health: a disclosure (summary line, the full tile inside). It opens by itself while the worker is down, slow, incompatible or failing,
   unless the person chose otherwise (o.pref true/false); o.onToggle(open) reports their click. null when claude-mem is off or not sampled. */
function memoryHealthDisclosure(h, o) {
  const opt = o || {};
  const tile = memoryHealthTile(h, { bare: true });
  if (!tile) return null;
  const bad = h.state === 'down' || h.state === 'degraded' || h.state === 'incompatible' || memErrLevel(h) === 'failing';
  const open = typeof opt.pref === 'boolean' ? opt.pref : bad;
  const sum = el('summary', { class: 'mem-health-sum', 'data-state': h.state || 'unknown', onclick: () => { if (typeof opt.onToggle === 'function') opt.onToggle(!node.open); } },
    el('b', { class: 'mem-health-k', text: 'Health' }), el('span', { class: 'mem-health-line' + (bad ? ' bad' : ' dim'), text: memHealthLine(h) }));
  const node = el('details', { class: 'mem-health', 'data-state': h.state || 'unknown' }, sum, tile);
  node.open = open;
  return node;
}

/* The last provider error: one line of message, its age, and "recovered" only when a later success came with no failures. Amber only while it is failing. */
function memErrorRow(h) {
  const e = h.last_error;
  if (!e || typeof e !== 'object') return memTileRow('Last provider error', el('span', { class: 'dim', text: 'none' }));
  const at = Date.parse(e.at);
  const ok = Date.parse(e.last_success_at);
  const recovered = Number.isFinite(at) && Number.isFinite(ok) && ok > at && !(e.failures > 0);
  const age = Number.isFinite(at) ? memAgo(at) : '';
  const msg = el('span', { class: 'mem-err-msg', title: e.message ? String(e.message) : null, text: memOneLine(e.message || 'no message', 140) });
  return memTileRow('Last provider error', el('span', { class: 'mem-err ' + (recovered ? 'recovered' : 'failing') },
    el('span', { class: 'mem-err-g', 'aria-hidden': 'true', text: recovered ? '✓' : '⚠' }), recovered ? 'recovered · ' : 'observer failing · ', msg, age ? el('span', { class: 'dim', text: ` · ${age}` }) : null));
}

/* ---------- the list tabs (Timeline, Summaries) and Search ---------- */

function memSummaryCard(s, ctx) {
  const node = el('article', { class: 'mem-sum', 'data-id': String(s.id) });
  const ms = memNum(s.created_at_epoch);
  const head = el('div', { class: 'mem-sum-head' }, ms !== null ? el('span', { class: 'mem-time mono', title: new Date(ms).toISOString(), text: memClock(ms) }) : null,
    s.repo ? el('span', { class: 'mem-repo mono', text: s.repo }) : null, s.platform_source === 'claude' || s.platform_source === 'codex' ? agentGlyph(s.platform_source) : null);
  const sid = s.session_id || s.memory_session_id;
  if (sid && ctx && typeof ctx.onSession === 'function') head.append(el('button', { class: 'chip-btn mem-sess mono', type: 'button', title: 'Show only this session', 'aria-label': `Show only session ${String(sid).slice(0, 8)}`, text: String(sid).slice(0, 8), onclick: () => ctx.onSession(sid) }));
  node.append(head);
  const parts = [['Request', s.request], ['Investigated', s.investigated], ['Learned', s.learned], ['Completed', s.completed], ['Next steps', s.next_steps]];
  let any = false;
  for (const [label, text] of parts) {
    if (!text) continue;
    any = true;
    node.append(el('div', { class: 'mem-k', text: label }), el('p', { class: 'mem-narr', text: String(text) }));
  }
  if (!any) node.append(el('p', { class: 'dim', text: 'This summary has no text.' }));
  return node;
}

function memPromptRow(p) {
  return el('article', { class: 'mem-prompt', 'data-id': String(p.id) }, el('div', { class: 'mem-meta' }, el('span', { class: 'mem-k', text: `Prompt ${memNum(p.prompt_number) === null ? '' : p.prompt_number}`.trim() }),
    memNum(p.created_at_epoch) !== null ? el('span', { class: 'mem-time mono', text: memClock(p.created_at_epoch) }) : null), el('p', { class: 'mem-narr', text: p.prompt_text || '' }));
}

/* ---------- the page ---------- */

const MemoryPage = {};

MemoryPage.st = () => (typeof state !== 'undefined' && state ? state : null);
MemoryPage.alive = (P) => Memory.cur === P && !P.dead;

MemoryPage.health = function (P) {
  const st = MemoryPage.st();
  const h = { ...((st && st.memory) || {}), ...(P.healthExtra || {}) };
  const e = P.env;                                                     // what the page's own last read said beats the 3 s poll when it says the worker is not fine
  if (e && (e.state === 'down' || e.state === 'degraded' || e.state === 'incompatible')) { h.state = e.state; h.reason = e.reason || h.reason; if (e.compat) h.compat = e.compat; }
  return h;
};

MemoryPage.paintHead = function (P) {
  const st = MemoryPage.st();
  const h = st && st.memory ? MemoryPage.health(P) : (P.healthExtra || null);   // the variant (down, degraded...) rides in through P.env
  const info = memPillInfo(h && Object.keys(h).length ? h : null, P.env && P.env.stale ? P.env : null);
  P.pill.className = `mem-pill ${info.cls}`;
  P.pill.setAttribute('title', info.title || '');
  P.pillG.textContent = info.glyph;
  P.pillT.textContent = info.text;
  const sig = JSON.stringify([h && h.state, h && h.observations, h && h.queue_depth, h && h.processing, h && h.last_error, h && h.rates, h && h.compat, h && h.reason, h && h.stale_sessions, h && h.active_sessions]);
  if (sig !== P.tileSig) {
    P.tileSig = sig;
    P.tileHost.textContent = '';
    const tile = st && st.memory ? memoryHealthDisclosure(h, { pref: P.healthPref, onToggle: (on) => { P.healthPref = on; } }) : null;
    if (tile) P.tileHost.append(tile);
    P.tileHost.classList.toggle('hidden', !tile);
  }
  const url = st && st.config && typeof st.config.mem_viewer_url === 'string' && /^https:\/\//.test(st.config.mem_viewer_url) ? st.config.mem_viewer_url : '';
  const vsig = url;
  if (vsig !== P.viewerSig) {
    P.viewerSig = vsig;
    P.viewer.textContent = '';
    if (url) {
      P.viewer.append(el('a', { class: 'btn small mem-viewer-link', href: url, target: '_blank', rel: 'noopener', text: 'Open claude-mem viewer' }),
        el('span', { class: 'mem-viewer-cap dim', text: `Opens claude-mem's own viewer, outside the board's sign-in: ${MEM_VIEWER_WARNING}.` }));
    }
    P.viewer.classList.toggle('hidden', !url);
  }
};

MemoryPage.paintNew = function (P) {
  const st = MemoryPage.st();
  const now = st && st.memory ? memNum(st.memory.observations) : null;
  const n = now !== null && P.baseTotal !== null ? now - P.baseTotal : 0;
  P.newPill.classList.toggle('hidden', !(n > 0) || P.tab === 'search');
  P.newBtn.textContent = n > 0 ? `${n} new · Load` : '';
  P.newBtn.setAttribute('title', 'claude-mem has more observations than when this view loaded. The count is for the whole worker, so some may belong to other projects.');
};

MemoryPage.persist = function (P) {
  memSaveFilters(P.project, P.filters);
  memStorePut(MEM_LAST, P.project);
};

/* Put the view in the address (replace: a filter change is not a history step). */
MemoryPage.mirror = function (P) {
  let h;
  try { h = buildHash('memory', { project: P.project }, memHashQuery(P.tab, P.q, P.filters)); } catch (_) { return; }
  if (h === (location.hash || '')) return;
  if (typeof navigate === 'function') navigate(h, { replace: true }); else location.hash = h;
};

MemoryPage.dropViews = function (P) { P.list = null; P.last = {}; };         // a view read under other filters is not this view

MemoryPage.setFilter = function (P, key, value) {
  P.filters = { ...P.filters, [key]: value };
  MemoryPage.dropViews(P);
  MemoryPage.persist(P);
  MemoryPage.mirror(P);
  MemoryPage.buildFilters(P);
  MemoryPage.load(P, { fresh: true });
};

MemoryPage.clearFilters = function (P) {
  P.filters = memFilterClean(null);
  MemoryPage.dropViews(P);
  MemoryPage.persist(P);
  MemoryPage.mirror(P);
  MemoryPage.buildFilters(P);
  MemoryPage.load(P, { fresh: true });
};

MemoryPage.repoOptions = function (P) {
  const st = MemoryPage.st();
  const p = ((st && st.projects) || []).find((x) => x.name === P.project);
  const names = new Set((p && p.repos ? p.repos : []).map((r) => r.name));
  if (p && p.root) names.add('root');
  if (P.filters.repo) names.add(P.filters.repo);
  return [['', 'All repos'], ...[...names].sort().map((n) => [n, n === 'root' ? 'project folder' : n])];
};

/* The filter bar: only what the tab can use. Palace: repo. Timeline: type, repo, agent, range (and the session chip). Summaries: repo, agent, range. Search: agent. */
MemoryPage.buildFilters = function (P) {
  const bar = P.filterBar;
  bar.textContent = '';
  const tab = P.tab;
  const f = P.filters;
  const on = (tab === 'palace') ? ['repo'] : tab === 'timeline' ? ['type', 'repo', 'agent', 'range'] : tab === 'summaries' ? ['repo', 'agent', 'range'] : ['agent'];
  const cell = (label, node) => el('label', { class: 'mem-fcell' }, el('span', { class: 'mem-k', text: label }), node);
  if (on.includes('type')) {
    const known = [...new Set([...MEM_KNOWN_TYPES, ...(f.type ? f.type.split(',') : [])])];
    const sel = selectEl([['', 'All types'], ...known.map((t) => [t, t])], f.type);
    sel.addEventListener('change', () => MemoryPage.setFilter(P, 'type', sel.value));
    bar.append(cell('Type', sel));
  }
  if (on.includes('repo')) {
    const sel = selectEl(MemoryPage.repoOptions(P), f.repo);
    sel.addEventListener('change', () => MemoryPage.setFilter(P, 'repo', sel.value));
    bar.append(cell('Repo', sel));
  }
  if (on.includes('agent')) bar.append(el('div', { class: 'mem-fcell' }, el('span', { class: 'mem-k', text: 'Agent' }), memSeg(MEM_AGENTS, f.agent, (v) => MemoryPage.setFilter(P, 'agent', v), 'Agent')));
  if (on.includes('range')) bar.append(el('div', { class: 'mem-fcell' }, el('span', { class: 'mem-k', text: 'Range' }), memSeg(MEM_RANGES, f.range || 'all', (v) => MemoryPage.setFilter(P, 'range', v === 'all' ? 'all' : v), 'Range')));
  if (f.session && (tab === 'timeline' || tab === 'summaries')) {
    bar.append(el('div', { class: 'mem-fcell' }, el('span', { class: 'mem-k', text: 'Session' }),
      el('button', { class: 'chip-btn mem-sess-chip mono', type: 'button', title: 'Show every session again', 'aria-label': `Session ${f.session.slice(0, 8)}, remove this filter`, text: `${f.session.slice(0, 8)} ✕`, onclick: () => MemoryPage.setFilter(P, 'session', '') })));
  }
  const active = tab === 'timeline' || tab === 'summaries' ? memHasFilters(f) : (tab === 'palace' ? !!f.repo : !!f.agent);
  if (active) bar.append(el('button', { class: 'minimal small mem-clear', type: 'button', text: 'Clear filters', onclick: () => MemoryPage.clearFilters(P) }));
  bar.classList.toggle('hidden', !on.length);
};

MemoryPage.setTab = function (P, tab, fromTabs) {
  if (P.tab === tab) return;
  P.tab = tab;
  if (!fromTabs && P.tabs) P.tabs.set(tab);
  MemoryPage.mirror(P);
  MemoryPage.buildFilters(P);
  MemoryPage.load(P, { fresh: false });
};

MemoryPage.setQuery = function (P, value) {
  P.q = String(value || '').trim().slice(0, MEM_Q_MAX);
  if (P.q && P.tab !== 'search') { P.tab = 'search'; if (P.tabs) P.tabs.set('search'); MemoryPage.buildFilters(P); }
  MemoryPage.mirror(P);
  if (P.tab === 'search') MemoryPage.load(P, { fresh: true });
};

/* A tab's data. Page one for a list; the palace and search are one answer. A newer call or an unmount drops an older answer. */
MemoryPage.load = function (P, o) {
  const tab = P.tab;
  const my = ++P.seq;
  const st = MemoryPage.st();
  P.baseTotal = st && st.memory ? memNum(st.memory.observations) : null;
  MemoryPage.paintNew(P);
  if (tab === 'palace') return MemoryPage.loadPalace(P, my);
  if (tab === 'search') return MemoryPage.loadSearch(P, my);
  return MemoryPage.loadList(P, my, { more: false, fresh: !!(o && o.fresh), anchor: o && o.anchor });
};

MemoryPage.loadPalace = function (P, my) {
  if (!P.palace || P.palace.project !== P.project) {
    if (P.palace) P.palace.view.destroy();
    P.palace = { project: P.project, view: memoryPalaceView(P.project, { repo: P.filters.repo, onData: (d) => { P.env = d; MemoryPage.paintHead(P); } }) };
  }
  P.palace.view.setRepo(P.filters.repo);
  P.panel.textContent = '';
  P.panel.append(P.palace.view.node);
  return P.palace.view.load().then(() => { void my; });
};

MemoryPage.loadSearch = function (P, my) {
  P.notes.textContent = '';
  P.panel.textContent = '';
  if (!P.q) {
    P.panel.append(pageEmpty('search', 'Search this project\'s memory', 'Type in the search box above: it looks through observations, session summaries and your prompts.'));
    return Promise.resolve();
  }
  P.panel.append(memSkeleton());
  const params = { q: P.q, agent: P.filters.agent, limit: 20 };
  return memGet(P.project, 'search', params).then((d) => {
    if (!MemoryPage.alive(P) || my !== P.seq) return;
    P.env = d;
    P.last.search = { d, at: Date.now() };
    MemoryPage.paintHead(P);
    MemoryPage.paintSearch(P, d, null);
  }, (e) => {
    if (!MemoryPage.alive(P) || my !== P.seq) return;
    const env = memErrorEnv(e);
    P.env = env;
    MemoryPage.paintHead(P);
    const prev = P.last.search;
    if (prev && prev.d.query === P.q) MemoryPage.paintSearch(P, prev.d, { ...env, at: prev.at });
    else { P.panel.textContent = ''; P.notes.textContent = ''; P.panel.append(memDegradedNode(env, () => MemoryPage.load(P, { fresh: true }), 'this search')); }
  });
};

MemoryPage.paintSearch = function (P, d, errEnv) {
  P.notes.textContent = '';
  P.panel.textContent = '';
  const env = errEnv ? { ...d, stale: true, stale_at: errEnv.at, reason: errEnv.reason } : d;
  P.notes.append(memNotesNode(memEnvNotes(env), () => MemoryPage.load(P, { fresh: true })));
  if (d.state === 'incompatible') return;
  const obs = d.observations || [];
  const sess = d.sessions || [];
  const prompts = d.prompts || [];
  if (!obs.length && !sess.length && !prompts.length) {
    P.panel.append(pageEmpty('search', `Nothing matched “${memOneLine(P.q, 60)}”`, 'Try fewer or different words, or pick another project above. Short single words work best.'));
    return;
  }
  const ctx = { project: P.project, open: P.open, meta: true, send: true, onSession: (sid) => MemoryPage.filterSession(P, sid) };
  P.panel.append(el('p', { class: 'mem-counts dim', text: `${memNum(d.total) === null ? obs.length + sess.length + prompts.length : d.total} match${(d.total || 0) === 1 ? '' : 'es'} for “${memOneLine(d.query || P.q, 60)}”${d.fallback === 'per_key' ? ' · searched key by key' : ''}` }));
  const section = (title, rows, build) => {
    if (!rows.length) return;
    P.panel.append(el('h3', { class: 'mem-sec', text: `${title} (${rows.length})` }), el('div', { class: 'mem-list' }, ...rows.map(build)));
  };
  section('Observations', obs, (o) => memItem(o, ctx));
  section('Session summaries', sess, (s) => memSummaryCard(s, { onSession: ctx.onSession }));
  section('Your prompts', prompts, (p) => memPromptRow(p));
};

MemoryPage.filterSession = function (P, sid) {
  if (!/^[A-Za-z0-9_-]{6,64}$/.test(String(sid))) return;
  const tab = P.tab === 'summaries' ? 'summaries' : 'timeline';
  P.filters = { ...P.filters, session: sid };
  MemoryPage.dropViews(P);
  MemoryPage.persist(P);
  if (P.tab !== tab) { P.tab = tab; if (P.tabs) P.tabs.set(tab); }
  MemoryPage.mirror(P);
  MemoryPage.buildFilters(P);
  MemoryPage.load(P, { fresh: true });
};

/* Timeline / Summaries: P.list = {tab, items, next_before, has_more, scan_capped, env}. A reload paints page one; Load more appends the next page. */
MemoryPage.loadList = function (P, my, o) {
  const tab = P.tab;
  const what = tab === 'timeline' ? 'observations' : 'summaries';
  const more = !!(o && o.more);
  const refresh = !!(o && o.refresh);
  const cur = P.list && P.list.tab === tab && P.list.project === P.project ? P.list : null;
  const cached = cur || (P.last[tab] && P.last[tab].project === P.project ? P.last[tab] : null);       // the last view of this tab under the same filters
  const params = { limit: MEM_PAGE, ...memFilterParams(P.filters, Date.now()) };
  if (tab === 'summaries') delete params.type;
  if (more && cur && cur.next_before) params.before = cur.next_before;
  if (!more && !refresh) {
    if (cached) { P.list = cached; MemoryPage.paintList(P, null); }                                  // paint what was read at once; the fresh page replaces it
    else { P.panel.textContent = ''; P.notes.textContent = ''; P.panel.append(memSkeleton()); }
  }
  P.loadingMore = more;
  return memGet(P.project, what, params).then((d) => {
    if (!MemoryPage.alive(P) || my !== P.seq) return;
    P.loadingMore = false;
    P.env = d;
    const fresh = Array.isArray(d.items) ? d.items : [];
    const base = more || refresh ? cur : null;
    let items = fresh;
    let keepTail = false;
    if (base && more) { items = [...base.items, ...fresh.filter((x) => !base.items.some((y) => y.id === x.id))]; }
    else if (base && refresh && fresh.length) {                              // "N new - Load": the new rows go on top of what is already read
      const top = base.items.length ? base.items[0].created_at_epoch : 0;
      const newer = fresh.filter((x) => x.created_at_epoch > top && !base.items.some((y) => y.id === x.id));
      if (newer.length < fresh.length) { items = [...newer, ...base.items]; keepTail = true; }       // some of page one is already read: the old rows and their cursor stay
    }
    P.list = { tab, project: P.project, items, next_before: keepTail ? base.next_before : d.next_before, has_more: keepTail ? base.has_more : !!d.has_more, scan_capped: !!d.scan_capped, env: d, at: Date.now() };
    P.last[tab] = P.list;
    MemoryPage.paintHead(P);
    MemoryPage.paintList(P, null, o && o.anchor);
  }, (e) => {
    if (!MemoryPage.alive(P) || my !== P.seq) return;
    P.loadingMore = false;
    const env = memErrorEnv(e);
    P.env = env;
    MemoryPage.paintHead(P);
    const keep = cur || cached;
    if (keep) { P.list = keep; MemoryPage.paintList(P, { ...env, at: keep.at }); return; }
    P.panel.textContent = '';
    P.notes.textContent = '';
    P.panel.append(memDegradedNode(env, () => MemoryPage.load(P, { fresh: true }), tab === 'timeline' ? 'the timeline' : 'the summaries'));
  });
};

MemoryPage.anchorTop = function (P, id) {
  try {
    const n = id ? P.panel.querySelector(`[data-id="${id}"]`) : null;
    return n ? n.getBoundingClientRect().top : null;
  } catch (_) { return null; }
};

/* Rebuild the list from P.list; the row the reader was on (anchor) keeps its place on the screen. */
MemoryPage.paintList = function (P, errEnv, anchorId) {
  const L = P.list;
  if (!L) return;
  const first = P.panel.querySelector ? P.panel.querySelector('.mem-item, .mem-sum') : null;
  const keep = anchorId || (first ? first.getAttribute('data-id') : null);
  const before = MemoryPage.anchorTop(P, keep);
  P.notes.textContent = '';
  const env = errEnv ? { ...L.env, stale: true, stale_at: errEnv.at, reason: errEnv.reason } : L.env;
  P.notes.append(memNotesNode(memEnvNotes(env), () => MemoryPage.load(P, { fresh: true })));
  P.panel.textContent = '';
  if (L.env && L.env.state === 'incompatible') return;
  const tab = L.tab;
  if (!L.items.length) {
    const none = L.env && L.env.keys_checked && !(L.env.keys || []).length;
    P.panel.append(none ? memEmptyNoMemory(P.project)
      : memHasFilters(P.filters) ? pageEmpty(tab === 'timeline' ? 'timeline-events' : 'document', tab === 'timeline' ? 'No observations match these filters' : 'No summaries match these filters', 'Widen the range or clear the filters.')
        : pageEmpty(tab === 'timeline' ? 'timeline-events' : 'document', tab === 'timeline' ? 'No observations yet' : 'No summaries yet', tab === 'timeline' ? 'claude-mem writes them while sessions run.' : 'claude-mem writes one when a session ends; run a session here and check back.'));
    if (memHasFilters(P.filters)) P.panel.append(el('button', { class: 'small', type: 'button', text: 'Clear filters', onclick: () => MemoryPage.clearFilters(P) }));
    return;
  }
  const ctx = { project: P.project, open: P.open, meta: true, send: tab === 'timeline', onSession: (sid) => MemoryPage.filterSession(P, sid) };
  const list = el('div', { class: 'mem-list', 'data-tab': tab });
  for (const g of memGroupDays(L.items)) {
    list.append(el('h3', { class: 'mem-day', text: g.ms === null ? 'Undated' : memDayLabel(g.ms) }));
    for (const it of g.rows) list.append(tab === 'timeline' ? memItem(it, ctx) : memSummaryCard(it, ctx));
  }
  P.panel.append(list);
  const foot = el('div', { class: 'mem-more' });
  if (L.has_more) {
    foot.append(el('button', { class: 'small mem-loadmore', type: 'button', text: 'Load more', title: 'Older entries', onclick: () => { if (!P.loadingMore) MemoryPage.loadMore(P); } }));
    if (L.scan_capped) foot.append(el('span', { class: 'dim', text: 'This page was cut short to keep it fast; there is more. Load more continues from here.' }));
  } else foot.append(el('span', { class: 'dim', text: `That is everything claude-mem kept${memHasFilters(P.filters) ? ' for these filters' : ''}.` }));
  P.panel.append(foot);
  const after = MemoryPage.anchorTop(P, keep);
  if (before !== null && after !== null && before !== after) { try { window.scrollBy(0, after - before); } catch (_) { /* no scrolling here */ } }
};

MemoryPage.loadMore = function (P) {
  const L = P.list;
  if (!L || !L.has_more) return Promise.resolve();
  const last = L.items[L.items.length - 1];
  return MemoryPage.loadList(P, ++P.seq, { more: true, anchor: last ? String(last.id) : null });
};

/* "N new - Load": page one again, the new rows on top, the reader's place kept. */
MemoryPage.loadNew = function (P) {
  const st = MemoryPage.st();
  P.baseTotal = st && st.memory ? memNum(st.memory.observations) : null;
  MemoryPage.paintNew(P);
  if (P.tab === 'palace') return P.palace ? P.palace.view.load() : Promise.resolve();
  if (P.tab === 'search') return MemoryPage.load(P, { fresh: true });
  return MemoryPage.loadList(P, ++P.seq, { more: false, refresh: true });
};

MemoryPage.healthFetch = function (P, refresh) {
  const url = refresh ? '/api/memory/health?refresh=1' : '/api/memory/health';
  return api('GET', url).then((h) => {
    if (!MemoryPage.alive(P) || !h || typeof h !== 'object') return;
    P.healthExtra = { compat: h.compat, worker_version: h.worker_version, tested_worker: h.tested_worker, plugin_version: h.plugin_version, rates: h.rates, stale_sessions: h.stale_sessions, last_error: h.last_error };
    if (refresh) Object.assign(P.healthExtra, { state: h.state, version: h.version, observations: h.observations, summaries: h.summaries, queue_depth: h.queue_depth, processing: h.processing, active_sessions: h.active_sessions, reason: h.reason });
    P.tileSig = null;
    MemoryPage.paintHead(P);
  }, () => { /* the pill keeps what the poll said */ });
};

MemoryPage.projectSelect = function (P) {
  const st = MemoryPage.st();
  const names = memProjectNames(st);
  const sig = names.join('\n') + '|' + P.project;
  if (P.selSig === sig) return;
  P.selSig = sig;
  const all = names.includes(P.project) || !P.project ? names : [P.project, ...names];
  P.select.textContent = '';
  for (const n of all) P.select.append(el('option', { value: n, text: n }));
  if (P.project) P.select.value = P.project;
};

MemoryPage.build = function (root, route) {
  const st = MemoryPage.st();
  const r = memParseRoute(route, st);
  const P = { id: ++Memory.ids, root, route, project: r.project, auto: r.auto, waiting: false, tab: r.tab, q: r.q, filters: r.filters, seq: 0, dead: false, env: null, healthExtra: null, baseTotal: null,
    open: new Set(), list: null, last: {}, palace: null, timer: null, tileSig: null, healthPref: null, viewerSig: null, selSig: null, loadingMore: false };
  P.pillG = el('span', { class: 'mem-pill-g', 'aria-hidden': 'true' });
  P.pillT = el('span', { class: 'mem-pill-t' });
  P.pill = el('span', { class: 'mem-pill unknown', role: 'status' }, P.pillG, P.pillT);
  P.select = el('select', { class: 'mem-project', 'aria-label': 'Project' });
  P.select.addEventListener('change', () => {
    if (!P.select.value || P.select.value === P.project) return;
    try { navigate(buildHash('memory', { project: P.select.value })); } catch (_) { /* a name the router cannot hold: stay */ }
  });
  P.search = el('input', { class: 'mem-search', type: 'search', placeholder: 'Search this project\'s memory', 'aria-label': 'Search this project\'s memory', autocomplete: 'off', spellcheck: 'false', maxlength: String(MEM_Q_MAX) });
  P.search.value = P.q;
  P.search.addEventListener('input', () => {
    if (P.timer) clearTimeout(P.timer);
    P.timer = setTimeout(() => { P.timer = null; if (MemoryPage.alive(P)) MemoryPage.setQuery(P, P.search.value); }, MEM_DEBOUNCE);
  });
  P.search.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    if (P.timer) { clearTimeout(P.timer); P.timer = null; }
    MemoryPage.setQuery(P, P.search.value);
  });
  P.viewer = el('div', { class: 'mem-viewer hidden' });
  P.tileHost = el('div', { class: 'mem-tile-host hidden' });
  P.tabs = tabs(MEM_TABS.map(([id, label]) => ({ id, label })), P.tab, (id) => MemoryPage.setTab(P, id, true));
  P.filterBar = el('div', { class: 'mem-filters', role: 'group', 'aria-label': 'Filters' });
  P.newBtn = el('button', { class: 'small mem-new-btn', type: 'button', onclick: () => MemoryPage.loadNew(P) });
  P.newPill = el('div', { class: 'mem-new hidden', role: 'status' }, P.newBtn);
  P.notes = el('div', { class: 'mem-notes-host' });
  P.panel = el('div', { class: 'mem-panel', role: 'tabpanel' });
  const head = el('div', { class: 'page-head mem-pagehead' }, el('h1', { text: 'Memory' }), P.pill);
  const controls = el('div', { class: 'mem-controls' }, el('label', { class: 'mem-fcell mem-proj' }, el('span', { class: 'mem-k', text: 'Project' }), P.select),
    el('label', { class: 'mem-fcell mem-q' }, el('span', { class: 'mem-k', text: 'Search' }), P.search));
  root.append(el('div', { class: 'memory-page', 'data-memory': '' }, head, controls, P.viewer, P.tileHost, P.tabs.root, P.filterBar, P.newPill, P.notes, P.panel));
  return P;
};

/* The remembered project, else the first that has memory, else the first. One project at a time with a one-row observations
   read (the box's disk is busy: no palace fan-out per project), stopping at the first hit; at most 8 are tried. */
MemoryPage.pickDefault = async function (P) {
  const names = memProjectNames(MemoryPage.st()).slice(0, 8);
  const has = (d) => !!d && d.state !== 'incompatible' && !d.shape_error && Array.isArray(d.items) && d.items.length > 0;
  for (const n of names) {
    let d = null;
    try { d = await memGet(n, 'observations', { limit: 1 }); } catch (e) { d = null; }
    if (has(d)) return n;
  }
  return names[0];
};

MemoryPage.start = function (P) {
  const st = MemoryPage.st();
  MemoryPage.projectSelect(P);
  MemoryPage.paintHead(P);
  if (!st || !Array.isArray(st.projects)) {                            // the board has not answered yet: not "no projects", just not known
    P.waiting = true;
    P.panel.append(memSkeleton());
    return;
  }
  P.waiting = false;
  if (!P.project) {
    MemoryPage.buildFilters(P);
    P.panel.append(pageEmpty('database', 'No projects yet', 'Add a project first: Memory is read one project at a time.'));
    return;
  }
  if (P.auto) {
    P.auto = false;
    P.panel.append(memSkeleton());
    MemoryPage.pickDefault(P).then((name) => {
      if (!MemoryPage.alive(P)) return;
      P.panel.textContent = '';
      if (name && name !== P.project) {
        P.project = name;
        P.filters = memFiltersFrom((P.route && P.route.query) || {}, memStoredFilters(name));
        P.selSig = null;
        MemoryPage.projectSelect(P);
      }
      MemoryPage.go(P);
    });
    return;
  }
  MemoryPage.go(P);
};

MemoryPage.go = function (P) {
  MemoryPage.buildFilters(P);
  MemoryPage.persist(P);
  MemoryPage.healthFetch(P, false);
  MemoryPage.load(P, { fresh: true });
};

MemoryPage.teardown = function (P) {
  P.dead = true;
  P.seq += 1;
  if (P.timer) { clearTimeout(P.timer); P.timer = null; }
  if (P.palace) P.palace.view.destroy();
};

registerPage('memory', {
  title(route) {
    const p = memParseRoute(route, typeof state !== 'undefined' ? state : null);
    return p.project ? `Memory ${p.project}` : 'Memory';
  },
  mount(root, route) {
    if (Memory.cur) MemoryPage.teardown(Memory.cur);
    const P = MemoryPage.build(root, route);
    Memory.cur = P;
    MemoryPage.start(P);
  },
  update(st) {
    const P = Memory.cur;
    if (!P || !st) return;
    if (P.waiting && Array.isArray(st.projects)) {                       // the first state arrived after the page was mounted: start for real
      const r = memParseRoute(P.route, st);
      P.project = r.project; P.auto = r.auto; P.filters = r.filters; P.tab = r.tab; P.q = r.q; P.selSig = null;
      P.panel.textContent = '';
      MemoryPage.start(P);
      return;
    }
    MemoryPage.projectSelect(P);
    MemoryPage.paintHead(P);
    MemoryPage.paintNew(P);
  },
  onRoute(route) {
    const P = Memory.cur;
    if (!P) return;
    P.route = route;
    const r = memParseRoute(route, MemoryPage.st());
    const sameFilters = JSON.stringify(r.filters) === JSON.stringify(P.filters);
    if (r.project !== P.project) {                                       // another project: a clean page
      MemoryPage.teardown(P);
      P.root.textContent = '';
      Memory.cur = MemoryPage.build(P.root, route);
      MemoryPage.start(Memory.cur);
      if (typeof refreshTitle === 'function') refreshTitle();
      return;
    }
    if (r.tab === P.tab && r.q === P.q && sameFilters) return;           // our own mirror, or nothing that matters
    const tabChanged = r.tab !== P.tab;
    const needLoad = tabChanged || r.q !== P.q || !sameFilters;
    P.tab = r.tab; P.q = r.q;
    if (!sameFilters) { P.filters = r.filters; MemoryPage.dropViews(P); }
    if (P.search.value !== P.q) P.search.value = P.q;
    P.tabs.set(P.tab);
    MemoryPage.buildFilters(P);
    if (needLoad) MemoryPage.load(P, { fresh: true });
  },
  unmount() {
    const P = Memory.cur;
    if (P) MemoryPage.teardown(P);
    Memory.cur = null;
  },
});
