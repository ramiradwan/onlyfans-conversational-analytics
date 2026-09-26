-- Share immutable compressed pages across generation-owned cache manifests.
ALTER TABLE conversation_pages RENAME TO conversation_owned_pages;

CREATE TABLE conversation_page_content (
    creator_account_id TEXT NOT NULL CHECK(length(creator_account_id)=67 AND substr(creator_account_id,1,3)='a1:' AND substr(creator_account_id,4) NOT GLOB '*[^0-9a-f]*'),
    content_id TEXT NOT NULL CHECK(length(content_id)=64 AND content_id NOT GLOB '*[^0-9a-f]*'),
    kind TEXT NOT NULL CHECK(kind IN ('message','node','edge','analyzer')),
    data BLOB NOT NULL CHECK(typeof(data)='blob' AND length(data)>0 AND length(data)<=262144),
    PRIMARY KEY(creator_account_id,content_id)
) WITHOUT ROWID;

CREATE TABLE conversation_page_refs (
    generation_id TEXT NOT NULL,
    creator_account_id TEXT NOT NULL,
    conversation_ref TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal>=0 AND ordinal<4096),
    content_id TEXT NOT NULL,
    PRIMARY KEY(generation_id,creator_account_id,conversation_ref,ordinal),
    FOREIGN KEY(generation_id,creator_account_id,conversation_ref)
        REFERENCES conversation_page_sets(generation_id,creator_account_id,conversation_ref) ON DELETE CASCADE,
    FOREIGN KEY(creator_account_id,content_id)
        REFERENCES conversation_page_content(creator_account_id,content_id)
) WITHOUT ROWID;
CREATE INDEX conversation_page_refs_by_content ON conversation_page_refs(creator_account_id,content_id);

CREATE TRIGGER conversation_page_content_immutable BEFORE UPDATE ON conversation_page_content
BEGIN SELECT RAISE(ABORT,'conversation_page_content_immutable'); END;
CREATE TRIGGER conversation_page_content_referenced BEFORE DELETE ON conversation_page_content
WHEN EXISTS(SELECT 1 FROM conversation_page_refs WHERE creator_account_id=OLD.creator_account_id AND content_id=OLD.content_id)
BEGIN SELECT RAISE(ABORT,'conversation_page_content_referenced'); END;
CREATE TRIGGER conversation_page_content_building BEFORE INSERT ON conversation_page_content
WHEN NOT EXISTS(SELECT 1 FROM conversation_page_sets s JOIN projection_generations g USING(generation_id,creator_account_id)
    WHERE s.creator_account_id=NEW.creator_account_id AND g.status='building')
BEGIN SELECT RAISE(ABORT,'conversation_page_requires_building_generation'); END;

CREATE TRIGGER conversation_page_refs_building BEFORE INSERT ON conversation_page_refs
WHEN COALESCE((SELECT status FROM projection_generations WHERE generation_id=NEW.generation_id AND creator_account_id=NEW.creator_account_id),'')!='building'
    OR EXISTS(SELECT 1 FROM conversation_owned_pages WHERE generation_id=NEW.generation_id AND creator_account_id=NEW.creator_account_id AND conversation_ref=NEW.conversation_ref)
BEGIN SELECT RAISE(ABORT,'conversation_page_reference_requires_building_generation'); END;
CREATE TRIGGER conversation_page_refs_immutable BEFORE UPDATE ON conversation_page_refs
BEGIN SELECT RAISE(ABORT,'conversation_page_reference_immutable'); END;
CREATE TRIGGER conversation_owned_pages_layout BEFORE INSERT ON conversation_owned_pages
WHEN EXISTS(SELECT 1 FROM conversation_page_refs WHERE generation_id=NEW.generation_id AND creator_account_id=NEW.creator_account_id AND conversation_ref=NEW.conversation_ref)
BEGIN SELECT RAISE(ABORT,'conversation_page_layout_mixed'); END;
CREATE TRIGGER conversation_page_content_reclaim AFTER DELETE ON conversation_page_refs
BEGIN
    DELETE FROM conversation_page_content WHERE creator_account_id=OLD.creator_account_id AND content_id=OLD.content_id
        AND NOT EXISTS(SELECT 1 FROM conversation_page_refs WHERE creator_account_id=OLD.creator_account_id AND content_id=OLD.content_id);
END;

CREATE VIEW conversation_pages AS
SELECT generation_id,creator_account_id,conversation_ref,ordinal,kind,data FROM conversation_owned_pages
UNION ALL
SELECT r.generation_id,r.creator_account_id,r.conversation_ref,r.ordinal,c.kind,c.data
FROM conversation_page_refs r JOIN conversation_page_content c USING(creator_account_id,content_id);

CREATE TRIGGER conversation_page_view_insert INSTEAD OF INSERT ON conversation_pages
BEGIN
    INSERT INTO conversation_owned_pages(generation_id,creator_account_id,conversation_ref,ordinal,kind,data)
        VALUES(NEW.generation_id,NEW.creator_account_id,NEW.conversation_ref,NEW.ordinal,NEW.kind,NEW.data);
END;
CREATE TRIGGER conversation_page_view_update INSTEAD OF UPDATE ON conversation_pages
BEGIN SELECT RAISE(ABORT,'conversation_page_immutable'); END;
CREATE TRIGGER conversation_page_view_delete INSTEAD OF DELETE ON conversation_pages
BEGIN
    DELETE FROM conversation_owned_pages WHERE generation_id=OLD.generation_id AND creator_account_id=OLD.creator_account_id AND conversation_ref=OLD.conversation_ref AND ordinal=OLD.ordinal;
    DELETE FROM conversation_page_refs WHERE generation_id=OLD.generation_id AND creator_account_id=OLD.creator_account_id AND conversation_ref=OLD.conversation_ref AND ordinal=OLD.ordinal;
END;

CREATE TRIGGER generation_content_conversation_page_content_insert AFTER INSERT ON conversation_page_content BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_conversation_page_content_update AFTER UPDATE ON conversation_page_content BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_conversation_page_content_delete AFTER DELETE ON conversation_page_content BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_conversation_page_refs_insert AFTER INSERT ON conversation_page_refs BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_conversation_page_refs_update AFTER UPDATE ON conversation_page_refs BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
CREATE TRIGGER generation_content_conversation_page_refs_delete AFTER DELETE ON conversation_page_refs BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

-- REPLACE must not bypass update guards when recursive triggers are disabled.
CREATE TRIGGER conversation_page_content_replace_blocked
BEFORE INSERT ON conversation_page_content
WHEN EXISTS(SELECT 1 FROM conversation_page_content
    WHERE creator_account_id=NEW.creator_account_id AND content_id=NEW.content_id)
BEGIN SELECT RAISE(ABORT,'conversation_page_content_immutable'); END;

CREATE TRIGGER conversation_page_refs_replace_blocked
BEFORE INSERT ON conversation_page_refs
WHEN EXISTS(SELECT 1 FROM conversation_page_refs
    WHERE generation_id=NEW.generation_id AND creator_account_id=NEW.creator_account_id
      AND conversation_ref=NEW.conversation_ref AND ordinal=NEW.ordinal)
BEGIN SELECT RAISE(ABORT,'conversation_page_reference_immutable'); END;
