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
