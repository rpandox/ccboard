/* ccboard agents page (#/agents) and the session card every session list shares.
   sessionCard(s, opts) builds one session as a keyed, patch-in-place node: the roster here, the inbox page and the session peek
   all use it (row layout by default, opts.peek for the peek's block layout), so the glyphs, age, model and context, last
   prompt and message, Open / Ack / Kill and the nudge chips look and behave the same everywhere. The node keeps references to
   its parts and node.ccPatch(session) updates only what changed, so a poll never recreates a row that holds focus.
   The roster groups the live sessions by project, blocked sessions first. Classic script: globals are prefixed to stay unique. */
'use strict';

const SESSION_NUDGES = ['continue', 'merge', 'push', 'pr', 'add commit push', 'do it'];   // the replies typed most often, in order
const SESSION_RANK = { waiting: 0, errored: 1, working: 2, idle: 3, done: 3, ended: 4, unknown: 5 };
const SESSION_NUDGE_STATES = ['waiting', 'idle', 'done', 'working', 'errored'];            // an ended pane is a shell prompt: never type into it
const agentsTicker = { n: 0, timer: null };
const CV_AUTO_ROWS = 30;                                                                    // a list longer than this gets the cv-auto class (content-visibility, see pages.css)

/* Every live session in a state payload, each with its project, repo and whether it sits in the project folder. */
function rosterSessions(st) {
  const out = [];
  for (const p of ((st && st.projects) || [])) {
    for (const r of [p.root, ...(p.repos || [])]) {
      if (!r) continue;
      for (const s of (r.sessions || [])) out.push({ ...s, project: p.name, repo: r.name, folder: r.name === 'root' || !!r.root });
    }
    for (const s of (p.orphan_sessions || [])) out.push({ ...s, project: p.name, repo: s.repo || '?', folder: s.repo === 'root' });
  }
  return out;
}

/* Last activity as epoch seconds: state_at is an ISO string, created is already epoch seconds. */
function sessionActivity(s) {
  const t = s.state_at ? Date.parse(s.state_at) / 1000 : NaN;
  return Number.isFinite(t) ? t : (s.created || 0);
}

function sessionStateKey(s) { return ownKey(STATE_GLYPH, s.state) ? s.state : 'unknown'; }

function sessionCompare(a, b) {
  return SESSION_RANK[sessionStateKey(a)] - SESSION_RANK[sessionStateKey(b)]
    || sessionActivity(b) - sessionActivity(a) || String(a.tmux).localeCompare(String(b.tmux));
}

function sessionHash(tmux) { try { return buildHash('session', { tmux }); } catch (_) { return '#/'; } }

function sessionWhere(s, withProject) {
  const folder = s.folder || s.repo === 'root';
  if (!withProject) return folder ? 'project folder' : (s.repo || '?');
  return folder ? `${s.project} (project folder)` : `${s.project}/${s.repo || '?'}`;
}

function sessionMetaText(s, st) {
  const t = s.stats || {};
  const parts = [GLYPH_LABEL[st]];
  if (t.model) parts.push(t.model);
  if (typeof t.context_pct === 'number') parts.push(`ctx ${Math.round(t.context_pct)}%`);
  return parts.join(' · ');
}

function sessionNudgeable(s) { return sessionAgent(s) !== 'shell' && SESSION_NUDGE_STATES.includes(s.state); }

function sessionPerm(tmux) {
  if (!state) return null;
  for (const pr of (state.pending_permissions || [])) if (pr.tmux_name === tmux) return pr;
  return null;
}

async function sessionNudge(s, text, btn) {
  if (!s || (btn && btn.disabled)) return;
  if (btn) btn.disabled = true;
  try {
    await api('POST', `/api/sessions/${encodeURIComponent(s.tmux)}/keys`, { text, enter: true });
    pageToast(`sent "${text}" to ${s.name || s.tmux}`, 'ok');
  } catch (e) { pageToast(e.message, 'bad'); }
  finally { if (btn) btn.disabled = false; }
}

/* The row's own send box (components.js composer): Enter sends, Shift+Enter adds a line; the draft survives polls because the
   row node is kept and patched. The text goes through /keys like the chips (multi-line arrives as one bracketed paste). */
async function sessionSend(s, ta) {
  const text = String(ta.value || '').replace(/\r\n?/g, '\n');
  if (!s || !text.trim()) return;
  ta.disabled = true;
  try {
    await api('POST', `/api/sessions/${encodeURIComponent(s.tmux)}/keys`, { text, enter: true });
    ta.value = '';
    if (typeof composerGrow === 'function') composerGrow(ta);
    pageToast(`sent to ${s.name || s.tmux}`, 'ok');
  } catch (e) { pageToast(e.message, 'bad'); }
  finally { ta.disabled = false; if (typeof ta.focus === 'function') ta.focus(); }
}

async function sessionAck(s) {
  try { await api('POST', `/api/sessions/${encodeURIComponent(s.tmux)}/ack`); } catch (e) { setError(e.message); }
  await poll(true);
}

function agentsAgeNode(node, epoch) {
  const e = epoch ? String(Math.floor(epoch)) : '';
  if (node.getAttribute('data-epoch') !== e) node.setAttribute('data-epoch', e);
  setTextIfChanged(node, epoch ? fmtAge(epoch) : '');
}

/* One timer for every <time class="age"> on screen; pages start it on mount and stop it on unmount. */
function agentsTick() {
  if (document.hidden) return;
  for (const n of Array.from(document.querySelectorAll('time.age[data-epoch]'))) {
    const e = parseInt(n.getAttribute('data-epoch'), 10);
    if (e) setTextIfChanged(n, fmtAge(e));
  }
}

function startAgeTicker() {
  agentsTicker.n += 1;
  if (agentsTicker.timer || typeof setInterval !== 'function') return;
  agentsTicker.timer = setInterval(agentsTick, 5000);
  if (agentsTicker.timer && typeof agentsTicker.timer.unref === 'function') agentsTicker.timer.unref();
}

function stopAgeTicker() {
  agentsTicker.n = Math.max(0, agentsTicker.n - 1);
  if (agentsTicker.n || !agentsTicker.timer) return;
  clearInterval(agentsTicker.timer);
  agentsTicker.timer = null;
}

/* opts: compact (shorter texts), peek (block layout for the dock / sheet), perm (show a pending permission with Allow / Deny),
   showProject (project/repo instead of just the repo), link (the name opens the peek), cls (extra class on the node). */
function sessionCard(s, opts) {
  const o = Object.assign({ compact: false, peek: false, perm: false, showProject: false, link: true, cls: '' }, opts || {});
  const cur = { s };
  const killKey = 'kill:' + s.tmux;
  const glyphs = el('span', { class: o.peek ? 'peek-glyphs' : 'rr-g' });
  const nameNode = o.link && !o.peek ? el('a', { class: 'rr-name', href: sessionHash(s.tmux) }) : el('span', { class: 'rr-name' });
  const where = el('span', { class: 'rr-where' });
  const age = el('time', { class: 'age rr-age', title: 'last activity' });
  const meta = el('span', { class: 'rr-meta' });
  const promptNode = el('span', { class: 'dim' });
  const msgNode = el('span', { class: 'rr-msg' });
  const permNote = el('span', { class: 'perm-note' });
  const permBtns = el('span', { class: 'actions perm-btns' });
  const ackSlot = el('span', { class: 'slot-ack' });
  const killSlot = el('span', { class: 'slot-kill' });
  const openLink = el('a', { class: o.peek ? 'btn primary' : 'btn small', href: `/term/${encodeURIComponent(s.tmux)}`, target: '_blank', rel: 'noopener', text: o.peek ? 'Open terminal' : 'Open' });
  const chips = el('div', { class: 'chips' + (o.peek ? '' : ' rr-chips'), role: 'group', 'aria-label': 'Quick replies' });
  for (const text of SESSION_NUDGES) {
    const b = el('button', { class: 'chip-btn', type: 'button', text });
    b.addEventListener('click', (e) => { e.stopPropagation(); sessionNudge(cur.s, text, b); });
    chips.append(b);
  }
  let sendRow = null;
  if (!o.peek) {                                             // the peek has its own composer (session.js)
    const ta = composer({ placeholder: `send to ${s.name || s.tmux} · ⇧Enter new line`, label: `send to ${s.name || s.tmux}`, onSend: () => sessionSend(cur.s, ta) });
    ta.addEventListener('click', (e) => e.stopPropagation());
    sendRow = el('form', { class: 'rr-send', onsubmit: (e) => { e.preventDefault(); e.stopPropagation(); sessionSend(cur.s, ta); } },
      ta, el('button', { class: 'small primary', type: 'submit', text: 'Send', onclick: (e) => e.stopPropagation() }));
  }
  let node;
  let promptHost = promptNode;
  let msgHost = msgNode;
  let permHost = null;                                       // what to hide when no permission is pending (peek: its own block)
  if (o.peek) {
    const block = (label, body, extra) => el('div', { class: 'peek-block' + (extra || '') }, el('span', { class: 'k', text: label }), body);
    promptHost = block('Last prompt', promptNode);
    msgHost = block('Last message', msgNode);
    permHost = o.perm ? block('Needs permission', [permNote, permBtns], ' peek-perm') : null;
    node = el('div', { class: 'peek-card', 'data-tmux': s.tmux },
      el('div', { class: 'peek-sub' }, glyphs, where, age, meta),
      promptHost, msgHost, permHost,
      el('div', { class: 'peek-actions' }, openLink, ackSlot, killSlot), chips);
  } else {
    node = el('div', { class: 'rrow' + (o.compact ? ' compact' : '') + (o.cls ? ' ' + o.cls : ''), 'data-tmux': s.tmux },
      glyphs, el('div', { class: 'rr-main' }, nameNode, where, age), meta,
      el('div', { class: 'rr-last' }, o.perm ? permNote : null, promptNode, msgNode),
      el('div', { class: 'rr-actions' }, o.perm ? permBtns : null, openLink, ackSlot, killSlot), chips, sendRow);
  }

  function patchPerm(pr) {
    const sig = pr ? `${pr.id}:${pr.summary || ''}` : '';
    if (cur.perm === sig) return;
    cur.perm = sig;
    permBtns.textContent = '';
    if (pr) {
      permBtns.append(
        el('button', { class: 'primary small', type: 'button', onclick: (e) => { e.stopPropagation(); decide(pr.id, 'allow'); }, text: 'Allow' }),
        el('button', { class: 'danger small', type: 'button', onclick: (e) => { e.stopPropagation(); decide(pr.id, 'deny'); }, text: 'Deny' }));
    }
    permNote.textContent = pr ? String(pr.summary || 'permission request').slice(0, o.peek ? 800 : 200) : '';
    for (const n of (permHost ? [permHost] : [permNote, permBtns])) n.classList.toggle('hidden', !pr);
  }

  function patch(s2) {
    cur.s = s2;
    const st = sessionStateKey(s2);
    const agent = sessionAgent(s2);
    const gk = st + '|' + agent;
    if (cur.g !== gk) { glyphs.textContent = ''; glyphs.append(stateGlyph(st), agentGlyph(agent)); cur.g = gk; }
    setTextIfChanged(nameNode, s2.name || s2.tmux);
    setTextIfChanged(where, sessionWhere(s2, o.showProject));
    agentsAgeNode(age, sessionActivity(s2));
    setTextIfChanged(meta, sessionMetaText(s2, st));
    const lim = o.peek ? [800, 2000] : (o.compact ? [120, 160] : [200, 320]);
    const pt = s2.last_prompt ? '› ' + String(s2.last_prompt).slice(0, lim[0]) : '';
    const mt = s2.last_message ? String(s2.last_message).slice(0, lim[1]) : '';
    setTextIfChanged(promptNode, pt);
    setTextIfChanged(msgNode, mt);
    promptHost.classList.toggle('hidden', !pt);
    msgHost.classList.toggle('hidden', !mt);
    node.classList.toggle('attn', !!s2.needs_attention);
    if (o.perm) patchPerm(sessionPerm(s2.tmux));
    const ack = !!s2.needs_attention;
    if (cur.ack !== ack) {
      cur.ack = ack;
      ackSlot.textContent = '';
      if (ack) ackSlot.append(el('button', { class: 'small', type: 'button', onclick: (e) => { e.stopPropagation(); sessionAck(cur.s); }, text: 'Ack' }));
    }
    const confirming = ui.confirm === killKey;                // rebuilt only when the two-tap state flips (confirmButton repaints through renderProjects)
    if (cur.kill !== confirming) {
      cur.kill = confirming;
      killSlot.textContent = '';
      killSlot.append(confirmButton(killKey, 'Kill', () => api('DELETE', `/api/sessions/${encodeURIComponent(cur.s.tmux)}`), true));
    }
    chips.classList.toggle('hidden', !sessionNudgeable(s2));
    if (sendRow) sendRow.classList.toggle('hidden', !sessionNudgeable(s2));
  }

  node.ccPatch = patch;
  patch(s);
  return node;
}

/* ---------- the roster ---------- */

function agentsCounts(list) {
  const n = { waiting: 0, working: 0, idle: 0, done: 0, errored: 0, ended: 0 };
  for (const s of list) { const k = sessionStateKey(s); if (ownKey(n, k)) n[k] += 1; }
  return n;
}

function agentsSummaryText(list) {
  const n = agentsCounts(list);
  const parts = [`${STATE_GLYPH.waiting} ${n.waiting} need you`, `${STATE_GLYPH.working} ${n.working} working`, `${STATE_GLYPH.idle} ${n.idle} idle`, `${STATE_GLYPH.done} ${n.done} done`];
  if (n.errored) parts.push(`${STATE_GLYPH.errored} ${n.errored} error`);
  if (n.ended) parts.push(`${STATE_GLYPH.ended} ${n.ended} ended`);
  return parts.join(' · ');
}

/* Groups by project (directory): the group with the most urgent session first, then the most recently active. */
function agentsGroups(list) {
  const by = new Map();
  for (const s of list) { if (!by.has(s.project)) by.set(s.project, []); by.get(s.project).push(s); }
  const groups = [];
  for (const [name, items] of by) {
    items.sort(sessionCompare);
    groups.push({ key: name, name, items, rank: SESSION_RANK[sessionStateKey(items[0])], at: Math.max(...items.map(sessionActivity)) });
  }
  return groups.sort((a, b) => a.rank - b.rank || b.at - a.at || a.name.localeCompare(b.name));
}

function agentsGroupNode(g) {
  const name = el('span', { class: 'rg-name' });
  const count = el('span', { class: 'mono' });
  const list = el('div', { class: 'rg-list' });
  const node = el('section', { class: 'rgroup' }, el('div', { class: 'rgroup-head' }, name, count), list);
  const rows = makeKeyedList(list, { key: (s) => s.tmux, create: (s) => sessionCard(s, { compact: true }), patch: (n, s) => n.ccPatch(s) });
  node.ccPatch = (grp) => {
    setTextIfChanged(name, grp.name);
    setTextIfChanged(count, `${grp.items.length} session${grp.items.length === 1 ? '' : 's'}`);
    rows.update(grp.items);
    list.classList.toggle('cv-auto', grp.items.length > CV_AUTO_ROWS);
  };
  node.ccPatch(g);
  return node;
}

const agentsPage = { refs: null };

registerPage('agents', {
  title: 'Agents',
  mount(root) {
    const summary = el('p', { class: 'summary' });
    const roster = el('div', { class: 'roster' });
    const none = pageEmpty('console', 'No live sessions', 'Start one from the + menu: every session on this box shows up here.');
    const external = pageEmpty('cloud', 'Background sessions', 'Background sessions from the Claude registry arrive in v0.5.4');
    const loading = !currentState();                                    // first paint before /api/state: skeleton rows, and no empty states yet
    if (loading) { none.classList.add('hidden'); external.classList.add('hidden'); }
    Pages.reset();
    root.append(el('div', { class: 'agents' },
      el('div', { class: 'page-head' }, el('h1', { text: 'Agents' }), summary), ...(loading ? [Pages.skeleton(3)] : []), roster, none, external));
    // a click on a row selects it, so the mouse and j / k share one selection
    roster.addEventListener('click', (e) => {
      const row = e.target && typeof e.target.closest === 'function' ? e.target.closest('.rrow') : null;
      const i = row ? Pages.items().findIndex((x) => x.tmux === row.getAttribute('data-tmux')) : -1;
      if (i >= 0) Pages.setIndex(i);
    });
    agentsPage.refs = { summary, none, external, groups: makeKeyedList(roster, { key: (g) => 'g:' + g.key, create: agentsGroupNode, patch: (n, g) => n.ccPatch(g) }) };
    startAgeTicker();
  },
  update(st) {
    const r = agentsPage.refs;
    if (!r) return;
    Pages.dropSkeleton();
    const list = rosterSessions(st);
    setTextIfChanged(r.summary, agentsSummaryText(list));
    r.groups.update(agentsGroups(list));
    r.none.classList.toggle('hidden', list.length > 0);
    r.external.classList.toggle('hidden', !!st.external);     // registry rows arrive with state.external in v0.5.4
    Pages.sync();                                             // the roster may have reordered or lost the selected session
    Pages.paint();
  },
  unmount() {
    agentsPage.refs = null;
    stopAgeTicker();
  },
});
