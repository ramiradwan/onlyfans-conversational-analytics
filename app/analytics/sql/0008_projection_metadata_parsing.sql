DROP TRIGGER projection_query_metadata_insert;
DROP TRIGGER projection_query_metadata_bound;

CREATE TRIGGER projection_query_metadata_insert
AFTER INSERT ON analytics_projections
BEGIN
    INSERT INTO projection_query_metadata
    SELECT NEW.generation_id,NEW.creator_account_id,
           json_extract(h.value,'$[0]'),
           json_extract(h.value,'$[1]'),
           json_extract(h.value,'$[2]'),NEW.content_digest
    FROM json_each(json_array(json_extract(NEW.document_json,
         '$.projection_generation','$.creator_metrics.message_count',
         '$.creator_metrics.active_from'))) AS h;
END;

CREATE TRIGGER projection_query_metadata_bound
BEFORE INSERT ON projection_query_metadata
WHEN NOT EXISTS (
    SELECT 1 FROM analytics_projections p,
         json_each(json_array(json_extract(p.document_json,
             '$.projection_generation','$.creator_metrics.message_count',
             '$.creator_metrics.active_from'))) AS h
    WHERE p.generation_id=NEW.generation_id AND p.creator_account_id=NEW.creator_account_id
      AND NEW.projection_digest IS p.content_digest
      AND NEW.projection_generation IS json_extract(h.value,'$[0]')
      AND NEW.source_message_count IS json_extract(h.value,'$[1]')
      AND NEW.first_source IS json_extract(h.value,'$[2]')
)
BEGIN SELECT RAISE(ABORT,'projection_query_metadata_mismatch'); END;
