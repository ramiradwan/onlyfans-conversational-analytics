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
  if (typeof value === 'string' && value.trim().length > 0) return value.trim();
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

export function messageDirection(record, senderId, creatorId) {
  const explicit = firstDefined(record, [['direction']]);
  if (typeof explicit === 'string') {
    const normalized = explicit.toLowerCase();
    if (['outbound', 'creator', 'outgoing', 'sent'].includes(normalized)) return 'outbound';
    if (['inbound', 'fan', 'incoming', 'received'].includes(normalized)) return 'inbound';
  }

  const creatorFlag = firstDefined(record, [
    ['isFromCreator'],
    ['is_from_creator'],
    ['isOutgoing'],
    ['is_outgoing'],
    ['outgoing'],
    ['fromUser', 'is_me'],
    ['fromUser', 'isMe'],
    ['fromUser', 'me'],
    ['from_user', 'is_me'],
  ]);
  if (typeof creatorFlag === 'boolean') return creatorFlag ? 'outbound' : 'inbound';
  if (creatorId !== null && senderId !== null) {
    return senderId === creatorId ? 'outbound' : 'inbound';
  }
  return null;
}

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
  const rawDisplayName = firstDefined(record, [
    ['display_name'],
    ['displayName'],
    ['withUser', 'name'],
    ['withUser', 'displayName'],
    ['withUser', 'username'],
    ['with_user', 'name'],
    ['with_user', 'username'],
    ['user', 'name'],
    ['user', 'username'],
  ]);
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
  { contextChatId = null, creatorPlatformUserId = null } = {},
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
  const direction = messageDirection(
    record,
    senderId,
    identifier(creatorPlatformUserId),
  );
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

/** Preview observations intentionally contain no identifiers or communication text. */
export function previewMessageObservation(record, creatorPlatformUserId, observedAt) {
  if (!isRecord(record) || normalizedTimestamp(observedAt) === null) return null;
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
    direction: messageDirection(
      record,
      senderId,
      identifier(creatorPlatformUserId),
    ) ?? 'unknown',
  };
}

export function previewChatObservation(observedAt) {
  const normalized = normalizedTimestamp(observedAt);
  return normalized === null ? null : {
    kind: 'chat',
    observed_at: normalized,
  };
}
