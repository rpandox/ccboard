/* ccboard frontend: one render(state) fed by polling. Dynamic text goes through textContent only. */
'use strict';

const $ = (sel) => document.querySelector(sel);

function el(tag, attrs, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') n.className = v;
    else if (k === 'text') n.textContent = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    n.append(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return n;
}

async function api(method, path, body) {
  const headers = { 'X-CCBoard': '1' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const r = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  let data = null;
  try { data = await r.json(); } catch (_) { /* not json */ }
  if (!r.ok) throw new Error((data && data.error) || `${r.status} ${r.statusText}`);
  return data;
}

const ui = { openForm: null, confirm: null, error: null, modal: false, lastJson: null };
let state = null;
let pollTimer = null;

function setError(msg) { ui.error = msg; renderBanner(); }

function codeServerUrl(path) {
  return `https://${location.hostname}:${state.config.code_https_port}/?folder=${encodeURIComponent(path)}`;
}

function fmtAge(epoch) {
  if (!epoch) return '';
  const s = Math.max(0, Math.floor(Date.now() / 1000 - epoch));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

/* ---------- header / banner ---------- */

function renderHeader() {
  const badge = $('#claude-badge');
  badge.textContent = '';
  badge.className = 'badge';
  const c = state.claude || {};
  if (!c.installed) { badge.classList.add('bad'); badge.textContent = 'claude not installed'; }
  else if (c.loggedIn) { badge.classList.add('ok'); badge.textContent = `Claude: ${c.email || 'logged in'}${c.subscriptionType ? ' (' + c.subscriptionType + ')' : ''}`; }
  else { badge.classList.add('warn'); badge.textContent = 'Claude: not logged in'; }
  const actions = $('#hdr-actions');
  actions.textContent = '';
  actions.append(el('span', { class: 'dim', text: state.user || '' }));
  if (c.installed && !c.loggedIn) actions.append(el('button', { class: 'primary', onclick: startLogin, text: 'Log in' }));
  if (c.installed && c.loggedIn) actions.append(el('button', { onclick: logout, text: 'Log out' }));
  if (state.login && state.login.running && !ui.modal) actions.append(el('button', { onclick: () => openModal(), text: 'Login in progress…' }));
}

function renderBanner() {
  const b = $('#banner');
  b.textContent = '';
  b.className = '';
  if (ui.error) {
    b.append(el('span', { text: ui.error }), el('button', { onclick: () => setError(null), text: 'dismiss' }));
  } else if (state && state.tmux_down) {
    b.append(el('span', { text: 'The ccboard tmux server is not running. On the box: sudo systemctl start ccboard-tmux' }));
  } else if (state && state.claude && state.claude.installed && !state.claude.loggedIn) {
    b.className = 'warn';
    b.append(el('span', { text: 'Claude Code is not logged in on this box.' }), el('button', { class: 'primary', onclick: startLogin, text: 'Log in' }));
  }
}

/* ---------- new project ---------- */

function renderNewProject() {
  const sec = $('#new-project');
  if (sec.childElementCount) return; // static form, built once
  const name = el('input', { type: 'text', placeholder: 'project name (e.g. shop)', required: true, maxlength: 64 });
  const url = el('input', { type: 'text', placeholder: 'optional: clone URL of the first repo' });
  const form = el('form', { class: 'inline', onsubmit: async (e) => {
    e.preventDefault();
    const body = { name: name.value.trim() };
    if (url.value.trim()) body.url = url.value.trim();
    try { await api('POST', '/api/projects', body); name.value = ''; url.value = ''; setError(null); await poll(true); }
    catch (err) { setError(err.message); }
  } }, name, url, el('button', { class: 'primary', type: 'submit', text: 'New project' }));
  sec.append(el('h2', { text: 'Projects' }), el('div', { class: 'dim', text: `Folder per project under ${state.config.projects_dir}; each repo is a subfolder; sessions run inside a repo.` }), form);
}

/* ---------- projects ---------- */

function confirmButton(key, label, action) {
  if (ui.confirm === key) {
    return el('span', { class: 'row' },
      el('button', { class: 'danger confirm', onclick: async () => { ui.confirm = null; try { await action(); setError(null); } catch (e) { setError(e.message); } await poll(true); }, text: `Confirm ${label}` }),
      el('button', { onclick: () => { ui.confirm = null; renderProjects(); }, text: 'Cancel' }));
  }
  return el('button', { class: 'danger', onclick: () => { ui.confirm = key; renderProjects(); }, text: label });
}

function allRepos() {
  const out = [];
  for (const p of state.projects) for (const r of p.repos) if (r.state === 'ok' || r.state === 'unknown') out.push({ id: `${p.name}/${r.name}`, project: p.name, repo: r.name });
  return out;
}

function sessionForm(p, r) {
  const launcher = el('select', {},
    el('option', { value: 'claude', text: 'claude (new session)' }),
    el('option', { value: 'resume', text: 'claude --resume' }),
    el('option', { value: 'continue', text: 'claude --continue' }),
    el('option', { value: 'shell', text: 'shell' }));
  const name = el('input', { type: 'text', placeholder: 'session name (auto: s1, s2…)', maxlength: 64 });
  const args = el('input', { type: 'text', placeholder: 'extra args, e.g. --permission-mode acceptEdits --model opus' });
  const resumeId = el('input', { type: 'text', placeholder: 'session id to resume (blank = picker)', class: 'hidden' });
  launcher.addEventListener('change', () => resumeId.classList.toggle('hidden', launcher.value !== 'resume'));
  const siblings = allRepos().filter(x => x.project === p.name && x.repo !== r.name);
  const others = allRepos().filter(x => x.project !== p.name);
  const checks = el('div', { class: 'checks' });
  const boxes = [];
  const mk = (x, checked) => { const cb = el('input', { type: 'checkbox', value: x.id, checked }); boxes.push(cb); return el('label', { class: 'row' }, cb, x.id); };
  for (const x of siblings) checks.append(mk(x, true));
  const otherBox = el('div', { class: 'checks' });
  for (const x of others) otherBox.append(mk(x, false));
  const form = el('form', { class: 'form', onsubmit: async (e) => {
    e.preventDefault();
    const body = { launcher: launcher.value };
    if (name.value.trim()) body.name = name.value.trim();
    if (args.value.trim()) body.args = args.value.trim();
    if (launcher.value === 'resume' && resumeId.value.trim()) body.resume_id = resumeId.value.trim();
    body.add_dirs = boxes.filter(b => b.checked).map(b => b.value);
    const tab = window.open('', '_blank');
    try {
      const res = await api('POST', `/api/projects/${encodeURIComponent(p.name)}/repos/${encodeURIComponent(r.name)}/sessions`, body);
      if (tab) tab.location = res.attach_url;
      ui.openForm = null; setError(null); await poll(true);
    } catch (err) { if (tab) tab.close(); setError(err.message); }
  } },
    el('div', { class: 'row' }, launcher, name),
    el('label', { text: 'extra args' }), args,
    resumeId,
    siblings.length ? el('label', { text: 'also give access to (--add-dir)' }) : null, checks,
    others.length ? el('details', {}, el('summary', { class: 'dim', text: 'repos of other projects' }), otherBox) : null,
    el('div', { class: 'row' },
      el('button', { class: 'primary', type: 'submit', text: 'Start & attach' }),
      el('button', { type: 'button', onclick: () => { ui.openForm = null; renderProjects(); }, text: 'Cancel' })));
  return form;
}

function addRepoForm(p) {
  const name = el('input', { type: 'text', placeholder: 'repo name (blank git init)', maxlength: 64 });
  const url = el('input', { type: 'text', placeholder: 'or clone URL' });
  return el('form', { class: 'form', onsubmit: async (e) => {
    e.preventDefault();
    const body = {};
    if (name.value.trim()) body.name = name.value.trim();
    if (url.value.trim()) body.url = url.value.trim();
    try { await api('POST', `/api/projects/${encodeURIComponent(p.name)}/repos`, body); ui.openForm = null; setError(null); await poll(true); }
    catch (err) { setError(err.message); }
  } },
    el('div', { class: 'row' }, name, url),
    el('div', { class: 'row' },
      el('button', { class: 'primary', type: 'submit', text: 'Add repo' }),
      el('button', { type: 'button', onclick: () => { ui.openForm = null; renderProjects(); }, text: 'Cancel' })));
}

function sessionRow(s) {
  return el('div', { class: 'sess' },
    el('span', { class: 'name', text: s.name }),
    el('span', { class: 'dim', text: s.launcher }),
    el('code', { text: s.command || '' }),
    el('span', { class: 'dim', text: `${fmtAge(s.created)} · ${s.attached} attached` }),
    el('a', { class: 'btn', href: `/tty/?arg=${encodeURIComponent(s.tmux)}`, target: '_blank', rel: 'noopener', text: 'Attach' }),
    el('button', { class: 'danger', onclick: async () => { try { await api('DELETE', `/api/sessions/${encodeURIComponent(s.tmux)}`); setError(null); } catch (e) { setError(e.message); } await poll(true); }, text: 'Kill' }));
}

function repoBlock(p, r) {
  const key = `${p.name}/${r.name}`;
  const title = el('div', { class: 'row head' },
    el('div', { class: 'row' },
      el('span', { class: 'title', text: r.name }),
      r.state === 'ok' ? [el('span', { class: 'dim', text: r.branch || '' }), el('span', { class: 'dot' + (r.dirty ? ' dirty' : ''), title: r.dirty ? 'uncommitted changes' : 'clean' })] : null,
      r.state === 'cloning' ? el('span', { class: 'badge warn', text: 'cloning…' }) : null,
      r.state === 'clone-failed' ? el('span', { class: 'badge bad', text: 'clone failed (attach to see the error)' }) : null,
      r.state === 'nogit' ? el('span', { class: 'badge bad', text: 'no git' }) : null,
      r.state === 'unknown' ? el('span', { class: 'dot unknown', title: 'git status unknown' }) : null),
    el('div', { class: 'row' },
      el('a', { class: 'btn', href: codeServerUrl(r.path), target: '_blank', rel: 'noopener', text: 'code-server' }),
      el('button', { class: 'primary', onclick: () => { ui.openForm = 'session:' + key; renderProjects(); }, text: 'New session' }),
      confirmButton('rm:' + key, 'Remove', () => api('DELETE', `/api/projects/${encodeURIComponent(p.name)}/repos/${encodeURIComponent(r.name)}`))));
  const block = el('div', { class: 'repo' }, title);
  if (ui.openForm === 'session:' + key) block.append(sessionForm(p, r));
  for (const s of r.sessions) block.append(sessionRow(s));
  return block;
}

function projectCard(p) {
  const nSess = p.repos.reduce((n, r) => n + r.sessions.length, 0) + p.orphan_sessions.length;
  const card = el('div', { class: 'card' },
    el('div', { class: 'row head' },
      el('div', { class: 'row' }, el('h2', { text: p.name }), el('span', { class: 'dim', text: `${p.repos.length} repo${p.repos.length === 1 ? '' : 's'} · ${nSess} session${nSess === 1 ? '' : 's'}` })),
      el('div', { class: 'row' },
        el('a', { class: 'btn', href: codeServerUrl(p.path), target: '_blank', rel: 'noopener', text: 'Open project in code-server' }),
        el('button', { onclick: () => { ui.openForm = 'repo:' + p.name; renderProjects(); }, text: 'Add repo' }),
        confirmButton('del:' + p.name, 'Delete', () => api('DELETE', `/api/projects/${encodeURIComponent(p.name)}`)))));
  if (ui.openForm === 'repo:' + p.name) card.append(addRepoForm(p));
  if (!p.repos.length) card.append(el('div', { class: 'dim', text: 'No repos yet. Add one (blank or clone URL).' }));
  for (const r of p.repos) card.append(repoBlock(p, r));
  for (const s of p.orphan_sessions) card.append(el('div', { class: 'repo' }, el('div', { class: 'dim', text: `sessions in removed repo ${s.repo}` }), sessionRow(s)));
  return card;
}

function renderProjects() {
  const sec = $('#projects');
  sec.textContent = '';
  if (!state.projects.length) sec.append(el('div', { class: 'card dim', text: 'No projects yet.' }));
  for (const p of state.projects) sec.append(projectCard(p));
}

/* ---------- login modal ---------- */

async function startLogin() {
  try { await api('POST', '/api/claude/login'); setError(null); openModal(); await poll(true); }
  catch (e) { setError(e.message); }
}

async function logout() {
  try { const r = await api('POST', '/api/claude/logout'); if (!r.ok) setError('logout: ' + (r.output || 'failed')); else setError(null); }
  catch (e) { setError(e.message); }
  await poll(true);
}

const modalParts = {};

function openModal() {
  ui.modal = true;
  const m = $('#modal');
  m.textContent = '';
  const link = el('a', { class: 'btn primary', href: '#', target: '_blank', rel: 'noopener', text: 'Open sign-in page' });
  const urlText = el('div', { class: 'url' });
  const copy = el('button', { onclick: async () => { try { await navigator.clipboard.writeText(modalParts.url || ''); copy.textContent = 'copied'; } catch (_) { copy.textContent = 'copy failed'; } }, text: 'Copy link' });
  const code = el('input', { type: 'text', placeholder: 'paste the whole code from the browser (code#state)', autocomplete: 'off' });
  const send = el('button', { class: 'primary', type: 'submit', text: 'Send code' });
  const form = el('form', { class: 'inline', onsubmit: async (e) => {
    e.preventDefault();
    try { await api('POST', '/api/claude/login/code', { code: code.value.trim() }); code.value = ''; setError(null); status.textContent = 'Code sent. Waiting for Claude to confirm…'; }
    catch (err) { status.textContent = err.message; }
    await poll(true);
  } }, code, send);
  const status = el('div', { class: 'dim' });
  const tail = el('pre', { class: 'tail' });
  Object.assign(modalParts, { link, urlText, code, status, tail, url: null });
  m.append(el('div', { class: 'modal-box' },
    el('h2', { text: 'Log in to Claude Code' }),
    el('ol', {},
      el('li', {}, 'Open the sign-in page and log in with your Anthropic account.'),
      el('li', {}, 'The page shows a code. Copy the whole thing and paste it below.')),
    el('div', { class: 'row' }, link, copy, el('a', { class: 'btn', href: '/tty/?arg=_ccboard-login', target: '_blank', rel: 'noopener', text: 'Open in terminal' })),
    urlText,
    form, status,
    el('details', {}, el('summary', { class: 'dim', text: 'terminal output' }), tail),
    el('div', { class: 'row' }, el('button', { onclick: closeModal, text: 'Close' }))));
  m.classList.remove('hidden');
  updateModal();
}

function updateModal() {
  if (!ui.modal) return;
  const l = state.login || {};
  const c = state.claude || {};
  modalParts.url = l.url;
  if (l.url) { modalParts.link.href = l.url; modalParts.link.classList.remove('muted'); modalParts.urlText.textContent = l.url; }
  else { modalParts.link.href = '#'; modalParts.link.classList.add('muted'); modalParts.urlText.textContent = l.running ? 'waiting for the sign-in link…' : 'login is not running'; }
  modalParts.tail.textContent = (l.tail || []).join('\n');
  if (c.loggedIn) { modalParts.status.textContent = `Logged in as ${c.email || ''}.`; modalParts.code.disabled = true; }
}

function closeModal() { ui.modal = false; $('#modal').classList.add('hidden'); }

/* ---------- polling ---------- */

function render() {
  renderHeader();
  renderBanner();
  renderNewProject();
  const typing = document.activeElement && document.activeElement.closest('#projects');
  if (!ui.openForm && !typing) renderProjects();
  updateModal();
}

async function poll(force) {
  try {
    const s = await api('GET', '/api/state');
    const j = JSON.stringify(s);
    const changed = j !== ui.lastJson;
    ui.lastJson = j;
    state = s;
    if (changed || force) render();
    else { renderHeader(); updateModal(); }
  } catch (e) {
    if (!state) { $('#banner').textContent = 'Cannot reach ccboard: ' + e.message; }
    else setError('poll failed: ' + e.message);
  }
  clearTimeout(pollTimer);
  pollTimer = setTimeout(() => poll(false), ui.modal ? 2000 : 3000);
}

poll(true);
