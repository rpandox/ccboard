/* ccboard epic worker panel (issue #157, phase P17): what a node's worker is doing, drawn the same way in Settings > Nodes (this board, with Stop worker and Start worker) and on a paired node's
   page (read only). Lazy bundle 'worker' (lazy.js): Settings and the node page ask for it on first use, so the first paint does not carry it. Classic script, definition-only: nothing here touches
   the DOM, a timer or the network until a page calls it.

   The data is app/nodes_worker.py Worker.view(), answered as `state.node.epic_worker` on every poll while the worker is ON (the key is absent while it is off), in the node card of a paired node
   (`rec.card.epic_worker`) and by GET /api/epic-worker. POST /api/epic-worker/stop and /start (a signed-in person only) answer the same view, which is painted at once and kept until a poll that is newer
   has the same thing to say. There is no switch here that turns the worker on: that is the env key CCBOARD_EPIC_WORKER on the box, and the panel says so.

   A state is a glyph and words, never colour alone (✽ working, ✻ waiting, ∙ idle, ○ off or stopped, ✕ refused). Everything the board sends is drawn through textContent. The panel is built once and painted in
   place, so a poll never recreates the button under a finger.

   Demo (?demo=1): demo/worker.json holds the views; ?demo=1&worker=working|idle|waiting|paused|pausedday|resting|stopped|refused|refusedwrites|refusedauthors|off picks one (working is the default).
   demoWorker() answers /api/epic-worker for core.js demoApi, and Stop and Start work for the life of the page. */
'use strict';

/* state -> glyph, tone ('sig' | 'warn' | 'bad' | '' quiet) and the short word; the headline adds the issue, the cap or the time */
const EW_STATE = {
  off: { glyph: '○', tone: '', word: 'Off' },
  refused: { glyph: '✕', tone: 'bad', word: 'Refused to start' },
  stopped: { glyph: '○', tone: '', word: 'Stopped' },
  paused: { glyph: '✻', tone: 'warn', word: 'Paused' },
  resting: { glyph: '✻', tone: 'warn', word: 'Resting' },
  working: { glyph: '✽', tone: 'sig', word: 'Working' },
  waiting: { glyph: '✻', tone: 'warn', word: 'Waiting' },
  idle: { glyph: '∙', tone: '', word: 'Idle' },
};
/* a decision kind -> glyph and word (app/nodes_worker.py decide(): start skip refused cap rest wait park stop resume error) */
const EW_KIND = {
  start: { glyph: '✽', word: 'Started' }, skip: { glyph: '∙', word: 'Skipped' }, refused: { glyph: '✕', word: 'Refused' }, cap: { glyph: '✻', word: 'Cap' }, rest: { glyph: '✻', word: 'Resting' },
  wait: { glyph: '✻', word: 'Waiting' }, park: { glyph: '○', word: 'Parked' }, stop: { glyph: '○', word: 'Stopped' }, resume: { glyph: '✓', word: 'Started again' }, error: { glyph: '✕', word: 'Error' },
};
const EW_SHOWN = 20;
const EW_ENV = ['CCBOARD_EPIC_WORKER=1', 'CCBOARD_EPIC_WRITES=1', 'CCBOARD_EPIC_REPOS=owner/name'];
const EW_OFF_WHAT = 'When it is on, this node watches the epics of the repositories you list and takes the next ready issue by itself whenever it has a free lane, with nobody pressing Run.';
const EW_RISK = 'The risk: the text of an issue starts an agent on this machine, with acceptEdits in a worktree and nobody watching. Anyone who can create or edit issues in the listed repositories can steer it. Use a private repository and a dedicated epic.';
const EW_HOW = 'To turn it on, set these on the box and rerun ./install.sh. It refuses to start until a repository is listed and an author is known (CCBOARD_EPIC_AUTHORS, or the gh login). The caps and the poll interval have their own keys (README, Settings). Nothing on this page turns it on, on purpose.';
const EW_NODE_NOTE = 'Read only here. Stop the worker on that node\'s own board (Open board).';

const EpicWorker = { override: null, busy: false, err: '', demo: null, demoMade: null, fixture: null };

function ewOwn(o, k) { return !!o && typeof o === 'object' && Object.prototype.hasOwnProperty.call(o, k); }
function ewEpoch(iso) { const t = typeof iso === 'string' ? Date.parse(iso) : NaN; return Number.isFinite(t) ? t / 1000 : 0; }
/* The board's sentence under the headline, in this page's clock: a time the board wrote in UTC is shown the way every other time here is, and a sentence that only
   repeats the headline (minus its time) is left out. */
function ewSentence(v, h) {
  let t = typeof v.text === 'string' ? v.text : '';
  if (v.until) t = t.replace(/\b\d\d:\d\d UTC\b/, ewClock(v.until));
  const norm = (x) => String(x || '').replace(/\buntil \d\d:\d\d\b/, '').replace(/[.\s]+$/, '').trim().toLowerCase();
  const first = norm(t.split('. ')[0]);
  if (first && first === norm(h && h.word)) t = t.split('. ').slice(1).join('. ');
  return t.trim();
}
function ewClock(iso) { const e = ewEpoch(iso); return e ? (typeof clockAt === 'function' ? clockAt(e) : new Date(e * 1000).toISOString().slice(11, 16)) : ''; }
function ewCap(s) { const t = String(s || ''); return t.charAt(0).toUpperCase() + t.slice(1); }
function ewText(v, n) { return typeof v === 'string' ? v.slice(0, n || 300) : ''; }

/* The view as far as the page can trust it: only the fields the panel draws, each of a known type. null when it is not an object. */
EpicWorker.clean = function (v) {
  if (!v || typeof v !== 'object') return null;
  const strs = (a, n) => (Array.isArray(a) ? a.filter((x) => typeof x === 'string').slice(0, n).map((x) => x.slice(0, 80)) : []);
  const cap = (c) => (c && typeof c === 'object' ? { used: Number.isFinite(c.used) ? c.used : 0, max: Number.isFinite(c.max) ? c.max : 0, clears_at: typeof c.clears_at === 'string' ? c.clears_at : null } : null);
  const caps = v.caps && typeof v.caps === 'object' ? { hour: cap(v.caps.hour), day: cap(v.caps.day) } : null;
  const decisions = (Array.isArray(v.decisions) ? v.decisions : []).filter((d) => d && typeof d === 'object').slice(0, EW_SHOWN).map((d) => ({
    at: typeof d.at === 'string' ? d.at : '', kind: ewOwn(EW_KIND, d.kind) ? d.kind : 'skip', issue: typeof d.issue === 'string' ? d.issue.slice(0, 120) : null, reason: ewText(d.reason, 40), text: ewText(d.text, 300),
  }));
  return {
    on: v.on === true, state: ewOwn(EW_STATE, v.state) ? v.state : 'idle', reason: typeof v.reason === 'string' ? v.reason.slice(0, 40) : null, text: ewText(v.text, 400), until: typeof v.until === 'string' ? v.until : null,
    issues: strs(v.issues, 5), repos: strs(v.repos, 25), authors: strs(v.authors, 25), poll_s: Number.isFinite(v.poll_s) ? v.poll_s : null, running: v.running === true, caps,
    cycle_at: typeof v.cycle_at === 'string' ? v.cycle_at : null, next_at: typeof v.next_at === 'string' ? v.next_at : null, at: typeof v.at === 'string' ? v.at : null,
    stopped_at: typeof v.stopped_at === 'string' ? v.stopped_at : null, decisions,
  };
};

/* The headline: glyph, tone and the words ("Working on example/website#12", "Waiting for a window until 14:30", "Paused by the daily cap until 09:00"). */
EpicWorker.headline = function (v) {
  const s = EW_STATE[v.state];
  const at = v.until ? ewClock(v.until) : '';
  const until = at ? ` until ${at}` : '';
  let word = s.word;
  if (v.state === 'working') word = v.issues.length ? `Working on ${v.issues.join(', ')}` : 'Working';
  else if (v.state === 'paused') word = (v.reason === 'day' ? 'Paused by the daily cap' : 'Paused by the hourly cap') + until;
  else if (v.state === 'resting') word = 'Resting, GitHub is not available' + until;
  else if (v.state === 'waiting') word = (v.reason === 'window' || v.reason === 'backoff' ? 'Waiting for a window' : v.reason === 'lane' ? 'Waiting for a free lane' : 'Waiting') + until;
  else if (v.state === 'idle') word = 'Idle, nothing ready';
  else if (v.state === 'stopped') word = 'Stopped by a person';
  return { glyph: s.glyph, tone: s.tone, word };
};

/* One cap as words: "2 of 6 used" with when it clears while it is spent, "none used" for 0. */
EpicWorker.capText = function (c) {
  if (!c) return 'not known';
  const full = c.max > 0 && c.used >= c.max;
  const at = c.clears_at ? ewClock(c.clears_at) : '';
  return `${c.used} of ${c.max} used${full ? (at ? `, full until ${at}` : ', full') : at && c.used ? `, the oldest frees at ${at}` : ''}`;
};

/* The view a page draws for THIS board. The poll's key wins when it is newer than the answer of a Stop or a Start; no key at all means the worker is off. */
EpicWorker.pick = function (st) {
  if (typeof demoOn === 'function' && demoOn()) return EpicWorker.clean(EpicWorker.demo || EpicWorker.demoView());
  const polled = EpicWorker.clean(st && st.node && st.node.epic_worker);
  const mine = EpicWorker.override;
  if (mine && (!polled || (mine.at || '') >= (polled.at || ''))) return mine;
  return polled || EpicWorker.clean({ on: false, state: 'off', text: 'The worker is off (CCBOARD_EPIC_WORKER).' });
};

/* The view in a paired node's card: its key, or in the demo the same fixture view. null when the card has none (the worker is off there, or the node is older). */
EpicWorker.fromCard = function (card) {
  if (card && typeof card === 'object' && card.epic_worker && typeof card.epic_worker === 'object') return EpicWorker.clean(card.epic_worker);
  if (typeof demoOn === 'function' && demoOn()) return EpicWorker.clean(EpicWorker.demoView());
  return null;
};

/* What a repaint of the Settings panel depends on: the view without its own clock, the busy flag and the error. */
EpicWorker.sig = function (st) {
  const v = EpicWorker.pick(st);
  if (!v) return null;
  const { at, ...rest } = v;
  return JSON.stringify([rest, EpicWorker.busy, EpicWorker.err]);
};

/* ---------- the panel ---------- */

EpicWorker.build = function (opts) {
  const o = opts || {};
  const node = o.mode === 'node';
  const chip = (t) => el('span', { class: 'ew-chip', text: t });
  const row = (label, ...kids) => { const v = el('span', { class: 'ew-v' }, kids); return { row: el('div', { class: 'ew-row' }, el('b', { class: 'ew-k', text: label }), v), v }; };
  const glyph = el('span', { class: 'ew-glyph', 'aria-hidden': 'true' });
  const word = el('span', { class: 'ew-word' });
  const head = el('div', { class: 'ew-state', role: 'status' }, glyph, word);
  const text = el('p', { class: 'ew-text dim' });
  const err = el('p', { class: 'ew-err bad hidden', role: 'alert' });
  const stop = node ? null : el('button', { class: 'ew-btn', type: 'button', text: 'Stop worker', title: 'Stop the worker now. It starts nothing more and gives back a claim that has no task.', onclick: () => EpicWorker.act('stop', o) });
  const start = node ? null : el('button', { class: 'ew-btn hidden', type: 'button', text: 'Start worker', title: 'Let the worker read the listed epics again', onclick: () => EpicWorker.act('start', o) });
  const acts = node ? null : el('div', { class: 'ew-acts' }, stop, start);                      // a paired node's worker is stopped on that node's own board
  const nodeNote = el('p', { class: 'ew-note dim hidden', text: EW_NODE_NOTE });
  const offBox = el('div', { class: 'ew-off hidden' }, el('p', { class: 'ew-what', text: EW_OFF_WHAT }), el('p', { class: 'ew-risk', text: EW_RISK }),
    el('p', { class: 'ew-how dim', text: EW_HOW }), el('ul', { class: 'ew-env' }, EW_ENV.map((k) => el('li', {}, el('code', { class: 'ew-code', text: k })))));
  const repos = row('Repositories'), authors = row('Authors'), hour = row('This hour'), day = row('Today'), poll = row('Looks every'), nextAt = row('Next look'), seen = row('Read');
  const risk = el('p', { class: 'ew-risk-short dim', text: 'An issue\'s text starts an agent here with nobody pressing Run. Only issues from the repositories and authors above, with the ready label set by an allowed author, are taken.' });
  const rows = el('div', { class: 'ew-rows' }, repos.row, authors.row, hour.row, day.row, poll.row, nextAt.row, seen.row, risk);
  const decHead = el('div', { class: 'ew-label', text: 'Last decisions' });
  const decList = el('div', { class: 'ew-decs' });
  const decNone = el('p', { class: 'ew-none dim hidden', text: 'No decision yet. The worker writes one line here each time it starts, skips or stops something.' });
  const on = el('div', { class: 'ew-on' }, rows, decHead, decList, decNone);
  const root = el('div', { class: 'ew' + (node ? ' ew-node' : '') }, head, text, acts, err, nodeNote, offBox, on);

  const list = (host, items, kind) => {
    const want = items.length ? items : [null];
    const cur = [...host.children];
    if (cur.length === want.length && cur.every((c, i) => c.getAttribute('data-k') === (want[i] || ''))) return;
    host.textContent = '';
    for (const t of items) host.append(chip(t));
    if (!items.length) host.append(el('span', { class: 'dim', text: kind === 'repos' ? 'none listed' : 'none known' }));
    [...host.children].forEach((c, i) => c.setAttribute('data-k', want[i] || ''));
  };
  const decRow = (d) => {
    const k = EW_KIND[d.kind];
    const reason = ewCap(d.text || d.reason);
    return el('div', { class: 'ew-dec ' + d.kind },
      el('span', { class: 'ew-dec-t dim', text: ewClock(d.at) }),
      el('span', { class: 'ew-dec-k' }, el('span', { class: 'ew-dglyph', 'aria-hidden': 'true', text: k.glyph }), el('span', { text: k.word })),
      d.issue ? el('span', { class: 'ew-dec-i', text: d.issue }) : null,
      reason ? el('span', { class: 'ew-dec-r dim', text: reason }) : null);
  };
  const patchDecs = (items) => {
    const sig = JSON.stringify(items);
    if (decList.ccSig === sig) return;
    decList.ccSig = sig;
    decList.textContent = '';
    for (const d of items) decList.append(decRow(d));
  };
  const setClass = (n, cls, add) => { if (n.classList.contains(cls) !== add) n.classList.toggle(cls, add); };

  function paint(view, ctx) {
    const v = view || null;
    const c = ctx || {};
    const off = !v || v.state === 'off' || !v.on;
    const h = v && !off ? EpicWorker.headline(v) : { glyph: EW_STATE.off.glyph, tone: '', word: 'Off' };
    setTextIfChanged(glyph, h.glyph);
    setTextIfChanged(word, h.word);
    head.className = 'ew-state' + (h.tone ? ' ' + h.tone : '');
    setTextIfChanged(text, off ? 'The worker is off on this node.' : ewSentence(v, h));
    setClass(offBox, 'hidden', !off);
    setClass(on, 'hidden', off);
    const live = !off && ['working', 'waiting', 'idle', 'paused', 'resting'].includes(v.state);
    const stopped = !off && v.state === 'stopped';
    if (!node) {
      setClass(acts, 'hidden', !(live || stopped));
      setClass(stop, 'hidden', !live);
      setClass(start, 'hidden', !stopped);
      for (const b of [stop, start]) { b.setAttribute('aria-busy', EpicWorker.busy ? 'true' : 'false'); setClass(b, 'busy', EpicWorker.busy); }
    }
    setClass(nodeNote, 'hidden', !(node && !off && (live || stopped)));
    setTextIfChanged(err, EpicWorker.err);
    setClass(err, 'hidden', !EpicWorker.err || node);
    if (off) return;
    list(repos.v, v.repos, 'repos');
    list(authors.v, v.authors, 'authors');
    setTextIfChanged(hour.v, EpicWorker.capText(v.caps && v.caps.hour));
    setTextIfChanged(day.v, EpicWorker.capText(v.caps && v.caps.day));
    setTextIfChanged(poll.v, v.poll_s ? `${v.poll_s} s, a little earlier or later each time` : 'not known');
    const nxt = live && v.next_at ? ewClock(v.next_at) : '';
    setTextIfChanged(nextAt.v, nxt || (stopped ? 'not while stopped' : v.state === 'refused' ? 'not while refused' : 'not known'));
    setTextIfChanged(seen.v, c.ageText || (v.at ? `at ${ewClock(v.at)}` : 'not known'));
    setClass(seen.row, 'hidden', !c.ageText && !node);
    patchDecs(v.decisions);
    setClass(decNone, 'hidden', v.decisions.length > 0);
    setClass(decList, 'hidden', v.decisions.length === 0);
  }
  return { root, paint, stop, start };
};

/* Stop worker / Start worker: one tap, no confirmation (Stop is the safe direction; Start only brings back what the owner switched on). The answer is painted at once; a refusal is said beside the button. */
EpicWorker.act = async function (kind, opts) {
  if (EpicWorker.busy) return false;
  EpicWorker.busy = true;
  EpicWorker.err = '';
  const again = () => { if (opts && typeof opts.onChange === 'function') opts.onChange(); };
  again();
  let ok = false;
  try {
    const r = await api('POST', `/api/epic-worker/${kind}`);
    const v = EpicWorker.clean(r);
    if (v) EpicWorker.override = v;
    ok = true;
    if (typeof pageToast === 'function') pageToast(kind === 'stop' ? 'Worker stopped' : 'Worker started', 'ok');
  } catch (e) {
    const w = e && e.body && typeof e.body === 'object' ? EpicWorker.clean(e.body.worker) : null;
    if (w) EpicWorker.override = w;
    EpicWorker.err = ewText((e && e.message) || `The board did not ${kind} the worker`, 300);
    if (e && e.status === 403) EpicWorker.err = 'Only a signed-in person can do that. This session is not one.';
  } finally {
    EpicWorker.busy = false;
    again();
  }
  try { if (ok && typeof poll === 'function') poll(true); } catch (_) { /* no board poll on this page */ }
  return ok;
};

/* ---------- the demo ---------- */

function ewDemoFlag() { try { const m = /[?&]worker=([a-z]+)/.exec(location.search); return m ? m[1] : 'working'; } catch (_) { return 'working'; } }

/* The fixture (demo/worker.json), asked for once; a page awaits this before it paints in the demo. */
EpicWorker.warm = async function () {
  if (typeof demoOn !== 'function' || !demoOn() || EpicWorker.fixture) return;
  const r = await fetch('/static/demo/worker.json');
  if (r.ok) EpicWorker.fixture = demoRebase(await r.json());
  EpicWorker.demo = EpicWorker.demo || EpicWorker.demoView();
};

/* The view of ?worker=<name> from the fixture: off, or base + the state's patch + its lead decisions + the shared ones (20 at most). A time written into the text follows the rebased clock. */
EpicWorker.demoView = function (flag) {
  const fx = EpicWorker.fixture;
  if (!fx) return null;
  const name = flag || ewDemoFlag();
  if (name === 'off') return fx.off;
  const patch = ewOwn(fx.states, name) ? fx.states[name] : fx.states.working;
  const { lead, ...rest } = patch;
  const v = { ...fx.base, ...rest };
  v.decisions = ewOwn(patch, 'decisions') ? patch.decisions : [...(lead || []), ...fx.decisions].slice(0, EW_SHOWN);
  if (v.until && typeof v.text === 'string') v.text = v.text.replace(/\d\d:\d\d UTC/, `${new Date(Date.parse(v.until)).toISOString().slice(11, 16)} UTC`);
  return v;
};

/* demoApi's answer for /api/epic-worker (GET), /stop and /start: the page's own make-believe worker. */
async function demoWorker(method, path) {
  await EpicWorker.warm();
  const cur = EpicWorker.demo || EpicWorker.demoView();
  if (method === 'GET') return cur;
  await new Promise((resolve) => setTimeout(resolve, 150));
  const bare = path.split('?')[0];
  if (!cur || cur.state === 'off') { const e = new Error('The worker is off (CCBOARD_EPIC_WORKER).'); e.status = 409; e.body = { error: e.message, reason: 'off' }; throw e; }
  const now = new Date().toISOString();
  const lead = (kind, reason, text) => ({ at: now, kind, issue: null, reason, text });
  if (/\/stop$/.test(bare)) {
    EpicWorker.demo = { ...cur, state: 'stopped', reason: 'stopped', text: 'A person stopped the worker. It starts nothing until Start is pressed.', until: null, issues: [], running: false, next_at: null, stopped_at: now, at: now,
      decisions: [lead('stop', 'person', 'Stopped by a person.'), ...cur.decisions].slice(0, EW_SHOWN) };
  } else if (/\/start$/.test(bare)) {
    if (cur.state === 'refused') { const e = new Error(cur.text); e.status = 409; e.body = { error: cur.text, reason: cur.reason, worker: cur }; throw e; }
    EpicWorker.demo = { ...cur, state: 'idle', reason: null, text: 'Idle: no ready issue in the listed epics.', until: null, running: true, next_at: new Date(Date.now() + 90000).toISOString(), stopped_at: null, at: now,
      decisions: [lead('resume', 'resumed', 'Started again by a person.'), ...cur.decisions].slice(0, EW_SHOWN) };
  } else return {};
  return EpicWorker.demo;
}
