import assert from 'node:assert/strict';
import test from 'node:test';
import { inspectSettingsFrames, settingsGeometryPolicy, settingsFixturePolicy } from './settings-stability.mjs';
import { dynamicSequence } from './dynamic-matrix.mjs';

const frame = (y = 0, overflow = false) => ({ regions: [{ id: 'settings', rect: [0, y, 300, 200], overflow }], shifts: [], errors: [] });
const trace = id => [{ id, before: { frame: 1, nativeAt: 10 }, after: { frame: 2, nativeAt: 20 } }];
const inspect = (frames, transitions) => {
  let sequence = 0;
  const timed = frames.map((frame, index) => ({ ...frame, nativeAt: 5 + index * 10,
    shifts: frame.shifts.map(shift => ({ sequence: ++sequence, at: 15, deliveredAt: 19, ...shift })) }));
  return inspectSettingsFrames(timed, transitions, ['settings'], { rawShifts: timed.flatMap(frame => frame.shifts),
    nativeTimeline: { basis: 'pre-clock-performance/v1', clockVerified: true, completed: true, timeOrigin: 1, capturedAt: 0, endAt: 100 } });
};

test('every Settings observation declares a geometry policy and unknown states fail', () => {
  for (const id of dynamicSequence('settings', 390)) assert(settingsGeometryPolicy(id));
  assert.throws(() => settingsGeometryPolicy('invented:state'), /Unclassified/);
  assert.equal(settingsGeometryPolicy('fixture-api:history:running:running').geometry, 'content-transition');
  assert.match(settingsGeometryPolicy('fixture-api:history:running:running').reason, /API object identities/);
});

test('passive status and command feedback reject drift, removal and layout shifts', () => {
  for (const id of ['browser:paused:granted:granted:false', 'history:acknowledgement:running', 'activation:admission:blocked', 'export:failure:response', 'delete:error']) {
    assert.equal(settingsGeometryPolicy(id).geometry, 'stable-status');
    for (const changed of [frame(1), { ...frame(), regions: [] }, { ...frame(), shifts: [{ value: 0.01 }] }]) {
      assert(inspect([frame(), changed], trace(id)).failures.length);
    }
    assert.deepEqual(inspect([frame(), frame()], trace(id)).failures, []);
  }
});

test('form expansion may reflow but cannot hide persistent sections, overflow or empty evidence', () => {
  const steps = trace('redemption:success:open');
  assert.deepEqual(inspect([frame(), frame(40)], steps).failures, []);
  assert(inspect([frame(), frame(40, true)], steps).failures.length);
  assert(inspect([frame(), { ...frame(), regions: [] }], steps).failures.length);
  assert(inspect([{ ...frame(), errors: ['settings: missing on first surface frame'] }], []).failures.length);
  assert(inspect([], steps).failures.length);
  assert(inspect([frame()], []).failures.length);
});

test('baseline delivery never erases a shift whose occurrence belongs to a stable action', () => {
  const shifted = { ...frame(), shifts: [{ value: 0.01 }] };
  const steps = trace('history:acknowledgement:running');
  assert(inspect([shifted, frame()], steps).failures.some(value => value.includes('layout shift')));
  assert(inspect([frame(), shifted], steps).failures.some(value => value.includes('layout shift')));
});

test('auxiliary resets have closed policies and ordinary auxiliary actions stay strict', () => {
  assert.equal(settingsFixturePolicy('fixture:response-order-reset').geometry, 'content-transition');
  assert.equal(settingsFixturePolicy('fixture:refresh:vault.get').geometry, 'content-transition');
  for (const id of ['fixture:activation-code-input', 'fixture:pairing-codes-match', 'fixture:scroll-audit']) {
    assert.equal(settingsFixturePolicy(id).geometry, 'stable-status');
  }
  assert.throws(() => settingsFixturePolicy('fixture:unobserved-gap'), /Unclassified/);
  assert.throws(() => settingsFixturePolicy('fixture:refresh:invented'), /Unclassified/);
});
