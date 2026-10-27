import assert from 'node:assert/strict';
import { test } from 'node:test';
import { firstView } from './first-view.mjs';

test('first view records bootstrap, real API demo prerequisites and Home widgets, including default enabled features', async () => {
  const view = await firstView();
  for (const file of ['boot.js', 'lazy.js', 'refs.js', 'core.js', 'main.js', 'demo-api.js', 'bare-play.js', 'pages/home.js', 'pages/widgets.js', 'pages/widgets.css', 'nodes-hub.js', 'team.js']) {
    assert.ok(view.files.includes('/static/' + file), file + ' must be paid for');
  }
  assert.ok(view.requests.includes('/static/demo/state.json'), 'uses the actual core.api/demo fetch path');
  assert.ok(view.requests.includes('/static/demo/usage_summary.json'), 'the widget was mounted');
  assert.equal(view.requests.filter(u => u === '/static/refs.js').length, 1, 'no eager fixture globals hide script bytes');
});
