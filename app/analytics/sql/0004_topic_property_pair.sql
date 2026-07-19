-- Close the topic taxonomy/label pair against SQL NULL semantics.
-- SQLite evaluates NULL comparisons as unknown, so the v3 trigger admitted
-- objects with only one paired field. This forward-only migration replaces it
-- without rewriting the released v1-v3 projection history.

DROP TRIGGER topic_taxonomy_pair_insert;

CREATE TRIGGER topic_taxonomy_pair_insert
BEFORE INSERT ON graph_nodes
WHEN NEW.kind='topic' AND (
       COALESCE(json_type(NEW.properties_json,'$.taxonomy_id'),'')!='text'
    OR COALESCE(json_type(NEW.properties_json,'$.label'),'')!='text'
    OR COALESCE(json_extract(NEW.properties_json,'$.label'),'') != COALESCE(
        CASE json_extract(NEW.properties_json,'$.taxonomy_id')
            WHEN 'feedback' THEN 'Feedback' WHEN 'greeting' THEN 'Greeting'
            WHEN 'media' THEN 'Media' WHEN 'pricing' THEN 'Pricing'
            WHEN 'scheduling' THEN 'Scheduling' WHEN 'support' THEN 'Support'
        END,
        ''
    )
)
BEGIN
    SELECT RAISE(ABORT,'graph_property_invalid');
END;

ALTER TABLE projection_store_identity RENAME TO projection_store_identity_v3;

CREATE TABLE projection_store_identity (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    store_id TEXT NOT NULL CHECK (
        length(store_id) = 32
        AND store_id NOT GLOB '*[^0-9a-f]*'
    ),
    schema_identity TEXT NOT NULL CHECK (schema_identity = 'projection-v4')
) WITHOUT ROWID;

INSERT INTO projection_store_identity(singleton, store_id, schema_identity)
SELECT singleton, store_id, 'projection-v4' FROM projection_store_identity_v3;

DROP TABLE projection_store_identity_v3;
