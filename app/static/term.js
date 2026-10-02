/* ccboard mobile terminal: ttyd in an iframe + a key toolbar and quick replies via tmux send-keys.
   Loaded after core.js and components.js, which provide $, el() and api(). */
'use strict';
const name = decodeURIComponent(location.pathname.replace(/^\/term\//, '').replace(/\/$/, ''));
const QUICK_KEY = 'ccboard:quick:' + name;
const DEFAULT_QUICK = ['y', 'yes', 'continue', 'n', 'run the tests', '/compact'];

function flash(msg) { const m = $('#termmsg'); m.textContent = msg; setTimeout(() => { if (m.textContent === msg) m.textContent = ''; }, 2500); }

async function sendKeys(keys) { try { await api('POST', `/api/sessions/${encodeURIComponent(name)}/keys`, { keys }); } catch (e) { flash(e.message); } }
async function sendText(text, enter) { try { await api('POST', `/api/sessions/${encodeURIComponent(name)}/keys`, { text, enter: !!enter }); } catch (e) { flash(e.message); } }

function loadQuick() { try { const v = JSON.parse(localStorage.getItem(QUICK_KEY) || 'null'); return Array.isArray(v) ? v : DEFAULT_QUICK; } catch (_) { return DEFAULT_QUICK; } }
function saveQuick(list) { try { localStorage.setItem(QUICK_KEY, JSON.stringify(list)); } catch (_) { /* ignore */ } }

function renderQuick() {
  const q = $('#quick'); q.textContent = '';
  for (const t of loadQuick()) {
    q.append(el('button', { type: 'button', text: t, onclick: () => sendText(t, true) }));
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
    const old = $('#termstate');
    if (!found) { old.replaceWith(el('span', { id: 'termstate', class: 'state', text: 'session not found' })); return; }
    const ctx = found.stats && typeof found.stats.context_pct === 'number' ? ' · ctx ' + Math.round(found.stats.context_pct) + '%' : '';
    old.replaceWith(el('span', { id: 'termstate', class: 'state ' + found.state, text: `${found.state}${ctx}` }));
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
