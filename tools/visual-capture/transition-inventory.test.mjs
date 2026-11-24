import assert from 'node:assert/strict';
import test from 'node:test';
import { SCREENS } from './capture.mjs';
import * as freshness from './freshness-transitions.mjs';
import * as dynamic from './dynamic-transitions.mjs';
import { staticFixtures } from './static-fixtures.mjs';
import { runInNewContext } from 'node:vm';
import { installSurfaceFixture } from '../../extension/qualification/surface-runtime-fixture.mjs';
import { SURFACE_STATES } from '../../extension/qualification/surface-states.mjs';
import { READINESS_REQUEST, READINESS_RESPONSE } from '../../extension/runtime/surface-readiness.mjs';

test('the route inventory includes every graph projection state', () => {
  for (const state of ['current', 'pending', 'unavailable', 'degraded']) assert(SCREENS.some((screen) => screen.workspace === 'graph' && screen.state === state));
});
test('freshness registers every directed state transition', () => {
  const matrix = freshness.FRESHNESS_TRANSITIONS;
  assert(Array.isArray(matrix), 'No dynamic transition registry');
  const states = new Set(matrix.flatMap(({ from, to }) => [from.id, to.id]));
  for (const from of states) for (const to of states) if (from !== to) assert(matrix.some((edge) => edge.from.id === from && edge.to.id === to));
});
test('each current browser setup fixture retains its production renderer and input state', async () => {
  const fixtures = (await staticFixtures()).filter((fixture) => fixture.surface === 'setup');
  const expected = Object.entries(SURFACE_STATES).filter(([, state]) => state.surface === 'setup');
  assert.deepEqual(fixtures.map(fixture => fixture.name), expected.map(([name]) => name));
  for (const fixture of fixtures) {
    assert(fixture.script.length > 0, `${fixture.name} has no production renderer`);
    assert.equal(fixture.state.surface, 'setup');
  }
  for (const name of ['runtime_unavailable', 'reauthorization', 'pairing_compare', 'setup_complete']) {
    assert(fixtures.some(fixture => fixture.name === name), `Missing native journey handoff state: ${name}`);
  }
});
test('serialized capture fixtures publish storage changes and return the requested readiness identity', async () => {
  const window = { fetch() {} };
  runInNewContext(`(${installSurfaceFixture})({ surface: 'popup', mode: 'full', ready: true, paired: true })`,
    { window, structuredClone, queueMicrotask, location: { origin: 'http://fixture.localhost' } });
  const calls = [];
  window.chrome.storage.onChanged.addListener((changes, area) => calls.push({ changes, area }));
  const port = window.chrome.runtime.connect();
  const replies = [];
  port.onMessage.addListener(message => { if (message.type === READINESS_RESPONSE) replies.push({ ...message }); });
  const requestId = '33333333-3333-4333-8333-333333333333';
  port.postMessage({ type: READINESS_REQUEST, request_id: requestId });
  await new Promise(queueMicrotask);
  assert.deepEqual(replies, [{ type: READINESS_RESPONSE, request_id: requestId,
    commercial_authority: 'active', analysis_admission: 'admitted' }]);
  window.__surfaceFixture.change({ mode: 'off' });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].area, 'local');
});

test('every dynamic view requires its reservations on the first surface frame', () => {
  assert.deepEqual(dynamic.DYNAMIC_VIEWS, ['home', 'analytics', 'inbox', 'settings', 'passkey', 'graph', 'popup', 'setup', 'options', 'production-boot']);
  for (const view of dynamic.DYNAMIC_VIEWS) assert(dynamic.REQUIRED_REGIONS?.[view]?.length, `${view} has no reservation inventory`);
});

test('dynamic inventory rejects a skipped trace and an unknown view', () => {
  assert.equal(typeof dynamic.validateInventory, 'function');
  assert.throws(() => dynamic.validateInventory([]), /missing/i);
  assert.throws(() => dynamic.validateInventory([{ view: 'unknown' }]), /unknown/i);
});

test('dynamic inventory rejects a registered view with missing states', () => {
  assert.throws(() => dynamic.validateInventory([{ view: 'setup', transitions: 1, states: ['unknown:phase'] }], ['setup']), /missing.*state/i);
});

test('directed trace records its declared source before the destination', async () => {
  let current = 'unrelated';
  const edge = { from: { id: 'source' }, to: { id: 'destination' } };
  const record = await freshness.captureDirectedTransition(edge, async (state) => { current = state.id; }, async () => ({ state: current }));
  assert.equal(record.before.state, edge.from.id);
  assert.equal(record.after.state, edge.to.id);
});

test('dynamic inventory rejects a skipped transition outside the minimum states', () => {
  const states = [...dynamic.REQUIRED_STATES.setup];
  assert.throws(() => dynamic.validateInventory([{ view: 'setup', width: 390, transitions: 55, states }], ['setup']), /transition count/i);
});
