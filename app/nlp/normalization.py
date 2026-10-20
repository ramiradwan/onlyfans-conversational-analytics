"""Bounded Brain-side normalization contracts; no storage or alias resolution."""
from __future__ import annotations

import hashlib
import unicodedata
from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.nlp.contracts import Contract, Digest, Identifier, NlpBoundaryError, SourceBinding, Span
from app.nlp.profiles import Descriptor, PolicyIdentity, canonical_bytes

MAX_RECORD_BYTES = 65_536
KEY_SCHEMA = "nlp-literal-key.v1"
References = Annotated[tuple[Digest, ...], Field(max_length=16)]


def _digest(domain: str, *parts: str) -> str:
    encoded = bytearray()
    for part in (domain, *parts):
        value = part.encode("utf-8")
        encoded.extend(len(value).to_bytes(8, "big"))
        encoded.extend(value)
    return hashlib.sha256(encoded).hexdigest()


class BoundedDescriptor(Descriptor):
    @model_validator(mode="after")
    def byte_bound(self):
        if len(canonical_bytes(self)) > MAX_RECORD_BYTES:
            raise ValueError("normalization record byte limit")
        return self


class LiteralNode(BoundedDescriptor):
    schema_version: Literal["nlp-literal-node.v1"] = "nlp-literal-node.v1"
    key_schema: Literal["nlp-literal-key.v1"] = KEY_SCHEMA
    account_namespace: Identifier
    type_id: Identifier
    literal_key: Annotated[str, Field(min_length=1, max_length=256)]
    node_id: Digest
    resolution: Literal["provisional"] = "provisional"

    @model_validator(mode="after")
    def identity(self):
        if self.literal_key != unicodedata.normalize("NFC", self.literal_key.strip()):
            raise ValueError("noncanonical literal key")
        expected = _digest("nlp-literal-node.v1", self.account_namespace,
                           self.key_schema, self.type_id, self.literal_key)
        if self.node_id != expected:
            raise ValueError("literal node identity mismatch")
        return self


def provisional_literal_node(account_namespace: str, type_id: str, surface: str) -> LiteralNode:
    """Namespace is caller-retained account state, never derived from the text."""
    key = unicodedata.normalize("NFC", surface.strip())
    return LiteralNode(account_namespace=account_namespace, type_id=type_id, literal_key=key,
                       node_id=_digest("nlp-literal-node.v1", account_namespace, KEY_SCHEMA, type_id, key))


class OccurrenceAnchor(Descriptor):
    """Source occurrence identity deliberately excludes execution identity."""
    source: SourceBinding
    category_id: Identifier
    span: Span

    @model_validator(mode="after")
    def source_bound(self):
        if self.span.end > 131_072:
            raise ValueError("occurrence beyond source ceiling")
        return self


class ExtractionProvenance(Contract):
    task_contract: Literal["concept_mentions.v1"] = "concept_mentions.v1"
    extraction_record_sha256: Digest
    extraction_snapshot_sha256: Digest
    profile_sha256: Digest
    component_id: Identifier
    component_sha256: Digest
    taxonomy: PolicyIdentity


class OccurrenceMapping(BoundedDescriptor):
    schema_version: Literal["nlp-occurrence-mapping.v1"] = "nlp-occurrence-mapping.v1"
    occurrence: OccurrenceAnchor
    extraction: ExtractionProvenance
    type_id: Identifier
    node_id: Digest
    normalization_policy: PolicyIdentity
    resolution: Literal["provisional"] = "provisional"
    provenance: Literal["literal_key"] = "literal_key"
    reason: Literal["unreviewed_equivalence", "equivalence_unresolved"]
    candidate_node_ids: Annotated[tuple[Digest, ...], Field(max_length=8)] = ()
    decision_refs: References = ()

    @model_validator(mode="after")
    def unique_refs(self):
        for refs in (self.candidate_node_ids, self.decision_refs):
            if len(set(refs)) != len(refs):
                raise ValueError("duplicate normalization reference")
        return self


class EquivalenceMembership(Contract):
    """Reviewed membership input only; this contract performs no merge."""
    member_node_ids: Annotated[tuple[Digest, ...], Field(min_length=1, max_length=256)]
    decision_refs: References

    @model_validator(mode="after")
    def ordered_refs(self):
        if not self.decision_refs:
            raise ValueError("equivalence requires decision provenance")
        for refs in (self.member_node_ids, self.decision_refs):
            if tuple(sorted(set(refs))) != refs:
                raise ValueError("membership references must be sorted and unique")
        return self


class NormalizationPublication(BoundedDescriptor):
    """Manifest inputs exclude all derived group IDs to avoid circular identity."""
    schema_version: Literal["nlp-normalization-publication.v1"] = "nlp-normalization-publication.v1"
    account_scope: Identifier
    extraction_snapshot_sha256: Digest
    normalization_policy: PolicyIdentity
    ledger_revision: Annotated[int, Field(ge=0, le=9_007_199_254_740_991)]
    ledger_sha256: Digest
    source_validity_sha256: Digest
    projection_policy: PolicyIdentity
    completion_revision: Annotated[int, Field(ge=0, le=9_007_199_254_740_991)]
    availability_revision: Annotated[int, Field(ge=0, le=9_007_199_254_740_991)]
    mapping_batch_sha256: Annotated[tuple[Digest, ...], Field(max_length=1024)]
    membership_batch_sha256: Annotated[tuple[Digest, ...], Field(max_length=1024)]

    @model_validator(mode="after")
    def ordered_batches(self):
        for refs in (self.mapping_batch_sha256, self.membership_batch_sha256):
            if tuple(sorted(set(refs))) != refs:
                raise ValueError("publication references must be sorted and unique")
        return self


def publication_group_id(publication: NormalizationPublication, membership: EquivalenceMembership) -> str:
    """Caller must verify membership belongs to the pinned manifest batch."""
    return _digest("nlp-publication-group.v1", publication.identity_digest(),
                   canonical_bytes(membership).decode("ascii"))


def encode_mapping(record: OccurrenceMapping) -> bytes:
    return canonical_bytes(record)


def decode_mapping(frame: bytes) -> OccurrenceMapping:
    if len(frame) > MAX_RECORD_BYTES:
        raise NlpBoundaryError("nlp_normalization_frame_too_large")
    try:
        import json
        from app.nlp.v2 import _unique_object
        json.loads(frame, object_pairs_hook=_unique_object)
        return OccurrenceMapping.model_validate_json(frame)
    except (ValueError, RecursionError, OverflowError):
        raise NlpBoundaryError("nlp_invalid_normalization_record") from None
