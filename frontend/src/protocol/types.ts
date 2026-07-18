export type ProtocolVersion = '1';
export type UUID = string;
export type IsoDateTime = string;
export type Sha256Digest = string;

export interface RawChat {
  chat_id: string;
  platform_user_id: string;
  display_name: string | null;
  updated_at: IsoDateTime;
}

export interface RawMessage {
  message_id: string;
  chat_id: string;
  sender_platform_user_id: string;
  text: string;
  sent_at: IsoDateTime;
  direction: 'inbound' | 'outbound';
}

export type RawIngestChange =
  | { type: 'chat.upsert'; chat: RawChat }
  | { type: 'chat.delete'; chat_id: string }
  | { type: 'message.upsert'; message: RawMessage }
  | { type: 'message.delete'; message_id: string; chat_id: string };

export interface MessageView {
  message_id: string;
  text: string;
  sent_at: IsoDateTime;
  direction: 'inbound' | 'outbound';
  sentiment: 'positive' | 'neutral' | 'negative' | 'unknown';
}

export interface ConversationView {
  conversation_id: string;
  platform_user_id: string;
  display_name: string | null;
  unread_count: number;
  last_message_at: IsoDateTime | null;
  messages: MessageView[];
}

export interface AnalyticsView {
  total_conversations: number;
  total_messages: number;
  inbound_messages: number;
  outbound_messages: number;
}

export type StateChange =
  | { type: 'conversation.upsert'; conversation: ConversationView }
  | { type: 'conversation.delete'; conversation_id: string }
  | { type: 'analytics.replace'; analytics: AnalyticsView };

export interface HealthSummary {
  status: 'healthy' | 'degraded';
  detail: string | null;
}

export interface LastPresenceObservation {
  observation_id: number;
  observed_at: IsoDateTime;
}

export interface CapabilityStatus {
  capability: 'capture.chats' | 'capture.messages' | 'capture.presence' | 'command.message.send';
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
  capabilities: ('capture.chats' | 'capture.messages' | 'capture.presence' | 'command.message.send')[];
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
  required_config_revision: string;
  lease: {
    heartbeat_interval_seconds: number;
    lease_timeout_seconds: number;
  };
}

export interface BridgeHelloPayload {
  auth_ticket: string;
  bridge_session_id: UUID;
  requested_creator_account_id: string;
  capabilities: ('state.snapshot' | 'state.delta' | 'presence.state')[];
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
  snapshot: { include_chats: true; include_messages: true };
}

export interface IngestSnapshotPayload {
  connection_id: UUID;
  fencing_token: string;
  creator_account_id: string;
  agent_installation_id: UUID;
  snapshot_id: UUID;
  agent_stream_id: UUID;
  through_seq: number;
  chats: RawChat[];
  messages: RawMessage[];
}

export interface IngestDeltaPayload {
  connection_id: UUID;
  fencing_token: string;
  creator_account_id: string;
  agent_installation_id: UUID;
  event_id: UUID;
  agent_stream_id: UUID;
  source_seq: number;
  change: RawIngestChange;
}

export interface IngestAckPayload {
  connection_id: UUID;
  creator_account_id: string;
  agent_stream_id: UUID;
  snapshot_id: UUID | null;
  committed_source_seq: number;
}

export interface IngestRejectedPayload {
  connection_id: UUID;
  creator_account_id: string;
  rejected_message_id: UUID;
  event_id: UUID | null;
  code: 'invalid_payload' | 'identity_conflict' | 'stale_fence' | 'sequence_gap' | 'invariant_failed';
  retryable: boolean;
  detail: string;
}

export interface StateSnapshotPayload {
  creator_account_id: string;
  view_revision: number;
  generated_at: IsoDateTime;
  conversations: ConversationView[];
  analytics: AnalyticsView;
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
  supported_config_schema_versions: '1'[];
}

export interface AgentConfigDocumentResponse {
  operation: 'agent.config.document';
  protocol_version: ProtocolVersion;
  creator_account_id: string;
  config_revision: string;
  config_schema_version: '1';
  digest: Sha256Digest;
  etag: string;
  issued_at: IsoDateTime;
  capture_policy: CapturePolicy;
  command_policy: CommandPolicy;
}
