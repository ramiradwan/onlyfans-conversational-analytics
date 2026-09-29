-- Batch synchronous page-cache reclamation during predecessor retirement.
CREATE TABLE generation_content_bulk_cleanup (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    page_retirement INTEGER NOT NULL CHECK(page_retirement IN (0,1))
) WITHOUT ROWID;
INSERT INTO generation_content_bulk_cleanup(singleton,page_retirement) VALUES(1,0);

CREATE TRIGGER generation_content_bulk_cleanup_insert
BEFORE INSERT ON generation_content_bulk_cleanup
WHEN EXISTS(SELECT 1 FROM generation_content_bulk_cleanup)
BEGIN SELECT RAISE(ABORT,'generation_content_bulk_cleanup_exists'); END;
CREATE TRIGGER generation_content_bulk_cleanup_delete
BEFORE DELETE ON generation_content_bulk_cleanup
BEGIN SELECT RAISE(ABORT,'generation_content_bulk_cleanup_delete_blocked'); END;
CREATE TRIGGER generation_content_bulk_cleanup_update
BEFORE UPDATE ON generation_content_bulk_cleanup
WHEN NEW.singleton!=OLD.singleton OR NEW.page_retirement=OLD.page_retirement
BEGIN SELECT RAISE(ABORT,'generation_content_bulk_cleanup_scope_invalid'); END;
CREATE TRIGGER generation_content_bulk_cleanup_arm_epoch
AFTER UPDATE ON generation_content_bulk_cleanup
WHEN OLD.page_retirement=0 AND NEW.page_retirement=1
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

DROP TRIGGER conversation_page_content_reclaim;
CREATE TRIGGER conversation_page_content_reclaim
AFTER DELETE ON conversation_page_refs
WHEN (SELECT page_retirement FROM generation_content_bulk_cleanup WHERE singleton=1)=0
BEGIN
    DELETE FROM conversation_page_content
    WHERE creator_account_id=OLD.creator_account_id AND content_id=OLD.content_id
      AND NOT EXISTS(SELECT 1 FROM conversation_page_refs
          WHERE creator_account_id=OLD.creator_account_id AND content_id=OLD.content_id);
END;
DROP TRIGGER generation_content_conversation_page_sets_delete;
CREATE TRIGGER generation_content_conversation_page_sets_delete
AFTER DELETE ON conversation_page_sets
WHEN (SELECT page_retirement FROM generation_content_bulk_cleanup WHERE singleton=1)=0
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

DROP TRIGGER generation_content_conversation_pages_delete;
CREATE TRIGGER generation_content_conversation_pages_delete
AFTER DELETE ON conversation_owned_pages
WHEN (SELECT page_retirement FROM generation_content_bulk_cleanup WHERE singleton=1)=0
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

DROP TRIGGER generation_content_conversation_page_content_delete;
CREATE TRIGGER generation_content_conversation_page_content_delete
AFTER DELETE ON conversation_page_content
WHEN (SELECT page_retirement FROM generation_content_bulk_cleanup WHERE singleton=1)=0
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

DROP TRIGGER generation_content_conversation_page_refs_delete;
CREATE TRIGGER generation_content_conversation_page_refs_delete
AFTER DELETE ON conversation_page_refs
WHEN (SELECT page_retirement FROM generation_content_bulk_cleanup WHERE singleton=1)=0
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
