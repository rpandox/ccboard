/* Shared Home / Inbox cards, loaded with their views. */
'use strict';
/* ---------- the context a card leads with ---------- */

Inbox.head = function (msg, n) { return msg.length > n ? msg.slice(0, n).trimEnd() + '…' : msg; };

Inbox.tail = function (msg, n) { return msg.length > n ? '…' + msg.slice(-n).trimStart() : msg; };

/* Local clock time of an epoch ('22:05'), with the weekday when it is more than a day away (a 5 h window that resets after midnight is still just a time). */
Inbox.clock = clockAt;

/* {text, mono, note, reply}: the lead text (mono for a permission summary), a dim note beside it, and a suggested reply (needs). */
Inbox.context = function (s, st, kind) {
  const msg = Inbox.message(s);
  const perm = Inbox.pending(s, st);
  if (kind === 'permission' || kind === 'terminal_prompt') {
    const here = kind !== 'terminal_prompt' && perm && inboxFullViewers(s) > 0;           // a live hook defers to the attached terminal; a terminal_prompt answers the pane itself
    const text = String((perm && perm.summary) || msg || 'permission request').slice(0, 300);
    const tool = String((perm && perm.tool_name) || '');
    // 'Bash: npm test' already names the tool: the dim line beside it only says it when the summary does not
    const dup = !!tool && text.toLowerCase().startsWith(tool.toLowerCase());
    const choices = kind === 'terminal_prompt' ? (perm.choices || []).map((c) => `${c.number}. ${c.label}`).join(' · ') : '';
    return { text, mono: true, note: choices || (here ? 'a terminal is attached: answer it there' : (dup ? '' : tool)), reply: '' };
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
    perm && [perm.id, perm.summary, perm.tool_name, perm.choices, permMark(perm)], s.viewers && s.viewers.full, job.needs, job.tempo, job.state, job.suggested_reply,
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
      permBtns.append(...permButtons(pr, `${v('allow') || 'primary'} small`, 'danger small'));
    }
    permBtns.classList.toggle('hidden', !cur.allow);
    replySlot.textContent = '';
    if (cur.nudge) replySlot.append(el('button', { class: `${v('reply') || 'minimal'} small ib-replybtn`, type: 'button', title: 'quick replies and a send box', onclick: toggleReply, text: 'Reply' }));
    syncReply();
    openSlot.textContent = '';
    if (cur.s.bare_device) openSlot.append(el('button', { class: 'small', type: 'button', text: 'Terminal', onclick: () => bareTerminal(cur.s) }));
    else openSlot.append(el('a', { class: ['btn', v('open'), 'small'].filter(Boolean).join(' '), href: `/term/${encodeURIComponent(tmux)}`, target: '_blank', rel: 'noopener', text: 'Open' }));
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
    if (s2.bare_device) { nameNode.removeAttribute('href'); nameNode.onclick = () => bareTerminal(cur.s); }
    const title = s2.task && s2.task.title ? String(s2.task.title).slice(0, 120) : '';
    setTextIfChanged(task, title);
    task.classList.toggle('hidden', !title);
    const pt = s2.last_prompt ? '› ' + String(s2.last_prompt).slice(0, 200) : '';
    setTextIfChanged(prompt, pt);
    prompt.classList.toggle('hidden', !pt);
    agentsAgeNode(age, sessionActivity(s2));
    node.classList.toggle('attn', !!s2.needs_attention || kind === 'permission' || kind === 'terminal_prompt' || kind === 'needs');
    const pr = Inbox.pending(s2, st2);
    cur.perm = pr;
    cur.allow = !!pr && (pr.kind === 'terminal_prompt' || inboxFullViewers(s2) === 0);           // terminal prompts remain answerable with a full client attached
    cur.ack = !s2.bare_device && !!s2.needs_attention;
    cur.nudge = sessionNudgeable(s2);
    if (cur.allow) cur.permSig = `${pr.id}:${pr.summary || ''}:${permMark(pr)}`;
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

