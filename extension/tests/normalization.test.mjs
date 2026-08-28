import assert from 'node:assert/strict';
import test from 'node:test';

import {
  messageDirection,
  previewMessageObservation,
} from '../capture/normalization.mjs';

test('message direction is inbound when the sender is the record counterparty', () => {
  assert.equal(messageDirection({ chatUserId: 'fan-synthetic' }, 'fan-synthetic'), 'inbound');
});

test('message direction is outbound when the sender is not the record counterparty', () => {
  assert.equal(messageDirection({ chatUserId: 'fan-synthetic' }, 'creator-synthetic'), 'outbound');
});

test('message direction is null when the record counterparty is absent', () => {
  assert.equal(messageDirection({}, 'fan-synthetic'), null);
});

test('preview message observations use the record counterparty direction', () => {
  const preview = previewMessageObservation({
    fromUser: { id: 'creator-synthetic' },
    chatUserId: 'fan-synthetic',
  }, '2030-01-08T12:00:00Z');

  assert.deepEqual(preview, {
    kind: 'message',
    observed_at: '2030-01-08T12:00:00.000Z',
    direction: 'outbound',
  });
});
