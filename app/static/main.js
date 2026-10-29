/* ccboard boot: loaded last, starts the state poll. */
'use strict';

/* Mark installed windows for the app shell CSS. */
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

/* palette.js is a lazy bundle (lazy.js): only a share needs it at boot, and only a share loads it. */
function sharePossible(search) { return /[?&](text|url|share)=/.test(String(search || '')); }     // a bare title counts only with share=1

function withPalette(fn) {
  if (typeof Palette !== 'undefined') return fn();
  if (typeof Lazy !== 'undefined') Lazy.run('palette', fn, 'the command palette');
  return undefined;
}

/* Offer shared text after the first state supplies session choices. */
let shareOffered = { text: '', at: 0 };

function offerShare(text) {
  if (!text) return;
  withPalette(() => {
    if (typeof Palette === 'undefined') return;
    if (text === shareOffered.text && Date.now() - shareOffered.at < 3000) return;   // a fresh launch hands over the same share twice: the address and launchQueue
    shareOffered = { text, at: Date.now() };
    Palette.open({ mode: 'send', text });
  });
}

function openSharedText() {
  if (typeof Palette === 'undefined' && !sharePossible(location.search)) return;
  withPalette(() => {
    if (typeof Palette === 'undefined' || typeof Palette.shareFromQuery !== 'function') return;
    offerShare(Palette.shareFromQuery(location.search));
  });
}

/* Handle share/shortcut URLs delivered to an already-open installed window. */
function watchLaunches() {
  const q = typeof window !== 'undefined' ? window.launchQueue : null;
  if (!q || typeof q.setConsumer !== 'function') return;
  q.setConsumer((params) => {
    let u = null;
    try { u = new URL(params.targetURL); } catch (_) { return; }
    const go = () => { if (/^#\//.test(u.hash) && u.hash !== location.hash && typeof navigate === 'function') navigate(u.hash); };
    if (typeof Palette === 'undefined' && !sharePossible(u.search)) { go(); return; }
    withPalette(() => {
      const text = typeof Palette !== 'undefined' && typeof Palette.shareFromQuery === 'function' ? Palette.shareFromQuery(u.search) : null;
      if (text) offerShare(text); else go();
    });
  });
}

// Precache every asset after the first route and state have painted; never compete with their requests.
function registerWorkerAfterPaint() {
  if (!('serviceWorker' in navigator)) return;
  if (!mountedId) { setTimeout(registerWorkerAfterPaint, 250); return; }
  requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(registerServiceWorker, 1500)));
}
installLifecycleListeners();
markStandalone();
// The keyboard layer goes in before the shell: its listener then runs first, so a key it handles ('/' and '[' too) is already
// defaultPrevented when the shell's own fallback listener sees it, and nothing fires twice.
if (typeof Keymap !== 'undefined') Keymap.install();
else if (typeof Lazy !== 'undefined') Lazy.watchKeys();
if (typeof Lazy !== 'undefined') Lazy.watchLinks();                // a pointer over a link to a lazy page starts loading it (lazy.js)
if (typeof installShell === 'function') installShell();           // shell.js: topbar, sidebar, bottom nav, drawer, keys
// Without shell.js (a partial deploy, a test page) the poll still needs a render() and the legacy header hooks: never shadow the shell's own.
if (typeof render !== 'function') window.render = () => { updateCurrentPage(state); if (typeof renderBanner === 'function') renderBanner(); };
if (typeof renderHeader !== 'function') window.renderHeader = () => {};
if (typeof renderUsage !== 'function') window.renderUsage = () => {};
rewriteLegacyHash();                                              // #s=<tmux> (old ntfy links) becomes #/s/<tmux> in place
watchLaunches();
// Fetch the route while the first state arrives.
if (typeof Lazy !== 'undefined') Lazy.warm(parseHash(location.hash).id);
// Narrow Usage fetches its chart data while shared state is in flight. Keep
// desktop's state barrier so its account columns do not shift after mounting.
if (parseHash(location.hash).id === 'usage' && !matchMedia('(min-width: 840px)').matches) route();
const firstStateReady = () => { route(); openSharedText(); registerWorkerAfterPaint(); };
startStatePolling().then(firstStateReady, firstStateReady);         // the poll's promise settles after the first state (or the offline fallback) is painted
