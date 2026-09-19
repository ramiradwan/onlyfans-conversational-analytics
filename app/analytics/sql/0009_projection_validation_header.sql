DROP TRIGGER projection_query_metadata_insert;
DROP TRIGGER projection_query_metadata_bound;
DROP TRIGGER projection_query_metadata_immutable;
DROP TRIGGER projection_query_metadata_delete_guard;
DROP TRIGGER projection_document_building_insert;
DROP TRIGGER projection_document_update_blocked;
DROP TRIGGER projection_document_delete_guard;

DROP TABLE projection_query_metadata;
DROP INDEX analytics_projections_by_account_revision;
ALTER TABLE analytics_projections RENAME TO projection_documents_before_header;

CREATE TABLE analytics_projections (
    generation_id TEXT NOT NULL,
    creator_account_id TEXT NOT NULL CHECK (
        length(creator_account_id)=67
        AND substr(creator_account_id,1,3)='a1:'
        AND substr(creator_account_id,4) NOT GLOB '*[^0-9a-f]*'
    ),
    source_revision INTEGER NOT NULL CHECK (source_revision >= 0),
    pipeline_revision TEXT NOT NULL,
    pipeline_config_digest TEXT NOT NULL,
    content_digest TEXT NOT NULL CHECK (
        length(content_digest)=71 AND substr(content_digest,1,7)='sha256:'
        AND substr(content_digest,8) NOT GLOB '*[^0-9a-f]*'
    ),
    document_json TEXT NOT NULL,
    validation_header TEXT GENERATED ALWAYS AS (ofca_json_header_v1(document_json,'["message_enrichments","conversation_metrics"]')) STORED NOT NULL CHECK (
        json_valid(validation_header) AND json_type(validation_header)='object'
        AND json_extract(validation_header,'$.account_ref')=creator_account_id
        AND json_type(validation_header,'$.creator_account_id') IS NULL
        AND json_type(validation_header,'$.content_digest') IS NULL
        AND json_extract(validation_header,'$.schema_version')='3'
    ),
    PRIMARY KEY (generation_id,creator_account_id),
    FOREIGN KEY (generation_id,creator_account_id)
        REFERENCES projection_generations(generation_id,creator_account_id)
        ON DELETE CASCADE
) WITHOUT ROWID;

CREATE INDEX analytics_projections_by_account_revision
    ON analytics_projections(creator_account_id,source_revision,generation_id);



INSERT INTO analytics_projections(generation_id,creator_account_id,source_revision,pipeline_revision,pipeline_config_digest,content_digest,document_json) SELECT generation_id,creator_account_id,source_revision,pipeline_revision,pipeline_config_digest,content_digest,document_json FROM projection_documents_before_header;
DROP TABLE projection_documents_before_header;

CREATE TRIGGER projection_document_building_insert
BEFORE INSERT ON analytics_projections
WHEN COALESCE((SELECT status FROM projection_generations
    WHERE generation_id=NEW.generation_id
      AND creator_account_id=NEW.creator_account_id),'')!='building'
BEGIN SELECT RAISE(ABORT,'projection_child_write_blocked'); END;


CREATE TRIGGER projection_document_update_blocked
BEFORE UPDATE ON analytics_projections
BEGIN SELECT RAISE(ABORT,'projection_child_update_blocked'); END;


CREATE TRIGGER projection_document_delete_guard
BEFORE DELETE ON analytics_projections
WHEN COALESCE((SELECT status FROM projection_generations
    WHERE generation_id=OLD.generation_id
      AND creator_account_id=OLD.creator_account_id),'') NOT IN ('','building','retired')
BEGIN SELECT RAISE(ABORT,'projection_child_delete_blocked'); END;


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
       json_extract(validation_header,'$.projection_generation'),
       json_extract(validation_header,'$.creator_metrics.message_count'),
       json_extract(validation_header,'$.creator_metrics.active_from'),content_digest
FROM analytics_projections;

CREATE TRIGGER projection_query_metadata_insert
AFTER INSERT ON analytics_projections
BEGIN
    INSERT INTO projection_query_metadata
    SELECT NEW.generation_id,NEW.creator_account_id,
           json_extract(NEW.validation_header,'$.projection_generation'),
           json_extract(NEW.validation_header,'$.creator_metrics.message_count'),
           json_extract(NEW.validation_header,'$.creator_metrics.active_from'),NEW.content_digest;
END;

CREATE TRIGGER projection_query_metadata_bound
BEFORE INSERT ON projection_query_metadata
WHEN NOT EXISTS (
    SELECT 1 FROM analytics_projections p WHERE p.generation_id=NEW.generation_id
      AND p.creator_account_id=NEW.creator_account_id
      AND NEW.projection_digest IS p.content_digest
      AND NEW.projection_generation IS json_extract(p.validation_header,'$.projection_generation')
      AND NEW.source_message_count IS json_extract(p.validation_header,'$.creator_metrics.message_count')
      AND NEW.first_source IS json_extract(p.validation_header,'$.creator_metrics.active_from')
)
BEGIN SELECT RAISE(ABORT,'projection_query_metadata_mismatch'); END;

CREATE TRIGGER projection_query_metadata_immutable
BEFORE UPDATE ON projection_query_metadata
BEGIN SELECT RAISE(ABORT,'projection_query_metadata_immutable'); END;

CREATE TRIGGER projection_query_metadata_delete_guard
BEFORE DELETE ON projection_query_metadata
WHEN COALESCE((SELECT status FROM projection_generations WHERE generation_id=OLD.generation_id),'retired')!='retired'
BEGIN SELECT RAISE(ABORT,'projection_query_metadata_active'); END;


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
    FROM json_each(json_array(json_extract(NEW.validation_header,
         '$.projection_generation','$.creator_metrics.message_count',
         '$.creator_metrics.active_from'))) AS h;
END;

CREATE TRIGGER projection_query_metadata_bound
BEFORE INSERT ON projection_query_metadata
WHEN NOT EXISTS (
    SELECT 1 FROM analytics_projections p,
         json_each(json_array(json_extract(p.validation_header,
             '$.projection_generation','$.creator_metrics.message_count',
             '$.creator_metrics.active_from'))) AS h
    WHERE p.generation_id=NEW.generation_id AND p.creator_account_id=NEW.creator_account_id
      AND NEW.projection_digest IS p.content_digest
      AND NEW.projection_generation IS json_extract(h.value,'$[0]')
      AND NEW.source_message_count IS json_extract(h.value,'$[1]')
      AND NEW.first_source IS json_extract(h.value,'$[2]')
)
BEGIN SELECT RAISE(ABORT,'projection_query_metadata_mismatch'); END;
