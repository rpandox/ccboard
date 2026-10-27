/* ccboard inbox (v0.5.5): what needs you, with the context to answer it. Home's first section and #/inbox are both made of the cards below.

   Inbox.kind(s, st) sorts a session into one of ten kinds, in the order the inbox lists them:
     permission     a permission is pending for it (st.pending_permissions)            Allow / Deny, only when no full terminal client is attached
     terminal_prompt a Claude tool approval in the pane after the hook wait          Allow / Deny, including with a full terminal attached
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
  // Requests first, then plans, questions and the remaining attention states.
  ORDER: ['permission', 'terminal_prompt', 'plan', 'question', 'needs', 'limit', 'error', 'done-question', 'waiting', 'done'],
  LABEL: { permission: 'permission', terminal_prompt: 'terminal prompt', plan: 'plan review', question: 'question', needs: 'job needs you', limit: 'limit hit', error: 'error',
           'done-question': 'done, asks', waiting: 'waiting', done: 'done' },
  PLAN_RE: /\bplan\b|ExitPlanMode/i,
  ASKS_RE: /\?[\s"'`)\]*_]*$|AskUserQuestion/,
  LIMIT_RE: /limit/i,
  HINT: 'j / k move · Enter open · o terminal · a ack · y allow · d deny · r reply · ? all keys',
  // the one action each kind of card leads with (v0.5.6d): the lead card shows it filled, the others tinted
  PRIMARY: { permission: 'allow', terminal_prompt: 'allow', plan: 'reply', question: 'reply', 'done-question': 'reply', waiting: 'reply', needs: 'open', error: 'open', limit: 'ack', done: 'ack' },
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
  const pending = Inbox.pending(s, st);
  if (pending) return pending.kind === 'terminal_prompt' ? 'terminal_prompt' : 'permission';
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
  const bareRows = (st.bare_devices || []).flatMap((d) => (d.sessions || []).map((s) => ({
    ...s, tmux: s.tmux_name, bare_device: d.ts_id, project: d.name, name: s.agent + ' on ' + d.name, folder: '',
  })));
  for (const s of [...rosterSessions(st), ...bareRows]) {
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

