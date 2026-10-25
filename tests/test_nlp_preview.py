"""Invented source records exercise selection without inference or persistence."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.analytics.evidence_contracts import EvidenceLocation, EvidenceMessage
from app.analytics.opaque_refs import participant_ref
from app.analytics import nlp_preview as preview
from app.models.knowledge_contributions import (
    CapabilityBinding, InputIdentity, JobSpec, SourceLocation, SourceMemberSpec, inventory_digest,
)
from app.models.text_analysis_preview import NoPreviewOutput, PreviewContribution, PreviewLabel
from app.nlp.contracts import SourceBinding
from app.nlp.profiles import AnalysisProfile

pytestmark = pytest.mark.ci_tier("fast")
FIXTURE = Path(__file__).resolve().parents[1] / "shared/nlp/v2/fixtures/joint-profile.json"
NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)


def source(ordinal, chat="chat", text="😀 echo echo"):
    return EvidenceMessage(account_id="preview-account", location=EvidenceLocation(
        conversation_id=chat, message_id=f"message-{ordinal}"), source_revision=7,
        text=text, sent_at=NOW, direction="inbound", sender_id="speaker",
        content_hash=f"content-{ordinal}", stream_epoch=1, source_sequence=ordinal)


def case(messages=None):
    messages = tuple(messages) if messages is not None else (source(0),)
    value = json.loads(FIXTURE.read_bytes())
    component = value["components"][0]
    for task in component["tasks"]:
        prefix = "topic" if task["task"] == "message_topics" else "concept"
        task["enabled_ids"] = [prefix + ".a", prefix + ".b"]
    component["executed_labels"] = [dict(task=t["task"], label_id=category, description="Invented category")
        for t in component["tasks"] for category in t["enabled_ids"]]
    profile = AnalysisProfile.model_validate_json(json.dumps(value))
    members = []
    for ordinal, message in enumerate(messages):
        ref = message.reference()
        binding = SourceBinding(account_scope=ref.account_ref, source_ref=ref.message_ref,
            source_version=ref.source_version_digest[7:], speaker_ref=participant_ref(message.account_id, message.sender_id))
        members.append(SourceMemberSpec(ordinal=ordinal, source=binding,
            location=SourceLocation(conversation_id=message.location.conversation_id, message_id=message.location.message_id),
            source_revision=7, text_sha256=hashlib.sha256(message.text.encode()).hexdigest()))
    members = tuple(members)
    job_id = str(uuid4())
    spec = JobSpec(request_id=job_id, job_id=job_id, contribution_id=str(uuid4()), installation_id="installation",
        organization_id="organization", creator_account_id="preview-account",
        capability=CapabilityBinding(capability_id="text-analysis", artifact_family="text-analysis-artifact",
            artifact_major=1, object_sha256="1" * 64, object_reference_id="invented-preview",
            profile_id="urn:bridge-clean:text-analysis-preview:v1", profile_sha256="2" * 64),
        parameter_sha256="3" * 64, policy_identity_sha256="4" * 64,
        algorithm_artifacts_sha256="5" * 64, model_or_rules_identity_sha256="6" * 64,
        algorithm_profile_sha256=profile.identity_digest(), input=InputIdentity(canonical_revision=7,
            canonical_content_sha256="7" * 64, inventory_sha256=inventory_digest(members), source_count=len(members)))
    return spec, profile, members, messages


def frame(data, ordinal, topics=("topic.a",), mentions=(("concept.a", 2, 6), ("concept.a", 7, 11)),
          outcomes=None):
    spec, profile, members, messages = data
    message, member = messages[ordinal], members[ordinal]
    tasks = []
    for binding in profile.bindings:
        component = profile.component(binding.component_id)
        findings = ([dict(topic_id=category) for category in topics] if binding.task == "message_topics" else
                    [dict(concept_id=category, span=dict(start=start, end=end)) for category, start, end in mentions])
        outcome = (outcomes or {}).get(binding.task, "assessed_findings" if findings else "assessed_no_match")
        coverage = dict(source_codepoints=len(message.text), examined=[dict(start=0, end=len(message.text))],
                        excluded=[], limits_reached=[])
        reason = "none"
        if outcome == "partial":
            coverage.update(examined=[dict(start=0, end=6)], excluded=[dict(start=6, end=len(message.text))], limits_reached=["windows"])
            reason = "limit_reached"
        elif outcome not in ("assessed_findings", "assessed_no_match"):
            findings = []
            reason = {"uncertain": "below_threshold", "unsupported": "unsupported_input", "failed": "worker_failure",
                      "unprocessed": "dependency_unavailable"}[outcome]
        tasks.append(dict(source=member.source.model_dump(mode="json"), profile_sha256=profile.identity_digest(),
            result=dict(component_id=component.component_id, component_sha256=component.identity_digest(),
                text_sha256=member.text_sha256, payload=dict(task=binding.task, findings=findings,
                    outcome=outcome, coverage=coverage, reason=reason))))
    document = dict(schema_version="nlp-result.v2", source=member.source.model_dump(mode="json"),
                    profile=profile.model_dump(mode="json"), analyzed_at="2026-10-25T00:00:00Z", tasks=tasks)
    return json.dumps(document, separators=(",", ":")).encode()


def selected(data=None):
    data = data or case()
    accumulator = preview.PreviewAccumulator(*data[:3])
    for ordinal, message in enumerate(data[3]):
        accumulator.add(ordinal, message, frame(data, ordinal))
    return data, accumulator.finish()


def test_selected_output_has_complete_original_evidence_and_two_stage_validation():
    data, output = selected()
    assert isinstance(output, PreviewContribution)
    assert [s.kind for s in output.summaries] == ["topic_category", "mention_category"]
    topic, mention = output.summaries
    assert topic.evidence[0].reference.span is None
    assert [data[3][0].text[e.reference.span.start:e.reference.span.end] for e in mention.evidence] == ["echo", "echo"]
    assert mention.occurrences == 2 and mention.supporting_messages == 1
    assert topic.additional_categories == mention.additional_categories == 0
    raw = preview.encode_preview(output)
    assert preview.validate_preview_output(*data[:3], raw) == output
    assert preview.validate_preview_source(output, data[1], data[2][0], data[3][0]) is None
    assert preview.decode_preview(raw) == output


def test_category_support_ranking_uses_conversations_before_messages_and_scores():
    data = case([source(0, "one"), source(1, "one"), source(2, "two"), source(3, "three")])
    acc = preview.PreviewAccumulator(*data[:3])
    for ordinal, message in enumerate(data[3]):
        category = "a" if ordinal < 2 else "b"
        raw = frame(data, ordinal, topics=("topic." + category,), mentions=(("concept." + category, 2, 6),))
        value = json.loads(raw)
        for stored in value["tasks"]:
            for finding in stored["result"]["payload"]["findings"]:
                finding["score"] = {"kind": "probability", "value": 0.99 if category == "a" else 0.01}
        raw = json.dumps(value).encode()
        acc.add(ordinal, message, raw)
    output = acc.finish()
    assert [s.label.category_id for s in output.summaries] == ["topic.b", "concept.b"]
    assert all(s.additional_categories == 1 for s in output.summaries)
    encoded = preview.encode_preview(output)
    assert b'"category_id":"topic.a"' not in encoded and b'"category_id":"concept.a"' not in encoded
    assert b'"score"' not in encoded and b'assessed_findings' not in encoded
    assert all(e.assessment == "assessed" for e in output.examination)


def test_mention_occurrences_break_support_tie_and_categories_break_exact_tie():
    data = case()
    acc = preview.PreviewAccumulator(*data[:3])
    acc.add(0, data[3][0], frame(data, 0, topics=("topic.b", "topic.a"),
        mentions=(("concept.a", 2, 6), ("concept.b", 2, 6), ("concept.b", 7, 11))))
    out = acc.finish()
    assert [s.label.category_id for s in out.summaries] == ["topic.a", "concept.b"]
    assert out.summaries[1].occurrences == 2


def test_arrival_order_does_not_change_selected_bytes():
    data = case([source(0), source(1, "other")])
    outputs = []
    for order in [(0, 1), (1, 0)]:
        acc = preview.PreviewAccumulator(*data[:3])
        for ordinal in order:
            acc.add(ordinal, data[3][ordinal], frame(data, ordinal))
        outputs.append(preview.encode_preview(acc.finish()))
    assert outputs[0] == outputs[1]


def test_messages_break_conversation_ties_before_category_id():
    data = case([source(0), source(1), source(2)])
    acc = preview.PreviewAccumulator(*data[:3])
    for ordinal, message in enumerate(data[3]):
        suffix = "b" if ordinal < 2 else "a"
        acc.add(ordinal, message, frame(data, ordinal, topics=("topic." + suffix,),
                mentions=(("concept." + suffix, 2, 6),)))
    assert [s.label.category_id for s in acc.finish().summaries] == ["topic.b", "concept.b"]


@pytest.mark.parametrize("outcome", ["uncertain", "unsupported", "failed", "unprocessed", "partial"])
def test_unresolved_task_with_even_complete_coverage_never_gets_total(outcome):
    data = case([source(0), source(1)])
    acc = preview.PreviewAccumulator(*data[:3])
    acc.add(0, data[3][0], frame(data, 0))
    acc.add(1, data[3][1], frame(data, 1, mentions=(("concept.b", 2, 6),), outcomes={"concept_mentions": outcome}))
    topic, mention = acc.finish().summaries
    assert topic.additional_categories == 0 and topic.ranking_scope == "frozen_scope"
    assert mention.additional_categories is None and mention.ranking_scope == "assessed_portion"
    assert mention.share_of_complete_messages == 1.0


def test_partial_positive_evidence_is_retained_without_calling_examination_complete():
    data = case()
    acc = preview.PreviewAccumulator(*data[:3])
    acc.add(0, data[3][0], frame(data, 0, mentions=(("concept.a", 2, 6),), outcomes={"concept_mentions": "partial"}))
    summary = acc.finish().summaries[1]
    assert summary.partial_supporting_messages == 1 and summary.complete_supporting_messages == 0
    assert summary.evidence[0].assessment == "partial" and summary.additional_categories is None
    assert summary.share_of_complete_messages is None


def test_empty_incomplete_unprocessed_and_missing_are_distinct():
    data = case()
    acc = preview.PreviewAccumulator(*data[:3])
    with pytest.raises(ValueError, match="members_missing"):
        acc.finish()
    acc.add(0, data[3][0], frame(data, 0, topics=(), mentions=()))
    outcome = acc.finish()
    assert isinstance(outcome, NoPreviewOutput) and outcome.disposition == "empty"
    assert preview.validate_preview_no_output(*data[:3], outcome) == outcome
    acc = preview.PreviewAccumulator(*data[:3]); acc.unprocessed(0)
    assert acc.finish().disposition == "incomplete"


def test_unprocessed_member_preserves_frozen_scope_and_no_total():
    data = case([source(0), source(1)])
    acc = preview.PreviewAccumulator(*data[:3])
    acc.add(0, data[3][0], frame(data, 0)); acc.unprocessed(1)
    output = acc.finish()
    assert output.source_count == 2 and all(t.eligible == 2 and t.unprocessed == 1 for t in output.tasks)
    assert all(s.additional_categories is None for s in output.summaries)
    assert all(e.coverage is None for e in output.examination if e.ordinal == 1)


def test_global_candidate_overflow_returns_no_output_and_discards_candidates(monkeypatch):
    data = case(); acc = preview.PreviewAccumulator(*data[:3])
    monkeypatch.setattr(preview, "MAX_PREVIEW_CANDIDATES", 2)
    acc.add(0, data[3][0], frame(data, 0))
    result = acc.finish()
    assert isinstance(result, NoPreviewOutput) and result.disposition == "overflow"
    assert acc._candidates == {} and acc._exams == {}
    with pytest.raises(ValueError):
        acc.add(0, data[3][0], frame(data, 0))


def test_output_overflow_does_not_replace_largest_winner_with_smaller_candidate(monkeypatch):
    data = case([source(0), source(1, "other")])
    acc = preview.PreviewAccumulator(*data[:3])
    acc.add(0, data[3][0], frame(data, 0, topics=("topic.a", "topic.b")))
    acc.add(1, data[3][1], frame(data, 1))
    monkeypatch.setattr(preview, "MAX_PREVIEW_BYTES", 100)
    assert acc.finish().disposition == "overflow"
    assert acc._candidates == {}


@pytest.mark.parametrize("change", ["text", "sender", "source", "account", "span", "record_hash"])
def test_actual_source_and_record_bindings_are_checked_before_selection(change):
    data = case(); acc = preview.PreviewAccumulator(*data[:3]); message = data[3][0]
    raw = frame(data, 0)
    if change in ("text", "sender", "account"):
        key = {"text": "text", "sender": "sender_id", "account": "account_id"}[change]
        message = message.model_copy(update={key: "different"})
    else:
        document = json.loads(raw)
        if change == "source": document["source"]["source_version"] = "a" * 64
        if change == "record_hash": document["tasks"][0]["result"]["text_sha256"] = "a" * 64
        if change == "span": document["tasks"][1]["result"]["payload"]["findings"][0]["span"]["end"] = 99
        raw = json.dumps(document).encode()
    with pytest.raises(ValueError):
        acc.add(0, message, raw)


def test_second_stage_rejects_current_text_change_but_accepts_unrelated_revision():
    data, out = selected()
    changed = data[3][0].model_copy(update={"source_revision": 100})
    preview.validate_preview_source(out, data[1], data[2][0], changed)
    with pytest.raises(ValueError):
        preview.validate_preview_source(out, data[1], data[2][0], changed.model_copy(update={"text": "changed"}))


def test_success_clears_transient_candidates_and_cannot_be_finished_twice():
    data = case(); acc = preview.PreviewAccumulator(*data[:3])
    acc.add(0, data[3][0], frame(data, 0)); acc.finish()
    assert acc._candidates == {} and acc._exams == {}
    with pytest.raises(ValueError): acc.finish()


def test_source_record_byte_limit_is_exact_and_does_not_truncate():
    data = case(); raw = frame(data, 0)
    boundary = raw + b" " * (65_536 - len(raw))
    acc = preview.PreviewAccumulator(*data[:3]); acc.add(0, data[3][0], boundary)
    assert isinstance(acc.finish(), PreviewContribution)
    acc = preview.PreviewAccumulator(*data[:3])
    with pytest.raises(ValueError): acc.add(0, data[3][0], boundary + b" ")


def test_inventory_mismatch_missing_source_and_duplicate_add_are_not_empty_output():
    data = case()
    with pytest.raises(ValueError): preview.PreviewAccumulator(data[0], data[1], ())
    acc = preview.PreviewAccumulator(*data[:3]); acc.unprocessed(0)
    with pytest.raises(ValueError): acc.add(0, data[3][0], frame(data, 0))
    assert acc.finish().disposition == "incomplete"


def configured_labels(profile):
    labels = []
    for component in profile.components:
        for descriptor in component.tasks:
            for category in descriptor.enabled_ids:
                value = dict(presentation=profile.presentation.model_dump(mode="json"),
                    task_contract=descriptor.task_contract, taxonomy=descriptor.taxonomy.model_dump(mode="json"),
                    category_id=category, display_label="Display " + category, label_status="resolved")
                digest = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                    separators=(",", ":")).encode()).hexdigest()
                labels.append(PreviewLabel.model_validate_json(json.dumps(dict(value, label_identity_sha256=digest))))
    return tuple(labels)


def test_independently_pinned_display_labels_are_retained_for_selected_categories_only():
    data = case(); labels = configured_labels(data[1])
    acc = preview.PreviewAccumulator(*data[:3], labels=labels)
    acc.add(0, data[3][0], frame(data, 0)); out = acc.finish()
    assert [s.label.display_label for s in out.summaries] == ["Display topic.a", "Display concept.a"]
    assert b"Display topic.b" not in preview.encode_preview(out)
    assert preview.validate_preview_output(*data[:3], preview.encode_preview(out)) == out


@pytest.mark.parametrize("fault", ["missing", "duplicate", "digest", "taxonomy"])
def test_display_label_context_cannot_change_category_identity(fault):
    data = case(); labels = list(configured_labels(data[1]))
    if fault == "missing": labels.pop()
    if fault == "duplicate": labels[1] = labels[0]
    if fault == "digest": labels[0] = labels[0].model_copy(update={"display_label": "candidate supplied"})
    if fault == "taxonomy": labels[0] = labels[0].model_copy(update={"taxonomy": data[1].aggregation})
    with pytest.raises(ValueError): preview.PreviewAccumulator(*data[:3], labels=tuple(labels))
