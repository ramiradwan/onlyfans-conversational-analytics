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

test('confirmed task changes establish a new baseline while passive updates stay strict', () => {
  const first = { ...frame(), task: 'consent', at: 10 };
  const next = { ...frame([0, 0, 280, 48]), task: 'site-access', at: 20,
    shifts: [{ value: 0.1, at: 15 }] };
  assert.deepEqual(inspectFrames([first, next]), []);
  assert(inspectFrames([first, { ...next, task: 'consent' }]).includes('arbitrary: moved or resized'));
  assert(inspectFrames([first, next, { ...next, at: 30, regions: frame([0.01, 0, 280, 48]).regions }]).some(issue => issue.includes('moved')));
  assert(inspectFrames([first, next, { ...next, at: 30, shifts: [{ value: 0.1, at: 25 }] }]).includes('layout shift after first paint'));
  assert(inspectFrames([first, { ...next, regions: frame([0, 0, 280, 48], true).regions }]).includes('arbitrary: overflow'));
  assert(inspectFrames([first, { ...next, errors: ['required: missing on first surface frame'] }]).includes('required: missing on first surface frame'));
});

test('changed status IDs cannot excuse movement within one confirmed requirement', () => {
  const first = { ...frame(), task: 'activation', status: 'activation_checking', at: 10 };
  const changed = { ...frame([0, 0, 280, 48]), task: 'activation', status: 'activation_required', at: 20 };
  assert(inspectFrames([first, changed]).includes('arbitrary: moved or resized'));
  assert.deepEqual(inspectFrames([first, { ...changed, regions: frame().regions }]), []);
  assert.deepEqual(inspectFrames([first, { ...changed, task: 'analysis' }]), []);
});

test('a task boundary covers its next paint and cannot excuse a later shift', () => {
  const first = { ...frame(), task: 'registration', at: 10, paintThrough: 12 };
  const changed = { ...frame([0, 0, 280, 48]), task: 'approval', at: 20, paintThrough: 24 };
  const report = { ...changed, at: 30, shifts: [{ value: 0.1, at: 21 }] };
  assert.deepEqual(inspectFrames([first, changed, report]), []);
  assert(inspectFrames([first, changed, { ...report, shifts: [{ value: 0.1, at: 25 }] }]).includes('layout shift after first paint'));
  assert(inspectFrames([first, { ...changed, task: 'registration' }, { ...report, task: 'registration' }]).includes('layout shift after first paint'));
});
