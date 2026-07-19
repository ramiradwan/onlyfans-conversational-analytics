CREATE TABLE analytics_projection_witness_sequences (
    creator_account_id TEXT PRIMARY KEY,
    last_witness_sequence INTEGER NOT NULL CHECK (last_witness_sequence >= 0)
);

CREATE TABLE analytics_projection_activation_intents (
    intent_id TEXT PRIMARY KEY,
    creator_account_id TEXT NOT NULL,
    generation_id TEXT NOT NULL UNIQUE,
    canonical_revision INTEGER NOT NULL CHECK (canonical_revision >= 0),
    canonical_content_digest TEXT NOT NULL,
    projection_digest TEXT NOT NULL,
    graph_digest TEXT NOT NULL,
    pipeline_revision TEXT NOT NULL,
    pipeline_config_digest TEXT NOT NULL,
    pipeline_identity_digest TEXT NOT NULL,
    witness_sequence INTEGER NOT NULL CHECK (witness_sequence > 0),
    state TEXT NOT NULL CHECK (state IN ('reserved', 'completed', 'cancelled')),
    reserved_at TEXT NOT NULL,
    completed_at TEXT,
    cancelled_at TEXT,
    UNIQUE (creator_account_id, witness_sequence)
);

CREATE UNIQUE INDEX one_reserved_analytics_projection_activation_per_account
    ON analytics_projection_activation_intents (creator_account_id)
    WHERE state = 'reserved';

CREATE INDEX analytics_projection_activation_intents_by_state
    ON analytics_projection_activation_intents (state, creator_account_id, reserved_at);

CREATE INDEX completed_analytics_projection_witnesses_by_account_revision
    ON analytics_projection_activation_intents (
        creator_account_id, canonical_revision, witness_sequence
    )
    WHERE state = 'completed';

CREATE TRIGGER analytics_projection_activation_identity_is_immutable
BEFORE UPDATE OF
    creator_account_id,
    generation_id,
    canonical_revision,
    canonical_content_digest,
    projection_digest,
    graph_digest,
    pipeline_revision,
    pipeline_config_digest,
    pipeline_identity_digest,
    witness_sequence,
    reserved_at
ON analytics_projection_activation_intents
BEGIN
    SELECT RAISE(ABORT, 'projection activation identity is immutable');
END;

CREATE TRIGGER analytics_projection_activation_state_is_terminal
BEFORE UPDATE OF state ON analytics_projection_activation_intents
WHEN OLD.state != 'reserved' OR NEW.state NOT IN ('completed', 'cancelled')
BEGIN
    SELECT RAISE(ABORT, 'projection activation state is terminal');
END;
