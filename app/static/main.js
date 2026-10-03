/* ccboard main: the boot sequence, loaded last by index.html. The only file that starts the state poll. */
'use strict';

registerServiceWorker();
installLifecycleListeners();
if (typeof installShell === 'function') installShell();           // shell.js: topbar, sidebar, bottom nav, drawer, keys
// Without shell.js (a partial deploy, a test page) the poll still needs a render() and the legacy header hooks: never shadow the shell's own.
if (typeof render !== 'function') window.render = () => { updateCurrentPage(state); if (typeof renderBanner === 'function') renderBanner(); if (typeof updateModal === 'function') updateModal(); };
if (typeof renderHeader !== 'function') window.renderHeader = () => {};
if (typeof renderUsage !== 'function') window.renderUsage = () => {};
rewriteLegacyHash();                                              // #s=<tmux> (old ntfy links) becomes #/s/<tmux> in place
// The live grid lives on the Home page: only remember that it was on; pages/home.js starts it when it mounts.
try { if (localStorage.getItem('ccboard:live') === '1') live.on = true; } catch (_) { /* storage may be unavailable */ }
route();
startStatePolling();
