/* ccboard inbox (v0.5.5): what needs you, with the context to answer it. Home's first section and #/inbox are both made of the cards below.

   Inbox.kind(s, st) sorts a session into one of nine kinds, in the order the inbox lists them:
     permission     a permission is pending for it (st.pending_permissions)            Allow / Deny, only when no full terminal client is attached
     plan           waiting, and its last message talks about a plan (/plan|ExitPlanMode|approve/i)
     question       waiting, and its last message ends with a question or has AskUserQuestion
     needs          the Claude registry says its job needs you (flags.registry.job.needs, or tempo 'blocked'), with the suggested reply as a chip
     limit          the account hit a rate limit on it (st.rate_limited names it, still in force) or it errored with a limit message
     error          errored
     done-question  done and not acknowledged, and its last message ends with a question
     waiting        any other waiting
     done           done and not acknowledged
   Inbox.items(st) is the list: every session of one of those kinds that is not acknowledged (needs_attention), plus the ones with a
   pending permission or a blocked job, which acknowledging does not answer; ordered by kind, then oldest first. Each item is the
   roster session ({...s, project, repo, folder}) plus .kind and .perm.
   inboxCard(s, st, opts) builds one keyed, patch-in-place card that LEADS with the context (the plan or question text, the permission
   summary in mono, the job's needs and suggested reply, the limit message with its reset time), then project/repo, session and age, then
   the actions. node.ccPatch(s, st) updates only what changed, so a poll never recreates a card that holds focus or a half-typed reply.
   Inbox.section(host, st, {limit, link}) keyed-reconciles the cards into host (Home) and returns how many items there are.
   The cards carry the legacy 'inbox-item' class beside 'inbox-card': the j/k selection (Pages.rowNodes in pages/home.js) paints that one.
   state.rate_limited is the kv wrapper {value: {session, message, kind, resets_at (epoch s)}, at}; a flat record is read too.
   Classic script: loaded before pages/agents.js, so everything from there (rosterSessions, sessionNudge, ...) is called, never captured. */
'use strict';

const Inbox = {
  // permission first on purpose (a pending permission blocks the agent and times out in 90 s; the Mac's bypass habit does not
  // hold on the box), then the plan's order: plan review, question, job needs, limit, error, done-with-question
  ORDER: ['permission', 'plan', 'question', 'needs', 'limit', 'error', 'done-question', 'waiting', 'done'],
  LABEL: { permission: 'permission', plan: 'plan review', question: 'question', needs: 'job needs you', limit: 'limit hit', error: 'error',
           'done-question': 'done, asks', waiting: 'waiting', done: 'done' },
  PLAN_RE: /\bplan\b|ExitPlanMode/i,
  ASKS_RE: /\?[\s"'`)\]*_]*$|AskUserQuestion/,
  LIMIT_RE: /limit/i,
  HINT: 'j / k move · Enter open · o terminal · a ack · y allow · d deny · r reply · ? all keys',
  // the one action each kind of card leads with (v0.5.6d): the lead card shows it filled, the others tinted
  PRIMARY: { permission: 'allow', plan: 'reply', question: 'reply', 'done-question': 'reply', waiting: 'reply', needs: 'open', error: 'open', limit: 'ack', done: 'ack' },
  CHIPS_SHOWN: 4,                                  // quick-reply chips before the '…' that shows the rest
};

/* ---------- classifying ---------- */

Inbox.message = function (s) { return String((s && s.last_message) || '').trim(); };

Inbox.asks = function (msg) { return Inbox.ASKS_RE.test(msg); };

/* The registry job of a session (flags.registry.job), or null. */
Inbox.job = function (s) {
  const r = s && s.flags && s.flags.registry;
  return r && r.job && typeof r.job === 'object' ? r.job : null;
};

/* Epoch seconds from a number (seconds or milliseconds) or an ISO string; 0 when unknown. */
Inbox.epoch = function (v) {
  if (typeof v === 'number' && Number.isFinite(v)) return v > 1e11 ? v / 1000 : v;
  if (typeof v === 'string' && v) { const n = Number(v); if (Number.isFinite(n)) return Inbox.epoch(n); const t = Date.parse(v); return Number.isNaN(t) ? 0 : t / 1000; }
  return 0;
};

/* The rate-limit record of a state payload as {session, message, kind, resets_at, at}, or null. */
Inbox.limit = function (st) {
  const r = st && st.rate_limited;
  if (!r || typeof r !== 'object') return null;
  const v = r.value && typeof r.value === 'object' ? r.value : r;
  if (!v.session && !v.message && !v.resets_at) return null;
  return { session: String(v.session || ''), message: String(v.message || ''), kind: String(v.kind || ''), resets_at: Inbox.epoch(v.resets_at), at: r.at || v.at || '' };
};

/* Is the limit still in force (no reset time known counts as in force)? */
Inbox.limitActive = function (lim) { return !!lim && (!lim.resets_at || lim.resets_at * 1000 > Date.now()); };

Inbox.pending = function (s, st) {
  for (const pr of ((st && st.pending_permissions) || [])) if (pr.tmux_name === s.tmux) return pr;
  return null;
};

Inbox.kind = function (s, st) {
  if (!s) return '';
  const msg = Inbox.message(s);
  const waiting = s.state === 'waiting';
  if (Inbox.pending(s, st)) return 'permission';
  if (waiting && Inbox.PLAN_RE.test(msg)) return 'plan';
  if (waiting && Inbox.asks(msg)) return 'question';
  const job = Inbox.job(s);
  if (job && (job.needs || job.tempo === 'blocked')) return 'needs';
  const lim = Inbox.limit(st);
  if ((lim && lim.session === s.tmux && Inbox.limitActive(lim) && s.state !== 'working') || (s.state === 'errored' && Inbox.LIMIT_RE.test(msg))) return 'limit';
  if (s.state === 'errored') return 'error';
  if (s.state === 'done' && s.needs_attention && Inbox.asks(msg)) return 'done-question';
  if (waiting) return 'waiting';
  if (s.state === 'done' && s.needs_attention) return 'done';
  return '';
};

Inbox.items = function (st) {
  if (!st || typeof rosterSessions !== 'function') return [];
  const out = [];
  for (const s of rosterSessions(st)) {
    const kind = Inbox.kind(s, st);
    const perm = Inbox.pending(s, st);
    if (!kind || !(perm || kind === 'needs' || s.needs_attention)) continue;   // acknowledged: gone, unless a permission or a blocked job still waits on you
    out.push({ ...s, kind, perm });
  }
  const at = (s) => (typeof sessionActivity === 'function' ? sessionActivity(s) : 0);
  return out.sort((a, b) => Inbox.ORDER.indexOf(a.kind) - Inbox.ORDER.indexOf(b.kind) || at(a) - at(b) || String(a.tmux).localeCompare(String(b.tmux)));
};

Inbox.counts = function (items) {
  const n = {};
  for (const it of items) n[it.kind] = (n[it.kind] || 0) + 1;
  return n;
};

/* ---------- the context a card leads with ---------- */

Inbox.head = function (msg, n) { return msg.length > n ? msg.slice(0, n).trimEnd() + '…' : msg; };

Inbox.tail = function (msg, n) { return msg.length > n ? '…' + msg.slice(-n).trimStart() : msg; };

/* Local clock time of an epoch ('22:05'), with the weekday when it is more than a day away (a 5 h window that resets after midnight is still just a time). */
Inbox.clock = clockAt;

/* {text, mono, note, reply}: the lead text (mono for a permission summary), a dim note beside it, and a suggested reply (needs). */
Inbox.context = function (s, st, kind) {
  const msg = Inbox.message(s);
  const perm = Inbox.pending(s, st);
  if (kind === 'permission') {
    const here = perm && inboxFullViewers(s) > 0;           // a full client has the TUI prompt: the board's buttons would answer nothing
    const text = String((perm && perm.summary) || msg || 'permission request').slice(0, 300);
    const tool = String((perm && perm.tool_name) || '');
    // 'Bash: npm test' already names the tool: the dim line beside it only says it when the summary does not
    const dup = !!tool && text.toLowerCase().startsWith(tool.toLowerCase());
    return { text, mono: true, note: here ? 'a terminal is attached: answer it there' : (dup ? '' : tool), reply: '' };
  }
  if (kind === 'plan') return { text: Inbox.head(msg, 700), mono: false, note: '', reply: '' };
  if (kind === 'question' || kind === 'done-question') return { text: Inbox.tail(msg, 400), mono: false, note: '', reply: '' };
  if (kind === 'needs') {
    const job = Inbox.job(s) || {};
    return { text: String(job.needs || '').trim() || (msg ? Inbox.head(msg, 320) : 'a background job is blocked'), mono: false, note: job.state && job.state !== 'blocked' ? String(job.state) : '',
             reply: String(job.suggested_reply || '').trim().slice(0, 60) };
  }
  if (kind === 'limit') {
    const lim = Inbox.limit(st);
    const mine = lim && lim.session === s.tmux;
    const reset = mine && lim.resets_at ? Inbox.clock(lim.resets_at) : '';
    const parked = limitParkedText(s);                  // #71: what happens next, in words
    return { text: Inbox.head((mine && lim.message) || msg || 'rate limit reached', 320), mono: false, reply: '',
             note: parked || (reset ? `resets ${reset}${lim.resets_at * 1000 > Date.now() ? ' · in ' + fmtIn(lim.resets_at) : ''}` : '') };
  }
  if (kind === 'error') return { text: Inbox.head(msg || 'the session failed', 320), mono: false, note: '', reply: '' };
  if (kind === 'done') return { text: Inbox.head(msg || 'finished', 320), mono: false, note: '', reply: '' };
  return { text: Inbox.head(msg || 'waiting for you', 320), mono: false, note: '', reply: '' };
};

/* Everything a card shows, as one string: a poll that changes none of it leaves the card alone. */
Inbox.sig = function (s, st, kind) {
  const perm = Inbox.pending(s, st);
  const job = Inbox.job(s) || {};
  const lim = Inbox.limit(st) || {};
  return JSON.stringify([kind, s.state, s.state_at, s.needs_attention, s.name, s.agent, s.last_message, s.last_prompt, s.project, s.repo, s.folder,
    perm && [perm.id, perm.summary, perm.tool_name], s.viewers && s.viewers.full, job.needs, job.tempo, job.state, job.suggested_reply,
    lim.session === s.tmux ? [lim.message, lim.resets_at] : null, s.task && s.task.title, limitParkedText(s)]);
};

/* ---------- the card ---------- */

function inboxFullViewers(s) { return Number((s.viewers && s.viewers.full) || 0); }

/* v0.5.6d: ONE filled primary per screen. node.ccLead(on) says whether this card is the lead (the j/k selection, else the first card: Pages.markLead in
   pages/home.js decides): the lead card shows its kind's primary action filled, the quick-reply chips and the send box; every other card shows the same
   action tinted and keeps the chips and the send box behind a quiet Reply (class `open` on the card). The primary per kind is Inbox.PRIMARY: Allow for a
   permission, Reply for a question or a plan, Open for a job that needs you or an error, Ack for a limit or a finished session. */
function inboxCard(s, st, opts) {
  const o = Object.assign({ cls: '', label: true }, opts || {});
  const cur = { s, st, kind: '', lead: true, allow: false, nudge: false, ack: false, actKey: '' };
  const tmux = s.tmux;
  const name = s.name || tmux;
  const kindNode = el('span', { class: 'ib-kind' });
  const ctx = el('div', { class: 'ib-ctx' });
  const note = el('span', { class: 'ib-note dim' });
  const reply = el('span', { class: 'ib-reply' });
  const glyphs = el('span', { class: 'ib-glyphs' });
  const nameNode = el('a', { class: 'ib-name', href: typeof sessionHash === 'function' ? sessionHash(tmux) : '#/s/' + tmux });
  const where = el('span', { class: 'ib-where' });
  const task = el('strong', { class: 'ib-task' });
  const age = el('time', { class: 'age ib-age', title: 'last activity' });
  const prompt = el('div', { class: 'ib-prompt dim' });
  const permBtns = el('span', { class: 'actions perm-btns' });
  const replySlot = el('span', { class: 'slot-reply' });
  const openSlot = el('span', { class: 'slot-open' });
  const ackSlot = el('span', { class: 'slot-ack' });
  const chips = el('div', { class: 'chips ib-chips', role: 'group', 'aria-label': 'Quick replies' });
  quickLoad(tmux, sessionAgent(s)).forEach((text, i) => {                // #69: the session's quick replies, its agent's defaults until edited
    const b = el('button', { class: 'chip-btn' + (i >= Inbox.CHIPS_SHOWN ? ' chip-extra' : ''), type: 'button', title: `types "${text}" into this session as a prompt`, text });
    b.addEventListener('click', (e) => { e.stopPropagation(); sessionNudge(cur.s, text, b); });
    chips.append(b);
  });
  const moreChips = el('button', { class: 'chip-btn chip-more', type: 'button', 'aria-label': 'More quick replies', title: 'more quick replies', text: '…' });
  moreChips.addEventListener('click', (e) => { e.stopPropagation(); chips.classList.add('all'); });
  chips.append(moreChips);
  const ta = composer({ placeholder: sessionPlaceholder('reply to', name), label: `reply to ${name}`, onSend: () => sessionSend(cur.s, ta) });
  ta.setAttribute('title', `reply to ${name}: Enter sends, Shift+Enter adds a line`);
  ta.addEventListener('click', (e) => e.stopPropagation());
  const sendRow = el('form', { class: 'ib-send', onsubmit: (e) => { e.preventDefault(); e.stopPropagation(); sessionSend(cur.s, ta); } },
    ta, el('button', { class: 'small primary', type: 'submit', text: 'Send', onclick: (e) => e.stopPropagation() }));
  const node = el('div', { class: 'inbox-card inbox-item lead' + (o.cls ? ' ' + o.cls : ''), 'data-tmux': tmux },
    el('div', { class: 'ib-lead' }, o.label ? kindNode : null, ctx, el('div', { class: 'ib-extra' }, note, reply)),
    el('div', { class: 'ib-sub' }, glyphs, where, nameNode, task, age),
    prompt,
    el('div', { class: 'ib-actions' }, permBtns, replySlot, openSlot, ackSlot),
    chips, sendRow);

  /* The one action this card leads with, falling back to Open when the preferred one is not there (no Allow while a terminal is attached, no Reply to a
     shell or an ended pane, no Ack once acknowledged). */
  function primaryRole() {
    const want = Inbox.PRIMARY[cur.kind] || 'open';
    if (want === 'allow') return cur.allow ? 'allow' : (cur.nudge ? 'reply' : 'open');
    if (want === 'reply') return cur.nudge ? 'reply' : 'open';
    if (want === 'ack') return cur.ack ? 'ack' : 'open';
    return 'open';
  }

  function replyVisible() { return node.classList.contains('lead') || node.classList.contains('open'); }
  function syncReply() {
    const b = replySlot.firstElementChild;
    if (b) b.setAttribute('aria-expanded', replyVisible() ? 'true' : 'false');
  }

  function toggleReply(e) {
    e.stopPropagation();
    if (!node.classList.contains('lead')) node.classList.toggle('open');
    syncReply();
    if (replyVisible() && typeof ta.focus === 'function') ta.focus();
  }

  /* The action buttons, rebuilt only when their variant changes (a poll that changes nothing here leaves them, and their focus, alone). */
  function paintActions() {
    const lead = cur.lead !== false;
    const role = primaryRole();
    const v = (r) => (role === r ? (lead ? 'primary' : 'primary tinted') : '');
    const key = [cur.allow ? cur.permSig : 0, cur.nudge ? 1 : 0, cur.ack ? 1 : 0, v('allow'), v('reply'), v('open'), v('ack')].join('|');
    if (cur.actKey === key) return;
    cur.actKey = key;
    permBtns.textContent = '';
    if (cur.allow) {
      const pr = cur.perm;
      permBtns.append(
        el('button', { class: `${v('allow') || 'primary'} small`, type: 'button', onclick: (e) => { e.stopPropagation(); decide(pr.id, 'allow'); }, text: 'Allow' }),
        el('button', { class: 'danger small', type: 'button', onclick: (e) => { e.stopPropagation(); decide(pr.id, 'deny'); }, text: 'Deny' }));
    }
    permBtns.classList.toggle('hidden', !cur.allow);
    replySlot.textContent = '';
    if (cur.nudge) replySlot.append(el('button', { class: `${v('reply') || 'minimal'} small ib-replybtn`, type: 'button', title: 'quick replies and a send box', onclick: toggleReply, text: 'Reply' }));
    syncReply();
    openSlot.textContent = '';
    openSlot.append(el('a', { class: ['btn', v('open'), 'small'].filter(Boolean).join(' '), href: `/term/${encodeURIComponent(tmux)}`, target: '_blank', rel: 'noopener', text: 'Open' }));
    ackSlot.textContent = '';
    if (cur.ack) ackSlot.append(el('button', { class: [v('ack'), 'small'].filter(Boolean).join(' '), type: 'button', onclick: (e) => { e.stopPropagation(); sessionAck(cur.s); }, text: 'Ack' }));
  }

  function patchReply(text) {
    if (cur.reply === text) return;
    cur.reply = text;
    reply.textContent = '';
    if (text) {
      const b = el('button', { class: 'chip-btn ib-suggest', type: 'button', title: 'the reply the job suggests', text });
      b.addEventListener('click', (e) => { e.stopPropagation(); sessionNudge(cur.s, text, b); });
      reply.append(b);
    }
    reply.classList.toggle('hidden', !text);
  }

  function patch(s2, st2) {
    cur.s = s2;
    cur.st = st2;
    const kind = Inbox.kind(s2, st2);
    const sig = Inbox.sig(s2, st2, kind);
    if (node._sig === sig) return;
    node._sig = sig;
    if (cur.kind !== kind) {
      if (cur.kind) node.classList.remove('kind-' + cur.kind);
      if (kind) node.classList.add('kind-' + kind);
      cur.kind = kind;
      setTextIfChanged(kindNode, Inbox.LABEL[kind] || '');
    }
    const st3 = typeof sessionStateKey === 'function' ? sessionStateKey(s2) : (s2.state || 'unknown');
    const agent = typeof sessionAgent === 'function' ? sessionAgent(s2) : (s2.agent || 'claude');
    const gk = st3 + '|' + agent;
    if (cur.g !== gk) {
      const ag = agentGlyph(agent);
      if (typeof chipHue === 'function') ag.classList.add(chipHue('agent', agent));
      glyphs.textContent = '';
      glyphs.append(stateGlyph(st3), ag);
      cur.g = gk;
    }
    const c = Inbox.context(s2, st2, kind);
    setTextIfChanged(ctx, c.text);
    ctx.classList.toggle('mono', c.mono);
    ctx.setAttribute('title', Inbox.message(s2).slice(0, 2000));
    setTextIfChanged(note, c.note);
    note.classList.toggle('hidden', !c.note);
    patchReply(c.reply);
    setTextIfChanged(where, typeof sessionWhere === 'function' ? sessionWhere(s2, true) : `${s2.project}/${s2.repo || '?'}`);
    if (s2.project && typeof chipHue === 'function') chipHueSet(where, chipHue('project', s2.project));
    setTextIfChanged(nameNode, s2.name || tmux);
    const title = s2.task && s2.task.title ? String(s2.task.title).slice(0, 120) : '';
    setTextIfChanged(task, title);
    task.classList.toggle('hidden', !title);
    const pt = s2.last_prompt ? '› ' + String(s2.last_prompt).slice(0, 200) : '';
    setTextIfChanged(prompt, pt);
    prompt.classList.toggle('hidden', !pt);
    agentsAgeNode(age, sessionActivity(s2));
    node.classList.toggle('attn', !!s2.needs_attention || kind === 'permission' || kind === 'needs');
    const pr = Inbox.pending(s2, st2);
    cur.perm = pr;
    cur.allow = !!pr && inboxFullViewers(s2) === 0;           // a full terminal client has the TUI prompt in front of it: no remote buttons
    cur.ack = !!s2.needs_attention;
    cur.nudge = sessionNudgeable(s2);
    if (cur.allow) cur.permSig = `${pr.id}:${pr.summary || ''}`;
    paintActions();
    chips.classList.toggle('hidden', !cur.nudge);
    sendRow.classList.toggle('hidden', !cur.nudge);
  }

  node.ccPatch = patch;
  node.ccCanLead = () => true;
  node.ccLead = (on) => {
    if (cur.lead === on) return;
    cur.lead = on;
    node.classList.toggle('lead', on);
    paintActions();
    syncReply();
  };
  patch(s, st);
  return node;
}

/* ---------- the section Home puts first ---------- */

/* Inbox.section(host, st, {limit, link}) -> number of items (all of them, not just the ones shown). Builds its parts into host on the first
   call and patches them after; host is hidden while the inbox is empty. limit 0 shows everything; link is where "Show all" goes. */
Inbox.section = function (host, st, opts) {
  const o = Object.assign({ limit: 0, link: '' }, opts || {});
  const items = Inbox.items(st);
  let sec = host._inbox;
  if (!sec) {
    const title = el('h2', { class: 'ib-title' });
    const more = el('a', { class: 'btn small ib-more', href: o.link || '#/inbox' });
    const list = el('div', { class: 'roster inbox-list' });
    sec = { title, more, list, st: null, kl: null };
    sec.kl = makeKeyedList(list, { key: (s) => s.tmux, create: (s) => inboxCard(s, sec.st, {}), patch: (n, s) => n.ccPatch(s, sec.st) });
    host.append(el('div', { class: 'row head ib-head' }, title, more), list);
    host._inbox = sec;
  }
  sec.st = st;
  const shown = o.limit > 0 ? items.slice(0, o.limit) : items;
  sec.kl.update(shown);
  if (typeof Pages !== 'undefined' && Pages && typeof Pages.markLead === 'function') Pages.markLead();   // the lead card leads before the first paint
  setTextIfChanged(sec.title, `Needs you (${items.length})`);
  sec.more.setAttribute('href', o.link || '#/inbox');
  setTextIfChanged(sec.more, items.length > shown.length ? `Show all ${items.length}` : 'Open inbox');
  sec.more.classList.toggle('hidden', !o.link);
  sec.list.classList.toggle('cv-auto', shown.length > CV_AUTO_ROWS);
  host.classList.toggle('hidden', !items.length);
  return items.length;
};

/* ---------- #/inbox ---------- */

const inboxPage = { refs: null, items: [], st: null };

function inboxTitle() {
  const n = typeof state !== 'undefined' && state ? Inbox.items(state).length : 0;
  return (n ? `(${n}) ` : '') + 'Needs you';
}

function inboxSummary(items) {
  if (!items.length) return '';
  const counts = Inbox.counts(items);
  const parts = [`${STATE_GLYPH.waiting} ${items.length} need${items.length === 1 ? 's' : ''} you`];
  for (const k of Inbox.ORDER) if (counts[k]) parts.push(`${counts[k]} ${Inbox.LABEL[k]}`);
  return parts.join(' · ');
}

registerPage('inbox', {
  title: () => inboxTitle(),
  mount(root) {
    const summary = el('p', { class: 'summary' });
    const hint = el('p', { class: 'hint', text: Inbox.HINT });
    const list = el('div', { class: 'roster inbox-list' });
    Pages.reset();
    const none = pageEmpty('inbox', 'Nothing needs you', 'Sessions that wait for you, finish or fail show up here until you acknowledge them.');
    root.append(el('div', { class: 'inbox-page' }, el('div', { class: 'page-head' }, el('h1', { text: 'Needs you' }), summary), hint, list, none));
    // a click on a card selects it (the keyboard's selection walks Pages.items(), so look the session up by name, not by position)
    const select = (tmux) => {
      const i = Pages.items().findIndex((x) => x.tmux === tmux);
      if (i < 0) return;
      Pages.setIndex(i);
      repaintPage();
    };
    inboxPage.refs = { summary, hint, none, listNode: list, list: makeKeyedList(list, {
      key: (s) => s.tmux,
      create: (s) => {
        const n = inboxCard(s, inboxPage.st, {});
        n.addEventListener('click', () => select(s.tmux));
        return n;
      },
      patch: (n, s) => n.ccPatch(s, inboxPage.st),
    }) };
    startAgeTicker();
  },
  update(st) {
    const r = inboxPage.refs;
    if (!r) return;
    const items = Inbox.items(st);
    inboxPage.st = st;
    inboxPage.items = items;
    Pages.sync();
    setTextIfChanged(r.summary, inboxSummary(items));
    r.list.update(items);
    r.listNode.classList.toggle('cv-auto', items.length > CV_AUTO_ROWS);       // long lists skip the layout of rows that are off screen
    Pages.paint();
    r.none.classList.toggle('hidden', items.length > 0);
    r.hint.classList.toggle('hidden', !items.length);
    Nodes.use(() => Nodes.slot('inbox', r.listNode, { none: r.none, local: items.length }));     // paired nodes: "On other nodes" under the local cards (nodes-hub.js)
    refreshTitle();
  },
  unmount() {
    inboxPage.refs = null;
    inboxPage.items = [];
    inboxPage.st = null;
    stopAgeTicker();
  },
});
