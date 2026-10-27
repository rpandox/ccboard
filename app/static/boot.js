/* First paint (#333): only skeleton.css precedes this small deferred script.
   Two animation frames give the browser a rendering opportunity before any app
   script is requested. CSS completes before main.js can build a control. */
'use strict';

function bootBoard() {
  const loader = document.createElement('script');
  loader.src = '/static/lazy.js';
  loader.defer = true;
  loader.async = false;
  loader.onload = () => Lazy.load('app').then(() => Lazy.load('main')).catch(bootFailed);
  loader.onerror = bootFailed;
  document.head.appendChild(loader);
}

function bootFailed() {
  const root = document.querySelector('#page');
  if (!root) return;
  root.textContent = '';
  const message = document.createElement('p');
  message.textContent = 'Could not load ccboard. Check the connection and reload.';
  const retry = document.createElement('button');
  retry.type = 'button';
  retry.textContent = 'Reload';
  retry.addEventListener('click', () => location.reload());
  root.append(message, retry);
}

requestAnimationFrame(() => requestAnimationFrame(bootBoard));
