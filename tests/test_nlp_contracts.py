"""Synthetic protocol failures and Unicode/source identity boundaries."""
import copy
import json
import traceback
from pathlib import Path

import pytest
from pydantic import ValidationError
from jsonschema import Draft202012Validator
from app.nlp.contracts import (EnrichmentSnapshot, WorkerResponse, NlpBoundaryError, decode_request,
                               validate_response, MAX_FRAME_BYTES)
from tools.generate_nlp_contracts import documents
from app.nlp import adapter, profiles, v2
from tools.generate_nlp_contracts import documents_v2, fixtures_v2

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
    with pytest.raises(ValueError, match="nlp_frame_too_large"):
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
        WorkerResponse.model_validate_json(frame(response))
    with pytest.raises(NlpBoundaryError, match="nlp_invalid_response"):
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
    with pytest.raises(ValueError, match="nlp_item_set_mismatch"):
        validate_response(request, frame(response))


def test_utf16_length_cannot_replace_original_codepoint_length():
    request = decode_request(frame(fixture("request")))
    response = fixture("response")
    # Both task partitions are internally valid, but the astral character counts once.
    for task in ("topics", "mentions"):
        coverage = response["items"][0][task]["coverage"]
        coverage.update(source_codepoints=12, examined=[dict(start=0, end=12)])
    with pytest.raises(ValueError, match="nlp_source_length_mismatch"):
        validate_response(request, frame(response))


def test_utf16_span_beyond_original_codepoints_is_rejected():
    request = decode_request(frame(fixture("request")))
    response = fixture("response")
    response["items"][0]["mentions"]["mentions"][-1]["span"] = dict(start=8, end=12)
    with pytest.raises(ValueError, match="nlp_invalid_response"):
        validate_response(request, frame(response))


def test_invalid_unicode_input_is_rejected():
    value = fixture("request")
    value["items"][0]["text"] = "\ud800"
    with pytest.raises(NlpBoundaryError, match="nlp_invalid_request"):
        decode_request(json.dumps(value).encode())


@pytest.mark.parametrize("boundary", ["request", "response"])
@pytest.mark.parametrize("malformed", ["typed", "json"])
def test_public_boundary_errors_never_render_payload_content(boundary, malformed):
    marker = "SYNTHETIC_PRIVATE_MARKER_5729"
    value = fixture(boundary)
    if boundary == "request":
        value["items"][0]["text"] = marker
        value["items"][0]["request_item_id"] = "invalid " + marker
    else:
        value["items"][0]["calibration_status"] = marker
    payload = frame(value) if malformed == "typed" else ("{bad:" + marker).encode()
    request = decode_request(frame(fixture("request")))
    with pytest.raises(NlpBoundaryError) as caught:
        if boundary == "request":
            decode_request(payload)
        else:
            validate_response(request, payload)
    error = caught.value
    assert str(error) == "nlp_invalid_" + boundary
    assert marker not in str(error)
    assert marker not in repr(error)
    assert marker not in "".join(traceback.format_exception(error))
    assert error.__cause__ is None
    assert error.__suppress_context__


def v2_fixture(name):
    return json.loads((ROOT / "shared/nlp/v2/fixtures" / (name + ".json")).read_text("utf-8"))


def v2_profile(name="joint"):
    return profiles.AnalysisProfile.model_validate_json(frame(v2_fixture(name + "-profile")))


def v2_case(name="joint", component="joint"):
    profile = v2_profile(name)
    request = v2.validate_request(profile, frame(v2_fixture(name + "-" + component + "-request")))
    response = v2_fixture(name + "-" + component + "-response")
    return profile, request, response


def bind_v2(profile, request, response):
    """Simulate an invocation admitted under a revised immutable descriptor."""
    request = request.model_dump(mode="json")
    component = profile.component(request["component_id"])
    request.update(profile_sha256=profile.identity_digest(), component_sha256=component.identity_digest())
    response = copy.deepcopy(response)
    response.update(profile_sha256=request["profile_sha256"], component_sha256=request["component_sha256"])
    for item in response["items"]:
        for task in item["tasks"]:
            task["component_sha256"] = component.identity_digest()
    return v2.validate_request(profile, frame(request)), response


def rehash_v2(value):
    """Recompute dependent identities as an artifact publisher would."""
    assets = [profiles.Artifact.model_validate_json(frame(asset)) for asset in value["artifacts"]]
    for component in value["components"]:
        closure = tuple(asset for asset in assets if asset.artifact_id in component["artifact_ids"] or
                        asset.role in ("engine", "runtime"))
        component["artifact_closure_sha256"] = profiles.artifact_closure_digest(closure)
        component["tokenizer_sha256"] = profiles.artifact_closure_digest(tuple(asset for asset in closure if asset.role == "tokenizer"))
    return profiles.AnalysisProfile.model_validate_json(frame(value))


def test_v2_generated_schemas_and_fixtures_match_authoritative_models():
    for name, schema in documents_v2().items():
        assert json.loads((ROOT / "shared/nlp/v2" / name).read_text()) == schema
        Draft202012Validator.check_schema(schema)
    for name, value in fixtures_v2().items():
        assert v2_fixture(name.removesuffix(".json")) == value
        model = "AnalysisProfile" if name.endswith("profile.json") else "WorkerRequest" if name.endswith("request.json") else "WorkerResponse"
        Draft202012Validator(documents_v2()[model + ".schema.json"]).validate(value)


class SyntheticAdapter:
    """No inference: exercise admission, lifecycle and typed output boundaries."""

    def __init__(self, profile, component, response, events):
        self.profile, self.component, self.response, self.events = profile, component, response, events
        self.loaded = False

    def describe(self, request):
        capabilities = []
        for component in self.profile.components:
            capabilities.append(dict(adapter=component.adapter.model_dump(mode="json"),
                                     task_contracts=[task.task_contract for task in component.tasks],
                                     score_kinds=["probability", "logit", "margin", "none"],
                                     runtime_requirements_sha256=component.runtime_requirements.sha256,
                                     **{name: getattr(component, name) for name in (
                                         "max_source_codepoints", "max_batch_items", "max_windows",
                                         "max_candidates", "max_output_findings")}))
        return adapter.WorkerHandshake.model_validate_json(frame(dict(
            request_id=request.request_id, engine=request.expected_engine.model_dump(mode="json"), adapters=capabilities)))

    def prepare(self, request):
        assert not self.loaded
        self.loaded = True
        self.events.append(("prepare", self.component.component_id))
        return adapter.PreparationResult(request_id=request.request_id, profile_sha256=request.profile_sha256,
                                         component_id=request.component_id, component_sha256=request.component_sha256,
                                         outcome="ready", reason="none")

    def analyze(self, request):
        assert self.loaded
        self.events.append(("analyze", self.component.component_id))
        return v2.WorkerResponse.model_validate_json(frame(self.response))

    def cancel(self, request):
        self.events.append(("cancel", self.component.component_id))
        return adapter.LifecycleResult(operation="cancel", request_id=request.request_id,
                                       profile_sha256=request.profile_sha256, component_sha256=request.component_sha256,
                                       target_request_id=request.target_request_id, outcome="completed", health="ready")

    def unload(self, request):
        assert self.loaded
        self.loaded = False
        self.events.append(("unload", self.component.component_id))
        return adapter.LifecycleResult(operation="unload", request_id=request.request_id,
                                       profile_sha256=request.profile_sha256, component_sha256=request.component_sha256,
                                       outcome="completed", health="unloaded")


def exercise_v2(name):
    profile = v2_profile(name)
    source = dict(account_scope="account.1", source_ref="source.1", source_version="1" * 64, speaker_ref="speaker.1")
    stored, events = [], []
    for component in profile.components:
        _, request, raw = v2_case(name, component.component_id)
        fake = SyntheticAdapter(profile, component, raw, events)
        hello = adapter.HandshakeRequest(request_id="hello", expected_engine=component.engine)
        adapter.validate_handshake(profile, hello, fake.describe(hello).model_dump_json().encode())
        preparation = dict(request_id="prepare", profile_sha256=profile.identity_digest(),
                           component_id=component.component_id, component_sha256=component.identity_digest(),
                           artifacts=[asset.model_dump(mode="json") for asset in profile.artifacts
                                      if asset.artifact_id in component.artifact_ids or asset.role in ("engine", "runtime")],
                           max_memory_bytes=profile.resources.transition_peak_memory_bytes,
                           inference_threads=profile.resources.inference_threads)
        prepare = adapter.validate_prepare_request(profile, frame(preparation))
        assert adapter.validate_preparation(prepare, fake.prepare(prepare).model_dump_json().encode()).outcome == "ready"
        response = v2.validate_response(profile, request, fake.analyze(request).model_dump_json().encode())
        stored.extend(dict(source=source, profile_sha256=profile.identity_digest(), result=task.model_dump(mode="json"))
                      for task in response.items[0].tasks)
        cancel = adapter.CancelRequest(request_id="cancel", target_request_id=request.request_id,
                                       profile_sha256=profile.identity_digest(), component_sha256=component.identity_digest())
        adapter.validate_lifecycle(cancel, fake.cancel(cancel).model_dump_json().encode())
        unload = adapter.UnloadRequest(request_id="unload", profile_sha256=profile.identity_digest(),
                                       component_sha256=component.identity_digest())
        adapter.validate_lifecycle(unload, fake.unload(unload).model_dump_json().encode())
        assert not fake.loaded
    record = v2.EnrichmentRecord.model_validate_json(frame(dict(source=source, profile=profile.model_dump(mode="json"),
                                                              analyzed_at="2026-01-01T00:00:00Z", tasks=stored)))
    decoded = v2.read_record(v2.encode_record(record))
    assert decoded == record
    return record, events


def test_joint_serial_split_and_pack_revision_share_typed_record_boundary():
    records = []
    for name in ("joint", "split", "revision"):
        record, events = exercise_v2(name)
        records.append(record)
        assert [event for event, _ in events] == ["prepare", "analyze", "cancel", "unload"] * len(record.profile.components)
        assert len({task.result.component_sha256 for task in record.tasks}) == len(record.profile.components)
        for task in record.tasks:
            assert task.profile_sha256 == record.profile.identity_digest() and task.source == record.source
    semantics = [{task.result.payload.task: task.result.payload for task in record.tasks} for record in records]
    assert semantics[0] == semantics[1] == semantics[2]
    assert len({record.profile.identity_digest() for record in records}) == 3
    assert records[0].tasks[0].result.component_sha256 != records[2].tasks[0].result.component_sha256


@pytest.mark.parametrize("fault", ["missing_item", "duplicate_item", "unknown_item", "missing_task", "duplicate_task",
                                  "extra_task", "component", "task_component", "profile", "source", "length", "category", "extra"])
def test_v2_rejects_malformed_identity_and_exact_membership(fault):
    profile, request, response = v2_case()
    item = response["items"][0]
    if fault == "missing_item": response["items"] = []
    if fault == "duplicate_item": response["items"].append(copy.deepcopy(item))
    if fault == "unknown_item": item["request_item_id"] = "unknown"
    if fault == "missing_task": item["tasks"].pop()
    if fault == "duplicate_task": item["tasks"][1] = copy.deepcopy(item["tasks"][0])
    if fault == "extra_task":
        raw = request.model_dump(mode="json")
        raw["tasks"] = ["message_topics"]
        request = v2.validate_request(profile, frame(raw))
    if fault == "component": response["component_sha256"] = "0" * 64
    if fault == "task_component": item["tasks"][0]["component_id"] = "other"
    if fault == "profile": response["profile_sha256"] = "0" * 64
    if fault == "source": item["tasks"][0]["text_sha256"] = "0" * 64
    if fault == "length": item["tasks"][0]["payload"]["coverage"] = dict(source_codepoints=12, examined=[dict(start=0, end=12)], excluded=[], limits_reached=[])
    if fault == "category": item["tasks"][1]["payload"]["findings"][0]["concept_id"] = "unknown"
    if fault == "extra": response["database_path"] = "forbidden"
    with pytest.raises(NlpBoundaryError):
        v2.validate_response(profile, request, frame(response))


@pytest.mark.parametrize("state,reason", [("failed", "worker_failure"), ("unprocessed", "cancelled"),
                                         ("unsupported", "unsupported_input"), ("partial", "limit_reached")])
def test_v2_independent_task_outcomes_do_not_create_absence(state, reason):
    profile, request, response = v2_case()
    payload = response["items"][0]["tasks"][1]["payload"]
    payload.update(outcome=state, reason=reason, findings=[])
    payload["coverage"].update(examined=[], excluded=[dict(start=0, end=11)])
    validated = v2.validate_response(profile, request, frame(response))
    assert validated.items[0].tasks[0].payload.outcome == "assessed_no_match"
    assert validated.items[0].tasks[1].payload.outcome == state
    payload.update(outcome="assessed_no_match", reason="none")
    with pytest.raises(NlpBoundaryError): v2.validate_response(profile, request, frame(response))


@pytest.mark.parametrize("field", ["download_bytes", "installed_bytes", "temporary_bytes",
                                   "transition_peak_memory_bytes", "inference_threads"])
def test_v2_resource_policy_is_explicit_local_admission(field):
    raw = v2_profile().model_dump(mode="json")
    baseline = profiles.BASELINE_RESOURCE_POLICY
    raw["resources"][field] = getattr(baseline, field) + 1
    if field == "temporary_bytes":
        raw["resources"][field] = 2048
        baseline = baseline.model_copy(update={"temporary_bytes": 1024})
    profile = profiles.AnalysisProfile.model_validate_json(frame(raw))
    with pytest.raises(NlpBoundaryError, match="nlp_resource_limit"):
        profiles.validate_resource_policy(profile, baseline)
    policy = profiles.ResourcePolicy.model_validate_json(baseline.model_dump_json())
    policy = policy.model_copy(update={field: raw["resources"][field]})
    profiles.validate_resource_policy(profile, policy)
    component = profile.components[0]
    request = dict(request_id="prepare", profile_sha256=profile.identity_digest(),
                   component_id=component.component_id, component_sha256=component.identity_digest(),
                   artifacts=[asset.model_dump(mode="json") for asset in profile.artifacts],
                   max_memory_bytes=profile.resources.transition_peak_memory_bytes,
                   inference_threads=profile.resources.inference_threads)
    adapter.validate_prepare_request(profile, frame(request), resource_policy=policy)
    if field != "temporary_bytes":
        with pytest.raises(NlpBoundaryError, match="nlp_resource_limit"):
            adapter.validate_prepare_request(profile, frame(request))


@pytest.mark.parametrize("field", ["download_bytes", "installed_bytes", "rollback_bytes", "temporary_bytes",
                                   "transition_peak_memory_bytes", "inference_threads"])
def test_v2_resource_declarations_reject_unsafe_integers(field):
    raw = v2_profile().model_dump(mode="json")
    raw["resources"][field] = profiles.MAX_SAFE_INTEGER + 1
    with pytest.raises(ValidationError):
        profiles.AnalysisProfile.model_validate_json(frame(raw))


def test_v2_aggregate_accounting_and_component_boundaries():
    exact = v2_profile().model_dump(mode="json")
    exact["resources"].update(download_bytes=profiles.MAX_DOWNLOAD_BYTES,
                              installed_bytes=profiles.MAX_INSTALLED_BYTES - 1, rollback_bytes=1,
                              transition_peak_memory_bytes=profiles.MAX_MEMORY_BYTES, inference_threads=2)
    profiles.validate_resource_policy(profiles.AnalysisProfile.model_validate_json(frame(exact)),
                                     profiles.BASELINE_RESOURCE_POLICY)
    exact["resources"]["rollback_bytes"] = 2
    with pytest.raises(NlpBoundaryError):
        profiles.validate_resource_policy(profiles.AnalysisProfile.model_validate_json(frame(exact)),
                                         profiles.BASELINE_RESOURCE_POLICY)
    raw = v2_profile().model_dump(mode="json")
    raw["resources"].update(installed_bytes=profiles.MAX_SAFE_INTEGER - 1, rollback_bytes=1)
    profile = profiles.AnalysisProfile.model_validate_json(frame(raw))
    policy = profiles.BASELINE_RESOURCE_POLICY.model_copy(update={"installed_bytes": profiles.MAX_SAFE_INTEGER})
    profiles.validate_resource_policy(profile, policy)
    raw["resources"]["rollback_bytes"] = 2
    with pytest.raises(ValidationError, match="overflow"):
        profiles.AnalysisProfile.model_validate_json(frame(raw))
    for field, resource in (("peak_memory_bytes", "transition_peak_memory_bytes"),
                            ("inference_threads", "inference_threads")):
        value = v2_profile("split").model_dump(mode="json")
        value["components"][1][field] = value["resources"][resource] + 1
        with pytest.raises(ValidationError, match="component exceeds"):
            profiles.AnalysisProfile.model_validate_json(frame(value))


def test_v2_large_artifact_accounting_remains_closed():
    raw = v2_profile().model_dump(mode="json")
    raw["artifacts"][-1]["size_bytes"] = profiles.MAX_INSTALLED_BYTES + 1
    raw["resources"]["installed_bytes"] = sum(asset["size_bytes"] for asset in raw["artifacts"])
    profile = rehash_v2(raw)
    with pytest.raises(NlpBoundaryError):
        profiles.validate_resource_policy(profile, profiles.BASELINE_RESOURCE_POLICY)
    for value in (profiles.MAX_SAFE_INTEGER + 1, 0):
        asset = profile.artifacts[-1].model_dump(mode="json")
        asset["size_bytes"] = value
        with pytest.raises(ValidationError):
            profiles.Artifact.model_validate_json(frame(asset))
    raw["artifacts"][-1]["size_bytes"] = profiles.MAX_SAFE_INTEGER
    raw["resources"]["installed_bytes"] = profiles.MAX_SAFE_INTEGER
    with pytest.raises(ValidationError, match="artifact accounting mismatch"):
        rehash_v2(raw)


@pytest.mark.parametrize("kind,field", [("component", "peak_memory_bytes"), ("component", "inference_threads"),
                                       ("prepare", "max_memory_bytes"), ("prepare", "inference_threads")])
def test_v2_component_and_prepare_resource_integer_bounds(kind, field):
    component = v2_profile().components[0]
    if kind == "component":
        value = component.model_dump(mode="json")
        model = profiles.ComponentDescriptor
    else:
        value = dict(request_id="prepare", profile_sha256="0" * 64, component_id=component.component_id,
                     component_sha256=component.identity_digest(), artifacts=[v2_profile().artifacts[0].model_dump(mode="json")],
                     max_memory_bytes=1024, inference_threads=2)
        model = adapter.PrepareRequest
    for invalid in (0, 1.0, profiles.MAX_SAFE_INTEGER + 1):
        value[field] = invalid
        with pytest.raises(ValidationError):
            model.model_validate_json(frame(value))


def test_v2_unicode_repeated_positions_partial_coverage_and_window_deduplication():
    profile, request, response = v2_case()
    payload = response["items"][0]["tasks"][1]["payload"]
    validated = v2.validate_response(profile, request, frame(response))
    spans = [finding.span for finding in validated.items[0].tasks[1].payload.findings]
    assert [request.items[0].text[span.start:span.end] for span in spans] == ["echo", "echo"]
    payload["findings"].append(copy.deepcopy(payload["findings"][0]))
    with pytest.raises(NlpBoundaryError): v2.validate_response(profile, request, frame(response))
    payload["findings"].pop()
    payload.update(outcome="partial", reason="limit_reached")
    payload["findings"].pop()
    payload["coverage"].update(examined=[dict(start=0, end=6)], excluded=[dict(start=6, end=11)], limits_reached=["windows"])
    v2.validate_response(profile, request, frame(response))
    payload["findings"][0]["span"] = dict(start=7, end=11)
    with pytest.raises(NlpBoundaryError): v2.validate_response(profile, request, frame(response))


@pytest.mark.parametrize("field", ["pack_sha256", "model_revision", "adapter", "executed_labels", "preprocessing",
                                  "windowing", "decoding", "output_mapping", "decision", "runtime_requirements"])
def test_v2_all_executed_policy_changes_invalidate_joint_outputs(field):
    profile, request, response = v2_case()
    value = profile.model_dump(mode="json")
    component = value["components"][0]
    if field == "pack_sha256": component[field] = "0" * 64
    elif field == "model_revision": component[field] = "2"
    elif field == "adapter": component[field]["adapter_build_sha256"] = "0" * 64
    elif field == "executed_labels": component[field][1]["description"] = "Revised conditioning"
    else: component[field]["sha256"] = "0" * 64
    revised = profiles.AnalysisProfile.model_validate_json(frame(value))
    assert revised.components[0].identity_digest() != profile.components[0].identity_digest()
    assert revised.identity_digest() != profile.identity_digest()
    with pytest.raises(NlpBoundaryError): v2.validate_response(revised, request, frame(response))
    current, _ = bind_v2(revised, request, response)
    with pytest.raises(NlpBoundaryError): v2.validate_response(revised, current, frame(response))


@pytest.mark.parametrize("artifact_id,affected", [("weights.topics", 1), ("tokenizer", 2), ("runtime", 2), ("engine", 2)])
def test_v2_component_hashes_bind_resolved_shared_and_independent_assets(artifact_id, affected):
    original = v2_profile("split")
    value = original.model_dump(mode="json")
    next(asset for asset in value["artifacts"] if asset["artifact_id"] == artifact_id)["sha256"] = "0" * 64
    with pytest.raises(ValidationError): profiles.AnalysisProfile.model_validate_json(frame(value))
    revised = rehash_v2(value)
    assert sum(a.identity_digest() != b.identity_digest() for a, b in zip(original.components, revised.components)) == affected
    assert original.identity_digest() != revised.identity_digest()


def test_v2_tokenizer_closure_supports_multiple_files_and_declared_absence():
    value = v2_profile().model_dump(mode="json")
    asset = copy.deepcopy(next(asset for asset in value["artifacts"] if asset["role"] == "tokenizer"))
    asset["artifact_id"] = "vocabulary"
    value["artifacts"].append(asset)
    value["components"][0]["artifact_ids"].append("vocabulary")
    multiple = rehash_v2(value)
    assert multiple.components[0].tokenizer_sha256 != v2_profile().components[0].tokenizer_sha256
    value["artifacts"] = [asset for asset in value["artifacts"] if asset["role"] != "tokenizer"]
    value["components"][0]["artifact_ids"] = ["weights.joint"]
    value["components"][0]["adapter"]["tokenizer_format"] = "none"
    absent = rehash_v2(value)
    assert absent.components[0].tokenizer_sha256 == profiles.artifact_closure_digest(())


def test_v2_canonical_encoding_is_order_independent_for_objects_and_exact_for_arrays_unicode_integers():
    value = v2_profile().model_dump(mode="json")
    reversed_keys = dict(reversed(list(value.items())))
    assert profiles.AnalysisProfile.model_validate_json(frame(reversed_keys)).identity_digest() == v2_profile().identity_digest()
    component = value["components"][0]
    component["executed_labels"][0]["description"] = "é😀"
    unicode_profile = profiles.AnalysisProfile.model_validate_json(frame(value))
    escaped = profiles.AnalysisProfile.model_validate_json(json.dumps(value, ensure_ascii=True).encode())
    assert escaped.identity_digest() == unicode_profile.identity_digest()
    assert b"\\u00e9\\ud83d\\ude00" in profiles.canonical_bytes(escaped)
    component["executed_labels"][0]["description"] = "e\u0301😀"
    assert profiles.AnalysisProfile.model_validate_json(frame(value)).identity_digest() != escaped.identity_digest()
    component["executed_labels"].reverse()
    assert profiles.AnalysisProfile.model_validate_json(frame(value)).identity_digest() != unicode_profile.identity_digest()
    value["resources"]["temporary_bytes"] = 1.0
    with pytest.raises(ValidationError): profiles.AnalysisProfile.model_validate_json(frame(value))
    value["resources"]["temporary_bytes"] = 9_007_199_254_740_992
    with pytest.raises(ValidationError): profiles.AnalysisProfile.model_validate_json(frame(value))


@pytest.mark.parametrize("kind,value", [("probability", .5), ("logit", -12.5), ("margin", 4.5), ("none", None)])
def test_v2_score_kinds_are_truthful_and_optional(kind, value):
    profile, request, response = v2_case()
    raw = profile.model_dump(mode="json")
    raw["components"][0]["tasks"][1]["score_policy"]["kind"] = kind
    profile = profiles.AnalysisProfile.model_validate_json(frame(raw))
    request, response = bind_v2(profile, request, response)
    findings = response["items"][0]["tasks"][1]["payload"]["findings"]
    for finding in findings:
        finding["score"] = None if value is None else dict(kind=kind, value=value)
    v2.validate_response(profile, request, frame(response))
    findings[0]["score"] = dict(kind="margin" if kind == "probability" else "probability", value=.5)
    with pytest.raises(NlpBoundaryError, match="score_policy"):
        v2.validate_response(profile, request, frame(response))
    for finding in findings: finding["score"] = None
    v2.validate_response(profile, request, frame(response))


@pytest.mark.parametrize("kind,value", [("probability", -0.1), ("probability", 1.1), ("probability", True),
                                       ("logit", float("inf")), ("margin", float("nan")), ("confidence", .5)])
def test_v2_invalid_scores_fail_closed(kind, value):
    profile, request, response = v2_case()
    response["items"][0]["tasks"][1]["payload"]["findings"][0]["score"] = dict(kind=kind, value=value)
    with pytest.raises(NlpBoundaryError): v2.validate_response(profile, request, frame(response))


@pytest.mark.parametrize("fault", ["abi", "adapter", "task", "score", "limit", "runtime", "engine", "duplicate", "protocol"])
def test_v2_capabilities_cannot_expand_admitted_profile(fault):
    profile, _, response = v2_case()
    fake = SyntheticAdapter(profile, profile.components[0], response, [])
    hello = adapter.HandshakeRequest(request_id="hello", expected_engine=profile.components[0].engine)
    handshake = fake.describe(hello).model_dump(mode="json")
    cap = handshake["adapters"][0]
    if fault == "abi": cap["adapter"]["abi"] = "nlp-adapter.v99"
    if fault == "adapter": cap["adapter"]["artifact_format"] = "unsupported"
    if fault == "task": cap["task_contracts"] = ["message_topics.v1"]
    if fault == "score": cap["score_kinds"] = ["none"]
    if fault == "limit": cap["max_source_codepoints"] = 8
    if fault == "runtime": cap["runtime_requirements_sha256"] = "0" * 64
    if fault == "engine": handshake["engine"]["engine_build_sha256"] = "0" * 64
    if fault == "duplicate": handshake["adapters"].append(copy.deepcopy(cap))
    if fault == "protocol": handshake["protocol"] = "nlp-worker.v1"
    with pytest.raises(NlpBoundaryError): adapter.validate_handshake(profile, hello, frame(handshake))


@pytest.mark.parametrize("fault", ["hash", "missing", "duplicate", "extra", "threads", "memory", "profile", "component"])
def test_v2_prepare_requires_exact_verified_closure_and_limits(fault):
    profile = v2_profile()
    component = profile.components[0]
    raw = dict(request_id="prepare", profile_sha256=profile.identity_digest(), component_id=component.component_id,
               component_sha256=component.identity_digest(), artifacts=[asset.model_dump(mode="json") for asset in profile.artifacts],
               max_memory_bytes=profile.resources.transition_peak_memory_bytes, inference_threads=2)
    accepted = adapter.validate_prepare_request(profile, frame(raw))
    ready = adapter.PreparationResult(request_id=accepted.request_id, profile_sha256=accepted.profile_sha256,
                                      component_id=accepted.component_id, component_sha256=accepted.component_sha256,
                                      outcome="ready", reason="none")
    assert adapter.validate_preparation(accepted, ready.model_dump_json().encode()) == ready
    if fault == "hash": raw["artifacts"][0]["sha256"] = "0" * 64
    if fault == "missing": raw["artifacts"].pop()
    if fault == "duplicate": raw["artifacts"].append(copy.deepcopy(raw["artifacts"][0]))
    if fault == "extra":
        extra = copy.deepcopy(raw["artifacts"][0]); extra["artifact_id"] = "extra"; raw["artifacts"].append(extra)
    if fault == "threads": raw["inference_threads"] = 3
    if fault == "memory": raw["max_memory_bytes"] -= 1
    if fault == "profile": raw["profile_sha256"] = "0" * 64
    if fault == "component": raw["component_id"] = "other"
    with pytest.raises(NlpBoundaryError): adapter.validate_prepare_request(profile, frame(raw))


@pytest.mark.parametrize("fault", ["third_component", "binding", "missing_closure", "storage", "rollback", "download", "memory", "format", "tokenizer", "engine"])
def test_v2_compositions_and_aggregate_budgets_are_closed(fault):
    value = v2_profile("split").model_dump(mode="json")
    if fault == "third_component": value["components"].append(copy.deepcopy(value["components"][0]))
    if fault == "binding": value["bindings"][1]["component_id"] = "topics"
    if fault == "missing_closure": value["artifacts"].pop()
    if fault == "storage": value["resources"]["installed_bytes"] = 1
    if fault == "rollback": value["resources"]["rollback_bytes"] = profiles.MAX_INSTALLED_BYTES
    if fault == "download": value["resources"]["download_bytes"] = profiles.MAX_DOWNLOAD_BYTES + 1
    if fault == "memory": value["resources"]["transition_peak_memory_bytes"] = 1
    if fault == "format": value["components"][0]["adapter"]["artifact_format"] = "unknown"
    if fault == "tokenizer": value["components"][0]["tokenizer_sha256"] = "0" * 64
    if fault == "engine": value["components"][0]["engine"]["engine_version"] = "other"
    if fault in ("rollback", "download"):
        declared = profiles.AnalysisProfile.model_validate_json(frame(value))
        with pytest.raises(NlpBoundaryError):
            profiles.validate_resource_policy(declared, profiles.BASELINE_RESOURCE_POLICY)
    else:
        with pytest.raises(ValidationError): profiles.AnalysisProfile.model_validate_json(frame(value))


def test_v2_enabled_output_mask_does_not_replace_joint_conditioning_identity():
    profile, request, response = v2_case()
    raw = profile.model_dump(mode="json")
    raw["components"][0]["tasks"][1]["enabled_ids"] = []
    revised = profiles.AnalysisProfile.model_validate_json(frame(raw))
    assert revised.components[0].executed_labels == profile.components[0].executed_labels
    assert revised.components[0].identity_digest() != profile.components[0].identity_digest()
    request, response = bind_v2(revised, request, response)
    with pytest.raises(NlpBoundaryError, match="category"): v2.validate_response(revised, request, frame(response))
    response["items"][0]["tasks"][1]["payload"].update(findings=[], outcome="assessed_no_match")
    v2.validate_response(revised, request, frame(response))


def test_v2_record_reuse_checks_source_profile_and_preserves_v1_reader():
    record, _ = exercise_v2("joint")
    encoded = v2.encode_record(record)
    assert v2.validate_record(record.profile, record.source, encoded) == record
    with pytest.raises(NlpBoundaryError, match="profile"):
        v2.validate_record(v2_profile("revision"), record.source, encoded)
    for field in ("source_version", "source_ref", "account_scope", "speaker_ref"):
        changed = record.source.model_copy(update={field: "0" * 64 if field == "source_version" else "other"})
        with pytest.raises(NlpBoundaryError, match="source_binding"):
            v2.validate_record(record.profile, changed, encoded)
    old = dict(schema_version="nlp-result.v1", source=record.source.model_dump(mode="json"),
               execution=fixture("response")["execution"], analyzed_at=record.analyzed_at, result=fixture("response")["items"][0])
    from app.nlp.contracts import EnrichmentRecord as V1Record
    decoded = v2.read_record(frame(old))
    assert isinstance(decoded, V1Record) and decoded.schema_version == "nlp-result.v1"
    assert decoded.execution.recipe_id == "topics_mentions_v1"
    with pytest.raises(NlpBoundaryError): v2.validate_record(record.profile, record.source, frame(old))


def test_v2_snapshot_currentness_tracks_completion_availability_coverage_and_profile():
    profile = v2_profile()
    tasks = [dict(task=binding.task, task_contract=profiles.TASK_CONTRACTS[binding.task],
                  component_sha256=profile.component(binding.component_id).identity_digest(), availability="current",
                  completion_revision=1, availability_revision=1, findings_sha256="1" * 64, coverage_sha256="2" * 64)
             for binding in profile.bindings]
    raw = dict(account_scope="account.1", canonical_revision=1, completion_revision=1, availability_revision=1,
               profile=profile.model_dump(mode="json"), source_validity_sha256="3" * 64, tasks=tasks)
    original = v2.decode_snapshot(frame(raw))
    for field in ("completion_revision", "availability_revision"):
        changed = copy.deepcopy(raw); changed[field] += 1
        assert v2.decode_snapshot(frame(changed)).identity_digest() != original.identity_digest()
        changed = copy.deepcopy(raw); changed["tasks"][0][field] += 1
        assert v2.decode_snapshot(frame(changed)).identity_digest() != original.identity_digest()
    for field in ("findings_sha256", "coverage_sha256", "availability"):
        changed = copy.deepcopy(raw); changed["tasks"][0][field] = "pending" if field == "availability" else "0" * 64
        assert v2.decode_snapshot(frame(changed)).identity_digest() != original.identity_digest()
    changed = copy.deepcopy(raw); changed["source_validity_sha256"] = "0" * 64
    assert v2.decode_snapshot(frame(changed)).identity_digest() != original.identity_digest()
    changed = copy.deepcopy(raw); changed["profile"] = v2_profile("revision").model_dump(mode="json")
    with pytest.raises(NlpBoundaryError): v2.decode_snapshot(frame(changed))


def test_v2_frames_records_snapshots_and_diagnostics_are_bounded():
    profile, request, response = v2_case()
    marker = "SYNTHETIC_PRIVATE_MARKER_9817"
    for parse, bound in ((v2.decode_request, v2.MAX_FRAME_BYTES), (v2.read_record, v2.MAX_RECORD_BYTES),
                         (v2.decode_snapshot, v2.MAX_RECORD_BYTES)):
        with pytest.raises(NlpBoundaryError, match="too_large"): parse(b" " * (bound + 1))
        for payload in (("{bad:" + marker).encode(), frame(dict(unexpected=marker)), b"{" * 2000):
            with pytest.raises(NlpBoundaryError) as caught: parse(payload)
            assert marker not in "".join(traceback.format_exception(caught.value))
    response["items"][0]["tasks"][0]["payload"]["outcome"] = marker
    with pytest.raises(NlpBoundaryError) as caught: v2.validate_response(profile, request, frame(response))
    assert marker not in "".join(traceback.format_exception(caught.value))
    duplicated = request.model_dump_json().replace('"request_id":', '"request_id":"extra","request_id":', 1)
    with pytest.raises(NlpBoundaryError): v2.decode_request(duplicated.encode())
    raw = request.model_dump(mode="json"); raw["items"][0]["text"] = "\ud800"
    with pytest.raises(NlpBoundaryError): v2.decode_request(json.dumps(raw).encode())


@pytest.mark.parametrize("name", ["joint", "split", "revision"])
def test_v2_normalized_decoder_coordinates_must_resolve_to_original_source(name):
    import hashlib
    import unicodedata

    text = "😀 e\u0301cho echo"
    normalized = unicodedata.normalize("NFC", text)
    assert len(normalized) + 1 == len(text)
    for component in v2_profile(name).components:
        profile, request, response = v2_case(name, component.component_id)
        raw = request.model_dump(mode="json")
        text_digest = hashlib.sha256(text.encode()).hexdigest()
        raw["items"][0].update(text=text, text_sha256=text_digest)
        request = v2.validate_request(profile, frame(raw))
        for task in response["items"][0]["tasks"]:
            task["text_sha256"] = text_digest
            task["payload"]["coverage"].update(source_codepoints=len(text), examined=[dict(start=0, end=len(text))])
            if task["payload"]["task"] == "concept_mentions":
                # Synthetic decoder mapping through a length-changing normalization.
                for finding, original in zip(task["payload"]["findings"], ((2, 7), (8, 12))):
                    finding["span"] = dict(start=original[0], end=original[1])
        result = v2.validate_response(profile, request, frame(response))
        for task in result.items[0].tasks:
            if task.payload.task == "concept_mentions":
                surfaces = [text[finding.span.start:finding.span.end] for finding in task.payload.findings]
                assert [unicodedata.normalize("NFC", surface) for surface in surfaces] == ["écho", "echo"]
        for task in response["items"][0]["tasks"]:
            task["payload"]["coverage"].update(source_codepoints=len(normalized), examined=[dict(start=0, end=len(normalized))])
            task["payload"]["findings"] = []
            task["payload"]["outcome"] = "assessed_no_match"
        with pytest.raises(NlpBoundaryError, match="source_length"):
            v2.validate_response(profile, request, frame(response))


def test_v2_ambiguous_surface_decoder_can_withhold_instead_of_selecting_first_occurrence():
    profile, request, response = v2_case()
    assert request.items[0].text.count("echo") == 2
    payload = response["items"][0]["tasks"][1]["payload"]
    payload.update(findings=[], outcome="uncertain", reason="below_threshold")
    result = v2.validate_response(profile, request, frame(response))
    assert result.items[0].tasks[1].payload.outcome == "uncertain"
    assert not result.items[0].tasks[1].payload.findings


def test_v2_lifecycle_identity_health_and_compatibility_fail_closed():
    profile = v2_profile()
    component = profile.components[0]
    declaration = adapter.ProfileCompatibility(profile_sha256=profile.identity_digest(), compatible_engines=(component.engine,),
                                                 adapters=(component.adapter,))
    adapter.validate_compatibility(profile, declaration)
    with pytest.raises(NlpBoundaryError): adapter.validate_compatibility(v2_profile("revision"), declaration)
    bad_engine = component.engine.model_copy(update={"engine_version": "other"})
    with pytest.raises(NlpBoundaryError):
        adapter.validate_compatibility(profile, declaration.model_copy(update={"compatible_engines": (bad_engine,)}))
    incompatible = component.adapter.model_copy(update={"architecture": "other"})
    with pytest.raises(NlpBoundaryError):
        adapter.validate_compatibility(profile, declaration.model_copy(update={"adapters": (incompatible,)}))
    cancel = adapter.CancelRequest(request_id="cancel", target_request_id="job", profile_sha256=profile.identity_digest(),
                                   component_sha256=component.identity_digest())
    response = dict(operation="cancel", request_id="cancel", profile_sha256=cancel.profile_sha256,
                    component_sha256=cancel.component_sha256, target_request_id="job", outcome="completed", health="ready")
    adapter.validate_lifecycle(cancel, frame(response))
    for field in ("target_request_id", "component_sha256", "profile_sha256", "request_id"):
        stale = copy.deepcopy(response); stale[field] = "0" * 64 if field.endswith("sha256") else "stale"
        with pytest.raises(NlpBoundaryError): adapter.validate_lifecycle(cancel, frame(stale))
    unload = adapter.UnloadRequest(request_id="unload", profile_sha256=cancel.profile_sha256,
                                   component_sha256=cancel.component_sha256)
    response.update(operation="unload", request_id="unload", target_request_id=None)
    with pytest.raises(NlpBoundaryError): adapter.validate_lifecycle(unload, frame(response))
    response["health"] = "unloaded"
    adapter.validate_lifecycle(unload, frame(response))


def test_v2_unreturned_joint_conditioning_remains_part_of_serving_component_identity():
    original, request, response = v2_case("split", "topics")
    raw = original.model_dump(mode="json")
    # This adapter computes topics using both schemas; another component serves mentions.
    raw["components"][0]["executed_labels"].append(dict(
        task="concept_mentions", label_id="conditioning.synthetic", description="Synthetic conditioning"))
    profile = profiles.AnalysisProfile.model_validate_json(frame(raw))
    assert profile.components[0].identity_digest() != original.components[0].identity_digest()
    assert profile.components[1] == original.components[1]
    request, response = bind_v2(profile, request, response)
    fake = SyntheticAdapter(profile, profile.components[0], response, [])
    hello = adapter.HandshakeRequest(request_id="hello", expected_engine=profile.components[0].engine)
    adapter.validate_handshake(profile, hello, fake.describe(hello).model_dump_json().encode())
    validated = v2.validate_response(profile, request, frame(response))
    assert [task.payload.task for task in validated.items[0].tasks] == ["message_topics"]
    revised = profile.model_dump(mode="json")
    revised["components"][0]["executed_labels"][-1]["description"] = "Revised conditioning"
    revised = profiles.AnalysisProfile.model_validate_json(frame(revised))
    assert revised.components[0].identity_digest() != profile.components[0].identity_digest()
    assert revised.components[1] == profile.components[1]
    with pytest.raises(NlpBoundaryError): v2.validate_response(revised, request, frame(response))
    injected = v2_fixture("split-mentions-response")["items"][0]["tasks"][0]
    injected.update(component_id=profile.components[0].component_id, component_sha256=profile.components[0].identity_digest())
    response["items"][0]["tasks"].append(injected)
    with pytest.raises(NlpBoundaryError): v2.validate_response(profile, request, frame(response))
