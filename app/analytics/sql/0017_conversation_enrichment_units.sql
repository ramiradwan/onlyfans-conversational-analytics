-- Rebuildable immutable conversation-local enrichment units.
CREATE TABLE conversation_enrichment_units (
    creator_account_id TEXT NOT NULL CHECK(length(creator_account_id)=67 AND substr(creator_account_id,1,3)='a1:' AND substr(creator_account_id,4) NOT GLOB '*[^0-9a-f]*'),
    unit_id TEXT NOT NULL CHECK(length(unit_id)=64 AND unit_id NOT GLOB '*[^0-9a-f]*'),
    message_count INTEGER NOT NULL CHECK(message_count>=1 AND message_count<=4000000),
    first_source_at TEXT NOT NULL,
    last_source_at TEXT NOT NULL,
    metrics_json TEXT NOT NULL CHECK(json_valid(metrics_json) AND json_type(metrics_json)='object'),
    sentiment_count INTEGER NOT NULL CHECK(sentiment_count>=0),
    sentiment_numerator TEXT NOT NULL,
    sentiment_denominator TEXT NOT NULL,
    topic_count INTEGER NOT NULL CHECK(topic_count>=0),
    topic_numerator TEXT NOT NULL,
    topic_denominator TEXT NOT NULL,
    engagement_count INTEGER NOT NULL CHECK(engagement_count>=0),
    engagement_numerator TEXT NOT NULL,
    engagement_denominator TEXT NOT NULL,
    canonical_digest TEXT NOT NULL CHECK(length(canonical_digest)=64 AND canonical_digest NOT GLOB '*[^0-9a-f]*'),
    analyzer_digest TEXT NOT NULL CHECK(length(analyzer_digest)=64 AND analyzer_digest NOT GLOB '*[^0-9a-f]*'),
    message_bytes BLOB NOT NULL CHECK(typeof(message_bytes)='blob' AND length(message_bytes)>0 AND length(message_bytes)<=134217728),
    analyzer_bytes BLOB NOT NULL CHECK(typeof(analyzer_bytes)='blob' AND length(analyzer_bytes)>0 AND length(analyzer_bytes)<=134217728),
    PRIMARY KEY(creator_account_id,unit_id)
) WITHOUT ROWID;

CREATE TABLE conversation_enrichment_refs (
    generation_id TEXT NOT NULL,
    creator_account_id TEXT NOT NULL,
    conversation_ref TEXT NOT NULL CHECK(length(conversation_ref)=67 AND substr(conversation_ref,1,3)='c1:' AND substr(conversation_ref,4) NOT GLOB '*[^0-9a-f]*'),
    ordinal INTEGER NOT NULL CHECK(ordinal>=0 AND ordinal<4096),
    input_digest TEXT NOT NULL CHECK(length(input_digest)=71 AND substr(input_digest,1,7)='sha256:' AND substr(input_digest,8) NOT GLOB '*[^0-9a-f]*'),
    config_digest TEXT NOT NULL CHECK(length(config_digest)=71 AND substr(config_digest,1,7)='sha256:' AND substr(config_digest,8) NOT GLOB '*[^0-9a-f]*'),
    retention_cutoff TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    unit_id TEXT NOT NULL,
    PRIMARY KEY(generation_id,creator_account_id,conversation_ref),
    UNIQUE(generation_id,creator_account_id,ordinal),
    FOREIGN KEY(generation_id,creator_account_id)
        REFERENCES projection_generations(generation_id,creator_account_id) ON DELETE CASCADE,
    FOREIGN KEY(creator_account_id,unit_id)
        REFERENCES conversation_enrichment_units(creator_account_id,unit_id)
) WITHOUT ROWID;
CREATE INDEX conversation_enrichment_refs_by_unit
ON conversation_enrichment_refs(creator_account_id,unit_id);

CREATE TRIGGER conversation_enrichment_units_immutable BEFORE UPDATE ON conversation_enrichment_units
BEGIN SELECT RAISE(ABORT,'conversation_enrichment_unit_immutable'); END;
CREATE TRIGGER conversation_enrichment_units_replace_blocked BEFORE INSERT ON conversation_enrichment_units
WHEN EXISTS(SELECT 1 FROM conversation_enrichment_units WHERE creator_account_id=NEW.creator_account_id AND unit_id=NEW.unit_id)
BEGIN SELECT RAISE(ABORT,'conversation_enrichment_unit_immutable'); END;
CREATE TRIGGER conversation_enrichment_units_building BEFORE INSERT ON conversation_enrichment_units
WHEN NOT EXISTS(SELECT 1 FROM projection_generations WHERE creator_account_id=NEW.creator_account_id AND status='building')
BEGIN SELECT RAISE(ABORT,'conversation_enrichment_unit_requires_building_generation'); END;
CREATE TRIGGER conversation_enrichment_units_referenced BEFORE DELETE ON conversation_enrichment_units
WHEN EXISTS(SELECT 1 FROM conversation_enrichment_refs WHERE creator_account_id=OLD.creator_account_id AND unit_id=OLD.unit_id)
BEGIN SELECT RAISE(ABORT,'conversation_enrichment_unit_referenced'); END;

CREATE TRIGGER conversation_enrichment_refs_building BEFORE INSERT ON conversation_enrichment_refs
WHEN COALESCE((SELECT status FROM projection_generations WHERE generation_id=NEW.generation_id AND creator_account_id=NEW.creator_account_id),'')!='building'
BEGIN SELECT RAISE(ABORT,'conversation_enrichment_reference_requires_building_generation'); END;
CREATE TRIGGER conversation_enrichment_refs_immutable BEFORE UPDATE ON conversation_enrichment_refs
BEGIN SELECT RAISE(ABORT,'conversation_enrichment_reference_immutable'); END;
CREATE TRIGGER conversation_enrichment_refs_delete BEFORE DELETE ON conversation_enrichment_refs
WHEN COALESCE((SELECT status FROM projection_generations WHERE generation_id=OLD.generation_id AND creator_account_id=OLD.creator_account_id),'') NOT IN ('','building','retired')
BEGIN SELECT RAISE(ABORT,'conversation_enrichment_reference_delete_blocked'); END;
CREATE TRIGGER conversation_enrichment_refs_replace_blocked BEFORE INSERT ON conversation_enrichment_refs
WHEN EXISTS(SELECT 1 FROM conversation_enrichment_refs WHERE generation_id=NEW.generation_id AND creator_account_id=NEW.creator_account_id AND conversation_ref=NEW.conversation_ref)
BEGIN SELECT RAISE(ABORT,'conversation_enrichment_reference_immutable'); END;
CREATE TRIGGER conversation_enrichment_unit_reclaim AFTER DELETE ON conversation_enrichment_refs
BEGIN
    DELETE FROM conversation_enrichment_units
    WHERE creator_account_id=OLD.creator_account_id AND unit_id=OLD.unit_id
      AND NOT EXISTS(SELECT 1 FROM conversation_enrichment_refs WHERE creator_account_id=OLD.creator_account_id AND unit_id=OLD.unit_id);
END;
CREATE TRIGGER conversation_enrichment_refs_retired_cleanup AFTER UPDATE OF status ON projection_generations
WHEN NEW.status='retired'
BEGIN
    DELETE FROM conversation_enrichment_refs WHERE generation_id=NEW.generation_id AND creator_account_id=NEW.creator_account_id;
END;

CREATE TRIGGER generation_content_conversation_enrichment_units_insert AFTER INSERT ON conversation_enrichment_units BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_conversation_enrichment_units_update AFTER UPDATE ON conversation_enrichment_units BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_conversation_enrichment_units_delete AFTER DELETE ON conversation_enrichment_units BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_conversation_enrichment_refs_insert AFTER INSERT ON conversation_enrichment_refs BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_conversation_enrichment_refs_update AFTER UPDATE ON conversation_enrichment_refs BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_conversation_enrichment_refs_delete AFTER DELETE ON conversation_enrichment_refs BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
