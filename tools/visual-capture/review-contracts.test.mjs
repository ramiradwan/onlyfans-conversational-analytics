import assert from 'node:assert/strict';
import test from 'node:test';
import { assertStateHierarchy, contrastAgainst, near } from './review-contracts.mjs';

const paper = [255, 255, 255, 1];
const rest = { fill: [0, 0, 0, 0], ink: [110, 110, 110, 1] };
const hover = { fill: [245, 245, 245, 1], ink: [90, 90, 90, 1] };
const selected = { fill: [230, 240, 238, 1], ink: [11, 92, 86, 1] };

test('composites transparent rest fills rather than treating them as black', () => {
  assert.equal(contrastAgainst(rest.fill, paper), 1);
  assert.equal(contrastAgainst([0, 0, 0, 1], paper), 21);
  assertStateHierarchy(rest, hover, selected, paper);
});
test('rejects hover outranking selection in either channel', () => {
  assert.throws(() => assertStateHierarchy(rest, selected, hover, paper));
  assert.throws(() => assertStateHierarchy(rest, { ...hover, ink: [0, 0, 0, 1] }, selected, paper));
});
test('rejects the original full-width rail geometry', () => {
  near(48, 48, 'width'); near(8, 8, 'inset');
  assert.throws(() => near(64, 48, 'width'));
  assert.throws(() => near(0, 8, 'inset'));
});
