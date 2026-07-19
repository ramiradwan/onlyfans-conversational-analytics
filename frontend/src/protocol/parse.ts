import type {
  AgentConfigDocumentResponse,
  AgentConfigGetRequest,
  AgentToBrainMessage,
  BrainToAgentMessage,
  BrainToBridgeMessage,
  BridgeToBrainMessage,
} from './types';
import {
  ProtocolValidationError,
  analyticsView,
  array,
  boolean,
  conversationSummary,
  coverageEvidence,
  digest,
  discriminated,
  discriminatedBy,
  historicalCoverage,
  integer,
  isoDateTime,
  literal,
  liveFreshness,
  nonEmptyString,
  nullable,
  object,
  projectionState,
  rawChat,
  rawIngestChange,
  rawMessage,
  stateChange,
  string,
  uuid,
  type Validator,
} from './validation';

const agentCapability = literal(
  'capture.chats',
  'capture.messages',
  'capture.presence',
  'history.sync',
  'command.message.send',
);
const bridgeCapability = literal(
  'state.snapshot',
  'state.delta',
  'presence.state',
  'message.page',
);

const healthSummary = object({
  status: literal('healthy', 'degraded'),
  detail: nullable(string),
});
const lastPresenceObservation = object({
  observation_id: integer(0),
  observed_at: isoDateTime,
});
const capabilityStatus = object({
  capability: agentCapability,
  status: literal('active', 'degraded', 'unsupported'),
  detail: nullable(string),
});
const commandAction = discriminated({
  'message.send': object({
    type: literal('message.send'),
    conversation_id: nonEmptyString,
    text: nonEmptyString,
    media_url: nullable(string),
  }),
});
const commandOutput = object({ external_message_id: nullable(string) });
const commandError = object({
  code: literal('rejected', 'deadline_exceeded', 'platform_error', 'execution_error'),
  detail: nonEmptyString,
  retryable: boolean,
});

const snapshotIdentity = {
  connection_id: uuid,
  fencing_token: nonEmptyString,
  creator_account_id: nonEmptyString,
  agent_installation_id: uuid,
  agent_stream_id: uuid,
  snapshot_id: uuid,
};
const snapshotChatRecord = discriminatedBy('tombstone', {
  false: object({ tombstone: literal(false), chat: rawChat }),
  true: object({ tombstone: literal(true), chat_id: nonEmptyString }),
});
const snapshotMessageRecord = discriminatedBy('tombstone', {
  false: object({ tombstone: literal(false), message: rawMessage }),
  true: object({
    tombstone: literal(true),
    message_id: nonEmptyString,
    chat_id: nonEmptyString,
  }),
});
const snapshotChunkShape = discriminatedBy('entity_kind', {
  chat: object({
    ...snapshotIdentity,
    frame_kind: literal('chunk'),
    chunk_index: integer(0),
    entity_kind: literal('chat'),
    records: array(snapshotChatRecord, 1, 100),
  }),
  message: object({
    ...snapshotIdentity,
    frame_kind: literal('chunk'),
    chunk_index: integer(0),
    entity_kind: literal('message'),
    records: array(snapshotMessageRecord, 1, 100),
  }),
  coverage_evidence: object({
    ...snapshotIdentity,
    frame_kind: literal('chunk'),
    chunk_index: integer(0),
    entity_kind: literal('coverage_evidence'),
    records: array(coverageEvidence, 1, 100),
  }),
});
const snapshotChunk: Validator = (value, path) => {
  snapshotChunkShape(value, path);
  const records = (value as { records: unknown[] }).records;
  records.forEach((record, index) => {
    if (new TextEncoder().encode(JSON.stringify(record)).byteLength > 384 * 1024) {
      throw new ProtocolValidationError(
        `${path}.records[${index}]`,
        'snapshot record exceeds 384 KiB',
      );
    }
  });
};
const ingestSnapshot = discriminatedBy('frame_kind', {
  begin: object({
    ...snapshotIdentity,
    frame_kind: literal('begin'),
    through_seq: integer(0),
    chunk_count: integer(0),
    record_counts: object({
      chats: integer(0),
      messages: integer(0),
      coverage_evidence: integer(0),
    }),
    max_frame_bytes: literal(524288),
  }),
  chunk: snapshotChunk,
  commit: object({
    ...snapshotIdentity,
    frame_kind: literal('commit'),
    chunk_count: integer(0),
  }),
});

const messagePayloadValidators: Record<string, Validator> = {
  'agent.hello': object({
    auth_ticket: nonEmptyString,
    agent_installation_id: uuid,
    requested_creator_account_id: nonEmptyString,
    capabilities: array(agentCapability, 1),
    extension_version: nonEmptyString,
    agent_stream_id: uuid,
    last_acknowledged_source_seq: integer(0),
    applied_config_revision: nullable(string),
  }),
  'agent.session': object({
    connection_id: uuid,
    fencing_token: nonEmptyString,
    creator_account_id: nonEmptyString,
    agent_installation_id: uuid,
    agent_stream_id: uuid,
    committed_source_seq: integer(0),
    resume_action: literal('resume', 'snapshot_required'),
    pending_snapshot_id: nullable(uuid),
    next_expected_chunk_index: integer(0),
    required_config_revision: nonEmptyString,
    reconnect_auth_ticket: nonEmptyString,
    config_auth_ticket: nonEmptyString,
    lease: object({
      heartbeat_interval_seconds: integer(1, 300),
      lease_timeout_seconds: integer(1, 900),
    }),
  }),
  'bridge.hello': object({
    auth_ticket: nonEmptyString,
    bridge_session_id: uuid,
    requested_creator_account_id: nonEmptyString,
    capabilities: array(bridgeCapability, 1),
    client_version: nonEmptyString,
    last_view_revision: nullable(integer(0)),
  }),
  'bridge.session': object({
    connection_id: uuid,
    bridge_session_id: uuid,
    creator_account_id: nonEmptyString,
    negotiated_protocol_version: literal('2'),
    server_version: nonEmptyString,
  }),
  'agent.heartbeat': object({
    connection_id: uuid,
    fencing_token: nonEmptyString,
    creator_account_id: nonEmptyString,
    applied_config_revision: nullable(string),
    health: healthSummary,
  }),
  'sync.required': object({
    connection_id: uuid,
    creator_account_id: nonEmptyString,
    reason: literal(
      'unknown_stream',
      'missing_checkpoint',
      'sequence_gap',
      'local_reset',
      'invariant_failed',
    ),
    expected_agent_stream_id: nullable(uuid),
    expected_next_source_seq: integer(0),
    pending_snapshot_id: nullable(uuid),
    next_expected_chunk_index: integer(0),
    snapshot: object({
      include_chats: literal(true),
      include_messages: literal(true),
      include_coverage_evidence: literal(true),
      max_records_per_chunk: literal(100),
      max_frame_bytes: literal(524288),
    }),
  }),
  'ingest.snapshot': ingestSnapshot,
  'ingest.delta': object({
    connection_id: uuid,
    fencing_token: nonEmptyString,
    creator_account_id: nonEmptyString,
    agent_installation_id: uuid,
    event_id: uuid,
    agent_stream_id: uuid,
    source_seq: integer(1),
    acquisition_origin: literal('passive', 'signer'),
    change: rawIngestChange,
  }),
  'ingest.ack': object({
    connection_id: uuid,
    creator_account_id: nonEmptyString,
    agent_stream_id: uuid,
    snapshot_id: nullable(uuid),
    committed_source_seq: integer(0),
    snapshot_progress: nullable(
      object({
        snapshot_id: uuid,
        next_expected_chunk_index: integer(0),
        committed: boolean,
      }),
    ),
  }),
  'ingest.rejected': object({
    connection_id: uuid,
    creator_account_id: nonEmptyString,
    rejected_message_id: uuid,
    event_id: nullable(uuid),
    code: literal(
      'invalid_payload',
      'identity_conflict',
      'stale_fence',
      'sequence_gap',
      'invariant_failed',
      'chunk_conflict',
      'snapshot_incomplete',
      'frame_too_large',
    ),
    retryable: boolean,
    detail: nonEmptyString,
  }),
  'state.snapshot': object({
    creator_account_id: nonEmptyString,
    view_revision: integer(0),
    generated_at: isoDateTime,
    conversations: array(conversationSummary),
    analytics: analyticsView,
    coverage: historicalCoverage,
    projection: projectionState,
    live_freshness: liveFreshness,
  }),
  'state.delta': object({
    creator_account_id: nonEmptyString,
    view_revision: integer(1),
    committed_at: isoDateTime,
    changes: array(stateChange, 1),
  }),
  'state.resync': object({
    connection_id: uuid,
    bridge_session_id: uuid,
    creator_account_id: nonEmptyString,
    last_applied_view_revision: integer(0),
    reason: literal('revision_gap', 'invalid_delta', 'reconnect', 'manual'),
  }),
  'presence.observed': object({
    connection_id: uuid,
    fencing_token: nonEmptyString,
    creator_account_id: nonEmptyString,
    observation_id: integer(0),
    observed_at: isoDateTime,
    online_platform_user_ids: array(nonEmptyString),
  }),
  'presence.state': object({
    creator_account_id: nonEmptyString,
    freshness: literal('current', 'unknown'),
    online_platform_user_ids: array(nonEmptyString),
    server_received_at: nullable(isoDateTime),
    expires_at: nullable(isoDateTime),
    last_observation: nullable(lastPresenceObservation),
  }),
  'agent.state': object({
    creator_account_id: nonEmptyString,
    status: literal('connected', 'stale', 'disconnected'),
    agent_installation_id: nullable(uuid),
    connection_id: nullable(uuid),
    required_config_revision: nonEmptyString,
    applied_config_revision: nullable(string),
    required_history_settings_revision: integer(0),
    applied_history_settings_revision: nullable(integer(0)),
    last_heartbeat_at: nullable(isoDateTime),
    degraded_reason: nullable(string),
  }),
  'system.state': object({
    creator_account_id: nonEmptyString,
    processing_mode: literal('processing_snapshot', 'realtime', 'resyncing'),
    readiness: literal('ready', 'degraded', 'unavailable'),
    updated_at: isoDateTime,
    detail: nullable(string),
  }),
  'protocol.error': object({
    code: literal(
      'unsupported_version',
      'wrong_role',
      'pre_handshake',
      'identity_conflict',
      'validation_failed',
      'unauthorized',
      'internal_error',
    ),
    related_message_id: nullable(uuid),
    retryable: boolean,
    fatal: boolean,
    detail: nonEmptyString,
  }),
  'config.available': object({
    connection_id: uuid,
    creator_account_id: nonEmptyString,
    required_config_revision: nonEmptyString,
    digest,
  }),
  'config.applied': object({
    connection_id: uuid,
    fencing_token: nonEmptyString,
    creator_account_id: nonEmptyString,
    config_revision: nonEmptyString,
    digest,
    outcome: literal('applied', 'degraded', 'rejected'),
    capabilities: array(capabilityStatus, 1),
  }),
  'command.execute': object({
    connection_id: uuid,
    fencing_token: nonEmptyString,
    creator_account_id: nonEmptyString,
    command_id: uuid,
    deadline: isoDateTime,
    idempotency_policy: literal('deduplicate'),
    action: commandAction,
  }),
  'command.result': object({
    connection_id: uuid,
    fencing_token: nonEmptyString,
    creator_account_id: nonEmptyString,
    command_id: uuid,
    result_id: uuid,
    status: literal('accepted', 'succeeded', 'failed'),
    completed_at: isoDateTime,
    output: nullable(commandOutput),
    error: nullable(commandError),
  }),
  'command.result.ack': object({
    connection_id: uuid,
    creator_account_id: nonEmptyString,
    command_id: uuid,
    result_id: uuid,
    recorded_at: isoDateTime,
  }),
};

const agentToBrainTypes = new Set([
  'agent.hello',
  'agent.heartbeat',
  'ingest.snapshot',
  'ingest.delta',
  'presence.observed',
  'config.applied',
  'command.result',
]);
const brainToAgentTypes = new Set([
  'agent.session',
  'sync.required',
  'ingest.ack',
  'ingest.rejected',
  'protocol.error',
  'config.available',
  'command.execute',
  'command.result.ack',
]);
const bridgeToBrainTypes = new Set(['bridge.hello', 'state.resync']);
const brainToBridgeTypes = new Set([
  'bridge.session',
  'state.snapshot',
  'state.delta',
  'presence.state',
  'agent.state',
  'system.state',
  'protocol.error',
]);

function parseDirectional<T>(value: unknown, allowedTypes: ReadonlySet<string>): T {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new ProtocolValidationError('$', 'expected object');
  }
  const type = (value as Record<string, unknown>).type;
  if (
    typeof type !== 'string' ||
    !allowedTypes.has(type) ||
    !(type in messagePayloadValidators)
  ) {
    throw new ProtocolValidationError('$.type', 'message is not allowed in this direction');
  }
  object(
    {
      type: literal(type),
      protocol_version: literal('2'),
      message_id: uuid,
      payload: messagePayloadValidators[type],
    },
    { correlation_id: nullable(uuid) },
  )(value, '$');
  if (
    type === 'ingest.snapshot' &&
    new TextEncoder().encode(JSON.stringify(value)).byteLength > 512 * 1024
  ) {
    throw new ProtocolValidationError('$', 'snapshot frame exceeds 512 KiB');
  }
  return value as T;
}

function isParsedBy<T>(parser: (value: unknown) => T, value: unknown): value is T {
  try {
    parser(value);
    return true;
  } catch (error) {
    if (error instanceof ProtocolValidationError) return false;
    throw error;
  }
}

export const parseAgentToBrainMessage = (value: unknown): AgentToBrainMessage =>
  parseDirectional(value, agentToBrainTypes);
export const parseBrainToAgentMessage = (value: unknown): BrainToAgentMessage =>
  parseDirectional(value, brainToAgentTypes);
export const parseBridgeToBrainMessage = (value: unknown): BridgeToBrainMessage =>
  parseDirectional(value, bridgeToBrainTypes);
export const parseBrainToBridgeMessage = (value: unknown): BrainToBridgeMessage =>
  parseDirectional(value, brainToBridgeTypes);

export const isAgentToBrainMessage = (value: unknown): value is AgentToBrainMessage =>
  isParsedBy(parseAgentToBrainMessage, value);
export const isBrainToAgentMessage = (value: unknown): value is BrainToAgentMessage =>
  isParsedBy(parseBrainToAgentMessage, value);
export const isBridgeToBrainMessage = (value: unknown): value is BridgeToBrainMessage =>
  isParsedBy(parseBridgeToBrainMessage, value);
export const isBrainToBridgeMessage = (value: unknown): value is BrainToBridgeMessage =>
  isParsedBy(parseBrainToBridgeMessage, value);

const captureRule = object({
  resource: literal('chats', 'messages', 'presence'),
  url_pattern: nonEmptyString,
  enabled: boolean,
});
const capturePolicy = object({
  observation_interval_seconds: integer(5, 3600),
  rules: array(captureRule, 1),
});
const commandPolicy = object({
  allowed_actions: array(literal('message.send')),
  max_text_length: integer(1),
  require_idempotency: boolean,
});
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
const historyAcquisition: Validator = (value, path) => {
  historyAcquisitionShape(value, path);
  const candidate = value as Record<string, unknown>;
  if (
    candidate.enabled === true &&
    (candidate.consent_revision === null || candidate.authorized_platform_creator_id === null)
  ) {
    throw new ProtocolValidationError(path, 'enabled history acquisition requires authorization');
  }
};
const configGetRequest = object({
  operation: literal('agent.config.get'),
  protocol_version: literal('2'),
  auth_ticket: nonEmptyString,
  agent_installation_id: uuid,
  creator_account_id: nonEmptyString,
  current_etag: nullable(string),
  current_config_revision: nullable(string),
  supported_config_schema_versions: array(literal('2'), 1),
});
const configDocumentResponse = object({
  operation: literal('agent.config.document'),
  protocol_version: literal('2'),
  creator_account_id: nonEmptyString,
  config_revision: nonEmptyString,
  config_schema_version: literal('2'),
  digest,
  etag: nonEmptyString,
  issued_at: isoDateTime,
  capture_policy: capturePolicy,
  command_policy: commandPolicy,
  history_acquisition: historyAcquisition,
});

export function parseAgentConfigGetRequest(value: unknown): AgentConfigGetRequest {
  configGetRequest(value, '$');
  return value as AgentConfigGetRequest;
}

export function parseAgentConfigDocumentResponse(value: unknown): AgentConfigDocumentResponse {
  configDocumentResponse(value, '$');
  return value as AgentConfigDocumentResponse;
}

export const isAgentConfigGetRequest = (value: unknown): value is AgentConfigGetRequest =>
  isParsedBy(parseAgentConfigGetRequest, value);
export const isAgentConfigDocumentResponse = (
  value: unknown,
): value is AgentConfigDocumentResponse =>
  isParsedBy(parseAgentConfigDocumentResponse, value);
