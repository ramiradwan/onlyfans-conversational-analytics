CREATE TABLE projection_accounts (
    creator_account_id TEXT NOT NULL,
    projection_slot INTEGER NOT NULL CHECK (projection_slot IN (0, 1)),
    generation_id TEXT NOT NULL,
    projected_revision INTEGER NOT NULL CHECK (projected_revision >= 0),
    read_revision INTEGER NOT NULL CHECK (read_revision >= 0),
    generated_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('current', 'degraded')),
    PRIMARY KEY (creator_account_id, projection_slot),
    UNIQUE (creator_account_id, generation_id),
    UNIQUE (creator_account_id, read_revision)
);

CREATE TABLE conversation_summaries (
    creator_account_id TEXT NOT NULL,
    projection_slot INTEGER NOT NULL CHECK (projection_slot IN (0, 1)),
    conversation_id TEXT NOT NULL,
    document_json TEXT NOT NULL,
    PRIMARY KEY (creator_account_id, projection_slot, conversation_id),
    FOREIGN KEY (creator_account_id, projection_slot)
        REFERENCES projection_accounts (creator_account_id, projection_slot)
        ON DELETE CASCADE
);

CREATE TABLE projection_messages (
    creator_account_id TEXT NOT NULL,
    projection_slot INTEGER NOT NULL CHECK (projection_slot IN (0, 1)),
    conversation_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    text TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    sentiment TEXT NOT NULL CHECK (sentiment IN ('positive', 'neutral', 'negative', 'unknown')),
    PRIMARY KEY (creator_account_id, projection_slot, message_id),
    FOREIGN KEY (creator_account_id, projection_slot, conversation_id)
        REFERENCES conversation_summaries (creator_account_id, projection_slot, conversation_id)
        ON DELETE CASCADE
);

CREATE INDEX projection_messages_page
    ON projection_messages (
        creator_account_id, projection_slot, conversation_id, sent_at DESC, message_id DESC
    );

CREATE TABLE projection_analytics (
    creator_account_id TEXT NOT NULL,
    projection_slot INTEGER NOT NULL CHECK (projection_slot IN (0, 1)),
    document_json TEXT NOT NULL,
    PRIMARY KEY (creator_account_id, projection_slot),
    FOREIGN KEY (creator_account_id, projection_slot)
        REFERENCES projection_accounts (creator_account_id, projection_slot)
        ON DELETE CASCADE
);

CREATE TABLE projection_message_analysis (
    creator_account_id TEXT NOT NULL,
    projection_slot INTEGER NOT NULL CHECK (projection_slot IN (0, 1)),
    conversation_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    analysis_status TEXT NOT NULL CHECK (analysis_status IN ('available', 'unavailable')),
    sentiment TEXT NOT NULL CHECK (sentiment IN ('positive', 'neutral', 'negative', 'unknown')),
    analyzer_id TEXT,
    document_json TEXT NOT NULL,
    PRIMARY KEY (creator_account_id, projection_slot, message_id),
    FOREIGN KEY (creator_account_id, projection_slot, message_id)
        REFERENCES projection_messages (creator_account_id, projection_slot, message_id)
        ON DELETE CASCADE
);

CREATE TABLE projection_lpg_nodes (
    creator_account_id TEXT NOT NULL,
    projection_slot INTEGER NOT NULL CHECK (projection_slot IN (0, 1)),
    conversation_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    node_kind TEXT NOT NULL CHECK (node_kind IN ('conversation', 'message')),
    entity_id TEXT NOT NULL,
    document_json TEXT NOT NULL,
    PRIMARY KEY (creator_account_id, projection_slot, node_id),
    UNIQUE (creator_account_id, projection_slot, node_kind, entity_id),
    FOREIGN KEY (creator_account_id, projection_slot)
        REFERENCES projection_accounts (creator_account_id, projection_slot)
        ON DELETE CASCADE
);

CREATE TABLE projection_lpg_edges (
    creator_account_id TEXT NOT NULL,
    projection_slot INTEGER NOT NULL CHECK (projection_slot IN (0, 1)),
    conversation_id TEXT NOT NULL,
    edge_id TEXT NOT NULL,
    source_node_id TEXT NOT NULL,
    target_node_id TEXT NOT NULL,
    relationship TEXT NOT NULL CHECK (relationship IN ('CONTAINS')),
    document_json TEXT NOT NULL,
    PRIMARY KEY (creator_account_id, projection_slot, edge_id),
    FOREIGN KEY (creator_account_id, projection_slot, source_node_id)
        REFERENCES projection_lpg_nodes (creator_account_id, projection_slot, node_id)
        ON DELETE CASCADE,
    FOREIGN KEY (creator_account_id, projection_slot, target_node_id)
        REFERENCES projection_lpg_nodes (creator_account_id, projection_slot, node_id)
        ON DELETE CASCADE
);

CREATE INDEX projection_lpg_edges_source
    ON projection_lpg_edges (
        creator_account_id, projection_slot, source_node_id, relationship
    );

CREATE INDEX projection_lpg_edges_target
    ON projection_lpg_edges (
        creator_account_id, projection_slot, target_node_id, relationship
    );

CREATE TABLE projection_change_log (
    creator_account_id TEXT NOT NULL,
    projection_slot INTEGER NOT NULL CHECK (projection_slot IN (0, 1)),
    read_revision INTEGER NOT NULL CHECK (read_revision > 0),
    generation_id TEXT NOT NULL,
    projected_revision INTEGER NOT NULL CHECK (projected_revision > 0),
    change_kind TEXT NOT NULL CHECK (
        change_kind IN ('incremental', 'coverage_refresh', 'reseed')
    ),
    touched_conversations_json TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    PRIMARY KEY (creator_account_id, projection_slot, read_revision),
    UNIQUE (creator_account_id, generation_id)
);

CREATE TABLE projection_work_applied (
    creator_account_id TEXT NOT NULL,
    projection_slot INTEGER NOT NULL CHECK (projection_slot IN (0, 1)),
    work_id INTEGER NOT NULL,
    applied_at TEXT NOT NULL,
    PRIMARY KEY (creator_account_id, projection_slot, work_id)
);
