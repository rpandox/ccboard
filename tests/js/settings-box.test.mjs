// Settings > Box > Host: the one-line note a WSL2 board adds ("host_note": "wsl2" in the health snapshot, issue #116 slice 6): its numbers describe the
// Linux VM, not Windows. Nothing is shown for a board without the note. Real core.js, components.js, router.js and pages/*.js on minidom's DOM.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { fakeState, homeWorld, page, text } from './world.mjs';

const HEALTH = { host: 'ubu', cpu_pct: 12.5, load1: 0.4, mem: { total: 1000, used: 500, pct: 50 }, disk: { total: 1000, used: 100, pct: 10 }, uptime_s: 90000, cores: 4, at: 1 };

function boxPanel(health) {
  const { w } = homeWorld({ state: fakeState({ health }) });
  w.location.hash = '#/settings?sec=box';
  return page(w).querySelector('.settings-panel[data-sec=box]');
}

test('a WSL2 board says its numbers are for the VM', () => {
  const p = boxPanel({ ...HEALTH, host_note: 'wsl2' });
  const note = p.querySelectorAll('.dim').find((n) => /WSL2/.test(text(n)));
  assert.ok(note, 'the note is there');
  assert.equal(text(note), 'WSL2: these numbers are for the Linux VM, not for Windows.');
});

test('a board without the note shows none', () => {
  const p = boxPanel(HEALTH);
  assert.equal(p.querySelectorAll('.dim').filter((n) => /WSL2/.test(text(n))).length, 0);
  assert.ok(p.querySelectorAll('.kv').length > 0, 'the host rows are still there');
});
