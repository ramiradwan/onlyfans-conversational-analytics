ALTER TABLE analytics_projection_activation_intents ADD COLUMN expected_previous_generation_id TEXT;
ALTER TABLE analytics_projection_activation_intents ADD COLUMN expected_previous_revision INTEGER;
ALTER TABLE analytics_projection_activation_intents ADD COLUMN publication_epoch TEXT;

DROP TRIGGER analytics_projection_activation_identity_is_immutable;
DROP TRIGGER analytics_projection_activation_state_is_terminal;

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
    reserved_at,
    expected_previous_generation_id,
    expected_previous_revision,
    publication_epoch
ON analytics_projection_activation_intents
BEGIN
    SELECT RAISE(ABORT, 'analytics_projection_activation_identity_immutable');
END;

CREATE TRIGGER analytics_projection_activation_state_is_terminal
BEFORE UPDATE OF state ON analytics_projection_activation_intents
WHEN NOT (
       (OLD.state = 'reserved' AND NEW.state IN ('completed', 'cancelled'))
    OR (OLD.state = 'completed' AND NEW.state = 'cancelled')
)
BEGIN
    SELECT RAISE(ABORT, 'analytics_projection_activation_state_invalid');
END;
