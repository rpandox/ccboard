/* ccboard mobile terminal: ttyd in an iframe + a key toolbar and quick replies via tmux send-keys. */
'use strict';
const name = decodeURIComponent(location.pathname.replace(/^\/term\//, '').replace(/\/$/, ''));
const $ = (s) => document.querySelector(s);
const QUICK_KEY = 'ccboard:quick:' + name;
const DEFAULT_QUICK = ['y', 'yes', 'continue', 'n', 'run the tests', '/compact'];

async function api(method, path, body) {
  const r = await fetch(path, { method, headers: { 'X-CCBoard': '1', ...(body ? { 'Content-Type': 'application/json' } : {}) }, body: body ? JSON.stringify(body) : undefined });
  let data = null; try { data = await r.json(); } catch (_) { /* no body */ }
  if (!r.ok) throw new Error((data && data.error) || `${r.status}`);
  return data;
}

function flash(msg) { const m = $('#termmsg'); m.textContent = msg; setTimeout(() => { if (m.textContent === msg) m.textContent = ''; }, 2500); }

async function sendKeys(keys) { try { await api('POST', `/api/sessions/${encodeURIComponent(name)}/keys`, { keys }); } catch (e) { flash(e.message); } }
async function sendText(text, enter) { try { await api('POST', `/api/sessions/${encodeURIComponent(name)}/keys`, { text, enter: !!enter }); } catch (e) { flash(e.message); } }

function loadQuick() { try { const v = JSON.parse(localStorage.getItem(QUICK_KEY) || 'null'); return Array.isArray(v) ? v : DEFAULT_QUICK; } catch (_) { return DEFAULT_QUICK; } }
function saveQuick(list) { try { localStorage.setItem(QUICK_KEY, JSON.stringify(list)); } catch (_) { /* ignore */ } }

function renderQuick() {
  const q = $('#quick'); q.textContent = '';
  for (const t of loadQuick()) {
    const b = Object.assign(document.createElement('button'), { className: 'bp5-button' }); b.type = 'button'; b.textContent = t;
    b.addEventListener('click', () => sendText(t, true));
    q.append(b);
  }
}

function editQuick() {
  const cur = loadQuick().join('\n');
  const v = window.prompt('Quick replies, one per line (sent with Enter):', cur);
  if (v === null) return;
  saveQuick(v.split('\n').map(s => s.trim()).filter(Boolean).slice(0, 12));
  renderQuick();
}

async function refreshState() {
  try {
    const st = await api('GET', '/api/state');
    let found = null;
    for (const p of st.projects) for (const r of (p.root ? [p.root, ...p.repos] : p.repos)) for (const s of r.sessions) if (s.tmux === name) found = s;
    const el = $('#termstate');
    const INTENT = { working: 'bp5-intent-primary', waiting: 'bp5-intent-warning', done: 'bp5-intent-success', errored: 'bp5-intent-danger' };
    if (!found) { el.className = 'state bp5-tag bp5-minimal bp5-round'; el.textContent = 'session not found'; return; }
    el.className = 'state bp5-tag bp5-minimal bp5-round ' + (INTENT[found.state] || '');
    el.textContent = `${found.state}${found.stats && typeof found.stats.context_pct === 'number' ? ' · ctx ' + Math.round(found.stats.context_pct) + '%' : ''}`;
    document.title = `${name} · ccboard`;
  } catch (_) { /* keep the last text */ }
}

$('#termname').textContent = name;
$('#tty').src = `/tty/?arg=${encodeURIComponent(name)}`;
for (const b of document.querySelectorAll('#keys .keys button')) {
  b.addEventListener('click', () => b.dataset.key ? sendKeys([b.dataset.key]) : sendText(b.dataset.text, b.dataset.enter === '1'));
}
$('#sendform').addEventListener('submit', (e) => { e.preventDefault(); const i = $('#sendtext'); if (i.value) { sendText(i.value, true); i.value = ''; } else { sendKeys(['Enter']); } });
$('#editquick').addEventListener('click', editQuick);
renderQuick();
refreshState();
setInterval(refreshState, 5000);
