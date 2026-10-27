/* Dock implementation, with core policy/state in shell.js. */
'use strict';
/* ---------- the terminal dock (v0.5.9): the live terminal of one session beside any page, from 1024 px up ----------
   One TermKit.termPane (full mode: it sizes the session like /term does) in #dock, with a drag handle on its left edge. The pane is created when the dock opens and destroyed
   when it closes or stops being allowed (a window under 1024 px, ccboard:dock:off): a mounted full-mode client sizes the tmux session even when it is hidden, so a hidden dock
   must not exist. localStorage: ccboard:dock = the session shown (restored on load), ccboard:dock:w = the width in px (absent = the CSS default min(44vw, 640px)),
   ccboard:dock:off = '1' turns the dock off in this browser (a row's Open then opens /term/<name> in its own tab as before). Below 1440 px an open dock folds the sidebar to
   the rail (body[data-sb=rail]) without writing ccboard:sb; asking for the full sidebar while the dock is open keeps it until the dock closes. The session peek (session.js)
   goes to the sheet while the dock is on, so the two never share #dock.
   The quad page (#/quad) and the dock are exclusive: the quad owns the window (its tiles are the terminals, and a tile's width must not change under it), so the dock is
   suspended while the quad is the page (Shell.dockSuspend, called by Quad.mount; Shell.dockResume by its destroy; mountedId === 'quad' is the same test the router makes):
   the pane goes (D.wanted keeps the session, ccboard:dock stays), no Open link opens it (they open a tab), and it comes back when the person leaves the quad. */

/* Wide enough for a dock (1024 px up); the media query the shell already watches, else asked now. */

/* Does this window have room for the sidebar and the dock together (1440 px up)? */

/* The dock exists in this window and this browser: wide enough, not switched off, and the terminal kit is loaded. session.js asks this to send the peek elsewhere. */

/* termkit.js is a lazy bundle (lazy.js): the dock opens it on first use. kitReady: it is here; kitLazy: it is not, but can be had. */


/* Should an Open link or button open the dock? Not on a coarse primary pointer in a window under 1180 px (an iPad in portrait, a small tablet): the dock would leave the
   page about 525 px at 1024, and the pages are laid out for 840 and more. There an Open link opens the terminal page as before; mod+j and "Add to dock" still open the dock
   on purpose (and a dock opened that way comes back on the next load), with its width capped at 38 vw by default (shell.css) and a 44 px grabber to change it. */


/* Does the quad page own the window? Then the dock is suspended. D.held is the quad's own say (Shell.dockSuspend); mountedId === 'quad' is the router's (the session peek is an
   overlay: it never changes mountedId, so a peek over the quad is still the quad). During the quad's unmount mountedId is still 'quad': Shell.dockResume reconciles after it. */

/* Quad.mount, before it measures its column: the dock lets go of the window (its pane is taken down at once, the sidebar goes back to its own rule). */
Shell.dockSuspend = function () {
  Shell.dock.held = true;
  if (Shell.mq) Shell.applyMode(); else Shell.dockSync();
  return true;
};

/* The quad is gone: the dock comes back (the saved session, when there is room). The router moves mountedId after the old page's unmount, so the mode pass is deferred one tick. */
Shell.dockResume = function () {
  Shell.dock.held = false;
  setTimeout(() => { if (!Shell.dockHeld() && Shell.mq) Shell.applyMode(); }, 0);
  return true;
};

/* Why the dock will not open, as a sentence (a toast on mod+j; the Open links simply navigate). */
Shell.dockWhy = function () {
  const say = (t) => { if (typeof toast === 'function') toast(t, { kind: 'info' }); };
  if (!Shell.kitReady() && !Shell.kitLazy()) say('The terminal kit is not loaded: reload the board');
  else if (Shell.dockOff()) say('The terminal dock is off in this browser (localStorage ccboard:dock:off)');
  else if (!Shell.dockWide()) say('The terminal dock needs a window 1024 px wide or more');
  else if (Shell.dockHeld()) say('The Quad page shows the terminals itself: the dock is back when you leave it');
};



/* --dock-w lives on #app (shell.css reads it on #dock), set through the CSSOM: never a style attribute. 0 = back to the CSS default. */
Shell.setDockWidth = function (w, persist) {
  const app = $('#app');
  const px = Shell.clampDockW(w);
  Shell.dock.w = px;
  if (app) {
    if (px) Shell.setVar(app, '--dock-w', px + 'px');
    else { try { app.style.removeProperty('--dock-w'); } catch (_) { /* no CSSOM */ } }
  }
  const r = Shell.dock.resizer;
  if (r) {
    if (px) r.setAttribute('aria-valuenow', String(px)); else r.removeAttribute('aria-valuenow');
  }
  if (persist) Shell.dockStore.set('ccboard:dock:w', px ? px : null);
  return px;
};

/* Back to the default width: the saved value is removed (not overwritten), so the viewport-relative default applies again. */
Shell.resetDockWidth = function () { return Shell.setDockWidth(0, true); };

Shell.buildDockResizer = function () {
  const app = $('#app');
  const h = el('div', { class: 'dock-resize', role: 'separator', 'aria-orientation': 'vertical', 'aria-label': 'Resize the terminal dock', tabindex: '0',
    'aria-valuemin': String(Shell.DOCK_MIN), 'aria-valuemax': String(Shell.DOCK_MAX), title: 'Drag to resize the dock (double-click or double-tap resets)' });
  let drag = null;
  const end = () => {
    if (!drag) return;
    drag = null;
    if (app) app.classList.remove('dock-dragging');
    if (Shell.dock.w) Shell.setDockWidth(Shell.dock.w, true);
  };
  h.addEventListener('pointerdown', (e) => {
    if (e.button) return;
    e.preventDefault();
    const dock = $('#dock');
    drag = { grab: (dock ? dock.getBoundingClientRect().left : e.clientX) - e.clientX, moved: false };     // where on the strip it was taken
    try { h.setPointerCapture(e.pointerId); } catch (_) { /* no pointer capture: the move events still arrive over the strip */ }
    if (app) app.classList.add('dock-dragging');
  });
  h.addEventListener('pointermove', (e) => {
    if (!drag || !app) return;
    drag.moved = true;
    Shell.setDockWidth(app.getBoundingClientRect().right - (e.clientX + drag.grab), false);
  });
  let lastTap = 0;
  h.addEventListener('pointerup', (e) => {                              // a double tap on the grabber resets the width (a touch screen sends no dblclick here)
    if (drag && !drag.moved && e && (e.pointerType === 'touch' || e.pointerType === 'pen')) {
      const now = Date.now();
      if (now - lastTap < 350) { lastTap = 0; Shell.resetDockWidth(); } else lastTap = now;
    }
  });
  for (const t of ['pointerup', 'pointercancel', 'lostpointercapture']) h.addEventListener(t, end);
  h.addEventListener('dblclick', () => Shell.resetDockWidth());
  h.addEventListener('keydown', (e) => {
    const step = e.shiftKey ? 48 : 16;
    const cur = Shell.dock.w || (() => { const d = $('#dock'); const r = d ? d.getBoundingClientRect() : null; return r && r.width > 0 ? Math.round(r.width) : 450; })();
    let w = null;
    if (e.key === 'ArrowLeft') w = cur + step;                       // the dock is on the right: left grows it
    else if (e.key === 'ArrowRight') w = cur - step;
    else if (e.key === 'Home') w = Shell.DOCK_MIN;
    else if (e.key === 'End') w = Shell.DOCK_MAX;
    else if (e.key === 'Enter') { e.preventDefault(); Shell.resetDockWidth(); return; }
    if (w === null) return;
    e.preventDefault();
    Shell.setDockWidth(w, true);
  });
  return h;
};

/* The session the dock offers when nothing was asked for (mod+j with nothing open and nothing remembered): the one that needs you, else an errored, working, idle one,
   the latest first (the sidebar's order); '' when there is no live session. */
Shell.dockPick = function (st) {
  if (!st) return '';
  const m = Shell.model(st);
  const all = [];
  for (const p of [...m.main, ...m.older]) for (const k of p.kids) if (k.kind === 'sess' && k.state !== 'ended' && !k.node) all.push(k);      // the dock shows this board's terminals only
  all.sort((a, b) => ((a.needs ? 0 : 1) - (b.needs ? 0 : 1)) || ((SHELL_STATE_RANK[a.state] ?? 5) - (SHELL_STATE_RANK[b.state] ?? 5)) || (b.at - a.at) || a.tmux.localeCompare(b.tmux));
  return all.length ? all[0].tmux : '';
};

/* The session's row in a state payload, or null. */
Shell.dockRow = function (st, tmux) {
  if (!st || typeof rosterSessions !== 'function') return null;
  return rosterSessions(st).find((s) => s.tmux === tmux && Ref.nodeOf(s) === null) || null;      // the same name on another node is not this terminal
};

/* Paint the open pane from the state (called by renderShell on every render). */
Shell.patchDock = function (st) {
  const D = Shell.dock;
  if (!D.pane || !st) return;
  try { D.pane.update(Shell.dockRow(st, D.tmux), st); } catch (e) { console.error('ccboard dock', e); }
};

/* {project, repo} (state objects) of the place a session name project--repo--name is in, or null when the board does not know it (yet). */


/* The dock's +: a new session in the place of the one the dock shows (launch(), components.js: the launcher sheet with that project and repo filled in, one tap to Start & open).
   When the board no longer knows the place, the repo picker. */
Shell.dockNew = function (tmux) {
  const place = Shell.placeOf(tmux || Shell.dock.tmux);
  if (place) return launch({ mode: 'session', project: place.project, repo: place.repo });
  return Shell.openCreate('session');
};

/* The + among the pane's own header buttons (termkit.js builds those; it has no callback for this one), first, before reconnect. */
Shell.dockAddButton = function (pane, tmux) {
  const btns = pane && pane.head && typeof pane.head.querySelector === 'function' ? pane.head.querySelector('.tp-btns') : null;
  if (!btns) return null;
  const add = el('button', { class: 'icon minimal tp-btn', type: 'button', 'data-act': 'new', 'aria-label': 'New session in this repo', title: 'New session in this repo', onclick: () => Shell.dockNew(tmux) }, ic('plus'));
  btns.insertBefore(add, btns.firstChild);
  return add;
};

/* A session of another node in the dock (issue #144; key '<handle>/<tmux>'): the same tail tile as the quad's (Nodes.tailTile, pages/node.js), never a terminal: no framed page, no socket. The hub
   bundle may still be on its way; the tile mounts when it is here (false now). Not remembered across a reload. */
Shell.dockRemote = function (key, o) {
  const D = Shell.dock;
  const host = $('#dock');
  const k = Ref.splitKey(key);
  if (!host || !k || k.node === null) return false;
  if (typeof Nodes === 'undefined' || !Nodes.enabled()) { if (typeof toast === 'function') toast('That session is on a node that is not paired on this board', { kind: 'bad' }); return false; }
  if (D.pane && D.tmux === key) { if (o && o.focus) D.pane.focus(); return true; }
  if (!Nodes.ready || typeof Nodes.tailTile !== 'function') { Nodes.use(() => { if (!Shell.dock.pane && !Shell.dockHeld() && !Shell.dockOff()) Shell.dockRemote(key, o); }); return false; }
  if (!Nodes.get(k.node)) { if (typeof toast === 'function') toast('That node is not paired on this board', { kind: 'bad' }); return false; }
  const tile = Nodes.tailTile(k.node, k.tmux, { head: true, onClose: () => Shell.closeDock() });
  if (!tile) return false;
  Shell.dockUnmount(true);
  if (!D.resizer) D.resizer = Shell.buildDockResizer();
  host.textContent = '';
  host.append(D.resizer, tile.root);
  host.classList.add('has-term');
  host.classList.remove('hidden');
  D.pane = { root: tile.root, head: tile.head, update() {}, focus: () => tile.focus(), destroy: () => tile.destroy() };
  D.tmux = key;
  D.last = key;
  if (D.w) D.resizer.setAttribute('aria-valuenow', String(D.w));
  if (Shell.mq) Shell.applyMode();
  if (o && o.focus) tile.focus();
  return true;
};

/* DOM only (no mode pass): put a pane for `tmux` into #dock, replacing the one there. */
Shell.dockMount = function (tmux, o) {
  const D = Shell.dock;
  const host = $('#dock');
  if (!host) return false;
  if ((Ref.splitKey(tmux) || {}).node) return Shell.dockRemote(tmux, o);
  const typing = TermKit.typingTarget();                              // a composer that has the keyboard keeps it, however the dock was opened
  let pane = null;
  try {                                                               // built detached first: a name that cannot be shown leaves the dock as it was
    pane = TermKit.termPane(tmux, {
      mode: 'full',
      restoreFocus: typing,
      holdFocus: !o.focus || !!typing,                                // opened by a restore, a route or a key: nothing may land in the terminal unasked
      onClose: () => Shell.closeDock(),
      onPopOut: (t) => Shell.popOutDock(t),
      onAddToQuad: (t) => Shell.quadAdd(t),
    });
  } catch (e) {
    if (typeof toast === 'function') toast('Not a terminal session name', { kind: 'bad' });
    return false;
  }
  Shell.dockAddButton(pane, tmux);
  Shell.dockUnmount(true);
  if (!D.resizer) D.resizer = Shell.buildDockResizer();
  host.textContent = '';
  host.append(D.resizer, pane.root);
  host.classList.add('has-term');
  host.classList.remove('hidden');
  D.pane = pane;
  D.tmux = tmux;
  D.last = tmux;
  if (D.w) D.resizer.setAttribute('aria-valuenow', String(D.w));
  if (typeof state !== 'undefined' && state) Shell.patchDock(state);
  return true;
};

/* DOM only: take the pane down (the iframe goes to about:blank first, see termPane.destroy) and hide #dock. */
Shell.dockUnmount = function (keepHost) {
  const D = Shell.dock;
  if (D.pane) { try { D.pane.destroy(); } catch (e) { console.error('ccboard dock', e); } }
  D.pane = null;
  D.tmux = '';
  const host = $('#dock');
  if (!host) return;
  host.textContent = '';
  host.classList.remove('has-term');
  if (!keepHost) host.classList.add('hidden');
};

/* Show `tmux` in the dock. opts.focus: the person asked (a click on Open, mod+j): the terminal takes the keyboard unless a text field has it; without it (a restore) the
   terminal never takes focus by itself. opts.quiet: no toast when the dock is not available. true when the dock shows the session. */
Shell.openDock = function (tmux, opts) {
  const o = opts || {};
  const D = Shell.dock;
  if (typeof tmux !== 'string' || !tmux) return false;
  if (!Shell.dockOn() || Shell.dockHeld()) { if (!o.quiet) Shell.dockWhy(); return false; }          // the quad owns the window: no dock over it
  if (!$('#dock')) return false;
  if ((Ref.splitKey(tmux) || {}).node) return Shell.dockRemote(tmux, o);                              // another node's session: its tail tile, not a terminal
  if (!Shell.kitReady()) {                                                                            // the terminal kit loads now; the dock opens when it is here (true: it is on its way)
    Lazy.load('termkit').then(() => { if (Shell.kitReady() && !Shell.dockHeld() && !Shell.dockOff()) Shell.openDock(tmux, o); }, (e) => { if (!o.quiet) Lazy.fail('the terminal', e); });
    return true;
  }
  if (D.pane && D.tmux === tmux) {
    if (o.focus && !TermKit.typingTarget()) D.pane.focus();
    return true;
  }
  if (!Shell.dockMount(tmux, o)) return false;
  Shell.dockStore.set('ccboard:dock', tmux);
  if (Shell.mq) Shell.applyMode();
  if (o.focus && !TermKit.typingTarget()) D.pane.focus();
  return true;
};

/* The one call for "open this session's terminal": the dock when it is on (true), else its own page through openPage (false). For the keys and the palette. */


/* Close the dock and forget it (the saved session goes too, the full sidebar the person asked for goes back to the rule); mod+j brings back the one that was shown. */
Shell.closeDock = function () {
  const D = Shell.dock;
  Shell.dockUnmount(false);
  D.keep = false;
  D.wanted = '';
  Shell.dockStore.set('ccboard:dock', null);
  if (Shell.mq) Shell.applyMode();
  return true;
};

/* mod+j: close the dock when it is open; else open the last one shown, else the one that needs you most. */
Shell.toggleDock = function () {
  const D = Shell.dock;
  if (D.pane) { Shell.closeDock(); return true; }
  if (!Shell.dockOn() || Shell.dockHeld()) { Shell.dockWhy(); return false; }
  const t = D.last || Shell.dockPick(typeof state === 'undefined' ? null : state);
  if (!t) { if (typeof toast === 'function') toast('No live session to show', { kind: 'info' }); return false; }
  return Shell.openDock(t, { focus: true });
};

/* The window crossed 1024 px or the dock was switched off: the pane goes (it must not hold a hidden full-mode client) and comes back with the room. Runs inside applyMode. */
Shell.dockSync = function () {
  const D = Shell.dock;
  const on = Shell.dockOn();
  const held = Shell.dockHeld();
  if (D.pane && (!on || held)) { D.wanted = D.tmux; Shell.dockUnmount(false); }              // below 1024 px, switched off, or the quad page: the pane goes, ccboard:dock stays
  else if (!D.pane && D.wanted && on && !held) {
    if (!Shell.kitReady()) { Lazy.load('termkit').then(() => { if (Shell.kitReady() && Shell.mq) Shell.applyMode(); }, () => { /* the dock stays wanted; the next mode pass asks again */ }); return; }
    const t = D.wanted; D.wanted = ''; Shell.dockMount(t, {});
  }
};

/* ccboard:dock:off from outside (a setting, a test): true turns the dock off and closes it. */
Shell.setDockOff = function (off) {
  Shell.dockStore.set('ccboard:dock:off', off ? '1' : null);
  if (off && Shell.dock.pane) Shell.closeDock();
  else if (Shell.mq) Shell.applyMode();
};

/* Does the open dock hold the sidebar at the rail? (below 1440 px, unless the person asked for the full sidebar while it is open) */

/* First state after load: the session the dock showed last time comes back, when it is still alive. */
Shell.restoreDock = function (st) {
  const D = Shell.dock;
  const t = Shell.dockStore.get('ccboard:dock');
  if (!t) return false;
  const row = Shell.dockRow(st, t);
  if (!row || row.state === 'ended') { Shell.dockStore.set('ccboard:dock', null); return false; }
  if (Shell.dockHeld()) { D.wanted = Shell.dockOff() ? '' : t; return false; }                          // a load straight onto the quad: the dock comes back when it is left
  if (!Shell.dockOn()) { D.wanted = !Shell.dockOff() && !Shell.dockWide() ? t : ''; return false; }       // a narrow window keeps it for the width that fits
  return Shell.openDock(t, { quiet: true });
};

/* Pop out: the session in its own window (/term/), and the dock lets go of it: two full clients on one session would fight over its size. */
Shell.popOutDock = function (tmux) {
  const t = tmux || Shell.dock.tmux;
  if (!t) return false;
  const rk = Ref.splitKey(t);
  if (rk && rk.node) { if (typeof Nodes !== 'undefined' && typeof Nodes.openTerm === 'function') Nodes.openTerm(rk.node, rk.tmux); return true; }      // the tail tile stays; the terminal opens on its node
  const url = '/term/' + encodeURIComponent(t);
  if (typeof openPage === 'function') openPage(url); else window.open(url, '_blank', 'noopener');
  Shell.closeDock();
  return true;
};

/* The quad's saved slots (ccboard:quad:all = {layout, slots[], ...}), as far as they can be read: [] when there are none. */


/* Add a session to the quad view and go there; the dock lets go of the session (the quad shows it). The quad page owns its saved slots, so when pages/quad.js is
   loaded this is Quad.addToQuad(tmux, project) (the project scope of the quad that is up, else the board-wide one). Without it (a partial deploy) the session is handed
   over as ?s= instead: the saved slots plus this one (a full quad gives up its last slot), with l=4 once there are more than two; the address wins over what is
   saved and the page writes it back. */
Shell.quadAdd = function (tmux) {
  if (typeof tmux !== 'string' || !tmux) return false;
  if (typeof Quad !== 'undefined' && Quad && typeof Quad.addToQuad === 'function') {
    if (Shell.dock.pane && Shell.dock.tmux === tmux) Shell.closeDock();
    return !!Quad.addToQuad(tmux, Quad.current && Quad.current.project ? Quad.current.project : '');
  }
  const slots = Shell.quadSlots();
  if (!slots.includes(tmux)) { if (slots.length >= 4) slots.pop(); slots.push(tmux); }
  const query = { s: slots.join(',') };
  if (slots.length > 2) query.l = '4';
  if (Shell.dock.pane && Shell.dock.tmux === tmux) Shell.closeDock();
  Shell.go(Shell.hash('quad', {}, query));
  return true;
};

/* A click on a link to /term/<session>, from 1024 px up and with the dock on, opens the dock instead of a new tab. Left button, no modifier (those keep the browser's own
   behaviour), not held for LONG_PRESS ms (a long press navigates, as it always did), not the dock's own pop-out link. Without the dock the click is left alone.
   Registered on <body> in the capture phase, so it also runs before core.js's installed-app handler, which would navigate this window to /term/ instead. */




