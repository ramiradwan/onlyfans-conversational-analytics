"""Small immutable identities for already staged analytics artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.analytics.historical_derivation import PARTICIPANT_ANALYTICS_MAX_DAYS
from app.analytics.identity import CanonicalIdentity
from app.models.analytics import AnalyticsProjection


@dataclass(frozen=True, slots=True)
class GenerationReference:
    generation_id: str
    account_ref: str
    source_revision: int
    projection_generation: int
    canonical_content_digest: str
    pipeline_revision: str
    pipeline_config_digest: str
    pipeline_identity_digest: str
    projection_digest: str
    graph_digest: str
    publication_epoch: str
    retention_due_at: datetime | None

    @property
    def canonical_identity(self) -> CanonicalIdentity:
        return CanonicalIdentity(self.source_revision, self.canonical_content_digest)

    @classmethod
    def from_projection(cls, projection: AnalyticsProjection, generation_id: str,
                        publication_epoch: str) -> GenerationReference:
        first = min((item.sent_at for item in projection.message_enrichments), default=None)
        return cls(generation_id, projection.account_ref, projection.source_revision,
            projection.projection_generation, projection.canonical_content_digest,
            projection.pipeline_revision, projection.pipeline_config_digest,
            projection.pipeline_identity_digest, projection.projection_digest,
            projection.graph_digest, publication_epoch,
            first + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS) if first else None)


def check_reference(store, account_id: str, reference: GenerationReference) -> None:
    from app.analytics.errors import CanonicalRevisionChanged
    from app.analytics.opaque_refs import account_ref
    from app.analytics.query_contracts import utc_instant

    if account_ref(account_id) != reference.account_ref:
        raise CanonicalRevisionChanged()
    with store.database.read() as db:
        row = db.execute("""SELECT g.*,q.projection_generation,q.first_source,
            q.projection_digest AS document_digest FROM projection_generations g
            JOIN projection_query_metadata q USING(generation_id,creator_account_id)
            WHERE g.generation_id=? AND g.creator_account_id=?""",
            (reference.generation_id, reference.account_ref)).fetchone()
    if row is None or row["status"] not in {"validated", "activation_pending", "active"}:
        raise CanonicalRevisionChanged()
    if row["status"] == "active":
        witness = store.activation.get(reference.generation_id)
        if (not store._intent_matches(row, witness, require_completed=True)
                or witness.creator_account_id != account_id):
            raise CanonicalRevisionChanged()
    fields = {"source_revision": "canonical_revision", "account_ref": "creator_account_id",
        "projection_generation": "projection_generation", "canonical_content_digest": "canonical_content_digest",
        "pipeline_revision": "pipeline_revision", "pipeline_config_digest": "pipeline_config_digest",
        "pipeline_identity_digest": "pipeline_identity_digest", "projection_digest": "projection_digest",
        "graph_digest": "graph_digest", "publication_epoch": "publication_epoch"}
    if any(getattr(reference, field) != row[column] for field, column in fields.items()):
        raise CanonicalRevisionChanged()
    due = (utc_instant(row["first_source"]) + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS)
           if row["first_source"] else None)
    if due != reference.retention_due_at or row["document_digest"] != reference.projection_digest:
        raise CanonicalRevisionChanged()


def read_referenced_artifact(store, account_id: str, reference: GenerationReference):
    from app.analytics.errors import CanonicalRevisionChanged
    from app.models.analytics import RebuildArtifact

    check_reference(store, account_id, reference)
    if store.canonical_identity_reader(account_id) != reference.canonical_identity:
        raise CanonicalRevisionChanged()
    values = store._validate_persisted_generation(reference.generation_id, materialize_graph=True)
    if (values["projection_digest"] != reference.projection_digest
            or values["graph_digest"] != reference.graph_digest):
        raise CanonicalRevisionChanged()
    check_reference(store, account_id, reference)
    if store.canonical_identity_reader(account_id) != reference.canonical_identity:
        raise CanonicalRevisionChanged()
    return RebuildArtifact(projection=values["projection"], nodes=values["nodes"], edges=values["edges"])
