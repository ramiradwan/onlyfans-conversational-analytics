import { mapPlatformObservation } from './capture-ingestion.mjs';

const CONVERSATION_KEYS = Object.freeze([
  'id',
  'platform_user_id',
  'display_name',
  'updated_at',
]);
const MESSAGE_KEYS = Object.freeze([
  'id',
  'chat_id',
  'sender_platform_user_id',
  'text',
  'sent_at',
  'direction',
]);

function requireExactRecord(record, keys, label) {
  if (
    typeof record !== 'object'
    || record === null
    || Array.isArray(record)
    || Object.keys(record).length !== keys.length
    || Object.keys(record).some((key) => !keys.includes(key))
  ) throw new Error(`Signer returned a non-canonical ${label} item`);
}

function normalizedObservation({ eventType, record, observedAt, creatorPlatformId, conversationId }) {
  const sourcePath = eventType === 'chat.observed'
    ? '/api2/v2/chats'
    : `/api2/v2/chats/${encodeURIComponent(conversationId)}/messages`;
  const mapped = mapPlatformObservation({
    event_type: eventType,
    observed_at: observedAt,
    source_path: sourcePath,
    creator_platform_user_id: creatorPlatformId,
    context_chat_id: conversationId,
    record,
  });
  if (!mapped.ok) throw new Error(`Signer returned an invalid ${eventType} item`);
  return mapped.change;
}

export function normalizeSignerConversation(record, context) {
  requireExactRecord(record, CONVERSATION_KEYS, 'conversation');
  return normalizedObservation({
    eventType: 'chat.observed',
    record,
    observedAt: context.observedAt,
    creatorPlatformId: context.creatorPlatformId,
    conversationId: null,
  });
}

export function normalizeSignerMessage(record, context) {
  requireExactRecord(record, MESSAGE_KEYS, 'message');
  if (record.chat_id !== null && record.chat_id !== context.conversationId) {
    throw new Error('Signer message does not belong to the requested conversation');
  }
  const inferredDirection = record.sender_platform_user_id === context.creatorPlatformId
    ? 'outbound'
    : 'inbound';
  if (record.direction !== null && record.direction !== inferredDirection) {
    throw new Error('Signer message direction conflicts with the verified creator identity');
  }
  return normalizedObservation({
    eventType: 'message.observed',
    record,
    observedAt: context.observedAt,
    creatorPlatformId: context.creatorPlatformId,
    conversationId: context.conversationId,
  });
}
