CREATE TABLE enrichment_reuse (
    generation_id TEXT NOT NULL,
    creator_account_id TEXT NOT NULL,
    cache_key TEXT NOT NULL CHECK (length(cache_key)=71 AND substr(cache_key,1,7)='sha256:'),
    expires_at TEXT NOT NULL,
    document_json TEXT NOT NULL CHECK (
        length(CAST(document_json AS BLOB))<=65536 AND json_valid(document_json)
        AND json_extract(document_json,'$.key.account_ref')=creator_account_id
    ),
    document_digest TEXT NOT NULL CHECK (length(document_digest)=64),
    PRIMARY KEY (generation_id,creator_account_id,cache_key),
    FOREIGN KEY (generation_id,creator_account_id)
        REFERENCES projection_generations(generation_id,creator_account_id) ON DELETE CASCADE
) WITHOUT ROWID;

CREATE INDEX enrichment_reuse_expiry ON enrichment_reuse(expires_at);

CREATE TRIGGER enrichment_reuse_building_insert
BEFORE INSERT ON enrichment_reuse WHEN COALESCE((
    SELECT status FROM projection_generations WHERE generation_id=NEW.generation_id
        AND creator_account_id=NEW.creator_account_id
),'missing')!='building'
BEGIN SELECT RAISE(ABORT,'enrichment_reuse_requires_building_generation'); END;

CREATE TRIGGER enrichment_reuse_update_blocked
BEFORE UPDATE ON enrichment_reuse
BEGIN SELECT RAISE(ABORT,'enrichment_reuse_immutable'); END;

CREATE TRIGGER enrichment_reuse_retired_cleanup
AFTER UPDATE OF status ON projection_generations WHEN NEW.status='retired'
BEGIN
    DELETE FROM enrichment_reuse WHERE generation_id=NEW.generation_id
        AND creator_account_id=NEW.creator_account_id;
END;
