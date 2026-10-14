import assert from 'node:assert/strict';
import test from 'node:test';
import { createSetupChoice } from '../ui/setup-choice.mjs';

test('a saved mode review never crosses journey, creator-draft or disclosure scope', () => {
  const values = new Map();
  const storage = { getItem: key => values.get(key), setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) };
  const record = { journey_id: '11111111-1111-4111-8111-111111111111', draft_scope: {
    scope_id: '22222222-2222-4222-8222-222222222222', disclosure_bundle_id: 'a'.repeat(64) } };
  const choice = createSetupChoice(storage);
  for (const changed of [
    { ...record, journey_id: '33333333-3333-4333-8333-333333333333' },
    { ...record, draft_scope: { ...record.draft_scope, scope_id: '33333333-3333-4333-8333-333333333333' } },
    { ...record, draft_scope: { ...record.draft_scope, disclosure_bundle_id: 'b'.repeat(64) } },
  ]) {
    choice.write(record, { mode: 'full', stage: 'access' });
    assert.deepEqual(choice.read(record), { mode: 'full', stage: 'access' });
    assert.equal(choice.read(changed), null);
    assert.equal(choice.read(record), null);
  }
  choice.write(record, { mode: 'ready', stage: 'authorized' });
  assert.equal(choice.read(record), null);
});
