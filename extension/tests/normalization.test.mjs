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
    id: 'message-synthetic', createdAt: '2030-01-07T12:00:00Z',
    fromUser: { id: 'creator-synthetic' },
    chatUserId: 'fan-synthetic',
  }, '2030-01-08T12:00:00Z', 'creator-synthetic', 'fan-synthetic');

  assert.deepEqual(preview, {
    kind: 'message',
    observed_at: '2030-01-08T12:00:00.000Z',
    activity_at: '2030-01-07T12:00:00.000Z',
    creator_id: 'creator-synthetic', record_id: 'message-synthetic', chat_id: 'fan-synthetic',
    direction: 'outbound',
  });
});

test('missing source IDs or activity dates cannot produce falsely dated Preview activity', () => {
  const base = { id: 'message-synthetic', chatUserId: 'fan-synthetic', fromUser: { id: 'fan-synthetic' } };
  assert.equal(previewMessageObservation(base, '2030-01-08T12:00:00Z', 'creator-synthetic', null), null);
  assert.equal(previewMessageObservation({ ...base, id: null, createdAt: '2030-01-08T11:00:00Z' },
    '2030-01-08T12:00:00Z', 'creator-synthetic', null), null);
});
