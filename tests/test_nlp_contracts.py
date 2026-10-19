"""Synthetic protocol failures and Unicode/source identity boundaries."""
import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from jsonschema import Draft202012Validator
from app.nlp.contracts import (EnrichmentSnapshot, decode_request,
                               validate_response, MAX_FRAME_BYTES)
from tools.generate_nlp_contracts import documents

pytestmark = pytest.mark.ci_tier("fast")
ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "shared/nlp/v1/fixtures"

def fixture(name):
    return json.loads((FIXTURES / (name + ".json")).read_text("utf-8"))

def frame(value):
    return json.dumps(value, ensure_ascii=False).encode()

def test_generated_schemas_and_shared_fixtures():
    for name, schema in documents().items():
        assert json.loads((ROOT / "shared/nlp/v1" / name).read_text()) == schema
        Draft202012Validator.check_schema(schema)
    for model, name in (("WorkerRequest", "request"), ("WorkerResponse", "response")):
        Draft202012Validator(documents()[model + ".schema.json"]).validate(fixture(name))

def test_unicode_repeated_mentions_are_distinct_original_codepoint_spans():
    request = decode_request(frame(fixture("request")))
    response = validate_response(request, frame(fixture("response")))
    text = request.items[0].text
    spans = [m.span for m in response.items[0].mentions.mentions]
    assert [text[s.start:s.end] for s in spans] == ["echo", "echo"]
    assert spans[0] != spans[1]

@pytest.mark.parametrize("fault", ["missing", "duplicate", "stale", "source_length", "span", "no_match", "cross_task", "extra"])
def test_brain_rejects_malformed_or_stale_worker_results(fault):
    request = decode_request(frame(fixture("request")))
    response = fixture("response")
    item = response["items"][0]
    if fault == "missing": response["items"] = []
    if fault == "duplicate": response["items"].append(copy.deepcopy(item))
    if fault == "stale": response["execution"]["pack_sha256"] = "f" * 64
    if fault == "source_length": item["topics"]["coverage"]["source_codepoints"] = 12
    if fault == "span": item["mentions"]["mentions"][0]["span"]["end"] = 12
    if fault == "no_match": item["mentions"]["outcome"] = "assessed_no_match"
    if fault == "cross_task": item["topics"]["mentions"] = item["mentions"]["mentions"]
    if fault == "extra": response["database_path"] = "forbidden"
    with pytest.raises((ValueError, ValidationError)):
        validate_response(request, frame(response))

def test_independent_failure_and_partial_do_not_become_no_match():
    request = decode_request(frame(fixture("request")))
    response = fixture("response")
    task = response["items"][0]["mentions"]
    task.update(outcome="failed", reason="worker_failure", mentions=[])
    task["coverage"].update(examined=[], excluded=[dict(start=0, end=11)])
    assert validate_response(request, frame(response)).items[0].topics.outcome == "assessed_no_match"
    task.update(outcome="partial", reason="limit_reached", mentions=[dict(concept_id="concept.synthetic", span=dict(start=2,end=6),score=.95)])
    task["coverage"].update(examined=[dict(start=0,end=6)],excluded=[dict(start=6,end=11)])
    validate_response(request, frame(response))
    task["outcome"] = "assessed_no_match"
    with pytest.raises(ValueError): validate_response(request, frame(response))

def test_frame_bound_checked_before_decode():
    with pytest.raises(ValueError, match="byte limit"):
        decode_request(b" " * (MAX_FRAME_BYTES + 1))

def test_completion_revision_changes_immutable_snapshot_identity():
    execution = decode_request(frame(fixture("request"))).execution
    value = EnrichmentSnapshot(account_scope="opaque", canonical_revision=1, completion_revision=1, availability_revision=1,
                               execution=execution,findings_sha256="a"*64,source_validity_sha256="b"*64)
    later = value.model_copy(update={"completion_revision": 2})
    assert later.identity_digest() != value.identity_digest()
    with pytest.raises(ValidationError): value.completion_revision = 2


@pytest.mark.parametrize("claim", ["qualified_per_category", "unqualified", "calibrated"])
def test_worker_cannot_assert_quality_approval_or_confidence_calibration(claim):
    request = decode_request(frame(fixture("request")))
    response = fixture("response")
    response["items"][0]["calibration_status"] = claim
    with pytest.raises(ValidationError, match="calibration_status"):
        validate_response(request, frame(response))


@pytest.mark.parametrize("fault", ["duplicate", "unknown", "missing"])
def test_response_matches_every_admitted_item_exactly_once(fault):
    value = fixture("request")
    value["items"].append(dict(request_item_id="item.2", text=value["items"][0]["text"]))
    request = decode_request(frame(value))
    response = fixture("response")
    second = copy.deepcopy(response["items"][0])
    second["request_item_id"] = "item.2"
    response["items"].append(second)
    assert len(validate_response(request, frame(response)).items) == 2
    if fault == "duplicate": second["request_item_id"] = "item.1"
    if fault == "unknown": second["request_item_id"] = "item.3"
    if fault == "missing": response["items"].pop()
    with pytest.raises(ValueError, match="missing, duplicate or unknown"):
        validate_response(request, frame(response))


def test_utf16_length_cannot_replace_original_codepoint_length():
    request = decode_request(frame(fixture("request")))
    response = fixture("response")
    # Both task partitions are internally valid, but the astral character counts once.
    for task in ("topics", "mentions"):
        coverage = response["items"][0][task]["coverage"]
        coverage.update(source_codepoints=12, examined=[dict(start=0, end=12)])
    with pytest.raises(ValueError, match="source codepoint length mismatch"):
        validate_response(request, frame(response))


def test_utf16_span_beyond_original_codepoints_is_rejected():
    request = decode_request(frame(fixture("request")))
    response = fixture("response")
    response["items"][0]["mentions"]["mentions"][-1]["span"] = dict(start=8, end=12)
    with pytest.raises(ValueError, match="examined original source"):
        validate_response(request, frame(response))


def test_invalid_unicode_input_is_rejected():
    value = fixture("request")
    value["items"][0]["text"] = "\ud800"
    with pytest.raises(ValidationError):
        decode_request(json.dumps(value).encode())
