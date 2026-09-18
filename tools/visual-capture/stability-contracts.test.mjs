import assert from 'node:assert/strict';
import test from 'node:test';
import { FONT_SWAP_SHIFT, colorDistance, grade, gradeLayoutShifts, parseColor } from './stability-contracts.mjs';

const shift = (value, sources = [{ key: false }], hadRecentInput = false) => ({ value, hadRecentInput, sources });

test('reads the same color identically across computed-value syntaxes', () => {
  assert.ok(colorDistance('rgb(255, 255, 255)', 'oklch(1 0 0)') < 1e-4);
  assert.ok(colorDistance('oklch(96.7853% 0.00453 78.3)', 'oklch(0.967853 0.00453 78.3)') < 1e-9);
  assert.ok(colorDistance('color(srgb 0 0 0)', 'rgb(0 0 0)') < 1e-9);
  assert.ok(Math.abs(colorDistance('rgb(255, 255, 255)', 'rgb(0, 0, 0)') - 1) < 1e-4);
});

test('refuses transparent canvases instead of measuring them', () => {
  assert.equal(parseColor('rgba(0, 0, 0, 0)').alpha, 0);
  assert.throws(() => colorDistance('rgba(0, 0, 0, 0)', 'rgb(0, 0, 0)'), /opaque/);
  assert.throws(() => parseColor('hsl(0 0% 0%)'), /unsupported/);
});

test('grades at the warn and fail limits', () => {
  const limits = { warn: 0.001, fail: 0.02 };
  assert.equal(grade(0, limits), 'pass');
  assert.equal(grade(0.001, limits), 'pass');
  assert.equal(grade(0.0011, limits), 'warn');
  assert.equal(grade(0.02, limits), 'fail');
});

test('warns on any unprompted layout shift and fails at the score limit', () => {
  assert.equal(gradeLayoutShifts([]).level, 'pass');
  assert.equal(gradeLayoutShifts([shift(0.0004)]).level, 'warn');
  assert.equal(gradeLayoutShifts([shift(0.00000612)]).score, 0.00000612);
  assert.equal(gradeLayoutShifts([shift(0.006), shift(0.004)]).level, 'fail');
});

test('grades font swaps against their own limits', () => {
  assert.equal(gradeLayoutShifts([shift(0.00006)], FONT_SWAP_SHIFT).level, 'pass');
  assert.equal(gradeLayoutShifts([shift(0.002)], FONT_SWAP_SHIFT).level, 'warn');
  assert.equal(gradeLayoutShifts([shift(0.01)], FONT_SWAP_SHIFT).level, 'fail');
  assert.equal(gradeLayoutShifts([shift(0.00006, [{ key: true }])], FONT_SWAP_SHIFT).level, 'fail');
});

test('fails any moved key element and ignores shifts that follow input', () => {
  assert.equal(gradeLayoutShifts([shift(0.0001, [{ key: true }])]).level, 'fail');
  assert.equal(gradeLayoutShifts([shift(0.5, [{ key: true }], true)]).level, 'pass');
});
