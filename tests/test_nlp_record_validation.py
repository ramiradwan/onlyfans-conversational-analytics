"""Invented evidence snapshots and source-bound records, without live callers."""
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import traceback
import warnings

import pytest
from pydantic import ValidationError

from app.analytics.evidence_contracts import EvidenceLocation, EvidenceMessage, MAX_EVIDENCE_TEXT_CHARS
from app.analytics.nlp_record_validation import validate_source_bound_record
from app.analytics.opaque_refs import participant_ref
from app.nlp import v2
from app.nlp.contracts import NlpBoundaryError, SourceBinding
from app.nlp.profiles import AnalysisProfile

pytestmark = pytest.mark.ci_tier("fast")
FIXTURES = Path(__file__).resolve().parents[1] / "shared/nlp/v2/fixtures"
NOW = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)


def source(**changes):
    values = dict(
        account_id="example-account", location=EvidenceLocation(conversation_id="example-chat", message_id="example-message"),
        source_revision=7, text="😀 echo echo", sent_at=NOW, direction="inbound", sender_id="example-sender",
        upstream_updated_at=None, content_hash="example-content", stream_epoch=2, source_sequence=3,
        source_state_token="example-state",
    )
    return EvidenceMessage(**{**values, **changes})


def binding(message):
    reference = message.reference()
    return SourceBinding(account_scope=reference.account_ref, source_ref=reference.message_ref,
                         source_version=reference.source_version_digest[7:],
                         speaker_ref=participant_ref(message.account_id, message.sender_id))


def case(kind="joint", message=None):
    message = source() if message is None else message
    profile = AnalysisProfile.model_validate_json((FIXTURES / (kind + "-profile.json")).read_bytes())
    tasks = []
    expected_source = binding(message).model_dump(mode="json")
    for component in profile.components:
        response = json.loads((FIXTURES / f"{kind}-{component.component_id}-response.json").read_bytes())
        for result in response["items"][0]["tasks"]:
            result["text_sha256"] = hashlib.sha256(message.text.encode()).hexdigest()
            result["payload"]["coverage"].update(source_codepoints=len(message.text),
                                                examined=[dict(start=0, end=len(message.text))], excluded=[])
            tasks.append(dict(source=copy.deepcopy(expected_source), profile_sha256=profile.identity_digest(), result=result))
    document = dict(schema_version="nlp-result.v2", source=expected_source,
                    profile=profile.model_dump(mode="json"), analyzed_at="2026-01-02T12:01:00Z", tasks=tasks)
    return profile, message, document


def frame(document):
    return json.dumps(document, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def payload(document, task):
    return next(row["result"]["payload"] for row in document["tasks"] if row["result"]["payload"]["task"] == task)


def rejected(profile, message, raw):
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        with pytest.raises(NlpBoundaryError) as caught:
            validate_source_bound_record(profile, message, raw)
    assert str(caught.value) == "nlp_invalid_source_record"
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__
    assert not recorded
    return caught.value


@pytest.mark.parametrize("kind", ["joint", "split"])
def test_both_tasks_bind_exact_original_text_and_existing_evidence_identity(kind):
    profile, message, document = case(kind)
    result = validate_source_bound_record(profile, message, frame(document))
    assert result == v2.EnrichmentRecord.model_validate_json(frame(document))
    assert result.source == binding(message)
    assert result.source.source_version == message.reference().source_version_digest[7:]
    assert result.source.source_version != hashlib.sha256(message.text.encode()).hexdigest()
    mentions = next(task.result.payload for task in result.tasks if task.result.payload.task == "concept_mentions")
    assert [message.text[finding.span.start:finding.span.end] for finding in mentions.findings] == ["echo", "echo"]
    assert mentions.findings[0].span != mentions.findings[1].span
    with pytest.raises(ValidationError):
        result.analyzed_at = "2026-01-02T12:02:00Z"


@pytest.mark.parametrize("change", ["hash", "length"])
def test_internally_coherent_record_is_not_enough_without_original_body_checks(change):
    profile, message, document = case()
    for row in document["tasks"]:
        if change == "hash":
            row["result"]["text_sha256"] = "f" * 64
        else:
            row["result"]["payload"]["coverage"].update(source_codepoints=12, examined=[dict(start=0, end=12)])
    raw = frame(document)
    assert v2.validate_record(profile, binding(message), raw)
    rejected(profile, message, raw)


@pytest.mark.parametrize("field,value", [
    ("text", "😀 different"), ("account_id", "other-account"),
    ("sender_id", "other-sender"), ("direction", "outbound"),
    ("sent_at", datetime(2026, 1, 2, 11, tzinfo=timezone.utc)),
    ("upstream_updated_at", NOW), ("content_hash", "other-content"),
    ("stream_epoch", 3), ("source_sequence", 4),
    ("location", EvidenceLocation(conversation_id="other-chat", message_id="example-message")),
    ("location", EvidenceLocation(conversation_id="example-chat", message_id="other-message")),
])
def test_changed_canonical_fields_invalidate_the_exact_existing_version(field, value):
    profile, message, document = case()
    changed = message.model_copy(update={field: value})
    assert changed.reference().source_version_digest != message.reference().source_version_digest
    rejected(profile, changed, frame(document))


def test_global_revision_and_state_token_do_not_change_message_version_or_imply_live_authorization():
    profile, message, document = case()
    changed = message.model_copy(update={"source_revision": 8, "source_state_token": "other-state"})
    assert changed.reference().source_version_digest == message.reference().source_version_digest
    assert validate_source_bound_record(profile, changed, frame(document)).source == binding(message)


@pytest.mark.parametrize("direction", ["inbound", "outbound"])
def test_speaker_binding_uses_actual_sender_in_both_directions(direction):
    message = source(direction=direction, sender_id="actual-sender")
    profile, message, document = case(message=message)
    result = validate_source_bound_record(profile, message, frame(document))
    assert result.source.speaker_ref == participant_ref(message.account_id, "actual-sender")
    assert result.source.speaker_ref != participant_ref(message.account_id, "remote-participant")


@pytest.mark.parametrize("task", ["message_topics", "concept_mentions"])
@pytest.mark.parametrize("outcome", ["assessed_findings", "assessed_no_match", "uncertain", "partial", "failed", "unsupported", "unprocessed"])
def test_independent_task_outcomes_are_retained_without_relabeling(task, outcome):
    profile, message, document = case("split")
    other_task = "concept_mentions" if task == "message_topics" else "message_topics"
    other = copy.deepcopy(payload(document, other_task))
    selected = payload(document, task)
    selected.update(outcome=outcome, findings=[])
    selected["reason"] = {
        "assessed_findings": "none", "assessed_no_match": "none", "uncertain": "below_threshold",
        "partial": "limit_reached", "failed": "worker_failure", "unsupported": "unsupported_input",
        "unprocessed": "dependency_unavailable",
    }[outcome]
    if outcome in ("assessed_findings", "partial"):
        selected["findings"] = ([dict(topic_id="topic.synthetic")] if task == "message_topics" else
                                [dict(concept_id="concept.synthetic", span=dict(start=2, end=6))])
    if outcome == "partial":
        selected["coverage"].update(examined=[dict(start=0, end=6)], excluded=[dict(start=6, end=11)], limits_reached=["candidates"])
    elif outcome in ("failed", "unsupported", "unprocessed"):
        selected["coverage"].update(examined=[], excluded=[dict(start=0, end=11)])
    result = validate_source_bound_record(profile, message, frame(document))
    assert next(row.result.payload for row in result.tasks if row.result.payload.task == task).outcome == outcome
    assert next(row.result.payload.model_dump(mode="json") for row in result.tasks if row.result.payload.task == other_task) == other


@pytest.mark.parametrize("fault", ["missing_task", "duplicate_task", "component", "category", "span", "duplicate_span", "coverage", "source", "speaker", "profile", "score"])
def test_existing_v2_task_profile_and_span_rejections_are_preserved(fault):
    profile, message, document = case()
    mentions = payload(document, "concept_mentions")
    if fault == "missing_task":
        document["tasks"].pop()
    elif fault == "duplicate_task":
        document["tasks"][1] = copy.deepcopy(document["tasks"][0])
    elif fault == "component":
        document["tasks"][0]["result"]["component_sha256"] = "f" * 64
    elif fault == "category":
        mentions["findings"][0]["concept_id"] = "concept.other"
    elif fault == "span":
        mentions["findings"][0]["span"]["end"] = 12
    elif fault == "duplicate_span":
        mentions["findings"].append(copy.deepcopy(mentions["findings"][0]))
    elif fault == "coverage":
        mentions["coverage"]["examined"] = [dict(start=1, end=11)]
    elif fault == "source":
        document["source"]["source_version"] = "f" * 64
        for row in document["tasks"]:
            row["source"] = copy.deepcopy(document["source"])
    elif fault == "speaker":
        document["source"]["speaker_ref"] = participant_ref(message.account_id, "other-sender")
        for row in document["tasks"]:
            row["source"] = copy.deepcopy(document["source"])
    elif fault == "profile":
        document["tasks"][0]["profile_sha256"] = "f" * 64
    else:
        mentions["findings"][0]["score"] = dict(kind="logit", value=1.5)
    rejected(profile, message, frame(document))


@pytest.mark.parametrize("fault", ["empty", "nested_missing", "nested_type", "text_type", "text_surrogate", "id_surrogate", "token_surrogate", "oversized"])
def test_unchecked_source_objects_fail_without_warnings_or_payload_errors(fault):
    profile, message, document = case()
    if fault == "empty":
        message = EvidenceMessage.model_construct()
    elif fault == "nested_missing":
        message = message.model_copy(update={"location": EvidenceLocation.model_construct()})
    elif fault == "nested_type":
        message = message.model_copy(update={"location": "EXAMPLE_INVALID_MARKER"})
    elif fault == "text_type":
        message = message.model_copy(update={"text": 123})
    elif fault == "text_surrogate":
        message = message.model_copy(update={"text": "\ud800"})
    elif fault == "id_surrogate":
        message = message.model_copy(update={"sender_id": "\udfff"})
    elif fault == "token_surrogate":
        message = message.model_copy(update={"source_state_token": "\ud800"})
    else:
        message = message.model_copy(update={"text": "x" * (MAX_EVIDENCE_TEXT_CHARS + 1)})
    error = rejected(profile, message, frame(document))
    assert "EXAMPLE_INVALID_MARKER" not in "".join(traceback.format_exception(error))


@pytest.mark.parametrize("fault", ["empty", "nested_missing", "nested_type", "nested_surrogate"])
def test_unchecked_profile_objects_are_fully_revalidated(fault):
    profile, message, document = case()
    if fault == "empty":
        profile = AnalysisProfile.model_construct()
    else:
        component = profile.components[0]
        if fault == "nested_missing":
            component = component.model_copy(update={"engine": type(component.engine).model_construct()})
        elif fault == "nested_type":
            component = component.model_copy(update={"engine": "EXAMPLE_INVALID_MARKER"})
        else:
            label = component.executed_labels[0].model_copy(update={"description": "\ud800"})
            component = component.model_copy(update={"executed_labels": (label,) + component.executed_labels[1:]})
        profile = profile.model_copy(update={"components": (component,)})
    rejected(profile, message, frame(document))


@pytest.mark.parametrize("fault", ["duplicate", "nested_duplicate", "malformed", "utf8", "deep", "null", "bytearray", "string"])
def test_malformed_records_have_one_non_disclosing_error(fault):
    profile, message, document = case()
    raw = frame(document)
    if fault == "duplicate":
        raw = raw[:-1] + b',"analyzed_at":"2026-01-02T12:01:00Z"}'
    elif fault == "nested_duplicate":
        raw = raw.replace(b'"start":2', b'"start":2,"start":2', 1)
    elif fault == "malformed":
        raw = b'{"EXAMPLE_INVALID_MARKER":'
    elif fault == "utf8":
        raw = b'"\xff"'
    elif fault == "deep":
        raw = b"[" * 1200 + b"0" + b"]" * 1200
    elif fault == "null":
        raw = b"null"
    elif fault == "bytearray":
        raw = bytearray(raw)
    else:
        raw = raw.decode()
    error = rejected(profile, message, raw)
    assert "EXAMPLE_INVALID_MARKER" not in "".join(traceback.format_exception(error))


def test_record_size_bound_precedes_object_processing_and_exact_bound_remains_valid(monkeypatch):
    profile, message, document = case()
    raw = frame(document)
    exact = raw + b" " * (v2.MAX_RECORD_BYTES - len(raw))
    assert validate_source_bound_record(profile, message, exact)

    def forbidden(*args, **kwargs):
        pytest.fail("oversized record reached source processing")

    monkeypatch.setattr(EvidenceMessage, "reference", forbidden)
    rejected(profile, message, exact + b" ")


def test_existing_source_codepoint_limit_accepts_astral_text_without_utf16_counting():
    message = source(text="😀" * MAX_EVIDENCE_TEXT_CHARS)
    profile, message, document = case(message=message)
    result = validate_source_bound_record(profile, message, frame(document))
    assert all(row.result.payload.coverage.source_codepoints == MAX_EVIDENCE_TEXT_CHARS for row in result.tasks)
    assert len(message.text.encode("utf-16-le")) // 2 == MAX_EVIDENCE_TEXT_CHARS * 2


def test_success_does_not_copy_source_body_ids_or_state_metadata():
    marker = "EXAMPLE_SOURCE_MARKER_3981"
    message = source(account_id=marker, sender_id=marker, text=marker, source_state_token=marker,
                     location=EvidenceLocation(conversation_id=marker, message_id=marker))
    profile, message, document = case(message=message)
    result = validate_source_bound_record(profile, message, frame(document))
    assert marker not in result.model_dump_json()
    assert marker not in repr(result)
    assert set(type(result).model_fields) == {"schema_version", "source", "profile", "analyzed_at", "tasks"}


@pytest.mark.parametrize("argument", ["profile", "source"])
def test_wrong_boundary_object_types_are_not_implicitly_admitted(argument):
    profile, message, document = case()
    if argument == "profile":
        profile = profile.model_dump(mode="python")
    else:
        message = message.model_dump(mode="python")
    rejected(profile, message, frame(document))
