CREATE TABLE projection_generations (
    generation_id TEXT PRIMARY KEY,
    creator_account_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('building', 'validated', 'activation_pending', 'active', 'retired')
    ),
    schema_version INTEGER NOT NULL CHECK (schema_version > 0),
    build_version TEXT NOT NULL,
    canonical_revision INTEGER NOT NULL CHECK (canonical_revision >= 0),
    canonical_content_digest TEXT NOT NULL,
    canonical_high_water_json TEXT NOT NULL CHECK (
        json_valid(canonical_high_water_json)
        AND json_type(canonical_high_water_json) = 'object'
    ),
    pipeline_revision TEXT NOT NULL,
    pipeline_config_digest TEXT NOT NULL,
    pipeline_identity_digest TEXT NOT NULL,
    projection_digest TEXT,
    graph_digest TEXT,
    node_count INTEGER NOT NULL DEFAULT 0 CHECK (node_count >= 0),
    edge_count INTEGER NOT NULL DEFAULT 0 CHECK (edge_count >= 0),
    activation_intent_id TEXT,
    witness_sequence INTEGER CHECK (witness_sequence IS NULL OR witness_sequence > 0),
    expected_active_generation_id TEXT,
    expected_active_revision INTEGER CHECK (
        expected_active_revision IS NULL OR expected_active_revision >= 0
    ),
    owner_id TEXT NOT NULL,
    lease_expires_at TEXT NOT NULL,
    started_at TEXT NOT NULL,
    validated_at TEXT,
    activated_at TEXT,
    retired_at TEXT,
    UNIQUE (generation_id, creator_account_id)
) WITHOUT ROWID;

CREATE UNIQUE INDEX one_active_projection_generation_per_account
    ON projection_generations (creator_account_id)
    WHERE status = 'active';

CREATE INDEX projection_generations_by_account_status
    ON projection_generations (creator_account_id, status, canonical_revision);

CREATE INDEX projection_generations_by_lease
    ON projection_generations (status, lease_expires_at, owner_id);

CREATE TABLE analytics_projections (
    generation_id TEXT NOT NULL,
    creator_account_id TEXT NOT NULL,
    source_revision INTEGER NOT NULL CHECK (source_revision >= 0),
    pipeline_revision TEXT NOT NULL,
    pipeline_config_digest TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    document_json TEXT NOT NULL CHECK (
        json_valid(document_json) AND json_type(document_json) = 'object'
    ),
    PRIMARY KEY (generation_id, creator_account_id),
    FOREIGN KEY (generation_id, creator_account_id)
        REFERENCES projection_generations (generation_id, creator_account_id)
        ON DELETE CASCADE
) WITHOUT ROWID;

CREATE INDEX analytics_projections_by_account_revision
    ON analytics_projections (creator_account_id, source_revision, generation_id);

CREATE TABLE graph_nodes (
    generation_id TEXT NOT NULL,
    creator_account_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    occurred_at TEXT,
    properties_json TEXT NOT NULL CHECK (
        json_valid(properties_json) AND json_type(properties_json) = 'object'
    ),
    PRIMARY KEY (generation_id, creator_account_id, node_id),
    FOREIGN KEY (generation_id, creator_account_id)
        REFERENCES projection_generations (generation_id, creator_account_id)
        ON DELETE CASCADE
) WITHOUT ROWID;

CREATE INDEX graph_nodes_by_account_kind_time
    ON graph_nodes (
        creator_account_id, generation_id, kind, occurred_at, node_id
    );

CREATE TABLE graph_edges (
    generation_id TEXT NOT NULL,
    creator_account_id TEXT NOT NULL,
    edge_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    occurred_at TEXT,
    sequence INTEGER CHECK (sequence IS NULL OR sequence >= 0),
    properties_json TEXT NOT NULL CHECK (
        json_valid(properties_json) AND json_type(properties_json) = 'object'
    ),
    PRIMARY KEY (generation_id, creator_account_id, edge_id),
    FOREIGN KEY (generation_id, creator_account_id)
        REFERENCES projection_generations (generation_id, creator_account_id)
        ON DELETE CASCADE,
    FOREIGN KEY (generation_id, creator_account_id, source_id)
        REFERENCES graph_nodes (generation_id, creator_account_id, node_id)
        ON DELETE CASCADE,
    FOREIGN KEY (generation_id, creator_account_id, target_id)
        REFERENCES graph_nodes (generation_id, creator_account_id, node_id)
        ON DELETE CASCADE
) WITHOUT ROWID;

CREATE INDEX graph_edges_by_account_relation_time
    ON graph_edges (
        creator_account_id, generation_id, relation, occurred_at, edge_id
    );

CREATE INDEX graph_edges_by_outgoing_endpoint
    ON graph_edges (
        creator_account_id, generation_id, source_id, occurred_at, relation, edge_id
    );

CREATE INDEX graph_edges_by_incoming_endpoint
    ON graph_edges (
        creator_account_id, generation_id, target_id, occurred_at, relation, edge_id
    );

CREATE TABLE graph_partition_stats (
    generation_id TEXT NOT NULL,
    creator_account_id TEXT NOT NULL,
    source_revision INTEGER NOT NULL CHECK (source_revision >= 0),
    node_count INTEGER NOT NULL CHECK (node_count >= 0),
    edge_count INTEGER NOT NULL CHECK (edge_count >= 0),
    graph_digest TEXT NOT NULL,
    PRIMARY KEY (generation_id, creator_account_id),
    FOREIGN KEY (generation_id, creator_account_id)
        REFERENCES projection_generations (generation_id, creator_account_id)
        ON DELETE CASCADE
) WITHOUT ROWID;

CREATE TABLE graph_algorithm_metrics (
    generation_id TEXT NOT NULL,
    creator_account_id TEXT NOT NULL,
    metric_kind TEXT NOT NULL CHECK (metric_kind IN ('centrality', 'community')),
    algorithm TEXT NOT NULL,
    parameter_hash TEXT NOT NULL,
    result_json TEXT NOT NULL CHECK (json_valid(result_json)),
    computed_at TEXT NOT NULL,
    PRIMARY KEY (
        generation_id, creator_account_id, metric_kind, algorithm, parameter_hash
    ),
    FOREIGN KEY (generation_id, creator_account_id)
        REFERENCES projection_generations (generation_id, creator_account_id)
        ON DELETE CASCADE
) WITHOUT ROWID;

CREATE TRIGGER immutable_projection_document_update
BEFORE UPDATE ON analytics_projections
WHEN (SELECT status FROM projection_generations
      WHERE generation_id = OLD.generation_id) != 'building'
BEGIN
    SELECT RAISE(ABORT, 'validated projection generation is immutable');
END;

CREATE TRIGGER immutable_projection_document_insert
BEFORE INSERT ON analytics_projections
WHEN (SELECT status FROM projection_generations
      WHERE generation_id = NEW.generation_id) != 'building'
BEGIN
    SELECT RAISE(ABORT, 'validated projection generation is immutable');
END;

CREATE TRIGGER immutable_projection_document_delete
BEFORE DELETE ON analytics_projections
WHEN (SELECT status FROM projection_generations
      WHERE generation_id = OLD.generation_id) IN ('validated', 'activation_pending', 'active')
BEGIN
    SELECT RAISE(ABORT, 'published projection generation is immutable');
END;

CREATE TRIGGER immutable_graph_node_update
BEFORE UPDATE ON graph_nodes
WHEN (SELECT status FROM projection_generations
      WHERE generation_id = OLD.generation_id) != 'building'
BEGIN
    SELECT RAISE(ABORT, 'validated projection generation is immutable');
END;

CREATE TRIGGER immutable_graph_node_insert
BEFORE INSERT ON graph_nodes
WHEN (SELECT status FROM projection_generations
      WHERE generation_id = NEW.generation_id) != 'building'
BEGIN
    SELECT RAISE(ABORT, 'validated projection generation is immutable');
END;

CREATE TRIGGER immutable_graph_edge_update
BEFORE UPDATE ON graph_edges
WHEN (SELECT status FROM projection_generations
      WHERE generation_id = OLD.generation_id) != 'building'
BEGIN
    SELECT RAISE(ABORT, 'validated projection generation is immutable');
END;

CREATE TRIGGER immutable_graph_edge_insert
BEFORE INSERT ON graph_edges
WHEN (SELECT status FROM projection_generations
      WHERE generation_id = NEW.generation_id) != 'building'
BEGIN
    SELECT RAISE(ABORT, 'validated projection generation is immutable');
END;

CREATE TRIGGER immutable_graph_stats_update
BEFORE UPDATE ON graph_partition_stats
WHEN (SELECT status FROM projection_generations
      WHERE generation_id = OLD.generation_id) != 'building'
BEGIN
    SELECT RAISE(ABORT, 'validated projection generation is immutable');
END;

CREATE TRIGGER immutable_graph_stats_insert
BEFORE INSERT ON graph_partition_stats
WHEN (SELECT status FROM projection_generations
      WHERE generation_id = NEW.generation_id) != 'building'
BEGIN
    SELECT RAISE(ABORT, 'validated projection generation is immutable');
END;

CREATE TRIGGER immutable_graph_stats_delete
BEFORE DELETE ON graph_partition_stats
WHEN (SELECT status FROM projection_generations
      WHERE generation_id = OLD.generation_id) IN ('validated', 'activation_pending', 'active')
BEGIN
    SELECT RAISE(ABORT, 'published projection generation is immutable');
END;

CREATE TRIGGER immutable_active_graph_node_delete
BEFORE DELETE ON graph_nodes
WHEN (SELECT status FROM projection_generations
      WHERE generation_id = OLD.generation_id) IN ('validated', 'activation_pending', 'active')
BEGIN
    SELECT RAISE(ABORT, 'published projection generation is immutable');
END;

CREATE TRIGGER immutable_active_graph_edge_delete
BEFORE DELETE ON graph_edges
WHEN (SELECT status FROM projection_generations
      WHERE generation_id = OLD.generation_id) IN ('validated', 'activation_pending', 'active')
BEGIN
    SELECT RAISE(ABORT, 'published projection generation is immutable');
END;
