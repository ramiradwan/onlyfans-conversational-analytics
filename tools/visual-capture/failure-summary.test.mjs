import assert from 'node:assert/strict';
import { test } from 'node:test';
import { failureSummary } from './failure-summary.mjs';

test('failure counts preserve rejection evidence without exposing browser content', () => {
  const privateValue = 'PRIVATE-CANARY-cookie=abc@example.test';
  const summary = failureSummary([privateValue, 'driver exception ' + privateValue], [
    privateValue + ': overflow', privateValue + ': moved or resized',
    privateValue + ': removed', privateValue + ': missing from captured frame',
    privateValue + ': layout shift after first paint',
    'Layout shift 1: outside a unique native transition window (gap, tail or boundary)',
    privateValue + ': invalid native observation window', privateValue, null,
  ]);
  assert.deepEqual(summary, { driver: 2, overflow: 1, geometry: 3, shift: 2, observation: 1, other: 2 });
  assert.equal(JSON.stringify(summary).includes(privateValue), false);
  assert.equal(Object.values(summary).reduce((total, count) => total + count, 0), 11);
  assert(Object.values(summary).every(count => Number.isSafeInteger(count) && count >= 0));
});

test('successful observations produce only zero counts', () => {
  assert(Object.values(failureSummary([], [])).every(count => count === 0));
});
