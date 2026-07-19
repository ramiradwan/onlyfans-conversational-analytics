export class ProtocolValidationError extends Error {
  constructor(path, message) {
    super(`${path}: ${message}`);
    this.name = 'ProtocolValidationError';
  }
}

function record(value, path) {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new ProtocolValidationError(path, 'expected object');
  }
  return value;
}

export function object(required, optional = {}) {
  return (value, path) => {
    const candidate = record(value, path);
    const allowed = new Set([...Object.keys(required), ...Object.keys(optional)]);
    for (const key of Object.keys(candidate)) {
      if (!allowed.has(key)) throw new ProtocolValidationError(`${path}.${key}`, 'unknown field');
    }
    for (const [key, validator] of Object.entries(required)) {
      if (!Object.hasOwn(candidate, key)) {
        throw new ProtocolValidationError(`${path}.${key}`, 'missing required field');
      }
      validator(candidate[key], `${path}.${key}`);
    }
    for (const [key, validator] of Object.entries(optional)) {
      if (Object.hasOwn(candidate, key)) validator(candidate[key], `${path}.${key}`);
    }
  };
}

export const nonEmptyString = (value, path) => {
  if (typeof value !== 'string' || value.length === 0) {
    throw new ProtocolValidationError(path, 'expected non-empty string');
  }
};

export const string = (value, path) => {
  if (typeof value !== 'string') throw new ProtocolValidationError(path, 'expected string');
};

export const boolean = (value, path) => {
  if (typeof value !== 'boolean') throw new ProtocolValidationError(path, 'expected boolean');
};

export function integer(minimum = Number.MIN_SAFE_INTEGER, maximum = Number.MAX_SAFE_INTEGER) {
  return (value, path) => {
    if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
      throw new ProtocolValidationError(path, `expected integer from ${minimum} through ${maximum}`);
    }
  };
}

export function literal(...allowed) {
  return (value, path) => {
    if (!allowed.includes(value)) {
      throw new ProtocolValidationError(path, `expected one of ${allowed.join(', ')}`);
    }
  };
}

export function nullable(validator) {
  return (value, path) => {
    if (value !== null) validator(value, path);
  };
}

export function array(validator, minimumLength = 0, maximumLength = Number.MAX_SAFE_INTEGER) {
  return (value, path) => {
    if (
      !Array.isArray(value)
      || value.length < minimumLength
      || value.length > maximumLength
    ) {
      throw new ProtocolValidationError(
        path,
        `expected array with ${minimumLength} through ${maximumLength} item(s)`,
      );
    }
    value.forEach((item, index) => validator(item, `${path}[${index}]`));
  };
}

export const uuid = (value, path) => {
  if (typeof value !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value)) {
    throw new ProtocolValidationError(path, 'expected UUID');
  }
};

export const isoDateTime = (value, path) => {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value) || Number.isNaN(Date.parse(value))) {
    throw new ProtocolValidationError(path, 'expected ISO 8601 datetime with timezone');
  }
};

export const digest = (value, path) => {
  if (typeof value !== 'string' || !/^sha256:[0-9a-f]{64}$/.test(value)) {
    throw new ProtocolValidationError(path, 'expected sha256 digest');
  }
};

export function discriminated(variants) {
  return (value, path) => {
    const candidate = record(value, path);
    if (typeof candidate.type !== 'string' || !(candidate.type in variants)) {
      throw new ProtocolValidationError(`${path}.type`, 'unknown discriminator');
    }
    variants[candidate.type](value, path);
  };
}

export function discriminatedBy(discriminator, variants) {
  return (value, path) => {
    const candidate = record(value, path);
    const variant = String(candidate[discriminator]);
    if (!Object.hasOwn(variants, variant)) {
      throw new ProtocolValidationError(`${path}.${discriminator}`, 'unknown discriminator');
    }
    variants[variant](value, path);
  };
}

const fullChat = object({
  chat_id: nonEmptyString,
  record_kind: literal('full'),
  platform_user_id: nonEmptyString,
  display_name: nullable(string),
  updated_at: isoDateTime,
});

const placeholderChat = object({
  chat_id: nonEmptyString,
  record_kind: literal('placeholder'),
  platform_user_id: nullable(nonEmptyString),
  display_name: nullable(string),
  updated_at: nullable(isoDateTime),
});

export const rawChat = discriminatedBy('record_kind', {
  full: fullChat,
  placeholder: placeholderChat,
});

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

export const captureRule = object({ resource: literal('chats', 'messages', 'presence'), url_pattern: nonEmptyString, enabled: boolean });
export const capturePolicy = object({ observation_interval_seconds: integer(5, 3600), rules: array(captureRule, 1) });
export const commandPolicy = object({ allowed_actions: array(literal('message.send')), max_text_length: integer(1), require_idempotency: boolean });
const historyAcquisitionShape = object({
  enabled: boolean,
  consent_revision: nullable(nonEmptyString),
  authorized_platform_creator_id: nullable(nonEmptyString),
  recent_window_days: integer(1, 365),
  page_size: integer(1, 100),
  pages_per_wake: integer(1),
  request_interval_ms: integer(0),
  retry_limit: integer(0),
});
export const historyAcquisition = (value, path) => {
  historyAcquisitionShape(value, path);
  const candidate = record(value, path);
  if (
    candidate.enabled === true
    && (candidate.consent_revision === null || candidate.authorized_platform_creator_id === null)
  ) {
    throw new ProtocolValidationError(
      path,
      'enabled history acquisition requires consent_revision and authorized_platform_creator_id',
    );
  }
};
