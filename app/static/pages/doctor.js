/* ccboard Settings > Doctor (v0.5.19): the box's checklist from GET /api/doctor, drawn as labelled sections (Box, Claude, Codex, Notifications) of rows: a status glyph, the
   check's name and one line of detail, and, for anything that is not passing, what to do about it (the fix text, a command with a Copy button, or a button for a fix the board
   can start: Log in, Send test). Never shown on its own: nothing opens this tab for you, and the only trace elsewhere is one quiet line under the Settings tabs when a check fails.
   Re-check asks again with ?refresh=1 (the server otherwise answers from a 20 s cache); coming back to the tab (visibilitychange) asks again, so a fix made on the box shows up.
   The data is kept here (doctorStore) and shared with Settings > Agents, which shows the Claude and Codex checks inline. A define-only script: pages/settings.js calls these.
   Groups: terminal folds into Box and memory (claude-mem) into Claude, so a phone sees four short sections and not seven. */
'use strict';

const DOCTOR_SECTIONS = [
  { id: 'box', label: 'Box', groups: ['box', 'terminal'] },
  { id: 'claude', label: 'Claude', groups: ['claude', 'memory'] },
  { id: 'codex', label: 'Codex', groups: ['codex'] },
  { id: 'notify', label: 'Notifications', groups: ['notify'] },
];
const DOCTOR_GLYPH = { pass: '✓', warn: '!', fail: '✕', skip: '–' };
const DOCTOR_WORD = { pass: 'passing', warn: 'warning', fail: 'failing', skip: 'skipped' };
const DOCTOR_ORDER = { fail: 0, warn: 1, pass: 2, skip: 3 };
const DOCTOR_ACTIONS = { claude_login: 'Log in', codex_login: 'Log in', notify_test: 'Send test', mcp_settings: 'Open' };      // what fix.action may be, and the button's words (mcp_settings: Settings > Agents, issue #14)
const DOCTOR_RECENT_MS = 5000;                                                                      // a refresh asked for within this of the last answer is not repeated
const doctorStore = { data: null, at: 0, busy: false, err: '', seq: 0, acting: null };

function doctorStatus(c) { return c && ownKey(DOCTOR_GLYPH, c.status) ? c.status : 'warn'; }

/* fix.action as the page knows it: the server writes claude_login / codex_login / notify_test, a hyphenated spelling (claude-login) is accepted too; '' for anything else. */
function doctorActionOf(f) {
  const a = f && typeof f.action === 'string' ? f.action.trim().toLowerCase().replace(/-/g, '_') : '';
  return ownKey(DOCTOR_ACTIONS, a) ? a : '';
}

function doctorChecks() {
  const d = doctorStore.data;
  return d && Array.isArray(d.checks) ? d.checks.filter((c) => c && typeof c === 'object' && c.id) : [];
}

/* The checks of one agent for Settings > Agents: Claude's own group, or Codex's, failing first like the Doctor tab. */
function doctorAgentChecks(agent) { return doctorByStatus(doctorChecks().filter((c) => c.group === agent)); }

/* Failing first, then warnings, passes and skips; the box's own order within a status. */
function doctorByStatus(rows) {
  return rows.map((c, i) => [c, i]).sort((a, b) => DOCTOR_ORDER[doctorStatus(a[0])] - DOCTOR_ORDER[doctorStatus(b[0])] || a[1] - b[1]).map((x) => x[0]);
}

function doctorFailing() {
  const s = doctorStore.data && doctorStore.data.summary;
  return s && Number.isFinite(Number(s.fail)) ? Number(s.fail) : 0;
}

/* The sections to draw: the four above in order, then any group a later phase registers (own section, named after the group). Each section's checks go failing first,
   then warnings, passes and skips, the box's own order within a status. */
function doctorSections(checks) {
  const known = new Set(DOCTOR_SECTIONS.flatMap((s) => s.groups));
  const extra = [...new Set(checks.map((c) => c.group).filter((g) => typeof g === 'string' && g && !known.has(g)))];
  const all = [...DOCTOR_SECTIONS, ...extra.map((g) => ({ id: g, label: g.charAt(0).toUpperCase() + g.slice(1), groups: [g] }))];
  const out = [];
  for (const s of all) {
    const rows = doctorByStatus(checks.filter((c) => s.groups.includes(c.group)));
    if (rows.length) out.push({ id: s.id, label: s.label, checks: rows });
  }
  return out;
}

function doctorCounts(checks) {
  const n = { pass: 0, warn: 0, fail: 0, skip: 0 };
  for (const c of checks) n[doctorStatus(c)] += 1;
  return n;
}

/* '3 failing · 1 warning' for a section head; 'all passing' when nothing needs a look. */
function doctorCountText(n) {
  const bits = [];
  if (n.fail) bits.push(`${n.fail} failing`);
  if (n.warn) bits.push(`${n.warn} warning${n.warn === 1 ? '' : 's'}`);
  return bits.length ? bits.join(' · ') : (n.pass ? 'all passing' : 'nothing to check');
}

function doctorSummaryText(n) { return `Doctor: ${n} check${n === 1 ? '' : 's'} failing`; }

/* One check as a row. o.noLogin: the inline rows of Settings > Agents leave the two login buttons out (the card above already has its Log in). */
function doctorRow(c, o) {
  const st = doctorStatus(c);
  const f = c.fix && typeof c.fix === 'object' ? c.fix : null;
  const act = doctorActionOf(f);
  const hide = !!(o && o.noLogin) && (act === 'claude_login' || act === 'codex_login');
  const label = typeof c.label === 'string' && c.label ? c.label : String(c.id);
  const main = el('div', { class: 'doc-main' }, el('b', { class: 'doc-label', text: label }),
    c.detail ? el('span', { class: 'doc-detail', text: String(c.detail) }) : null);
  if (f && st !== 'pass') {
    if (f.text) main.append(el('span', { class: 'doc-fix dim', text: String(f.text) }));
    if (f.cmd) main.append(el('span', { class: 'doc-cmdline' }, el('code', { class: 'doc-cmd', text: String(f.cmd) }), copyButton(String(f.cmd), 'the command')));
  }
  const busy = doctorStore.acting === c.id;
  const btn = act && !hide && st !== 'pass' ? el('button', { class: 'doc-act', type: 'button', disabled: !!doctorStore.acting, 'aria-label': `${DOCTOR_ACTIONS[act]}: ${label}`,
    onclick: () => doctorRun(c, act), text: busy ? 'Working…' : DOCTOR_ACTIONS[act] }) : null;
  return el('div', { class: `doc-row doc-${st}`, 'data-check': c.id, 'data-status': st },
    el('span', { class: `glyph doc-glyph doc-g-${st}`, role: 'img', 'aria-label': DOCTOR_WORD[st], title: DOCTOR_WORD[st], text: DOCTOR_GLYPH[st] }), main,
    el('div', { class: 'doc-side' }, btn));
}

/* What a fix button does: the two logins open Settings > Accounts (the add blocks live there), Send test pushes through the check's own channel and then asks again. */
async function doctorRun(c, action) {
  if (doctorStore.acting) return false;
  if (action === 'claude_login') { if (typeof accountLogin === 'function') accountLogin(); return true; }
  if (action === 'codex_login') { if (typeof settingsCodexLogin === 'function') settingsCodexLogin(); return true; }
  if (action === 'mcp_settings') {                                     // the device tokens live in Settings > Agents > Connect from another device
    if (typeof navigate === 'function' && typeof buildHash === 'function') navigate(buildHash('settings', {}, { sec: 'agents' }));
    if (typeof settingsPage !== 'undefined' && settingsPage.refs && typeof settingsShow === 'function') settingsShow('agents');
    return true;
  }
  if (action !== 'notify_test') return false;
  doctorStore.acting = c.id;
  doctorPaint();
  try {
    if (c.id === 'push') {
      const r = await api('POST', '/api/push/test');
      if (r && r.sent) pageToast('Test push sent', 'ok'); else pageToast(`No push went out (${r && Number.isFinite(r.subscriptions) ? r.subscriptions : 0} subscriptions)`, 'warn');
    } else {
      const r = await api('POST', '/api/notify/test');
      if (r && r.ok) pageToast('Test sent to ntfy. If the phone stays silent, check the ntfy app and the Tailscale VPN.', 'ok'); else pageToast('ntfy did not take the test (is it running?)', 'warn');
    }
  } catch (e) {
    pageToast(`Could not send the test: ${(e && e.message) || 'the board did not answer'}`, 'bad');
  }
  doctorStore.acting = null;
  doctorPaint();
  return doctorLoad(true);
}

/* GET /api/doctor (refresh: ?refresh=1). A newer ask makes an older answer stale; a failed ask keeps the last good answer on screen and says so. */
async function doctorLoad(refresh) {
  const mine = ++doctorStore.seq;
  doctorStore.busy = true;
  doctorStore.err = '';
  doctorPaint();
  let d = null;
  try { d = await api('GET', '/api/doctor' + (refresh ? '?refresh=1' : '')); } catch (e) {
    if (mine !== doctorStore.seq) return false;
    doctorStore.busy = false;
    doctorStore.err = (e && e.message) || 'the board did not answer';
    doctorPaint();
    return false;
  }
  if (mine !== doctorStore.seq) return false;
  doctorStore.busy = false;
  if (d && Array.isArray(d.checks)) { doctorStore.data = d; doctorStore.at = Date.now(); } else doctorStore.err = 'the board answered without a checklist';
  doctorPaint();
  return true;
}

/* Back on the tab: ask again (forced on the two panels that show the checks, so a fix made on the box shows), at most once per DOCTOR_RECENT_MS. */
function doctorOnVisible() {
  if (typeof document !== 'undefined' && document.hidden) return;
  if (typeof settingsPage === 'undefined' || !settingsPage.refs) return;
  if (doctorStore.busy || Date.now() - doctorStore.at < DOCTOR_RECENT_MS) return;
  doctorLoad(settingsPage.active === 'doctor' || settingsPage.active === 'agents');
}

/* Repaint what shows the checklist: the panel that is open (rebuilt only when its signature moved) and the line under the tabs. */
function doctorPaint() {
  const r = typeof settingsPage !== 'undefined' ? settingsPage.refs : null;
  if (!r) return;
  if ((settingsPage.active === 'doctor' || settingsPage.active === 'agents') && typeof settingsFill === 'function') settingsFill(settingsPage.active, false);
  doctorLinePaint();
}

/* The quiet line under the Settings tabs: 'Doctor: 2 checks failing', a link to the Doctor tab; not shown when nothing fails or while that tab is the one open. */
function doctorLinePaint() {
  const r = typeof settingsPage !== 'undefined' ? settingsPage.refs : null;
  if (!r || !r.docLine) return;
  const n = doctorFailing();
  setText(r.docLink, doctorSummaryText(n));
  r.docLine.classList.toggle('hidden', !n || settingsPage.active === 'doctor');
}

/* The signature of what the Doctor and Agents panels show of the checklist, for settingsSig. */
function doctorSig() {
  return [doctorStore.at, doctorStore.busy, doctorStore.err, doctorStore.acting, Math.floor(Date.now() / 60000)];
}

function settingsDoctor(p) {
  p.textContent = '';
  const d = doctorStore.data;
  const checks = doctorChecks();
  const stamp = d ? `Checked ${fmtAge(doctorStore.at / 1000)} ago` : (doctorStore.busy ? 'Checking this box…' : 'Not checked yet');
  const bar = el('div', { class: 'doc-bar' }, el('span', { class: 'doc-stamp dim', role: 'status', text: stamp }),
    el('button', { class: 'doc-recheck', type: 'button', disabled: doctorStore.busy, onclick: () => doctorLoad(true), text: doctorStore.busy ? 'Checking…' : 'Re-check' }));
  p.append(bar);
  if (doctorStore.err) p.append(el('div', { class: 'set-err bad', role: 'alert', text: `Could not run the checks: ${doctorStore.err}` }));
  if (!checks.length) {
    if (!doctorStore.err && !doctorStore.busy) p.append(el('div', { class: 'dim set-note', text: 'Nothing to show yet. Press Re-check.' }));
    return;
  }
  p.append(el('p', { class: 'dim set-note doc-lede', text: 'What this box needs to run the board: nothing here opens by itself. A warning is worth a look; a failure stops something from working.' }));
  for (const s of doctorSections(checks)) {
    const n = doctorCounts(s.checks);
    p.append(el('section', { class: 'doc-sec', 'data-doc': s.id, 'aria-label': s.label },
      el('div', { class: 'doc-sec-head' }, settingsHead(s.label), el('span', { class: n.fail ? 'doc-count bad' : n.warn ? 'doc-count warn' : 'doc-count dim', text: doctorCountText(n) })),
      el('div', { class: 'doc-list' }, s.checks.map((c) => doctorRow(c)))));
  }
}
