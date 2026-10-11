import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { setupProgress } from '../ui/setup-progress.mjs';
const state = (view, extra = {}) => setupProgress({ view, full: true, connected: false,
  journey: { id: 'desktop_app_needed', title: 'Connect the desktop app' }, ...extra });
test('permission task and review destination agree; mode choice is not repeated', () => {
  const rail = state('access');
  assert.equal(rail.summary, 'Allow site access');
  assert.equal(rail.steps[1].label, 'Site access'); assert.equal(rail.steps[1].review, 'access');
  assert.equal(rail.steps[1].current, true); assert.equal(rail.steps[0].complete, true);
  assert.equal(rail.steps[2].locked, true);
  assert.equal(state('journey').steps[1].review, 'access');
  assert.equal(state('mode').steps[1].review, 'mode');
});
test('completed permissions, connected Full and Preview use accurate rails', () => {
  assert.equal(state('journey', { connected: true }).steps[3].current, true);
  assert.equal(state('start').visible, false);
  assert.equal(state('journey', { full: false }).visible, false);
  assert.equal(state('journey', { journey: { id: 'full_ready' } }).visible, false);
});
test('one receiving shortcut follows primary start choices and preserves the receiving form', () => {
  const html = readFileSync(new URL('../setup.html', import.meta.url), 'utf8');
  assert.equal((html.match(/id="setup-transfer-open"/g) ?? []).length, 1);
  assert.ok(html.indexOf('id="setup-transfer-open"') > html.indexOf('id="start-full"'));
  assert.equal((html.match(/id="setup-transfer-entry"/g) ?? []).length, 1);
  assert.match(html, /id="pause"[^>]*>Pause<\/button>/);
});
