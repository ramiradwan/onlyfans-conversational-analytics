import assert from 'node:assert/strict';
import test from 'node:test';
import { fullReviewIntent, createFullReviewIntentConsumer } from '../runtime/onboarding-full-intent.mjs';

const record = { journey_id: '11111111-1111-4111-8111-111111111111',
  draft_scope: { scope_id: '22222222-2222-4222-8222-222222222222', disclosure_bundle_id: 'a'.repeat(64) } };
function fixture() {
  const values = new Map();
  const storage = { getItem: (key) => values.get(key), setItem: (key, value) => values.set(key, value) };
  const state = { view: 'preview-ready', mode: 'preview', terms: true, risk: true };
  const apply = () => { state.view = 'full-disclosure'; };
  return { state, storage, apply, consume: createFullReviewIntentConsumer({ storage, apply }) };
}
test('an explicit Full review updates the already-open Preview without changing consent or drafts', () => {
  const f = fixture();
  const intent = fullReviewIntent(record.journey_id, record.draft_scope);
  assert.equal(f.consume(intent, record), true);
  assert.deepEqual(f.state, { view: 'full-disclosure', mode: 'preview', terms: true, risk: true });
  f.state.view = 'preview-ready';
  assert.equal(f.consume(intent, record), false);
  assert.equal(f.state.view, 'preview-ready');
  const explicitAgain = fullReviewIntent(record.journey_id, record.draft_scope);
  assert.notEqual(explicitAgain.request_id, intent.request_id);
  assert.equal(f.consume(explicitAgain, record), true);
  assert.equal(f.state.view, 'full-disclosure');
});
test('reload does not reopen a dismissed review from the same request', () => {
  const f = fixture();
  const intent = fullReviewIntent(record.journey_id, record.draft_scope);
  f.consume(intent, record); f.state.view = 'preview-ready';
  const restored = createFullReviewIntentConsumer({ storage: f.storage, apply: f.apply });
  assert.equal(restored(intent, record), false);
  assert.equal(f.state.view, 'preview-ready');
});
test('old journey, creator or disclosure intent cannot change the current review', () => {
  const f = fixture();
  for (const current of [null, {}, { ...record, journey_id: crypto.randomUUID() },
    { ...record, draft_scope: { ...record.draft_scope, scope_id: crypto.randomUUID() } },
    { ...record, draft_scope: { ...record.draft_scope, disclosure_bundle_id: 'b'.repeat(64) } }]) {
    assert.equal(f.consume(fullReviewIntent(record.journey_id, record.draft_scope), current), false);
  }
  assert.equal(f.state.view, 'preview-ready');
});
