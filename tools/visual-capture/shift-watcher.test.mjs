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
    regions: [{ id: 'passkey-card', rect: [16, 80, 358, 240] },
      { id: 'passkey-heading', rect: [40, 142, 310, 30] }, { id: 'passkey-feedback', rect: [40, 296, 310, 0] }] };
  const next = { ...first, task: 'passkey:sign-in:feedback', at: 120, nativeAt: 20,
    regions: [{ id: 'passkey-card', rect: [16, 80, 358, 276] }, first.regions[1],
      { id: 'passkey-feedback', rect: [40, 296, 310, 36] }],
    shifts: [{ value: 0.1, at: 16, sources: [{ region: 'passkey-feedback' }] }] };
  assert.deepEqual(inspectFrames([first, next]), []);
  assert(inspectFrames([first, { ...next, regions: [{ id: 'passkey-card', rect: [17, 80, 358, 276] }, ...next.regions.slice(1)] }])
    .includes('passkey-card: moved across task change'));
  assert(inspectFrames([first, { ...next, task: first.task }]).includes('passkey-card: moved or resized'));
  const shiftedFeedback = { ...next.regions[2], rect: [40, 300, 310, 36] };
  assert(inspectFrames([first, { ...next, regions: [...next.regions.slice(0, 2), shiftedFeedback] }])
    .includes('passkey-feedback: moved across task change'));
  const shiftedHeading = { ...next.regions[1], rect: [40, 143, 310, 30] };
  assert(inspectFrames([first, { ...next, regions: [next.regions[0], shiftedHeading, next.regions[2]] }])
    .includes('passkey-heading: moved across task change'));
  assert(inspectFrames([first, { ...next, shifts: [{ value: 0.1, at: 16, sources: [{ tag: 'HEADER', region: null }] }] }])
    .includes('layout shift after first paint'));
});

test('dashboard task changes preserve unaffected sections and only displace their successors', () => {
  const task = (notice) => `dashboard:${JSON.stringify(['true:false', 'true', notice, 'true', 'setup'])}`;
  const metrics = [
    { id: 'metric-conversations', rect: [40, 119, 310, 20] },
    { id: 'metric-messages', rect: [40, 179, 310, 20] },
    { id: 'metric-Received', rect: [40, 219, 80, 20] },
    { id: 'metric-Sent', rect: [160, 219, 80, 20] },
  ];
  const first = { task: task(''), at: 10, shifts: [], regions: [
    { id: 'dashboard-title', rect: [16, 50, 358, 40] },
    { id: 'freshness-status', rect: [16, 16, 358, 24] },
    { id: 'dashboard-overview', rect: [16, 90, 358, 180] },
    { id: 'dashboard-basis', rect: [16, 270, 358, 40] },
    { id: 'dashboard-notice', rect: [16, 310, 358, 0] },
    { id: 'dashboard-recent', rect: [16, 310, 358, 100] },
    { id: 'dashboard-setup', rect: [16, 410, 358, 30] },
    ...metrics,
  ] };
  const resized = (snapshot, id, rect) => snapshot.regions.map(region => region.id === id ? { ...region, rect } : region);
  const next = { ...first, task: task('Reconnecting'), at: 20, regions: [
    ...first.regions.slice(0, 4), { id: 'dashboard-notice', rect: [16, 310, 358, 24] },
    { id: 'dashboard-recent', rect: [16, 334, 358, 100] }, { id: 'dashboard-setup', rect: [16, 434, 358, 30] },
    ...metrics,
  ], shifts: [{ at: 15, value: 0.1, sources: [{ region: 'dashboard-recent' }] }] };
  assert.deepEqual(inspectFrames([first, next]), []);
  for (const [id, rect] of [['freshness-status', [16, 17, 358, 24]], ['dashboard-basis', [16, 271, 358, 40]],
    ['dashboard-recent', [16, 334, 358, 101]], ['dashboard-setup', [16, 435, 358, 30]],
    ['metric-Received', [40, 220, 80, 20]]]) {
    assert(inspectFrames([first, { ...next, regions: resized(next, id, rect) }]).includes(`${id}: moved across task change`));
  }
  assert(inspectFrames([first, { ...next, shifts: [{ at: 15, value: 0.1, sources: [{ region: 'freshness-status' }] }] }])
    .includes('layout shift after first paint'));
});
