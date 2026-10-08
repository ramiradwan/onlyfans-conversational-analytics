-- Rebuildable graph-unit and canonical-segment caches for incremental graph assembly.

CREATE TABLE conversation_graph_units (
    creator_account_id TEXT NOT NULL CHECK(length(creator_account_id)=67 AND substr(creator_account_id,1,3)='a1:' AND substr(creator_account_id,4) NOT GLOB '*[^0-9a-f]*'),
    unit_id TEXT NOT NULL CHECK(length(unit_id)=64 AND unit_id NOT GLOB '*[^0-9a-f]*'),
    graph_digest TEXT NOT NULL CHECK(length(graph_digest)=71 AND substr(graph_digest,1,7)='sha256:' AND substr(graph_digest,8) NOT GLOB '*[^0-9a-f]*'),
    node_count INTEGER NOT NULL CHECK(node_count>=1),
    edge_count INTEGER NOT NULL CHECK(edge_count>=0),
    node_ids BLOB NOT NULL CHECK(typeof(node_ids)='blob' AND length(node_ids)>0 AND length(node_ids)<=134217728),
    edge_ids BLOB NOT NULL CHECK(typeof(edge_ids)='blob' AND length(edge_ids)>0 AND length(edge_ids)<=134217728),
    PRIMARY KEY(creator_account_id,unit_id)
) WITHOUT ROWID;

CREATE TABLE conversation_graph_refs (
    generation_id TEXT NOT NULL,
    creator_account_id TEXT NOT NULL,
    conversation_ref TEXT NOT NULL CHECK(length(conversation_ref)=67 AND substr(conversation_ref,1,3)='c1:' AND substr(conversation_ref,4) NOT GLOB '*[^0-9a-f]*'),
    input_digest TEXT NOT NULL CHECK(length(input_digest)=71 AND substr(input_digest,1,7)='sha256:' AND substr(input_digest,8) NOT GLOB '*[^0-9a-f]*'),
    config_digest TEXT NOT NULL CHECK(length(config_digest)=71 AND substr(config_digest,1,7)='sha256:' AND substr(config_digest,8) NOT GLOB '*[^0-9a-f]*'),
    retention_cutoff TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    participant_ref TEXT NOT NULL CHECK(length(participant_ref)=67 AND substr(participant_ref,1,3)='p1:' AND substr(participant_ref,4) NOT GLOB '*[^0-9a-f]*'),
    started_at TEXT,
    ended_at TEXT,
    unit_id TEXT NOT NULL,
    PRIMARY KEY(generation_id,creator_account_id,conversation_ref),
    FOREIGN KEY(generation_id,creator_account_id)
        REFERENCES projection_generations(generation_id,creator_account_id) ON DELETE CASCADE,
    FOREIGN KEY(creator_account_id,unit_id)
        REFERENCES conversation_graph_units(creator_account_id,unit_id)
) WITHOUT ROWID;
CREATE INDEX conversation_graph_refs_by_unit
ON conversation_graph_refs(creator_account_id,unit_id);

CREATE TRIGGER conversation_graph_units_immutable
BEFORE UPDATE ON conversation_graph_units
BEGIN SELECT RAISE(ABORT,'conversation_graph_unit_immutable'); END;

CREATE TRIGGER conversation_graph_units_replace_blocked
BEFORE INSERT ON conversation_graph_units
WHEN EXISTS(
    SELECT 1 FROM conversation_graph_units
    WHERE creator_account_id=NEW.creator_account_id AND unit_id=NEW.unit_id
)
BEGIN SELECT RAISE(ABORT,'conversation_graph_unit_immutable'); END;

CREATE TRIGGER conversation_graph_units_building
BEFORE INSERT ON conversation_graph_units
WHEN NOT EXISTS(
    SELECT 1 FROM projection_generations
    WHERE creator_account_id=NEW.creator_account_id AND status='building'
)
BEGIN SELECT RAISE(ABORT,'conversation_graph_unit_requires_building_generation'); END;

CREATE TRIGGER conversation_graph_units_referenced
BEFORE DELETE ON conversation_graph_units
WHEN EXISTS(
    SELECT 1 FROM conversation_graph_refs
    WHERE creator_account_id=OLD.creator_account_id AND unit_id=OLD.unit_id
)
BEGIN SELECT RAISE(ABORT,'conversation_graph_unit_referenced'); END;

CREATE TRIGGER conversation_graph_refs_building
BEFORE INSERT ON conversation_graph_refs
WHEN COALESCE((
    SELECT status FROM projection_generations
    WHERE generation_id=NEW.generation_id
      AND creator_account_id=NEW.creator_account_id
),'')!='building'
BEGIN SELECT RAISE(ABORT,'conversation_graph_reference_requires_building_generation'); END;

CREATE TRIGGER conversation_graph_refs_immutable
BEFORE UPDATE ON conversation_graph_refs
BEGIN SELECT RAISE(ABORT,'conversation_graph_reference_immutable'); END;

CREATE TRIGGER conversation_graph_refs_delete
BEFORE DELETE ON conversation_graph_refs
WHEN COALESCE((
    SELECT status FROM projection_generations
    WHERE generation_id=OLD.generation_id
      AND creator_account_id=OLD.creator_account_id
),'') NOT IN ('','building','retired')
BEGIN SELECT RAISE(ABORT,'conversation_graph_reference_delete_blocked'); END;

CREATE TRIGGER conversation_graph_refs_replace_blocked
BEFORE INSERT ON conversation_graph_refs
WHEN EXISTS(
    SELECT 1 FROM conversation_graph_refs
    WHERE generation_id=NEW.generation_id
      AND creator_account_id=NEW.creator_account_id
      AND conversation_ref=NEW.conversation_ref
)
BEGIN SELECT RAISE(ABORT,'conversation_graph_reference_immutable'); END;

CREATE TRIGGER conversation_graph_unit_reclaim
AFTER DELETE ON conversation_graph_refs
BEGIN
    DELETE FROM conversation_graph_units
    WHERE creator_account_id=OLD.creator_account_id
      AND unit_id=OLD.unit_id
      AND NOT EXISTS(
          SELECT 1 FROM conversation_graph_refs
          WHERE creator_account_id=OLD.creator_account_id
            AND unit_id=OLD.unit_id
      );
END;

CREATE TRIGGER conversation_graph_refs_retired_cleanup
AFTER UPDATE OF status ON projection_generations
WHEN NEW.status='retired'
BEGIN
    DELETE FROM conversation_graph_refs
    WHERE generation_id=NEW.generation_id
      AND creator_account_id=NEW.creator_account_id;
END;

CREATE TABLE graph_segment_chunks (
    creator_account_id TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('node','edge')),
    record_count INTEGER NOT NULL CHECK(record_count>=1),
    canonical_bytes BLOB NOT NULL CHECK(typeof(canonical_bytes)='blob' AND length(canonical_bytes)>0 AND length(canonical_bytes)<=134217728),
    canonical_digest TEXT NOT NULL CHECK(length(canonical_digest)=64 AND canonical_digest NOT GLOB '*[^0-9a-f]*'),
    PRIMARY KEY(creator_account_id,segment_id),
    FOREIGN KEY(creator_account_id,segment_id)
        REFERENCES graph_segments(creator_account_id,segment_id) ON DELETE CASCADE
) WITHOUT ROWID;

CREATE TRIGGER graph_segment_chunks_immutable
BEFORE UPDATE ON graph_segment_chunks
BEGIN SELECT RAISE(ABORT,'graph_segment_chunk_immutable'); END;

CREATE TRIGGER graph_segment_chunks_replace_blocked
BEFORE INSERT ON graph_segment_chunks
WHEN EXISTS(
    SELECT 1 FROM graph_segment_chunks
    WHERE creator_account_id=NEW.creator_account_id
      AND segment_id=NEW.segment_id
)
BEGIN SELECT RAISE(ABORT,'graph_segment_chunk_immutable'); END;

CREATE TRIGGER graph_segment_chunks_building
BEFORE INSERT ON graph_segment_chunks
WHEN NOT EXISTS(
    SELECT 1 FROM graph_segments s
    JOIN generation_graph_segments m USING(creator_account_id,segment_id)
    JOIN projection_generations g USING(creator_account_id,generation_id)
    WHERE s.creator_account_id=NEW.creator_account_id
      AND s.segment_id=NEW.segment_id
      AND s.kind=NEW.kind
      AND g.status='building'
)
BEGIN SELECT RAISE(ABORT,'graph_segment_chunk_requires_building_generation'); END;

CREATE TRIGGER graph_segment_chunks_referenced
BEFORE DELETE ON graph_segment_chunks
WHEN EXISTS(
    SELECT 1 FROM generation_graph_segments
    WHERE creator_account_id=OLD.creator_account_id
      AND segment_id=OLD.segment_id
)
BEGIN SELECT RAISE(ABORT,'graph_segment_chunk_referenced'); END;

CREATE TRIGGER graph_segment_chunks_reclaim
AFTER DELETE ON graph_segments
BEGIN
    DELETE FROM graph_segment_chunks
    WHERE creator_account_id=OLD.creator_account_id
      AND segment_id=OLD.segment_id;
END;
