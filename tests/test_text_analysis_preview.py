"""Closed output decoding and counterexamples to retained preview claims."""
import copy
import json

import pytest

from app.analytics import nlp_preview as preview
from test_nlp_preview import case, frame, selected

pytestmark = pytest.mark.ci_tier("fast")


def encoded(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


@pytest.mark.parametrize("field", ["job_id", "contribution_id", "spec_sha256", "input_inventory_sha256", "profile_sha256", "selection_policy"])
def test_stored_intent_bindings_cannot_be_substituted(field):
    data, out = selected(); value = json.loads(preview.encode_preview(out))
    if field in ("job_id", "contribution_id"): value[field] = "00000000-0000-4000-8000-000000000001"
    elif field == "selection_policy": value[field]["revision"] = "other"
    else: value[field] = "a" * 64
    with pytest.raises(ValueError): preview.validate_preview_output(*data[:3], encoded(value))


@pytest.mark.parametrize("field", ["score", "findings", "record_sha256", "hidden_positive", "assessed_findings"])
def test_examination_metadata_cannot_retain_discarded_positive_details(field):
    _, out = selected(); value = json.loads(preview.encode_preview(out))
    if field == "assessed_findings": value["examination"][0]["assessment"] = field
    else: value["examination"][0][field] = "discarded"
    with pytest.raises(ValueError): preview.decode_preview(encoded(value))


@pytest.mark.parametrize("mutation", ["count", "duplicate", "span", "assessment", "category", "label", "source", "conversation", "ordering"])
def test_summary_and_evidence_claims_are_verified(mutation):
    data, out = selected(); value = json.loads(preview.encode_preview(out)); summary = value["summaries"][1]
    if mutation == "count": summary["occurrences"] += 1
    if mutation == "duplicate": summary["evidence"].append(copy.deepcopy(summary["evidence"][0]))
    if mutation == "span": summary["evidence"][0]["reference"]["span"]["end"] = 99
    if mutation == "assessment": summary["evidence"][0]["assessment"] = "partial"
    if mutation == "category": summary["label"]["category_id"] = "not-enabled"
    if mutation == "label": summary["label"]["display_label"] = "other"
    if mutation == "source": summary["evidence"][0]["source"]["speaker_ref"] = "other"
    if mutation == "conversation": summary["evidence"][0]["reference"]["conversation_ref"] = "c1:" + "a" * 64
    if mutation == "ordering": summary["evidence"].reverse()
    with pytest.raises(ValueError): preview.validate_preview_output(*data[:3], encoded(value))


def test_plus_n_requires_resolved_assessment_not_just_complete_examination():
    data = case(); acc = preview.PreviewAccumulator(*data[:3])
    acc.add(0, data[3][0], frame(data, 0, outcomes={"message_topics": "uncertain"}))
    out = acc.finish(); value = json.loads(preview.encode_preview(out))
    # The mention kind is resolved; make its examination uncertain while preserving complete source coverage.
    value["examination"][1]["assessment"] = "uncertain"
    value["examination"][1]["reason"] = "below_threshold"
    value["tasks"][1].update(assessed=0, uncertain=1)
    with pytest.raises(ValueError): preview.decode_preview(encoded(value))


@pytest.mark.parametrize("mutation", ["missing", "extra", "reordered", "duplicate"])
def test_examination_covers_exact_frozen_members_and_tasks(mutation):
    data, out = selected(); value = json.loads(preview.encode_preview(out))
    if mutation == "missing": value["examination"].pop()
    if mutation == "extra": value["examination"].append(copy.deepcopy(value["examination"][0]))
    if mutation == "reordered": value["examination"].reverse()
    if mutation == "duplicate": value["examination"][1] = value["examination"][0]
    with pytest.raises(ValueError): preview.decode_preview(encoded(value))


@pytest.mark.parametrize("raw", [b"", b"null", b"[]", b"\xff", b" " * 1_048_577],
                         ids=["empty", "null", "array", "utf8", "over-limit"])
def test_decoder_rejects_wrong_framing_without_interpreting_candidates(raw):
    with pytest.raises(ValueError): preview.decode_preview(raw)


def test_duplicate_noncanonical_and_model_construct_bypasses_are_rejected():
    data, out = selected(); raw = preview.encode_preview(out)
    with pytest.raises(ValueError): preview.decode_preview(raw.replace(b'"source_count":1', b'"source_count":1,"source_count":1'))
    with pytest.raises(ValueError): preview.decode_preview(raw + b" ")
    with pytest.raises(ValueError): preview.decode_preview(bytearray(raw))
    invalid = out.model_copy(update={"source_count": True})
    with pytest.raises(ValueError): preview.encode_preview(invalid)
    invalid = data[0].model_copy(update={"algorithm_profile_sha256": "a" * 64})
    with pytest.raises(ValueError): preview.validate_preview_output(invalid, data[1], data[2], raw)
    value = json.loads(raw); value["source_count"] = 1.0
    with pytest.raises(ValueError): preview.decode_preview(encoded(value))


def test_zero_source_inventory_is_a_complete_empty_disposition():
    data = case([]); acc = preview.PreviewAccumulator(*data[:3])
    result = acc.finish()
    assert result.disposition == "empty"
    assert preview.validate_preview_no_output(*data[:3], result) == result
    with pytest.raises(ValueError): preview.encode_preview(result)


def test_assessed_examination_merges_both_positive_and_no_match_outcomes():
    data = case(); values = []
    for topics, mentions in [((), ()), (("topic.a",), (("concept.a", 2, 6),))]:
        acc = preview.PreviewAccumulator(*data[:3]); acc.add(0, data[3][0], frame(data, 0, topics=topics, mentions=mentions))
        values.append(tuple(e.model_dump() for e in acc._exams.values()))
    assert values[0] == values[1]
