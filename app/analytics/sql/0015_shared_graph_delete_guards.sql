-- Make referenced shared graph content deletion-resistant without relying on foreign keys.

CREATE TRIGGER graph_node_content_referenced
BEFORE DELETE ON graph_node_content
WHEN EXISTS (
    SELECT 1 FROM graph_segment_nodes
    WHERE creator_account_id=OLD.creator_account_id
      AND content_id=OLD.content_id
)
BEGIN SELECT RAISE(ABORT,'graph_content_referenced'); END;

CREATE TRIGGER graph_edge_content_referenced
BEFORE DELETE ON graph_edge_content
WHEN EXISTS (
    SELECT 1 FROM graph_segment_edges
    WHERE creator_account_id=OLD.creator_account_id
      AND content_id=OLD.content_id
)
BEGIN SELECT RAISE(ABORT,'graph_content_referenced'); END;

CREATE TRIGGER graph_segments_referenced
BEFORE DELETE ON graph_segments
WHEN EXISTS (
    SELECT 1 FROM generation_graph_segments
    WHERE creator_account_id=OLD.creator_account_id
      AND segment_id=OLD.segment_id
)
BEGIN SELECT RAISE(ABORT,'graph_segment_referenced'); END;
