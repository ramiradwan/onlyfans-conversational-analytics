CREATE TABLE ingest_streams (
    creator_account_id TEXT NOT NULL,
    agent_installation_id TEXT NOT NULL,
    agent_stream_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (creator_account_id, agent_installation_id, agent_stream_id)
);

CREATE TABLE ingest_checkpoints (
    creator_account_id TEXT NOT NULL,
    agent_installation_id TEXT NOT NULL,
    agent_stream_id TEXT NOT NULL,
    committed_source_seq INTEGER NOT NULL CHECK (committed_source_seq >= 0),
    committed_at TEXT NOT NULL,
    PRIMARY KEY (creator_account_id, agent_installation_id, agent_stream_id),
    FOREIGN KEY (creator_account_id, agent_installation_id, agent_stream_id)
        REFERENCES ingest_streams (creator_account_id, agent_installation_id, agent_stream_id)
        ON DELETE CASCADE
);

CREATE TABLE account_heads (
    creator_account_id TEXT PRIMARY KEY,
    canonical_revision INTEGER NOT NULL DEFAULT 0 CHECK (canonical_revision >= 0),
    view_revision INTEGER NOT NULL DEFAULT 0 CHECK (view_revision >= 0),
    data_revision INTEGER NOT NULL DEFAULT 0 CHECK (data_revision >= 0),
    updated_at TEXT NOT NULL
);

CREATE TABLE stream_epochs (
    creator_account_id TEXT NOT NULL,
    agent_installation_id TEXT NOT NULL,
    agent_stream_id TEXT NOT NULL,
    stream_epoch INTEGER NOT NULL CHECK (stream_epoch > 0),
    activated_at TEXT NOT NULL,
    PRIMARY KEY (creator_account_id, agent_installation_id, agent_stream_id),
    UNIQUE (creator_account_id, stream_epoch)
);

CREATE TABLE snapshot_uploads (
    creator_account_id TEXT NOT NULL,
    agent_installation_id TEXT NOT NULL,
    agent_stream_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    starting_checkpoint INTEGER CHECK (
        starting_checkpoint IS NULL OR starting_checkpoint >= 0
    ),
    through_seq INTEGER NOT NULL CHECK (through_seq >= 0),
    chunk_count INTEGER NOT NULL CHECK (chunk_count >= 0),
    expected_chats INTEGER NOT NULL CHECK (expected_chats >= 0),
    expected_messages INTEGER NOT NULL CHECK (expected_messages >= 0),
    expected_coverage_evidence INTEGER NOT NULL CHECK (expected_coverage_evidence >= 0),
    next_chunk_index INTEGER NOT NULL DEFAULT 0 CHECK (next_chunk_index >= 0),
    received_chats INTEGER NOT NULL DEFAULT 0 CHECK (received_chats >= 0),
    received_messages INTEGER NOT NULL DEFAULT 0 CHECK (received_messages >= 0),
    received_coverage_evidence INTEGER NOT NULL DEFAULT 0 CHECK (received_coverage_evidence >= 0),
    last_entity_kind TEXT CHECK (
        last_entity_kind IS NULL OR last_entity_kind IN ('chat', 'message', 'coverage_evidence')
    ),
    begin_fingerprint TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('staging', 'committed')),
    created_at TEXT NOT NULL,
    committed_at TEXT,
    PRIMARY KEY (creator_account_id, agent_installation_id, agent_stream_id, snapshot_id)
);

CREATE TABLE snapshot_chunks (
    creator_account_id TEXT NOT NULL,
    agent_installation_id TEXT NOT NULL,
    agent_stream_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
    entity_kind TEXT NOT NULL CHECK (entity_kind IN ('chat', 'message', 'coverage_evidence')),
    record_count INTEGER NOT NULL CHECK (record_count BETWEEN 1 AND 100),
    fingerprint TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    PRIMARY KEY (
        creator_account_id, agent_installation_id, agent_stream_id,
        snapshot_id, chunk_index
    ),
    FOREIGN KEY (
        creator_account_id, agent_installation_id, agent_stream_id, snapshot_id
    ) REFERENCES snapshot_uploads (
        creator_account_id, agent_installation_id, agent_stream_id, snapshot_id
    ) ON DELETE CASCADE
);

CREATE TABLE snapshot_chat_records (
    creator_account_id TEXT NOT NULL,
    agent_installation_id TEXT NOT NULL,
    agent_stream_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    record_json TEXT NOT NULL,
    is_tombstone INTEGER NOT NULL CHECK (is_tombstone IN (0, 1)),
    record_kind TEXT CHECK (record_kind IN ('placeholder', 'full')),
    platform_user_id TEXT,
    display_name TEXT,
    upstream_updated_at TEXT,
    content_hash TEXT,
    CHECK (
        is_tombstone=1 OR (
            record_kind IS NOT NULL AND content_hash IS NOT NULL
            AND (record_kind='placeholder' OR (
                platform_user_id IS NOT NULL AND upstream_updated_at IS NOT NULL
            ))
        )
    ),
    PRIMARY KEY (
        creator_account_id, agent_installation_id, agent_stream_id,
        snapshot_id, chat_id
    ),
    FOREIGN KEY (
        creator_account_id, agent_installation_id, agent_stream_id,
        snapshot_id, chunk_index
    ) REFERENCES snapshot_chunks (
        creator_account_id, agent_installation_id, agent_stream_id,
        snapshot_id, chunk_index
    ) ON DELETE CASCADE
);

CREATE TABLE snapshot_message_records (
    creator_account_id TEXT NOT NULL,
    agent_installation_id TEXT NOT NULL,
    agent_stream_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    record_json TEXT NOT NULL,
    is_tombstone INTEGER NOT NULL CHECK (is_tombstone IN (0, 1)),
    sender_platform_user_id TEXT,
    text TEXT,
    sent_at TEXT,
    direction TEXT CHECK (direction IN ('inbound', 'outbound')),
    upstream_updated_at TEXT,
    content_hash TEXT,
    CHECK (
        is_tombstone=1 OR (
            sender_platform_user_id IS NOT NULL AND text IS NOT NULL
            AND sent_at IS NOT NULL AND direction IS NOT NULL
            AND content_hash IS NOT NULL
        )
    ),
    PRIMARY KEY (
        creator_account_id, agent_installation_id, agent_stream_id,
        snapshot_id, message_id
    ),
    FOREIGN KEY (
        creator_account_id, agent_installation_id, agent_stream_id,
        snapshot_id, chunk_index
    ) REFERENCES snapshot_chunks (
        creator_account_id, agent_installation_id, agent_stream_id,
        snapshot_id, chunk_index
    ) ON DELETE CASCADE
);

CREATE INDEX snapshot_messages_by_chat
    ON snapshot_message_records (
        creator_account_id, agent_installation_id, agent_stream_id,
        snapshot_id, chat_id
    );

CREATE TABLE stream_chat_membership (
    creator_account_id TEXT NOT NULL,
    agent_installation_id TEXT NOT NULL,
    agent_stream_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    observed_source_seq INTEGER NOT NULL CHECK (observed_source_seq >= 0),
    PRIMARY KEY (
        creator_account_id, agent_installation_id, agent_stream_id, chat_id
    ),
    FOREIGN KEY (creator_account_id, agent_installation_id, agent_stream_id)
        REFERENCES ingest_streams (creator_account_id, agent_installation_id, agent_stream_id)
        ON DELETE CASCADE
);

CREATE TABLE stream_message_membership (
    creator_account_id TEXT NOT NULL,
    agent_installation_id TEXT NOT NULL,
    agent_stream_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    observed_source_seq INTEGER NOT NULL CHECK (observed_source_seq >= 0),
    PRIMARY KEY (
        creator_account_id, agent_installation_id, agent_stream_id, message_id
    ),
    FOREIGN KEY (creator_account_id, agent_installation_id, agent_stream_id)
        REFERENCES ingest_streams (creator_account_id, agent_installation_id, agent_stream_id)
        ON DELETE CASCADE
);

CREATE TABLE snapshot_coverage_records (
    creator_account_id TEXT NOT NULL,
    agent_installation_id TEXT NOT NULL,
    agent_stream_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    record_index INTEGER NOT NULL CHECK (record_index >= 0),
    record_json TEXT NOT NULL,
    PRIMARY KEY (
        creator_account_id, agent_installation_id, agent_stream_id,
        snapshot_id, evidence_id
    ),
    UNIQUE (
        creator_account_id, agent_installation_id, agent_stream_id,
        snapshot_id, chunk_index, record_index
    ),
    FOREIGN KEY (
        creator_account_id, agent_installation_id, agent_stream_id,
        snapshot_id, chunk_index
    ) REFERENCES snapshot_chunks (
        creator_account_id, agent_installation_id, agent_stream_id,
        snapshot_id, chunk_index
    ) ON DELETE CASCADE
);

CREATE TABLE account_chats (
    creator_account_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    record_kind TEXT NOT NULL CHECK (record_kind IN ('placeholder', 'full')),
    platform_user_id TEXT,
    display_name TEXT,
    upstream_updated_at TEXT,
    content_hash TEXT NOT NULL,
    winning_stream_epoch INTEGER NOT NULL,
    winning_source_seq INTEGER NOT NULL CHECK (winning_source_seq >= 0),
    winning_event_id TEXT,
    is_deleted INTEGER NOT NULL DEFAULT 0 CHECK (is_deleted IN (0, 1)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (creator_account_id, chat_id)
);

CREATE TABLE account_messages (
    creator_account_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    sender_platform_user_id TEXT NOT NULL,
    text TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    upstream_updated_at TEXT,
    content_hash TEXT NOT NULL,
    winning_stream_epoch INTEGER NOT NULL,
    winning_source_seq INTEGER NOT NULL CHECK (winning_source_seq >= 0),
    winning_event_id TEXT,
    is_deleted INTEGER NOT NULL DEFAULT 0 CHECK (is_deleted IN (0, 1)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (creator_account_id, message_id),
    FOREIGN KEY (creator_account_id, chat_id)
        REFERENCES account_chats (creator_account_id, chat_id)
);

CREATE INDEX account_messages_page
    ON account_messages (creator_account_id, chat_id, sent_at, message_id)
    WHERE is_deleted = 0;

CREATE TABLE entity_tombstones (
    creator_account_id TEXT NOT NULL,
    entity_kind TEXT NOT NULL CHECK (entity_kind IN ('chat', 'message')),
    entity_id TEXT NOT NULL,
    chat_id TEXT,
    stream_epoch INTEGER NOT NULL,
    source_seq INTEGER NOT NULL CHECK (source_seq >= 0),
    event_id TEXT,
    deleted_at TEXT NOT NULL,
    PRIMARY KEY (creator_account_id, entity_kind, entity_id)
);

CREATE TABLE entity_conflicts (
    conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
    creator_account_id TEXT NOT NULL,
    entity_kind TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    existing_hash TEXT NOT NULL,
    incoming_hash TEXT NOT NULL,
    stream_epoch INTEGER NOT NULL,
    source_seq INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    reason TEXT NOT NULL
);

CREATE UNIQUE INDEX entity_conflicts_material_dedup
    ON entity_conflicts (
        creator_account_id,
        entity_kind,
        entity_id,
        existing_hash,
        incoming_hash,
        stream_epoch,
        source_seq,
        reason
    );

CREATE TABLE raw_ingest_events (
    creator_account_id TEXT NOT NULL,
    agent_installation_id TEXT NOT NULL,
    agent_stream_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    source_seq INTEGER NOT NULL CHECK (source_seq > 0),
    origin TEXT NOT NULL CHECK (origin IN ('passive', 'signer')),
    observed_at TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    event_json TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    PRIMARY KEY (creator_account_id, agent_installation_id, agent_stream_id, event_id),
    UNIQUE (creator_account_id, agent_installation_id, agent_stream_id, source_seq),
    FOREIGN KEY (creator_account_id, agent_installation_id, agent_stream_id)
        REFERENCES ingest_streams (creator_account_id, agent_installation_id, agent_stream_id)
        ON DELETE CASCADE
);

CREATE TABLE committed_snapshots (
    creator_account_id TEXT NOT NULL,
    agent_installation_id TEXT NOT NULL,
    agent_stream_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    through_seq INTEGER NOT NULL,
    chat_count INTEGER NOT NULL,
    message_count INTEGER NOT NULL,
    coverage_evidence_count INTEGER NOT NULL,
    committed_at TEXT NOT NULL,
    PRIMARY KEY (creator_account_id, agent_installation_id, agent_stream_id, snapshot_id),
    FOREIGN KEY (creator_account_id, agent_installation_id, agent_stream_id)
        REFERENCES ingest_streams (creator_account_id, agent_installation_id, agent_stream_id)
        ON DELETE CASCADE
);

CREATE TABLE coverage_generations (
    creator_account_id TEXT NOT NULL,
    generation_id TEXT NOT NULL,
    authorization_revision TEXT NOT NULL,
    as_of TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('discovering', 'backfilling', 'complete', 'partial', 'superseded')),
    inventory_ended_at TEXT,
    closed_at TEXT,
    reason_code TEXT,
    PRIMARY KEY (creator_account_id, generation_id)
);

CREATE UNIQUE INDEX one_open_coverage_generation
    ON coverage_generations (creator_account_id)
    WHERE state IN ('discovering', 'backfilling');

CREATE TABLE coverage_members (
    creator_account_id TEXT NOT NULL,
    generation_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    history_started_at TEXT,
    earliest_observed_at TEXT,
    head_reconciled_through TEXT,
    PRIMARY KEY (creator_account_id, generation_id, conversation_id),
    FOREIGN KEY (creator_account_id, generation_id)
        REFERENCES coverage_generations (creator_account_id, generation_id) ON DELETE CASCADE
);

CREATE TABLE account_coverage_heads (
    creator_account_id TEXT PRIMARY KEY,
    active_generation_id TEXT,
    last_complete_generation_id TEXT,
    coverage_revision INTEGER NOT NULL DEFAULT 0 CHECK (coverage_revision >= 0),
    updated_at TEXT NOT NULL
);

CREATE TABLE projection_work (
    work_id INTEGER PRIMARY KEY AUTOINCREMENT,
    creator_account_id TEXT NOT NULL,
    canonical_revision INTEGER NOT NULL,
    work_kind TEXT NOT NULL CHECK (work_kind IN ('entity', 'coverage', 'reseed')),
    conversation_id TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE (creator_account_id, canonical_revision, work_kind, conversation_id)
);

CREATE INDEX projection_work_pending
    ON projection_work (creator_account_id, work_id) WHERE completed_at IS NULL;

CREATE TABLE projection_activation_intents (
    creator_account_id TEXT NOT NULL,
    target_canonical_revision INTEGER NOT NULL CHECK (target_canonical_revision > 0),
    state TEXT NOT NULL CHECK (state IN ('pending', 'activated', 'superseded')),
    requested_at TEXT NOT NULL,
    generation_id TEXT,
    activated_view_revision INTEGER CHECK (
        activated_view_revision IS NULL OR activated_view_revision > 0
    ),
    projection_committed_at TEXT,
    activated_at TEXT,
    PRIMARY KEY (creator_account_id, target_canonical_revision),
    FOREIGN KEY (creator_account_id) REFERENCES account_heads (creator_account_id)
);

CREATE INDEX projection_activation_pending
    ON projection_activation_intents (creator_account_id, state, target_canonical_revision);

CREATE TABLE launcher_bootstrap_consumptions (
    ticket_hash TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    creator_account_id TEXT NOT NULL,
    consumed_at TEXT NOT NULL
);

CREATE TABLE live_ingest_state (
    creator_account_id TEXT PRIMARY KEY,
    last_observed_at TEXT,
    last_committed_at TEXT,
    expires_at TEXT,
    pending_event_count INTEGER CHECK (pending_event_count IS NULL OR pending_event_count >= 0)
);

CREATE TABLE history_settings (
    creator_account_id TEXT PRIMARY KEY,
    settings_revision INTEGER NOT NULL CHECK (settings_revision >= 0),
    consent_policy_version TEXT NOT NULL,
    consent_revision TEXT,
    authorized_platform_creator_id TEXT,
    desired_state TEXT NOT NULL CHECK (
        desired_state IN ('not_started', 'running', 'paused', 'revoked')
    ),
    effective_state TEXT NOT NULL CHECK (
        effective_state IN ('not_applied', 'running', 'paused', 'revoked')
    ),
    required_config_revision TEXT,
    effective_config_revision TEXT,
    effective_settings_revision INTEGER CHECK (
        effective_settings_revision IS NULL OR effective_settings_revision >= 0
    ),
    recent_window_days INTEGER NOT NULL CHECK (recent_window_days BETWEEN 1 AND 365),
    page_size INTEGER NOT NULL CHECK (page_size BETWEEN 1 AND 100),
    pages_per_wake INTEGER NOT NULL CHECK (pages_per_wake >= 1),
    request_interval_ms INTEGER NOT NULL CHECK (request_interval_ms >= 0),
    retry_limit INTEGER NOT NULL CHECK (retry_limit >= 0),
    updated_at TEXT NOT NULL
);
