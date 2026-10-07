/* ccboard placeholder pages: routes whose page arrives in a later phase (none now: the memory route left with v0.5.20, pages/memory.js). Each one
   registers now so the route exists, the title and breadcrumbs work and a deep link shows what is coming instead of a blank page.
   They take onRoute, so changing only the params or the query (#/memory to #/memory/shop) redraws in place without a remount.
   The project route left this file with v0.5.6: pages/project.js is its real page; the usage route left with v0.5.17 (pages/usage.js); the quad route
   left with v0.5.9 (pages/quad.js); the onboarding route left with v0.5.19 (pages/onboarding.js). */
'use strict';

const PLACEHOLDER_INFO = {};

/* What the route names, for the title: the project (and repo) or step, then the tab, if any. */
function placeholderDetail(id, route) {
  const p = (route && route.params) || {};
  const q = (route && route.query) || {};
  const parts = [];
  if (p.project) parts.push(p.project);
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

/* No route is a placeholder now: a later phase adds PLACEHOLDER_INFO[id] and a registerPage(id, placeholderPage(id)) line here. */
