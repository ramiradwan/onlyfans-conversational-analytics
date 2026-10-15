import assert from 'node:assert/strict';
import test from 'node:test';
import { inspectSettingsFrames, settingsGeometryPolicy, settingsFixturePolicy, ordinarySettingsTransition, settingsFailureSummary } from './settings-stability.mjs';
import { dynamicSequence } from './dynamic-matrix.mjs';

const frame = (y = 0, overflow = false) => ({ regions: [{ id: 'settings', rect: [0, y, 300, 200], overflow }], shifts: [], errors: [] });

test('CI stage diagnostics partition failures without exposing browser data or invented IDs', () => {
  const privateValue = 'PRIVATE-CANARY-cookie=abc@example.test';
  const errors = [
    `history:acknowledgement:running: ${privateValue}: moved or resized`,
    `history:acknowledgement:running: ${privateValue}: removed`,
    'history:acknowledgement:running: layout shift after first paint',
    `fixture:scroll-audit: ${privateValue}: overflow`,
    `${privateValue}: moved or resized`,
    `invented-stage: ${privateValue}: layout shift after first paint`, null,
  ];
  const summary = settingsFailureSummary(errors);
  assert.deepEqual(summary.stages.map(stage => stage.id), ['history:acknowledgement:running', 'fixture:scroll-audit']);
  assert.equal(summary.stages[0].counts.geometry, 2);
  assert.equal(summary.stages[0].counts.shift, 1);
  assert.equal(summary.stages[1].counts.overflow, 1);
  assert.equal(summary.unassigned.geometry, 1);
  assert.equal(summary.unassigned.shift, 1);
  assert.equal(summary.unassigned.other, 1);
  assert.equal(JSON.stringify(summary).includes(privateValue), false);
  assert.equal(JSON.stringify(summary).includes('invented-stage'), false);
  const total = [...summary.stages.map(stage => stage.counts), summary.unassigned]
    .flatMap(counts => Object.values(counts)).reduce((sum, count) => sum + count, 0);
  assert.equal(total, errors.length);
  assert.deepEqual(settingsFailureSummary([]).stages, []);
});
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

test('ordinary Settings captures retain the task policy and reject undeclared actions', () => {
  assert.equal(settingsGeometryPolicy(ordinarySettingsTransition('archive-dialog')).geometry, 'content-transition');
  assert.equal(settingsGeometryPolicy(ordinarySettingsTransition('linked-reconnecting')).geometry, 'stable-status');
  assert.throws(() => ordinarySettingsTransition('passive-refresh'), /Unclassified/);
});

test('initial Settings reveal cannot excuse a later shift, drift, missing section or overflow', () => {
  const frames = [frame(), frame(40), frame(40)].map((value, index) => ({ ...value, nativeAt: 5 + index * 10 }));
  const transitions = [{ id: 'loading', before: { frame: 0, nativeAt: 0 }, after: { frame: 2, nativeAt: 20 } }];
  const auxiliary = [{ id: 'fixture:scroll-audit', before: { frame: 2, nativeAt: 20 }, after: { frame: 3, nativeAt: 100 } }];
  const check = changed => inspectSettingsFrames(changed, transitions, ['settings'], {
    auxiliary, rawShifts: changed.flatMap(value => value.shifts),
    nativeTimeline: { basis: 'pre-clock-performance/v1', clockVerified: true, completed: true, timeOrigin: 1, capturedAt: 0, endAt: 100 },
  });
  assert.deepEqual(check(frames).failures, []);
  const late = { sequence: 1, at: 24, deliveredAt: 25, value: 0.01 };
  assert(check([frames[0], frames[1], { ...frames[2], shifts: [late] }]).failures.some(value => value.includes('layout shift')));
  assert(check([frames[0], frames[1], { ...frames[2], regions: frame(41).regions }]).failures.some(value => value.includes('moved')));
  assert(check([frames[0], { ...frames[1], regions: [] }, frames[2]]).failures.some(value => value.includes('missing')));
  assert(check([frames[0], { ...frames[1], regions: frame(40, true).regions }, frames[2]]).failures.some(value => value.includes('overflow')));
});
