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

registerPage('project', placeholderPage('project'));
registerPage('quad', placeholderPage('quad'));
registerPage('usage', placeholderPage('usage'));
registerPage('memory', placeholderPage('memory'));
registerPage('onboarding', placeholderPage('onboarding'));
