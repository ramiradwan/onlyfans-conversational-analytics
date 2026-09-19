CREATE TABLE conversation_fragments (
    generation_id TEXT NOT NULL,
    creator_account_id TEXT NOT NULL,
    conversation_ref TEXT NOT NULL CHECK (length(conversation_ref)=67 AND substr(conversation_ref,1,3)='c1:' AND substr(conversation_ref,4) NOT GLOB '*[^0-9a-f]*'),
    input_digest TEXT NOT NULL CHECK (length(input_digest)=71 AND substr(input_digest,1,7)='sha256:' AND substr(input_digest,8) NOT GLOB '*[^0-9a-f]*'),
    config_digest TEXT NOT NULL CHECK (length(config_digest)=71 AND substr(config_digest,1,7)='sha256:' AND substr(config_digest,8) NOT GLOB '*[^0-9a-f]*'),
    document_json TEXT NOT NULL CHECK (
        length(CAST(document_json AS BLOB))<=8388608 AND json_valid(document_json)
        AND COALESCE(json_type(document_json,'$.account_ref'),'')='text'
        AND json_extract(document_json,'$.account_ref')=creator_account_id
        AND COALESCE(json_type(document_json,'$.conversation_ref'),'')='text'
        AND json_extract(document_json,'$.conversation_ref')=conversation_ref
        AND COALESCE(json_type(document_json,'$.input_digest'),'')='text'
        AND json_extract(document_json,'$.input_digest')=input_digest
        AND COALESCE(json_type(document_json,'$.config_digest'),'')='text'
        AND json_extract(document_json,'$.config_digest')=config_digest
    ),
    document_digest TEXT NOT NULL CHECK (length(document_digest)=64 AND document_digest NOT GLOB '*[^0-9a-f]*'),
    PRIMARY KEY (generation_id,creator_account_id,conversation_ref),
    FOREIGN KEY (generation_id,creator_account_id)
        REFERENCES projection_generations(generation_id,creator_account_id) ON DELETE CASCADE
) WITHOUT ROWID;

CREATE TRIGGER conversation_fragments_building_insert
BEFORE INSERT ON conversation_fragments WHEN COALESCE((
    SELECT status FROM projection_generations WHERE generation_id=NEW.generation_id
        AND creator_account_id=NEW.creator_account_id
),'missing')!='building'
BEGIN SELECT RAISE(ABORT,'conversation_fragment_requires_building_generation'); END;

CREATE TRIGGER conversation_fragments_update_blocked
BEFORE UPDATE ON conversation_fragments
BEGIN SELECT RAISE(ABORT,'conversation_fragment_immutable'); END;

CREATE TRIGGER conversation_fragments_retired_cleanup
AFTER UPDATE OF status ON projection_generations WHEN NEW.status='retired'
BEGIN
    DELETE FROM conversation_fragments WHERE generation_id=NEW.generation_id
        AND creator_account_id=NEW.creator_account_id;
END;
