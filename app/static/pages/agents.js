/* ccboard agents page (#/agents) and the session card every session list shares. The page itself (summary, roster groups, the Codex threads section, registerPage)
   is pages/agents-page.js, a lazy bundle (lazy.js); this file keeps what the first paint reads.
   sessionCard(s, opts) builds one session as a keyed, patch-in-place node: the roster here, the inbox page and the session peek
   all use it (row layout by default, opts.peek for the peek's block layout), so the glyphs, age, model and context, last
   prompt and message, Open / Ack / Kill and the nudge chips look and behave the same everywhere. The node keeps references to
   its parts and node.ccPatch(session) updates only what changed, so a poll never recreates a row that holds focus.
   The roster groups the live sessions by project, blocked sessions first. Classic script: globals are prefixed to stay unique.
   opts.rich (Home) adds the dense row: a model chip, a context meter with a one-tap compact chip, worktree / PR / subagents / cost / limit /
   blocked chips and a tail expander (the last 12 pane lines through Live.subscribe); the plain row (Agents, Inbox) is unchanged. */
'use strict';

// the nudge chips are the session's quick replies (components.js quickLoad: the edited list, else QUICK_DEFAULTS_BY_AGENT[its agent]; #69)
function sessionPlaceholder(verb, name) {
  let coarse = false;
  try { coarse = document.documentElement.classList.contains('force-coarse') || (typeof matchMedia === 'function' && !!matchMedia('(pointer:coarse)').matches); } catch (_) { coarse = false; }
  const who = name ? ' ' + name : '';
  const bare = String(verb).split(/\s+/)[0];
  return coarse ? `${bare.charAt(0).toUpperCase()}${bare.slice(1)}…` : `${verb}${who} · ⇧Enter new line`;
}

/* A send box was emptied from code (no input event fired): let its listeners (the auto-grow, the row's has-text class) see it. */
function rowCleared(ta) {
  if (typeof composerGrow === 'function') composerGrow(ta);
  try { if (typeof ta.dispatchEvent === 'function' && typeof Event === 'function') ta.dispatchEvent(new Event('input')); } catch (_) { /* no events here */ }
}

/* Every live session in a state payload, each with its project, repo and whether it sits in the project folder. */

/* Last activity as epoch seconds: state_at is an ISO string, created is already epoch seconds. */




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

/* #84: auto-continue means something for an agent session that is still open (a shell has no turn to continue, an ended row no prompt) */
function autoContinueApplies(s) { return sessionAgent(s) !== 'shell' && s.state !== 'ended'; }
/* A device session takes a typed reply only at its input prompt: the server refuses one in any other state. */
function sessionNudgeable(s) { if (s.bare_device) return s.state === 'waiting'; return sessionAgent(s) !== 'shell' && SESSION_NUDGE_STATES.includes(s.state); }

/* The active rate-limit episode of a state payload, or null. state.rate_limited is the kv record, {value: {session, message, kind, resets_at}, at}
   as the poll serves it, or the bare {session, message, kind, resets_at}: both shapes read the same. An episode is over once resets_at has passed
   (resets_at is epoch seconds); without one it lasts 5 hours from `at`. */

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

/* The permission waiting on a session. `waiting` (the keys, the nudges: whatever would send an answer): a permission whose answer is already out reads as not
   waiting; the cards pass nothing, so they keep the permission on screen as allowing… / denying… (#318). */
function sessionPerm(tmux, waiting) {
  if (!state) return null;
  for (const pr of (state.pending_permissions || [])) if (pr.tmux_name === tmux && !(waiting && permMark(pr))) return pr;
  return null;
}

/* 10x pass: the row echoes a sent text at once ('› continue'); the next poll's last_prompt is the same text. `from` is any node inside the row. Returns the undo (#311). */
function sessionEcho(from, text) {
  const row = from && typeof from.closest === 'function' ? from.closest('.rrow, .peek, .inbox-card') : null;
  const line = row && row.querySelector('.rr-last .dim, .peek-block .dim');
  const before = line && line.textContent;
  if (line) setTextIfChanged(line, '› ' + String(text).split('\n')[0].slice(0, 160));
  return () => { if (line) setTextIfChanged(line, before); };
}

async function sessionNudge(s, text, btn) {
  if (!s || (btn && btn.disabled)) return;
  if (btn) btn.disabled = true;
  try {
    await api('POST', s.bare_device ? `/api/bare/${encodeURIComponent(s.bare_device)}/sessions/${encodeURIComponent(s.tmux)}/reply` : `/api/sessions/${encodeURIComponent(s.tmux)}/keys`, s.bare_device ? { text } : { text, enter: true });
    pageToast(`sent "${text}" to ${s.name || s.tmux}`, 'ok');
    sessionEcho(btn, text);
  } catch (e) { pageToast(e.message, 'bad'); }
  finally { if (btn) btn.disabled = false; }
}

/* The row's own send box (components.js composer): Enter sends, Shift+Enter adds a line; the draft survives polls because the
   row node is kept and patched. The text goes through /keys like the chips (multi-line arrives as one bracketed paste). */
async function sessionSend(s, ta) {
  const text = String(ta.value || '').replace(/\r\n?/g, '\n');
  if (!s || !text.trim()) return;
  const typed = ta.value;
  ta.value = '';                                               // #311: empty at once; a refusal puts text and echo back
  rowCleared(ta);
  const undo = sessionEcho(ta, text);
  try {
    await api('POST', s.bare_device ? `/api/bare/${encodeURIComponent(s.bare_device)}/sessions/${encodeURIComponent(s.tmux)}/reply` : `/api/sessions/${encodeURIComponent(s.tmux)}/keys`, s.bare_device ? { text } : { text, enter: true });
    pageToast(`sent to ${s.name || s.tmux}`, 'ok');
  } catch (e) {
    if (!ta.value) { ta.value = typed; rowCleared(ta); }
    undo();
    pageToast(errorText(e), 'bad');
  }
}

async function sessionAck(s) {
  const ok = await actPost('ack:' + s.tmux, '', `/api/sessions/${encodeURIComponent(s.tmux)}/ack`, undefined,
    (on) => { ackFlag(state, s.tmux, !on); (typeof render === 'function' ? render : repaintPage)(true); }, `${s.name || s.tmux} was not acknowledged`);        // #313: the card and the counts go at the click, a refusal brings them back
  await poll(true);
  return ok;
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
   rich (the dense row: model, context meter, compact, worktree / PR / subagents / cost / limit chips; the nudge chips and the send box stay behind
   the row's own `...` menu > Reply), tail (the tail expander in that menu; defaults to rich), noWhere (hide the repo text: the block above already
   names it), autoOpen (Agents: a waiting row keeps its chips and send box open until the person toggles them).
   v0.5.6d: a row's actions are [Allow / Deny while a permission waits] [Open] [...]: Ack, Reply, Tail and Kill live in the menu and Kill's second tap
   shows inline. node.ccLead(on) says whether this row carries the screen's one filled primary (Allow); the others show it tinted. Only the peek keeps
   Open terminal / Ack / Kill in view. */
function sessionCard(s, opts) {
  const o = Object.assign({ compact: false, peek: false, perm: false, showProject: false, link: true, cls: '', rich: false, tail: null, noWhere: false, autoOpen: false }, opts || {});
  const rich = !!o.rich && !o.peek;
  const wantTail = !o.peek && (o.tail === null || o.tail === undefined ? rich : !!o.tail) && sessionTailAvailable();
  const cur = { s, lead: undefined, pr: null, manual: false };
  const tmux = s.tmux;
  const killKey = 'kill:' + tmux;
  const glyphs = el('span', { class: o.peek ? 'peek-glyphs' : 'rr-g' });
  const nameNode = o.link && !o.peek ? el('a', { class: 'rr-name', href: sessionHash(tmux) }) : el('span', { class: 'rr-name' });
  const where = el('span', { class: 'rr-where' + (o.noWhere ? ' hidden' : '') });
  const age = el('time', { class: 'age rr-age', title: 'last activity' });
  const meta = el('span', { class: 'rr-meta' });
  const promptNode = el('span', { class: 'dim' });
  const msgNode = el('span', { class: 'rr-msg' });
  const permNote = el('span', { class: 'perm-note' + (o.peek ? ' mono' : '') });      // the peek shows what is asked the way a notification does: `Bash: npm test` in mono
  const taskNode = el('strong', { class: 'peek-task' });
  const permBtns = el('span', { class: 'actions perm-btns' });
  const ackSlot = el('span', { class: 'slot-ack' });
  const killSlot = el('span', { class: 'slot-kill' });
  const openSlot = el('span', { class: 'slot-open' });     // the peek's Open terminal: the primary only while no permission waits
  const openHref = `/term/${encodeURIComponent(tmux)}`;
  const openNode = (primary) => el('a', { class: 'btn' + (primary ? ' primary' : ''), href: openHref, target: '_blank', rel: 'noopener', text: 'Open terminal' });
  const openLink = o.peek ? null : el('a', { class: 'btn small', href: openHref, target: '_blank', rel: 'noopener', text: 'Open' });
  if (o.peek) { openSlot.append(openNode(true)); cur.openP = true; }
  const chips = el('div', { class: 'chips' + (o.peek ? '' : ' rr-chips'), role: 'group', 'aria-label': 'Quick replies' });
  for (const text of quickLoad(tmux, sessionAgent(s))) {
    const b = el('button', { class: 'chip-btn', type: 'button', title: `types "${text}" into this session as a prompt`, text });
    b.addEventListener('click', (e) => { e.stopPropagation(); sessionNudge(cur.s, text, b); });
    chips.append(b);
  }
  let sendRow = null;
  if (!o.peek) {                                             // the peek has its own composer (session.js)
    const ta = composer({ placeholder: sessionPlaceholder('send to', s.name || tmux), label: `send to ${s.name || tmux}`, onSend: () => sessionSend(cur.s, ta) });
    ta.setAttribute('title', `send to ${s.name || tmux}: Enter sends, Shift+Enter adds a line`);
    ta.addEventListener('click', (e) => e.stopPropagation());
    sendRow = el('form', { class: 'rr-send', onsubmit: (e) => { e.preventDefault(); e.stopPropagation(); sessionSend(cur.s, ta); } },
      ta, el('button', { class: 'small primary', type: 'submit', text: 'Send', onclick: (e) => e.stopPropagation() }));
  }

  // rich row: badges (built once, shown and patched in place), the `...` menu with Reply and Tail
  let badges = null;
  let moreBtn = null;
  let tailHost = null;
  let tailPre = null;
  const b = {};
  if (rich) {
    b.model = el('span', { class: 'bdg bdg-model mono hidden' });
    b.acct = el('span', { class: 'bdg bdg-acct hidden' });                    // which subscription account the session runs on: only when the board has more than one
    b.ctx = ctxMeter();
    b.ctx.classList.add('hidden');
    b.compact = el('button', { class: 'chip-btn compact-chip hue-slate hidden', type: 'button', title: 'send /compact to this session', text: 'compact' });
    b.compact.addEventListener('click', (e) => { e.stopPropagation(); sessionNudge(cur.s, '/compact', b.compact); });
    b.worktree = el('span', { class: 'bdg bdg-wt hue-slate hidden', text: 'worktree' });
    b.pr = el('a', { class: 'bdg bdg-pr hue-slate hidden', target: '_blank', rel: 'noopener' });
    b.sub = el('span', { class: 'bdg bdg-sub hue-slate hidden' });
    b.cost = el('span', { class: 'bdg bdg-cost mono hidden', title: 'session cost (API-equivalent)' });
    b.queued = queuedPromptsChip();                                          // #79: '+2 queued', only the prompts the board itself holds for this session
    b.limit = el('span', { class: 'bdg bdg-limit hidden', text: 'limit' });
    b.blocked = el('span', { class: 'bdg bdg-blocked hidden', text: 'blocked' });
    b.hooks = el('span', { class: 'rr-hooks hidden' });                     // #96: 'no hooks (untrusted?)' (hooksMissingChip), a link to the Doctor
    b.noauto = el('span', { class: 'bdg bdg-noauto hidden', title: 'Auto-continue is off for this session: the board will not type continue after a limit reset or a reboot' },
      el('span', { class: 'bdg-noauto-g', 'aria-hidden': 'true', text: '⊘' }), ' no auto-continue');       // #84: only while the session is opted out
    badges = el('span', { class: 'rr-badges' }, b.model, b.acct, b.ctx, b.compact, b.worktree, b.pr, b.sub, b.queued, b.cost, b.limit, b.blocked, b.noauto, b.hooks);
  }
  // the peek says it in words too (a title is no help on touch): the chip and its explanation, only while hooks_missing
  const hooksNote = o.peek ? el('div', { class: 'peek-hooks hidden', role: 'status' }) : null;
  if (!o.peek) {
    moreBtn = el('button', { class: 'icon minimal rr-more', type: 'button', 'aria-label': 'More actions', title: 'More: acknowledge, reply, tail, add to quad, kill' }, ic('more'));
    moreBtn.addEventListener('click', (e) => e.stopPropagation());
    if (typeof menu === 'function') menu(moreBtn, () => moreItems());
  }
  if (wantTail) {
    tailPre = el('pre', { class: 'tail' });
    tailHost = el('div', { class: 'rr-tail hidden' }, tailPre);
  }

  /* The `...` menu, read when it opens: only what applies to the session right now. Kill is the first tap of the two-tap button: it asks the row to
     show 'Confirm Kill' / 'Cancel' inline, where the second tap (or Esc / Cancel) decides. */
  function moreItems() {
    const s2 = cur.s;
    const items = [];
    if (s2.needs_attention) items.push({ label: 'Acknowledge', icon: 'tick', onClick: () => sessionAck(cur.s) });
    if (rich && sessionNudgeable(s2)) items.push({ label: node.classList.contains('open') ? 'Hide reply box' : 'Reply', icon: 'comment', onClick: toggleReply });
    if (wantTail) items.push({ label: cur.tailFn ? 'Hide tail' : 'Tail', icon: 'console', onClick: () => { if (cur.tailFn) tailOff(); else tailOn(); } });
    // the Quad view (v0.5.9) needs 840 px: the row offers it where tiles fit (Shell.quadAdd -> Quad.addToQuad: the session joins the saved slots and #/quad opens)
    if (s2.state !== 'ended' && typeof Shell !== 'undefined' && Shell && typeof Shell.wide === 'function' && Shell.wide() && typeof Shell.quadAdd === 'function') {
      items.push({ label: 'Add to quad', icon: 'layout-grid', onClick: () => Shell.quadAdd(tmux) });
    }
    if (rich && autoContinueApplies(s2)) {                  // #84: one switch per session (the rich rows wear its chip); the label names the current state, a tap flips it
      const off = sessionAutoContinueOff(s2);
      const boardOff = boardAutoContinueOff();
      items.push({ label: autoContinueLabel(off), icon: off ? 'disable' : 'fast-forward', sub: boardOff ? 'off for the whole board (CCBOARD_AUTO_CONTINUE=0)' : AUTO_CONTINUE_SUB,
        title: AUTO_CONTINUE_WHAT + (boardOff ? ' ' + AUTO_CONTINUE_BOARD_OFF : ''), onClick: () => toggleAutoContinue() });
    }
    items.push({ label: 'Kill', icon: 'trash', onClick: () => { ui.confirm = killKey; if (typeof repaintPage === 'function') repaintPage(); } });
    return items;
  }

  /* #84: optimistic flip of flags.no_autoresume on this row's own copy (the next poll replaces it with the server's), put back when the board refuses */
  function toggleAutoContinue() {
    const want = !sessionAutoContinueOff(cur.s);
    return setAutoContinue(tmux, want, { apply: (off) => {
      const flags = Object.assign({}, cur.s.flags || {});
      if (off) flags.no_autoresume = true; else delete flags.no_autoresume;
      cur.s = Object.assign({}, cur.s, { flags });
      if (rich) patchBadges(cur.s, typeof state !== 'undefined' ? state : null);
    } });
  }

  function toggleReply() { cur.manual = true; node.classList.toggle('open'); syncMore(); }

  /* the `...` button wears the accent while the row's reply box or tail is open */
  function syncMore() { if (moreBtn) moreBtn.classList.toggle('on', (rich && node.classList.contains('open')) || !!cur.tailFn); }

  function tailOn() {
    if (cur.tailFn || !sessionTailAvailable()) return;
    cur.tailFn = (lines) => {
      const text = (Array.isArray(lines) ? lines : []).slice(-TAIL_LINES).join('\n');
      setTextIfChanged(tailPre, text || '(no output yet)');
      tailPre.scrollTop = tailPre.scrollHeight;
    };
    setTextIfChanged(tailPre, 'waiting for output…');
    tailHost.classList.remove('hidden');
    agentsTails.add(node);
    syncMore();
    Live.subscribe(tmux, cur.tailFn);
  }

  function tailOff() {
    const fn = cur.tailFn;
    if (!fn) return;
    cur.tailFn = null;
    agentsTails.delete(node);
    try { if (sessionTailAvailable()) Live.unsubscribe(tmux, fn); } catch (e) { console.error('ccboard tail', e); }
    tailHost.classList.add('hidden');
    syncMore();
  }

  let node;
  let promptHost = promptNode;
  let msgHost = msgNode;
  let permHost = null;                                       // what to hide when no permission is pending (peek: its own block)
  let taskHost = null;                                       // the peek's Task block: the task card behind the session, hidden without one
  if (o.peek) {
    const block = (label, body, extra) => el('div', { class: 'peek-block' + (extra || '') }, el('span', { class: 'k', text: label }), body);
    taskHost = block('Task', taskNode);
    promptHost = block('Last prompt', promptNode);
    msgHost = block('Last message', msgNode);
    permHost = o.perm ? block('Needs permission', [permNote, permBtns], ' peek-perm') : null;
    node = el('div', { class: 'peek-card', 'data-tmux': tmux },
      el('div', { class: 'peek-sub' }, glyphs, where, age, meta), hooksNote,
      taskHost, promptHost, msgHost, permHost,
      el('div', { class: 'peek-actions' }, openSlot, ackSlot, killSlot), chips);
  } else {
    node = el('div', { class: 'rrow' + (o.compact ? ' compact' : '') + (rich ? ' rich' : '') + (o.cls ? ' ' + o.cls : ''), 'data-tmux': tmux },
      glyphs, el('div', { class: 'rr-main' }, nameNode, where, age), rich ? el('div', { class: 'rr-metaline' }, meta, badges) : meta,
      el('div', { class: 'rr-last' }, o.perm ? permNote : null, promptNode, msgNode),
      el('div', { class: 'rr-actions' }, o.perm ? permBtns : null, openLink, killSlot, moreBtn), chips, sendRow, tailHost);
    if (typeof Dnd !== 'undefined') Dnd.bind(node, { drop: 'session', tmux });
    else if (typeof Lazy !== 'undefined') Lazy.later('dnd', () => { if (typeof Dnd !== 'undefined') Dnd.bind(node, { drop: 'session', tmux }); });     // dnd.js is lazy (lazy.js): bound once a board has loaded it      // a backlog card dropped on the row is handed to this session (dnd.js)
  }

  function patchPerm(pr) {
    const lead = o.peek || cur.lead !== false;                // the peek is its own surface: its Allow is always the filled one
    const sig = pr ? `${pr.id}:${pr.summary || ''}:${lead ? 1 : 0}:${permMark(pr)}` : '';
    cur.pr = pr || null;
    if (cur.perm === sig) return;
    cur.perm = sig;
    permBtns.textContent = '';
    if (pr) {
      permBtns.append(...permButtons(pr, (lead ? 'primary' : 'primary tinted') + ' small', 'danger small'));
    }
    permNote.textContent = pr ? String(pr.summary || 'permission request').slice(0, o.peek ? 800 : 200) : '';
    for (const n of (permHost ? [permHost] : [permNote, permBtns])) n.classList.toggle('hidden', !pr);
  }

  const show = (n, on) => n.classList.toggle('hidden', !on);

  function patchBadges(s2, st) {
    const t = s2.stats || {};
    const nudge = sessionNudgeable(s2);
    show(b.model, !!t.model);
    if (t.model) { setTextIfChanged(b.model, String(t.model)); chipHueSet(b.model, chipHue('model', t.model)); }
    const accts = agentsAccounts(st);
    const acct = s2.account && accts.length > 1 ? (accts.find((x) => x.key === s2.account) || { key: s2.account }) : null;
    show(b.acct, !!acct);
    if (acct) {
      setTextIfChanged(b.acct, agentsAcctName(acct));
      chipHueSet(b.acct, chipHue('account', acct.key));
      b.acct.setAttribute('title', `subscription account: ${agentsAcctName(acct)}${acct.email && acct.email !== agentsAcctName(acct) ? ' · ' + acct.email : ''}`);
    }
    const hasCtx = typeof t.context_pct === 'number';
    show(b.ctx, hasCtx);
    if (hasCtx) b.ctx.ccSet(t.context_pct);
    show(b.compact, hasCtx && t.context_pct >= CTX_HI && sessionAgent(s2) === 'claude' && nudge);
    show(b.worktree, sessionWorktree(s2, st));
    show(b.noauto, autoContinueApplies(s2) && sessionAutoContinueOff(s2));
    const task = sessionTask(s2, st);
    const prNum = task && task.pr_number;
    show(b.pr, !!prNum);
    if (prNum) {
      const bucket = task.ci && task.ci.bucket;
      setTextIfChanged(b.pr, `PR #${prNum}`);
      b.pr.className = 'bdg bdg-pr hue-slate' + (bucket === 'fail' ? ' bad' : bucket === 'pass' ? ' ok' : bucket === 'pending' ? ' warn' : '');
      if (task.pr_url) b.pr.setAttribute('href', task.pr_url); else b.pr.removeAttribute('href');
      b.pr.setAttribute('title', `${task.pr_state || 'PR'}${bucket && bucket !== 'none' ? ' · CI ' + bucket : ''}`);
    }
    const nSub = s2.flags && typeof s2.flags.subagents === 'number' ? s2.flags.subagents : 0;
    show(b.sub, nSub > 0);
    if (nSub > 0) setTextIfChanged(b.sub, `${nSub} subagent${nSub === 1 ? '' : 's'}`);
    queuedPromptsSet(b.queued, s2.queued_prompts);
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
    const accts = rich && s2.account ? agentsAccounts(st) : [];
    const ac = accts.length > 1 ? (accts.find((x) => x.key === s2.account) || null) : null;
    return JSON.stringify([s2.state, s2.state_at, s2.name, s2.last_prompt, s2.last_message, s2.needs_attention, s2.agent, s2.launcher, s2.created,
      s2.project, s2.repo, s2.folder, s2.stats, s2.path, s2.flags, s2.task, s2.hooks_missing || null, s2.parked, s2.queued_prompts || 0,
      pr ? pr.id + ':' + pr.summary + permMark(pr) : '', ui.confirm === killKey, sessionClosing.has(tmux),
      t ? [t.id, t.pr_number, t.pr_url, t.pr_state, t.mode, t.ci && t.ci.bucket] : null, rl && rl.session === s2.tmux ? [rl.message, rl.resets_at] : null,
      rich ? [s2.account || '', accts.length > 1, ac ? [ac.label, ac.name, ac.email] : null] : null, Nodes.sig(s2)]);
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
    if (cur.g !== gk) {
      const ag = agentGlyph(agent);
      ag.classList.add(chipHue('agent', agent));             // claude violet, codex teal, shell slate
      glyphs.textContent = '';
      glyphs.append(stateGlyph(sk), ag);
      cur.g = gk;
    }
    setTextIfChanged(nameNode, s2.name || tmux);
    setTextIfChanged(where, sessionWhere(s2, o.showProject));
    Nodes.mark(where, s2);                                     // the node chip (dim for this board) once nodes are paired (nodes.js)
    if (o.showProject && s2.project) chipHueSet(where, chipHue('project', s2.project));
    agentsAgeNode(age, sessionActivity(s2));
    const closing = sessionClosing.has(tmux);                  // #311
    node.classList.toggle('closing', closing);
    if (moreBtn) moreBtn.disabled = closing;
    setTextIfChanged(meta, closing ? 'closing…' : rich ? limitParkedText(s2) || GLYPH_LABEL[sk] : sessionMetaText(s2, sk));       // #71: a parked session says the limit, not 'error'
    const lim = o.peek ? [800, 2000] : (o.compact || rich ? [120, 160] : [200, 320]);
    const pt = s2.last_prompt ? '› ' + String(s2.last_prompt).slice(0, lim[0]) : '';
    const mt = s2.last_message ? String(s2.last_message).slice(0, lim[1]) : '';
    setTextIfChanged(promptNode, pt);
    setTextIfChanged(msgNode, mt);
    if (taskHost) {
      const tt = s2.task && s2.task.title ? String(s2.task.title).slice(0, 160) : '';
      setTextIfChanged(taskNode, tt);
      taskHost.classList.toggle('hidden', !tt);
    }
    promptHost.classList.toggle('hidden', !pt);
    msgHost.classList.toggle('hidden', !mt);
    node.classList.toggle('attn', !!s2.needs_attention);
    const hm = s2.hooks_missing || '';
    if (cur.hm !== hm) {                                       // #96: the chip (rich row) and the worded note (peek), rebuilt only when the kind flips
      cur.hm = hm;
      for (const host of [b.hooks, hooksNote]) {
        if (!host) continue;
        host.textContent = '';
        if (hm) host.append(hooksMissingChip(hm), host === hooksNote ? el('span', { class: 'peek-hooks-why', text: hooksMissingWhy(hm) }) : '');
        host.classList.toggle('hidden', !hm);
      }
    }
    if (o.perm) patchPerm(sessionPerm(s2.tmux));
    if (o.peek) {
      const wantPrimary = !cur.pr;                            // Open terminal leads only while nothing waits on an Allow
      if (cur.openP !== wantPrimary) { cur.openP = wantPrimary; openSlot.textContent = ''; openSlot.append(openNode(wantPrimary)); }
      const ack = !!s2.needs_attention;
      if (cur.ack !== ack) {
        cur.ack = ack;
        ackSlot.textContent = '';
        if (ack) ackSlot.append(el('button', { class: 'small minimal', type: 'button', onclick: (e) => { e.stopPropagation(); sessionAck(cur.s); }, text: 'Ack' }));
      }
    }
    const confirming = ui.confirm === killKey;                // rebuilt only when the two-tap state flips (confirmButton repaints through repaintPage)
    const killState = closing ? 'closing' : confirming;
    if (cur.kill !== killState) {
      cur.kill = killState;
      killSlot.textContent = '';
      if (closing) killSlot.append(el('button', { class: 'danger small minimal', disabled: true, text: 'Closing…' }));
      else if (confirming || o.peek) killSlot.append(confirmButton(killKey, 'Kill', () => sessionKill(cur.s.tmux), true));
    }
    const nudge = sessionNudgeable(s2);
    chips.classList.toggle('hidden', !nudge);
    if (sendRow) sendRow.classList.toggle('hidden', !nudge);
    if (rich) {
      patchBadges(s2, st);
      if (o.autoOpen && !cur.manual) node.classList.toggle('open', sk === 'waiting' && nudge);   // a waiting row keeps its reply box open until the person toggles it
      syncMore();
    }
  }

  node.ccPatch = patch;
  node.ccDestroy = () => { tailOff(); };
  node.ccCanLead = () => !!cur.pr;                           // only a row with an Allow button can carry the screen's filled primary
  node.ccLead = (on) => {
    if (cur.lead === on) return;
    cur.lead = on;
    if (o.perm) patchPerm(sessionPerm(cur.s.tmux));
  };
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

async function bareTerminal(s) {
  try {
    const r = await api('POST', `/api/bare/${encodeURIComponent(s.bare_device)}/sessions/${encodeURIComponent(s.tmux)}/attach`, {});
    location.assign(r.attach_url + (demoOn() ? '?demo=1' : ''));
  } catch (e) { pageToast(e.message, 'bad'); }
}
