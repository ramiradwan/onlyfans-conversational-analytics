import {
  ProtocolValidationError,
  array,
  boolean,
  capturePolicy,
  commandPolicy,
  digest,
  discriminated,
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
  capability: literal('capture.chats', 'capture.messages', 'capture.presence', 'command.message.send'),
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

const payloadValidators = {
  'agent.hello': object({
    auth_ticket: nonEmptyString,
    agent_installation_id: uuid,
    requested_creator_account_id: nonEmptyString,
    capabilities: array(literal('capture.chats', 'capture.messages', 'capture.presence', 'command.message.send'), 1),
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
    snapshot: object({ include_chats: literal(true), include_messages: literal(true) }),
  }),
  'ingest.snapshot': object({
    connection_id: uuid,
    fencing_token: nonEmptyString,
    creator_account_id: nonEmptyString,
    agent_installation_id: uuid,
    snapshot_id: uuid,
    agent_stream_id: uuid,
    through_seq: integer(0),
    chats: array(rawChat),
    messages: array(rawMessage),
  }),
  'ingest.delta': object({
    connection_id: uuid,
    fencing_token: nonEmptyString,
    creator_account_id: nonEmptyString,
    agent_installation_id: uuid,
    event_id: uuid,
    agent_stream_id: uuid,
    source_seq: integer(1),
    change: rawIngestChange,
  }),
  'ingest.ack': object({
    connection_id: uuid,
    creator_account_id: nonEmptyString,
    agent_stream_id: uuid,
    snapshot_id: nullable(uuid),
    committed_source_seq: integer(0),
  }),
  'ingest.rejected': object({
    connection_id: uuid,
    creator_account_id: nonEmptyString,
    rejected_message_id: uuid,
    event_id: nullable(uuid),
    code: literal('invalid_payload', 'identity_conflict', 'stale_fence', 'sequence_gap', 'invariant_failed'),
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
    { type: literal(type), protocol_version: literal('1'), message_id: uuid, payload: payloadValidators[type] },
    { correlation_id: nullable(uuid) },
  )(value, '$');
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
  protocol_version: literal('1'),
  auth_ticket: nonEmptyString,
  agent_installation_id: uuid,
  creator_account_id: nonEmptyString,
  current_etag: nullable(string),
  current_config_revision: nullable(string),
  supported_config_schema_versions: array(literal('1'), 1),
});

const configDocumentResponse = object({
  operation: literal('agent.config.document'),
  protocol_version: literal('1'),
  creator_account_id: nonEmptyString,
  config_revision: nonEmptyString,
  config_schema_version: literal('1'),
  digest,
  etag: nonEmptyString,
  issued_at: isoDateTime,
  capture_policy: capturePolicy,
  command_policy: commandPolicy,
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
