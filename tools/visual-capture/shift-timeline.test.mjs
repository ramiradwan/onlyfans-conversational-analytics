import assert from 'node:assert/strict';
import test from 'node:test';
import { inspectShiftTimeline } from './shift-timeline.mjs';
import { inspectSettingsFrames } from './settings-stability.mjs';

const clock = { basis: 'pre-clock-performance/v1', clockVerified: true, completed: true, timeOrigin: 1_000, capturedAt: 0, endAt: 100 };
const window = (id, start, end, geometry = 'stable-status') => ({ id, kind: 'transition', index: 0, geometry,
  before: { nativeAt: start }, after: { nativeAt: end } });
const entry = (at, deliveredAt = 90, more = {}) => ({ sequence: 1, at, deliveredAt, value: 0.01, hadRecentInput: true, sources: [], ...more });
const frame = (nativeAt, shifts = []) => ({ at: 300_000 + nativeAt * 10, nativeAt, shifts, errors: [],
  regions: [{ id: 'settings', rect: [0, 0, 300, 200], overflow: false }] });
const inspect = (shifts, windows, metadata = clock) => inspectShiftTimeline([frame(95, shifts)], windows, shifts, metadata);

test('late delivery belongs to the preceding content occurrence, with the raw record retained', () => {
  const raw = [entry(15, 35)];
  const frozen = structuredClone(raw);
  const result = inspect(raw, [window('content', 10, 20, 'content-transition'), window('passive', 30, 40)]);
  assert.deepEqual(result.failures, []);
  assert.equal(result.attribution.length, raw.length);
  assert.equal(result.attribution[0].window.id, 'content');
  assert.deepEqual(raw, frozen);
});

test('late passive movement cannot be excused by delivery during a later content change', () => {
  const result = inspect([entry(15, 35)], [window('passive', 10, 20), window('content', 30, 40, 'content-transition')]);
  assert.deepEqual(result.failures, ['passive: layout shift after first paint']);
  assert.equal(result.attribution[0].window.id, 'passive');
});

test('unknown gaps, tail, startup and exact boundaries fail closed', () => {
  for (const at of [1, 10, 20, 25, 30, 40, 80]) {
    assert(inspect([entry(at)], [window('content', 10, 20, 'content-transition'), window('other', 30, 40, 'content-transition')])
      .failures.some(value => value.includes('outside a unique')));
  }
});

test('overlap, missing clocks and ledger loss cannot pass even with no region movement', () => {
  assert(inspect([entry(15)], [window('one', 10, 20, 'content-transition'), window('two', 12, 25, 'content-transition')])
    .failures.some(value => value.includes('overlapping')));
  for (const metadata of [null, {}, { ...clock, clockVerified: false }, { ...clock, completed: false }]) {
    assert(inspect([], [window('passive', 10, 20)], metadata).failures.length);
  }
  const raw = [entry(15)];
  assert(inspectShiftTimeline([frame(95)], [], raw, clock).failures.some(value => value.includes('ledger and raw')));
  assert(inspectShiftTimeline([frame(95, raw)], [], undefined, clock).failures.some(value => value.includes('ledger is missing')));
  for (const bad of [{ sequence: 2 }, { at: NaN }, { deliveredAt: 5 }, { deliveredAt: 101 }, { value: -1 }]) {
    assert(inspect([entry(15, 90, bad)], [window('content', 10, 20, 'content-transition')]).failures.length);
  }
});

test('zero-valued observations stay in the ledger and large virtual clock values are irrelevant', () => {
  const result = inspect([entry(25, 90, { value: 0 })], [window('content', 10, 20, 'content-transition')]);
  assert.deepEqual(result.failures, []);
  assert.equal(result.attribution[0].classification, 'zero-valued');
});

test('same outer boxes and out-and-back internal shifts still fail a stable Settings stage', () => {
  const raw = [entry(35, 75), entry(37, 76, { sequence: 2 })];
  const frames = [frame(5), frame(25), frame(45), frame(80, raw)];
  const transitions = [
    { id: 'activation:review', before: { nativeAt: 10, frame: 1 }, after: { nativeAt: 20, frame: 1 } },
    { id: 'activation:uncertain', before: { nativeAt: 30, frame: 2 }, after: { nativeAt: 40, frame: 2 } },
  ];
  const result = inspectSettingsFrames(frames, transitions, ['settings'], { rawShifts: raw, nativeTimeline: clock });
  assert.deepEqual(result.failures, ['activation:uncertain: layout shift after first paint']);
  assert.equal(result.shiftAttribution.length, 2);
});

test('named auxiliary content windows neither cover gaps nor excuse stable auxiliary drift', () => {
  const transitions = [{ id: 'activation:uncertain', before: { nativeAt: 30, frame: 2 }, after: { nativeAt: 40, frame: 2 } }];
  const auxiliary = [{ id: 'fixture:response-order-reset', before: { nativeAt: 10, frame: 1 }, after: { nativeAt: 20, frame: 1 } }];
  const check = (at, extra = auxiliary) => {
    const raw = [entry(at)];
    return inspectSettingsFrames([frame(5), frame(25), frame(95, raw)], transitions, ['settings'], { rawShifts: raw, nativeTimeline: clock, auxiliary: extra });
  };
  assert.deepEqual(check(15).failures, []);
  assert.equal(check(15).shiftAttribution[0].window.kind, 'fixture');
  assert(check(25).failures.some(value => value.includes('gap, tail or boundary')));
  assert(check(35).failures.includes('activation:uncertain: layout shift after first paint'));
  assert(check(15, [{ ...auxiliary[0], id: 'fixture:activation-code-input' }]).failures.includes('fixture:activation-code-input: layout shift after first paint'));
  assert(check(35, [{ ...auxiliary[0], after: { nativeAt: 45, frame: 2 } }]).failures.some(value => value.includes('overlapping')));
});
