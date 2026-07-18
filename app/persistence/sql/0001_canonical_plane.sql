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

CREATE TABLE canonical_chats (
    creator_account_id TEXT NOT NULL,
    agent_installation_id TEXT NOT NULL,
    agent_stream_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    document_json TEXT NOT NULL,
    PRIMARY KEY (creator_account_id, agent_installation_id, agent_stream_id, chat_id),
    FOREIGN KEY (creator_account_id, agent_installation_id, agent_stream_id)
        REFERENCES ingest_streams (creator_account_id, agent_installation_id, agent_stream_id)
        ON DELETE CASCADE
);

CREATE TABLE canonical_messages (
    creator_account_id TEXT NOT NULL,
    agent_installation_id TEXT NOT NULL,
    agent_stream_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    document_json TEXT NOT NULL,
    PRIMARY KEY (creator_account_id, agent_installation_id, agent_stream_id, message_id),
    FOREIGN KEY (creator_account_id, agent_installation_id, agent_stream_id, chat_id)
        REFERENCES canonical_chats (creator_account_id, agent_installation_id, agent_stream_id, chat_id)
        ON DELETE CASCADE
);

CREATE INDEX canonical_messages_by_chat
    ON canonical_messages (creator_account_id, agent_installation_id, agent_stream_id, chat_id);

CREATE TABLE raw_ingest_events (
    creator_account_id TEXT NOT NULL,
    agent_installation_id TEXT NOT NULL,
    agent_stream_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    source_seq INTEGER NOT NULL CHECK (source_seq > 0),
    fingerprint TEXT NOT NULL,
    event_json TEXT,
    committed_at TEXT NOT NULL,
    PRIMARY KEY (creator_account_id, agent_installation_id, agent_stream_id, event_id),
    UNIQUE (creator_account_id, agent_installation_id, agent_stream_id, source_seq),
    FOREIGN KEY (creator_account_id, agent_installation_id, agent_stream_id)
        REFERENCES ingest_streams (creator_account_id, agent_installation_id, agent_stream_id)
        ON DELETE CASCADE
);

CREATE TABLE raw_ingest_snapshots (
    creator_account_id TEXT NOT NULL,
    agent_installation_id TEXT NOT NULL,
    agent_stream_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    through_seq INTEGER NOT NULL CHECK (through_seq >= 0),
    fingerprint TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    PRIMARY KEY (creator_account_id, agent_installation_id, agent_stream_id, snapshot_id),
    FOREIGN KEY (creator_account_id, agent_installation_id, agent_stream_id)
        REFERENCES ingest_streams (creator_account_id, agent_installation_id, agent_stream_id)
        ON DELETE CASCADE
);

CREATE TABLE account_read_models (
    creator_account_id TEXT PRIMARY KEY,
    view_revision INTEGER NOT NULL CHECK (view_revision >= 0)
);

CREATE TRIGGER account_view_revision_is_contiguous
BEFORE UPDATE OF view_revision ON account_read_models
WHEN NEW.view_revision != OLD.view_revision + 1
BEGIN
    SELECT RAISE(ABORT, 'view revision must advance contiguously');
END;

CREATE TABLE read_model_chats (
    creator_account_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    document_json TEXT NOT NULL,
    PRIMARY KEY (creator_account_id, conversation_id),
    FOREIGN KEY (creator_account_id) REFERENCES account_read_models (creator_account_id)
        ON DELETE CASCADE
);

CREATE TABLE read_model_messages (
    creator_account_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    document_json TEXT NOT NULL,
    PRIMARY KEY (creator_account_id, message_id),
    UNIQUE (creator_account_id, conversation_id, ordinal),
    FOREIGN KEY (creator_account_id, conversation_id)
        REFERENCES read_model_chats (creator_account_id, conversation_id)
        ON DELETE CASCADE
);

CREATE TABLE config_documents (
    creator_account_id TEXT NOT NULL,
    config_revision TEXT NOT NULL,
    revision_sequence INTEGER NOT NULL CHECK (revision_sequence >= 0),
    config_schema_version TEXT NOT NULL,
    digest TEXT NOT NULL,
    etag TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    document_json TEXT NOT NULL,
    PRIMARY KEY (creator_account_id, config_revision),
    UNIQUE (creator_account_id, revision_sequence)
);

CREATE TRIGGER config_documents_are_immutable
BEFORE UPDATE ON config_documents
BEGIN
    SELECT RAISE(ABORT, 'configuration documents are immutable');
END;

CREATE TRIGGER config_document_revision_is_monotonic
BEFORE INSERT ON config_documents
WHEN NEW.revision_sequence <= COALESCE(
    (
        SELECT MAX(revision_sequence) FROM config_documents
        WHERE creator_account_id = NEW.creator_account_id
    ),
    -1
)
BEGIN
    SELECT RAISE(ABORT, 'configuration revision must increase monotonically');
END;

CREATE TABLE config_required (
    creator_account_id TEXT PRIMARY KEY,
    config_revision TEXT NOT NULL,
    FOREIGN KEY (creator_account_id, config_revision)
        REFERENCES config_documents (creator_account_id, config_revision)
);

CREATE TABLE config_installations (
    creator_account_id TEXT NOT NULL,
    agent_installation_id TEXT NOT NULL,
    required_config_revision TEXT NOT NULL,
    applied_config_revision TEXT,
    last_failure TEXT,
    PRIMARY KEY (creator_account_id, agent_installation_id),
    FOREIGN KEY (creator_account_id, required_config_revision)
        REFERENCES config_documents (creator_account_id, config_revision),
    FOREIGN KEY (creator_account_id, applied_config_revision)
        REFERENCES config_documents (creator_account_id, config_revision)
);

CREATE TABLE commands (
    command_id TEXT PRIMARY KEY,
    creator_account_id TEXT NOT NULL,
    action_json TEXT NOT NULL,
    deadline TEXT NOT NULL,
    idempotency_policy TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('issued', 'accepted', 'succeeded', 'failed', 'unknown')),
    connection_id TEXT,
    fencing_token TEXT,
    delivery_attempts INTEGER NOT NULL CHECK (delivery_attempts >= 0),
    failure_reason TEXT,
    result_apply_count INTEGER NOT NULL CHECK (result_apply_count >= 0)
);

CREATE INDEX commands_by_account ON commands (creator_account_id, issued_at, command_id);

CREATE TABLE command_transitions (
    command_id TEXT NOT NULL,
    transition_index INTEGER NOT NULL CHECK (transition_index >= 0),
    state TEXT NOT NULL CHECK (state IN ('issued', 'accepted', 'succeeded', 'failed', 'unknown')),
    occurred_at TEXT NOT NULL,
    detail TEXT,
    PRIMARY KEY (command_id, transition_index),
    FOREIGN KEY (command_id) REFERENCES commands (command_id) ON DELETE CASCADE
);

CREATE TABLE command_results (
    command_id TEXT PRIMARY KEY,
    result_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('accepted', 'succeeded', 'failed')),
    completed_at TEXT NOT NULL,
    output_json TEXT,
    error_json TEXT,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (command_id) REFERENCES commands (command_id) ON DELETE CASCADE
);

CREATE TABLE command_result_receipts (
    command_id TEXT NOT NULL,
    receipt_index INTEGER NOT NULL CHECK (receipt_index >= 0),
    result_id TEXT NOT NULL,
    received_at TEXT NOT NULL,
    duplicate INTEGER NOT NULL CHECK (duplicate IN (0, 1)),
    late INTEGER NOT NULL CHECK (late IN (0, 1)),
    PRIMARY KEY (command_id, receipt_index),
    FOREIGN KEY (command_id) REFERENCES commands (command_id) ON DELETE CASCADE
);
