"""Carry an existing enrichment proof through protected generation cleanup."""

from dataclasses import dataclass, field, replace
import hashlib
import json

from app.analytics.conversation_enrichment_units import ConversationEnrichmentProof
from app.analytics.validation_receipt import content_stamp, generation_binding


GUARD_NAMES = (
    'conversation_enrichment_units_immutable',
    'conversation_enrichment_units_replace_blocked',
    'conversation_enrichment_units_referenced',
    'conversation_enrichment_refs_building',
    'conversation_enrichment_refs_immutable',
    'conversation_enrichment_refs_delete',
    'conversation_enrichment_refs_replace_blocked',
    'projection_generation_transition_monotonic',
    'projection_generation_identity_immutable',
    'projection_generation_delete_retired_only',
)
GUARD_DIGEST = "256dbd8dfa5dfac13911a2683fa9cdc665bd45fa3b03f251fd1dc687465cc85b"
GUARD_DIGESTS = {
    17: GUARD_DIGEST,
    18: GUARD_DIGEST,
    19: "74e6d61917e9f0b644d2d85fcd2166689280849c7e913c7042d7556f8c6f8ba0",
}


def _guards_match(connection):
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        return False
    rows = connection.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger'"
    )
    try:
        signatures = {row[0]: hashlib.sha256(row[1].encode()).hexdigest() for row in rows}
    finally:
        rows.close()
    encoded = json.dumps(signatures, sort_keys=True, separators=(",", ":")).encode()
    expected = GUARD_DIGESTS.get(connection.execute("PRAGMA user_version").fetchone()[0])
    return expected is not None and hashlib.sha256(encoded).hexdigest() == expected


@dataclass(frozen=True, slots=True)
class EnrichmentProofTransition:
    proof: ConversationEnrichmentProof
    account_ref: str
    references: tuple[tuple, ...]
    connection: object = field(repr=False, compare=False)


def _references(connection, generation_id, expected_count):
    rows = connection.execute(
        """SELECT r.creator_account_id,r.conversation_ref,r.unit_id,r.ordinal,
                  r.input_digest,r.config_digest,r.retention_cutoff,r.expires_at,
                  u.unit_id
           FROM conversation_enrichment_refs r
           LEFT JOIN conversation_enrichment_units u
             USING(creator_account_id,unit_id)
           WHERE r.generation_id=? ORDER BY r.conversation_ref""",
        (generation_id,),
    )
    try:
        return tuple(tuple(row) for row in rows.fetchmany(expected_count + 1))
    finally:
        rows.close()


def capture_transition(connection, generation, proof):
    """Capture only a currently valid proof inside the caller's write transaction."""
    if not connection.in_transaction:
        raise ValueError("enrichment_transition_requires_transaction")
    if generation is None or proof is None:
        return None
    if (generation["status"] not in ("active", "activation_pending")
            or proof.generation_id != generation["generation_id"]
            or proof.binding != generation_binding(generation)
            or proof.stamp != content_stamp(connection)
            or not _guards_match(connection)):
        return None
    account = generation["creator_account_id"]
    expected = tuple(sorted(
        (header.account_ref, header.conversation_ref, header.unit_id)
        for header in proof.headers
    ))
    if not expected or any(value[0] != account for value in expected):
        return None
    references = _references(connection, proof.generation_id, len(expected))
    if (tuple(row[:3] for row in references) != expected
            or any(row[2] != row[8] for row in references)):
        return None
    return EnrichmentProofTransition(proof, account, references, connection)


def finish_transition(connection, transition):
    """Return a proof to install only after the enclosing transaction commits."""
    if not connection.in_transaction:
        raise ValueError("enrichment_transition_requires_transaction")
    if transition is None or transition.connection is not connection:
        return None
    proof = transition.proof
    generation = connection.execute(
        """SELECT * FROM projection_generations
           WHERE generation_id=? AND creator_account_id=? AND status='active'""",
        (proof.generation_id, transition.account_ref),
    ).fetchone()
    stamp = content_stamp(connection)
    if (generation is None or proof.binding != generation_binding(generation)
            or stamp is None or stamp[:3] != proof.stamp[:3]
            or stamp[3] < proof.stamp[3] or not _guards_match(connection)):
        return None
    # With the same schema, active references and their selected content cannot
    # be replaced or updated. Check the complete selection, including order and
    # retention bounds, without opening immutable payloads again.
    if _references(connection, proof.generation_id, len(transition.references)) != transition.references:
        return None
    return replace(proof, stamp=tuple(stamp))
