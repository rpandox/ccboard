/* Two frames paint the skeleton before versioned app assets are requested. */
'use strict';
const BOOT_RELEASE = '__ASSET_VERSION__';

function bootBoard() {
  if (typeof Lazy !== 'undefined') { Lazy.boot().catch(bootFailed); return; }
  const loader = document.createElement('script');
  loader.src = '/static/lazy.js?v=' + encodeURIComponent(BOOT_RELEASE);
  loader.defer = true;
  loader.async = false;
  loader.onload = () => Lazy.boot().catch(bootFailed);
  loader.onerror = bootFailed;
  document.head.appendChild(loader);
}

function bootFailed() {
  const root = document.querySelector('#page');
  if (!root) return;
  root.textContent = '';
  const message = document.createElement('p');
  message.textContent = 'This page could not be loaded. The connection dropped or the board is restarting. Try again in a moment.';
  const retry = document.createElement('button');
  retry.type = 'button';
  retry.textContent = 'Retry';
  retry.addEventListener('click', bootBoard);
  root.append(message, retry);
}

requestAnimationFrame(() => requestAnimationFrame(bootBoard));
