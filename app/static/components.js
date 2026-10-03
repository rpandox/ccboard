/* ccboard components: small renderers shared by the pages (badges, rows, cards, form fields, two-tap buttons).
   Definitions only. Blueprint classes come from el() in core.js; no Blueprint class literals here. */
'use strict';

function pctBar(pct) {
  const cls = pct >= 85 ? ' bad' : pct >= 60 ? ' warn' : '';
  const i = el('i'); i.style.width = `${Math.max(0, Math.min(100, pct))}%`;
  return el('span', { class: 'bar' + cls }, i);
}

function confirmButton(key, label, action, quiet) {
  // Two taps: the first turns the button into "Confirm …" + Cancel. `quiet` renders the first state low-key
  // (minimal, no red fill) for destructive actions that sit next to everyday ones on a phone.
  // Repaint whichever page is mounted (a keyed roster row, the peek, the board): never a home-only renderer, so it is safe off the board.
  const repaint = () => { if (typeof repaintPage === 'function') repaintPage(); else if (typeof render === 'function') render(true); };
  if (ui.confirm === key) {
    return el('span', { class: 'row' },
      el('button', { class: 'danger confirm', onclick: async () => { ui.confirm = null; try { await action(); setError(null); } catch (e) { setError(e.message); } await poll(true); }, text: `Confirm ${label}` }),
      el('button', { onclick: () => { ui.confirm = null; repaint(); }, text: 'Cancel' }));
  }
  return el('button', { class: 'danger' + (quiet ? ' minimal' : ''), onclick: () => { ui.confirm = key; repaint(); }, text: label });
}

function repoGroups(p) { return p.root ? [p.root, ...p.repos] : p.repos; }   // the project folder row first, then the repos

function allRepos() {
  const out = [];
  for (const p of state.projects) for (const r of p.repos) if (r.state === 'ok' || r.state === 'unknown') out.push({ id: `${p.name}/${r.name}`, project: p.name, repo: r.name });
  return out;
}

function field(label, control, hint) { return el('div', { class: 'field' }, el('span', { text: label }), control, hint ? el('span', { class: 'dim', text: hint }) : null); }
function selectEl(options, value) {
  const sel = el('select');
  for (const [v, t] of options) sel.append(el('option', { value: v, text: t }));
  if (value !== undefined && value !== null) sel.value = value;
  return sel;
}

const STATE_LABEL = { idle: 'idle', working: 'working', waiting: 'needs you', done: 'done', errored: 'error', ended: 'ended', unknown: '' };

function stateBadge(s) {
  const st = s.state || 'unknown';
  if (!STATE_LABEL[st]) return null;
  const age = s.state_at ? fmtAge(Date.parse(s.state_at) / 1000) : '';
  // beside its text label the glyph is decoration: hide it from screen readers (no double announcement) and let the
  // badge's own title (the last hook event) show on hover
  const glyph = stateGlyph(st);
  glyph.setAttribute('aria-hidden', 'true');
  glyph.removeAttribute('title');
  return el('span', { class: `state ${st}` + (s.needs_attention ? ' attn' : ''), title: s.last_event || '' },
    glyph, ' ', STATE_LABEL[st] + (age ? ` ${age}` : ''));
}

function statsText(s) {
  const t = s.stats;
  if (!t) return '';
  const parts = [];
  if (t.model) parts.push(t.model);
  if (typeof t.context_pct === 'number') parts.push(`ctx ${Math.round(t.context_pct)}%`);
  if (typeof t.cost_usd === 'number') parts.push(`$${t.cost_usd.toFixed(2)}`);
  return parts.join(' · ');
}

// Sessions carry no agent field until the adapter phase: shell and clone launchers run a plain shell, the rest run Claude.
function sessionAgent(s) { return s.agent || (s.launcher === 'shell' || s.launcher === 'clone' ? 'shell' : 'claude'); }

function sessionMeta(s) {
  const stats = statsText(s);
  const parts = [s.launcher || null, stats ? el('span', { class: 'mono', text: stats }) : null, `${fmtAge(s.created)} · ${s.attached} attached`].filter(Boolean);
  const kids = [];
  parts.forEach((p, i) => { if (i) kids.push(' · '); kids.push(p); });
  return el('span', { class: 'meta' }, ...kids);
}

function sessionRow(s) {
  const row = el('div', { class: 'sess' + (s.needs_attention ? ' attn' : ''), 'data-tmux': s.tmux },
    el('div', { class: 'main' },
      agentGlyph(sessionAgent(s)),
      el('span', { class: 'name', text: s.name }),
      stateBadge(s),
      sessionMeta(s),
      el('code', { class: 'mono', text: s.command || '' })),
    el('div', { class: 'actions' },
      el('a', { class: 'btn primary', href: `/term/${encodeURIComponent(s.tmux)}`, target: '_blank', rel: 'noopener', text: 'Open terminal' }),
      s.needs_attention ? el('button', { onclick: async () => { try { await api('POST', `/api/sessions/${encodeURIComponent(s.tmux)}/ack`); } catch (e) { setError(e.message); } await poll(true); }, text: 'Ack' }) : null,
      confirmButton('kill:' + s.tmux, 'Kill session', () => api('DELETE', `/api/sessions/${encodeURIComponent(s.tmux)}`), true)));
  if (s.last_message || s.last_prompt) {
    row.append(el('div', { class: 'last' },
      s.last_prompt ? el('span', { class: 'dim', text: '› ' + s.last_prompt.slice(0, 120) }) : null,
      s.last_message ? el('span', { text: s.last_message.slice(0, 160) }) : null));
  }
  return row;
}

function costText(p) {
  const c = state.cost && state.cost.value && state.cost.value.projects && state.cost.value.projects[p.name];
  if (!c) return '';
  return `$${c.today.toFixed(2)} today · $${c.week.toFixed(2)} 7d · $${c.total.toFixed(2)} total`;
}

function repoCost(p, r) {
  const c = state.cost && state.cost.value && state.cost.value.projects && state.cost.value.projects[p.name];
  const v = c && c.repos && c.repos[r.name];
  return typeof v === 'number' ? `$${v.toFixed(2)}` : '';
}

const COLUMNS = [['in_progress', 'In progress'], ['needs_you', 'Needs you'], ['done', 'Done'], ['pr', 'PR open'], ['merged', 'Merged']];

function ciBadge(t) {
  if (!t.pr_url) return null;
  const b = (t.ci && t.ci.bucket) || 'none';
  const cls = b === 'pass' ? 'ok' : b === 'fail' ? 'bad' : b === 'pending' ? 'warn' : '';
  const review = t.pr && t.pr.review ? ` · ${t.pr.review.toLowerCase().replace('_', ' ')}` : '';
  const txt = `${(t.pr_state || 'PR').toLowerCase()} · CI ${b}${review}`;
  return el('span', { class: 'badge ' + cls, title: (t.ci && t.ci.checks || []).map(c => `${c.name}: ${c.bucket}`).join('\n'), text: txt });
}

function taskCard(t) {
  const s = t.session;
  const ciFail = t.ci && t.ci.bucket === 'fail';
  const card = el('div', { class: 'task' + (s && s.needs_attention ? ' attn' : ''), 'data-task': t.id },
    el('div', { class: 'row' }, el('span', { class: 'title', text: t.title }), s ? stateBadge(s) : el('span', { class: 'state ended', text: 'no session' }), ciBadge(t)),
    el('div', { class: 'meta' }, `${t.project}/${t.repo} · ${t.branch}${t.pr_url ? ' · PR #' + t.pr_number : ''}`,
      typeof t.cost_usd === 'number' ? [' · ', el('span', { class: 'mono', text: '$' + t.cost_usd.toFixed(2) })] : null),
    s && s.last_message ? el('div', { class: 'last', text: s.last_message.slice(0, 160) }) : null,
    (t.overlap && t.overlap.length) ? el('div', { class: 'last bad', title: t.overlap.map(o => `${o.title}: ${o.files.join(', ')}`).join('\n'),
      text: '⚠ overlaps ' + t.overlap.map(o => `"${o.title}" (${o.files.length} file${o.files.length === 1 ? '' : 's'}: ${o.files.slice(0, 3).join(', ')}${o.files.length > 3 ? '…' : ''})`).join('; ') }) : null,
    el('div', { class: 'actions' },
      el('a', { class: 'btn primary', href: `/term/${encodeURIComponent(t.tmux)}`, target: '_blank', rel: 'noopener' }, ic('console'), 'Terminal'),
      el('button', { onclick: () => openTaskModal(t), text: t.pr_url ? 'Diff / PR' : 'Diff / PR…' }),
      t.pr_url ? el('a', { class: 'btn', href: t.pr_url, target: '_blank', rel: 'noopener', text: 'PR' }) : null,
      t.preview_url ? el('a', { class: 'btn', href: t.preview_url, target: '_blank', rel: 'noopener', text: `Preview :${t.preview_port}` }) : null,
      t.preview_url ? el('button', { onclick: async () => { try { await api('DELETE', `/api/tasks/${t.id}/preview`); } catch (e) { setError(e.message); } await poll(true); }, title: 'stop exposing the preview', text: '⏏' }) :
        el('button', { onclick: async () => {
          try { const r = await api('POST', `/api/tasks/${t.id}/preview`, {}); toast(`preview at ${r.url} → 127.0.0.1:${r.port}`, { kind: 'ok', ttl: 8000 }); }
          catch (e) { if (/no listening port/.test(e.message)) { const p = window.prompt(e.message + '\n\nDev server port (leave blank to cancel):'); if (p) { try { await api('POST', `/api/tasks/${t.id}/preview`, { port: parseInt(p, 10) }); } catch (e2) { setError(e2.message); } } } else setError(e.message); }
          await poll(true);
        }, title: 'expose a dev server running in this session on its own tailnet HTTPS port', text: 'Preview' }),
      ciFail ? el('button', { class: 'danger', onclick: async () => { try { const r = await api('POST', `/api/tasks/${t.id}/fix-ci`); setError(null); toast(`CI logs (${r.chars} chars) sent to ${t.title}${r.relaunched ? ' (session relaunched)' : ''}`, { kind: 'ok', ttl: 8000 }); } catch (e) { setError(e.message); } await poll(true); }, text: 'Fix CI' }) : null,
      t.pr_url ? el('button', { onclick: async () => { try { await api('POST', `/api/tasks/${t.id}/refresh`); } catch (e) { setError(e.message); } await poll(true); }, title: 'refresh PR / CI status', text: '↻' }) : null,
      confirmButton('arch:' + t.id, 'Archive', async () => {
        try { await api('POST', `/api/tasks/${t.id}/archive`, { force: false }); }
        catch (e) {
          if (/force/.test(e.message) && window.confirm(e.message + '\n\nDiscard the worktree anyway?')) await api('POST', `/api/tasks/${t.id}/archive`, { force: true });
          else throw e;
        }
      })));
  return card;
}

/* ---------- shell components (v0.5.3): tabs, sheet, toast, menu, empty state. Definitions only: the DOM is touched when they are called. ---------- */

/* tabs(items:[{id,label,count?}], activeId, onChange) -> { root, set(id), setCount(id, n), value }. Only the tab list: the page renders the panel.
   set() repaints without calling onChange (so a route change cannot loop); a click or an arrow key calls onChange(id). */
function tabs(items, activeId, onChange) {
  const list = el('div', { class: 'tablist', role: 'tablist' });
  const root = el('div', { class: 'tabs' }, list);
  const nodes = new Map();
  const counts = new Map();
  const ids = items.map((it) => it.id);
  let current = ids.includes(activeId) ? activeId : ids[0];
  const paint = (id) => {
    current = id;
    for (const [tid, n] of nodes) {
      n.setAttribute('aria-selected', tid === id ? 'true' : 'false');
      n.setAttribute('tabindex', tid === id ? '0' : '-1');
    }
  };
  const pick = (id, focus) => {
    if (id !== current) { paint(id); if (typeof onChange === 'function') onChange(id); }
    if (focus) nodes.get(id).focus();
  };
  for (const it of items) {
    const count = it.count === undefined || it.count === null ? null : el('span', { class: 'tab-count', text: String(it.count) });
    if (count) counts.set(it.id, count);
    const n = el('div', { class: 'tab', role: 'tab', 'data-tab': it.id, onclick: () => pick(it.id, false), onkeydown: (e) => {
      const i = ids.indexOf(it.id);
      let to = null;
      if (e.key === 'ArrowRight') to = ids[(i + 1) % ids.length];
      else if (e.key === 'ArrowLeft') to = ids[(i + ids.length - 1) % ids.length];
      else if (e.key === 'Home') to = ids[0];
      else if (e.key === 'End') to = ids[ids.length - 1];
      else if (e.key === 'Enter' || e.key === ' ') to = it.id;
      if (to === null) return;
      e.preventDefault();
      pick(to, true);
    } }, it.label, count);
    nodes.set(it.id, n);
    list.append(n);
  }
  paint(current);
  return {
    root,
    set(id) { if (nodes.has(id)) paint(id); },
    setCount(id, n) { const c = counts.get(id); if (c) setText(c, n); },
    get value() { return current; },
  };
}

/* openSheet({title, body, actions?, placement?, onClose?}) -> { dialog, body, close }. dialog#sheet through showModal(): a right panel from 840 px up,
   a bottom sheet below (placement 'right' | 'bottom' forces one). Calling it again while open swaps the content in place. body and actions
   take a node, an array of nodes or a string. Esc, the backdrop and the close button end it; closeSheet() does the same from code. */
function sheetKids(x) { return x === null || x === undefined || x === false ? [] : (Array.isArray(x) ? x : [x]).flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false); }

function openSheet(opts) {
  const dlg = document.getElementById('sheet');
  if (!dlg) return null;
  const o = opts || {};
  if (!dlg.dataset.wired) {
    dlg.dataset.wired = '1';
    dlg.addEventListener('close', () => {
      const cb = dlg._onClose;
      dlg._onClose = null;
      dlg.textContent = '';
      if (typeof cb === 'function') { try { cb(); } catch (e) { console.error('ccboard sheet onClose', e); } }
    });
    dlg.addEventListener('click', (e) => { if (e.target === dlg) closeSheet(); });   // the dialog has no padding: a click on itself is a backdrop click
  }
  const wide = !!(window.matchMedia && window.matchMedia('(min-width: 840px)').matches);
  const place = o.placement === 'bottom' || o.placement === 'right' ? o.placement : (wide ? 'right' : 'bottom');
  dlg.classList.toggle('bottom', place === 'bottom');
  dlg.classList.toggle('right', place === 'right');
  const body = el('div', { class: 'sheet-body' }, ...sheetKids(typeof o.body === 'string' ? document.createTextNode(o.body) : o.body));
  const actions = sheetKids(o.actions);
  dlg.textContent = '';
  dlg.append(
    el('div', { class: 'sheet-head' }, el('h2', { class: 'sheet-title', text: o.title || '' }),
      el('button', { class: 'icon minimal', type: 'button', 'aria-label': 'Close', title: 'Close', onclick: () => closeSheet() }, ic('cross'))),
    body);
  if (actions.length) dlg.append(el('div', { class: 'sheet-actions' }, ...actions));   // Element.append(null) would add the text "null"
  dlg._onClose = typeof o.onClose === 'function' ? o.onClose : null;
  if (!dlg.open) { try { dlg.showModal(); } catch (_) { dlg.setAttribute('open', ''); } }
  return { dialog: dlg, body, close: closeSheet };
}

function closeSheet() {
  const dlg = document.getElementById('sheet');
  if (!dlg) return;
  if (dlg.open) { try { dlg.close(); } catch (_) { dlg.removeAttribute('open'); } }
  else if (dlg._onClose) { const cb = dlg._onClose; dlg._onClose = null; dlg.textContent = ''; try { cb(); } catch (e) { console.error('ccboard sheet onClose', e); } }
}

/* toast(text, {kind, ttl}): appended to #toasts, removed after ttl ms (default 4 s, 7 s for 'bad'), click to dismiss. kind: info | ok | warn | bad.
   #toasts is a manual popover where the browser has them, so it is re-promoted above any modal dialog that is open (a sheet, the drawer). */
function toastHost(on) {
  const host = document.getElementById('toasts');
  if (!host || typeof host.showPopover !== 'function') return host;
  try {
    if (!host.hasAttribute('popover')) host.setAttribute('popover', 'manual');
    if (host.matches(':popover-open')) host.hidePopover();
    if (on) host.showPopover();
  } catch (_) { /* no popover support: the fixed container still shows */ }
  return host;
}

function toast(text, opts) {
  const host = toastHost(true);
  if (!host) return null;
  const o = opts || {};
  const kind = ['info', 'ok', 'warn', 'bad'].includes(o.kind) ? o.kind : 'info';
  const ttl = typeof o.ttl === 'number' ? o.ttl : (kind === 'bad' ? 7000 : 4000);
  const node = el('div', { class: 'toast ' + kind, role: kind === 'bad' ? 'alert' : null }, el('span', { class: 'toast-text', text: String(text) }));
  const gone = () => { node.remove(); if (!host.childElementCount) toastHost(false); };
  node.addEventListener('click', gone);
  host.append(node);
  while (host.childElementCount > 4) host.firstElementChild.remove();
  if (ttl > 0) setTimeout(gone, ttl);
  return node;
}

/* menu(button, items:[{label, icon?, onClick}] | () => items) -> { open(), close(), toggle(), root }. A small popover under the button, in the
   button's dialog when it sits in one, else inside #topbar. Click outside, Esc, Tab or a route change close it; arrows move, Enter picks. */
function menu(button, items) {
  let pop = null;
  const ctl = { get root() { return pop; } };
  const onDoc = (e) => { if (pop && !pop.contains(e.target) && !button.contains(e.target)) ctl.close(false); };
  const onKey = (e) => {
    if (!pop) return;
    const rows = [...pop.querySelectorAll('.menuitem')];
    const i = rows.indexOf(document.activeElement);
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); ctl.close(true); }
    else if (e.key === 'Tab') ctl.close(false);
    else if (e.key === 'ArrowDown') { e.preventDefault(); rows[(i + 1) % rows.length].focus(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); rows[(i + rows.length - 1) % rows.length].focus(); }
    else if (e.key === 'Home') { e.preventDefault(); rows[0].focus(); }
    else if (e.key === 'End') { e.preventDefault(); rows[rows.length - 1].focus(); }
  };
  const onHash = () => ctl.close(false);
  ctl.close = (refocus) => {
    if (!pop) return;
    pop.remove();
    pop = null;
    button.setAttribute('aria-expanded', 'false');
    document.removeEventListener('pointerdown', onDoc, true);
    document.removeEventListener('keydown', onKey, true);
    window.removeEventListener('hashchange', onHash);
    if (refocus) button.focus();
  };
  ctl.open = () => {
    if (pop) return;
    const list = typeof items === 'function' ? items() : items;
    pop = el('div', { class: 'menu menu-pop', role: 'menu' });
    for (const it of list) {
      const pick = () => { ctl.close(false); if (typeof it.onClick === 'function') it.onClick(); };
      pop.append(el('div', { class: 'menuitem', role: 'menuitem', tabindex: '-1', onclick: pick,
        onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); } } },
      it.icon ? ic(it.icon) : null, el('span', { class: 'mi-text', text: it.label })));
    }
    (button.closest('dialog') || document.getElementById('topbar') || document.body).append(pop);
    const r = button.getBoundingClientRect();
    pop.style.top = `${Math.round(r.bottom + 4)}px`;
    if (r.left + r.width / 2 > window.innerWidth / 2) pop.style.right = `${Math.max(8, Math.round(window.innerWidth - r.right))}px`;
    else pop.style.left = `${Math.max(8, Math.round(r.left))}px`;
    button.setAttribute('aria-expanded', 'true');
    document.addEventListener('pointerdown', onDoc, true);
    document.addEventListener('keydown', onKey, true);
    window.addEventListener('hashchange', onHash);
    const first = pop.querySelector('.menuitem');
    if (first) first.focus();
  };
  ctl.toggle = () => (pop ? ctl.close(true) : ctl.open());
  button.setAttribute('aria-haspopup', 'menu');
  button.setAttribute('aria-expanded', 'false');
  button.addEventListener('click', () => ctl.toggle());
  return ctl;
}

/* emptyState(icon, title, hint): the centred "nothing here" block (icon = a Blueprint icon name). */
function emptyState(icon, title, hint) {
  return el('div', { class: 'nonideal empty' },
    el('div', { class: 'empty-visual' }, ic(icon || 'info-sign')),
    el('h4', { class: 'empty-title', text: title || '' }),
    hint ? el('div', { class: 'empty-hint dim', text: hint }) : null);
}
