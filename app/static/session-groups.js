/* Home and project session groups; no route registration. */
'use strict';
function homeScan(st) {
  const all = [];
  const projects = [];
  for (const p of ((st && st.projects) || [])) {
    const groups = [];
    const add = (key, label, repo, folder, sessions) => {
      const items = (sessions || []).map((s) => ({ ...s, project: p.name, repo: repo ? repo.name : (s.repo || '?'), folder }));
      for (const it of items) all.push(it);
      groups.push({ key, label, repo, folder, items });
    };
    if (p.root) add('root', 'project folder', p.root, true, p.root.sessions);
    for (const r of (p.repos || [])) add(r.name, r.name, r, r.name === 'root' || !!r.root, r.sessions);
    const orphans = new Map();
    for (const s of (p.orphan_sessions || [])) { const k = s.repo || '?'; if (!orphans.has(k)) orphans.set(k, []); orphans.get(k).push(s); }
    for (const [k, list] of orphans) add('x:' + k, `removed repo ${k}`, null, k === 'root', list);
    projects.push({ p, groups });
  }
  return { all, projects };
}

function homeNewSession(p, r) { return launch({ mode: 'session', project: p, repo: r }); }

function homeRowOpts(kind) {
  return { rich: true, perm: true, showProject: kind !== 'project', noWhere: kind === 'project' };
}

function homeGroupNode(g, kind, getProject) {
  const name = el('span', { class: 'pg-name mono' });
  const branch = el('span', { class: 'pg-branch dim mono' });
  const dot = el('span', { class: 'dot hidden' });
  const cost = el('span', { class: 'pg-cost dim mono' });
  const add = el('button', { class: 'minimal small pg-add', type: 'button', 'aria-label': 'New session in this repo', title: 'new session here' }, ic('plus'));
  const head = el('div', { class: 'pgroup-head' }, name, branch, dot, cost, add);
  const list = el('div', { class: 'pg-list' });
  const node = el('div', { class: 'pgroup' }, head, list);
  const cur = { g };
  add.addEventListener('click', () => {
    const r = cur.g.repo;
    const p = getProject();
    if (r && p) homeNewSession(p, r);
  });
  const rows = makeKeyedList(list, { key: (s) => s.tmux, create: (s) => sessionCard(s, homeRowOpts(kind)), patch: (n, s) => n.ccPatch(s) });
  node.ccPatch = (g2) => {
    cur.g = g2;
    head.classList.toggle('hidden', !g2.showHead);
    patchLinkedName(name, g2.label, g2.repo);
    const r = g2.repo;
    setTextIfChanged(branch, r && r.state === 'ok' && r.branch ? r.branch : '');
    dot.classList.toggle('hidden', !(r && r.state === 'ok'));
    dot.classList.toggle('dirty', !!(r && r.dirty));
    dot.setAttribute('title', r && r.dirty ? 'uncommitted changes' : 'clean');
    const p = getProject();
    setTextIfChanged(cost, r && p ? (repoCost(p, r) || '') : '');
    add.classList.toggle('hidden', !(r && (r.state === 'ok' || r.state === 'unknown' || g2.folder)));
    rows.update(g2.items);
  };
  node.ccPatch(g);
  return node;
}
