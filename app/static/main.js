/* ccboard main: the boot sequence, loaded last by index.html. The only file that starts the state poll. */
'use strict';

/* html.pwa marks an installed window (standalone, minimal-ui, fullscreen or the window-controls overlay; iPad and iPhone report it as navigator.standalone):
   the CSS only styles the title-bar overlay and the app-like chrome under it, and never needs to sniff the user agent. */
function markStandalone() {
  const root = document.documentElement;
  if (!root || !root.classList) return false;
  const mq = (q) => { try { return !!(window.matchMedia && window.matchMedia(q).matches); } catch (_) { return false; } };
  let on = false;
  try { on = isStandalone(); } catch (_) { /* no navigator */ }
  on = on || mq('(display-mode: window-controls-overlay)') || mq('(display-mode: minimal-ui)') || mq('(display-mode: fullscreen)');
  root.classList.toggle('pwa', on);
  return on;
}

/* A page or snippet shared to the installed app (the manifest's share_target, GET /?title=&text=&url=) opens the palette's "send to session" list
   with the text in its box. Once the first state is in, so the list has sessions to offer. */
let shareOffered = { text: '', at: 0 };

function offerShare(text) {
  if (!text || typeof Palette === 'undefined') return;
  if (text === shareOffered.text && Date.now() - shareOffered.at < 3000) return;   // a fresh launch hands over the same share twice: the address and launchQueue
  shareOffered = { text, at: Date.now() };
  Palette.open({ mode: 'send', text });
}

function openSharedText() {
  if (typeof Palette === 'undefined' || typeof Palette.shareFromQuery !== 'function') return;
  offerShare(Palette.shareFromQuery(location.search));
}

/* The manifest says launch_handler focus-existing: with the app already open, a share or a shortcut does not navigate its window, the browser
   hands the URL to launchQueue instead (Chromium; elsewhere there is no launchQueue and the address carries it). */
function watchLaunches() {
  const q = typeof window !== 'undefined' ? window.launchQueue : null;
  if (!q || typeof q.setConsumer !== 'function') return;
  q.setConsumer((params) => {
    let u = null;
    try { u = new URL(params.targetURL); } catch (_) { return; }
    const text = typeof Palette !== 'undefined' && typeof Palette.shareFromQuery === 'function' ? Palette.shareFromQuery(u.search) : null;
    if (text) { offerShare(text); return; }
    if (/^#\//.test(u.hash) && u.hash !== location.hash && typeof navigate === 'function') navigate(u.hash);
  });
}

registerServiceWorker();
installLifecycleListeners();
markStandalone();
// The keyboard layer goes in before the shell: its listener then runs first, so a key it handles ('/' and '[' too) is already
// defaultPrevented when the shell's own fallback listener sees it, and nothing fires twice.
if (typeof Keymap !== 'undefined') Keymap.install();
if (typeof installShell === 'function') installShell();           // shell.js: topbar, sidebar, bottom nav, drawer, keys
// Without shell.js (a partial deploy, a test page) the poll still needs a render() and the legacy header hooks: never shadow the shell's own.
if (typeof render !== 'function') window.render = () => { updateCurrentPage(state); if (typeof renderBanner === 'function') renderBanner(); if (typeof updateModal === 'function') updateModal(); };
if (typeof renderHeader !== 'function') window.renderHeader = () => {};
if (typeof renderUsage !== 'function') window.renderUsage = () => {};
rewriteLegacyHash();                                              // #s=<tmux> (old ntfy links) becomes #/s/<tmux> in place
watchLaunches();
// The live grid lives on the Home page: only remember that it was on; pages/home.js starts it when it mounts.
try { if (localStorage.getItem('ccboard:live') === '1') live.on = true; } catch (_) { /* storage may be unavailable */ }
route();
startStatePolling().then(openSharedText, openSharedText);         // the poll's promise settles after the first state (or the offline fallback) is painted
