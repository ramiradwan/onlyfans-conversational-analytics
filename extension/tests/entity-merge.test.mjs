import assert from 'node:assert/strict';
import test from 'node:test';

import {
  InvariantViolation,
  mergeChat,
  mergeMessage,
} from '../transport/entity-merge.mjs';

const fullChat = (overrides = {}) => ({
  chat_id: 'chat-1',
  record_kind: 'full',
  platform_user_id: 'fan-1',
  display_name: 'Alex',
  updated_at: '2026-07-18T10:00:00Z',
  ...overrides,
});
const placeholder = (overrides = {}) => ({
  chat_id: 'chat-1',
  record_kind: 'placeholder',
  platform_user_id: null,
  display_name: null,
  updated_at: null,
  ...overrides,
});
const message = (overrides = {}) => ({
  message_id: 'message-1',
  chat_id: 'chat-1',
  sender_platform_user_id: 'fan-1',
  text: 'Hello',
  sent_at: '2026-07-18T10:01:00Z',
  direction: 'inbound',
  ...overrides,
});

test('chat merge is deterministic across older/newer observations and capture origins', () => {
  const older = fullChat({ updated_at: '2026-07-18T09:00:00Z', display_name: 'Old' });
  const newer = fullChat({ updated_at: '2026-07-18T11:00:00Z', display_name: 'New' });
  const firstOrder = mergeChat(mergeChat(null, older).value, newer).value;
  const secondOrder = mergeChat(mergeChat(null, newer).value, {
    ...older,
    origin: 'signer',
  }).value;
  assert.deepEqual(firstOrder, newer);
  assert.deepEqual(secondOrder, newer);
  assert.equal(mergeChat(newer, { ...newer, origin: 'passive' }).action, 'noop');
});

test('full chats upgrade placeholders and placeholders never downgrade full records', () => {
  assert.deepEqual(mergeChat(placeholder(), fullChat()), {
    action: 'replace',
    value: fullChat(),
  });
  assert.equal(mergeChat(fullChat(), placeholder()).action, 'noop');
});

test('equal-version chat conflict and platform identity conflict fail explicitly', () => {
  assert.throws(
    () => mergeChat(fullChat(), fullChat({ display_name: 'Different' })),
    (error) => error instanceof InvariantViolation && error.code === 'material_conflict',
  );
  assert.throws(
    () => mergeChat(fullChat(), fullChat({
      platform_user_id: 'fan-2',
      updated_at: '2026-07-18T11:00:00Z',
    })),
    (error) => error instanceof InvariantViolation && error.code === 'identity_conflict',
  );
});

test('messages are immutable and tombstones cannot be revived', () => {
  const existing = message();
  assert.equal(mergeMessage(existing, { ...existing, origin: 'signer' }).action, 'noop');
  assert.throws(
    () => mergeMessage(existing, message({ text: 'Changed' })),
    (error) => error instanceof InvariantViolation && error.code === 'material_conflict',
  );
  const deleted = mergeMessage(existing, { message_id: 'message-1', tombstone: true }).value;
  assert.equal(deleted.tombstone, true);
  assert.throws(
    () => mergeMessage(deleted, existing),
    (error) => error instanceof InvariantViolation && error.code === 'tombstone_revive',
  );
});

test('unknown deletes create complete tombstones and matching repeated deletes are no-ops', () => {
  const chatDelete = { chat_id: 'chat-1', tombstone: true };
  const messageDelete = { message_id: 'message-1', chat_id: 'chat-1', tombstone: true };
  assert.equal(mergeChat(null, chatDelete).value.tombstone, true);
  assert.equal(mergeChat(chatDelete, chatDelete).action, 'noop');
  assert.deepEqual(mergeMessage(null, messageDelete), {
    action: 'insert',
    value: messageDelete,
  });
  assert.equal(mergeMessage(messageDelete, messageDelete).action, 'noop');
});

test('message tombstones reject conflicting parent chat identities', () => {
  const firstDelete = { message_id: 'message-1', chat_id: 'chat-1', tombstone: true };
  const conflictingDelete = { message_id: 'message-1', chat_id: 'chat-2', tombstone: true };
  assert.throws(
    () => mergeMessage(firstDelete, conflictingDelete),
    (error) => error instanceof InvariantViolation && error.code === 'identity_conflict',
  );
  assert.throws(
    () => mergeMessage(message(), conflictingDelete),
    (error) => error instanceof InvariantViolation && error.code === 'identity_conflict',
  );
});
