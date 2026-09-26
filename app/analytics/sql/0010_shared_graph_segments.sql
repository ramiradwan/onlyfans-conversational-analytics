-- Immutable graph content and generation-selected record segments.


ALTER TABLE graph_edges RENAME TO graph_owned_edges;

ALTER TABLE graph_nodes RENAME TO graph_owned_nodes;

CREATE TABLE graph_node_identities (creator_account_id TEXT NOT NULL CHECK(length(creator_account_id)=67 AND substr(creator_account_id,1,3)='a1:' AND substr(creator_account_id,4) NOT GLOB '*[^0-9a-f]*'), node_id TEXT NOT NULL CHECK(length(node_id)=67 AND substr(node_id,1,3)='g1:' AND substr(node_id,4) NOT GLOB '*[^0-9a-f]*'), PRIMARY KEY(creator_account_id,node_id)) WITHOUT ROWID;

CREATE TABLE graph_node_content (
    creator_account_id TEXT NOT NULL CHECK (
        length(creator_account_id)=67
        AND substr(creator_account_id,1,3)='a1:'
        AND substr(creator_account_id,4) NOT GLOB '*[^0-9a-f]*'
    ),
    node_id TEXT NOT NULL CHECK (
        length(node_id)=67 AND substr(node_id,1,3)='g1:'
        AND substr(node_id,4) NOT GLOB '*[^0-9a-f]*'
    ),
    kind TEXT NOT NULL CHECK (kind IN (
        'participant','conversation','message','topic','entity',
        'affect_state','engagement_state'
    )),
    occurred_at TEXT CHECK (
        occurred_at IS NULL OR (
            length(occurred_at)=27 AND substr(occurred_at,11,1)='T'
            AND substr(occurred_at,20,1)='.' AND substr(occurred_at,27,1)='Z'
            AND datetime(occurred_at) IS NOT NULL
        )
    ),
    properties_json TEXT NOT NULL CHECK (
        json_valid(properties_json) AND json_type(properties_json)='object'
    ),
    content_id TEXT NOT NULL CHECK(length(content_id)=64 AND content_id NOT GLOB '*[^0-9a-f]*'),
    FOREIGN KEY(creator_account_id,node_id) REFERENCES graph_node_identities(creator_account_id,node_id),
    PRIMARY KEY(creator_account_id,content_id),
    UNIQUE(creator_account_id,content_id,node_id)
) WITHOUT ROWID;

CREATE INDEX graph_node_content_by_id ON graph_node_content(creator_account_id,node_id,content_id);

CREATE TRIGGER graph_node_content_immutable BEFORE UPDATE ON graph_node_content BEGIN SELECT RAISE(ABORT,'graph_content_immutable'); END;

CREATE TABLE graph_edge_content (
    creator_account_id TEXT NOT NULL CHECK (
        length(creator_account_id)=67
        AND substr(creator_account_id,1,3)='a1:'
        AND substr(creator_account_id,4) NOT GLOB '*[^0-9a-f]*'
    ),
    edge_id TEXT NOT NULL CHECK (
        length(edge_id)=67 AND substr(edge_id,1,3)='e1:'
        AND substr(edge_id,4) NOT GLOB '*[^0-9a-f]*'
    ),
    source_id TEXT NOT NULL CHECK (
        length(source_id)=67 AND substr(source_id,1,3)='g1:'
        AND substr(source_id,4) NOT GLOB '*[^0-9a-f]*'
    ),
    target_id TEXT NOT NULL CHECK (
        length(target_id)=67 AND substr(target_id,1,3)='g1:'
        AND substr(target_id,4) NOT GLOB '*[^0-9a-f]*'
    ),
    relation TEXT NOT NULL CHECK (relation IN (
        'participates_in','contains','sent','received_by','expresses_affect',
        'has_engagement_state','mentions_topic','mentions_entity','precedes'
    )),
    occurred_at TEXT CHECK (
        occurred_at IS NULL OR (
            length(occurred_at)=27 AND substr(occurred_at,11,1)='T'
            AND substr(occurred_at,20,1)='.' AND substr(occurred_at,27,1)='Z'
            AND datetime(occurred_at) IS NOT NULL
        )
    ),
    sequence INTEGER CHECK (sequence IS NULL OR sequence >= 0),
    properties_json TEXT NOT NULL CHECK (
        json_valid(properties_json) AND json_type(properties_json)='object'
    ),
    content_id TEXT NOT NULL CHECK(length(content_id)=64 AND content_id NOT GLOB '*[^0-9a-f]*'),
    FOREIGN KEY (creator_account_id,source_id) REFERENCES graph_node_identities(creator_account_id,node_id),
    FOREIGN KEY (creator_account_id,target_id) REFERENCES graph_node_identities(creator_account_id,node_id),
    PRIMARY KEY(creator_account_id,content_id),
    UNIQUE(creator_account_id,content_id,edge_id)
) WITHOUT ROWID;

CREATE INDEX graph_edge_content_by_id ON graph_edge_content(creator_account_id,edge_id,content_id);

CREATE TRIGGER graph_edge_content_immutable BEFORE UPDATE ON graph_edge_content BEGIN SELECT RAISE(ABORT,'graph_content_immutable'); END;

CREATE INDEX graph_edge_content_by_source ON graph_edge_content(creator_account_id,source_id);

CREATE INDEX graph_edge_content_by_target ON graph_edge_content(creator_account_id,target_id);

CREATE TABLE graph_segments (
    creator_account_id TEXT NOT NULL CHECK(length(creator_account_id)=67 AND substr(creator_account_id,1,3)='a1:' AND substr(creator_account_id,4) NOT GLOB '*[^0-9a-f]*'),
    segment_id TEXT NOT NULL CHECK(length(segment_id)=36),
    kind TEXT NOT NULL CHECK(kind IN ('node','edge')),
    bucket TEXT NOT NULL CHECK(length(bucket)=2 AND bucket NOT GLOB '*[^0-9a-f]*'),
    content_digest TEXT NOT NULL CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),
    sealed INTEGER NOT NULL DEFAULT 0 CHECK(sealed IN (0,1)),
    PRIMARY KEY(creator_account_id,segment_id),
    UNIQUE(creator_account_id,segment_id,kind,bucket)
) WITHOUT ROWID;
CREATE TABLE generation_graph_segments (
    generation_id TEXT NOT NULL,
    creator_account_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    bucket TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    PRIMARY KEY(generation_id,creator_account_id,kind,bucket),
    FOREIGN KEY(generation_id,creator_account_id) REFERENCES projection_generations(generation_id,creator_account_id) ON DELETE CASCADE,
    FOREIGN KEY(creator_account_id,segment_id,kind,bucket) REFERENCES graph_segments(creator_account_id,segment_id,kind,bucket)
) WITHOUT ROWID;
CREATE INDEX generation_graph_segments_by_segment ON generation_graph_segments(creator_account_id,segment_id);
CREATE TRIGGER generation_graph_segments_immutable BEFORE UPDATE ON generation_graph_segments
BEGIN SELECT RAISE(ABORT,'graph_manifest_immutable'); END;
CREATE TRIGGER graph_segments_immutable BEFORE UPDATE ON graph_segments
WHEN NEW.creator_account_id!=OLD.creator_account_id OR NEW.segment_id!=OLD.segment_id
 OR NEW.kind!=OLD.kind OR NEW.bucket!=OLD.bucket OR NEW.content_digest!=OLD.content_digest
 OR OLD.sealed!=0 OR NEW.sealed!=1
BEGIN SELECT RAISE(ABORT,'graph_segment_immutable'); END;


CREATE TABLE graph_segment_nodes (
    creator_account_id TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    content_id TEXT NOT NULL,
    PRIMARY KEY(creator_account_id,segment_id,node_id),
    FOREIGN KEY(creator_account_id,segment_id) REFERENCES graph_segments(creator_account_id,segment_id) ON DELETE CASCADE,
    FOREIGN KEY(creator_account_id,content_id,node_id) REFERENCES graph_node_content(creator_account_id,content_id,node_id)
) WITHOUT ROWID;
CREATE INDEX graph_segment_nodes_by_content ON graph_segment_nodes(creator_account_id,content_id);
CREATE TRIGGER graph_segment_nodes_immutable BEFORE UPDATE ON graph_segment_nodes
BEGIN SELECT RAISE(ABORT,'graph_segment_immutable'); END;
CREATE TRIGGER graph_segment_nodes_insert BEFORE INSERT ON graph_segment_nodes
WHEN NOT EXISTS (
 SELECT 1 FROM graph_segments s JOIN generation_graph_segments m USING(creator_account_id,segment_id)
 JOIN projection_generations g USING(creator_account_id,generation_id)
 WHERE s.creator_account_id=NEW.creator_account_id AND s.segment_id=NEW.segment_id
 AND s.kind='node' AND s.bucket=substr(NEW.node_id,4,2)
 AND s.sealed=0 AND g.status='building')
BEGIN SELECT RAISE(ABORT,'graph_segment_not_building'); END;
CREATE TRIGGER graph_segment_nodes_delete BEFORE DELETE ON graph_segment_nodes
WHEN EXISTS(SELECT 1 FROM graph_segments s WHERE s.creator_account_id=OLD.creator_account_id AND s.segment_id=OLD.segment_id AND s.sealed=1)
BEGIN SELECT RAISE(ABORT,'graph_segment_immutable'); END;


CREATE VIEW graph_nodes AS SELECT generation_id,creator_account_id,node_id,kind,occurred_at,properties_json FROM graph_owned_nodes UNION ALL SELECT m.generation_id,m.creator_account_id,c.node_id,c.kind,c.occurred_at,c.properties_json FROM generation_graph_segments m JOIN graph_segment_nodes s USING(creator_account_id,segment_id) JOIN graph_node_content c USING(creator_account_id,content_id,node_id) WHERE m.kind='node';

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


CREATE TABLE graph_segment_edges (
    creator_account_id TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    edge_id TEXT NOT NULL,
    content_id TEXT NOT NULL,
    PRIMARY KEY(creator_account_id,segment_id,edge_id),
    FOREIGN KEY(creator_account_id,segment_id) REFERENCES graph_segments(creator_account_id,segment_id) ON DELETE CASCADE,
    FOREIGN KEY(creator_account_id,content_id,edge_id) REFERENCES graph_edge_content(creator_account_id,content_id,edge_id)
) WITHOUT ROWID;
CREATE INDEX graph_segment_edges_by_content ON graph_segment_edges(creator_account_id,content_id);
CREATE TRIGGER graph_segment_edges_immutable BEFORE UPDATE ON graph_segment_edges
BEGIN SELECT RAISE(ABORT,'graph_segment_immutable'); END;
CREATE TRIGGER graph_segment_edges_insert BEFORE INSERT ON graph_segment_edges
WHEN NOT EXISTS (
 SELECT 1 FROM graph_segments s JOIN generation_graph_segments m USING(creator_account_id,segment_id)
 JOIN projection_generations g USING(creator_account_id,generation_id)
 WHERE s.creator_account_id=NEW.creator_account_id AND s.segment_id=NEW.segment_id
 AND s.kind='edge' AND s.bucket=substr(NEW.edge_id,4,2)
 AND s.sealed=0 AND g.status='building')
BEGIN SELECT RAISE(ABORT,'graph_segment_not_building'); END;
CREATE TRIGGER graph_segment_edges_delete BEFORE DELETE ON graph_segment_edges
WHEN EXISTS(SELECT 1 FROM graph_segments s WHERE s.creator_account_id=OLD.creator_account_id AND s.segment_id=OLD.segment_id AND s.sealed=1)
BEGIN SELECT RAISE(ABORT,'graph_segment_immutable'); END;


CREATE VIEW graph_edges AS SELECT generation_id,creator_account_id,edge_id,source_id,target_id,relation,occurred_at,sequence,properties_json FROM graph_owned_edges UNION ALL SELECT m.generation_id,m.creator_account_id,c.edge_id,c.source_id,c.target_id,c.relation,c.occurred_at,c.sequence,c.properties_json FROM generation_graph_segments m JOIN graph_segment_edges s USING(creator_account_id,segment_id) JOIN graph_edge_content c USING(creator_account_id,content_id,edge_id) WHERE m.kind='edge';

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


CREATE TRIGGER shared_graph_node_property_contract_insert
BEFORE INSERT ON graph_node_content
WHEN EXISTS (
    SELECT 1 FROM json_each(NEW.properties_json) AS property
    WHERE property.type IN ('array','object')
       OR (NEW.kind='participant' AND property.key NOT IN ('role'))
       OR (NEW.kind='conversation' AND property.key NOT IN (
            'message_count','turn_count','average_sentiment_score','response_coverage'))
       OR (NEW.kind='message' AND property.key NOT IN (
            'direction','source_ordinal','character_count'))
       OR (NEW.kind='topic' AND property.key NOT IN ('taxonomy_id','label'))
       OR (NEW.kind='entity' AND property.key NOT IN ('entity_type','entity_ref'))
       OR (NEW.kind='affect_state' AND property.key NOT IN ('label','score','confidence'))
       OR (NEW.kind='engagement_state' AND property.key NOT IN ('state','confidence'))
       OR (NEW.kind='participant' AND property.key='role'
           AND (property.type!='text' OR property.value NOT IN ('creator','counterpart')))
       OR (NEW.kind='conversation' AND property.key IN ('message_count','turn_count')
           AND (property.type!='integer' OR property.value<0))
       OR (NEW.kind='conversation' AND property.key='average_sentiment_score'
           AND property.type!='null'
           AND (property.type NOT IN ('integer','real') OR property.value NOT BETWEEN -1 AND 1))
       OR (NEW.kind='conversation' AND property.key='response_coverage'
           AND property.type!='null'
           AND (property.type NOT IN ('integer','real') OR property.value NOT BETWEEN 0 AND 1))
       OR (NEW.kind='message' AND property.key='direction'
           AND (property.type!='text' OR property.value NOT IN ('inbound','outbound')))
       OR (NEW.kind='message' AND property.key IN ('source_ordinal','character_count')
           AND (property.type!='integer' OR property.value<0))
       OR (NEW.kind='topic' AND property.key='taxonomy_id'
           AND (property.type!='text' OR property.value NOT IN (
                'feedback','greeting','media','pricing','scheduling','support')))
       OR (NEW.kind='topic' AND property.key='label'
           AND (property.type!='text' OR property.value NOT IN (
                'Feedback','Greeting','Media','Pricing','Scheduling','Support')))
       OR (NEW.kind='entity' AND property.key='entity_type'
           AND (property.type!='text' OR property.value NOT IN ('amount','hashtag','mention','url')))
       OR (NEW.kind='entity' AND property.key='entity_ref'
           AND (property.type!='text' OR length(property.value)!=67
                OR substr(property.value,1,3)!='x1:'
                OR substr(property.value,4) GLOB '*[^0-9a-f]*'))
       OR (NEW.kind='affect_state' AND property.key='label'
           AND (property.type!='text' OR property.value NOT IN ('positive','neutral','negative')))
       OR (NEW.kind='affect_state' AND property.key='score'
           AND property.type!='null'
           AND (property.type NOT IN ('integer','real') OR property.value NOT BETWEEN -1 AND 1))
       OR (NEW.kind IN ('affect_state','engagement_state') AND property.key='confidence'
           AND property.type!='null'
           AND (property.type NOT IN ('integer','real') OR property.value NOT BETWEEN 0 AND 1))
       OR (NEW.kind='engagement_state' AND property.key='state'
           AND (property.type!='text' OR property.value NOT IN (
                'acknowledgement','commitment','constraint','coordination',
                'information','inquiry','minimal','transactional')))
)
BEGIN
    SELECT RAISE(ABORT,'graph_property_invalid');
END;

CREATE TRIGGER shared_graph_edge_property_contract_insert
BEFORE INSERT ON graph_edge_content
WHEN EXISTS (
    SELECT 1 FROM json_each(NEW.properties_json) AS property
    WHERE property.type IN ('array','object')
       OR (NEW.relation='participates_in' AND property.key NOT IN ('role'))
       OR (NEW.relation IN (
            'contains','sent','received_by','expresses_affect','has_engagement_state')
           AND 1)
       OR (NEW.relation IN ('mentions_topic','mentions_entity')
           AND property.key NOT IN ('confidence'))
       OR (NEW.relation='precedes'
           AND property.key NOT IN ('scope','interval_seconds'))
       OR (NEW.relation='participates_in' AND property.key='role'
           AND (property.type!='text' OR property.value NOT IN ('creator','counterpart')))
       OR (NEW.relation IN ('mentions_topic','mentions_entity')
           AND property.key='confidence' AND property.type!='null'
           AND (property.type NOT IN ('integer','real') OR property.value NOT BETWEEN 0 AND 1))
       OR (NEW.relation='precedes' AND property.key='scope'
           AND (property.type!='text' OR property.value NOT IN ('message','conversation')))
       OR (NEW.relation='precedes' AND property.key='interval_seconds'
           AND property.type!='null'
           AND (property.type NOT IN ('integer','real') OR property.value<0
                OR abs(property.value)>1.7976931348623157e308))
)
BEGIN
    SELECT RAISE(ABORT,'graph_property_invalid');
END;

CREATE TRIGGER shared_topic_taxonomy_pair_insert
BEFORE INSERT ON graph_node_content
WHEN NEW.kind='topic' AND (
       COALESCE(json_type(NEW.properties_json,'$.taxonomy_id'),'')!='text'
    OR COALESCE(json_type(NEW.properties_json,'$.label'),'')!='text'
    OR COALESCE(json_extract(NEW.properties_json,'$.label'),'') != COALESCE(
        CASE json_extract(NEW.properties_json,'$.taxonomy_id')
            WHEN 'feedback' THEN 'Feedback' WHEN 'greeting' THEN 'Greeting'
            WHEN 'media' THEN 'Media' WHEN 'pricing' THEN 'Pricing'
            WHEN 'scheduling' THEN 'Scheduling' WHEN 'support' THEN 'Support'
        END,
        ''
    )
)
BEGIN
    SELECT RAISE(ABORT,'graph_property_invalid');
END;

CREATE TRIGGER generation_graph_segments_insert BEFORE INSERT ON generation_graph_segments
WHEN COALESCE((SELECT status FROM projection_generations WHERE generation_id=NEW.generation_id AND creator_account_id=NEW.creator_account_id),'')!='building'
 OR EXISTS(SELECT 1 FROM graph_owned_nodes WHERE generation_id=NEW.generation_id AND creator_account_id=NEW.creator_account_id)
 OR EXISTS(SELECT 1 FROM graph_owned_edges WHERE generation_id=NEW.generation_id AND creator_account_id=NEW.creator_account_id)
BEGIN SELECT RAISE(ABORT,'graph_manifest_not_building'); END;
CREATE TRIGGER generation_graph_segments_delete BEFORE DELETE ON generation_graph_segments
WHEN COALESCE((SELECT status FROM projection_generations WHERE generation_id=OLD.generation_id AND creator_account_id=OLD.creator_account_id),'') NOT IN ('','building','retired')
BEGIN SELECT RAISE(ABORT,'graph_manifest_delete_blocked'); END;


CREATE TRIGGER graph_segment_seal_bound BEFORE UPDATE OF sealed ON graph_segments
WHEN NOT EXISTS(SELECT 1 FROM generation_graph_segments m JOIN projection_generations g USING(generation_id,creator_account_id)
 WHERE m.creator_account_id=NEW.creator_account_id AND m.segment_id=NEW.segment_id AND g.status='building')
BEGIN SELECT RAISE(ABORT,'graph_segment_not_building'); END;
CREATE TRIGGER graph_generation_segments_sealed BEFORE UPDATE OF status ON projection_generations
WHEN NEW.status IN ('validated','activation_pending','active') AND EXISTS(
 SELECT 1 FROM generation_graph_segments m JOIN graph_segments s USING(creator_account_id,segment_id)
 WHERE m.generation_id=NEW.generation_id AND m.creator_account_id=NEW.creator_account_id AND s.sealed!=1)
BEGIN SELECT RAISE(ABORT,'graph_segment_unsealed'); END;
CREATE TRIGGER graph_segment_reclaim AFTER DELETE ON generation_graph_segments
WHEN NOT EXISTS(SELECT 1 FROM generation_graph_segments WHERE creator_account_id=OLD.creator_account_id AND segment_id=OLD.segment_id)
BEGIN DELETE FROM graph_segments WHERE creator_account_id=OLD.creator_account_id AND segment_id=OLD.segment_id; END;


CREATE TRIGGER graph_node_identity_insert BEFORE INSERT ON graph_node_content
BEGIN INSERT OR IGNORE INTO graph_node_identities(creator_account_id,node_id) VALUES(NEW.creator_account_id,NEW.node_id); END;
CREATE TRIGGER graph_node_identity_immutable BEFORE UPDATE ON graph_node_identities
BEGIN SELECT RAISE(ABORT,'graph_identity_immutable'); END;
CREATE TRIGGER graph_node_content_reclaim AFTER DELETE ON graph_segment_nodes
BEGIN DELETE FROM graph_node_content WHERE creator_account_id=OLD.creator_account_id AND content_id=OLD.content_id AND NOT EXISTS(SELECT 1 FROM graph_segment_nodes WHERE creator_account_id=graph_node_content.creator_account_id AND content_id=graph_node_content.content_id); END;
CREATE TRIGGER graph_edge_content_reclaim AFTER DELETE ON graph_segment_edges
BEGIN DELETE FROM graph_edge_content WHERE creator_account_id=OLD.creator_account_id AND content_id=OLD.content_id AND NOT EXISTS(SELECT 1 FROM graph_segment_edges WHERE creator_account_id=OLD.creator_account_id AND content_id=OLD.content_id); END;
CREATE TRIGGER graph_node_identity_reclaim AFTER DELETE ON graph_node_content
BEGIN DELETE FROM graph_node_identities WHERE creator_account_id=OLD.creator_account_id AND node_id=OLD.node_id AND NOT EXISTS(SELECT 1 FROM graph_node_content WHERE creator_account_id=graph_node_identities.creator_account_id AND node_id=graph_node_identities.node_id) AND NOT EXISTS(SELECT 1 FROM graph_edge_content WHERE creator_account_id=graph_node_identities.creator_account_id AND source_id=graph_node_identities.node_id) AND NOT EXISTS(SELECT 1 FROM graph_edge_content WHERE creator_account_id=graph_node_identities.creator_account_id AND target_id=graph_node_identities.node_id); END;
CREATE TRIGGER graph_endpoint_identity_reclaim AFTER DELETE ON graph_edge_content
BEGIN DELETE FROM graph_node_identities WHERE creator_account_id=OLD.creator_account_id AND node_id IN(OLD.source_id,OLD.target_id) AND NOT EXISTS(SELECT 1 FROM graph_node_content WHERE creator_account_id=graph_node_identities.creator_account_id AND node_id=graph_node_identities.node_id) AND NOT EXISTS(SELECT 1 FROM graph_edge_content WHERE creator_account_id=graph_node_identities.creator_account_id AND source_id=graph_node_identities.node_id) AND NOT EXISTS(SELECT 1 FROM graph_edge_content WHERE creator_account_id=graph_node_identities.creator_account_id AND target_id=graph_node_identities.node_id); END;


CREATE TRIGGER graph_node_content_building BEFORE INSERT ON graph_node_content
WHEN NOT EXISTS(SELECT 1 FROM graph_segments s CROSS JOIN generation_graph_segments m USING(creator_account_id,segment_id)
 CROSS JOIN projection_generations g USING(creator_account_id,generation_id)
 WHERE s.creator_account_id=NEW.creator_account_id AND s.kind='node'
 AND s.bucket=substr(NEW.node_id,4,2) AND s.sealed=0 AND g.status='building')
BEGIN SELECT RAISE(ABORT,'graph_content_not_building'); END;

CREATE TRIGGER graph_edge_content_building BEFORE INSERT ON graph_edge_content
WHEN NOT EXISTS(SELECT 1 FROM graph_segments s CROSS JOIN generation_graph_segments m USING(creator_account_id,segment_id)
 CROSS JOIN projection_generations g USING(creator_account_id,generation_id)
 WHERE s.creator_account_id=NEW.creator_account_id AND s.kind='edge'
 AND s.bucket=substr(NEW.edge_id,4,2) AND s.sealed=0 AND g.status='building')
BEGIN SELECT RAISE(ABORT,'graph_content_not_building'); END;

CREATE TRIGGER graph_owned_node_layout BEFORE INSERT ON graph_owned_nodes
WHEN EXISTS(SELECT 1 FROM generation_graph_segments WHERE generation_id=NEW.generation_id AND creator_account_id=NEW.creator_account_id)
BEGIN SELECT RAISE(ABORT,'graph_generation_layout_mixed'); END;

CREATE TRIGGER graph_owned_edge_layout BEFORE INSERT ON graph_owned_edges
WHEN EXISTS(SELECT 1 FROM generation_graph_segments WHERE generation_id=NEW.generation_id AND creator_account_id=NEW.creator_account_id)
BEGIN SELECT RAISE(ABORT,'graph_generation_layout_mixed'); END;

CREATE INDEX graph_segments_building_lookup
ON graph_segments(creator_account_id,kind,bucket,sealed,segment_id);
