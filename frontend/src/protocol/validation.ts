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

export function array(validator: Validator, minimumLength = 0): Validator {
  return (value, path) => {
    if (!Array.isArray(value) || value.length < minimumLength) {
      throw new ProtocolValidationError(path, `expected array with at least ${minimumLength} item(s)`);
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
  return (value, path) => {
    const candidate = record(value, path);
    const discriminator = candidate.type;
    if (typeof discriminator !== 'string' || !(discriminator in variants)) {
      throw new ProtocolValidationError(`${path}.type`, 'unknown discriminator');
    }
    variants[discriminator](value, path);
  };
}

export const rawChat = object({
  chat_id: nonEmptyString,
  platform_user_id: nonEmptyString,
  display_name: nullable(string),
  updated_at: isoDateTime,
});

export const rawMessage = object({
  message_id: nonEmptyString,
  chat_id: nonEmptyString,
  sender_platform_user_id: nonEmptyString,
  text: string,
  sent_at: isoDateTime,
  direction: literal('inbound', 'outbound'),
});

export const rawIngestChange = discriminated({
  'chat.upsert': object({ type: literal('chat.upsert'), chat: rawChat }),
  'chat.delete': object({ type: literal('chat.delete'), chat_id: nonEmptyString }),
  'message.upsert': object({ type: literal('message.upsert'), message: rawMessage }),
  'message.delete': object({ type: literal('message.delete'), message_id: nonEmptyString, chat_id: nonEmptyString }),
});

export const messageView = object({
  message_id: nonEmptyString,
  text: string,
  sent_at: isoDateTime,
  direction: literal('inbound', 'outbound'),
  sentiment: literal('positive', 'neutral', 'negative', 'unknown'),
});

export const conversationView = object({
  conversation_id: nonEmptyString,
  platform_user_id: nonEmptyString,
  display_name: nullable(string),
  unread_count: integer(0),
  last_message_at: nullable(isoDateTime),
  messages: array(messageView),
});

export const analyticsView = object({
  total_conversations: integer(0),
  total_messages: integer(0),
  inbound_messages: integer(0),
  outbound_messages: integer(0),
});

export const stateChange = discriminated({
  'conversation.upsert': object({ type: literal('conversation.upsert'), conversation: conversationView }),
  'conversation.delete': object({ type: literal('conversation.delete'), conversation_id: nonEmptyString }),
  'analytics.replace': object({ type: literal('analytics.replace'), analytics: analyticsView }),
});
