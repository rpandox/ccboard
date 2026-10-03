/* ccboard search page (#/search?q=...): full-text search over the Claude transcripts (display only). The topbar search box
   navigates here; the page also carries its own box for phones. A changed ?q= re-runs the search without remounting. */
'use strict';

const searchPage = { refs: null, seq: 0 };

/* Older callers asked for a search by name: it is a navigation now. */
function runSearch(q) {
  const text = (q || '').trim();
  if (text) navigate(buildHash('search', {}, { q: text }));
}

async function renderSearch(sec, q) {
  const mine = ++searchPage.seq;                       // a slow answer to an older query must not overwrite a newer one
  sec.textContent = '';
  if (!q) { sec.append(el('div', { class: 'dim', text: 'Type a query to search every Claude transcript on this box.' })); return; }
  sec.append(el('div', { class: 'head' }, el('h2', { text: `Search: ${q}` })));
  const body = el('div', { class: 'dim', text: 'searching…' });
  sec.append(body);
  try {
    const r = await api('GET', `/api/search?q=${encodeURIComponent(q)}`);
    if (mine !== searchPage.seq) return;
    body.remove();
    if (!r.results || !r.results.length) { sec.append(el('div', { class: 'dim', text: 'no matches' })); return; }
    for (const hit of r.results) {
      const where = hit.project ? `${hit.project}/${hit.repo}` : (hit.cwd || '').split('/').slice(-2).join('/');
      sec.append(el('div', { class: 'sess' },
        el('div', { class: 'main' },
          el('span', { class: 'state ' + (hit.kind === 'assistant' ? 'done' : ''), text: hit.kind }),
          el('span', { class: 'name', text: where }),
          el('span', { class: 'dim', text: (hit.ts || '').replace('T', ' ').slice(0, 16) }),
          hit.tmux ? el('a', { class: 'btn small', href: sessionHash(hit.tmux), text: 'Peek' }) : null,
          hit.tmux ? el('a', { class: 'btn small', href: `/term/${encodeURIComponent(hit.tmux)}`, target: '_blank', rel: 'noopener', text: 'Attach' }) : el('code', { text: String(hit.session_id || '').slice(0, 8) })),
        el('div', { class: 'last', text: hit.snippet })));
    }
  } catch (e) {
    if (mine !== searchPage.seq) return;
    body.className = 'bad';
    body.textContent = e.message;
  }
}

function searchQuery(r) { return ((r && r.query && r.query.q) || '').trim(); }

registerPage('search', {
  title: (r) => (searchQuery(r) ? `Search: ${searchQuery(r)}` : 'Search'),
  mount(root, route) {
    const input = el('input', { type: 'search', placeholder: 'search transcripts…', 'aria-label': 'Search transcripts', autocomplete: 'off', enterkeyhint: 'search' });
    input.value = searchQuery(route);
    const form = el('form', { class: 'inline', onsubmit: (e) => {
      e.preventDefault();
      const q = input.value.trim();
      if (q) navigate(buildHash('search', {}, { q }));
    } }, input, el('button', { class: 'primary', type: 'submit', text: 'Search' }));
    const results = el('section', { id: 'search', class: 'card' });
    root.append(el('div', { class: 'page-head' }, el('h1', { text: 'Search' })), form, results);
    searchPage.refs = { input, results };
    renderSearch(results, searchQuery(route));
  },
  update() {},
  onRoute(route) {
    const r = searchPage.refs;
    if (!r) return;
    if (document.activeElement !== r.input) r.input.value = searchQuery(route);
    renderSearch(r.results, searchQuery(route));
  },
  unmount() { searchPage.seq += 1; searchPage.refs = null; },
});
