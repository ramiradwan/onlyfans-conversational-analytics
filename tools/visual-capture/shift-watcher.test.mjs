import assert from 'node:assert/strict';
import test from 'node:test';
import { gradeLayoutShifts } from './stability-contracts.mjs';
import { inspectFrames } from './shift-watcher.mjs';

const frame = (rect = [0, 0, 280, 24], overflow = false) => ({ regions: [{ id: 'arbitrary', rect, overflow }], shifts: [] });
test('zero movement passes and fractional movement fails regardless of input', () => {
  assert.equal(gradeLayoutShifts([]).level, 'pass');
  assert.equal(gradeLayoutShifts([{ value: 0.000001, hadRecentInput: true, sources: [] }]).level, 'fail');
  assert.deepEqual(inspectFrames([frame(), frame()]), []);
  assert(inspectFrames([frame(), frame([0.01, 0, 280, 24])]).some((issue) => issue.includes('moved')));
});
test('arbitrary resized, removed and overflowing regions fail without inspecting their copy', () => {
  assert(inspectFrames([frame(), frame([0, 0, 281, 24])]).length);
  assert(inspectFrames([frame(), { regions: [], shifts: [] }]).length);
  assert(inspectFrames([frame([0, 0, 280, 24], true)]).some((issue) => issue.includes('overflow')));
});

test('a reading viewport remains fixed while its contents change', () => {
  const child = { id: 'reading-row', rect: [0, 0, 100, 20], overflow: false, readingChild: true };
  assert.deepEqual(inspectFrames([{ ...frame(), regions: [...frame().regions, child] }, frame()]), []);
  assert(inspectFrames([{ ...frame(), regions: [...frame().regions, child] }, frame([0, 0, 280, 25])]).length);
});
