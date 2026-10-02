/* ccboard live: the SSE last-lines grid (one EventSource, tiles keyed by tmux name). */
'use strict';

const live = { on: false, es: null, tiles: {} };

function liveTile(name) {
  let t = live.tiles[name];
  if (t) return t;
  const pre = el('pre');
  const parts = name.split('--');
  t = el('div', { class: 'tile', 'data-tmux': name },
    el('div', { class: 'row' }, el('span', { class: 'name', text: `${parts[0]}/${parts[1]} · ${parts[2] || ''}` }),
      el('a', { class: 'btn', href: `/term/${encodeURIComponent(name)}`, target: '_blank', rel: 'noopener', text: 'Attach' })),
    pre);
  t.pre = pre;
  live.tiles[name] = t;
  $('#live .live-grid').append(t);
  return t;
}

function liveStart() {
  if (live.es) return;
  const sec = $('#live');
  sec.textContent = '';
  sec.append(el('div', { class: 'row head' }, el('h2', { text: 'Live' }), el('span', { class: 'dim', text: 'last 20 lines of every session, every 2 s' })),
    el('div', { class: 'live-grid' }));
  sec.classList.remove('hidden');
  live.tiles = {};
  const es = new EventSource('/api/stream');
  live.es = es;
  es.addEventListener('lines', (e) => {
    try { const d = JSON.parse(e.data); liveTile(d.name).pre.textContent = d.lines.join('\n'); } catch (_) { /* ignore */ }
  });
  es.addEventListener('tick', (e) => {
    try {
      const d = JSON.parse(e.data);
      for (const n of Object.keys(live.tiles)) if (!d.sessions.includes(n)) { live.tiles[n].remove(); delete live.tiles[n]; }
      const attn = new Set(inboxItems().map(s => s.tmux));
      for (const [n, t] of Object.entries(live.tiles)) t.classList.toggle('attn', attn.has(n));
      if (!d.sessions.length) $('#live .live-grid').textContent = '';
    } catch (_) { /* ignore */ }
  });
  es.onerror = () => { /* EventSource reconnects on its own */ };
}

function liveStop() {
  if (live.es) { live.es.close(); live.es = null; }
  $('#live').classList.add('hidden');
  live.tiles = {};
}

function toggleLive() {
  live.on = !live.on;
  try { localStorage.setItem('ccboard:live', live.on ? '1' : '0'); } catch (_) { /* ignore */ }
  if (live.on) liveStart(); else liveStop();
  renderHeader();
}
