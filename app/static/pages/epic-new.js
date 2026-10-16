/* ccboard New epic sheet (issue #158), part of the lazy 'epic' bundle (lazy.js, after pages/epic.js; styles in pages/epic.css under #sheet .epn-*): a native <dialog> (components.js openSheet) in which
   the owner types an epic, looks at a PREVIEW of exactly what the board will write on GitHub, and presses CREATE. Entry points: the Tasks > Epics tab, the project page's More menu and the command palette
   (all call epicNewOpen). Classic script, definition-only: nothing here runs, asks the board or draws until epicNewOpen is called.

   The board does the work. This sheet asks:  GET /api/epics/new (the repositories of this board's projects and the writes switch), POST /api/epics/preview (what Create will write; writes nothing, so it works
   with GitHub writes off) and POST /api/epics (Create). Create is enabled only after a Preview of the exact inputs on screen: any change to any field, a row added, moved or removed, or the repository, makes the preview
   out of date and disables Create again. With GitHub writes off (CCBOARD_EPIC_WRITES) Create stays disabled and says why in words.
   A create can take over a minute (the board writes one issue at a time, 1.1 s apart): the sheet says how many issues, how long, and keeps every value. A failure midway (the board's `ok: false`, or a connection
   that broke) lists what exists on GitHub and offers Resume: the same request again, which the board answers by looking for the hidden marker lines first, so it writes only what is missing and never a duplicate.
   The inputs are locked while a create is running or waiting to be resumed (a changed title would be a different epic).
   Everything the board sends back is shown with textContent; a link is drawn only for an address that is exactly https://github.com/<owner>/<repo>/issues/<n>. The typed text is never put in markup. */
'use strict';

const EPN_LIMITS = { children: 20, title: 120, body: 10000 };       // the board's answer (GET /api/epics/new) replaces them
const EPN_TIERS = [['A', 'A'], ['B', 'B'], ['C', 'C'], ['check', 'Check']];
const EPN_TIER_WORDS = { A: 'open design, risky or cross-cutting work', B: 'standard feature work', C: 'mechanical work with a clear recipe', check: 'a check of finished work' };
const EPN_SIZES = [['S', 'S'], ['M', 'M'], ['L', 'L']];
const EPN_GH = 'https://github.com/';          // the demo builds the new issues' addresses from the checked repository; every other link is the board's
const EPN_LINK_RE = /^https:\/\/github\.com\/[A-Za-z0-9-]{1,39}\/[A-Za-z0-9_.-]{1,100}\/(?:issues|pull)\/[1-9][0-9]{0,8}$/;
const EPN_REPO_RE = /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\/(?![-.])[A-Za-z0-9_.-]{1,100}$/;
const EPN_REPO_KEY = 'ccboard:epic-new-repo';
const EPN_WRITES_OFF = 'GitHub writes are off on this board. Set CCBOARD_EPIC_WRITES=1 on the box to allow Create. Preview still works and writes nothing.';
const EPN_STEP = {                   // where a failed create stopped, in words (the board's `step`)
  read: 'before anything was written', label: 'while making the labels', parent: 'while writing the epic issue', edit: 'while filling in the epic\'s list of issues',
};
const EPN_BROKEN = 'The connection broke while the board was writing. Some issues may already be on GitHub. Resume looks on GitHub first and writes only what is missing, so nothing is written twice.';
const EPN_UNKNOWN = 'GitHub did not say whether the last write happened. Resume looks on GitHub first, so that write is not made twice.';
const EPN_KEYS = 'Alt+Up and Alt+Down move an issue. Ctrl+Enter (Cmd+Enter on a Mac) adds one below. Delete on the Remove button removes it.';

const epicNew = { S: null };         // the open sheet, if any

/* ---------- small pure helpers ---------- */

function epnSteps(step) {
  const s = String(step || '');
  let m = /^child ([0-9]+)$/.exec(s);
  if (m) return `while writing issue ${m[1]}`;
  m = /^attach ([0-9]+)$/.exec(s);
  if (m) return `while attaching issue ${m[1]} to the epic`;
  return ownKey(EPN_STEP, s) ? EPN_STEP[s] : '';
}

function epnLink(url, text, cls) {
  if (typeof url === 'string' && EPN_LINK_RE.test(url)) return el('a', { class: cls || null, href: url, target: '_blank', rel: 'noopener noreferrer', text });
  return el('span', { class: cls || null, text });
}

/* A sentence from the board, ended with a full stop when it has none. */
function epnSentence(t) { const x = String(t || '').trim(); return /[.!?]$/.test(x) ? x : x + '.'; }

function epnCount(n, one, many) { return `${n} ${n === 1 ? one : many}`; }

function epnSeconds(s) {
  if (typeof s !== 'number' || !Number.isFinite(s) || s < 1) return 'a few seconds';
  if (s < 90) return `about ${Math.round(s)} s`;
  return `about ${Math.round(s / 60)} min`;
}

function epnLastRepo() { try { return localStorage.getItem(EPN_REPO_KEY) || ''; } catch (_) { return ''; } }
function epnRememberRepo(repo) { try { localStorage.setItem(EPN_REPO_KEY, repo); } catch (_) { /* storage may be unavailable */ } }

/* A segmented control (one track, arrow keys move). items [[value, label]]; onChange(value) runs on a change by the person, not at build. */
function epnSeg(items, initial, onChange, label, cls) {
  const node = el('div', { class: 'seg-ctl epn-seg' + (cls ? ' ' + cls : ''), role: 'group', 'aria-label': label });
  const btns = new Map();
  let cur = items.some(([v]) => v === initial) ? initial : items[0][0];
  const paint = () => { for (const [v, b] of btns) b.setAttribute('aria-pressed', v === cur ? 'true' : 'false'); };
  const set = (v, focus, quiet) => {
    if (!btns.has(v)) return;
    const changed = v !== cur;
    cur = v;
    paint();
    if (focus) btns.get(v).focus();
    if (changed && !quiet && typeof onChange === 'function') onChange(v);
  };
  for (const [v, text] of items) {
    const b = el('button', { class: 'seg-btn', type: 'button', 'aria-pressed': 'false', 'data-v': v, text, onclick: () => set(v), onkeydown: (e) => {
      const i = items.findIndex(([x]) => x === cur);
      const to = e.key === 'ArrowRight' ? items[(i + 1) % items.length][0] : e.key === 'ArrowLeft' ? items[(i + items.length - 1) % items.length][0] : null;
      if (to === null) return;
      e.preventDefault();
      set(to, true);
    } });
    btns.set(v, b);
    node.append(b);
  }
  paint();
  return { node, get value() { return cur; }, set, lock(on) { for (const b of btns.values()) b.disabled = !!on; } };
}

/* ---------- the model: the sheet's state is the source of truth, the DOM only shows it ---------- */

function epnRow(S, init) {
  return { id: ++S.ids, title: '', desc: '', tier: 'B', size: 'M', dep: null, ui: null, ...(init || {}) };
}

/* What Preview and Create send: depends_on is the 1-based number of an EARLIER row, or null. */
function epnPayload(S) {
  const at = new Map(S.rows.map((r, i) => [r.id, i + 1]));
  return {
    repo: S.repo, title: S.title, goal: S.goal,
    children: S.rows.map((r, i) => ({ title: r.title, description: r.desc, tier: r.tier, size: r.size, depends_on: r.dep !== null && at.has(r.dep) && at.get(r.dep) <= i ? at.get(r.dep) : null })),
  };
}

function epnSig(S) { return JSON.stringify(epnPayload(S)); }

function epnWritesOn(S) { return S.writesOn !== false && !(S.preview && S.preview.writes_on === false); }

function epnVisibility(S) {
  const p = S.preview;
  const priv = p && typeof p.private === 'boolean' ? p.private : null;
  return priv === true ? 'private' : priv === false ? 'public' : 'not known to be public or private';
}

/* ---------- the actions: Preview, Create and the sentence beside them ---------- */

function epnCanCreate(S) {
  if (S.phase === 'creating' || S.phase === 'previewing' || !S.preview || S.previewSig === null) return false;
  if (!epnWritesOn(S)) return false;
  return S.previewSig === epnSig(S);
}

function epnWording(S) {
  const p = S.preview;
  if (S.phase === 'creating') return `Writing ${epnCount(S.writing, 'issue', 'issues')} to ${S.repo}, ${epnSeconds(S.estimate)}${S.elapsed ? ` (${S.elapsed} s so far)` : ''}. Keep this open: every value is kept if it breaks.`;
  if (S.phase === 'partial') return S.partialText || EPN_BROKEN;
  if (!p) return 'Preview first. Nothing is written until you press Create.';
  if (!epnWritesOn(S)) return EPN_WRITES_OFF;
  if (S.previewSig !== epnSig(S)) return 'You changed something after the preview. Preview again, then Create.';
  const n = p.calls && p.calls.issues;
  return `Create writes ${epnCount(n, 'issue', 'issues')} on GitHub in ${S.repo} (${epnVisibility(S)}). The board cannot delete them.`;
}

function epnPaintActions(S) {
  if (!S.ui) return;
  const busy = S.phase === 'creating' || S.phase === 'previewing';
  const resume = S.phase === 'partial';
  const can = resume ? epnWritesOn(S) : epnCanCreate(S);
  S.ui.words.textContent = epnWording(S);
  S.ui.words.classList.toggle('warn', !epnWritesOn(S) || S.phase === 'partial');
  S.ui.create.textContent = resume ? 'Resume' : 'Create';
  S.ui.create.disabled = !!(busy || !can);
  S.ui.create.setAttribute('title', can && !busy ? (resume ? 'Look on GitHub first, then write only what is missing' : `Write ${S.preview ? S.preview.calls.issues : 0} issues on GitHub`) : (epnWritesOn(S) ? 'Preview the epic first' : EPN_WRITES_OFF));
  S.ui.preview.disabled = !!(busy || resume || !S.repo);
  S.ui.preview.textContent = S.phase === 'previewing' ? 'Previewing…' : 'Preview';
  S.ui.preview.classList.toggle('hidden', resume);
  S.ui.again.classList.toggle('hidden', !resume);                  // "Edit as a new epic" shows only while a stopped create waits for Resume
}

function epnLock(S, on) {
  if (!S.ui) return;
  for (const n of S.ui.body.querySelectorAll('input, textarea, select')) { n.disabled = !!on; }
  for (const n of S.ui.body.querySelectorAll('.epn-lockable')) { n.disabled = !!on; }
  for (const r of S.rows) { if (r.ui) { r.ui.tierSeg.lock(on); r.ui.sizeSeg.lock(on); } }
  S.ui.body.classList.toggle('epn-locked', on);
  if (!on) epnRenderRows(S);                                   // the move buttons know first and last again
}

/* A change of any input: the preview is out of date, Create is disabled again, the sentence beside it says so. */
function epnChanged(S) {
  if (S.phase === 'creating') return;
  if (S.ui) S.ui.preview_host.classList.toggle('stale', !!S.preview);
  epnPaintActions(S);
}

/* ---------- the rows ---------- */

function epnFieldErr(f, text) { if (f) fieldError(f, text || ''); }

function epnClearErrors(S) {
  for (const f of [S.ui.fRepo, S.ui.fTitle, S.ui.fGoal]) epnFieldErr(f, '');
  S.ui.rowsErr.textContent = '';
  for (const r of S.rows) if (r.ui) for (const f of Object.values(r.ui.fields)) epnFieldErr(f, '');
}

function epnDepLabel(r, j) { const t = (r.title || '').trim(); return `Issue ${j + 1}${t ? ': ' + (t.length > 40 ? t.slice(0, 40) + '…' : t) : ''}`; }

function epnRefreshDeps(S) {
  S.rows.forEach((r, i) => {
    if (!r.ui) return;
    const sel = r.ui.dep;
    sel.textContent = '';
    sel.append(el('option', { value: '', text: i ? 'none' : 'none (first issue)' }));
    for (let j = 0; j < i; j++) sel.append(el('option', { value: String(S.rows[j].id), text: epnDepLabel(S.rows[j], j) }));
    sel.value = r.dep !== null ? String(r.dep) : '';
    r.ui.fields.dep.classList.toggle('hidden', i === 0);
  });
}

function epnTitleCount(S, r) {
  const n = (r.title || '').trim().length;
  const f = r.ui.fields.title;
  if (n > S.limits.title) epnFieldErr(f, `the title is ${n} characters; the most is ${S.limits.title}`);
  else if (f.errNode.textContent.startsWith('the title is ')) epnFieldErr(f, '');
}

function epnRowNode(S, r) {
  const ui = { fields: {} };
  r.ui = ui;
  ui.title = el('input', { type: 'text', autocomplete: 'off', spellcheck: 'true', placeholder: 'What this issue does', 'data-ctl': 'title',
    oninput: () => { r.title = ui.title.value; epnTitleCount(S, r); epnRefreshDeps(S); epnChanged(S); } });
  ui.desc = el('textarea', { class: 'epn-desc', rows: '2', autocomplete: 'off', spellcheck: 'true', placeholder: 'A sentence or two. It goes under Goal in the issue.', 'data-ctl': 'desc',
    oninput: () => { r.desc = ui.desc.value; epnFieldErr(ui.fields.desc, ''); epnChanged(S); } });
  ui.tierHint = el('span', { class: 'dim field-hint epn-tierhint' });
  ui.tierSeg = epnSeg(EPN_TIERS, r.tier, (v) => { r.tier = v; ui.tierHint.textContent = EPN_TIER_WORDS[v]; epnFieldErr(ui.fields.tier, ''); epnChanged(S); }, 'Tier', 'epn-tier');
  ui.sizeSeg = epnSeg(EPN_SIZES, r.size, (v) => { r.size = v; epnFieldErr(ui.fields.size, ''); epnChanged(S); }, 'Size', 'epn-size');
  ui.dep = el('select', { 'data-ctl': 'dep', onchange: () => { r.dep = ui.dep.value ? Number(ui.dep.value) : null; epnFieldErr(ui.fields.dep, ''); epnChanged(S); } });
  ui.fields.title = field('Title', ui.title);
  ui.fields.desc = field('Description (optional)', ui.desc);
  ui.fields.tier = field('Tier', ui.tierSeg.node);
  ui.fields.tier.append(ui.tierHint);
  ui.fields.size = field('Size', ui.sizeSeg.node);
  ui.fields.dep = field('Depends on', ui.dep);
  ui.head = el('span', { class: 'epn-rowk' });
  const btn = (name, label, icon, fn) => el('button', { class: 'icon minimal epn-rowbtn epn-lockable', type: 'button', 'data-act': name, 'aria-label': label, title: label, onclick: fn }, ic(icon));
  ui.up = btn('up', 'Move this issue up', 'arrow-up', () => epnMove(S, r, -1, 'up'));
  ui.down = btn('down', 'Move this issue down', 'arrow-down', () => epnMove(S, r, 1, 'down'));
  ui.rm = btn('remove', 'Remove this issue', 'cross', () => epnRemove(S, r));
  for (const b of [ui.up, ui.down, ui.rm]) {
    b.addEventListener('keydown', (e) => { if ((e.key === 'Delete' || e.key === 'Backspace') && !e.altKey && !e.ctrlKey && !e.metaKey) { e.preventDefault(); epnRemove(S, r); } });
  }
  const keys = (e) => {
    if (e.altKey && (e.key === 'ArrowUp' || e.key === 'ArrowDown') && !e.ctrlKey && !e.metaKey) { e.preventDefault(); epnMove(S, r, e.key === 'ArrowUp' ? -1 : 1, e.target && e.target.getAttribute ? e.target.getAttribute('data-ctl') : 'title'); return; }
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); epnAdd(S, S.rows.indexOf(r) + 1); }
  };
  ui.title.addEventListener('keydown', keys);
  ui.desc.addEventListener('keydown', keys);
  ui.note = el('div', { class: 'dim epn-rownote', role: 'status', 'aria-live': 'polite' });
  ui.node = el('li', { class: 'epn-row', role: 'group', 'data-row': String(r.id) },
    el('div', { class: 'epn-rowhead' }, ui.head, el('span', { class: 'spacer' }), ui.up, ui.down, ui.rm),
    ui.fields.title, ui.fields.desc,
    el('div', { class: 'epn-pair' }, ui.fields.tier, ui.fields.size),
    ui.fields.dep, ui.note);
  ui.title.value = r.title;
  ui.desc.value = r.desc;
  ui.tierHint.textContent = EPN_TIER_WORDS[r.tier];
  return ui;
}

function epnRenderRows(S) {
  const list = S.ui.list;
  const had = S.focus;
  S.focus = null;
  list.textContent = '';
  S.rows.forEach((r, i) => {
    const ui = r.ui || epnRowNode(S, r);
    ui.head.textContent = `Issue ${i + 1}`;
    ui.node.setAttribute('aria-label', `Issue ${i + 1}`);
    ui.up.disabled = !!(i === 0 || S.locked);
    ui.down.disabled = !!(i === S.rows.length - 1 || S.locked);
    ui.rm.disabled = !!(S.rows.length === 1 || S.locked);
    list.append(ui.node);
  });
  epnRefreshDeps(S);
  S.ui.count.textContent = `${S.rows.length} of ${S.limits.children}`;
  const full = S.rows.length >= S.limits.children;
  S.ui.add.disabled = !!(full || S.locked);
  S.ui.addNote.textContent = full ? `${S.limits.children} issues is the most an epic can have.` : '';
  if (had) {
    const r = S.rows.find((x) => x.id === had.id);
    const ui = r && r.ui;
    let target = ui ? (had.ctl === 'up' ? ui.up : had.ctl === 'down' ? ui.down : had.ctl === 'remove' ? ui.rm : had.ctl === 'desc' ? ui.desc : ui.title) : null;
    if (target && target.disabled) target = ui.title;      // the button it stood on has nothing left to do (first row, last row)
    if (target && typeof target.focus === 'function') target.focus();
  }
}

/* A dependency that points at a row now below or at its own is cleared, and the row says so in words. */
function epnFixDeps(S) {
  const at = new Map(S.rows.map((r, i) => [r.id, i]));
  for (const r of S.rows) {
    if (r.dep === null || !r.ui) { if (r.ui) r.ui.note.textContent = ''; continue; }
    if (!at.has(r.dep) || at.get(r.dep) >= at.get(r.id)) { r.dep = null; r.ui.note.textContent = 'Depends on was cleared: an issue can depend only on one above it.'; } else r.ui.note.textContent = '';
  }
}

function epnAdd(S, index) {
  if (S.locked || S.rows.length >= S.limits.children) return null;
  const r = epnRow(S);
  const i = typeof index === 'number' ? index : S.rows.length;
  S.rows.splice(i, 0, r);
  S.undo = null;
  S.ui.undo.textContent = '';
  S.focus = { id: r.id, ctl: 'title' };
  epnRenderRows(S);
  epnFixDeps(S);
  epnChanged(S);
  return r;
}

function epnMove(S, r, d, ctl) {
  if (S.locked) return;
  const i = S.rows.indexOf(r);
  const j = i + d;
  if (i < 0 || j < 0 || j >= S.rows.length) return;
  S.rows.splice(i, 1);
  S.rows.splice(j, 0, r);
  S.focus = { id: r.id, ctl: ctl || 'title' };
  epnRenderRows(S);
  epnFixDeps(S);
  epnRefreshDeps(S);
  S.ui.status.textContent = `Issue moved to place ${j + 1} of ${S.rows.length}.`;
  epnChanged(S);
}

function epnRemove(S, r) {
  if (S.locked || S.rows.length <= 1) return;
  const i = S.rows.indexOf(r);
  if (i < 0) return;
  S.rows.splice(i, 1);
  const cleared = S.rows.filter((x) => x.dep === r.id).length;
  for (const x of S.rows) if (x.dep === r.id) x.dep = null;
  S.undo = { row: r, index: i };
  const next = S.rows[Math.min(i, S.rows.length - 1)];
  S.focus = { id: next.id, ctl: 'title' };
  epnRenderRows(S);
  epnFixDeps(S);
  S.ui.undo.textContent = '';
  S.ui.undo.append(el('span', { class: 'epn-undo-t', text: `Issue ${i + 1} removed.${cleared ? ` ${epnCount(cleared, 'issue depended', 'issues depended')} on it; that was cleared.` : ''} ` }),
    el('button', { class: 'small minimal epn-undo-b', type: 'button', text: 'Undo', onclick: () => epnUndo(S) }));
  epnChanged(S);
}

function epnUndo(S) {
  const u = S.undo;
  if (!u || S.locked || S.rows.length >= S.limits.children) return;
  S.undo = null;
  S.ui.undo.textContent = '';
  S.rows.splice(Math.min(u.index, S.rows.length), 0, u.row);
  S.focus = { id: u.row.id, ctl: 'title' };
  epnRenderRows(S);
  epnFixDeps(S);
  epnChanged(S);
}

/* ---------- errors from the board: {field, index, message}, each beside its field; every typed value stays ---------- */

function epnShowErrors(S, errors) {
  epnClearErrors(S);
  let first = null;
  const put = (f, message) => { epnFieldErr(f, message); if (!first && f) first = f; };
  for (const e of Array.isArray(errors) ? errors : []) {
    if (!e || typeof e.message !== 'string') continue;
    const row = Number.isInteger(e.index) ? S.rows[e.index] : null;
    const rf = row && row.ui ? row.ui.fields : null;
    switch (e.field) {
      case 'repo': put(S.ui.fRepo, e.message); break;
      case 'title': put(S.ui.fTitle, e.message); break;
      case 'goal': put(S.ui.fGoal, e.message); break;
      case 'child_title': put(rf && rf.title, e.message); break;
      case 'child_description': put(rf && rf.desc, e.message); break;
      case 'child_tier': put(rf && rf.tier, e.message); break;
      case 'child_size': put(rf && rf.size, e.message); break;
      case 'depends_on': put(rf && rf.dep, e.message); break;
      default: S.ui.rowsErr.textContent = (S.ui.rowsErr.textContent ? S.ui.rowsErr.textContent + ' ' : '') + e.message; if (!first) first = S.ui.rowsErr; break;
    }
  }
  if (first) {
    const t = first.target || first;
    try { if (typeof first.scrollIntoView === 'function') first.scrollIntoView({ block: 'center', behavior: typeof scrollBehavior === 'function' ? scrollBehavior() : 'auto' }); } catch (_) { /* no layout */ }
    if (t && typeof t.focus === 'function') t.focus();
  }
}

/* ---------- the preview ---------- */

function epnBodyBlock(label, body) {
  return el('details', { class: 'epn-body' }, el('summary', { text: label }), el('pre', { class: 'epn-pre mono', text: String(body || '') }));
}

function epnChip(text, title) { return el('span', { class: 'epn-chip mono', text, title: title || null }); }

function epnPreviewPaint(S) {
  const host = S.ui.preview_host;
  host.textContent = '';
  const p = S.preview;
  host.classList.toggle('hidden', !p);
  host.classList.toggle('stale', !!p && S.previewSig !== epnSig(S));
  if (!p) return;
  const c = p.calls || {};
  const vis = typeof p.private === 'boolean' ? (p.private ? 'private' : 'public') : null;
  const notes = [];
  notes.push(el('p', { class: 'epn-calls', 'data-calls': String(c.writes) },
    `${epnCount(c.writes, 'GitHub call', 'GitHub calls')}: ${epnCount(c.labels, 'label', 'labels')}, ${epnCount(c.issues, 'issue', 'issues')}, ${epnCount(c.attachments, 'attachment', 'attachments')} and ${epnCount(c.edits, 'edit', 'edits')} of the epic. ${epnSeconds(c.est_seconds)[0].toUpperCase()}${epnSeconds(c.est_seconds).slice(1)}, one at a time.`));
  notes.push(el('p', { class: 'epn-vis' + (vis === 'public' || vis === null ? ' warn' : ''), 'data-vis': vis || 'unknown' },
    el('span', { class: 'epn-glyph', 'aria-hidden': 'true', text: vis === 'private' ? '○' : vis === 'public' ? '!' : '?' }),
    vis === 'public' ? `${p.repo} is public: anyone can read what is written.` : vis === 'private' ? `${p.repo} is private.` : `Whether ${p.repo} is public is not known.`));
  const parent = el('li', { class: 'epn-pitem', 'data-kind': 'parent' },
    el('div', { class: 'epn-ptitle' }, el('span', { class: 'epn-pk', text: 'Epic' }), ' ', el('span', { class: 'epn-pt', text: p.parent.title }), ' ', ...(p.parent.labels || []).map((l) => epnChip(l, 'label'))),
    epnBodyBlock('Body', p.parent.body), epnBodyBlock('Body after the list is filled in', p.parent.edit_body));
  const kids = (p.children || []).map((k) => el('li', { class: 'epn-pitem', 'data-kind': 'child', 'data-row': String(k.row) },
    el('div', { class: 'epn-ptitle' }, el('span', { class: 'epn-pk', text: `Issue ${k.row}` }), ' ', el('span', { class: 'epn-pt', text: k.title }), ' ', ...(k.labels || []).map((l) => epnChip(l, 'label')),
      epnChip(`Tier ${k.tier}`, 'tier'), epnChip(`Size ${k.size}`, 'size'), k.depends_on ? epnChip(`after issue ${k.depends_on}`, 'depends on') : null),
    el('div', { class: 'dim epn-models mono', text: `${k.claude}  |  ${k.codex}` }), epnBodyBlock('Body', k.body)));
  if ((p.children || []).some((k) => /#\[row [0-9]+\]/.test(k.body)) || /#\[row [0-9]+\]/.test(p.parent.edit_body || '')) notes.push(el('p', { class: 'dim epn-hint', text: '#[row 2] stands for the number GitHub gives that issue when it is written.' }));
  host.append(el('h3', { class: 'epn-sec', text: 'Preview' }), ...notes, el('ul', { class: 'epn-plist' }, parent, ...kids),
    el('p', { class: 'epn-stale-note warn', role: 'status', text: 'You changed something after this preview. Preview again before Create.' }));
}

/* ---------- what exists on GitHub after a failed create ---------- */

function epnExistsPaint(S) {
  const host = S.ui.exists;
  host.textContent = '';
  const d = S.result;
  host.classList.toggle('hidden', !d);
  if (!d) return;
  const [o, r] = String(d.repo || S.repo).split('/');
  const url = (n) => `${EPN_GH}${o}/${r}/issues/${n}`;
  const items = [];
  if (d.parent && d.parent.number) items.push(el('li', { class: 'epn-ex', 'data-ex': 'parent' }, el('span', { class: 'epn-exk', text: 'Epic' }), ' ', epnLink(d.parent.url || url(d.parent.number), `#${d.parent.number}`), ' ', el('span', { class: 'dim', text: d.parent.state === 'existing' ? 'was already there' : 'written' })));
  for (const k of d.children || []) items.push(el('li', { class: 'epn-ex', 'data-ex': String(k.row) }, el('span', { class: 'epn-exk', text: `Issue ${k.row}` }), ' ', epnLink(k.url || url(k.number), `#${k.number}`), ' ',
    el('span', { class: 'dim', text: `${k.state === 'existing' ? 'was already there' : 'written'}${k.attached ? ', attached' : ', not attached yet'}` })));
  host.append(el('h3', { class: 'epn-sec', text: 'On GitHub now' }),
    items.length ? el('ul', { class: 'epn-exlist' }, ...items) : el('p', { class: 'dim', text: 'Nothing was written.' }));
}

/* ---------- Preview ---------- */

async function epnPreview(S) {
  if (S.phase === 'creating' || S.phase === 'previewing' || S.phase === 'partial' || !S.repo) return;
  epnClearErrors(S);
  const sig = epnSig(S);
  S.phase = 'previewing';
  epnPaintActions(S);
  try {
    const d = await api('POST', '/api/epics/preview', epnPayload(S));
    if (epicNew.S !== S) return;
    S.phase = 'idle';
    S.preview = d;
    S.previewSig = sig;
    S.private = d && typeof d.private === 'boolean' ? d.private : null;
    if (d && d.writes_on === false) S.writesOn = false;
    epnPreviewPaint(S);
    epnPaintActions(S);
    S.ui.status.textContent = `Preview ready: ${epnCount(d.calls.issues, 'issue', 'issues')}, ${epnCount(d.calls.writes, 'GitHub call', 'GitHub calls')}.`;
    if (typeof S.ui.preview_host.scrollIntoView === 'function') { try { S.ui.preview_host.scrollIntoView({ block: 'nearest', behavior: typeof scrollBehavior === 'function' ? scrollBehavior() : 'auto' }); } catch (_) { /* no layout */ } }
  } catch (e) {
    if (epicNew.S !== S) return;
    S.phase = 'idle';
    S.preview = null;
    S.previewSig = null;
    epnPreviewPaint(S);
    if (e && e.status === 400 && e.body && Array.isArray(e.body.errors)) epnShowErrors(S, e.body.errors);
    else S.ui.rowsErr.textContent = e && e.message ? `The board could not make a preview: ${e.message}` : 'The board could not make a preview. Try again.';
    epnPaintActions(S);
  }
}

/* ---------- Create (and Resume: the same request) ---------- */

function epnTick(S) {
  S.elapsed = Math.max(0, Math.round((Date.now() - S.started) / 1000));
  if (S.ui && S.phase === 'creating') S.ui.words.textContent = epnWording(S);
}

function epnStopTimer(S) { if (S.timer) { clearInterval(S.timer); S.timer = null; } }

function epnAfter(S, text, partial) {
  S.phase = partial ? 'partial' : 'idle';
  S.partialText = partial ? text : '';
  S.locked = !!partial;
  epnLock(S, !!partial);
  S.ui.status.textContent = text;
  epnPaintActions(S);
}

async function epnCreate(S) {
  const resume = S.phase === 'partial';
  if (!(resume ? epnWritesOn(S) : epnCanCreate(S))) return;
  if (S.phase === 'creating') return;
  epnClearErrors(S);
  S.phase = 'creating';
  S.locked = true;
  S.writing = S.preview.calls.issues;
  S.estimate = S.preview.calls.est_seconds;
  S.started = Date.now();
  S.elapsed = 0;
  epnLock(S, true);
  epnPaintActions(S);
  S.ui.status.textContent = `Writing ${epnCount(S.writing, 'issue', 'issues')} to ${S.repo}.`;
  S.timer = setInterval(() => epnTick(S), 5000);                  // a screen reader hears the sentence each time it changes: every 5 s is often enough
  const payload = epnPayload(S);
  try {
    const d = await api('POST', '/api/epics', payload);
    epnStopTimer(S);
    S.result = d;
    if (d && d.ok === true && d.parent && d.parent.number) { epnDone(S, d); return; }
    if (epicNew.S !== S) { epnLate(S, d); return; }
    epnExistsPaint(S);
    const where = epnSteps(d && d.step);
    const why = (d && d.error) || 'GitHub did not accept a write.';
    const head = where ? `Stopped ${where}: ` : 'Stopped: ';
    if (d && d.reason === 'differs') {                              // Resume would find the same text: the person edits the issue on GitHub or starts a new epic
      const said = [head + epnSentence(why)].concat(Array.isArray(d.notes) ? d.notes.map(epnSentence) : []).join(' ');
      epnAfter(S, said, false);
      toast(said, { kind: 'bad' });
      return;
    }
    epnAfter(S, head + epnSentence(why) + (d && d.unknown ? ' ' + EPN_UNKNOWN : '') + ' Resume writes only what is missing.', true);
  } catch (e) {
    epnStopTimer(S);
    if (epicNew.S !== S) { epnLate(S, null, e); return; }
    const body = e && e.body && typeof e.body === 'object' ? e.body : null;
    if (e && e.status === 409 && body && body.reason === 'writes_off') { S.writesOn = false; epnAfter(S, body.error || EPN_WRITES_OFF, false); return; }
    if (e && e.status === 409 && body) { epnAfter(S, (body.error || 'The board did not start.') + (body.reason === 'in_progress' ? ' Wait for it to finish, then press Resume.' : ''), body.reason === 'in_progress'); return; }
    if (e && e.status === 400 && body && Array.isArray(body.errors)) { S.previewSig = null; epnAfter(S, 'The board did not accept some of the text. Nothing was written.', false); epnShowErrors(S, body.errors); epnPaintActions(S); return; }
    if (e && typeof e.status === 'number' && e.status >= 400 && e.status < 500) { epnAfter(S, `${e.message || 'The board refused.'} Nothing was written.`, false); return; }
    epnAfter(S, EPN_BROKEN, true);
  }
}

/* The sheet was closed while the board was still writing: say how it ended in a toast, since nothing else will. */
function epnLate(S, d, err) {
  if (d && d.ok === true && d.parent) { toast(`Created ${d.repo}#${d.parent.number} with ${epnCount((d.children || []).length, 'issue', 'issues')}.`, { kind: 'ok' }); if (typeof Epics !== 'undefined') Epics.at = 0; return; }
  toast(`Creating the epic stopped${d && d.error ? ': ' + d.error : (err && err.message ? ': ' + err.message : '')}. Open New epic again and press Create with the same titles: it writes only what is missing.`, { kind: 'bad' });
}

function epnDone(S, d) {
  const open = epicNew.S === S;
  const [o, r] = String(d.repo || S.repo).split('/');
  const n = d.parent.number;
  epnRememberRepo(S.repo);
  if (typeof Epics !== 'undefined') { Epics.at = 0; Epics.list = null; }
  const k = (d.children || []).length;
  if (open) closeSheet();
  toast(`Created ${o}/${r}#${n} with ${epnCount(k, 'issue', 'issues')}${d.resumed ? ' (some were already there)' : ''}.${open ? ' Run is on this page.' : ''}`, { kind: 'ok' });
  let h = null;
  try { h = typeof Epics !== 'undefined' && typeof Epics.hash === 'function' ? Epics.hash(o, r, n) : buildHash('epic', { owner: o, repo: r, n }); } catch (_) { h = null; }
  if (h && open && typeof location !== 'undefined') location.hash = h;
}

/* "Edit as a new epic": give the values back after a stop. The issues already written stay on GitHub. */
function epnUnlock(S) {
  S.phase = 'idle';
  S.partialText = '';
  S.result = null;
  S.previewSig = null;
  S.locked = false;
  epnLock(S, false);
  epnExistsPaint(S);
  epnPreviewPaint(S);
  epnPaintActions(S);
  S.ui.status.textContent = 'You can edit again. Issues already written stay on GitHub, and a changed title makes a different epic.';
}

/* ---------- the sheet ---------- */

function epnOptionsPaint(S) {
  const u = S.ui;
  u.form.classList.toggle('hidden', !S.ready || !S.repos.length);
  u.empty.classList.toggle('hidden', !(S.ready && !S.repos.length && !S.loadFailed));
  u.failed.classList.toggle('hidden', !S.loadFailed);
  u.loading.classList.toggle('hidden', S.ready || S.loadFailed);
  u.repoSel.textContent = '';
  for (const x of S.repos) u.repoSel.append(el('option', { value: x.repo, text: x.repo }));
  if (S.repo) u.repoSel.value = S.repo;
  u.writesOff.classList.toggle('hidden', epnWritesOn(S));
  u.actions.classList.toggle('hidden', !S.ready || !S.repos.length);
}

async function epnLoad(S) {
  S.loadFailed = false;
  S.ready = false;
  epnOptionsPaint(S);
  try {
    const d = await api('GET', '/api/epics/new' + (S.opts.project ? '?project=' + encodeURIComponent(S.opts.project) : ''));
    if (epicNew.S !== S) return;
    S.repos = (d && Array.isArray(d.repos) ? d.repos : []).filter((x) => x && typeof x.repo === 'string' && EPN_REPO_RE.test(x.repo));
    S.writesOn = d && d.writes_on === false ? false : true;
    if (d && d.limits && Number.isInteger(d.limits.children) && Number.isInteger(d.limits.title)) S.limits = { ...S.limits, ...d.limits };
    const want = [S.opts.repo, epnLastRepo()].find((x) => x && S.repos.some((y) => y.repo.toLowerCase() === String(x).toLowerCase()));
    S.repo = want ? S.repos.find((y) => y.repo.toLowerCase() === String(want).toLowerCase()).repo : (S.repos[0] ? S.repos[0].repo : '');
    S.ready = true;
    epnOptionsPaint(S);
    epnRenderRows(S);
    epnPaintActions(S);
    if (S.repos.length && typeof focusFine === 'function') focusFine(S.ui.titleIn);
  } catch (e) {
    if (epicNew.S !== S) return;
    S.loadFailed = true;
    S.loadMessage = e && e.message ? e.message : 'The board did not answer.';
    S.ui.failedText.textContent = `Could not read this board's repositories: ${S.loadMessage}`;
    epnOptionsPaint(S);
  }
}

function epnBuild(S) {
  const u = {};
  S.ui = u;
  u.loading = el('p', { class: 'dim epn-loading', role: 'status', text: 'Reading this board\'s repositories…' });
  u.empty = el('div', { class: 'epn-empty hidden' }, el('b', { text: 'No GitHub repository here' }),
    el('p', { class: 'dim', text: 'An epic is written to a repository of one of this board\'s projects, read from its origin remote. None of them has a github.com remote.' }));
  u.failedText = el('span', { class: 'epn-ft' });
  u.failed = el('div', { class: 'epn-failed hidden', role: 'alert' }, u.failedText, el('button', { class: 'small', type: 'button', text: 'Try again', onclick: () => epnLoad(S) }));
  u.repoSel = el('select', { 'data-ctl': 'repo', onchange: () => { S.repo = u.repoSel.value; S.private = null; epnFieldErr(u.fRepo, ''); epnChanged(S); } });
  u.fRepo = field('Repository', u.repoSel, 'Where the issues are written. These are the GitHub remotes of this board\'s projects.');
  u.writesOff = el('p', { class: 'epn-writesoff warn hidden', role: 'status', text: EPN_WRITES_OFF });
  u.titleIn = el('input', { type: 'text', autocomplete: 'off', spellcheck: 'true', placeholder: 'e.g. Checkout rework', 'data-ctl': 'epic-title',
    oninput: () => {
      S.title = u.titleIn.value;
      const n = S.title.trim().length;
      if (n > S.limits.title) epnFieldErr(u.fTitle, `the title is ${n} characters; the most is ${S.limits.title}`); else if (u.fTitle.errNode.textContent.startsWith('the title is ')) epnFieldErr(u.fTitle, '');
      epnChanged(S);
    } });
  u.fTitle = field('Epic title', u.titleIn, `Up to ${S.limits.title} characters.`);
  u.goalIn = el('textarea', { class: 'epn-goal', rows: '3', autocomplete: 'off', spellcheck: 'true', placeholder: 'What this epic is for, in a few lines.', 'data-ctl': 'goal',
    oninput: () => { S.goal = u.goalIn.value; epnFieldErr(u.fGoal, ''); epnChanged(S); } });
  u.fGoal = field('Goal (optional)', u.goalIn, 'The epic issue opens with this. Without it the title is used.');
  u.list = el('ol', { class: 'epn-rows' });
  u.count = el('span', { class: 'dim epn-n' });
  u.rowsErr = el('div', { class: 'field-err bad epn-rowserr', role: 'alert' });
  u.undo = el('div', { class: 'epn-undo dim', role: 'status', 'aria-live': 'polite' });
  u.add = el('button', { class: 'epn-add epn-lockable', type: 'button', onclick: () => epnAdd(S) }, ic('plus'), 'Add issue');
  u.addNote = el('span', { class: 'dim epn-addnote' });
  u.exists = el('section', { class: 'epn-exists hidden', 'aria-label': 'What exists on GitHub' });
  u.preview_host = el('section', { class: 'epn-preview hidden', 'aria-label': 'Preview' });
  u.status = el('div', { class: 'epn-status', role: 'status', 'aria-live': 'polite' });
  u.body = el('div', { class: 'epn-form' });
  u.form = el('div', { class: 'form epn-fields hidden' },
    el('h3', { class: 'epn-sec', text: 'Repository' }), u.fRepo, u.writesOff,
    el('h3', { class: 'epn-sec', text: 'Epic' }), u.fTitle, u.fGoal,
    el('h3', { class: 'epn-sec' }, 'Issues ', u.count),
    u.list, u.rowsErr, u.undo, el('div', { class: 'epn-addrow' }, u.add, u.addNote), el('p', { class: 'dim epn-hint', text: EPN_KEYS }),
    u.exists, u.preview_host);
  u.body.append(el('p', { class: 'dim epn-lead', text: 'Fill in the epic, Preview it, then Create. Nothing is written to GitHub until Create.' }), u.loading, u.empty, u.failed, u.form, u.status);
  u.words = el('p', { class: 'epn-words', role: 'status', 'aria-live': 'polite' });
  u.preview = el('button', { class: 'epn-prev', type: 'button', text: 'Preview', onclick: () => epnPreview(S) });
  u.create = el('button', { class: 'primary epn-create', type: 'button', text: 'Create', onclick: () => epnCreate(S) });
  u.create.disabled = true;
  u.again = el('button', { class: 'minimal small epn-again hidden', type: 'button', text: 'Edit as a new epic', onclick: () => epnUnlock(S) });
  u.actions = el('div', { class: 'epn-actions hidden' }, u.words, el('div', { class: 'epn-btns' }, u.again, u.preview, u.create));
  return u;
}

function epicNewClose(S) {
  epnStopTimer(S);
  if (epicNew.S === S) epicNew.S = null;
  const dlg = typeof document !== 'undefined' ? document.getElementById('sheet') : null;
  if (dlg && dlg.classList) dlg.classList.remove('epn-wide');
  const inv = S.invoker;
  if (inv && typeof inv.focus === 'function' && document.body && document.body.contains(inv)) { try { inv.focus(); } catch (_) { /* gone */ } }
}

/* Open the sheet. opts: {repo?: 'owner/name', project?: 'name'} (the repository to start on; the project's repositories come first). Returns the sheet's state (tests read it). */
function epicNewOpen(opts) {
  if (typeof openSheet !== 'function') return null;
  const S = { opts: opts && typeof opts === 'object' ? opts : {}, ids: 0, rows: [], limits: { ...EPN_LIMITS }, repos: [], repo: '', title: '', goal: '', ready: false, loadFailed: false, writesOn: null,
    preview: null, previewSig: null, private: null, phase: 'idle', locked: false, result: null, partialText: '', undo: null, focus: null, timer: null, started: 0, elapsed: 0, writing: 0, estimate: 0,
    invoker: typeof document !== 'undefined' ? document.activeElement : null, ui: null };
  if (epicNew.S) epnStopTimer(epicNew.S);
  epicNew.S = S;
  S.rows.push(epnRow(S));
  const u = epnBuild(S);
  const sheet = openSheet({ title: 'New epic', body: u.body, actions: [u.actions], placement: 'right', onClose: () => epicNewClose(S) });
  if (!sheet) { epicNew.S = null; return null; }
  if (sheet.dialog && sheet.dialog.classList) sheet.dialog.classList.add('epn-wide');
  epnLoad(S);
  return S;
}

/* ---------- the demo (?demo=1): the board's three answers, played in this page. ?newepic=writesoff|fail|unknown|private|norepo|down picks a state ---------- */

const demoNew = { n: 300, epics: {}, tries: {} };            // number counter; "key" -> what the visitor's create wrote; "key" -> how many creates were pressed
function demoNewFlag() { try { const m = /[?&]newepic=([a-z]+)/.exec(location.search); return m ? m[1] : ''; } catch (_) { return ''; } }

async function demoSha12(raw) {
  try {
    if (typeof crypto !== 'undefined' && crypto.subtle) {
      const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(raw));
      return Array.from(new Uint8Array(buf)).map((b) => b.toString(16).padStart(2, '0')).join('').slice(0, 12);
    }
  } catch (_) { /* an insecure page has no subtle crypto */ }
  let h = 2166136261;
  for (let i = 0; i < raw.length; i++) { h ^= raw.charCodeAt(i); h = Math.imul(h, 16777619) >>> 0; }
  return (h.toString(16).padStart(8, '0') + h.toString(16).padStart(8, '0')).slice(0, 12);
}

const DEMO_NEW_TIERS = {            // the tier table of CONTRIBUTING.md, as the board writes it (tests/test_epic_new_demo.py holds this to app/nodes_epics.py)
  A: ['claude --model opus --effort high --permission-mode acceptEdits', 'codex -m gpt-6.1-sol -c model_reasoning_effort="high" -s workspace-write -a on-request', 'open design, risky or cross-cutting work.', 'mechanical parts may go to a cheaper tier.'],
  B: ['claude --model sonnet --effort medium --permission-mode acceptEdits', 'codex -m gpt-6.1-sol -c model_reasoning_effort="medium" -s workspace-write -a on-request', 'standard feature work: code, pages and the tests with them.', 'text, docs and mechanical edits may go to tier C.'],
  C: ['claude --model haiku --effort low --permission-mode acceptEdits', 'codex -m gpt-6-luna -c model_reasoning_effort="low" -s workspace-write -a on-request', 'mechanical work with a clear recipe.', 'none.'],
  check: ['claude --model sonnet --effort medium --permission-mode acceptEdits', 'codex -m gpt-6.1-sol -s workspace-write -a on-request', 'a check or review of finished work, started on the machine that holds the branch.', 'none.'],
};

function demoNewOneLine(s) { return String(s || '').replace(/[\u0000-\u001f\u007f-\u009f​-‏‪-‮⁦-⁩﻿]/g, ' ').replace(/\s+/g, ' ').trim(); }
function demoNewText(s) {
  const lines = String(s || '').replace(/\r\n?/g, '\n').replace(/[\u0000-\u0009\u000b-\u001f\u007f-\u009f​-‏‪-‮⁦-⁩﻿]/g, '').split('\n').map((l) => l.replace(/\s+$/, ''));
  return lines.join('\n').replace(/\n{3,}/g, '\n\n').replace(/^\n+|\n+$/g, '');
}
function demoNewEsc(text) {
  return text.replace(/</g, '&lt;').replace(/```/g, '` ` `').replace(/~~~/g, '~ ~ ~').split('\n').map((ln) => (/^[ \t]*#/.test(ln) || /^[ \t>*+-]*depends[ \t]+on/i.test(ln) ? '\\' + ln : ln)).join('\n');
}
function demoNewWho(tier) { const t = DEMO_NEW_TIERS[tier]; return `- Claude: \`${t[0]}\`\n- Codex: \`${t[1]}\`\n- Why this tier: ${t[2]}\n- Cheaper parts: ${t[3]}`; }
const DEMO_NEW_LIST = '<!-- ccboard:epic-list -->';
function demoNewParentBody(spec, key) {
  return `## Goal\n\n${demoNewEsc(spec.goal || spec.title)}\n\n## Issues\n\n${DEMO_NEW_LIST}\n\n## How this epic runs\n\nThe issues listed above are the sub-issues of this epic, in order. The board's Epics tab shows who works on which.\n\n<!-- ccboard:epic ${key} -->`;
}
function demoNewChildBody(spec, row, key, numbers) {
  const c = spec.children[row - 1];
  const dep = c.depends_on;
  const refs = dep ? `- depends on ${numbers && numbers[dep] ? '#' + numbers[dep] : `#[row ${dep}]`}` : '- none yet';
  return ['## Goal', '', demoNewEsc(c.description || c.title), '', `Size: ${c.size}`, '',
    '## Why / evidence', '', '<!-- What was seen, where, and on which commit. -->', '',
    '## Scope', '', '- [ ]', '', '## Files', '', '<!-- "Read first" and "Change" lists. -->', '',
    '## Tests', '', '<!-- The tests to add or change, and the two suites staying green. -->', '', '## Acceptance', '', '- [ ]', '',
    '## Who should do it', '', demoNewWho(c.tier), '',
    '## Rules', '', '- Keep emails, tokens, host names and personal paths out of anything you write here. Say "the box".',
    '- Run both test suites before opening the pull request. Work lands through a pull request.', '',
    '## References', '', refs, '', `<!-- ccboard:epic-child ${key}-${String(row).padStart(2, '0')} -->`].join('\n');
}

/* The board's own checks, as far as the demo needs them: every problem at once, as {field, index, message}. */
function demoNewNormalize(raw, repos) {
  const errors = [];
  const bad = /bypass|dangerously|danger-full-access|--yolo/i;
  const refuse = (field, index, text) => { if (bad.test(text)) errors.push({ field, index, message: 'this text names a permission bypass, which the board never writes into an issue' }); };
  const repo = repos.find((r) => r.toLowerCase() === String(raw.repo || '').toLowerCase());
  if (!repo) throw Object.assign(demoError(404, 'played'), { body: { error: `this board has no project for ${raw.repo}` } });
  const title = demoNewOneLine(raw.title);
  if (!title) errors.push({ field: 'title', index: null, message: 'give the epic a title' });
  else if (title.length > 120) errors.push({ field: 'title', index: null, message: `the title is ${title.length} characters; the most is 120` });
  else refuse('title', null, title);
  const goal = demoNewText(raw.goal);
  refuse('goal', null, goal);
  const kidsIn = Array.isArray(raw.children) ? raw.children : [];
  if (!kidsIn.length) errors.push({ field: 'children', index: null, message: 'add at least one issue' });
  else if (kidsIn.length > 20) errors.push({ field: 'children', index: null, message: `an epic has at most 20 issues; this one has ${kidsIn.length}` });
  const kids = [];
  kidsIn.slice(0, 20).forEach((k, i) => {
    const t = demoNewOneLine(k.title);
    if (!t) errors.push({ field: 'child_title', index: i, message: 'give this issue a title' });
    else if (t.length > 120) errors.push({ field: 'child_title', index: i, message: `the title is ${t.length} characters; the most is 120` });
    else refuse('child_title', i, t);
    const desc = demoNewText(k.description);
    refuse('child_description', i, desc);
    let tier = k.tier;
    if (!ownKey(DEMO_NEW_TIERS, tier)) { errors.push({ field: 'child_tier', index: i, message: 'the tier is A, B, C or check' }); tier = 'B'; }
    let size = k.size;
    if (!['S', 'M', 'L'].includes(size)) { errors.push({ field: 'child_size', index: i, message: 'the size is S, M or L' }); size = 'M'; }
    let dep = k.depends_on === undefined ? null : k.depends_on;
    if (dep !== null && !(Number.isInteger(dep) && dep >= 1 && dep <= i)) { errors.push({ field: 'depends_on', index: i, message: 'depends on must be the number of an earlier row' }); dep = null; }
    kids.push({ title: t, description: desc, tier, size, depends_on: dep });
  });
  if (errors.length) throw Object.assign(demoError(400, errors[0].message), { body: { error: errors[0].message, errors } });
  return { repo, title, goal, children: kids };
}

async function demoNewPreview(spec, flag) {
  const key = await demoSha12([spec.repo.toLowerCase(), spec.title, ...spec.children.slice().map((c) => c.title)].join('\n'));
  const k = spec.children.length;
  const pb = demoNewParentBody(spec, key);
  const lines = Array.from({ length: k }, (_, i) => `- [ ] #[row ${i + 1}]`);
  const n = 2 + 1 + k + k + 1;
  const est = Math.round(Math.max(0, n - 1) * 1.1 * 10) / 10;
  return {
    repo: spec.repo, private: flag === 'private', writes_on: flag !== 'writesoff', key,
    calls: { writes: n, labels: 2, issues: 1 + k, attachments: k, edits: 1, min_gap_s: 1.1, min_seconds: est, est_seconds: est },
    parent: { title: spec.title, labels: ['ccboard:epic'], body: pb, edit_body: pb.replace(DEMO_NEW_LIST + '\n', DEMO_NEW_LIST + '\n' + lines.map((l) => l + '\n').join('')), marker: `<!-- ccboard:epic ${key} -->` },
    children: spec.children.slice().map((c, i) => ({ index: i, row: i + 1, slug: `${key}-${String(i + 1).padStart(2, '0')}`, title: c.title, labels: ['ccboard:epic-child'], body: demoNewChildBody(spec, i + 1, key), marker: `<!-- ccboard:epic-child ${key}-${String(i + 1).padStart(2, '0')} -->`,
      tier: c.tier, size: c.size, depends_on: c.depends_on, claude: DEMO_NEW_TIERS[c.tier][0], codex: DEMO_NEW_TIERS[c.tier][1] })),
    limits: { children: 20, title: 120, body: 10000 },
  };
}

/* The visitor's create: all of it, or (?newepic=fail) a stop at issue 2 that Resume finishes, or (?newepic=unknown) a stop whose outcome is not known, found again by its marker. Nothing leaves the page. */
async function demoNewCreate(spec, flag) {
  const pv = await demoNewPreview(spec, flag);
  const key = pv.key;
  const [o, r] = spec.repo.split('/');
  const url = (n) => `${EPN_GH}${o}/${r}/issues/${n}`;
  const rec = demoNew.epics[key] || (demoNew.epics[key] = { parent: null, kids: [], spec });
  rec.spec = spec;
  demoNew.tries[key] = (demoNew.tries[key] || 0) + 1;
  const resumed = !!rec.parent;
  await new Promise((resolve) => setTimeout(resolve, 900));
  const out = { ok: false, repo: spec.repo, key, resumed, writes: 0, parent: null, children: [], done: false, notes: [], error: null, reason: null, unknown: false, step: null };
  const view = () => {
    out.parent = rec.parent ? { number: rec.parent.number, url: url(rec.parent.number), state: rec.parent.fresh ? 'created' : 'existing' } : null;
    out.children = rec.kids.map((c, i) => ({ index: i, row: i + 1, slug: `${key}-${String(i + 1).padStart(2, '0')}`, number: c.number, url: url(c.number), state: c.fresh ? 'created' : 'existing', attached: c.attached }));
  };
  for (const c of rec.kids) c.fresh = false;
  if (rec.parent) rec.parent.fresh = false;
  if (!rec.parent) { rec.parent = { number: ++demoNew.n, fresh: true }; out.writes += 3; }
  for (let i = rec.kids.length; i < spec.children.length; i++) {
    if (i === 1 && flag === 'fail' && demoNew.tries[key] === 1) {
      view();
      return { ...out, error: 'GitHub asked us to wait', reason: 'rate_limited', step: 'child 2' };
    }
    if (i === 1 && flag === 'unknown' && demoNew.tries[key] === 1) {
      rec.kids.push({ number: ++demoNew.n, fresh: true, attached: false });          // the write happened on GitHub; the answer never came back
      view();
      return { ...out, error: 'gh did not answer in time, so the outcome of the write is not known', reason: 'timeout', unknown: true, step: 'child 2', notes: ['GitHub may have applied the last write; press Create again and the board looks before it writes'] };
    }
    rec.kids.push({ number: ++demoNew.n, fresh: true, attached: false });
    out.writes += 1;
  }
  for (const c of rec.kids) if (!c.attached) { c.attached = true; out.writes += 1; }
  out.writes += 1;
  view();
  const detail = { repo: spec.repo, number: rec.parent.number, title: spec.title, kids: rec.kids.map((c, i) => ({ number: c.number, title: spec.children[i].title })) };
  demoNew.byRef = demoNew.byRef || {};
  demoNew.byRef[`${spec.repo.toLowerCase()}#${rec.parent.number}`] = detail;
  return { ...out, ok: true, done: true };
}

/* core.js demoApi hands every /api/epics/new, /api/epics/preview and POST /api/epics here (before the epic view's own answers). */
async function demoEpicNew(method, path, body) {
  const bare = path.split('?')[0];
  const flag = demoNewFlag();
  await new Promise((resolve) => setTimeout(resolve, 60));
  if (method === 'GET' && bare === '/api/epics/new') {
    if (flag === 'down') throw demoError(503, 'The board did not answer');
    const q = new URLSearchParams(path.split('?')[1] || '');
    const all = flag === 'norepo' ? [] : [{ repo: 'example/website', project: 'website' }, { repo: 'example/board', project: 'ccboard' }];
    const first = q.get('project');
    all.sort((a, b) => (a.project === first ? 0 : 1) - (b.project === first ? 0 : 1));
    return { repos: all, writes_on: flag !== 'writesoff', limits: { children: 20, title: 120, body: 10000 } };
  }
  const repos = ['example/website', 'example/board'];
  const spec = demoNewNormalize(body || {}, repos);
  if (bare === '/api/epics/preview') return demoNewPreview(spec, flag);
  if (flag === 'writesoff') { const e = demoError(409, 'played'); e.body = { error: 'this board does not write to GitHub (CCBOARD_EPIC_WRITES is off). Preview still works and writes nothing.', reason: 'writes_off' }; throw e; }
  return demoNewCreate(spec, flag);
}

/* The list of the Epics tab with the epics the visitor made on top. */
function demoNewListed(l) {
  const made = Object.values(demoNew.byRef || {});
  if (!made.length || !l || !Array.isArray(l.epics)) return l;
  const rows = made.map((m) => {
    const [o, r] = m.repo.split('/');
    return { repo: m.repo, owner: o, name: r, number: m.number, title: m.title, url: `${EPN_GH}${m.repo}/issues/${m.number}`, state: 'open', children: { open: m.kids.length, closed: 0, total: m.kids.length, more: false },
      updated_at: l.server_now, labels: ['ccboard:epic'], paused: false };
  });
  return { ...l, epics: [...rows.reverse(), ...l.epics] };
}

/* The epic page of an epic the visitor just created (demoEpics asks for it by key): the parent and its new issues, all open and unclaimed, as the board would read them from GitHub. */
function demoNewBase(fx, key) {
  const rec = demoNew.byRef && ownKey(demoNew.byRef, key) ? demoNew.byRef[key] : null;
  const tpl = fx && fx.details ? fx.details['example/website#147'] : null;
  if (!rec || !tpl) return null;
  const [o, r] = rec.repo.split('/');
  const gh = `${EPN_GH}${rec.repo}/issues/`;
  const now = tpl.server_now;
  const kids = rec.kids.map((c, i) => ({ repo: rec.repo, number: c.number, title: c.title, state: 'open', url: gh + c.number, labels: ['ccboard:epic-child'], depends_on: [], blocked_by: [], dependency_cycle: false, order: i + 1,
    updated_at: now, claim: null, claim_rejected: 0, claim_note: null, task: null, pr: null, pin: null, label_pin: null, bucket: { node: null, state: 'open' }, claim_state: 'none', claim_unknown: null,
    claims_read_at: now, author: 'maya-r', author_ok: true, work: null, phase: 'open', route: null }));
  return { ...tpl, epic: { repo: rec.repo, owner: o, name: r, number: rec.number, title: rec.title, url: gh + rec.number, state: 'open', labelled: true, updated_at: now }, children: kids,
    progress: { by_node: {}, unclaimed: { open: kids.length, closed: 0 }, total: kids.length, open: kids.length, closed: 0, unknown: 0 }, pins: {}, epic_pin: null, paused: false, own_claims: [],
    route: { ...tpl.route, running: false, assigned: {}, waiting: {}, held: {} } };
}
