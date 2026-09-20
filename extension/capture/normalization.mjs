function isRecord(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function firstDefined(record, paths) {
  for (const path of paths) {
    let value = record;
    for (const segment of path) {
      if (!isRecord(value) || !Object.hasOwn(value, segment)) {
        value = undefined;
        break;
      }
      value = value[segment];
    }
    if (value !== undefined && value !== null) return value;
  }
  return undefined;
}

export function identifier(value) {
  if (typeof value === 'string' && value.length <= 200 && value.trim().length > 0) return value.trim();
  if (Number.isSafeInteger(value) && value >= 0) return String(value);
  return null;
}

export function normalizedTimestamp(value) {
  let milliseconds;
  if (typeof value === 'number' && Number.isFinite(value)) {
    milliseconds = Math.abs(value) < 1_000_000_000_000 ? value * 1000 : value;
  } else if (
    typeof value === 'string'
    && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value)
  ) {
    milliseconds = Date.parse(value);
  } else {
    return null;
  }
  if (!Number.isFinite(milliseconds)) return null;
  try {
    return new Date(milliseconds).toISOString();
  } catch (_error) {
    return null;
  }
}

export function messageDirection(record, senderId) {
  const counterpartyId = identifier(firstDefined(record, [['chatUserId']]));
  if (senderId === null || counterpartyId === null) return null;
  return senderId === counterpartyId ? 'inbound' : 'outbound';
}

/**
 * Display-name aliases in the precedence the authenticated read connector
 * applies to full upstream conversation objects. Passive capture and signer
 * history read the same conversations, so a different precedence here resolves
 * a different display_name for one chat and the account merge rejects the
 * second observation as material_conflict.
 */
export const CHAT_DISPLAY_NAME_ALIASES = Object.freeze([
  ['display_name'],
  ['displayName'],
  ['withUser', 'displayName'],
  ['withUser', 'name'],
  ['withUser', 'username'],
  ['with_user', 'displayName'],
  ['with_user', 'name'],
  ['with_user', 'username'],
  ['user', 'displayName'],
  ['user', 'name'],
  ['user', 'username'],
]);

/** Reduce a platform chat record to the only fields allowed across the page boundary. */
export function normalizeChatRecord(record, observedAt) {
  if (!isRecord(record)) return null;
  const chatId = identifier(firstDefined(record, [
    ['chat_id'],
    ['chatId'],
    ['id'],
    ['withUser', 'id'],
    ['with_user', 'id'],
  ]));
  const platformUserId = identifier(firstDefined(record, [
    ['platform_user_id'],
    ['platformUserId'],
    ['withUser', 'id'],
    ['with_user', 'id'],
    ['user', 'id'],
  ]));
  const rawDisplayName = firstDefined(record, CHAT_DISPLAY_NAME_ALIASES);
  const displayName = typeof rawDisplayName === 'string' && rawDisplayName.length > 0
    ? rawDisplayName
    : null;
  const updatedAt = normalizedTimestamp(firstDefined(record, [
    ['updated_at'],
    ['updatedAt'],
    ['changedAt'],
    ['lastMessage', 'createdAt'],
    ['last_message', 'created_at'],
  ])) ?? normalizedTimestamp(observedAt);
  if (chatId === null || platformUserId === null || updatedAt === null) return null;
  return {
    chat_id: chatId,
    platform_user_id: platformUserId,
    display_name: displayName,
    updated_at: updatedAt,
  };
}

/** Reduce a platform message record to the exact canonical message inputs. */
export function normalizeMessageRecord(
  record,
  { contextChatId = null } = {},
) {
  if (!isRecord(record)) return null;
  const messageId = identifier(firstDefined(record, [
    ['message_id'],
    ['messageId'],
    ['id'],
  ]));
  const chatId = identifier(firstDefined(record, [
    ['chat_id'],
    ['chatId'],
    ['chat', 'id'],
  ])) ?? identifier(contextChatId);
  const senderId = identifier(firstDefined(record, [
    ['sender_platform_user_id'],
    ['senderPlatformUserId'],
    ['sender_id'],
    ['senderId'],
    ['fromUser', 'id'],
    ['from_user', 'id'],
    ['sender', 'id'],
  ]));
  const text = firstDefined(record, [['text'], ['body']]);
  const sentAt = normalizedTimestamp(firstDefined(record, [
    ['sent_at'],
    ['sentAt'],
    ['created_at'],
    ['createdAt'],
    ['postedAt'],
  ]));
  const direction = messageDirection(record, senderId);
  if (
    messageId === null
    || chatId === null
    || senderId === null
    || typeof text !== 'string'
    || sentAt === null
    || direction === null
  ) return null;
  return {
    message_id: messageId,
    chat_id: chatId,
    sender_platform_user_id: senderId,
    text,
    sent_at: sentAt,
    direction,
  };
}

/** Transient IDs are used only to derive local deduplication tokens; no text is emitted. */
export function previewMessageObservation(record, observedAt, creatorId, contextChatId) {
  if (!isRecord(record) || normalizedTimestamp(observedAt) === null) return null;
  const recordId = identifier(firstDefined(record, [['message_id'], ['messageId'], ['id']]));
  const chatId = identifier(firstDefined(record, [['chat_id'], ['chatId'], ['chat', 'id'], ['chatUserId']]))
    ?? identifier(contextChatId);
  const activityAt = normalizedTimestamp(firstDefined(record, [
    ['sent_at'], ['sentAt'], ['created_at'], ['createdAt'], ['postedAt'],
  ]));
  if (recordId === null || chatId === null || identifier(creatorId) === null || activityAt === null) return null;
  const senderId = identifier(firstDefined(record, [
    ['sender_platform_user_id'],
    ['senderPlatformUserId'],
    ['sender_id'],
    ['senderId'],
    ['fromUser', 'id'],
    ['from_user', 'id'],
    ['sender', 'id'],
  ]));
  return {
    kind: 'message',
    observed_at: normalizedTimestamp(observedAt),
    activity_at: activityAt,
    creator_id: identifier(creatorId),
    record_id: recordId,
    chat_id: chatId,
    direction: messageDirection(record, senderId) ?? 'unknown',
  };
}

export function previewChatObservation(record, observedAt, creatorId) {
  if (!isRecord(record)) return null;
  const normalized = normalizedTimestamp(observedAt);
  const recordId = identifier(firstDefined(record, [['chat_id'], ['chatId'], ['id'], ['withUser', 'id'], ['with_user', 'id']]));
  const activityAt = normalizedTimestamp(firstDefined(record, [
    ['lastMessage', 'createdAt'], ['last_message', 'created_at'], ['updated_at'], ['updatedAt'], ['changedAt'],
  ]));
  return normalized === null || recordId === null || identifier(creatorId) === null || activityAt === null ? null : {
    kind: 'chat',
    observed_at: normalized,
    activity_at: activityAt,
    creator_id: identifier(creatorId),
    record_id: recordId,
  };
}
