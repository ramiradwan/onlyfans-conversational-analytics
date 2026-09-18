"""Read publication metadata for questions answered from live canonical facts."""

from datetime import timedelta

from app.analytics.errors import ProjectionUnavailable
from app.analytics.historical_derivation import PARTICIPANT_ANALYTICS_MAX_DAYS
from app.analytics.opaque_refs import account_ref
from app.analytics.query_contracts import QuestionSnapshot, utc_instant
from app.analytics.query_sql import bounded_sql


def published_snapshot(store, account, identity, budget):
    partition = account_ref(account)
    budget.check()
    generation = store._active_generation_row(partition)
    if generation is None or generation["canonical_revision"] != identity.revision:
        raise ProjectionUnavailable()
    if generation["canonical_content_digest"] != identity.content_digest:
        raise ProjectionUnavailable(availability="building")
    intent = store.activation.get(generation["generation_id"])
    if (not store._intent_matches(generation, intent, require_completed=True)
            or intent.creator_account_id != account):
        raise ProjectionUnavailable()
    with store.database.read() as db, bounded_sql(db, budget):
        row = db.execute("""SELECT content_digest,
            json_extract(document_json,'$.projection_generation') AS sequence,
            json_extract(document_json,'$.creator_metrics.message_count') AS messages,
            json_extract(document_json,'$.creator_metrics.active_from') AS first_source
            FROM analytics_projections WHERE creator_account_id=? AND generation_id=?""",
            (partition, generation["generation_id"])).fetchone()
    if row is None or row["content_digest"] != generation["projection_digest"]:
        raise ProjectionUnavailable(availability="error")
    budget.check()
    snapshot = QuestionSnapshot(account_ref=partition, source_revision=identity.revision,
        projection_generation=row["sequence"], generation_id=generation["generation_id"],
        canonical_content_digest=generation["canonical_content_digest"],
        projection_digest=generation["projection_digest"],
        derived_at=generation["started_at"], source_message_count=row["messages"],
        retention_due_at=(utc_instant(row["first_source"])
            + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS)) if row["first_source"] else None)
    return snapshot, generation["pipeline_revision"], generation["pipeline_config_digest"]


def published_pricing(store, account, snapshot, references, budget):
    """Read stored topic relationships without classifying text during a query."""

    from app.analytics.graph_projection import stable_node_id
    from app.analytics.opaque_refs import topic_ref
    from app.models.analytics import GraphNodeKind

    partition = account_ref(account)
    if partition != snapshot.account_ref:
        raise ProjectionUnavailable()
    topic = stable_node_id(partition, GraphNodeKind.TOPIC, topic_ref(account, "pricing"))
    nodes = {stable_node_id(partition, GraphNodeKind.MESSAGE, ref): ref for ref in references}
    result = {}
    with store.database.read() as db, bounded_sql(db, budget):
        ids = list(nodes)
        for offset in range(0, len(ids), 64):
            batch = ids[offset:offset+64]
            budget.consume(len(batch))
            placeholders = ",".join("?" for _ in batch)
            rows = db.execute("""SELECT n.node_id, EXISTS (
                SELECT 1 FROM graph_edges AS e
                WHERE e.creator_account_id=n.creator_account_id AND e.generation_id=n.generation_id
                  AND e.source_id=n.node_id AND e.relation='mentions_topic' AND e.target_id=?) AS pricing
                FROM graph_nodes AS n WHERE n.creator_account_id=? AND n.generation_id=?
                  AND n.kind='message' AND n.node_id IN (""" + placeholders + ")",
                (topic, partition, snapshot.generation_id, *batch)).fetchall()
            for row in rows:
                result[nodes[row["node_id"]]] = "positive" if row["pricing"] else "negative"
    return result
