"""Neutral synthetic normalization identity and bounded record checks."""
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.nlp import normalization as n
from tools.generate_nlp_contracts import documents_normalization

pytestmark = pytest.mark.ci_tier("fast")


def policy(name):
    return dict(policy_id=name, revision="1", sha256="a" * 64)


def mapping():
    return n.OccurrenceMapping.model_validate_json(json.dumps(dict(
        occurrence=dict(source=dict(account_scope="account.1", source_ref="message.1",
                                    source_version="b" * 64, speaker_ref="speaker.1"),
                        category_id="category.example", span=dict(start=2, end=6)),
        extraction=dict(extraction_record_sha256="c" * 64, extraction_snapshot_sha256="d" * 64,
                        profile_sha256="e" * 64, component_id="component.1", component_sha256="f" * 64,
                        taxonomy=policy("taxonomy")),
        type_id="type.example", node_id=n.provisional_literal_node("namespace.1", "type.example", "echo").node_id,
        normalization_policy=policy("normalization"), reason="unreviewed_equivalence")))


def publication(revision=1):
    return n.NormalizationPublication.model_validate_json(json.dumps(dict(
        account_scope="account.1", extraction_snapshot_sha256="b" * 64,
        normalization_policy=policy("normalization"), ledger_revision=revision,
        ledger_sha256="c" * 64, source_validity_sha256="d" * 64,
        projection_policy=policy("projection"), completion_revision=1, availability_revision=1,
        mapping_batch_sha256=["e" * 64], membership_batch_sha256=["f" * 64])))


def test_literal_identity_is_nfc_provisional_and_separated():
    node = n.provisional_literal_node("namespace.1", "type.example", " café ")
    assert node.node_id == "cf2f834909ac31bf1fe121b3c214867c443a4e4409ebc42100319c87d1801fad"
    assert node == n.provisional_literal_node("namespace.1", "type.example", "cafe\u0301")
    assert node.resolution == "provisional"
    for namespace, type_id, surface in (("namespace.2", "type.example", "café"),
                                        ("namespace.1", "type.other", "café"),
                                        ("namespace.1", "type.example", "Café"),
                                        ("namespace.1", "type.example", "ca fé")):
        assert n.provisional_literal_node(namespace, type_id, surface).node_id != node.node_id
    assert n._digest("x", "ab", "c") != n._digest("x", "a", "bc")
    with pytest.raises(ValidationError):
        n.provisional_literal_node("namespace.1", "type.example", "x" * 257)
    with pytest.raises(ValidationError):
        n.provisional_literal_node("namespace.1", "type.example", "  ")
    with pytest.raises(ValidationError):
        node.literal_key = "changed"


def test_occurrences_are_separate_from_execution_and_repeats():
    first = mapping()
    changed = first.model_dump(mode="json")
    changed["extraction"]["profile_sha256"] = "0" * 64
    second = n.OccurrenceMapping.model_validate_json(json.dumps(changed))
    assert first.occurrence.identity_digest() == second.occurrence.identity_digest()
    assert first.identity_digest() != second.identity_digest()
    changed["occurrence"]["span"] = dict(start=7, end=11)
    repeated = n.OccurrenceMapping.model_validate_json(json.dumps(changed))
    assert first.node_id == repeated.node_id
    assert first.occurrence.identity_digest() != repeated.occurrence.identity_digest()
    changed["occurrence"]["source"]["source_version"] = "1" * 64
    assert n.OccurrenceMapping.model_validate_json(json.dumps(changed)).occurrence != repeated.occurrence


def test_mapping_roundtrip_closed_shape_and_bounds():
    record = mapping()
    assert n.decode_mapping(n.encode_mapping(record)) == record
    for change in ({"resolution": "accepted"}, {"candidate_node_ids": ["a" * 64] * 9},
                   {"candidate_node_ids": ["a" * 64] * 2}, {"unknown": "value"}):
        value = record.model_dump(mode="json") | change
        with pytest.raises(ValueError):
            n.decode_mapping(json.dumps(value).encode())
    with pytest.raises(ValueError):
        n.decode_mapping(b" " * (n.MAX_RECORD_BYTES + 1))
    with pytest.raises(ValueError):
        n.decode_mapping(b'{"schema_version":"x","schema_version":"y"}')


def test_group_ids_are_publication_scoped_without_manifest_feedback():
    membership = n.EquivalenceMembership(member_node_ids=("a" * 64, "b" * 64), decision_refs=("c" * 64,))
    first, second = publication(), publication(2)
    before = first.identity_digest()
    assert n.publication_group_id(first, membership) != n.publication_group_id(second, membership)
    assert first.identity_digest() == before
    with pytest.raises(ValidationError):
        n.EquivalenceMembership(member_node_ids=("b" * 64, "a" * 64), decision_refs=("c" * 64,))
    with pytest.raises(ValidationError):
        n.EquivalenceMembership(member_node_ids=("a" * 64,), decision_refs=())
    value = first.model_dump(mode="json")
    value["mapping_batch_sha256"] = [f"{i:064x}" for i in range(1024)]
    with pytest.raises(ValidationError, match="byte limit"):
        n.NormalizationPublication.model_validate_json(json.dumps(value))


def test_generated_normalization_schemas():
    root = Path(__file__).resolve().parents[1] / "shared/nlp/normalization/v1"
    for name, schema in documents_normalization().items():
        Draft202012Validator.check_schema(schema)
        assert json.loads((root / name).read_text()) == schema
    Draft202012Validator(documents_normalization()["OccurrenceMapping.schema.json"]).validate(mapping().model_dump(mode="json"))
