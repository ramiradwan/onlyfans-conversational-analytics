/** Dependency-free JSDoc contract for protocol v1 Agent operations. */

/** @template {string} T @template P @typedef {{type: T, protocol_version: '1', message_id: string, correlation_id?: string|null, payload: P}} Envelope */
/** @typedef {{status: 'healthy'|'degraded', detail: string|null}} HealthSummary */
/** @typedef {{chat_id: string, platform_user_id: string, display_name: string|null, updated_at: string}} RawChat */
/** @typedef {{message_id: string, chat_id: string, sender_platform_user_id: string, text: string, sent_at: string, direction: 'inbound'|'outbound'}} RawMessage */
/** @typedef {{type: 'chat.upsert', chat: RawChat}|{type: 'chat.delete', chat_id: string}|{type: 'message.upsert', message: RawMessage}|{type: 'message.delete', message_id: string, chat_id: string}} RawIngestChange */
/** @typedef {{capability: 'capture.chats'|'capture.messages'|'capture.presence'|'command.message.send', status: 'active'|'degraded'|'unsupported', detail: string|null}} CapabilityStatus */
/** @typedef {{type: 'message.send', conversation_id: string, text: string, media_url: string|null}} CommandAction */
/** @typedef {{external_message_id: string|null}} CommandOutput */
/** @typedef {{code: 'rejected'|'deadline_exceeded'|'platform_error'|'execution_error', detail: string, retryable: boolean}} CommandError */

/** @typedef {{auth_ticket: string, agent_installation_id: string, requested_creator_account_id: string, capabilities: Array<'capture.chats'|'capture.messages'|'capture.presence'|'command.message.send'>, extension_version: string, agent_stream_id: string, last_acknowledged_source_seq: number, applied_config_revision: string|null}} AgentHelloPayload */
/** @typedef {{connection_id: string, fencing_token: string, creator_account_id: string, agent_installation_id: string, agent_stream_id: string, committed_source_seq: number, resume_action: 'resume'|'snapshot_required', required_config_revision: string, lease: {heartbeat_interval_seconds: number, lease_timeout_seconds: number}}} AgentSessionPayload */
/** @typedef {{connection_id: string, fencing_token: string, creator_account_id: string, applied_config_revision: string|null, health: HealthSummary}} AgentHeartbeatPayload */
/** @typedef {{connection_id: string, creator_account_id: string, reason: 'unknown_stream'|'missing_checkpoint'|'sequence_gap'|'local_reset'|'invariant_failed', expected_agent_stream_id: string|null, expected_next_source_seq: number, snapshot: {include_chats: true, include_messages: true}}} SyncRequiredPayload */
/** @typedef {{connection_id: string, fencing_token: string, creator_account_id: string, agent_installation_id: string, snapshot_id: string, agent_stream_id: string, through_seq: number, chats: RawChat[], messages: RawMessage[]}} IngestSnapshotPayload */
/** @typedef {{connection_id: string, fencing_token: string, creator_account_id: string, agent_installation_id: string, event_id: string, agent_stream_id: string, source_seq: number, change: RawIngestChange}} IngestDeltaPayload */
/** @typedef {{connection_id: string, creator_account_id: string, agent_stream_id: string, snapshot_id: string|null, committed_source_seq: number}} IngestAckPayload */
/** @typedef {{connection_id: string, creator_account_id: string, rejected_message_id: string, event_id: string|null, code: 'invalid_payload'|'identity_conflict'|'stale_fence'|'sequence_gap'|'invariant_failed', retryable: boolean, detail: string}} IngestRejectedPayload */
/** @typedef {{connection_id: string, fencing_token: string, creator_account_id: string, observation_id: number, observed_at: string, online_platform_user_ids: string[]}} PresenceObservedPayload */
/** @typedef {{code: 'unsupported_version'|'wrong_role'|'pre_handshake'|'identity_conflict'|'validation_failed'|'unauthorized'|'internal_error', related_message_id: string|null, retryable: boolean, fatal: boolean, detail: string}} ProtocolErrorPayload */
/** @typedef {{connection_id: string, creator_account_id: string, required_config_revision: string, digest: string}} ConfigAvailablePayload */
/** @typedef {{connection_id: string, fencing_token: string, creator_account_id: string, config_revision: string, digest: string, outcome: 'applied'|'degraded'|'rejected', capabilities: CapabilityStatus[]}} ConfigAppliedPayload */
/** @typedef {{connection_id: string, fencing_token: string, creator_account_id: string, command_id: string, deadline: string, idempotency_policy: 'deduplicate', action: CommandAction}} CommandExecutePayload */
/** @typedef {{connection_id: string, fencing_token: string, creator_account_id: string, command_id: string, result_id: string, status: 'accepted'|'succeeded'|'failed', completed_at: string, output: CommandOutput|null, error: CommandError|null}} CommandResultPayload */
/** @typedef {{connection_id: string, creator_account_id: string, command_id: string, result_id: string, recorded_at: string}} CommandResultAckPayload */

/** @typedef {Envelope<'agent.hello', AgentHelloPayload>} AgentHelloMessage */
/** @typedef {Envelope<'agent.session', AgentSessionPayload>} AgentSessionMessage */
/** @typedef {Envelope<'agent.heartbeat', AgentHeartbeatPayload>} AgentHeartbeatMessage */
/** @typedef {Envelope<'sync.required', SyncRequiredPayload>} SyncRequiredMessage */
/** @typedef {Envelope<'ingest.snapshot', IngestSnapshotPayload>} IngestSnapshotMessage */
/** @typedef {Envelope<'ingest.delta', IngestDeltaPayload>} IngestDeltaMessage */
/** @typedef {Envelope<'ingest.ack', IngestAckPayload>} IngestAckMessage */
/** @typedef {Envelope<'ingest.rejected', IngestRejectedPayload>} IngestRejectedMessage */
/** @typedef {Envelope<'presence.observed', PresenceObservedPayload>} PresenceObservedMessage */
/** @typedef {Envelope<'protocol.error', ProtocolErrorPayload>} ProtocolErrorMessage */
/** @typedef {Envelope<'config.available', ConfigAvailablePayload>} ConfigAvailableMessage */
/** @typedef {Envelope<'config.applied', ConfigAppliedPayload>} ConfigAppliedMessage */
/** @typedef {Envelope<'command.execute', CommandExecutePayload>} CommandExecuteMessage */
/** @typedef {Envelope<'command.result', CommandResultPayload>} CommandResultMessage */
/** @typedef {Envelope<'command.result.ack', CommandResultAckPayload>} CommandResultAckMessage */
/** @typedef {AgentHelloMessage|AgentHeartbeatMessage|IngestSnapshotMessage|IngestDeltaMessage|PresenceObservedMessage|ConfigAppliedMessage|CommandResultMessage} AgentToBrainMessage */
/** @typedef {AgentSessionMessage|SyncRequiredMessage|IngestAckMessage|IngestRejectedMessage|ProtocolErrorMessage|ConfigAvailableMessage|CommandExecuteMessage|CommandResultAckMessage} BrainToAgentMessage */

/** @typedef {{operation: 'agent.config.get', protocol_version: '1', auth_ticket: string, agent_installation_id: string, creator_account_id: string, current_etag: string|null, current_config_revision: string|null, supported_config_schema_versions: Array<'1'>}} AgentConfigGetRequest */
/** @typedef {{resource: 'chats'|'messages'|'presence', url_pattern: string, enabled: boolean}} CaptureRule */
/** @typedef {{observation_interval_seconds: number, rules: CaptureRule[]}} CapturePolicy */
/** @typedef {{allowed_actions: Array<'message.send'>, max_text_length: number, require_idempotency: boolean}} CommandPolicy */
/** @typedef {{operation: 'agent.config.document', protocol_version: '1', creator_account_id: string, config_revision: string, config_schema_version: '1', digest: string, etag: string, issued_at: string, capture_policy: CapturePolicy, command_policy: CommandPolicy}} AgentConfigDocumentResponse */

export {};
