export class InvariantViolation extends Error {
  constructor(code, detail) {
    super(detail);
    this.name = 'InvariantViolation';
    this.code = code;
  }
}

const clone = (value) => structuredClone(value);

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(
      Object.keys(value)
        .filter((key) => !['origin', 'observed_at', 'source_seq'].includes(key))
        .sort()
        .map((key) => [key, canonical(value[key])]),
    );
  }
  return value;
}

export function normalizedMaterialEqual(left, right) {
  return JSON.stringify(canonical(left)) === JSON.stringify(canonical(right));
}

function requireEntityId(entity, key, label) {
  const value = entity?.[key];
  if (typeof value !== 'string' || value.length === 0) {
    throw new InvariantViolation('invalid_entity', `${label} requires ${key}`);
  }
  return value;
}

function tombstone(entity, idKey) {
  return entity?.tombstone === true
    ? { [idKey]: entity[idKey], tombstone: true }
    : null;
}

function messageTombstone(entity, fallbackChatId = null) {
  const messageId = requireEntityId(entity, 'message_id', 'Message tombstone');
  const chatId = entity?.chat_id ?? fallbackChatId;
  if (typeof chatId !== 'string' || chatId.length === 0) {
    throw new InvariantViolation('invalid_entity', `Message tombstone ${messageId} requires chat_id`);
  }
  return { message_id: messageId, chat_id: chatId, tombstone: true };
}

/**
 * Applies the account-level chat merge contract. The result is deliberately
 * independent of capture origin and transport provenance.
 */
export function mergeChat(existing, incoming) {
  const incomingId = requireEntityId(incoming, 'chat_id', 'Chat');
  if (existing === null || existing === undefined) {
    return { action: 'insert', value: clone(tombstone(incoming, 'chat_id') ?? incoming) };
  }
  const existingId = requireEntityId(existing, 'chat_id', 'Stored chat');
  if (incomingId !== existingId) {
    throw new InvariantViolation('identity_conflict', 'Chat IDs do not match');
  }
  if (existing.tombstone === true) {
    if (incoming.tombstone === true) return { action: 'noop', value: clone(existing) };
    throw new InvariantViolation('tombstone_revive', `Chat ${incomingId} cannot be resurrected`);
  }
  if (incoming.tombstone === true) {
    return { action: 'replace', value: { chat_id: incomingId, tombstone: true } };
  }

  const existingKind = existing.record_kind ?? 'full';
  const incomingKind = incoming.record_kind ?? 'full';
  if (!['placeholder', 'full'].includes(existingKind) || !['placeholder', 'full'].includes(incomingKind)) {
    throw new InvariantViolation('invalid_entity', 'Chat record_kind must be placeholder or full');
  }
  if (existingKind === 'full' && incomingKind === 'placeholder') {
    return { action: 'noop', value: clone(existing) };
  }
  if (existingKind === 'placeholder' && incomingKind === 'full') {
    return { action: 'replace', value: clone(incoming) };
  }
  if (existingKind === 'placeholder' && incomingKind === 'placeholder') {
    if (!normalizedMaterialEqual(existing, incoming)) {
      throw new InvariantViolation(
        'material_conflict',
        `Placeholder chat ${incomingId} has conflicting material`,
      );
    }
    return { action: 'noop', value: clone(existing) };
  }

  if (existing.platform_user_id !== incoming.platform_user_id) {
    throw new InvariantViolation(
      'identity_conflict',
      `Chat ${incomingId} has conflicting platform identity`,
    );
  }
  const existingTime = Date.parse(existing.updated_at);
  const incomingTime = Date.parse(incoming.updated_at);
  if (!Number.isFinite(existingTime) || !Number.isFinite(incomingTime)) {
    throw new InvariantViolation('invalid_entity', `Chat ${incomingId} has an invalid updated_at`);
  }
  if (incomingTime < existingTime) return { action: 'noop', value: clone(existing) };
  if (incomingTime > existingTime) return { action: 'replace', value: clone(incoming) };
  if (normalizedMaterialEqual(existing, incoming)) {
    return { action: 'noop', value: clone(existing) };
  }
  throw new InvariantViolation(
    'material_conflict',
    `Chat ${incomingId} conflicts at the same upstream updated_at`,
  );
}

/** Applies the immutable account-level message merge contract. */
export function mergeMessage(existing, incoming) {
  const incomingId = requireEntityId(incoming, 'message_id', 'Message');
  if (existing === null || existing === undefined) {
    return {
      action: 'insert',
      value: clone(incoming.tombstone === true ? messageTombstone(incoming) : incoming),
    };
  }
  const existingId = requireEntityId(existing, 'message_id', 'Stored message');
  if (incomingId !== existingId) {
    throw new InvariantViolation('identity_conflict', 'Message IDs do not match');
  }
  if (existing.tombstone === true) {
    if (incoming.tombstone === true) {
      const storedTombstone = messageTombstone(existing);
      const repeatedTombstone = messageTombstone(incoming, storedTombstone.chat_id);
      if (repeatedTombstone.chat_id !== storedTombstone.chat_id) {
        throw new InvariantViolation(
          'identity_conflict',
          `Message ${incomingId} tombstone has conflicting parent chat_id`,
        );
      }
      return { action: 'noop', value: clone(storedTombstone) };
    }
    throw new InvariantViolation('tombstone_revive', `Message ${incomingId} cannot be resurrected`);
  }
  if (incoming.tombstone === true) {
    const existingChatId = requireEntityId(existing, 'chat_id', 'Stored message');
    const nextTombstone = messageTombstone(incoming, existingChatId);
    if (nextTombstone.chat_id !== existingChatId) {
      throw new InvariantViolation(
        'identity_conflict',
        `Message ${incomingId} tombstone has conflicting parent chat_id`,
      );
    }
    return {
      action: 'replace',
      value: nextTombstone,
    };
  }
  if (normalizedMaterialEqual(existing, incoming)) {
    return { action: 'noop', value: clone(existing) };
  }
  throw new InvariantViolation(
    'material_conflict',
    `Message ${incomingId} has conflicting immutable material`,
  );
}

export function entityForChange(change) {
  switch (change?.type) {
    case 'chat.upsert':
      return { kind: 'chat', id: change.chat.chat_id, value: change.chat };
    case 'chat.delete':
      return {
        kind: 'chat',
        id: change.chat_id,
        value: { chat_id: change.chat_id, tombstone: true },
      };
    case 'message.upsert':
      return { kind: 'message', id: change.message.message_id, value: change.message };
    case 'message.delete':
      return {
        kind: 'message',
        id: change.message_id,
        value: {
          message_id: change.message_id,
          chat_id: change.chat_id,
          tombstone: true,
        },
      };
    case 'coverage.observed':
      return { kind: 'coverage_evidence', id: null, value: change.evidence };
    default:
      throw new InvariantViolation('invalid_change', `Unsupported change ${String(change?.type)}`);
  }
}
