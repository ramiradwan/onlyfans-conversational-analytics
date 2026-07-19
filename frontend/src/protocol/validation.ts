export class ProtocolValidationError extends Error {
  constructor(path: string, message: string) {
    super(`${path}: ${message}`);
    this.name = 'ProtocolValidationError';
  }
}

export type Validator = (value: unknown, path: string) => void;

function record(value: unknown, path: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new ProtocolValidationError(path, 'expected object');
  }
  return value as Record<string, unknown>;
}

export function object(
  required: Record<string, Validator>,
  optional: Record<string, Validator> = {},
): Validator {
  return (value, path) => {
    const candidate = record(value, path);
    const allowed = new Set([...Object.keys(required), ...Object.keys(optional)]);
    for (const key of Object.keys(candidate)) {
      if (!allowed.has(key)) {
        throw new ProtocolValidationError(`${path}.${key}`, 'unknown field');
      }
    }
    for (const [key, validator] of Object.entries(required)) {
      if (!Object.hasOwn(candidate, key)) {
        throw new ProtocolValidationError(`${path}.${key}`, 'missing required field');
      }
      validator(candidate[key], `${path}.${key}`);
    }
    for (const [key, validator] of Object.entries(optional)) {
      if (Object.hasOwn(candidate, key)) {
        validator(candidate[key], `${path}.${key}`);
      }
    }
  };
}

export const nonEmptyString: Validator = (value, path) => {
  if (typeof value !== 'string' || value.length === 0) {
    throw new ProtocolValidationError(path, 'expected non-empty string');
  }
};

export const string: Validator = (value, path) => {
  if (typeof value !== 'string') {
    throw new ProtocolValidationError(path, 'expected string');
  }
};

export const boolean: Validator = (value, path) => {
  if (typeof value !== 'boolean') {
    throw new ProtocolValidationError(path, 'expected boolean');
  }
};

export function integer(minimum = Number.MIN_SAFE_INTEGER, maximum = Number.MAX_SAFE_INTEGER): Validator {
  return (value, path) => {
    if (!Number.isSafeInteger(value) || (value as number) < minimum || (value as number) > maximum) {
      throw new ProtocolValidationError(path, `expected integer from ${minimum} through ${maximum}`);
    }
  };
}

export function literal(...allowed: readonly unknown[]): Validator {
  return (value, path) => {
    if (!allowed.includes(value)) {
      throw new ProtocolValidationError(path, `expected one of ${allowed.join(', ')}`);
    }
  };
}

export function nullable(validator: Validator): Validator {
  return (value, path) => {
    if (value !== null) validator(value, path);
  };
}

export function array(
  validator: Validator,
  minimumLength = 0,
  maximumLength = Number.MAX_SAFE_INTEGER,
): Validator {
  return (value, path) => {
    if (!Array.isArray(value) || value.length < minimumLength || value.length > maximumLength) {
      throw new ProtocolValidationError(
        path,
        `expected array with ${minimumLength} through ${maximumLength} item(s)`,
      );
    }
    value.forEach((item, index) => validator(item, `${path}[${index}]`));
  };
}

export const uuid: Validator = (value, path) => {
  if (typeof value !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value)) {
    throw new ProtocolValidationError(path, 'expected UUID');
  }
};

export const isoDateTime: Validator = (value, path) => {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value) || Number.isNaN(Date.parse(value))) {
    throw new ProtocolValidationError(path, 'expected ISO 8601 datetime with timezone');
  }
};

export const digest: Validator = (value, path) => {
  if (typeof value !== 'string' || !/^sha256:[0-9a-f]{64}$/.test(value)) {
    throw new ProtocolValidationError(path, 'expected sha256 digest');
  }
};

export function discriminated(variants: Record<string, Validator>): Validator {
  return discriminatedBy('type', variants);
}

export function discriminatedBy(
  field: string,
  variants: Record<string, Validator>,
): Validator {
  return (value, path) => {
    const candidate = record(value, path);
    const discriminator = candidate[field];
    const key = String(discriminator);
    if (!(key in variants)) {
      throw new ProtocolValidationError(`${path}.${field}`, 'unknown discriminator');
    }
    variants[key](value, path);
  };
}

const rawChatShape = object({
  record_kind: literal('placeholder', 'full'),
  chat_id: nonEmptyString,
  platform_user_id: nullable(nonEmptyString),
  display_name: nullable(string),
  updated_at: nullable(isoDateTime),
});

export const rawChat: Validator = (value, path) => {
  rawChatShape(value, path);
  const candidate = record(value, path);
  if (
    candidate.record_kind === 'full' &&
    (candidate.platform_user_id === null || candidate.updated_at === null)
  ) {
    throw new ProtocolValidationError(path, 'full chat requires platform identity and updated_at');
  }
};

export const rawMessage = object({
  message_id: nonEmptyString,
  chat_id: nonEmptyString,
  sender_platform_user_id: nonEmptyString,
  text: string,
  sent_at: isoDateTime,
  direction: literal('inbound', 'outbound'),
});

export const coverageEvidence = discriminated({
  'generation.started': object({
    type: literal('generation.started'),
    generation_id: uuid,
    as_of: isoDateTime,
    authorization_revision: nonEmptyString,
  }),
  'inventory.member': object({
    type: literal('inventory.member'),
    generation_id: uuid,
    conversation_id: nonEmptyString,
  }),
  'inventory.ended': object({
    type: literal('inventory.ended'),
    generation_id: uuid,
    observed_at: isoDateTime,
  }),
  'conversation.history_started': object({
    type: literal('conversation.history_started'),
    generation_id: uuid,
    conversation_id: nonEmptyString,
    earliest_observed_at: nullable(isoDateTime),
    observed_at: isoDateTime,
  }),
  'conversation.head_reconciled': object({
    type: literal('conversation.head_reconciled'),
    generation_id: uuid,
    conversation_id: nonEmptyString,
    reconciled_through: isoDateTime,
  }),
  'generation.closed': object({
    type: literal('generation.closed'),
    generation_id: uuid,
    closed_at: isoDateTime,
  }),
});

export const rawIngestChange = discriminated({
  'chat.upsert': object({ type: literal('chat.upsert'), chat: rawChat }),
  'chat.delete': object({ type: literal('chat.delete'), chat_id: nonEmptyString }),
  'message.upsert': object({ type: literal('message.upsert'), message: rawMessage }),
  'message.delete': object({ type: literal('message.delete'), message_id: nonEmptyString, chat_id: nonEmptyString }),
  'coverage.observed': object({ type: literal('coverage.observed'), evidence: coverageEvidence }),
});

export const messageView = object({
  message_id: nonEmptyString,
  text: string,
  sent_at: isoDateTime,
  direction: literal('inbound', 'outbound'),
  sentiment: literal('positive', 'neutral', 'negative', 'unknown'),
});

export const conversationCoverage = object({
  status: literal('unknown', 'partial', 'complete'),
  boundary: nullable(literal('history_start')),
  earliest_available_at: nullable(isoDateTime),
  latest_acquired_at: nullable(isoDateTime),
  data_as_of: nullable(isoDateTime),
  reason_code: nullable(string),
});

export const conversationSummary = object({
  conversation_id: nonEmptyString,
  platform_user_id: nullable(nonEmptyString),
  display_name: nullable(string),
  unread_count: integer(0),
  last_message_at: nullable(isoDateTime),
  latest_message: nullable(messageView),
  coverage: conversationCoverage,
});

export const historicalCoverage = object({
  status: literal('unknown', 'partial', 'complete'),
  phase: literal(
    'not_started',
    'discovering',
    'backfilling',
    'paused',
    'repairing',
    'blocked',
    'complete',
  ),
  generation_id: nullable(uuid),
  as_of: nullable(isoDateTime),
  discovered_conversations: nullable(integer(0)),
  complete_conversations: integer(0),
  complete_as_of: nullable(isoDateTime),
  reason: nullable(string),
});

export const projectionState = object({
  status: literal('pending', 'current', 'degraded', 'unavailable'),
  canonical_revision: integer(0),
  projected_revision: integer(0),
  projected_at: nullable(isoDateTime),
  reason: nullable(string),
});

export const liveFreshness = object({
  status: literal('current', 'delayed', 'unknown'),
  last_observed_at: nullable(isoDateTime),
  last_committed_at: nullable(isoDateTime),
  expires_at: nullable(isoDateTime),
  pending_count: nullable(integer(0)),
  reason: nullable(string),
});

export const analyticsRange = object({
  start: nullable(isoDateTime),
  end: nullable(isoDateTime),
});

export const analyticsMetric = object({
  value: nullable(integer(0)),
  basis: literal('complete', 'synced_subset'),
  observed_range: analyticsRange,
  complete_range: nullable(analyticsRange),
  sample_size: integer(0),
  as_of: isoDateTime,
  projection_revision: integer(0),
});

export const analyticsView = object({
  total_conversations: analyticsMetric,
  total_messages: analyticsMetric,
  inbound_messages: analyticsMetric,
  outbound_messages: analyticsMetric,
});

export const stateChange = discriminated({
  'conversation.upsert': object({
    type: literal('conversation.upsert'),
    conversation: conversationSummary,
  }),
  'conversation.delete': object({
    type: literal('conversation.delete'),
    conversation_id: nonEmptyString,
  }),
  'conversation.coverage.replace': object({
    type: literal('conversation.coverage.replace'),
    conversation_id: nonEmptyString,
    coverage: conversationCoverage,
  }),
  'message.tail.upsert': object({
    type: literal('message.tail.upsert'),
    conversation_id: nonEmptyString,
    message: messageView,
  }),
  'message.tail.delete': object({
    type: literal('message.tail.delete'),
    conversation_id: nonEmptyString,
    message_id: nonEmptyString,
  }),
  'analytics.replace': object({ type: literal('analytics.replace'), analytics: analyticsView }),
  'coverage.replace': object({ type: literal('coverage.replace'), coverage: historicalCoverage }),
  'projection.replace': object({ type: literal('projection.replace'), projection: projectionState }),
  'live_freshness.replace': object({
    type: literal('live_freshness.replace'),
    live_freshness: liveFreshness,
  }),
});
