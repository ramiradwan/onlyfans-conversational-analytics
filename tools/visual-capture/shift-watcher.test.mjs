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

test('passkey task changes allow trailing feedback while retaining the card origin and width', () => {
  const first = { ...frame(), task: 'passkey:sign-in', at: 110, nativeAt: 10,
    regions: [{ id: 'passkey-card', rect: [16, 80, 358, 240] }] };
  const next = { ...first, task: 'passkey:sign-in:feedback', at: 120, nativeAt: 20,
    regions: [{ id: 'passkey-card', rect: [16, 80, 358, 276] }],
    shifts: [{ value: 0.1, at: 16 }] };
  assert.deepEqual(inspectFrames([first, next]), []);
  assert(inspectFrames([first, { ...next, regions: [{ id: 'passkey-card', rect: [17, 80, 358, 276] }] }])
    .includes('passkey-card: moved across task change'));
  assert(inspectFrames([first, { ...next, task: first.task }]).includes('passkey-card: moved or resized'));
});

test('dashboard task changes retain fixed chrome and section columns', () => {
  const first = { task: 'dashboard:setup', at: 10, shifts: [], regions: [
    { id: 'dashboard-title', rect: [16, 50, 358, 40] },
    { id: 'freshness-status', rect: [16, 16, 358, 24] },
    { id: 'dashboard-setup', rect: [16, 90, 358, 180] },
  ] };
  const next = { ...first, task: 'dashboard:overview', at: 20, regions: [
    ...first.regions.slice(0, 2), { id: 'dashboard-setup', rect: [16, 200, 358, 240] },
  ] };
  assert.deepEqual(inspectFrames([first, next]), []);
  assert(inspectFrames([first, { ...next, regions: next.regions.map(region => ({ ...region,
    rect: region.id === 'freshness-status' ? [16, 17, 358, 24] : region.rect,
  })) }]).includes('freshness-status: moved across task change'));
  assert(inspectFrames([first, { ...next, regions: next.regions.map(region => ({ ...region,
    rect: region.id === 'dashboard-setup' ? [17, 200, 358, 240] : region.rect,
  })) }]).includes('dashboard-setup: moved across task change'));
});
