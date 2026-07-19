export type ProtocolVersion = '2';
export type UUID = string;
export type IsoDateTime = string;
export type Sha256Digest = string;

export interface RawChat {
  record_kind: 'placeholder' | 'full';
  chat_id: string;
  platform_user_id: string | null;
  display_name: string | null;
  updated_at: IsoDateTime | null;
}

export interface RawMessage {
  message_id: string;
  chat_id: string;
  sender_platform_user_id: string;
  text: string;
  sent_at: IsoDateTime;
  direction: 'inbound' | 'outbound';
}

export type CoverageEvidence =
  | {
      type: 'generation.started';
      generation_id: UUID;
      as_of: IsoDateTime;
      authorization_revision: string;
    }
  | { type: 'inventory.member'; generation_id: UUID; conversation_id: string }
  | { type: 'inventory.ended'; generation_id: UUID; observed_at: IsoDateTime }
  | {
      type: 'conversation.history_started';
      generation_id: UUID;
      conversation_id: string;
      earliest_observed_at: IsoDateTime | null;
      observed_at: IsoDateTime;
    }
  | {
      type: 'conversation.head_reconciled';
      generation_id: UUID;
      conversation_id: string;
      reconciled_through: IsoDateTime;
    }
  | { type: 'generation.closed'; generation_id: UUID; closed_at: IsoDateTime };

export type RawIngestChange =
  | { type: 'chat.upsert'; chat: RawChat }
  | { type: 'chat.delete'; chat_id: string }
  | { type: 'message.upsert'; message: RawMessage }
  | { type: 'message.delete'; message_id: string; chat_id: string }
  | { type: 'coverage.observed'; evidence: CoverageEvidence };

export interface MessageView {
  message_id: string;
  text: string;
  sent_at: IsoDateTime;
  direction: 'inbound' | 'outbound';
  sentiment: 'positive' | 'neutral' | 'negative' | 'unknown';
}

export interface ConversationCoverage {
  status: 'unknown' | 'partial' | 'complete';
  boundary: 'history_start' | null;
  earliest_available_at: IsoDateTime | null;
  latest_acquired_at: IsoDateTime | null;
  data_as_of: IsoDateTime | null;
  reason_code: string | null;
}

/** Protocol-v2 bounded conversation record. Full messages are fetched over REST. */
export interface ConversationSummary {
  conversation_id: string;
  platform_user_id: string | null;
  display_name: string | null;
  unread_count: number;
  last_message_at: IsoDateTime | null;
  latest_message: MessageView | null;
  coverage: ConversationCoverage;
}

export type ConversationRecord = ConversationSummary;

export interface HistoricalCoverage {
  status: 'unknown' | 'partial' | 'complete';
  phase:
    | 'not_started'
    | 'discovering'
    | 'backfilling'
    | 'paused'
    | 'repairing'
    | 'blocked'
    | 'complete';
  generation_id: UUID | null;
  as_of: IsoDateTime | null;
  discovered_conversations: number | null;
  complete_conversations: number;
  complete_as_of: IsoDateTime | null;
  reason: string | null;
}

export interface ProjectionState {
  status: 'pending' | 'current' | 'degraded' | 'unavailable';
  canonical_revision: number;
  projected_revision: number;
  projected_at: IsoDateTime | null;
  reason: string | null;
}

export interface LiveFreshness {
  status: 'current' | 'delayed' | 'unknown';
  last_observed_at: IsoDateTime | null;
  last_committed_at: IsoDateTime | null;
  expires_at: IsoDateTime | null;
  pending_count: number | null;
  reason: string | null;
}

export interface AnalyticsRange {
  start: IsoDateTime | null;
  end: IsoDateTime | null;
}

export interface AnalyticsMetric {
  value: number | null;
  basis: 'complete' | 'synced_subset';
  observed_range: AnalyticsRange;
  complete_range: AnalyticsRange | null;
  sample_size: number;
  as_of: IsoDateTime;
  projection_revision: number;
}

export interface AnalyticsView {
  total_conversations: AnalyticsMetric;
  total_messages: AnalyticsMetric;
  inbound_messages: AnalyticsMetric;
  outbound_messages: AnalyticsMetric;
}

export type StateChange =
  | { type: 'conversation.upsert'; conversation: ConversationSummary }
  | { type: 'conversation.delete'; conversation_id: string }
  | {
      type: 'conversation.coverage.replace';
      conversation_id: string;
      coverage: ConversationCoverage;
    }
  | { type: 'message.tail.upsert'; conversation_id: string; message: MessageView }
  | { type: 'message.tail.delete'; conversation_id: string; message_id: string }
  | { type: 'analytics.replace'; analytics: AnalyticsView }
  | { type: 'coverage.replace'; coverage: HistoricalCoverage }
  | { type: 'projection.replace'; projection: ProjectionState }
  | { type: 'live_freshness.replace'; live_freshness: LiveFreshness };

export interface HealthSummary {
  status: 'healthy' | 'degraded';
  detail: string | null;
}

export interface LastPresenceObservation {
  observation_id: number;
  observed_at: IsoDateTime;
}

export interface CapabilityStatus {
  capability: 'capture.chats' | 'capture.messages' | 'capture.presence' | 'history.sync' | 'command.message.send';
  status: 'active' | 'degraded' | 'unsupported';
  detail: string | null;
}

export interface MessageSendAction {
  type: 'message.send';
  conversation_id: string;
  text: string;
  media_url: string | null;
}

export type CommandAction = MessageSendAction;

export interface CommandOutput {
  external_message_id: string | null;
}

export interface CommandError {
  code: 'rejected' | 'deadline_exceeded' | 'platform_error' | 'execution_error';
  detail: string;
  retryable: boolean;
}

export interface CaptureRule {
  resource: 'chats' | 'messages' | 'presence';
  url_pattern: string;
  enabled: boolean;
}

export interface CapturePolicy {
  observation_interval_seconds: number;
  rules: CaptureRule[];
}

export interface CommandPolicy {
  allowed_actions: 'message.send'[];
  max_text_length: number;
  require_idempotency: boolean;
}

export interface AgentHelloPayload {
  auth_ticket: string;
  agent_installation_id: UUID;
  requested_creator_account_id: string;
  capabilities: (
    | 'capture.chats'
    | 'capture.messages'
    | 'capture.presence'
    | 'history.sync'
    | 'command.message.send'
  )[];
  extension_version: string;
  agent_stream_id: UUID;
  last_acknowledged_source_seq: number;
  applied_config_revision: string | null;
}

export interface AgentSessionPayload {
  connection_id: UUID;
  fencing_token: string;
  creator_account_id: string;
  agent_installation_id: UUID;
  agent_stream_id: UUID;
  committed_source_seq: number;
  resume_action: 'resume' | 'snapshot_required';
  pending_snapshot_id: UUID | null;
  next_expected_chunk_index: number;
  required_config_revision: string;
  reconnect_auth_ticket: string;
  config_auth_ticket: string;
  lease: {
    heartbeat_interval_seconds: number;
    lease_timeout_seconds: number;
  };
}

export interface BridgeHelloPayload {
  auth_ticket: string;
  bridge_session_id: UUID;
  requested_creator_account_id: string;
  capabilities: ('state.snapshot' | 'state.delta' | 'presence.state' | 'message.page')[];
  client_version: string;
  last_view_revision: number | null;
}

export interface BridgeSessionPayload {
  connection_id: UUID;
  bridge_session_id: UUID;
  creator_account_id: string;
  negotiated_protocol_version: ProtocolVersion;
  server_version: string;
}

export interface AgentHeartbeatPayload {
  connection_id: UUID;
  fencing_token: string;
  creator_account_id: string;
  applied_config_revision: string | null;
  health: HealthSummary;
}

export interface SyncRequiredPayload {
  connection_id: UUID;
  creator_account_id: string;
  reason: 'unknown_stream' | 'missing_checkpoint' | 'sequence_gap' | 'local_reset' | 'invariant_failed';
  expected_agent_stream_id: UUID | null;
  expected_next_source_seq: number;
  pending_snapshot_id: UUID | null;
  next_expected_chunk_index: number;
  snapshot: {
    include_chats: true;
    include_messages: true;
    include_coverage_evidence: true;
    max_records_per_chunk: 100;
    max_frame_bytes: 524288;
  };
}

interface SnapshotIdentity {
  connection_id: UUID;
  fencing_token: string;
  creator_account_id: string;
  agent_installation_id: UUID;
  snapshot_id: UUID;
  agent_stream_id: UUID;
}

export type SnapshotChatRecord =
  | { tombstone: false; chat: RawChat }
  | { tombstone: true; chat_id: string };

export type SnapshotMessageRecord =
  | { tombstone: false; message: RawMessage }
  | { tombstone: true; message_id: string; chat_id: string };

export type IngestSnapshotPayload =
  | (SnapshotIdentity & {
      frame_kind: 'begin';
      through_seq: number;
      chunk_count: number;
      record_counts: { chats: number; messages: number; coverage_evidence: number };
      max_frame_bytes: 524288;
    })
  | (SnapshotIdentity & {
      frame_kind: 'chunk';
      chunk_index: number;
      entity_kind: 'chat';
      records: SnapshotChatRecord[];
    })
  | (SnapshotIdentity & {
      frame_kind: 'chunk';
      chunk_index: number;
      entity_kind: 'message';
      records: SnapshotMessageRecord[];
    })
  | (SnapshotIdentity & {
      frame_kind: 'chunk';
      chunk_index: number;
      entity_kind: 'coverage_evidence';
      records: CoverageEvidence[];
    })
  | (SnapshotIdentity & {
      frame_kind: 'commit';
      chunk_count: number;
    });

export interface IngestDeltaPayload {
  connection_id: UUID;
  fencing_token: string;
  creator_account_id: string;
  agent_installation_id: UUID;
  event_id: UUID;
  agent_stream_id: UUID;
  source_seq: number;
  acquisition_origin: 'passive' | 'signer';
  change: RawIngestChange;
}

export interface IngestAckPayload {
  connection_id: UUID;
  creator_account_id: string;
  agent_stream_id: UUID;
  snapshot_id: UUID | null;
  committed_source_seq: number;
  snapshot_progress: {
    snapshot_id: UUID;
    next_expected_chunk_index: number;
    committed: boolean;
  } | null;
}

export interface IngestRejectedPayload {
  connection_id: UUID;
  creator_account_id: string;
  rejected_message_id: UUID;
  event_id: UUID | null;
  code:
    | 'invalid_payload'
    | 'identity_conflict'
    | 'stale_fence'
    | 'sequence_gap'
    | 'invariant_failed'
    | 'chunk_conflict'
    | 'snapshot_incomplete'
    | 'frame_too_large';
  retryable: boolean;
  detail: string;
}

export interface StateSnapshotPayload {
  creator_account_id: string;
  view_revision: number;
  generated_at: IsoDateTime;
  conversations: ConversationSummary[];
  analytics: AnalyticsView;
  coverage: HistoricalCoverage;
  projection: ProjectionState;
  live_freshness: LiveFreshness;
}

export interface StateDeltaPayload {
  creator_account_id: string;
  view_revision: number;
  committed_at: IsoDateTime;
  changes: StateChange[];
}

export interface StateResyncPayload {
  connection_id: UUID;
  bridge_session_id: UUID;
  creator_account_id: string;
  last_applied_view_revision: number;
  reason: 'revision_gap' | 'invalid_delta' | 'reconnect' | 'manual';
}

export interface PresenceObservedPayload {
  connection_id: UUID;
  fencing_token: string;
  creator_account_id: string;
  observation_id: number;
  observed_at: IsoDateTime;
  online_platform_user_ids: string[];
}

export interface PresenceStatePayload {
  creator_account_id: string;
  freshness: 'current' | 'unknown';
  online_platform_user_ids: string[];
  server_received_at: IsoDateTime | null;
  expires_at: IsoDateTime | null;
  last_observation: LastPresenceObservation | null;
}

export interface AgentStatePayload {
  creator_account_id: string;
  status: 'connected' | 'stale' | 'disconnected';
  agent_installation_id: UUID | null;
  connection_id: UUID | null;
  required_config_revision: string;
  applied_config_revision: string | null;
  required_history_settings_revision: number;
  applied_history_settings_revision: number | null;
  last_heartbeat_at: IsoDateTime | null;
  degraded_reason: string | null;
}

export interface SystemStatePayload {
  creator_account_id: string;
  processing_mode: 'processing_snapshot' | 'realtime' | 'resyncing';
  readiness: 'ready' | 'degraded' | 'unavailable';
  updated_at: IsoDateTime;
  detail: string | null;
}

export interface ProtocolErrorPayload {
  code: 'unsupported_version' | 'wrong_role' | 'pre_handshake' | 'identity_conflict' | 'validation_failed' | 'unauthorized' | 'internal_error';
  related_message_id: UUID | null;
  retryable: boolean;
  fatal: boolean;
  detail: string;
}

export interface ConfigAvailablePayload {
  connection_id: UUID;
  creator_account_id: string;
  required_config_revision: string;
  digest: Sha256Digest;
}

export interface ConfigAppliedPayload {
  connection_id: UUID;
  fencing_token: string;
  creator_account_id: string;
  config_revision: string;
  digest: Sha256Digest;
  outcome: 'applied' | 'degraded' | 'rejected';
  capabilities: CapabilityStatus[];
}

export interface CommandExecutePayload {
  connection_id: UUID;
  fencing_token: string;
  creator_account_id: string;
  command_id: UUID;
  deadline: IsoDateTime;
  idempotency_policy: 'deduplicate';
  action: CommandAction;
}

export interface CommandResultPayload {
  connection_id: UUID;
  fencing_token: string;
  creator_account_id: string;
  command_id: UUID;
  result_id: UUID;
  status: 'accepted' | 'succeeded' | 'failed';
  completed_at: IsoDateTime;
  output: CommandOutput | null;
  error: CommandError | null;
}

export interface CommandResultAckPayload {
  connection_id: UUID;
  creator_account_id: string;
  command_id: UUID;
  result_id: UUID;
  recorded_at: IsoDateTime;
}

export interface Envelope<TType extends string, TPayload> {
  type: TType;
  protocol_version: ProtocolVersion;
  message_id: UUID;
  correlation_id?: UUID | null;
  payload: TPayload;
}

export type AgentHelloMessage = Envelope<'agent.hello', AgentHelloPayload>;
export type AgentSessionMessage = Envelope<'agent.session', AgentSessionPayload>;
export type BridgeHelloMessage = Envelope<'bridge.hello', BridgeHelloPayload>;
export type BridgeSessionMessage = Envelope<'bridge.session', BridgeSessionPayload>;
export type AgentHeartbeatMessage = Envelope<'agent.heartbeat', AgentHeartbeatPayload>;
export type SyncRequiredMessage = Envelope<'sync.required', SyncRequiredPayload>;
export type IngestSnapshotMessage = Envelope<'ingest.snapshot', IngestSnapshotPayload>;
export type IngestDeltaMessage = Envelope<'ingest.delta', IngestDeltaPayload>;
export type IngestAckMessage = Envelope<'ingest.ack', IngestAckPayload>;
export type IngestRejectedMessage = Envelope<'ingest.rejected', IngestRejectedPayload>;
export type StateSnapshotMessage = Envelope<'state.snapshot', StateSnapshotPayload>;
export type StateDeltaMessage = Envelope<'state.delta', StateDeltaPayload>;
export type StateResyncMessage = Envelope<'state.resync', StateResyncPayload>;
export type PresenceObservedMessage = Envelope<'presence.observed', PresenceObservedPayload>;
export type PresenceStateMessage = Envelope<'presence.state', PresenceStatePayload>;
export type AgentStateMessage = Envelope<'agent.state', AgentStatePayload>;
export type SystemStateMessage = Envelope<'system.state', SystemStatePayload>;
export type ProtocolErrorMessage = Envelope<'protocol.error', ProtocolErrorPayload>;
export type ConfigAvailableMessage = Envelope<'config.available', ConfigAvailablePayload>;
export type ConfigAppliedMessage = Envelope<'config.applied', ConfigAppliedPayload>;
export type CommandExecuteMessage = Envelope<'command.execute', CommandExecutePayload>;
export type CommandResultMessage = Envelope<'command.result', CommandResultPayload>;
export type CommandResultAckMessage = Envelope<'command.result.ack', CommandResultAckPayload>;

export type AgentToBrainMessage = AgentHelloMessage | AgentHeartbeatMessage | IngestSnapshotMessage | IngestDeltaMessage | PresenceObservedMessage | ConfigAppliedMessage | CommandResultMessage;
export type BrainToAgentMessage = AgentSessionMessage | SyncRequiredMessage | IngestAckMessage | IngestRejectedMessage | ProtocolErrorMessage | ConfigAvailableMessage | CommandExecuteMessage | CommandResultAckMessage;
export type BridgeToBrainMessage = BridgeHelloMessage | StateResyncMessage;
export type BrainToBridgeMessage = BridgeSessionMessage | StateSnapshotMessage | StateDeltaMessage | PresenceStateMessage | AgentStateMessage | SystemStateMessage | ProtocolErrorMessage;

export interface AgentConfigGetRequest {
  operation: 'agent.config.get';
  protocol_version: ProtocolVersion;
  auth_ticket: string;
  agent_installation_id: UUID;
  creator_account_id: string;
  current_etag: string | null;
  current_config_revision: string | null;
  supported_config_schema_versions: '2'[];
}

export interface HistoryAcquisitionConfig {
  enabled: boolean;
  consent_revision: string | null;
  authorized_platform_creator_id: string | null;
  recent_window_days: number;
  page_size: number;
  pages_per_wake: number;
  request_interval_ms: number;
  retry_limit: number;
}

export interface AgentConfigDocumentResponse {
  operation: 'agent.config.document';
  protocol_version: ProtocolVersion;
  creator_account_id: string;
  config_revision: string;
  config_schema_version: '2';
  digest: Sha256Digest;
  etag: string;
  issued_at: IsoDateTime;
  capture_policy: CapturePolicy;
  command_policy: CommandPolicy;
  history_acquisition: HistoryAcquisitionConfig;
}

/** Authenticated, revision-bound REST message window returned by Brain. */
export interface MessagePageResponse {
  creator_account_id: string;
  conversation_id: string;
  projection_generation: string;
  read_revision: number;
  generated_at: IsoDateTime;
  items: MessageView[];
  older_cursor: string | null;
  has_older_stored_items: boolean;
  conversation_coverage: ConversationCoverage;
  projection: ProjectionState;
}

export interface HistorySettings {
  creator_account_id: string;
  settings_revision: number;
  consent_policy_version: string;
  consent_revision: string | null;
  authorized_platform_creator_id: string | null;
  desired_state: 'not_started' | 'running' | 'paused' | 'revoked';
  effective_state: 'not_applied' | 'running' | 'paused' | 'revoked';
  effective_config_revision: string | null;
  recent_window_days: number;
  page_size: number;
  pages_per_wake: number;
  request_interval_ms: number;
  retry_limit: number;
  updated_at: IsoDateTime;
}

export interface UpdateHistorySettingsRequest {
  desired_state: 'running' | 'paused';
  consent_policy_version: string | null;
  accept_consent: boolean;
  recent_window_days: number;
  page_size: number;
  pages_per_wake: number;
  request_interval_ms: number;
  retry_limit: number;
}
