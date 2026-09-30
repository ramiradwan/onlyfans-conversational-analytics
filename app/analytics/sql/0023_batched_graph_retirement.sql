-- Batch synchronous graph-unit reclamation during predecessor retirement.
ALTER TABLE generation_content_bulk_cleanup
ADD COLUMN graph_retirement INTEGER NOT NULL DEFAULT 0
CHECK(graph_retirement IN (0,1));

DROP TRIGGER generation_content_bulk_cleanup_update;
CREATE TRIGGER generation_content_bulk_cleanup_update
BEFORE UPDATE ON generation_content_bulk_cleanup
WHEN NEW.singleton!=OLD.singleton
  OR (NEW.page_retirement=OLD.page_retirement
      AND NEW.graph_retirement=OLD.graph_retirement)
BEGIN SELECT RAISE(ABORT,'generation_content_bulk_cleanup_scope_invalid'); END;

CREATE TRIGGER generation_content_bulk_cleanup_graph_arm_epoch
AFTER UPDATE ON generation_content_bulk_cleanup
WHEN OLD.graph_retirement=0 AND NEW.graph_retirement=1
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

DROP TRIGGER conversation_graph_refs_delete;
CREATE TRIGGER conversation_graph_refs_delete
BEFORE DELETE ON conversation_graph_refs
WHEN COALESCE((
    SELECT status FROM projection_generations
    WHERE generation_id=OLD.generation_id
      AND creator_account_id=OLD.creator_account_id
),'') NOT IN ('','building','retired')
AND NOT (
    COALESCE((
        SELECT status FROM projection_generations
        WHERE generation_id=OLD.generation_id
          AND creator_account_id=OLD.creator_account_id
    ),'')='active'
    AND (SELECT graph_retirement FROM generation_content_bulk_cleanup WHERE singleton=1)=1
)
BEGIN SELECT RAISE(ABORT,'conversation_graph_reference_delete_blocked'); END;

DROP TRIGGER conversation_graph_unit_reclaim;
CREATE TRIGGER conversation_graph_unit_reclaim
AFTER DELETE ON conversation_graph_refs
WHEN (SELECT graph_retirement FROM generation_content_bulk_cleanup WHERE singleton=1)=0
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
