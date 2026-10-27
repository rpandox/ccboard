/* Shared rate-limit callout and widget state; charts/usage card load on use. */
'use strict';

const Widgets = {
  REFRESH_MS: 60000,                      // how often the usage card refetches while the tab is visible
  DISMISS_KEY: 'ccboard:limit:dismissed', // sessionStorage: the resets_at of the limit the person dismissed
  SPARK_W: 240, SPARK_H: 44,              // the sparkline's own coordinate space (the svg stretches to the card)
  BARS_W: 280, BARS_H: 56,
  limitNode: null,
};

/* ---------- small helpers ---------- */

Widgets.epoch = function (v) {
  if (typeof v === 'number' && Number.isFinite(v)) return v > 1e11 ? v / 1000 : v;
  if (typeof v === 'string' && v) { const n = Number(v); if (Number.isFinite(n)) return Widgets.epoch(n); const t = Date.parse(v); return Number.isNaN(t) ? 0 : t / 1000; }
  return 0;
};

/* The rate-limit record of a state payload as {session, message, kind, resets_at, at}, or null. */
Widgets.limit = function (st) {
  const r = st && st.rate_limited;
  if (!r || typeof r !== 'object') return null;
  const v = r.value && typeof r.value === 'object' ? r.value : r;
  if (!v.session && !v.message && !v.resets_at) return null;
  return { session: String(v.session || ''), message: String(v.message || ''), kind: String(v.kind || ''), resets_at: Widgets.epoch(v.resets_at), at: r.at || v.at || '' };
};

/* Local clock time of an epoch ('22:05'), with the weekday when it is more than a day away (a 5 h window that resets after midnight is still just a time). */
Widgets.clock = function (epoch) {
  if (!epoch) return '';
  const d = new Date(epoch * 1000);
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  if (Math.abs(epoch - Date.now() / 1000) < 20 * 3600) return hm;
  return `${['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][d.getDay()]} ${hm}`;
};

/* ---------- the rate-limit banner ---------- */

Widgets.dismissed = function () { try { return sessionStorage.getItem(Widgets.DISMISS_KEY) || ''; } catch (_) { return ''; } };

/* The same episode keeps its reset time to within a few minutes (parsed from a clock time in the message); a new window is hours away. */
Widgets.isDismissed = function (resetsAt) {
  const d = Number(Widgets.dismissed());
  return Number.isFinite(d) && d > 0 && Math.abs(d - resetsAt) < 600;
};

Widgets.dismiss = function (resetsAt) { try { sessionStorage.setItem(Widgets.DISMISS_KEY, String(resetsAt)); } catch (_) { /* storage may be unavailable */ } };

Widgets.limitText = function (lim) {
  const kind = lim.kind === '5h' || lim.kind === '7d' ? ` (${lim.kind})` : '';
  const who = lim.session ? lim.session.split('--').pop() : '';
  return `Claude rate limit${kind} · resets ${Widgets.clock(lim.resets_at)}${who ? ' · ' + who : ''}`;
};

Widgets.limitBanner = function (st) {
  if (typeof document === 'undefined') return null;
  const banner = document.getElementById('banner');
  if (!banner) return null;
  const lim = Widgets.limit(st);
  const active = !!lim && lim.resets_at > 0 && lim.resets_at * 1000 > Date.now() && !Widgets.isDismissed(lim.resets_at);
  let node = Widgets.limitNode;
  if (!active) {
    if (node) node.remove();
    Widgets.limitNode = null;
    banner.classList.remove('limit-only');
    return null;
  }
  const text = Widgets.limitText(lim);
  if (!node || node.parentNode !== banner) {                    // first time, or renderBanner() emptied the banner
    if (node) node.remove();
    const label = el('span', { class: 'lc-text', text });
    node = el('div', { class: 'callout warn limit-callout', role: 'status' }, label,
      el('button', { class: 'icon minimal small', type: 'button', 'aria-label': 'Dismiss', title: 'Dismiss', onclick: () => {
        Widgets.dismiss(node._resets);
        node.remove();
        Widgets.limitNode = null;
        banner.classList.remove('limit-only');
      } }, ic('cross')));
    node._label = label;
    banner.append(node);
    Widgets.limitNode = node;
  } else setTextIfChanged(node._label, text);
  node._resets = lim.resets_at;
  banner.classList.toggle('limit-only', banner.children.length === 1);   // alone in the banner: no second frame around the callout
  return node;
};
