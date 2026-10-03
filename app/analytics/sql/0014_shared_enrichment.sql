-- Keep one immutable analyzer document for each account and content hash.
ALTER TABLE enrichment_reuse RENAME TO enrichment_owned_records;
DROP TRIGGER enrichment_reuse_update_blocked;
DROP TRIGGER enrichment_reuse_retired_cleanup;

CREATE TABLE enrichment_content (
    creator_account_id TEXT NOT NULL CHECK(length(creator_account_id)=67 AND substr(creator_account_id,1,3)='a1:' AND substr(creator_account_id,4) NOT GLOB '*[^0-9a-f]*'),
    content_id TEXT NOT NULL CHECK(length(content_id)=64 AND content_id NOT GLOB '*[^0-9a-f]*'),
    document_json TEXT NOT NULL CHECK(length(CAST(document_json AS BLOB))<=65536 AND json_valid(document_json)
        AND json_extract(document_json,'$.key.account_ref') IS creator_account_id),
    PRIMARY KEY(creator_account_id,content_id)
) WITHOUT ROWID;

CREATE TABLE enrichment_refs (
    generation_id TEXT NOT NULL,
    creator_account_id TEXT NOT NULL,
    cache_key TEXT NOT NULL CHECK(length(cache_key)=71 AND substr(cache_key,1,7)='sha256:'),
    expires_at TEXT NOT NULL,
    content_id TEXT NOT NULL,
    PRIMARY KEY(generation_id,creator_account_id,cache_key),
    FOREIGN KEY(generation_id,creator_account_id)
        REFERENCES projection_generations(generation_id,creator_account_id) ON DELETE CASCADE,
    FOREIGN KEY(creator_account_id,content_id) REFERENCES enrichment_content(creator_account_id,content_id)
) WITHOUT ROWID;
CREATE INDEX enrichment_refs_by_content ON enrichment_refs(creator_account_id,content_id);
CREATE INDEX enrichment_refs_expiry ON enrichment_refs(expires_at);

CREATE TRIGGER enrichment_content_immutable BEFORE UPDATE ON enrichment_content
BEGIN SELECT RAISE(ABORT,'enrichment_reuse_immutable'); END;
CREATE TRIGGER enrichment_content_referenced BEFORE DELETE ON enrichment_content
WHEN EXISTS(SELECT 1 FROM enrichment_refs WHERE creator_account_id=OLD.creator_account_id AND content_id=OLD.content_id)
BEGIN SELECT RAISE(ABORT,'enrichment_content_referenced'); END;
CREATE TRIGGER enrichment_content_building BEFORE INSERT ON enrichment_content
WHEN NOT EXISTS(SELECT 1 FROM projection_generations WHERE creator_account_id=NEW.creator_account_id AND status='building')
BEGIN SELECT RAISE(ABORT,'enrichment_reuse_requires_building_generation'); END;
CREATE TRIGGER enrichment_content_replace_blocked BEFORE INSERT ON enrichment_content
WHEN EXISTS(SELECT 1 FROM enrichment_content WHERE creator_account_id=NEW.creator_account_id AND content_id=NEW.content_id)
BEGIN SELECT RAISE(ABORT,'enrichment_reuse_immutable'); END;

CREATE TRIGGER enrichment_refs_building BEFORE INSERT ON enrichment_refs
WHEN COALESCE((SELECT status FROM projection_generations WHERE generation_id=NEW.generation_id AND creator_account_id=NEW.creator_account_id),'')!='building'
BEGIN SELECT RAISE(ABORT,'enrichment_reuse_requires_building_generation'); END;
CREATE TRIGGER enrichment_refs_immutable BEFORE UPDATE ON enrichment_refs
BEGIN SELECT RAISE(ABORT,'enrichment_reuse_immutable'); END;
CREATE TRIGGER enrichment_refs_replace_blocked BEFORE INSERT ON enrichment_refs
WHEN EXISTS(SELECT 1 FROM enrichment_refs WHERE generation_id=NEW.generation_id AND creator_account_id=NEW.creator_account_id AND cache_key=NEW.cache_key)
    OR EXISTS(SELECT 1 FROM enrichment_owned_records WHERE generation_id=NEW.generation_id AND creator_account_id=NEW.creator_account_id AND cache_key=NEW.cache_key)
BEGIN SELECT RAISE(ABORT,'enrichment_reuse_immutable'); END;
CREATE TRIGGER enrichment_owned_immutable BEFORE UPDATE ON enrichment_owned_records
BEGIN SELECT RAISE(ABORT,'enrichment_reuse_immutable'); END;
CREATE TRIGGER enrichment_owned_replace_blocked BEFORE INSERT ON enrichment_owned_records
WHEN EXISTS(SELECT 1 FROM enrichment_owned_records WHERE generation_id=NEW.generation_id AND creator_account_id=NEW.creator_account_id AND cache_key=NEW.cache_key)
    OR EXISTS(SELECT 1 FROM enrichment_refs WHERE generation_id=NEW.generation_id AND creator_account_id=NEW.creator_account_id AND cache_key=NEW.cache_key)
BEGIN SELECT RAISE(ABORT,'enrichment_reuse_immutable'); END;
CREATE TRIGGER enrichment_content_reclaim AFTER DELETE ON enrichment_refs
BEGIN
    DELETE FROM enrichment_content WHERE creator_account_id=OLD.creator_account_id AND content_id=OLD.content_id
        AND NOT EXISTS(SELECT 1 FROM enrichment_refs WHERE creator_account_id=OLD.creator_account_id AND content_id=OLD.content_id);
END;

CREATE VIEW enrichment_reuse AS
SELECT generation_id,creator_account_id,cache_key,expires_at,document_json,document_digest FROM enrichment_owned_records
UNION ALL
SELECT r.generation_id,r.creator_account_id,r.cache_key,r.expires_at,c.document_json,c.content_id AS document_digest
FROM enrichment_refs r JOIN enrichment_content c USING(creator_account_id,content_id);
CREATE TRIGGER enrichment_reuse_view_insert INSTEAD OF INSERT ON enrichment_reuse
BEGIN
    INSERT INTO enrichment_owned_records(generation_id,creator_account_id,cache_key,expires_at,document_json,document_digest)
        VALUES(NEW.generation_id,NEW.creator_account_id,NEW.cache_key,NEW.expires_at,NEW.document_json,NEW.document_digest);
END;
CREATE TRIGGER enrichment_reuse_update_blocked INSTEAD OF UPDATE ON enrichment_reuse
BEGIN SELECT RAISE(ABORT,'enrichment_reuse_immutable'); END;
CREATE TRIGGER enrichment_reuse_view_delete INSTEAD OF DELETE ON enrichment_reuse
BEGIN
    DELETE FROM enrichment_owned_records WHERE generation_id=OLD.generation_id AND creator_account_id=OLD.creator_account_id AND cache_key=OLD.cache_key;
    DELETE FROM enrichment_refs WHERE generation_id=OLD.generation_id AND creator_account_id=OLD.creator_account_id AND cache_key=OLD.cache_key;
END;
CREATE TRIGGER enrichment_reuse_retired_cleanup
AFTER UPDATE OF status ON projection_generations WHEN NEW.status='retired'
BEGIN
    DELETE FROM enrichment_owned_records WHERE generation_id=NEW.generation_id AND creator_account_id=NEW.creator_account_id;
    DELETE FROM enrichment_refs WHERE generation_id=NEW.generation_id AND creator_account_id=NEW.creator_account_id;
END;

CREATE TRIGGER generation_content_enrichment_content_insert AFTER INSERT ON enrichment_content BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_enrichment_content_update AFTER UPDATE ON enrichment_content BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_enrichment_content_delete AFTER DELETE ON enrichment_content BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_enrichment_refs_insert AFTER INSERT ON enrichment_refs BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_enrichment_refs_update AFTER UPDATE ON enrichment_refs BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_enrichment_refs_delete AFTER DELETE ON enrichment_refs BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
