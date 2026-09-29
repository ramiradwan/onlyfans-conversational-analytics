/** Dependency-free JSDoc contract for the single current protocol v2 Agent surface. */

/** @template {string} T @template P @typedef {{type: T, protocol_version: '2', message_id: string, correlation_id?: string|null, payload: P}} Envelope */
/** @typedef {{status: 'healthy'|'degraded', detail: string|null}} HealthSummary */
/** @typedef {{chat_id: string, record_kind: 'full'|'placeholder', platform_user_id: string|null, display_name: string|null, updated_at: string|null}} RawChat */
/** @typedef {{message_id: string, chat_id: string, sender_platform_user_id: string, text: string, sent_at: string, direction: 'inbound'|'outbound'}} RawMessage */
/** @typedef {{type:'generation.started',generation_id:string,as_of:string,authorization_revision:string}|{type:'inventory.member',generation_id:string,conversation_id:string}|{type:'inventory.ended',generation_id:string,observed_at:string}|{type:'conversation.history_started',generation_id:string,conversation_id:string,earliest_observed_at:string|null,observed_at:string}|{type:'conversation.head_reconciled',generation_id:string,conversation_id:string,reconciled_through:string}|{type:'generation.closed',generation_id:string,closed_at:string}|CheckEvidence} CoverageEvidence */
/** @typedef {{type: 'chat.upsert', chat: RawChat}|{type: 'chat.delete', chat_id: string}|{type: 'message.upsert', message: RawMessage}|{type: 'message.delete', message_id: string, chat_id: string}|{type:'coverage.observed',evidence:CoverageEvidence}} RawIngestChange */
/** @typedef {{capability: 'capture.chats'|'capture.messages'|'capture.presence'|'history.sync'|'history.catchup.v1'|'command.message.send', status: 'active'|'degraded'|'unsupported', detail: string|null}} CapabilityStatus */
/** @typedef {{type: 'message.send', conversation_id: string, text: string, media_url: string|null}} CommandAction */
/** @typedef {{external_message_id: string|null}} CommandOutput */
/** @typedef {{code: 'rejected'|'deadline_exceeded'|'platform_error'|'execution_error', detail: string, retryable: boolean}} CommandError */

/** @typedef {{auth_ticket: string, agent_installation_id: string, requested_creator_account_id: string, capabilities: Array<'capture.chats'|'capture.messages'|'capture.presence'|'history.sync'|'history.catchup.v1'|'command.message.send'>, extension_version: string, agent_stream_id: string, last_acknowledged_source_seq: number, applied_config_revision: string|null}} AgentHelloPayload */
/** @typedef {{connection_id: string, fencing_token: string, creator_account_id: string, agent_installation_id: string, agent_stream_id: string, committed_source_seq: number, resume_action: 'resume'|'snapshot_required', required_config_revision: string, reconnect_auth_ticket:string, config_auth_ticket:string, pending_snapshot_id:string|null,next_expected_chunk_index:number,lease: {heartbeat_interval_seconds: number, lease_timeout_seconds: number}}} AgentSessionPayload */
/** @typedef {{connection_id: string, fencing_token: string, creator_account_id: string, applied_config_revision: string|null, health: HealthSummary}} AgentHeartbeatPayload */
/** @typedef {{connection_id: string, creator_account_id: string, reason: 'unknown_stream'|'missing_checkpoint'|'sequence_gap'|'local_reset'|'invariant_failed', expected_agent_stream_id: string|null, expected_next_source_seq: number,pending_snapshot_id:string|null,next_expected_chunk_index:number,snapshot:{include_chats:true,include_messages:true,include_coverage_evidence:true,max_frame_bytes:524288,max_records_per_chunk:100}}} SyncRequiredPayload */
/** @typedef {{tombstone:false,chat:RawChat}|{tombstone:true,chat_id:string}} SnapshotChatRecord */
/** @typedef {{tombstone:false,message:RawMessage}|{tombstone:true,message_id:string,chat_id:string}} SnapshotMessageRecord */
/** @typedef {{frame_kind:'begin',snapshot_id:string,agent_stream_id:string,through_seq:number,chunk_count:number,record_counts:{chats:number,messages:number,coverage_evidence:number},max_frame_bytes:524288}|{frame_kind:'chunk',snapshot_id:string,agent_stream_id:string,chunk_index:number,entity_kind:'chat'|'message'|'coverage_evidence',records:Array<SnapshotChatRecord|SnapshotMessageRecord|CoverageEvidence>}|{frame_kind:'commit',snapshot_id:string,agent_stream_id:string,chunk_count:number}} IngestSnapshotPayload */
/** @typedef {{connection_id: string, fencing_token: string, creator_account_id: string, agent_installation_id: string, event_id: string, agent_stream_id: string, source_seq: number, acquisition_origin:'passive'|'signer',check_id?:string|null,change: RawIngestChange}} IngestDeltaPayload */
/** @typedef {{connection_id: string, creator_account_id: string, agent_stream_id: string, snapshot_id: string|null, committed_source_seq: number,snapshot_progress:{snapshot_id:string,next_expected_chunk_index:number,committed:boolean}|null}} IngestAckPayload */
/** @typedef {{connection_id: string, creator_account_id: string, rejected_message_id: string, event_id: string|null, code: 'invalid_payload'|'identity_conflict'|'stale_fence'|'sequence_gap'|'invariant_failed'|'chunk_conflict'|'snapshot_incomplete'|'frame_too_large', retryable: boolean, detail: string}} IngestRejectedPayload */
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

/** @typedef {{operation: 'agent.config.get', protocol_version: '2', auth_ticket: string, agent_installation_id: string, creator_account_id: string, current_etag: string|null, current_config_revision: string|null, supported_config_schema_versions: Array<'2'>}} AgentConfigGetRequest */
/** @typedef {{resource: 'chats'|'messages'|'presence', url_pattern: string, enabled: boolean}} CaptureRule */
/** @typedef {{observation_interval_seconds: number, rules: CaptureRule[]}} CapturePolicy */
/** @typedef {{allowed_actions: Array<'message.send'>, max_text_length: number, require_idempotency: boolean}} CommandPolicy */
/** @typedef {{enabled:boolean,consent_revision:string|null,authorized_platform_creator_id:string|null,recent_window_days:number,page_size:number,pages_per_wake:number,request_interval_ms:number,retry_limit:number}} HistoryAcquisition */
/** @typedef {{operation: 'agent.config.document', protocol_version: '2', creator_account_id: string, config_revision: string, config_schema_version: '2', digest: string, etag: string, issued_at: string, capture_policy: CapturePolicy, command_policy: CommandPolicy,history_acquisition:HistoryAcquisition}} AgentConfigDocumentResponse */

export {};

/** @typedef {{chat_id:string,head_message_id?:string|null,head_sent_at?:string|null}} CheckHead */
/** @typedef {{list:number,messages:number,probes:number}} CheckCounts */
/** @typedef {{type:'check.chat_reconciled',generation_id:string,chat_id:string,target_head:{message_id?:string|null,sent_at?:string|null},reached:'boundary'|'history_start',final_source_seq:number}|{type:'check.inventory_closed',generation_id:string,strategy:'timestamp'|'probe'|'mixed',scanned:number,changed:number,movers:number}|{type:'check.completed',generation_id:string,kind:'catch_up'|'canary',final_source_seq:number,pages_read:number,counts:CheckCounts,heads?:CheckHead[]}|{type:'check.abandoned',generation_id:string,reason:'account_changed'|'authorization_changed'|'paused'|'cursor_invalid'|'retry_exhausted'|'storage_lost'}} CheckEvidence */
/** @typedef {{protocol_version:'2',auth_ticket:string,agent_installation_id:string,creator_account_id:string}} CatchupRequest */
/** @typedef {CatchupRequest & {operation:'capture.state.report',worker_instance_id:string,report_seq:number,observing:boolean,reason:'ok'|'capture_off'|'consent_needed'|'no_onlyfans_tab'|'tab_frozen'|'tab_discarded'|'hook_not_armed'|'reload_required'|'page_socket_closed'|'account_mismatch'|'storage_locked'|'paused',tabs:{armed:number,frozen:number,discarded:number},page_socket_open:boolean,drops_since_last:{expired:number,rejected:number},requests_since_last:{canary_list:number,catchup_list:number,catchup_messages:number,history_list:number,history_messages:number,identity:number,retries:number},utc_day:string,automatic_pages_today:number}} CaptureStateReportRequest */
/** @typedef {{acknowledged_seq:number}} CaptureStateReportResponse */
/** @typedef {CatchupRequest & {operation:'history.check.begin',request_id:string,worker_instance_id:string,trigger:'admission'|'tab_runnable'|'observing'|'alarm'|'renew',config_revision:string,head_evidence:'none'|'timestamp'|'full',active_check_id:string|null}} HistoryCheckBeginRequest */
/** @typedef {{result:'not_needed'}|{result:'deferred',retry_after_seconds:number,reason:'grant_interval'|'daily_cap'|'not_runnable'|'history_incomplete'|'check_active'}|{result:'granted',check_id:string,kind:'catch_up'|'canary',gap_epoch:number,uncertain_since:string|null,granted_at:string,blind:boolean,page_budget:number,lease_expires_at:string,resume:boolean}} HistoryCheckBeginResponse */
