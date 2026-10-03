/* A stand-in for ttyd's page (1.7.7) for the terminal page's dev checks: no tmux, no ttyd, no network.
   It exposes what TermKit.bind reads from the iframe: window.term with fit() and options.fontSize, an .xterm > .xterm-screen to put the
   touch-to-wheel shim on, and a .xterm-helper-textarea. Counters for scripts/qa_terminal.sh and by-hand checks:
     window.__fits        times term.fit() ran (a font size change or a layout refit)
     window.__wheel       wheel events that reached .xterm (a real wheel or the shim's synthesised ones)
     window.__lastDelta   deltaY of the last one (negative = scroll back in time)
   Everything visual goes through the CSSOM, so the page works under any Content-Security-Policy. */
(function () {
  'use strict';
  var BANNER = ['ccboard fake tty (dev harness, not ttyd)', 'wheel events are counted in window.__wheel', ''];

  window.term = {
    fit: function () { window.__fits = (window.__fits || 0) + 1; paint(); },
    options: { fontSize: 13 },
    cols: 80,
    rows: 24,
  };
  window.__fits = 0;
  window.__wheel = 0;
  window.__lastDelta = 0;

  var root = document.documentElement;
  root.style.height = '100%';
  var bs = document.body.style;
  bs.margin = '0';
  bs.height = '100%';
  bs.background = '#000';
  bs.color = '#9fe8a5';
  bs.overflow = 'hidden';
  bs.fontFamily = 'ui-monospace, Menlo, Consolas, monospace';

  var xterm = document.createElement('div');
  xterm.className = 'terminal xterm';
  var xs = xterm.style;
  xs.position = 'absolute';
  xs.left = xs.top = xs.right = xs.bottom = '0';
  xs.overflow = 'hidden';

  var viewport = document.createElement('div');
  viewport.className = 'xterm-viewport';
  viewport.style.position = 'absolute';
  viewport.style.left = viewport.style.top = viewport.style.right = viewport.style.bottom = '0';
  viewport.style.overflowY = 'scroll';

  var screen = document.createElement('div');
  screen.className = 'xterm-screen';
  var ss = screen.style;
  ss.position = 'absolute';
  ss.left = ss.top = ss.right = ss.bottom = '0';
  ss.overflow = 'hidden';
  ss.whiteSpace = 'pre';
  ss.padding = '4px 6px';
  ss.boxSizing = 'border-box';

  var helper = document.createElement('textarea');
  helper.className = 'xterm-helper-textarea';
  helper.setAttribute('aria-label', 'Terminal input');
  helper.setAttribute('autocapitalize', 'off');
  helper.setAttribute('autocorrect', 'off');
  var hs = helper.style;
  hs.position = 'absolute';
  hs.left = '-9999px';
  hs.top = '0';
  hs.width = '1px';
  hs.height = '1px';
  hs.opacity = '0';

  xterm.append(viewport, screen, helper);
  document.body.append(xterm);

  var lines = BANNER.slice();
  for (var i = 1; i <= 120; i++) lines.push('user@box:~/shop/api$ echo line ' + i + ' of the fake scrollback');
  var offset = 0;

  function paint() {
    ss.fontSize = window.term.options.fontSize + 'px';
    ss.lineHeight = '1.25';
    var rows = Math.max(1, Math.floor(screen.clientHeight / (window.term.options.fontSize * 1.25)));
    var first = Math.min(Math.max(0, lines.length - rows - offset), lines.length - 1);
    screen.textContent = lines.slice(first, first + rows).join('\n');
  }

  /* xterm listens for wheel on .xterm (it bubbles from .xterm-screen, from the canvas in the real thing) */
  xterm.addEventListener('wheel', function (e) {
    window.__wheel += 1;
    window.__lastDelta = e.deltaY;
    offset = Math.min(lines.length - 1, Math.max(0, offset - Math.sign(e.deltaY)));
    paint();
    e.preventDefault();
  }, { passive: false });
  xterm.addEventListener('click', function () { helper.focus(); });
  window.addEventListener('resize', paint);
  paint();
})();
