-- Detect content changes between validation and atomic activation.
CREATE TABLE generation_content_epoch (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    value INTEGER NOT NULL CHECK(typeof(value)='integer' AND value>=0)
) WITHOUT ROWID;
INSERT INTO generation_content_epoch(singleton,value) VALUES(1,0);

CREATE TRIGGER generation_content_epoch_insert BEFORE INSERT ON generation_content_epoch WHEN EXISTS(SELECT 1 FROM generation_content_epoch) BEGIN SELECT RAISE(ABORT,'generation_epoch_exists'); END;

CREATE TRIGGER generation_content_epoch_delete BEFORE DELETE ON generation_content_epoch BEGIN SELECT RAISE(ABORT,'generation_epoch_delete_blocked'); END;

CREATE TRIGGER generation_content_epoch_update BEFORE UPDATE ON generation_content_epoch WHEN NEW.singleton!=OLD.singleton OR NEW.value!=OLD.value+1 BEGIN SELECT RAISE(ABORT,'generation_epoch_must_advance'); END;

CREATE TRIGGER generation_content_analytics_projections_insert AFTER INSERT ON analytics_projections BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_analytics_projections_update AFTER UPDATE ON analytics_projections BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_analytics_projections_delete AFTER DELETE ON analytics_projections BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_owned_nodes_insert AFTER INSERT ON graph_owned_nodes BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_owned_nodes_update AFTER UPDATE ON graph_owned_nodes BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_owned_nodes_delete AFTER DELETE ON graph_owned_nodes BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_owned_edges_insert AFTER INSERT ON graph_owned_edges BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_owned_edges_update AFTER UPDATE ON graph_owned_edges BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_owned_edges_delete AFTER DELETE ON graph_owned_edges BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_node_content_insert AFTER INSERT ON graph_node_content BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_node_content_update AFTER UPDATE ON graph_node_content BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_node_content_delete AFTER DELETE ON graph_node_content BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_edge_content_insert AFTER INSERT ON graph_edge_content BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_edge_content_update AFTER UPDATE ON graph_edge_content BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_edge_content_delete AFTER DELETE ON graph_edge_content BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_node_identities_insert AFTER INSERT ON graph_node_identities BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_node_identities_update AFTER UPDATE ON graph_node_identities BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_node_identities_delete AFTER DELETE ON graph_node_identities BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_segments_insert AFTER INSERT ON graph_segments BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_segments_update AFTER UPDATE ON graph_segments BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_segments_delete AFTER DELETE ON graph_segments BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_generation_graph_segments_insert AFTER INSERT ON generation_graph_segments BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_generation_graph_segments_update AFTER UPDATE ON generation_graph_segments BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_generation_graph_segments_delete AFTER DELETE ON generation_graph_segments BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_segment_nodes_insert AFTER INSERT ON graph_segment_nodes BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_segment_nodes_update AFTER UPDATE ON graph_segment_nodes BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_segment_nodes_delete AFTER DELETE ON graph_segment_nodes BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_segment_edges_insert AFTER INSERT ON graph_segment_edges BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_segment_edges_update AFTER UPDATE ON graph_segment_edges BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_segment_edges_delete AFTER DELETE ON graph_segment_edges BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_partition_stats_insert AFTER INSERT ON graph_partition_stats BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_partition_stats_update AFTER UPDATE ON graph_partition_stats BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_partition_stats_delete AFTER DELETE ON graph_partition_stats BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_projection_query_metadata_insert AFTER INSERT ON projection_query_metadata BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_projection_query_metadata_update AFTER UPDATE ON projection_query_metadata BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_projection_query_metadata_delete AFTER DELETE ON projection_query_metadata BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_enrichment_reuse_insert AFTER INSERT ON enrichment_reuse BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_enrichment_reuse_update AFTER UPDATE ON enrichment_reuse BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_enrichment_reuse_delete AFTER DELETE ON enrichment_reuse BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_conversation_fragments_insert AFTER INSERT ON conversation_fragments BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_conversation_fragments_update AFTER UPDATE ON conversation_fragments BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_conversation_fragments_delete AFTER DELETE ON conversation_fragments BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_conversation_page_sets_insert AFTER INSERT ON conversation_page_sets BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_conversation_page_sets_update AFTER UPDATE ON conversation_page_sets BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_conversation_page_sets_delete AFTER DELETE ON conversation_page_sets BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_conversation_pages_insert AFTER INSERT ON conversation_pages BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_conversation_pages_update AFTER UPDATE ON conversation_pages BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_conversation_pages_delete AFTER DELETE ON conversation_pages BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_algorithm_metrics_insert AFTER INSERT ON graph_algorithm_metrics BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_algorithm_metrics_update AFTER UPDATE ON graph_algorithm_metrics BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_algorithm_metrics_delete AFTER DELETE ON graph_algorithm_metrics BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_projection_store_identity_insert AFTER INSERT ON projection_store_identity BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_projection_store_identity_update AFTER UPDATE ON projection_store_identity BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_projection_store_identity_delete AFTER DELETE ON projection_store_identity BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;
