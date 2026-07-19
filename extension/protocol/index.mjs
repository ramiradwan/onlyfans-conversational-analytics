import {
  ProtocolValidationError,
  array,
  boolean,
  capturePolicy,
  commandPolicy,
  coverageEvidence,
  digest,
  discriminated,
  discriminatedBy,
  historyAcquisition,
  integer,
  isoDateTime,
  literal,
  nonEmptyString,
  nullable,
  object,
  rawChat,
  rawIngestChange,
  rawMessage,
  string,
  uuid,
} from './validation.mjs';

const healthSummary = object({ status: literal('healthy', 'degraded'), detail: nullable(string) });
const capabilityStatus = object({
  capability: literal(
    'capture.chats',
    'capture.messages',
    'capture.presence',
    'history.sync',
    'command.message.send',
  ),
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
  snapshot_id: uuid,
  agent_stream_id: uuid,
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
const anyValue = () => {};
const snapshotChunkBase = object({
  ...snapshotIdentity,
  frame_kind: literal('chunk'),
  chunk_index: integer(0),
  entity_kind: literal('chat', 'message', 'coverage_evidence'),
  records: array(anyValue, 1, 100),
});
const snapshotFrame = discriminatedBy('frame_kind', {
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
    max_frame_bytes: literal(524_288),
  }),
  chunk: (value, path) => {
    snapshotChunkBase(value, path);
    const validators = {
      chat: snapshotChatRecord,
      message: snapshotMessageRecord,
      coverage_evidence: coverageEvidence,
    };
    value.records.forEach((record, index) => {
      validators[value.entity_kind](record, `${path}.records[${index}]`);
      if (new TextEncoder().encode(JSON.stringify(record)).byteLength > 393_216) {
        throw new ProtocolValidationError(
          `${path}.records[${index}]`,
          'normalized snapshot record exceeds 384 KiB',
        );
      }
    });
  },
  commit: object({
    ...snapshotIdentity,
    frame_kind: literal('commit'),
    chunk_count: integer(0),
  }),
});

const payloadValidators = {
  'agent.hello': object({
    auth_ticket: nonEmptyString,
    agent_installation_id: uuid,
    requested_creator_account_id: nonEmptyString,
    capabilities: array(literal(
      'capture.chats',
      'capture.messages',
      'capture.presence',
      'history.sync',
      'command.message.send',
    ), 1),
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
    required_config_revision: nonEmptyString,
    reconnect_auth_ticket: nonEmptyString,
    config_auth_ticket: nonEmptyString,
    pending_snapshot_id: nullable(uuid),
    next_expected_chunk_index: integer(0),
    lease: object({ heartbeat_interval_seconds: integer(1, 300), lease_timeout_seconds: integer(1, 900) }),
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
    reason: literal('unknown_stream', 'missing_checkpoint', 'sequence_gap', 'local_reset', 'invariant_failed'),
    expected_agent_stream_id: nullable(uuid),
    expected_next_source_seq: integer(0),
    pending_snapshot_id: nullable(uuid),
    next_expected_chunk_index: integer(0),
    snapshot: object({
      include_chats: literal(true),
      include_messages: literal(true),
      include_coverage_evidence: literal(true),
      max_frame_bytes: literal(524_288),
      max_records_per_chunk: literal(100),
    }),
  }),
  'ingest.snapshot': snapshotFrame,
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
    snapshot_progress: nullable(object({
      snapshot_id: uuid,
      next_expected_chunk_index: integer(0),
      committed: boolean,
    })),
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
  'presence.observed': object({
    connection_id: uuid,
    fencing_token: nonEmptyString,
    creator_account_id: nonEmptyString,
    observation_id: integer(0),
    observed_at: isoDateTime,
    online_platform_user_ids: array(nonEmptyString),
  }),
  'protocol.error': object({
    code: literal('unsupported_version', 'wrong_role', 'pre_handshake', 'identity_conflict', 'validation_failed', 'unauthorized', 'internal_error'),
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

const agentToBrainTypes = new Set(['agent.hello', 'agent.heartbeat', 'ingest.snapshot', 'ingest.delta', 'presence.observed', 'config.applied', 'command.result']);
const brainToAgentTypes = new Set(['agent.session', 'sync.required', 'ingest.ack', 'ingest.rejected', 'protocol.error', 'config.available', 'command.execute', 'command.result.ack']);

function parseDirectional(value, allowedTypes) {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new ProtocolValidationError('$', 'expected object');
  }
  const type = value.type;
  if (typeof type !== 'string' || !allowedTypes.has(type) || !(type in payloadValidators)) {
    throw new ProtocolValidationError('$.type', 'message is not allowed in this direction');
  }
  object(
    { type: literal(type), protocol_version: literal('2'), message_id: uuid, payload: payloadValidators[type] },
    { correlation_id: nullable(uuid) },
  )(value, '$');
  if (
    type === 'ingest.snapshot'
    && new TextEncoder().encode(JSON.stringify(value)).byteLength > 524_288
  ) {
    throw new ProtocolValidationError('$', 'snapshot frame exceeds 512 KiB');
  }
  return value;
}

function isParsedBy(parser, value) {
  try {
    parser(value);
    return true;
  } catch (error) {
    if (error instanceof ProtocolValidationError) return false;
    throw error;
  }
}

export const parseAgentToBrainMessage = (value) => parseDirectional(value, agentToBrainTypes);
export const parseBrainToAgentMessage = (value) => parseDirectional(value, brainToAgentTypes);
export const isAgentToBrainMessage = (value) => isParsedBy(parseAgentToBrainMessage, value);
export const isBrainToAgentMessage = (value) => isParsedBy(parseBrainToAgentMessage, value);

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

export function parseAgentConfigGetRequest(value) {
  configGetRequest(value, '$');
  return value;
}

export function parseAgentConfigDocumentResponse(value) {
  configDocumentResponse(value, '$');
  return value;
}

export const isAgentConfigGetRequest = (value) => isParsedBy(parseAgentConfigGetRequest, value);
export const isAgentConfigDocumentResponse = (value) => isParsedBy(parseAgentConfigDocumentResponse, value);
export { ProtocolValidationError };
