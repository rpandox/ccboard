/* ccboard agents page (#/agents) and the session card every session list shares.
   sessionCard(s, opts) builds one session as a keyed, patch-in-place node: the roster here, the inbox page and the session peek
   all use it (row layout by default, opts.peek for the peek's block layout), so the glyphs, age, model and context, last
   prompt and message, Open / Ack / Kill and the nudge chips look and behave the same everywhere. The node keeps references to
   its parts and node.ccPatch(session) updates only what changed, so a poll never recreates a row that holds focus.
   The roster groups the live sessions by project, blocked sessions first. Classic script: globals are prefixed to stay unique.
   opts.rich (Home) adds the dense row: a model chip, a context meter with a one-tap compact chip, worktree / PR / subagents / cost / limit /
   blocked chips and a tail expander (the last 12 pane lines through Live.subscribe); the plain row (Agents, Inbox) is unchanged. */
'use strict';

const SESSION_NUDGES = ['continue', 'merge', 'push', 'pr', 'add commit push', 'do it'];   // the replies typed most often, in order
const SESSION_RANK = { waiting: 0, errored: 1, working: 2, idle: 3, done: 3, ended: 4, unknown: 5 };
const SESSION_NUDGE_STATES = ['waiting', 'idle', 'done', 'working', 'errored'];            // an ended pane is a shell prompt: never type into it
const agentsTicker = { n: 0, timer: null };
const CTX_HI = 60;                                                                          // context meter turns amber here and offers the one-tap compact chip
const CTX_CRIT = 85;                                                                        // and red here
const COST_WARN = 10;                                                                       // session cost chip: amber at $10 (about the p99 of a session), red at $100
const COST_BAD = 100;
const TAIL_LINES = 12;
const WORKTREE_PATH_RE = /(?:^|\/)\.claude\/worktrees\//;
const agentsTails = new Set();                                                              // row nodes that hold a Live subscription (swept when their row leaves the DOM)
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

/* The active rate-limit episode of a state payload, or null. state.rate_limited is the kv record, {value: {session, message, kind, resets_at}, at}
   as the poll serves it, or the bare {session, message, kind, resets_at}: both shapes read the same. An episode is over once resets_at has passed
   (resets_at is epoch seconds); without one it lasts 5 hours from `at`. */
function rateLimitOf(st) {
  const raw = st && st.rate_limited;
  if (!raw || typeof raw !== 'object') return null;
  const v = raw.value && typeof raw.value === 'object' ? raw.value : raw;
  if (!v || typeof v !== 'object' || (!v.session && !v.message && !v.kind)) return null;
  const now = Date.now() / 1000;
  let reset = v.resets_at;
  if (typeof reset === 'string') reset = Date.parse(reset) / 1000;
  if (typeof reset === 'number' && Number.isFinite(reset) && reset > 0) return reset > now ? v : null;
  const at = raw.at ? Date.parse(raw.at) / 1000 : 0;
  return at && now - at > 5 * 3600 ? null : v;
}

/* The full task (state.tasks) behind a session's task chip: by the chip's id, else by the tmux name (legacy tasks bind by name). */
function sessionTask(s, st) {
  const list = (st && st.tasks) || [];
  if (s.task && s.task.id !== undefined && s.task.id !== null) { const t = list.find((x) => x.id === s.task.id); if (t) return t; }
  return list.find((x) => x.tmux === s.tmux && !x.archived_at) || null;
}

function sessionWorktree(s, st) {
  if (WORKTREE_PATH_RE.test(String(s.path || ''))) return true;
  const t = sessionTask(s, st);
  return !!(t && t.mode === 'worktree' && s.task);
}

function sessionRegistryJob(s) {
  const reg = s.flags && s.flags.registry;
  return reg && reg.job && typeof reg.job === 'object' ? reg.job : null;
}

/* Subscriptions held by tail expanders: a row that left the DOM (its block collapsed, the group changed, the session ended) drops its own. */
function sessionTailSweep() { for (const n of Array.from(agentsTails)) if (n.isConnected === false && typeof n.ccDestroy === 'function') n.ccDestroy(); }
function sessionTailStopAll() { for (const n of Array.from(agentsTails)) if (typeof n.ccDestroy === 'function') n.ccDestroy(); }
function sessionTailAvailable() { return typeof Live !== 'undefined' && !!Live && typeof Live.subscribe === 'function' && typeof Live.unsubscribe === 'function'; }

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
  for (const n of Array.from(document.querySelectorAll('time.age[data-epoch], time.until[data-epoch]'))) {
    const e = parseInt(n.getAttribute('data-epoch'), 10);
    if (e) setTextIfChanged(n, n.classList.contains('until') ? fmtIn(e) : fmtAge(e));
  }
}

function startAgeTicker() {
  agentsTicker.n += 1;
  if (agentsTicker.timer || typeof setInterval !== 'function') return;
  agentsTicker.timer = setInterval(agentsTick, 1000);
  if (agentsTicker.timer && typeof agentsTicker.timer.unref === 'function') agentsTicker.timer.unref();
}

function stopAgeTicker() {
  agentsTicker.n = Math.max(0, agentsTicker.n - 1);
  if (agentsTicker.n || !agentsTicker.timer) return;
  clearInterval(agentsTicker.timer);
  agentsTicker.timer = null;
}

/* The context meter: a 40 px bar and the percentage (class ctx-hi from CTX_HI, ctx-crit from CTX_CRIT). The bar width is set through the CSSOM. */
function ctxMeter() {
  const fill = el('i');
  const pctText = el('span', { class: 'ctx-pct mono' });
  const node = el('span', { class: 'ctx', title: 'context window used' }, el('span', { class: 'ctx-bar' }, fill), pctText);
  node.ccSet = (pct) => {
    const p = Math.max(0, Math.min(100, Math.round(pct)));
    if (node._p === p) return;
    node._p = p;
    fill.style.width = p + '%';
    setTextIfChanged(pctText, p + '%');
    node.classList.toggle('ctx-hi', p >= CTX_HI && p < CTX_CRIT);
    node.classList.toggle('ctx-crit', p >= CTX_CRIT);
  };
  return node;
}

function sessionCostClass(usd) { return usd >= COST_BAD ? ' cost-bad' : usd >= COST_WARN ? ' cost-warn' : ''; }

/* opts: compact (shorter texts), peek (block layout for the dock / sheet), perm (show a pending permission with Allow / Deny),
   showProject (project/repo instead of just the repo), link (the name opens the peek), cls (extra class on the node),
   rich (Home's dense row: model, context meter, compact, worktree / PR / subagents / cost / limit chips, Reply and tail toggles),
   tail (the tail expander; defaults to rich), noWhere (hide the repo text: the block above already names it). */
function sessionCard(s, opts) {
  const o = Object.assign({ compact: false, peek: false, perm: false, showProject: false, link: true, cls: '', rich: false, tail: null, noWhere: false }, opts || {});
  const rich = !!o.rich && !o.peek;
  const wantTail = !o.peek && (o.tail === null || o.tail === undefined ? rich : !!o.tail) && sessionTailAvailable();
  const cur = { s };
  const tmux = s.tmux;
  const killKey = 'kill:' + tmux;
  const glyphs = el('span', { class: o.peek ? 'peek-glyphs' : 'rr-g' });
  const nameNode = o.link && !o.peek ? el('a', { class: 'rr-name', href: sessionHash(tmux) }) : el('span', { class: 'rr-name' });
  const where = el('span', { class: 'rr-where' + (o.noWhere ? ' hidden' : '') });
  const age = el('time', { class: 'age rr-age', title: 'last activity' });
  const meta = el('span', { class: 'rr-meta' });
  const promptNode = el('span', { class: 'dim' });
  const msgNode = el('span', { class: 'rr-msg' });
  const permNote = el('span', { class: 'perm-note' });
  const permBtns = el('span', { class: 'actions perm-btns' });
  const ackSlot = el('span', { class: 'slot-ack' });
  const killSlot = el('span', { class: 'slot-kill' });
  const openLink = el('a', { class: o.peek ? 'btn primary' : 'btn small', href: `/term/${encodeURIComponent(tmux)}`, target: '_blank', rel: 'noopener', text: o.peek ? 'Open terminal' : 'Open' });
  const chips = el('div', { class: 'chips' + (o.peek ? '' : ' rr-chips'), role: 'group', 'aria-label': 'Quick replies' });
  for (const text of SESSION_NUDGES) {
    const b = el('button', { class: 'chip-btn', type: 'button', text });
    b.addEventListener('click', (e) => { e.stopPropagation(); sessionNudge(cur.s, text, b); });
    chips.append(b);
  }
  let sendRow = null;
  if (!o.peek) {                                             // the peek has its own composer (session.js)
    const ta = composer({ placeholder: `send to ${s.name || tmux} · ⇧Enter new line`, label: `send to ${s.name || tmux}`, onSend: () => sessionSend(cur.s, ta) });
    ta.addEventListener('click', (e) => e.stopPropagation());
    sendRow = el('form', { class: 'rr-send', onsubmit: (e) => { e.preventDefault(); e.stopPropagation(); sessionSend(cur.s, ta); } },
      ta, el('button', { class: 'small primary', type: 'submit', text: 'Send', onclick: (e) => e.stopPropagation() }));
  }

  // rich row: badges (built once, shown and patched in place), the Reply and tail toggles
  let badges = null;
  let replyBtn = null;
  let tailBtn = null;
  let tailHost = null;
  let tailPre = null;
  const b = {};
  if (rich) {
    b.model = el('span', { class: 'bdg bdg-model mono hidden' });
    b.ctx = ctxMeter();
    b.ctx.classList.add('hidden');
    b.compact = el('button', { class: 'chip-btn compact-chip hidden', type: 'button', title: 'send /compact to this session', text: 'compact' });
    b.compact.addEventListener('click', (e) => { e.stopPropagation(); sessionNudge(cur.s, '/compact', b.compact); });
    b.worktree = el('span', { class: 'bdg bdg-wt hidden', text: 'worktree' });
    b.pr = el('a', { class: 'bdg bdg-pr hidden', target: '_blank', rel: 'noopener' });
    b.sub = el('span', { class: 'bdg bdg-sub hidden' });
    b.cost = el('span', { class: 'bdg bdg-cost mono hidden', title: 'session cost (API-equivalent)' });
    b.limit = el('span', { class: 'bdg bdg-limit hidden', text: 'limit' });
    b.blocked = el('span', { class: 'bdg bdg-blocked hidden', text: 'blocked' });
    badges = el('span', { class: 'rr-badges' }, b.model, b.ctx, b.compact, b.worktree, b.pr, b.sub, b.cost, b.limit, b.blocked);
    replyBtn = el('button', { class: 'small minimal rr-replybtn', type: 'button', 'aria-expanded': 'false', title: 'quick replies and a send box', text: 'Reply' });
    replyBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const on = !node.classList.contains('open');
      node.classList.toggle('open', on);
      replyBtn.setAttribute('aria-expanded', on ? 'true' : 'false');
    });
  }
  if (wantTail) {
    tailPre = el('pre', { class: 'tail' });
    tailHost = el('div', { class: 'rr-tail hidden' }, tailPre);
    tailBtn = el('button', { class: 'small minimal rr-tailbtn', type: 'button', 'aria-expanded': 'false', title: `last ${TAIL_LINES} lines of the pane`, text: 'Tail' });
    tailBtn.addEventListener('click', (e) => { e.stopPropagation(); if (cur.tailFn) tailOff(); else tailOn(); });
  }

  function tailOn() {
    if (cur.tailFn || !sessionTailAvailable()) return;
    cur.tailFn = (lines) => {
      const text = (Array.isArray(lines) ? lines : []).slice(-TAIL_LINES).join('\n');
      setTextIfChanged(tailPre, text || '(no output yet)');
      tailPre.scrollTop = tailPre.scrollHeight;
    };
    setTextIfChanged(tailPre, 'waiting for output…');
    tailHost.classList.remove('hidden');
    tailBtn.setAttribute('aria-expanded', 'true');
    tailBtn.classList.add('on');
    agentsTails.add(node);
    Live.subscribe(tmux, cur.tailFn);
  }

  function tailOff() {
    const fn = cur.tailFn;
    if (!fn) return;
    cur.tailFn = null;
    agentsTails.delete(node);
    try { if (sessionTailAvailable()) Live.unsubscribe(tmux, fn); } catch (e) { console.error('ccboard tail', e); }
    tailHost.classList.add('hidden');
    tailBtn.setAttribute('aria-expanded', 'false');
    tailBtn.classList.remove('on');
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
    node = el('div', { class: 'peek-card', 'data-tmux': tmux },
      el('div', { class: 'peek-sub' }, glyphs, where, age, meta),
      promptHost, msgHost, permHost,
      el('div', { class: 'peek-actions' }, openLink, ackSlot, killSlot), chips);
  } else {
    node = el('div', { class: 'rrow' + (o.compact ? ' compact' : '') + (rich ? ' rich' : '') + (o.cls ? ' ' + o.cls : ''), 'data-tmux': tmux },
      glyphs, el('div', { class: 'rr-main' }, nameNode, where, age), rich ? el('div', { class: 'rr-metaline' }, meta, badges) : meta,
      el('div', { class: 'rr-last' }, o.perm ? permNote : null, promptNode, msgNode),
      el('div', { class: 'rr-actions' }, o.perm ? permBtns : null, openLink, ackSlot, killSlot, replyBtn, tailBtn), chips, sendRow, tailHost);
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

  const show = (n, on) => n.classList.toggle('hidden', !on);

  function patchBadges(s2, st) {
    const t = s2.stats || {};
    const nudge = sessionNudgeable(s2);
    show(b.model, !!t.model);
    if (t.model) setTextIfChanged(b.model, String(t.model));
    const hasCtx = typeof t.context_pct === 'number';
    show(b.ctx, hasCtx);
    if (hasCtx) b.ctx.ccSet(t.context_pct);
    show(b.compact, hasCtx && t.context_pct >= CTX_HI && sessionAgent(s2) === 'claude' && nudge);
    show(b.worktree, sessionWorktree(s2, st));
    const task = sessionTask(s2, st);
    const prNum = task && task.pr_number;
    show(b.pr, !!prNum);
    if (prNum) {
      const bucket = task.ci && task.ci.bucket;
      setTextIfChanged(b.pr, `PR #${prNum}`);
      b.pr.className = 'bdg bdg-pr' + (bucket === 'fail' ? ' bad' : bucket === 'pass' ? ' ok' : bucket === 'pending' ? ' warn' : '');
      if (task.pr_url) b.pr.setAttribute('href', task.pr_url); else b.pr.removeAttribute('href');
      b.pr.setAttribute('title', `${task.pr_state || 'PR'}${bucket && bucket !== 'none' ? ' · CI ' + bucket : ''}`);
    }
    const nSub = s2.flags && typeof s2.flags.subagents === 'number' ? s2.flags.subagents : 0;
    show(b.sub, nSub > 0);
    if (nSub > 0) setTextIfChanged(b.sub, `${nSub} subagent${nSub === 1 ? '' : 's'}`);
    const usd = typeof t.cost_usd === 'number' ? t.cost_usd : 0;
    show(b.cost, usd > 0);
    if (usd > 0) { setTextIfChanged(b.cost, '$' + (usd >= 100 ? usd.toFixed(0) : usd.toFixed(2))); b.cost.className = 'bdg bdg-cost mono' + sessionCostClass(usd); }
    const rl = rateLimitOf(st);
    const limited = !!(rl && rl.session === s2.tmux);
    show(b.limit, limited);
    if (limited) b.limit.setAttribute('title', `${rl.message || 'rate limited'}${rl.resets_at ? ' · resets ' + new Date(rl.resets_at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : ''}`);
    const job = sessionRegistryJob(s2);
    const blocked = !!(job && job.state === 'blocked');
    show(b.blocked, blocked);
    if (blocked) b.blocked.setAttribute('title', job.needs ? String(job.needs) : 'blocked job');
  }

  /* What the node shows, as one string: when it did not change since the last patch there is nothing to write. The pieces that come from outside
     the session object (a pending permission, the two-tap Kill, the linked task, the rate-limit episode) are part of it. */
  function sigOf(s2, st) {
    const t = rich ? sessionTask(s2, st) : null;
    const rl = rich ? rateLimitOf(st) : null;
    const pr = o.perm ? sessionPerm(s2.tmux) : null;
    return JSON.stringify([s2.state, s2.state_at, s2.name, s2.last_prompt, s2.last_message, s2.needs_attention, s2.agent, s2.launcher, s2.created,
      s2.project, s2.repo, s2.folder, s2.stats, s2.path, s2.flags, s2.task,
      pr ? pr.id + ':' + pr.summary : '', ui.confirm === killKey,
      t ? [t.id, t.pr_number, t.pr_url, t.pr_state, t.mode, t.ci && t.ci.bucket] : null, rl && rl.session === s2.tmux ? [rl.message, rl.resets_at] : null]);
  }

  function patch(s2) {
    cur.s = s2;
    const st = typeof state !== 'undefined' ? state : null;
    const sig = sigOf(s2, st);
    if (node._sig === sig) return;
    node._sig = sig;
    const sk = sessionStateKey(s2);
    const agent = sessionAgent(s2);
    const gk = sk + '|' + agent;
    if (cur.g !== gk) { glyphs.textContent = ''; glyphs.append(stateGlyph(sk), agentGlyph(agent)); cur.g = gk; }
    setTextIfChanged(nameNode, s2.name || tmux);
    setTextIfChanged(where, sessionWhere(s2, o.showProject));
    agentsAgeNode(age, sessionActivity(s2));
    setTextIfChanged(meta, rich ? GLYPH_LABEL[sk] : sessionMetaText(s2, sk));
    const lim = o.peek ? [800, 2000] : (o.compact || rich ? [120, 160] : [200, 320]);
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
    const nudge = sessionNudgeable(s2);
    chips.classList.toggle('hidden', !nudge);
    if (sendRow) sendRow.classList.toggle('hidden', !nudge);
    if (rich) { patchBadges(s2, st); show(replyBtn, nudge); }
  }

  node.ccPatch = patch;
  node.ccDestroy = () => { tailOff(); };
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
