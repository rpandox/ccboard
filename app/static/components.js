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
  if (ui.confirm === key) {
    return el('span', { class: 'row' },
      el('button', { class: 'danger confirm', onclick: async () => { ui.confirm = null; try { await action(); setError(null); } catch (e) { setError(e.message); } await poll(true); }, text: `Confirm ${label}` }),
      el('button', { onclick: () => { ui.confirm = null; renderProjects(); }, text: 'Cancel' }));
  }
  return el('button', { class: 'danger' + (quiet ? ' minimal' : ''), onclick: () => { ui.confirm = key; renderProjects(); }, text: label });
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
          try { const r = await api('POST', `/api/tasks/${t.id}/preview`, {}); ui.notice = `preview at ${r.url} → 127.0.0.1:${r.port}`; }
          catch (e) { if (/no listening port/.test(e.message)) { const p = window.prompt(e.message + '\n\nDev server port (leave blank to cancel):'); if (p) { try { await api('POST', `/api/tasks/${t.id}/preview`, { port: parseInt(p, 10) }); } catch (e2) { setError(e2.message); } } } else setError(e.message); }
          await poll(true);
        }, title: 'expose a dev server running in this session on its own tailnet HTTPS port', text: 'Preview' }),
      ciFail ? el('button', { class: 'danger', onclick: async () => { try { const r = await api('POST', `/api/tasks/${t.id}/fix-ci`); setError(null); ui.notice = `CI logs (${r.chars} chars) sent to ${t.title}${r.relaunched ? ' (session relaunched)' : ''}`; } catch (e) { setError(e.message); } await poll(true); }, text: 'Fix CI' }) : null,
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
