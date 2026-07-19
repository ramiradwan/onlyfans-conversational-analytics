ALTER TABLE analytics_projection_activation_intents ADD COLUMN account_ref TEXT;
ALTER TABLE analytics_projection_activation_intents ADD COLUMN writer_owner_id TEXT;
ALTER TABLE analytics_projection_activation_intents ADD COLUMN writer_owner_pid INTEGER;
ALTER TABLE analytics_projection_activation_intents ADD COLUMN writer_process_started_at TEXT;
ALTER TABLE analytics_projection_activation_intents ADD COLUMN writer_instance_nonce TEXT;
ALTER TABLE analytics_projection_activation_intents ADD COLUMN writer_capability_digest TEXT;
ALTER TABLE analytics_projection_activation_intents ADD COLUMN publication_capability_digest TEXT;

CREATE TABLE analytics_projection_publication_epochs (
    publication_epoch TEXT PRIMARY KEY,
    scheduler_owner_id TEXT NOT NULL,
    scheduler_capability_digest TEXT NOT NULL CHECK (
        length(scheduler_capability_digest)=71
        AND substr(scheduler_capability_digest,1,7)='sha256:'
        AND substr(scheduler_capability_digest,8) NOT GLOB '*[^0-9a-f]*'
    ),
    state TEXT NOT NULL CHECK (state IN ('open','revoked')),
    opened_at TEXT NOT NULL,
    revoked_at TEXT
) WITHOUT ROWID;

CREATE TRIGGER canonical_analytics_projection_epoch_identity_immutable
BEFORE UPDATE OF publication_epoch,scheduler_owner_id,
    scheduler_capability_digest,opened_at
ON analytics_projection_publication_epochs
BEGIN
    SELECT RAISE(ABORT,'canonical_analytics_projection_epoch_identity_immutable');
END;

CREATE TRIGGER canonical_analytics_projection_epoch_monotonic
BEFORE UPDATE OF state ON analytics_projection_publication_epochs
WHEN NOT (OLD.state='open' AND NEW.state='revoked')
BEGIN
    SELECT RAISE(ABORT,'canonical_analytics_projection_epoch_transition_invalid');
END;

CREATE TRIGGER canonical_analytics_projection_epoch_delete_blocked
BEFORE DELETE ON analytics_projection_publication_epochs
BEGIN
    SELECT RAISE(ABORT,'canonical_analytics_projection_epoch_delete_blocked');
END;

DROP TRIGGER analytics_projection_activation_identity_is_immutable;
DROP TRIGGER analytics_projection_activation_state_is_terminal;

-- Projection v3 discards every v1/v2 generation. Its old canonical witnesses
-- therefore cannot remain publication authority after this upgrade.
UPDATE analytics_projection_activation_intents
SET state='cancelled',
    cancelled_at=COALESCE(
        cancelled_at,
        strftime('%Y-%m-%dT%H:%M:%fZ','now')
    )
WHERE state IN ('reserved','completed');

CREATE TRIGGER analytics_projection_activation_v5_identity_required
BEFORE INSERT ON analytics_projection_activation_intents
WHEN NEW.account_ref IS NULL
  OR length(NEW.account_ref)!=67
  OR substr(NEW.account_ref,1,3)!='a1:'
  OR substr(NEW.account_ref,4) GLOB '*[^0-9a-f]*'
  OR NEW.writer_owner_id IS NULL
  OR NEW.writer_owner_pid IS NULL OR NEW.writer_owner_pid<=0
  OR NEW.writer_process_started_at IS NULL
  OR NEW.writer_instance_nonce IS NULL
  OR NEW.writer_capability_digest IS NULL
  OR length(NEW.writer_capability_digest)!=71
  OR substr(NEW.writer_capability_digest,1,7)!='sha256:'
  OR substr(NEW.writer_capability_digest,8) GLOB '*[^0-9a-f]*'
  OR NEW.publication_capability_digest IS NULL
  OR length(NEW.publication_capability_digest)!=71
  OR substr(NEW.publication_capability_digest,1,7)!='sha256:'
  OR substr(NEW.publication_capability_digest,8) GLOB '*[^0-9a-f]*'
BEGIN
    SELECT RAISE(ABORT,'analytics_projection_activation_v5_identity_required');
END;

CREATE TRIGGER analytics_projection_activation_identity_is_immutable
BEFORE UPDATE OF
    creator_account_id,
    account_ref,
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
    publication_epoch,
    writer_owner_id,
    writer_owner_pid,
    writer_process_started_at,
    writer_instance_nonce,
    writer_capability_digest,
    publication_capability_digest
ON analytics_projection_activation_intents
BEGIN
    SELECT RAISE(ABORT,'analytics_projection_activation_identity_immutable');
END;

CREATE TRIGGER analytics_projection_activation_state_is_terminal
BEFORE UPDATE OF state ON analytics_projection_activation_intents
WHEN NOT (
       (OLD.state='reserved' AND NEW.state IN ('completed','cancelled'))
    OR (OLD.state='completed' AND NEW.state='cancelled')
)
BEGIN
    SELECT RAISE(ABORT,'analytics_projection_activation_state_invalid');
END;
