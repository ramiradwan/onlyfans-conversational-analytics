-- Check shared references before opening content for reclamation.

DROP TRIGGER graph_node_content_reclaim;
CREATE TRIGGER graph_node_content_reclaim
AFTER DELETE ON graph_segment_nodes
WHEN NOT EXISTS (
    SELECT 1 FROM graph_segment_nodes
    WHERE creator_account_id=OLD.creator_account_id
      AND content_id=OLD.content_id
)
BEGIN
    DELETE FROM graph_node_content
    WHERE creator_account_id=OLD.creator_account_id
      AND content_id=OLD.content_id;
END;

DROP TRIGGER graph_edge_content_reclaim;
CREATE TRIGGER graph_edge_content_reclaim
AFTER DELETE ON graph_segment_edges
WHEN NOT EXISTS (
    SELECT 1 FROM graph_segment_edges
    WHERE creator_account_id=OLD.creator_account_id
      AND content_id=OLD.content_id
)
BEGIN
    DELETE FROM graph_edge_content
    WHERE creator_account_id=OLD.creator_account_id
      AND content_id=OLD.content_id;
END;
