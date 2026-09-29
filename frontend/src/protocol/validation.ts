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

const checkCounts = object({list: integer(0), messages: integer(0), probes: integer(0)});
const checkHead = object({chat_id: nonEmptyString}, {head_message_id: nullable(nonEmptyString), head_sent_at: nullable(isoDateTime)});

export const coverageEvidence = discriminated({
  'check.chat_reconciled': object({
    type: literal('check.chat_reconciled'), generation_id: uuid, chat_id: nonEmptyString,
    target_head: object({}, {message_id: nullable(nonEmptyString), sent_at: nullable(isoDateTime)}),
    reached: literal('boundary', 'history_start'), final_source_seq: integer(0),
  }),
  'check.inventory_closed': object({
    type: literal('check.inventory_closed'), generation_id: uuid,
    strategy: literal('timestamp', 'probe', 'mixed'), scanned: integer(0), changed: integer(0), movers: integer(0),
  }),
  'check.completed': discriminatedBy('kind', {
    catch_up: object({type: literal('check.completed'), generation_id: uuid, kind: literal('catch_up'),
      final_source_seq: integer(0), pages_read: integer(0), counts: checkCounts}),
    canary: object({type: literal('check.completed'), generation_id: uuid, kind: literal('canary'),
      final_source_seq: integer(0), pages_read: integer(0), counts: checkCounts}, {heads: array(checkHead, 0, 100)}),
  }),
  'check.abandoned': object({type: literal('check.abandoned'), generation_id: uuid,
    reason: literal('account_changed', 'authorization_changed', 'paused', 'cursor_invalid', 'retry_exhausted', 'storage_lost')}),

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

export const catchupFreshness = object({
  status: literal('paused', 'checking', 'never_checked', 'behind', 'current'),
  reason: nullable(literal('user_paused', 'consent_needed', 'extension_offline', 'no_onlyfans_tab',
    'onlyfans_sleeping', 'account_changed', 'applying_settings', 'capture_off', 'extension_outdated',
    'catch_up', 'canary', 'awaiting_check', 'daily_cap', 'check_incomplete', 'not_observing')),
  gap_epoch: integer(0), uncertain_since: nullable(isoDateTime), check_id: nullable(uuid),
  last_closed_at: nullable(isoDateTime), observing_since: nullable(isoDateTime), evaluated_at: isoDateTime,
});

export const stateChange = discriminated({
  'catchup_freshness.replace': object({type: literal('catchup_freshness.replace'), catchup_freshness: catchupFreshness}),
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

const utcDay: Validator = (value, path) => {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)
      || Number.isNaN(Date.parse(value)) || new Date(value).toISOString().slice(0, 10) !== value) {
    throw new ProtocolValidationError(path, 'expected UTC calendar day');
  }
};

const catchupAuth = {protocol_version: literal('2'), auth_ticket: nonEmptyString,
  agent_installation_id: uuid, creator_account_id: nonEmptyString};
export const captureStateReportRequest = object({
  ...catchupAuth, operation: literal('capture.state.report'), worker_instance_id: uuid,
  report_seq: integer(1), observing: boolean,
  reason: literal('ok', 'capture_off', 'consent_needed', 'no_onlyfans_tab', 'tab_frozen', 'tab_discarded',
    'hook_not_armed', 'reload_required', 'page_socket_closed', 'account_mismatch', 'storage_locked', 'paused'),
  tabs: object({armed: integer(0), frozen: integer(0), discarded: integer(0)}), page_socket_open: boolean,
  drops_since_last: object({expired: integer(0), rejected: integer(0)}),
  requests_since_last: object({canary_list: integer(0), catchup_list: integer(0), catchup_messages: integer(0),
    history_list: integer(0), history_messages: integer(0), identity: integer(0), retries: integer(0)}),
  utc_day: utcDay, automatic_pages_today: integer(0),
});
export const captureStateReportResponse = object({acknowledged_seq: integer(1)});
export const historyCheckBeginRequest = object({
  ...catchupAuth, operation: literal('history.check.begin'), request_id: uuid, worker_instance_id: uuid,
  trigger: literal('admission', 'tab_runnable', 'observing', 'alarm', 'renew'), config_revision: nonEmptyString,
  head_evidence: literal('none', 'timestamp', 'full'), active_check_id: nullable(uuid),
});
export const historyCheckBeginResponse = discriminatedBy('result', {
  not_needed: object({result: literal('not_needed')}),
  deferred: object({result: literal('deferred'), retry_after_seconds: integer(1),
    reason: literal('grant_interval', 'daily_cap', 'not_runnable', 'history_incomplete', 'check_active')}),
  granted: object({result: literal('granted'), check_id: uuid, kind: literal('catch_up', 'canary'), gap_epoch: integer(0),
    uncertain_since: nullable(isoDateTime), granted_at: isoDateTime, blind: boolean, page_budget: integer(1),
    lease_expires_at: isoDateTime, resume: boolean}),
});
