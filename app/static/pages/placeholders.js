/* ccboard placeholder pages: routes whose page arrives in a later phase (project, quad, usage, memory, onboarding). Each one
   registers now so the route exists, the title and breadcrumbs work and a deep link shows what is coming instead of a blank page.
   They take onRoute, so changing only the params or the query (#/p/a to #/p/a?tab=files) redraws in place without a remount. */
'use strict';

const PLACEHOLDER_INFO = {
  project: { icon: 'folder-close', name: 'Project', version: 'v0.5.6' },
  quad: { icon: 'grid-view', name: 'Quad view', version: 'v0.5.9' },
  usage: { icon: 'timeline-line-chart', name: 'Usage', version: 'v0.5.17' },
  memory: { icon: 'database', name: 'Memory', version: 'v0.5.20' },
  onboarding: { icon: 'build', name: 'Onboarding', version: 'v0.5.19' },
};

/* What the route names, for the title: the project (and repo) or step, then the tab, if any. */
function placeholderDetail(id, route) {
  const p = (route && route.params) || {};
  const q = (route && route.query) || {};
  const parts = [];
  if (id === 'project') parts.push(p.project + (p.repo ? '/' + p.repo : ''));
  else if (id === 'memory' && p.project) parts.push(p.project);
  else if (id === 'onboarding' && p.step) parts.push('new project');
  if (q.tab) parts.push(q.tab);
  return parts.join(' · ');
}

function placeholderTitle(id, route) {
  const detail = placeholderDetail(id, route);
  return PLACEHOLDER_INFO[id].name + (detail ? ' ' + detail : '');
}

function placeholderPage(id) {
  const info = PLACEHOLDER_INFO[id];
  let host = null;
  const draw = (route) => {
    host.textContent = '';
    host.append(pageEmpty(info.icon, placeholderTitle(id, route), `arrives in ${info.version}`));
    document.title = placeholderTitle(id, route) + ' · ccboard';
  };
  return {
    title: (route) => placeholderTitle(id, route),
    mount(root, route) {
      host = el('div', { class: 'page-narrow placeholder', 'data-placeholder': id });
      root.append(host);
      draw(route);
    },
    update() {},
    onRoute(route) { if (host) draw(route); },
    unmount() { host = null; },
  };
}

/* Project page: v0.5.6 builds the real one (tree, tabs, files). Until then the route carries the quick links that matter
   every day: open the project folder or any of its repos in code-server, and the roster rows live on Home / Agents. */
function projectLinks(route) {
  const st = typeof currentState === 'function' ? currentState() : null;
  const name = route && route.params && route.params.project;
  const p = st && (st.projects || []).find((x) => x.name === name);
  const box = el('div', { class: 'proj-links' });
  if (!p) {
    box.append(el('span', { class: 'dim', text: st ? 'unknown project' : 'loading…' }));
    return box;
  }
  const port = st.config && st.config.code_https_port;
  const open = (label, path) => el('a', { class: 'btn small', href: codeServerUrl(path), target: '_blank', rel: 'noopener', title: path }, ic('code'), label);
  const row = el('div', { class: 'proj-row' }, el('span', { class: 'lbl', text: 'code-server' }));
  if (!port) row.append(el('span', { class: 'dim', text: 'code-server port unknown (rerun install.sh)' }));
  else {
    row.append(open('project folder', p.path));
    for (const r of (p.repos || [])) row.append(open(r.name, r.path));
  }
  box.append(row);
  // repos and their state (cloning / failed / no git), Remove per repo, Add repo, Delete project: the v0.4 Home card's actions
  const repos = el('div', { class: 'proj-row proj-repos' }, el('span', { class: 'lbl', text: 'repos' }));
  for (const r of (p.repos || [])) {
    const bad = r.state && r.state !== 'ok' && r.state !== 'project';
    repos.append(el('span', { class: 'proj-repo' },
      el('span', { class: 'mono', text: r.name }),
      r.branch ? el('span', { class: 'dim', text: ' ' + r.branch }) : null,
      bad ? el('span', { class: 'badge warn', text: r.state }) : null,
      typeof confirmButton === 'function' ? confirmButton('rm:' + p.name + '/' + r.name, 'Remove', () => api('DELETE', `/api/projects/${encodeURIComponent(p.name)}/repos/${encodeURIComponent(r.name)}`), true) : null));
  }
  if (!(p.repos || []).length) repos.append(el('span', { class: 'dim', text: 'no repos yet' }));
  box.append(repos);
  const actions = el('div', { class: 'proj-row proj-actions' });
  if (typeof addRepoForm === 'function' && typeof openSheet === 'function') {
    actions.append(el('button', { class: 'small', type: 'button', text: 'Add repo', onclick: () => openSheet({ title: `Add a repo to ${p.name}`, body: addRepoForm(p) }) }));
  }
  if (typeof confirmButton === 'function') {
    actions.append(confirmButton('del:' + p.name, 'Delete project', async () => { await api('DELETE', `/api/projects/${encodeURIComponent(p.name)}`); navigate('#/'); }));
  }
  box.append(actions);
  return box;
}

function projectPlaceholderPage() {
  const base = placeholderPage('project');
  let links = null;
  let sig = '';
  const shape = (route) => {
    const st = typeof currentState === 'function' ? currentState() : null;
    const name = route && route.params && route.params.project;
    const p = st && (st.projects || []).find((x) => x.name === name);
    return JSON.stringify([name, !!st, p ? [p.path, (p.repos || []).map((r) => [r.name, r.state, r.branch])] : null, st && st.config && st.config.code_https_port]);
  };
  const draw = (route) => {
    if (!links) return;
    const now = shape(route);
    if (now === sig) return;                                  // nothing changed: keep the nodes (confirm buttons mid two-tap, focus)
    sig = now;
    links.textContent = '';
    links.append(projectLinks(route));
  };
  return Object.assign({}, base, {
    mount(root, route) {
      links = el('div', { class: 'proj-head' });
      root.append(links);
      base.mount(root, route);
      draw(route);
    },
    update(st, route) { draw(route); },                       // the first state arrives after mount: fill the links then
    onRoute(route) { base.onRoute(route); draw(route); },
    unmount() { links = null; sig = ''; base.unmount(); },
  });
}

registerPage('project', projectPlaceholderPage());
registerPage('quad', placeholderPage('quad'));
registerPage('usage', placeholderPage('usage'));
registerPage('memory', placeholderPage('memory'));
registerPage('onboarding', placeholderPage('onboarding'));
