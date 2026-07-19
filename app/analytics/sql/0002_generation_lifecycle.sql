ALTER TABLE projection_generations ADD COLUMN publication_epoch TEXT;
ALTER TABLE projection_generations ADD COLUMN owner_pid INTEGER;
ALTER TABLE projection_generations ADD COLUMN owner_process_started_at TEXT;
ALTER TABLE projection_generations ADD COLUMN owner_instance_nonce TEXT;

CREATE TABLE projection_publication_epochs (
    publication_epoch TEXT PRIMARY KEY,
    scheduler_owner_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('open', 'revoked')),
    opened_at TEXT NOT NULL,
    revoked_at TEXT
) WITHOUT ROWID;

CREATE TRIGGER projection_publication_epoch_identity_immutable
BEFORE UPDATE OF publication_epoch, scheduler_owner_id, opened_at
ON projection_publication_epochs
BEGIN
    SELECT RAISE(ABORT, 'projection_epoch_identity_immutable');
END;

CREATE TRIGGER projection_publication_epoch_monotonic
BEFORE UPDATE OF state ON projection_publication_epochs
WHEN NOT (OLD.state = 'open' AND NEW.state = 'revoked')
BEGIN
    SELECT RAISE(ABORT, 'projection_epoch_transition_invalid');
END;

CREATE TRIGGER projection_publication_epoch_delete_blocked
BEFORE DELETE ON projection_publication_epochs
BEGIN
    SELECT RAISE(ABORT, 'projection_epoch_delete_blocked');
END;

CREATE TRIGGER projection_generation_transition_monotonic
BEFORE UPDATE OF status ON projection_generations
WHEN OLD.status != NEW.status AND NOT (
       (OLD.status = 'building' AND NEW.status IN ('validated', 'retired'))
    OR (OLD.status = 'validated' AND NEW.status IN ('activation_pending', 'retired'))
    OR (OLD.status = 'activation_pending' AND NEW.status IN ('active', 'retired'))
    OR (OLD.status = 'active' AND NEW.status = 'retired')
)
BEGIN
    SELECT RAISE(ABORT, 'projection_generation_transition_invalid');
END;

CREATE TRIGGER projection_generation_identity_immutable
BEFORE UPDATE OF
    generation_id,
    creator_account_id,
    schema_version,
    build_version,
    canonical_revision,
    canonical_content_digest,
    canonical_high_water_json,
    pipeline_revision,
    pipeline_config_digest,
    pipeline_identity_digest,
    projection_digest,
    graph_digest,
    node_count,
    edge_count,
    expected_active_generation_id,
    expected_active_revision,
    publication_epoch,
    owner_id,
    owner_pid,
    owner_process_started_at,
    owner_instance_nonce,
    started_at
ON projection_generations
WHEN OLD.status != 'building'
BEGIN
    SELECT RAISE(ABORT, 'projection_generation_identity_immutable');
END;

CREATE TRIGGER projection_generation_validation_complete
BEFORE UPDATE OF status ON projection_generations
WHEN OLD.status = 'building' AND NEW.status = 'validated' AND (
       NEW.projection_digest IS NULL
    OR NEW.graph_digest IS NULL
    OR NEW.validated_at IS NULL
)
BEGIN
    SELECT RAISE(ABORT, 'projection_generation_validation_incomplete');
END;

CREATE TRIGGER projection_generation_activation_bound
BEFORE UPDATE OF status ON projection_generations
WHEN NEW.status IN ('activation_pending', 'active') AND (
       NEW.activation_intent_id IS NULL
    OR NEW.witness_sequence IS NULL
    OR NEW.publication_epoch IS NULL
)
BEGIN
    SELECT RAISE(ABORT, 'projection_generation_activation_unbound');
END;

CREATE TRIGGER projection_generation_witness_immutable
BEFORE UPDATE OF activation_intent_id, witness_sequence
ON projection_generations
WHEN NOT (
       (OLD.activation_intent_id IS NEW.activation_intent_id
        AND OLD.witness_sequence IS NEW.witness_sequence)
    OR (OLD.status = 'validated' AND NEW.status = 'activation_pending'
        AND OLD.activation_intent_id IS NULL
        AND OLD.witness_sequence IS NULL
        AND NEW.activation_intent_id IS NOT NULL
        AND NEW.witness_sequence IS NOT NULL)
)
BEGIN
    SELECT RAISE(ABORT, 'projection_generation_witness_immutable');
END;

CREATE TRIGGER projection_generation_activation_epoch_open
BEFORE UPDATE OF status ON projection_generations
WHEN NEW.status = 'active' AND NOT EXISTS (
    SELECT 1 FROM projection_publication_epochs AS epoch
    WHERE epoch.publication_epoch = NEW.publication_epoch
      AND epoch.state = 'open'
)
BEGIN
    SELECT RAISE(ABORT, 'projection_generation_epoch_revoked');
END;

CREATE TRIGGER projection_generation_delete_retired_only
BEFORE DELETE ON projection_generations
WHEN OLD.status != 'retired'
BEGIN
    SELECT RAISE(ABORT, 'projection_generation_delete_blocked');
END;

DROP TRIGGER immutable_projection_document_update;
DROP TRIGGER immutable_projection_document_insert;
DROP TRIGGER immutable_projection_document_delete;
DROP TRIGGER immutable_graph_node_update;
DROP TRIGGER immutable_graph_node_insert;
DROP TRIGGER immutable_graph_edge_update;
DROP TRIGGER immutable_graph_edge_insert;
DROP TRIGGER immutable_graph_stats_update;
DROP TRIGGER immutable_graph_stats_insert;
DROP TRIGGER immutable_graph_stats_delete;
DROP TRIGGER immutable_active_graph_node_delete;
DROP TRIGGER immutable_active_graph_edge_delete;

CREATE TRIGGER projection_document_building_insert
BEFORE INSERT ON analytics_projections
WHEN COALESCE((SELECT status FROM projection_generations
               WHERE generation_id=NEW.generation_id
                 AND creator_account_id=NEW.creator_account_id), '') != 'building'
BEGIN
    SELECT RAISE(ABORT, 'projection_child_write_blocked');
END;

CREATE TRIGGER projection_document_building_update
BEFORE UPDATE ON analytics_projections
WHEN COALESCE((SELECT status FROM projection_generations
               WHERE generation_id=OLD.generation_id
                 AND creator_account_id=OLD.creator_account_id), '') != 'building'
  OR COALESCE((SELECT status FROM projection_generations
               WHERE generation_id=NEW.generation_id
                 AND creator_account_id=NEW.creator_account_id), '') != 'building'
BEGIN
    SELECT RAISE(ABORT, 'projection_child_write_blocked');
END;

CREATE TRIGGER projection_document_delete_guard
BEFORE DELETE ON analytics_projections
WHEN COALESCE((SELECT status FROM projection_generations
               WHERE generation_id=OLD.generation_id
                 AND creator_account_id=OLD.creator_account_id), '')
     NOT IN ('', 'building', 'retired')
BEGIN
    SELECT RAISE(ABORT, 'projection_child_delete_blocked');
END;

CREATE TRIGGER graph_node_building_insert
BEFORE INSERT ON graph_nodes
WHEN COALESCE((SELECT status FROM projection_generations
               WHERE generation_id=NEW.generation_id
                 AND creator_account_id=NEW.creator_account_id), '') != 'building'
BEGIN
    SELECT RAISE(ABORT, 'projection_child_write_blocked');
END;

CREATE TRIGGER graph_node_building_update
BEFORE UPDATE ON graph_nodes
WHEN COALESCE((SELECT status FROM projection_generations
               WHERE generation_id=OLD.generation_id
                 AND creator_account_id=OLD.creator_account_id), '') != 'building'
  OR COALESCE((SELECT status FROM projection_generations
               WHERE generation_id=NEW.generation_id
                 AND creator_account_id=NEW.creator_account_id), '') != 'building'
BEGIN
    SELECT RAISE(ABORT, 'projection_child_write_blocked');
END;

CREATE TRIGGER graph_node_delete_guard
BEFORE DELETE ON graph_nodes
WHEN COALESCE((SELECT status FROM projection_generations
               WHERE generation_id=OLD.generation_id
                 AND creator_account_id=OLD.creator_account_id), '')
     NOT IN ('', 'building', 'retired')
BEGIN
    SELECT RAISE(ABORT, 'projection_child_delete_blocked');
END;

CREATE TRIGGER graph_edge_building_insert
BEFORE INSERT ON graph_edges
WHEN COALESCE((SELECT status FROM projection_generations
               WHERE generation_id=NEW.generation_id
                 AND creator_account_id=NEW.creator_account_id), '') != 'building'
BEGIN
    SELECT RAISE(ABORT, 'projection_child_write_blocked');
END;

CREATE TRIGGER graph_edge_building_update
BEFORE UPDATE ON graph_edges
WHEN COALESCE((SELECT status FROM projection_generations
               WHERE generation_id=OLD.generation_id
                 AND creator_account_id=OLD.creator_account_id), '') != 'building'
  OR COALESCE((SELECT status FROM projection_generations
               WHERE generation_id=NEW.generation_id
                 AND creator_account_id=NEW.creator_account_id), '') != 'building'
BEGIN
    SELECT RAISE(ABORT, 'projection_child_write_blocked');
END;

CREATE TRIGGER graph_edge_delete_guard
BEFORE DELETE ON graph_edges
WHEN COALESCE((SELECT status FROM projection_generations
               WHERE generation_id=OLD.generation_id
                 AND creator_account_id=OLD.creator_account_id), '')
     NOT IN ('', 'building', 'retired')
BEGIN
    SELECT RAISE(ABORT, 'projection_child_delete_blocked');
END;

CREATE TRIGGER graph_stats_building_insert
BEFORE INSERT ON graph_partition_stats
WHEN COALESCE((SELECT status FROM projection_generations
               WHERE generation_id=NEW.generation_id
                 AND creator_account_id=NEW.creator_account_id), '') != 'building'
BEGIN
    SELECT RAISE(ABORT, 'projection_child_write_blocked');
END;

CREATE TRIGGER graph_stats_building_update
BEFORE UPDATE ON graph_partition_stats
WHEN COALESCE((SELECT status FROM projection_generations
               WHERE generation_id=OLD.generation_id
                 AND creator_account_id=OLD.creator_account_id), '') != 'building'
  OR COALESCE((SELECT status FROM projection_generations
               WHERE generation_id=NEW.generation_id
                 AND creator_account_id=NEW.creator_account_id), '') != 'building'
BEGIN
    SELECT RAISE(ABORT, 'projection_child_write_blocked');
END;

CREATE TRIGGER graph_stats_delete_guard
BEFORE DELETE ON graph_partition_stats
WHEN COALESCE((SELECT status FROM projection_generations
               WHERE generation_id=OLD.generation_id
                 AND creator_account_id=OLD.creator_account_id), '')
     NOT IN ('', 'building', 'retired')
BEGIN
    SELECT RAISE(ABORT, 'projection_child_delete_blocked');
END;

ALTER TABLE graph_algorithm_metrics ADD COLUMN activation_intent_id TEXT;
ALTER TABLE graph_algorithm_metrics ADD COLUMN witness_sequence INTEGER;
ALTER TABLE graph_algorithm_metrics ADD COLUMN publication_epoch TEXT;

CREATE TRIGGER graph_metric_active_insert
BEFORE INSERT ON graph_algorithm_metrics
WHEN NOT EXISTS (
    SELECT 1 FROM projection_generations AS generation
    WHERE generation.generation_id = NEW.generation_id
      AND generation.creator_account_id = NEW.creator_account_id
      AND generation.status = 'active'
      AND generation.activation_intent_id = NEW.activation_intent_id
      AND generation.witness_sequence = NEW.witness_sequence
      AND generation.publication_epoch = NEW.publication_epoch
)
BEGIN
    SELECT RAISE(ABORT, 'graph_metric_active_identity_mismatch');
END;

CREATE TRIGGER graph_metric_update_blocked
BEFORE UPDATE ON graph_algorithm_metrics
BEGIN
    SELECT RAISE(ABORT, 'graph_metric_update_blocked');
END;

CREATE TRIGGER graph_metric_delete_guard
BEFORE DELETE ON graph_algorithm_metrics
WHEN COALESCE((SELECT status FROM projection_generations
               WHERE generation_id=OLD.generation_id
                 AND creator_account_id=OLD.creator_account_id), '')
     NOT IN ('', 'retired')
BEGIN
    SELECT RAISE(ABORT, 'graph_metric_delete_blocked');
END;
