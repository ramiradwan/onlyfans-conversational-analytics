CREATE TABLE projection_query_metadata (
    generation_id TEXT NOT NULL,
    creator_account_id TEXT NOT NULL,
    projection_generation INTEGER NOT NULL CHECK (projection_generation>=1),
    source_message_count INTEGER NOT NULL CHECK (source_message_count>=0),
    first_source TEXT,
    projection_digest TEXT NOT NULL,
    PRIMARY KEY(generation_id,creator_account_id),
    FOREIGN KEY(generation_id,creator_account_id)
        REFERENCES analytics_projections(generation_id,creator_account_id) ON DELETE CASCADE
) WITHOUT ROWID;

INSERT INTO projection_query_metadata
SELECT generation_id,creator_account_id,
       json_extract(document_json,'$.projection_generation'),
       json_extract(document_json,'$.creator_metrics.message_count'),
       json_extract(document_json,'$.creator_metrics.active_from'),content_digest
FROM analytics_projections;

CREATE TRIGGER projection_query_metadata_insert
AFTER INSERT ON analytics_projections
BEGIN
    INSERT INTO projection_query_metadata
    SELECT NEW.generation_id,NEW.creator_account_id,
           json_extract(NEW.document_json,'$.projection_generation'),
           json_extract(NEW.document_json,'$.creator_metrics.message_count'),
           json_extract(NEW.document_json,'$.creator_metrics.active_from'),NEW.content_digest;
END;

CREATE TRIGGER projection_query_metadata_bound
BEFORE INSERT ON projection_query_metadata
WHEN NOT EXISTS (
    SELECT 1 FROM analytics_projections p WHERE p.generation_id=NEW.generation_id
      AND p.creator_account_id=NEW.creator_account_id
      AND NEW.projection_digest IS p.content_digest
      AND NEW.projection_generation IS json_extract(p.document_json,'$.projection_generation')
      AND NEW.source_message_count IS json_extract(p.document_json,'$.creator_metrics.message_count')
      AND NEW.first_source IS json_extract(p.document_json,'$.creator_metrics.active_from')
)
BEGIN SELECT RAISE(ABORT,'projection_query_metadata_mismatch'); END;

CREATE TRIGGER projection_query_metadata_immutable
BEFORE UPDATE ON projection_query_metadata
BEGIN SELECT RAISE(ABORT,'projection_query_metadata_immutable'); END;

CREATE TRIGGER projection_query_metadata_delete_guard
BEFORE DELETE ON projection_query_metadata
WHEN COALESCE((SELECT status FROM projection_generations WHERE generation_id=OLD.generation_id),'retired')!='retired'
BEGIN SELECT RAISE(ABORT,'projection_query_metadata_active'); END;
