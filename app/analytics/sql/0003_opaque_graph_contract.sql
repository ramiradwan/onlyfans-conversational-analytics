-- Projection v3 is disposable. Retire the v1/v2 representation instead of
-- retaining raw account partitions or legacy graph/cache identities.
DROP TRIGGER IF EXISTS projection_publication_epoch_identity_immutable;
DROP TRIGGER IF EXISTS projection_publication_epoch_monotonic;
DROP TRIGGER IF EXISTS projection_publication_epoch_delete_blocked;
DROP TRIGGER IF EXISTS projection_generation_transition_monotonic;
DROP TRIGGER IF EXISTS projection_generation_identity_immutable;
DROP TRIGGER IF EXISTS projection_generation_validation_complete;
DROP TRIGGER IF EXISTS projection_generation_activation_bound;
DROP TRIGGER IF EXISTS projection_generation_witness_immutable;
DROP TRIGGER IF EXISTS projection_generation_activation_epoch_open;
DROP TRIGGER IF EXISTS projection_generation_delete_retired_only;
DROP TRIGGER IF EXISTS projection_document_building_insert;
DROP TRIGGER IF EXISTS projection_document_building_update;
DROP TRIGGER IF EXISTS projection_document_delete_guard;
DROP TRIGGER IF EXISTS graph_node_building_insert;
DROP TRIGGER IF EXISTS graph_node_building_update;
DROP TRIGGER IF EXISTS graph_node_delete_guard;
DROP TRIGGER IF EXISTS graph_edge_building_insert;
DROP TRIGGER IF EXISTS graph_edge_building_update;
DROP TRIGGER IF EXISTS graph_edge_delete_guard;
DROP TRIGGER IF EXISTS graph_stats_building_insert;
DROP TRIGGER IF EXISTS graph_stats_building_update;
DROP TRIGGER IF EXISTS graph_stats_delete_guard;
DROP TRIGGER IF EXISTS graph_metric_active_insert;
DROP TRIGGER IF EXISTS graph_metric_update_blocked;
DROP TRIGGER IF EXISTS graph_metric_delete_guard;

DROP TABLE graph_algorithm_metrics;
DROP TABLE graph_partition_stats;
DROP TABLE graph_edges;
DROP TABLE graph_nodes;
DROP TABLE analytics_projections;
DROP TABLE projection_generations;
DROP TABLE projection_publication_epochs;

CREATE TABLE projection_store_identity (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    store_id TEXT NOT NULL CHECK (
        length(store_id) = 32
        AND store_id NOT GLOB '*[^0-9a-f]*'
    ),
    schema_identity TEXT NOT NULL CHECK (schema_identity = 'projection-v3')
) WITHOUT ROWID;

INSERT INTO projection_store_identity(singleton, store_id, schema_identity)
VALUES (1, lower(hex(randomblob(16))), 'projection-v3');

CREATE TABLE projection_publication_epochs (
    publication_epoch TEXT PRIMARY KEY,
    scheduler_owner_id TEXT NOT NULL,
    scheduler_capability_digest TEXT NOT NULL CHECK (
        length(scheduler_capability_digest)=71
        AND substr(scheduler_capability_digest,1,7)='sha256:'
        AND substr(scheduler_capability_digest,8) NOT GLOB '*[^0-9a-f]*'
    ),
    state TEXT NOT NULL CHECK (state IN ('open', 'revoked')),
    opened_at TEXT NOT NULL CHECK (
        length(opened_at)=27 AND substr(opened_at,11,1)='T'
        AND substr(opened_at,20,1)='.' AND substr(opened_at,27,1)='Z'
        AND datetime(opened_at) IS NOT NULL
    ),
    revoked_at TEXT CHECK (
        revoked_at IS NULL OR (
            length(revoked_at)=27 AND substr(revoked_at,11,1)='T'
            AND substr(revoked_at,20,1)='.' AND substr(revoked_at,27,1)='Z'
            AND datetime(revoked_at) IS NOT NULL
        )
    )
) WITHOUT ROWID;

CREATE TABLE projection_generations (
    generation_id TEXT PRIMARY KEY,
    creator_account_id TEXT NOT NULL CHECK (
        length(creator_account_id)=67
        AND substr(creator_account_id,1,3)='a1:'
        AND substr(creator_account_id,4) NOT GLOB '*[^0-9a-f]*'
    ),
    status TEXT NOT NULL CHECK (
        status IN ('building','validated','activation_pending','active','retired')
    ),
    schema_version INTEGER NOT NULL CHECK (schema_version = 3),
    build_version TEXT NOT NULL,
    canonical_revision INTEGER NOT NULL CHECK (canonical_revision >= 0),
    canonical_content_digest TEXT NOT NULL CHECK (
        length(canonical_content_digest)=71
        AND substr(canonical_content_digest,1,7)='sha256:'
        AND substr(canonical_content_digest,8) NOT GLOB '*[^0-9a-f]*'
    ),
    canonical_high_water_json TEXT NOT NULL CHECK (
        json_valid(canonical_high_water_json)
        AND json_type(canonical_high_water_json)='object'
    ),
    pipeline_revision TEXT NOT NULL,
    pipeline_config_digest TEXT NOT NULL CHECK (
        length(pipeline_config_digest)=71
        AND substr(pipeline_config_digest,1,7)='sha256:'
        AND substr(pipeline_config_digest,8) NOT GLOB '*[^0-9a-f]*'
    ),
    pipeline_identity_digest TEXT NOT NULL CHECK (
        length(pipeline_identity_digest)=71
        AND substr(pipeline_identity_digest,1,7)='sha256:'
        AND substr(pipeline_identity_digest,8) NOT GLOB '*[^0-9a-f]*'
    ),
    projection_digest TEXT CHECK (
        projection_digest IS NULL OR (
            length(projection_digest)=71
            AND substr(projection_digest,1,7)='sha256:'
            AND substr(projection_digest,8) NOT GLOB '*[^0-9a-f]*'
        )
    ),
    graph_digest TEXT CHECK (
        graph_digest IS NULL OR (
            length(graph_digest)=71
            AND substr(graph_digest,1,7)='sha256:'
            AND substr(graph_digest,8) NOT GLOB '*[^0-9a-f]*'
        )
    ),
    node_count INTEGER NOT NULL DEFAULT 0 CHECK (node_count >= 0),
    edge_count INTEGER NOT NULL DEFAULT 0 CHECK (edge_count >= 0),
    activation_intent_id TEXT,
    witness_sequence INTEGER CHECK (witness_sequence IS NULL OR witness_sequence > 0),
    expected_active_generation_id TEXT,
    expected_active_revision INTEGER CHECK (
        expected_active_revision IS NULL OR expected_active_revision >= 0
    ),
    publication_epoch TEXT,
    owner_id TEXT NOT NULL,
    owner_pid INTEGER NOT NULL CHECK (owner_pid > 0),
    owner_process_started_at TEXT NOT NULL,
    owner_instance_nonce TEXT NOT NULL,
    owner_capability_digest TEXT NOT NULL CHECK (
        length(owner_capability_digest)=71
        AND substr(owner_capability_digest,1,7)='sha256:'
        AND substr(owner_capability_digest,8) NOT GLOB '*[^0-9a-f]*'
    ),
    lease_expires_at TEXT NOT NULL CHECK (
        length(lease_expires_at)=27 AND substr(lease_expires_at,11,1)='T'
        AND substr(lease_expires_at,20,1)='.' AND substr(lease_expires_at,27,1)='Z'
        AND datetime(lease_expires_at) IS NOT NULL
    ),
    started_at TEXT NOT NULL CHECK (
        length(started_at)=27 AND substr(started_at,11,1)='T'
        AND substr(started_at,20,1)='.' AND substr(started_at,27,1)='Z'
        AND datetime(started_at) IS NOT NULL
    ),
    validated_at TEXT CHECK (
        validated_at IS NULL OR (
            length(validated_at)=27 AND substr(validated_at,11,1)='T'
            AND substr(validated_at,20,1)='.' AND substr(validated_at,27,1)='Z'
            AND datetime(validated_at) IS NOT NULL
        )
    ),
    activated_at TEXT CHECK (
        activated_at IS NULL OR (
            length(activated_at)=27 AND substr(activated_at,11,1)='T'
            AND substr(activated_at,20,1)='.' AND substr(activated_at,27,1)='Z'
            AND datetime(activated_at) IS NOT NULL
        )
    ),
    retired_at TEXT CHECK (
        retired_at IS NULL OR (
            length(retired_at)=27 AND substr(retired_at,11,1)='T'
            AND substr(retired_at,20,1)='.' AND substr(retired_at,27,1)='Z'
            AND datetime(retired_at) IS NOT NULL
        )
    ),
    UNIQUE (generation_id, creator_account_id),
    FOREIGN KEY (publication_epoch)
        REFERENCES projection_publication_epochs(publication_epoch)
) WITHOUT ROWID;

CREATE UNIQUE INDEX one_active_projection_generation_per_account
    ON projection_generations(creator_account_id) WHERE status='active';
CREATE INDEX projection_generations_by_account_status
    ON projection_generations(creator_account_id,status,canonical_revision);
CREATE INDEX projection_generations_by_lease
    ON projection_generations(status,lease_expires_at,owner_id);

CREATE TABLE analytics_projections (
    generation_id TEXT NOT NULL,
    creator_account_id TEXT NOT NULL CHECK (
        length(creator_account_id)=67
        AND substr(creator_account_id,1,3)='a1:'
        AND substr(creator_account_id,4) NOT GLOB '*[^0-9a-f]*'
    ),
    source_revision INTEGER NOT NULL CHECK (source_revision >= 0),
    pipeline_revision TEXT NOT NULL,
    pipeline_config_digest TEXT NOT NULL,
    content_digest TEXT NOT NULL CHECK (
        length(content_digest)=71 AND substr(content_digest,1,7)='sha256:'
        AND substr(content_digest,8) NOT GLOB '*[^0-9a-f]*'
    ),
    document_json TEXT NOT NULL CHECK (
        json_valid(document_json) AND json_type(document_json)='object'
        AND json_extract(document_json,'$.account_ref')=creator_account_id
        AND json_type(document_json,'$.creator_account_id') IS NULL
        AND json_type(document_json,'$.content_digest') IS NULL
        AND json_extract(document_json,'$.schema_version')='3'
    ),
    PRIMARY KEY (generation_id,creator_account_id),
    FOREIGN KEY (generation_id,creator_account_id)
        REFERENCES projection_generations(generation_id,creator_account_id)
        ON DELETE CASCADE
) WITHOUT ROWID;

CREATE INDEX analytics_projections_by_account_revision
    ON analytics_projections(creator_account_id,source_revision,generation_id);

CREATE TABLE graph_nodes (
    generation_id TEXT NOT NULL,
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
    PRIMARY KEY (generation_id,creator_account_id,node_id),
    FOREIGN KEY (generation_id,creator_account_id)
        REFERENCES projection_generations(generation_id,creator_account_id)
        ON DELETE CASCADE
) WITHOUT ROWID;

CREATE INDEX graph_nodes_by_account_kind_time
    ON graph_nodes(creator_account_id,generation_id,kind,occurred_at,node_id);

CREATE TABLE graph_edges (
    generation_id TEXT NOT NULL,
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
    PRIMARY KEY (generation_id,creator_account_id,edge_id),
    FOREIGN KEY (generation_id,creator_account_id)
        REFERENCES projection_generations(generation_id,creator_account_id)
        ON DELETE CASCADE,
    FOREIGN KEY (generation_id,creator_account_id,source_id)
        REFERENCES graph_nodes(generation_id,creator_account_id,node_id)
        ON DELETE CASCADE,
    FOREIGN KEY (generation_id,creator_account_id,target_id)
        REFERENCES graph_nodes(generation_id,creator_account_id,node_id)
        ON DELETE CASCADE
) WITHOUT ROWID;

CREATE INDEX graph_edges_by_account_relation_time
    ON graph_edges(creator_account_id,generation_id,relation,occurred_at,edge_id);
CREATE INDEX graph_edges_by_outgoing_endpoint
    ON graph_edges(creator_account_id,generation_id,source_id,occurred_at,relation,edge_id);
CREATE INDEX graph_edges_by_incoming_endpoint
    ON graph_edges(creator_account_id,generation_id,target_id,occurred_at,relation,edge_id);

CREATE TABLE graph_partition_stats (
    generation_id TEXT NOT NULL,
    creator_account_id TEXT NOT NULL,
    source_revision INTEGER NOT NULL CHECK (source_revision >= 0),
    node_count INTEGER NOT NULL CHECK (node_count >= 0),
    edge_count INTEGER NOT NULL CHECK (edge_count >= 0),
    graph_digest TEXT NOT NULL CHECK (
        length(graph_digest)=71 AND substr(graph_digest,1,7)='sha256:'
        AND substr(graph_digest,8) NOT GLOB '*[^0-9a-f]*'
    ),
    PRIMARY KEY (generation_id,creator_account_id),
    FOREIGN KEY (generation_id,creator_account_id)
        REFERENCES projection_generations(generation_id,creator_account_id)
        ON DELETE CASCADE
) WITHOUT ROWID;

CREATE TABLE graph_algorithm_metrics (
    generation_id TEXT NOT NULL,
    creator_account_id TEXT NOT NULL,
    metric_kind TEXT NOT NULL CHECK (metric_kind IN ('centrality','community')),
    algorithm TEXT NOT NULL,
    parameter_hash TEXT NOT NULL CHECK (
        length(parameter_hash)=71 AND substr(parameter_hash,1,7)='sha256:'
        AND substr(parameter_hash,8) NOT GLOB '*[^0-9a-f]*'
    ),
    result_json TEXT NOT NULL CHECK (json_valid(result_json)),
    computed_at TEXT NOT NULL CHECK (
        length(computed_at)=27 AND substr(computed_at,11,1)='T'
        AND substr(computed_at,20,1)='.' AND substr(computed_at,27,1)='Z'
        AND datetime(computed_at) IS NOT NULL
    ),
    activation_intent_id TEXT NOT NULL,
    witness_sequence INTEGER NOT NULL CHECK (witness_sequence > 0),
    publication_epoch TEXT NOT NULL,
    PRIMARY KEY (
        generation_id,creator_account_id,metric_kind,algorithm,parameter_hash
    ),
    FOREIGN KEY (generation_id,creator_account_id)
        REFERENCES projection_generations(generation_id,creator_account_id)
        ON DELETE CASCADE
) WITHOUT ROWID;

CREATE TRIGGER graph_node_property_contract_insert
BEFORE INSERT ON graph_nodes
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

CREATE TRIGGER graph_node_building_update
BEFORE UPDATE ON graph_nodes
BEGIN
    SELECT CASE WHEN COALESCE((SELECT status FROM projection_generations
        WHERE generation_id=NEW.generation_id
          AND creator_account_id=NEW.creator_account_id),'')!='building'
        THEN RAISE(ABORT,'projection_child_write_blocked') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM json_each(NEW.properties_json) AS property
        WHERE property.type IN ('array','object')
    ) THEN RAISE(ABORT,'graph_property_invalid') END;
    SELECT CASE WHEN NEW.properties_json != OLD.properties_json
        OR NEW.node_id != OLD.node_id OR NEW.kind != OLD.kind
        OR NEW.occurred_at IS NOT OLD.occurred_at
        OR NEW.creator_account_id != OLD.creator_account_id
        OR NEW.generation_id != OLD.generation_id
        THEN RAISE(ABORT,'graph_node_update_use_upsert') END;
END;

CREATE TRIGGER graph_edge_property_contract_insert
BEFORE INSERT ON graph_edges
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

CREATE TRIGGER graph_edge_building_update
BEFORE UPDATE ON graph_edges
BEGIN
    SELECT CASE WHEN COALESCE((SELECT status FROM projection_generations
        WHERE generation_id=NEW.generation_id
          AND creator_account_id=NEW.creator_account_id),'')!='building'
        THEN RAISE(ABORT,'projection_child_write_blocked') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM json_each(NEW.properties_json) AS property
        WHERE property.type IN ('array','object')
    ) THEN RAISE(ABORT,'graph_property_invalid') END;
    SELECT CASE WHEN NEW.properties_json != OLD.properties_json
        OR NEW.edge_id != OLD.edge_id OR NEW.source_id != OLD.source_id
        OR NEW.target_id != OLD.target_id OR NEW.relation != OLD.relation
        OR NEW.occurred_at IS NOT OLD.occurred_at OR NEW.sequence IS NOT OLD.sequence
        OR NEW.creator_account_id != OLD.creator_account_id
        OR NEW.generation_id != OLD.generation_id
        THEN RAISE(ABORT,'graph_edge_update_use_upsert') END;
END;

CREATE TRIGGER topic_taxonomy_pair_insert
BEFORE INSERT ON graph_nodes
WHEN NEW.kind='topic' AND (
       json_type(NEW.properties_json,'$.taxonomy_id')!='text'
    OR json_type(NEW.properties_json,'$.label')!='text'
    OR json_extract(NEW.properties_json,'$.label') != CASE json_extract(NEW.properties_json,'$.taxonomy_id')
        WHEN 'feedback' THEN 'Feedback' WHEN 'greeting' THEN 'Greeting'
        WHEN 'media' THEN 'Media' WHEN 'pricing' THEN 'Pricing'
        WHEN 'scheduling' THEN 'Scheduling' WHEN 'support' THEN 'Support' END
)
BEGIN
    SELECT RAISE(ABORT,'graph_property_invalid');
END;

CREATE TRIGGER projection_publication_epoch_identity_immutable
BEFORE UPDATE OF publication_epoch,scheduler_owner_id,scheduler_capability_digest,opened_at
ON projection_publication_epochs
BEGIN SELECT RAISE(ABORT,'projection_epoch_identity_immutable'); END;

CREATE TRIGGER projection_publication_epoch_monotonic
BEFORE UPDATE OF state ON projection_publication_epochs
WHEN NOT (OLD.state='open' AND NEW.state='revoked')
BEGIN SELECT RAISE(ABORT,'projection_epoch_transition_invalid'); END;

CREATE TRIGGER projection_publication_epoch_delete_blocked
BEFORE DELETE ON projection_publication_epochs
BEGIN SELECT RAISE(ABORT,'projection_epoch_delete_blocked'); END;

CREATE TRIGGER projection_generation_transition_monotonic
BEFORE UPDATE OF status ON projection_generations
WHEN OLD.status!=NEW.status AND NOT (
       (OLD.status='building' AND NEW.status IN ('validated','retired'))
    OR (OLD.status='validated' AND NEW.status IN ('activation_pending','retired'))
    OR (OLD.status='activation_pending' AND NEW.status IN ('active','retired'))
    OR (OLD.status='active' AND NEW.status='retired')
)
BEGIN SELECT RAISE(ABORT,'projection_generation_transition_invalid'); END;

CREATE TRIGGER projection_generation_identity_immutable
BEFORE UPDATE OF generation_id,creator_account_id,schema_version,build_version,
    canonical_revision,canonical_content_digest,canonical_high_water_json,
    pipeline_revision,pipeline_config_digest,pipeline_identity_digest,
    projection_digest,graph_digest,node_count,edge_count,
    expected_active_generation_id,expected_active_revision,publication_epoch,
    owner_id,owner_pid,owner_process_started_at,owner_instance_nonce,
    owner_capability_digest,started_at
ON projection_generations
WHEN OLD.status!='building'
BEGIN SELECT RAISE(ABORT,'projection_generation_identity_immutable'); END;

CREATE TRIGGER projection_generation_validation_complete
BEFORE UPDATE OF status ON projection_generations
WHEN OLD.status='building' AND NEW.status='validated' AND (
    NEW.projection_digest IS NULL OR NEW.graph_digest IS NULL OR NEW.validated_at IS NULL
)
BEGIN SELECT RAISE(ABORT,'projection_generation_validation_incomplete'); END;

CREATE TRIGGER projection_generation_activation_bound
BEFORE UPDATE OF status ON projection_generations
WHEN NEW.status IN ('activation_pending','active') AND (
    NEW.activation_intent_id IS NULL OR NEW.witness_sequence IS NULL
    OR NEW.publication_epoch IS NULL
)
BEGIN SELECT RAISE(ABORT,'projection_generation_activation_unbound'); END;

CREATE TRIGGER projection_generation_witness_immutable
BEFORE UPDATE OF activation_intent_id,witness_sequence ON projection_generations
WHEN NOT (
    (OLD.activation_intent_id IS NEW.activation_intent_id
     AND OLD.witness_sequence IS NEW.witness_sequence)
    OR (OLD.status='validated' AND NEW.status='activation_pending'
        AND OLD.activation_intent_id IS NULL AND OLD.witness_sequence IS NULL
        AND NEW.activation_intent_id IS NOT NULL AND NEW.witness_sequence IS NOT NULL)
)
BEGIN SELECT RAISE(ABORT,'projection_generation_witness_immutable'); END;

CREATE TRIGGER projection_generation_activation_epoch_open
BEFORE UPDATE OF status ON projection_generations
WHEN NEW.status='active' AND NOT EXISTS (
    SELECT 1 FROM projection_publication_epochs AS epoch
    WHERE epoch.publication_epoch=NEW.publication_epoch AND epoch.state='open'
)
BEGIN SELECT RAISE(ABORT,'projection_generation_epoch_revoked'); END;

CREATE TRIGGER projection_generation_delete_retired_only
BEFORE DELETE ON projection_generations WHEN OLD.status!='retired'
BEGIN SELECT RAISE(ABORT,'projection_generation_delete_blocked'); END;

CREATE TRIGGER projection_document_building_insert
BEFORE INSERT ON analytics_projections
WHEN COALESCE((SELECT status FROM projection_generations
    WHERE generation_id=NEW.generation_id
      AND creator_account_id=NEW.creator_account_id),'')!='building'
BEGIN SELECT RAISE(ABORT,'projection_child_write_blocked'); END;

CREATE TRIGGER graph_node_building_insert
BEFORE INSERT ON graph_nodes
WHEN COALESCE((SELECT status FROM projection_generations
    WHERE generation_id=NEW.generation_id
      AND creator_account_id=NEW.creator_account_id),'')!='building'
BEGIN SELECT RAISE(ABORT,'projection_child_write_blocked'); END;

CREATE TRIGGER graph_edge_building_insert
BEFORE INSERT ON graph_edges
WHEN COALESCE((SELECT status FROM projection_generations
    WHERE generation_id=NEW.generation_id
      AND creator_account_id=NEW.creator_account_id),'')!='building'
BEGIN SELECT RAISE(ABORT,'projection_child_write_blocked'); END;

CREATE TRIGGER graph_stats_building_insert
BEFORE INSERT ON graph_partition_stats
WHEN COALESCE((SELECT status FROM projection_generations
    WHERE generation_id=NEW.generation_id
      AND creator_account_id=NEW.creator_account_id),'')!='building'
BEGIN SELECT RAISE(ABORT,'projection_child_write_blocked'); END;

CREATE TRIGGER projection_document_update_blocked
BEFORE UPDATE ON analytics_projections
BEGIN SELECT RAISE(ABORT,'projection_child_update_blocked'); END;
CREATE TRIGGER graph_stats_update_blocked
BEFORE UPDATE ON graph_partition_stats
BEGIN SELECT RAISE(ABORT,'projection_child_update_blocked'); END;

CREATE TRIGGER projection_document_delete_guard
BEFORE DELETE ON analytics_projections
WHEN COALESCE((SELECT status FROM projection_generations
    WHERE generation_id=OLD.generation_id
      AND creator_account_id=OLD.creator_account_id),'') NOT IN ('','building','retired')
BEGIN SELECT RAISE(ABORT,'projection_child_delete_blocked'); END;
CREATE TRIGGER graph_node_delete_guard
BEFORE DELETE ON graph_nodes
WHEN COALESCE((SELECT status FROM projection_generations
    WHERE generation_id=OLD.generation_id
      AND creator_account_id=OLD.creator_account_id),'') NOT IN ('','building','retired')
BEGIN SELECT RAISE(ABORT,'projection_child_delete_blocked'); END;
CREATE TRIGGER graph_edge_delete_guard
BEFORE DELETE ON graph_edges
WHEN COALESCE((SELECT status FROM projection_generations
    WHERE generation_id=OLD.generation_id
      AND creator_account_id=OLD.creator_account_id),'') NOT IN ('','building','retired')
BEGIN SELECT RAISE(ABORT,'projection_child_delete_blocked'); END;
CREATE TRIGGER graph_stats_delete_guard
BEFORE DELETE ON graph_partition_stats
WHEN COALESCE((SELECT status FROM projection_generations
    WHERE generation_id=OLD.generation_id
      AND creator_account_id=OLD.creator_account_id),'') NOT IN ('','building','retired')
BEGIN SELECT RAISE(ABORT,'projection_child_delete_blocked'); END;

CREATE TRIGGER graph_metric_active_insert
BEFORE INSERT ON graph_algorithm_metrics
WHEN NOT EXISTS (
    SELECT 1 FROM projection_generations AS generation
    WHERE generation.generation_id=NEW.generation_id
      AND generation.creator_account_id=NEW.creator_account_id
      AND generation.status='active'
      AND generation.activation_intent_id=NEW.activation_intent_id
      AND generation.witness_sequence=NEW.witness_sequence
      AND generation.publication_epoch=NEW.publication_epoch
)
BEGIN SELECT RAISE(ABORT,'graph_metric_active_identity_mismatch'); END;
CREATE TRIGGER graph_metric_update_blocked
BEFORE UPDATE ON graph_algorithm_metrics
BEGIN SELECT RAISE(ABORT,'graph_metric_update_blocked'); END;
