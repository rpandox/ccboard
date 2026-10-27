// Browser-shaped network: execute every appended classic script, record styles,
// and answer demo fetches from disk. No declared bundle closure or stubbed api().
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { makeWorld, STATIC } from './harness.mjs';
import { installDom } from './minidom.mjs';

export async function firstView() {
  const requests = [], errors = [];
  const timers = new Set();
  const w = makeWorld({ companions: false, primitives: false,
    requestAnimationFrame: fn => setImmediate(fn),
    setTimeout: (fn, ms) => { const t = setTimeout(fn, ms); t.unref(); timers.add(t); return t; },
    setInterval: (fn, ms) => { const t = setInterval(fn, ms); t.unref(); timers.add(t); return t; },
    fetch: async url => {
      const file = new URL(url, 'http://board').pathname;
      requests.push(file);
      if (!file.startsWith('/static/demo/')) throw new Error('Unexpected API request ' + url);
      return { ok: true, json: async () => JSON.parse(fs.readFileSync(path.join(STATIC, file.slice(8)), 'utf8')) };
    },
    console: { ...console, error: (...a) => errors.push(a.map(String).join(' ')) },
  });
  const dom = installDom(w);
  w.location.search = '?demo=1';
  w.setHash('#/', { silent: true });
  w.document.documentElement.setAttribute('data-release', 'test-release');
  const head = new dom.El('head'); w.document.head = head;
  w.document.documentElement.insertBefore(head, w.document.body);
  let pending = 0;
  const insert = head.appendChild.bind(head);
  const before = head.insertBefore.bind(head);
  const load = n => {
    if (!['SCRIPT', 'LINK'].includes(n.tagName)) return;
    const url = n.src || n.getAttribute('src') || n.getAttribute('href');
    if (!url) return;
    const file = new URL(url, 'http://board').pathname;
    requests.push(file); pending++;
    setImmediate(() => {
      try {
        if (n.tagName === 'SCRIPT') {
          if (file === '/static/boot.js') w.run(fs.readFileSync(path.join(STATIC, 'boot.js'), 'utf8').replaceAll('__ASSET_VERSION__', 'test-release'));
          else w.load(file.slice(8));
        }
      } catch (e) { errors.push(String(e)); }
      pending--; n.onload?.();
    });
  };
  head.appendChild = n => { const r = insert(n); load(n); return r; };
  head.insertBefore = (n, ref) => { const r = before(n, ref); load(n); return r; };
  // Read actual index tags, including any future startup dependency.
  const html = fs.readFileSync(path.join(STATIC, 'index.html'), 'utf8');
  for (const m of html.matchAll(/<(script|link)\b([^>]+)>/g)) {
    const attrs = Object.fromEntries([...m[2].matchAll(/([\w-]+)="([^"]*)"/g)].map(a => [a[1], a[2]]));
    if (m[1] === 'link' && attrs.rel !== 'stylesheet') continue;
    const n = w.document.createElement(m[1]);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    head.appendChild(n);
  }
  let stable = 0;
  for (let i = 0; i < 2000 && stable < 20; i++) {
    await new Promise(r => setImmediate(r));
    const ready = w.run("typeof mountedId !== 'undefined' && mountedId === 'home' && typeof Widgets.usageCard === 'function'");
    stable = ready && !pending ? stable + 1 : 0;
  }
  for (const t of timers) clearTimeout(t);
  if (stable < 20 || errors.length) throw new Error('First view incomplete: ' + errors.join('\n'));
  w.run('takeDownPage()');
  const files = [...new Set(requests.filter(f => /\.(js|css)$/.test(f)))].sort();
  return { files, requests, total: files.reduce((n, f) => {
    const body = fs.readFileSync(path.join(STATIC, f.slice(8)));
    // boot is a server template: count the bytes served with a 12-character hash.
    return n + (f === '/static/boot.js' ? Buffer.byteLength(body.toString().replaceAll('__ASSET_VERSION__', 'test-release')) : body.length);
  }, 0) };
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) console.log(JSON.stringify(await firstView()));
