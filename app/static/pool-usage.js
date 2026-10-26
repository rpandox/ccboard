'use strict';

/* Pool quota groups in the attention layer. Definitions stay separate from boot for unit tests. */
function poolAge(group) {
  const age = group.age_s;
  if (typeof age !== 'number') return 'no sample yet';
  if (age < 60) return `${Math.floor(age)} s old`;
  if (age < 3600) return `${Math.floor(age / 60)} min old`;
  return `${Math.floor(age / 3600)} h old`;
}
function poolWindow(group) {
  const windows = [['5 h', group.window_5h_pct], ['weekly', group.weekly_pct]].filter(([, v]) => typeof v === 'number' && Number.isFinite(v));
  return windows.sort((a, b) => b[1] - a[1])[0];
}
function poolKind(group) {
  const worst = poolWindow(group);
  if (group.state !== 'ok' || !worst) return 'unknown';
  return worst[1] < 50 ? 'filled' : worst[1] <= 85 ? 'half' : 'hollow';
}
function poolNames(group) {
  return (group.accounts || []).map((id) => group.account_labels?.[id] || id).join(', ');
}
function poolNameNodes(group) {
  return (group.accounts || []).flatMap((id, i) => [i ? ', ' : '', el('span', { class: group.account_labels?.[id] ? '' : 'dim', text: group.account_labels?.[id] || id })]);
}
function poolReset(group) {
  const dates = [group.window_5h_reset_at, group.weekly_reset_at].map((v) => v ? new Date(v) : null).filter((d) => d && Number.isFinite(d.getTime()));
  if (!dates.length) return 'reset unknown';
  const next = dates.sort((a, b) => a - b)[0];
  return `resets ${next.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
}
function poolBar(label, pct, known) {
  const valid = known && typeof pct === 'number' && Number.isFinite(pct);
  return el('div', { class: 'pool-window' },
    el('div', { class: 'pool-window-label', text: `${label}: ${valid ? Math.round(pct) + '% used' : 'unknown'}` }),
    valid ? svg('svg', { class: 'pool-bar', viewBox: '0 0 100 6', preserveAspectRatio: 'none', role: 'img', 'aria-label': `${label} ${Math.round(pct)}% used` },
      svg('rect', { class: 'pool-bar-track', x: 0, y: 0, width: 100, height: 6 }),
      svg('rect', { class: 'pool-bar-fill', x: 0, y: 0, width: Math.max(0, Math.min(100, pct)), height: 6 }))
      : el('div', { class: 'pool-bar pool-hatched', role: 'img', 'aria-label': `${label} unknown` }));
}
function poolSheetBody(groups) {
  const body = el('div', { class: 'pool-details' });
  for (const g of groups) {
    const known = g.state === 'ok';
    body.append(el('section', { class: 'pool-detail' },
      el('h3', {}, `${g.provider === 'codex' ? 'Codex' : 'Claude'} · `, ...poolNameNodes(g)),
      el('p', { class: 'dim pool-accounts', text: `Accounts: ${poolNames(g)}` }),
      poolBar('5 h', g.window_5h_pct, known), poolBar('Weekly', g.weekly_pct, known),
      el('p', { class: 'dim', text: `${known ? '' : 'unknown, '}${poolAge(g)} · ${poolReset(g)}` }),
      el('p', { class: 'dim', text: `Source: ${g.source || 'none'} · Confidence: ${typeof g.confidence === 'number' ? Math.round(g.confidence * 100) + '%' : 'unknown'}` })));
  }
  return body;
}
function poolStrip(pools) {
  if (!pools) return null;
  const groups = pools.groups || [];
  const strip = el('div', { class: 'pool-strip', 'aria-label': 'Pool usage' });
  for (const provider of ['claude', 'codex']) {
    const members = groups.filter((g) => g.provider === provider);
    if (!members.length) continue;
    const row = el('div', { class: 'pool-provider', 'data-provider': provider }, el('span', { class: 'pool-provider-name', text: provider === 'codex' ? 'Codex' : 'Claude' }));
    const items = el('div', { class: 'pool-groups' });
    for (const g of members) {
      const button = el('button', { type: 'button', class: 'pool-group', 'data-pool-key': `${provider}:${g.id}` });
      updatePoolButton(button, g, groups);
      items.append(button);
    }
    row.append(items); strip.append(row);
  }
  return strip;
}
function updatePoolButton(button, g, groups) {
  const kind = poolKind(g), worst = poolWindow(g);
  const label = kind === 'unknown' ? `unknown, ${poolAge(g)}` : `${worst[0] === 'weekly' ? 'weekly ' : ''}${Math.round(worst[1])}% · ${poolReset(g)}`;
  button.setAttribute('title', `${poolNames(g)}: ${label}`);
  button.setAttribute('aria-label', `${g.provider} ${poolNames(g)}: ${label}`);
  button.onclick = () => openSheet({ title: 'Pool usage', body: poolSheetBody(groups) });
  button.textContent = '';
  button.append(el('span', { class: `pool-symbol ${kind}${kind === 'unknown' ? ' pool-hatched' : ''}`, 'aria-hidden': 'true' }),
    el('span', {}, ...poolNameNodes(g), ` · ${label}`));
}
function paintPoolStrip(st) {
  let host = document.getElementById('pool-strip-host');
  if (!st || !st.pools) { if (host) host.remove(); return; }
  if (!host) {
    const banner = document.getElementById('banner');
    if (!banner) return;
    host = el('div', { id: 'pool-strip-host' });
    banner.parentNode.insertBefore(host, banner.nextSibling);
  }
  if (!host.firstChild) { host.append(poolStrip(st.pools)); return; }
  const strip = host.firstChild, groups = st.pools.groups || [];
  for (const provider of ['claude', 'codex']) {
    const members = groups.filter((g) => g.provider === provider);
    let row = strip.querySelector(`[data-provider="${provider}"]`);
    if (!members.length) { if (row) row.remove(); continue; }
    if (!row) {
      row = poolStrip({ groups: members }).firstChild;
      strip.append(row);
    }
    const items = row.querySelector('.pool-groups');
    const existing = new Map([...items.children].map((button) => [button.getAttribute('data-pool-key'), button]));
    const keep = new Set();
    members.forEach((g, i) => {
      const key = `${provider}:${g.id}`;
      const button = existing.get(key) || el('button', { type: 'button', class: 'pool-group', 'data-pool-key': key });
      updatePoolButton(button, g, groups);
      if (items.children[i] !== button) items.insertBefore(button, items.children[i] || null);
      keep.add(key);
    });
    for (const [key, button] of existing) if (!keep.has(key)) button.remove();
  }
}
