/* A stand-in for ttyd's page (1.7.7) for the terminal page's dev checks: no tmux, no ttyd, no network.
   It exposes what TermKit.bind reads from the iframe: window.term with fit() and options.fontSize, an .xterm > .xterm-screen to put the
   touch-to-wheel shim on, and a .xterm-helper-textarea. Counters for scripts/qa_terminal.sh and by-hand checks:
     window.__fits        times term.fit() ran (a font size change or a layout refit)
     window.__wheel       wheel events that reached .xterm (a real wheel or the shim's synthesised ones)
     window.__lastDelta   deltaY of the last one (negative = scroll back in time)
   For the font spike (TermKit.bind with ccboard:term:font=1) it also has what ttyd's page has: window.FontFace, document.fonts and
   term.options.fontFamily (applied to the text, measured into term.cols and term.rows by fit()). They are recorded in
     window.__font        {mode, made, loaded, failed, added, families[]}: FontFace objects built / loaded / rejected / added to document.fonts
     window.__fitsAtFamily  the value of __fits when term.options.fontFamily was last assigned (a fit() after it makes __fits larger)
   Which FontFace the page sees follows localStorage['ccboard:fake:font'] on the board's origin (the iframe is same-origin):
     (unset)  the browser's own FontFace, wrapped to count (a real download of the vendored woff2); a stand-in when the browser has none
     stub     a stand-in that loads after 20 ms, no network     fail   a stand-in whose load() rejects
     hang     a stand-in that never settles                     missing  no window.FontFace and no document.fonts at all
   For the soft keyboard (scripts/qa_terminal.sh KBD) it can pretend to be one: ?vv=<height> on the terminal PAGE's URL (the page that embeds this
   iframe), or localStorage['ccboard:fake:vv'] = '<height>' on the board's origin, shrinks the page's visualViewport to that many px (height, and
   offsetTop 0) and fires `resize` on it, which is what a phone does when the keyboard opens; window.__vv records it. term.js reads the visual
   viewport on that event, so body.kbd, --vvh and the key bar follow. It runs when the iframe loads, which is after term.js has registered its
   listeners; nothing in the board's own HTML is touched (a dev-only shim in term.html would ship).
   Everything visual goes through the CSSOM, so the page works under any Content-Security-Policy. */
(function () {
  'use strict';
  var BANNER = ['ccboard fake tty (dev harness, not ttyd)', 'wheel events are counted in window.__wheel', ''];
  var TTYD_FAMILY = 'Consolas,Liberation Mono,Menlo,Courier,monospace';       // ttyd's own default fontFamily

  window.term = {
    fit: function () { window.__fits = (window.__fits || 0) + 1; measure(); paint(); },
    options: { fontSize: 13 },
    cols: 80,
    rows: 24,
  };
  window.__fits = 0;
  window.__fitsAtFamily = -1;
  window.__wheel = 0;
  window.__lastDelta = 0;

  var family = TTYD_FAMILY;
  Object.defineProperty(window.term.options, 'fontFamily', {
    enumerable: true,
    get: function () { return family; },
    set: function (v) { family = String(v); window.__fitsAtFamily = window.__fits; },
  });

  /* ---- soft-keyboard simulation (see the header): pin the real visualViewport of the embedding page, then tell its listeners */
  (function softKeyboard() {
    var h = 0;
    var par = null;
    try { par = window.parent !== window ? window.parent : null; } catch (_) { par = null; }
    try { var m = par ? /[?&]vv=(\d+)/.exec(par.location.search) : null; if (m) h = parseInt(m[1], 10) || 0; } catch (_) { h = 0; }
    if (!(h > 0)) { try { h = parseInt(window.localStorage.getItem('ccboard:fake:vv') || '0', 10) || 0; } catch (_) { h = 0; } }
    if (!(h > 0) || !par || !par.visualViewport) return;
    var vv = par.visualViewport;
    try {
      Object.defineProperty(vv, 'height', { configurable: true, get: function () { return h; } });          // own accessors shadow the prototype's
      Object.defineProperty(vv, 'offsetTop', { configurable: true, get: function () { return 0; } });
      window.__vv = { height: h, offsetTop: 0 };
      vv.dispatchEvent(new par.Event('resize'));
    } catch (_) { /* a browser that will not take the shim: no simulation */ }
  })();

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

  /* ---- fonts: what the spike needs from ttyd's page ---- */
  var fontMode = '';
  try { fontMode = window.localStorage.getItem('ccboard:fake:font') || ''; } catch (_) { fontMode = ''; }
  if (['', 'stub', 'fail', 'hang', 'missing'].indexOf(fontMode) < 0) fontMode = 'stub';        // an unknown value is a stand-in too
  var rec = window.__font = { mode: fontMode, made: 0, loaded: 0, failed: 0, added: 0, families: [] };

  function standIn() {
    function Face(fam, source, descriptors) {
      this.family = fam;
      this.source = source;
      this.descriptors = descriptors || {};
      this.status = 'unloaded';
      rec.made += 1;
      rec.families.push(fam);
    }
    Face.prototype.load = function () {
      var self = this;
      self.status = 'loading';
      return new Promise(function (resolve, reject) {
        if (fontMode === 'hang') return;
        setTimeout(function () {
          if (fontMode === 'fail') { self.status = 'error'; rec.failed += 1; reject(new Error('fake font load failed')); return; }
          self.status = 'loaded';
          rec.loaded += 1;
          resolve(self);
        }, 20);
      });
    };
    return Face;
  }

  function standInSet() {
    var faces = [];
    return {
      add: function (f) { faces.push(f); rec.added += 1; return this; },
      check: function () { return faces.length > 0; },
      ready: Promise.resolve(),
    };
  }

  function installFonts() {
    if (fontMode === 'missing') {
      try { window.FontFace = undefined; } catch (_) { /* read-only: leave it */ }
      try { Object.defineProperty(document, 'fonts', { value: undefined, configurable: true }); } catch (_) { /* ignore */ }
      return;
    }
    var Native = window.FontFace;
    var natural = fontMode === '' && typeof Native === 'function' && document.fonts && typeof document.fonts.add === 'function';
    if (!natural) {
      window.FontFace = standIn();
      try { Object.defineProperty(document, 'fonts', { value: standInSet(), configurable: true }); } catch (_) { /* ignore */ }
      return;
    }
    class Counted extends Native {
      constructor(fam, source, descriptors) { super(fam, source, descriptors); rec.made += 1; rec.families.push(fam); }
      load() {
        var p = super.load();
        p.then(function () { rec.loaded += 1; }, function () { rec.failed += 1; });
        return p;
      }
    }
    window.FontFace = Counted;
    var set = document.fonts;
    var add = set.add;
    set.add = function (f) { rec.added += 1; return add.call(set, f); };
  }
  installFonts();

  /* one cell's width in px for the current family and size: measured on a canvas when there is one (the real glyphs), else 0.6 em for
     a family that names JetBrains Mono and 0.55 em for anything else */
  function cellWidth() {
    var size = window.term.options.fontSize;
    try {
      var ctx = document.createElement('canvas').getContext('2d');
      ctx.font = size + 'px ' + family;
      var w = ctx.measureText('M').width;
      if (w > 0) return w;
    } catch (_) { /* no canvas: estimate */ }
    return size * (family.indexOf('JetBrains Mono') >= 0 ? 0.6 : 0.55);
  }

  function measure() {
    var size = window.term.options.fontSize;
    window.term.cols = Math.max(1, Math.floor(((screen.clientWidth || 600) - 12) / cellWidth()));
    window.term.rows = Math.max(1, Math.floor((screen.clientHeight || 600) / (size * 1.25)));
  }

  var lines = BANNER.slice();
  for (var i = 1; i <= 120; i++) lines.push('user@box:~/shop/api$ echo line ' + i + ' of the fake scrollback');
  var offset = 0;

  function paint() {
    ss.fontSize = window.term.options.fontSize + 'px';
    ss.fontFamily = family;
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
