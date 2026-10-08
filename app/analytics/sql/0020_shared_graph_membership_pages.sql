-- Share immutable membership pages without changing logical graph records.

CREATE TABLE graph_membership_pages (
    creator_account_id TEXT NOT NULL,
    page_id TEXT NOT NULL CHECK(length(page_id) IN (36,38)),
    kind TEXT NOT NULL CHECK(kind IN ('node','edge')),
    bucket TEXT NOT NULL CHECK(length(bucket)=3 AND bucket NOT GLOB '*[^0-9a-f]*'),
    sealed INTEGER NOT NULL DEFAULT 0 CHECK(sealed IN (0,1)),
    PRIMARY KEY(creator_account_id,page_id),
    UNIQUE(creator_account_id,page_id,kind,bucket)
) WITHOUT ROWID;

CREATE TABLE graph_segment_membership_pages (
    creator_account_id TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    bucket TEXT NOT NULL,
    page_id TEXT NOT NULL,
    PRIMARY KEY(creator_account_id,segment_id,bucket),
    FOREIGN KEY(creator_account_id,segment_id)
        REFERENCES graph_segments(creator_account_id,segment_id) ON DELETE CASCADE,
    FOREIGN KEY(creator_account_id,page_id,kind,bucket)
        REFERENCES graph_membership_pages(creator_account_id,page_id,kind,bucket)
) WITHOUT ROWID;
CREATE INDEX graph_membership_page_references
ON graph_segment_membership_pages(creator_account_id,page_id);


CREATE TABLE graph_membership_nodes (
    creator_account_id TEXT NOT NULL,
    page_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    content_id TEXT NOT NULL,
    PRIMARY KEY(creator_account_id,page_id,node_id),
    FOREIGN KEY(creator_account_id,page_id)
        REFERENCES graph_membership_pages(creator_account_id,page_id) ON DELETE CASCADE,
    FOREIGN KEY(creator_account_id,content_id,node_id)
        REFERENCES graph_node_content(creator_account_id,content_id,node_id)
) WITHOUT ROWID;
CREATE INDEX graph_membership_nodes_by_content
ON graph_membership_nodes(creator_account_id,content_id);

INSERT INTO graph_membership_pages(creator_account_id,page_id,kind,bucket,sealed)
SELECT r.creator_account_id,r.segment_id||':'||substr(r.node_id,6,1),
       'node',substr(r.node_id,4,3),s.sealed
FROM graph_segment_nodes r JOIN graph_segments s USING(creator_account_id,segment_id)
GROUP BY r.creator_account_id,r.segment_id,substr(r.node_id,4,3);
INSERT INTO graph_segment_membership_pages(creator_account_id,segment_id,kind,bucket,page_id)
SELECT creator_account_id,segment_id,'node',substr(node_id,4,3),
       segment_id||':'||substr(node_id,6,1)
FROM graph_segment_nodes
GROUP BY creator_account_id,segment_id,substr(node_id,4,3);
INSERT INTO graph_membership_nodes(creator_account_id,page_id,node_id,content_id)
SELECT creator_account_id,segment_id||':'||substr(node_id,6,1),node_id,content_id
FROM graph_segment_nodes;
DROP TABLE graph_segment_nodes;
CREATE VIEW graph_segment_nodes AS
SELECT m.creator_account_id,m.segment_id,r.node_id,r.content_id
FROM graph_segment_membership_pages m
CROSS JOIN graph_membership_nodes r USING(creator_account_id,page_id)
WHERE m.kind='node';


CREATE TABLE graph_membership_edges (
    creator_account_id TEXT NOT NULL,
    page_id TEXT NOT NULL,
    edge_id TEXT NOT NULL,
    content_id TEXT NOT NULL,
    PRIMARY KEY(creator_account_id,page_id,edge_id),
    FOREIGN KEY(creator_account_id,page_id)
        REFERENCES graph_membership_pages(creator_account_id,page_id) ON DELETE CASCADE,
    FOREIGN KEY(creator_account_id,content_id,edge_id)
        REFERENCES graph_edge_content(creator_account_id,content_id,edge_id)
) WITHOUT ROWID;
CREATE INDEX graph_membership_edges_by_content
ON graph_membership_edges(creator_account_id,content_id);

INSERT INTO graph_membership_pages(creator_account_id,page_id,kind,bucket,sealed)
SELECT r.creator_account_id,r.segment_id||':'||substr(r.edge_id,6,1),
       'edge',substr(r.edge_id,4,3),s.sealed
FROM graph_segment_edges r JOIN graph_segments s USING(creator_account_id,segment_id)
GROUP BY r.creator_account_id,r.segment_id,substr(r.edge_id,4,3);
INSERT INTO graph_segment_membership_pages(creator_account_id,segment_id,kind,bucket,page_id)
SELECT creator_account_id,segment_id,'edge',substr(edge_id,4,3),
       segment_id||':'||substr(edge_id,6,1)
FROM graph_segment_edges
GROUP BY creator_account_id,segment_id,substr(edge_id,4,3);
INSERT INTO graph_membership_edges(creator_account_id,page_id,edge_id,content_id)
SELECT creator_account_id,segment_id||':'||substr(edge_id,6,1),edge_id,content_id
FROM graph_segment_edges;
DROP TABLE graph_segment_edges;
CREATE VIEW graph_segment_edges AS
SELECT m.creator_account_id,m.segment_id,r.edge_id,r.content_id
FROM graph_segment_membership_pages m
CROSS JOIN graph_membership_edges r USING(creator_account_id,page_id)
WHERE m.kind='edge';


CREATE TRIGGER graph_membership_pages_insert BEFORE INSERT ON graph_membership_pages
WHEN NEW.sealed!=0
 OR EXISTS(SELECT 1 FROM graph_membership_pages WHERE creator_account_id=NEW.creator_account_id AND page_id=NEW.page_id)
 OR NOT EXISTS(
    SELECT 1 FROM graph_segments s CROSS JOIN generation_graph_segments m USING(creator_account_id,segment_id)
    CROSS JOIN projection_generations g USING(creator_account_id,generation_id)
    WHERE s.creator_account_id=NEW.creator_account_id AND s.kind=NEW.kind
      AND s.bucket=substr(NEW.bucket,1,2) AND s.sealed=0 AND g.status='building')
BEGIN SELECT RAISE(ABORT,'graph_membership_page_not_building'); END;
CREATE TRIGGER graph_membership_pages_immutable BEFORE UPDATE ON graph_membership_pages
WHEN NEW.creator_account_id!=OLD.creator_account_id OR NEW.page_id!=OLD.page_id
 OR NEW.kind!=OLD.kind OR NEW.bucket!=OLD.bucket OR OLD.sealed!=0 OR NEW.sealed!=1
 OR NOT EXISTS(
    SELECT 1 FROM graph_segment_membership_pages p
    CROSS JOIN generation_graph_segments m USING(creator_account_id,segment_id)
    CROSS JOIN projection_generations g USING(creator_account_id,generation_id)
    WHERE p.creator_account_id=OLD.creator_account_id AND p.page_id=OLD.page_id AND g.status='building')
BEGIN SELECT RAISE(ABORT,'graph_membership_page_immutable'); END;
CREATE TRIGGER graph_membership_pages_referenced BEFORE DELETE ON graph_membership_pages
WHEN EXISTS(SELECT 1 FROM graph_segment_membership_pages
            WHERE creator_account_id=OLD.creator_account_id AND page_id=OLD.page_id)
BEGIN SELECT RAISE(ABORT,'graph_membership_page_referenced'); END;

CREATE TRIGGER graph_membership_selection_insert BEFORE INSERT ON graph_segment_membership_pages
WHEN EXISTS(SELECT 1 FROM graph_segment_membership_pages
            WHERE creator_account_id=NEW.creator_account_id AND segment_id=NEW.segment_id AND bucket=NEW.bucket)
 OR NOT EXISTS(
    SELECT 1 FROM graph_segments s CROSS JOIN generation_graph_segments m USING(creator_account_id,segment_id)
    CROSS JOIN projection_generations g USING(creator_account_id,generation_id)
    WHERE s.creator_account_id=NEW.creator_account_id AND s.segment_id=NEW.segment_id
      AND s.kind=NEW.kind AND s.bucket=substr(NEW.bucket,1,2) AND s.sealed=0 AND g.status='building')
 OR NOT EXISTS(
    SELECT 1 FROM graph_membership_pages p
    WHERE p.creator_account_id=NEW.creator_account_id AND p.page_id=NEW.page_id
      AND p.kind=NEW.kind AND p.bucket=NEW.bucket
      AND (p.sealed=1 OR NOT EXISTS(
          SELECT 1 FROM graph_segment_membership_pages r
          WHERE r.creator_account_id=p.creator_account_id AND r.page_id=p.page_id)))
BEGIN SELECT RAISE(ABORT,'graph_membership_selection_invalid'); END;
CREATE TRIGGER graph_membership_selection_immutable BEFORE UPDATE ON graph_segment_membership_pages
BEGIN SELECT RAISE(ABORT,'graph_segment_immutable'); END;
CREATE TRIGGER graph_membership_selection_delete BEFORE DELETE ON graph_segment_membership_pages
WHEN EXISTS(SELECT 1 FROM graph_segments
            WHERE creator_account_id=OLD.creator_account_id AND segment_id=OLD.segment_id AND sealed=1)
BEGIN SELECT RAISE(ABORT,'graph_segment_immutable'); END;
CREATE TRIGGER graph_membership_page_reclaim AFTER DELETE ON graph_segment_membership_pages
WHEN NOT EXISTS(SELECT 1 FROM graph_segment_membership_pages
                WHERE creator_account_id=OLD.creator_account_id AND page_id=OLD.page_id)
BEGIN DELETE FROM graph_membership_pages WHERE creator_account_id=OLD.creator_account_id AND page_id=OLD.page_id; END;
CREATE TRIGGER graph_membership_page_seal AFTER UPDATE OF sealed ON graph_segments
WHEN NEW.sealed=1
BEGIN
 UPDATE graph_membership_pages SET sealed=1
 WHERE creator_account_id=NEW.creator_account_id AND sealed=0 AND page_id IN (
     SELECT page_id FROM graph_segment_membership_pages
     WHERE creator_account_id=NEW.creator_account_id AND segment_id=NEW.segment_id);
END;
CREATE TRIGGER graph_membership_page_cleanup AFTER DELETE ON graph_membership_pages
BEGIN
 DELETE FROM graph_membership_edges WHERE creator_account_id=OLD.creator_account_id AND page_id=OLD.page_id;
 DELETE FROM graph_membership_nodes WHERE creator_account_id=OLD.creator_account_id AND page_id=OLD.page_id;
END;
CREATE TRIGGER graph_segment_membership_cleanup AFTER DELETE ON graph_segments
BEGIN
 DELETE FROM graph_segment_membership_pages WHERE creator_account_id=OLD.creator_account_id AND segment_id=OLD.segment_id;
END;


CREATE TRIGGER graph_membership_nodes_insert BEFORE INSERT ON graph_membership_nodes
WHEN NOT EXISTS(
    SELECT 1 FROM graph_membership_pages p
    CROSS JOIN graph_segment_membership_pages r USING(creator_account_id,page_id)
    CROSS JOIN graph_segments s USING(creator_account_id,segment_id)
    CROSS JOIN generation_graph_segments m USING(creator_account_id,segment_id)
    CROSS JOIN projection_generations g USING(creator_account_id,generation_id)
    WHERE p.creator_account_id=NEW.creator_account_id AND p.page_id=NEW.page_id
      AND p.kind='node' AND p.bucket=substr(NEW.node_id,4,3)
      AND p.sealed=0 AND s.sealed=0 AND g.status='building')
 OR EXISTS(SELECT 1 FROM graph_membership_nodes
           WHERE creator_account_id=NEW.creator_account_id AND page_id=NEW.page_id AND node_id=NEW.node_id)
BEGIN SELECT RAISE(ABORT,'graph_membership_page_not_building'); END;
CREATE TRIGGER graph_membership_nodes_immutable BEFORE UPDATE ON graph_membership_nodes
BEGIN SELECT RAISE(ABORT,'graph_membership_page_immutable'); END;
CREATE TRIGGER graph_membership_nodes_delete BEFORE DELETE ON graph_membership_nodes
WHEN EXISTS(SELECT 1 FROM graph_membership_pages
            WHERE creator_account_id=OLD.creator_account_id AND page_id=OLD.page_id AND sealed=1)
BEGIN SELECT RAISE(ABORT,'graph_membership_page_immutable'); END;

CREATE TRIGGER graph_segment_nodes_insert INSTEAD OF INSERT ON graph_segment_nodes
BEGIN SELECT RAISE(ABORT,'graph_membership_page_required'); END;
CREATE TRIGGER graph_segment_nodes_immutable INSTEAD OF UPDATE ON graph_segment_nodes
BEGIN SELECT RAISE(ABORT,'graph_segment_immutable'); END;
CREATE TRIGGER graph_segment_nodes_delete INSTEAD OF DELETE ON graph_segment_nodes
BEGIN SELECT RAISE(ABORT,'graph_manifest_delete_required'); END;

DROP TRIGGER graph_node_content_referenced;
CREATE TRIGGER graph_node_content_referenced BEFORE DELETE ON graph_node_content
WHEN EXISTS(SELECT 1 FROM graph_membership_nodes
            WHERE creator_account_id=OLD.creator_account_id AND content_id=OLD.content_id)
BEGIN SELECT RAISE(ABORT,'graph_content_referenced'); END;
CREATE TRIGGER graph_node_content_reclaim AFTER DELETE ON graph_membership_nodes
WHEN NOT EXISTS(SELECT 1 FROM graph_membership_nodes
                WHERE creator_account_id=OLD.creator_account_id AND content_id=OLD.content_id)
BEGIN DELETE FROM graph_node_content WHERE creator_account_id=OLD.creator_account_id AND content_id=OLD.content_id; END;

CREATE TRIGGER generation_content_graph_segment_nodes_insert
AFTER INSERT ON graph_membership_nodes
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_segment_nodes_update
AFTER UPDATE ON graph_membership_nodes
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_segment_nodes_delete
AFTER DELETE ON graph_membership_nodes
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;


CREATE TRIGGER graph_membership_edges_insert BEFORE INSERT ON graph_membership_edges
WHEN NOT EXISTS(
    SELECT 1 FROM graph_membership_pages p
    CROSS JOIN graph_segment_membership_pages r USING(creator_account_id,page_id)
    CROSS JOIN graph_segments s USING(creator_account_id,segment_id)
    CROSS JOIN generation_graph_segments m USING(creator_account_id,segment_id)
    CROSS JOIN projection_generations g USING(creator_account_id,generation_id)
    WHERE p.creator_account_id=NEW.creator_account_id AND p.page_id=NEW.page_id
      AND p.kind='edge' AND p.bucket=substr(NEW.edge_id,4,3)
      AND p.sealed=0 AND s.sealed=0 AND g.status='building')
 OR EXISTS(SELECT 1 FROM graph_membership_edges
           WHERE creator_account_id=NEW.creator_account_id AND page_id=NEW.page_id AND edge_id=NEW.edge_id)
BEGIN SELECT RAISE(ABORT,'graph_membership_page_not_building'); END;
CREATE TRIGGER graph_membership_edges_immutable BEFORE UPDATE ON graph_membership_edges
BEGIN SELECT RAISE(ABORT,'graph_membership_page_immutable'); END;
CREATE TRIGGER graph_membership_edges_delete BEFORE DELETE ON graph_membership_edges
WHEN EXISTS(SELECT 1 FROM graph_membership_pages
            WHERE creator_account_id=OLD.creator_account_id AND page_id=OLD.page_id AND sealed=1)
BEGIN SELECT RAISE(ABORT,'graph_membership_page_immutable'); END;

CREATE TRIGGER graph_segment_edges_insert INSTEAD OF INSERT ON graph_segment_edges
BEGIN SELECT RAISE(ABORT,'graph_membership_page_required'); END;
CREATE TRIGGER graph_segment_edges_immutable INSTEAD OF UPDATE ON graph_segment_edges
BEGIN SELECT RAISE(ABORT,'graph_segment_immutable'); END;
CREATE TRIGGER graph_segment_edges_delete INSTEAD OF DELETE ON graph_segment_edges
BEGIN SELECT RAISE(ABORT,'graph_manifest_delete_required'); END;

DROP TRIGGER graph_edge_content_referenced;
CREATE TRIGGER graph_edge_content_referenced BEFORE DELETE ON graph_edge_content
WHEN EXISTS(SELECT 1 FROM graph_membership_edges
            WHERE creator_account_id=OLD.creator_account_id AND content_id=OLD.content_id)
BEGIN SELECT RAISE(ABORT,'graph_content_referenced'); END;
CREATE TRIGGER graph_edge_content_reclaim AFTER DELETE ON graph_membership_edges
WHEN NOT EXISTS(SELECT 1 FROM graph_membership_edges
                WHERE creator_account_id=OLD.creator_account_id AND content_id=OLD.content_id)
BEGIN DELETE FROM graph_edge_content WHERE creator_account_id=OLD.creator_account_id AND content_id=OLD.content_id; END;

CREATE TRIGGER generation_content_graph_segment_edges_insert
AFTER INSERT ON graph_membership_edges
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_segment_edges_update
AFTER UPDATE ON graph_membership_edges
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_segment_edges_delete
AFTER DELETE ON graph_membership_edges
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_membership_pages_insert
AFTER INSERT ON graph_membership_pages
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_membership_pages_update
AFTER UPDATE ON graph_membership_pages
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_membership_pages_delete
AFTER DELETE ON graph_membership_pages
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_segment_membership_pages_insert
AFTER INSERT ON graph_segment_membership_pages
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_segment_membership_pages_update
AFTER UPDATE ON graph_segment_membership_pages
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

CREATE TRIGGER generation_content_graph_segment_membership_pages_delete
AFTER DELETE ON graph_segment_membership_pages
BEGIN UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1; END;

-- Route point reads through their selected identity bucket.

DROP VIEW graph_nodes;
CREATE VIEW graph_nodes AS SELECT generation_id,creator_account_id,node_id,kind,occurred_at,properties_json FROM graph_owned_nodes UNION ALL SELECT m.generation_id,m.creator_account_id,c.node_id,c.kind,c.occurred_at,c.properties_json FROM generation_graph_segments m JOIN graph_segment_nodes s USING(creator_account_id,segment_id) JOIN graph_node_content c USING(creator_account_id,content_id,node_id) WHERE m.kind='node' AND m.bucket=substr(c.node_id,4,2);

CREATE TRIGGER graph_node_view_insert INSTEAD OF INSERT ON graph_nodes
BEGIN
 SELECT CASE WHEN EXISTS(SELECT 1 FROM generation_graph_segments WHERE generation_id=NEW.generation_id AND creator_account_id=NEW.creator_account_id) THEN RAISE(ABORT,'graph_generation_layout_mixed') END;
 INSERT INTO graph_owned_nodes(generation_id,creator_account_id,node_id,kind,occurred_at,properties_json) VALUES(NEW.generation_id,NEW.creator_account_id,NEW.node_id,NEW.kind,NEW.occurred_at,NEW.properties_json);
END;
CREATE TRIGGER graph_node_view_update INSTEAD OF UPDATE ON graph_nodes
BEGIN
 SELECT CASE WHEN EXISTS(SELECT 1 FROM generation_graph_segments WHERE generation_id=OLD.generation_id AND creator_account_id=OLD.creator_account_id) THEN RAISE(ABORT,'projection_child_write_blocked') END;
 UPDATE graph_owned_nodes SET generation_id=NEW.generation_id,creator_account_id=NEW.creator_account_id,node_id=NEW.node_id,kind=NEW.kind,occurred_at=NEW.occurred_at,properties_json=NEW.properties_json WHERE generation_id=OLD.generation_id AND creator_account_id=OLD.creator_account_id AND node_id=OLD.node_id;
END;
CREATE TRIGGER graph_node_view_delete INSTEAD OF DELETE ON graph_nodes
BEGIN
 SELECT CASE WHEN EXISTS(SELECT 1 FROM generation_graph_segments WHERE generation_id=OLD.generation_id AND creator_account_id=OLD.creator_account_id) THEN RAISE(ABORT,'graph_manifest_delete_required') END;
 DELETE FROM graph_owned_nodes WHERE generation_id=OLD.generation_id AND creator_account_id=OLD.creator_account_id AND node_id=OLD.node_id;
END;

DROP VIEW graph_edges;
CREATE VIEW graph_edges AS SELECT generation_id,creator_account_id,edge_id,source_id,target_id,relation,occurred_at,sequence,properties_json FROM graph_owned_edges UNION ALL SELECT m.generation_id,m.creator_account_id,c.edge_id,c.source_id,c.target_id,c.relation,c.occurred_at,c.sequence,c.properties_json FROM generation_graph_segments m JOIN graph_segment_edges s USING(creator_account_id,segment_id) JOIN graph_edge_content c USING(creator_account_id,content_id,edge_id) WHERE m.kind='edge' AND m.bucket=substr(c.edge_id,4,2);

CREATE TRIGGER graph_edge_view_insert INSTEAD OF INSERT ON graph_edges
BEGIN
 SELECT CASE WHEN EXISTS(SELECT 1 FROM generation_graph_segments WHERE generation_id=NEW.generation_id AND creator_account_id=NEW.creator_account_id) THEN RAISE(ABORT,'graph_generation_layout_mixed') END;
 INSERT INTO graph_owned_edges(generation_id,creator_account_id,edge_id,source_id,target_id,relation,occurred_at,sequence,properties_json) VALUES(NEW.generation_id,NEW.creator_account_id,NEW.edge_id,NEW.source_id,NEW.target_id,NEW.relation,NEW.occurred_at,NEW.sequence,NEW.properties_json);
END;
CREATE TRIGGER graph_edge_view_update INSTEAD OF UPDATE ON graph_edges
BEGIN
 SELECT CASE WHEN EXISTS(SELECT 1 FROM generation_graph_segments WHERE generation_id=OLD.generation_id AND creator_account_id=OLD.creator_account_id) THEN RAISE(ABORT,'projection_child_write_blocked') END;
 UPDATE graph_owned_edges SET generation_id=NEW.generation_id,creator_account_id=NEW.creator_account_id,edge_id=NEW.edge_id,source_id=NEW.source_id,target_id=NEW.target_id,relation=NEW.relation,occurred_at=NEW.occurred_at,sequence=NEW.sequence,properties_json=NEW.properties_json WHERE generation_id=OLD.generation_id AND creator_account_id=OLD.creator_account_id AND edge_id=OLD.edge_id;
END;
CREATE TRIGGER graph_edge_view_delete INSTEAD OF DELETE ON graph_edges
BEGIN
 SELECT CASE WHEN EXISTS(SELECT 1 FROM generation_graph_segments WHERE generation_id=OLD.generation_id AND creator_account_id=OLD.creator_account_id) THEN RAISE(ABORT,'graph_manifest_delete_required') END;
 DELETE FROM graph_owned_edges WHERE generation_id=OLD.generation_id AND creator_account_id=OLD.creator_account_id AND edge_id=OLD.edge_id;
END;
