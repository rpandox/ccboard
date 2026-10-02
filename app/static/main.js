/* ccboard main: the boot sequence, loaded last by index.html. The only file that starts the state poll. */
'use strict';

registerServiceWorker();
installLifecycleListeners();
// v0.5.1 rewrites no hashes: the legacy #s=<tmux> deep link is still handled by applyDeepLink() in pages/home.js.
try { if (localStorage.getItem('ccboard:live') === '1') { live.on = true; liveStart(); } } catch (_) { /* storage may be unavailable */ }
startStatePolling();
