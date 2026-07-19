import { rawIngestChange } from '../protocol/validation.mjs';

export const CAPTURE_MESSAGE_TYPE = 'ofca.capture.observation';
export const CAPTURE_PROTOCOL_VERSION = '1';

const OBSERVATION_KEYS = Object.freeze([
  'event_type',
  'observed_at',
  'source_path',
  'creator_platform_user_id',
  'context_chat_id',
  'record',
]);
const HOOK_DIAGNOSTIC_KEYS = Object.freeze([
  'event_type',
  'source_event_type',
  'code',
  'observed_at',
  'source_path',
]);
const KNOWN_EVENT_TYPES = new Set([
  'chat.observed',
  'message.observed',
  'hook.diagnostic',
  'unknown',
]);
const KNOWN_DROP_REASONS = new Set([
  'capture_disabled',
  'enqueue_failed',
  'hook_invalid_json',
  'hook_unrecognized_payload',
  'invalid_bridge_message',
  'invalid_chat',
  'invalid_message',
  'malformed_observation',
  'unrecognized_event',
]);

function isRecord(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function hasExactKeys(value, expected) {
  const keys = Object.keys(value);
  return keys.length === expected.length && keys.every((key) => expected.includes(key));
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

function identifier(value) {
  if (typeof value === 'string' && value.trim().length > 0) return value.trim();
  if (Number.isSafeInteger(value) && value >= 0) return String(value);
  return null;
}

function timestamp(value) {
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

function contextChatId(observation) {
  const explicit = identifier(observation.context_chat_id);
  if (explicit !== null) return explicit;
  const match = /^\/api2\/v2\/chats\/([^/]+)\/messages\/?$/.exec(observation.source_path);
  if (!match) return null;
  try {
    return identifier(decodeURIComponent(match[1]));
  } catch (_error) {
    return identifier(match[1]);
  }
}

function messageDirection(record, senderId, creatorId) {
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
  if (creatorId !== null) return senderId === creatorId ? 'outbound' : 'inbound';
  return null;
}

function mapChat(observation, observedAt) {
  const { record } = observation;
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
  const updatedAt = timestamp(firstDefined(record, [
    ['updated_at'],
    ['updatedAt'],
    ['changedAt'],
    ['lastMessage', 'createdAt'],
    ['last_message', 'created_at'],
  ])) ?? observedAt;
  if (chatId === null || platformUserId === null || updatedAt === null) return null;
  return {
    type: 'chat.upsert',
    chat: {
      chat_id: chatId,
      platform_user_id: platformUserId,
      display_name: displayName,
      updated_at: updatedAt,
    },
  };
}

function mapMessage(observation) {
  const { record } = observation;
  const messageId = identifier(firstDefined(record, [
    ['message_id'],
    ['messageId'],
    ['id'],
  ]));
  const chatId = identifier(firstDefined(record, [
    ['chat_id'],
    ['chatId'],
    ['chat', 'id'],
  ])) ?? contextChatId(observation);
  const senderId = identifier(firstDefined(record, [
    ['sender_platform_user_id'],
    ['senderPlatformUserId'],
    ['sender_id'],
    ['senderId'],
    ['fromUser', 'id'],
    ['from_user', 'id'],
    ['sender', 'id'],
  ]));
  const rawText = firstDefined(record, [['text'], ['body']]);
  const sentAt = timestamp(firstDefined(record, [
    ['sent_at'],
    ['sentAt'],
    ['created_at'],
    ['createdAt'],
    ['postedAt'],
  ]));
  const creatorId = identifier(observation.creator_platform_user_id);
  const direction = senderId === null ? null : messageDirection(record, senderId, creatorId);
  if (
    messageId === null
    || chatId === null
    || senderId === null
    || typeof rawText !== 'string'
    || sentAt === null
    || direction === null
  ) return null;
  return {
    type: 'message.upsert',
    message: {
      message_id: messageId,
      chat_id: chatId,
      sender_platform_user_id: senderId,
      text: rawText,
      sent_at: sentAt,
      direction,
    },
  };
}

function placeholderChatForMessage(change) {
  const { message } = change;
  return {
    type: 'chat.upsert',
    chat: {
      chat_id: message.chat_id,
      platform_user_id: message.chat_id,
      display_name: null,
      updated_at: message.sent_at,
    },
  };
}

function invalid(reason, eventType = 'unknown') {
  return { ok: false, reason, eventType };
}

/** Maps one page observation to exactly one protocol v1 raw-ingest change. */
export function mapPlatformObservation(observation) {
  if (!isRecord(observation) || typeof observation.event_type !== 'string') {
    return invalid('malformed_observation');
  }
  if (observation.event_type === 'hook.diagnostic') {
    if (
      !hasExactKeys(observation, HOOK_DIAGNOSTIC_KEYS)
      || !['http.response', 'websocket.message'].includes(observation.source_event_type)
      || !['invalid_json', 'unrecognized_payload'].includes(observation.code)
      || timestamp(observation.observed_at) === null
      || typeof observation.source_path !== 'string'
      || !observation.source_path.startsWith('/')
    ) return invalid('malformed_observation', 'hook.diagnostic');
    return invalid(
      observation.code === 'invalid_json'
        ? 'hook_invalid_json'
        : 'hook_unrecognized_payload',
      'hook.diagnostic',
    );
  }
  if (!['chat.observed', 'message.observed'].includes(observation.event_type)) {
    return invalid('unrecognized_event');
  }
  if (
    !hasExactKeys(observation, OBSERVATION_KEYS)
    || !isRecord(observation.record)
    || typeof observation.source_path !== 'string'
    || !observation.source_path.startsWith('/')
    || observation.source_path.length > 2048
    || (
      observation.creator_platform_user_id !== null
      && identifier(observation.creator_platform_user_id) === null
    )
    || (
      observation.context_chat_id !== null
      && identifier(observation.context_chat_id) === null
    )
  ) return invalid('malformed_observation', observation.event_type);

  const observedAt = timestamp(observation.observed_at);
  if (observedAt === null) return invalid('malformed_observation', observation.event_type);
  const change = observation.event_type === 'chat.observed'
    ? mapChat(observation, observedAt)
    : mapMessage(observation);
  if (change === null) {
    return invalid(
      observation.event_type === 'chat.observed' ? 'invalid_chat' : 'invalid_message',
      observation.event_type,
    );
  }
  try {
    rawIngestChange(change, '$.change');
  } catch (_error) {
    return invalid(
      observation.event_type === 'chat.observed' ? 'invalid_chat' : 'invalid_message',
      observation.event_type,
    );
  }
  return {
    ok: true,
    eventType: observation.event_type,
    resource: observation.event_type === 'chat.observed' ? 'chats' : 'messages',
    sourcePath: observation.source_path,
    change,
  };
}

function normalizePath(path) {
  return path.length > 1 && path.endsWith('/') ? path.slice(0, -1) : path;
}

function globMatches(pattern, sourcePath) {
  if (typeof pattern !== 'string' || !pattern.startsWith('/')) return false;
  const expression = pattern
    .split('*')
    .map((part) => part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
    .join('[^/]+');
  return new RegExp(`^${expression}$`).test(normalizePath(sourcePath));
}

export function captureIsEnabled(document, resource, sourcePath) {
  const rules = document?.capture_policy?.rules;
  if (!Array.isArray(rules)) return false;
  return rules.some((rule) => (
    rule?.enabled === true
    && rule.resource === resource
    && globMatches(normalizePath(rule.url_pattern), normalizePath(sourcePath))
  ));
}

export class CaptureDiagnostics {
  constructor(report = () => {}) {
    this.report = report;
    this.counts = new Map();
  }

  record(reason, eventType = 'unknown') {
    const safeReason = KNOWN_DROP_REASONS.has(reason) ? reason : 'enqueue_failed';
    const safeEventType = KNOWN_EVENT_TYPES.has(eventType) ? eventType : 'unknown';
    const key = `${safeEventType}:${safeReason}`;
    const count = (this.counts.get(key) ?? 0) + 1;
    this.counts.set(key, count);
    const diagnostic = Object.freeze({
      reason: safeReason,
      event_type: safeEventType,
      count,
    });
    try {
      this.report(diagnostic);
    } catch (_error) {
      // Diagnostics never affect capture durability.
    }
    return diagnostic;
  }

  snapshot() {
    return Object.fromEntries(this.counts);
  }
}

export class CaptureIngestionService {
  constructor({ runtime, diagnostics = new CaptureDiagnostics() }) {
    if (typeof runtime?.wake !== 'function') throw new Error('Agent runtime is required');
    this.runtime = runtime;
    this.diagnostics = diagnostics;
  }

  rejectBridgeMessage() {
    this.diagnostics.record('invalid_bridge_message');
    return { ok: false, code: 'invalid_bridge_message', retryable: false };
  }

  async ingest(observation) {
    const mapped = mapPlatformObservation(observation);
    if (!mapped.ok) {
      this.diagnostics.record(mapped.reason, mapped.eventType);
      return { ok: false, code: mapped.reason, retryable: false };
    }

    try {
      const transport = await this.runtime.wake();
      if (!captureIsEnabled(
        this.runtime.configuration?.activeDocument,
        mapped.resource,
        mapped.sourcePath,
      )) {
        this.diagnostics.record('capture_disabled', mapped.eventType);
        return { ok: false, code: 'capture_disabled', retryable: false };
      }
      if (typeof transport?.captureDelta !== 'function') {
        throw new Error('Agent transport does not support durable capture');
      }
      let item;
      if (mapped.change.type === 'message.upsert') {
        if (typeof transport.captureMessageWithParent !== 'function') {
          throw new Error('Agent transport cannot capture a dependency-closed message');
        }
        const parent = placeholderChatForMessage(mapped.change);
        rawIngestChange(parent, '$.parent_change');
        item = await transport.captureMessageWithParent(mapped.change, parent);
      } else {
        item = await transport.captureDelta(mapped.change);
      }
      return {
        ok: true,
        event_type: mapped.eventType,
        source_seq: item.source_seq,
      };
    } catch (_error) {
      this.diagnostics.record('enqueue_failed', mapped.eventType);
      return { ok: false, code: 'enqueue_failed', retryable: true };
    }
  }
}

function isTrustedContentSender(sender, chromeApi) {
  if (sender?.id !== chromeApi.runtime.id || sender?.frameId !== 0) return false;
  try {
    return new URL(sender.url).origin === 'https://onlyfans.com';
  } catch (_error) {
    return false;
  }
}

function isCaptureRuntimeMessage(message) {
  return isRecord(message)
    && hasExactKeys(message, ['type', 'protocol_version', 'observation'])
    && message.type === CAPTURE_MESSAGE_TYPE
    && message.protocol_version === CAPTURE_PROTOCOL_VERSION;
}

/** Registers the synchronous MV3 listener that keeps the response port alive through enqueue. */
export function createCaptureMessageBridge({
  ingestion,
  chromeApi = globalThis.chrome,
}) {
  if (typeof ingestion?.ingest !== 'function') throw new Error('Capture ingestion service is required');
  if (!chromeApi?.runtime?.onMessage?.addListener) {
    throw new Error('chrome.runtime.onMessage is unavailable');
  }
  let registered = false;

  const listener = (message, sender, sendResponse) => {
    if (!isTrustedContentSender(sender, chromeApi)) return false;
    if (message?.type !== CAPTURE_MESSAGE_TYPE) return false;
    if (!isCaptureRuntimeMessage(message)) {
      sendResponse(ingestion.rejectBridgeMessage());
      return false;
    }
    void ingestion.ingest(message.observation).then(
      (response) => sendResponse(response),
      () => sendResponse({ ok: false, code: 'enqueue_failed', retryable: true }),
    );
    return true;
  };

  return Object.freeze({
    register() {
      if (registered) return;
      chromeApi.runtime.onMessage.addListener(listener);
      registered = true;
    },
    unregister() {
      if (!registered) return;
      chromeApi.runtime.onMessage.removeListener?.(listener);
      registered = false;
    },
    listener,
  });
}
