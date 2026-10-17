/* The fragment never goes to the server as a URL, and is removed before redemption. */
'use strict';
(async () => {
  let token = window.location.hash.slice(1);
  window.history.replaceState(null, '', '/auth/local');
  const status = document.getElementById('local-status');
  try {
    if (!token) throw new Error('missing link');
    const response = await fetch('/auth/local/redeem', {
      method: 'POST', headers: { 'X-CCBoard': '1', 'Content-Type': 'text/plain' },
      body: token, credentials: 'same-origin', cache: 'no-store',
    });
    token = '';
    if (!response.ok) throw new Error('refused');
    window.location.replace('/');
  } catch (_) {
    token = '';
    status.textContent = 'This link expired or was refused. Run bin/ccboard-open on this machine for a new link.';
  }
})();
