"""Generate shared NLP schemas from the typed Brain contract owner."""
from pathlib import Path
import json
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.nlp.contracts import (WorkerRequest, WorkerResponse, PackCompatibility,
                               EnrichmentRecord, EnrichmentSnapshot, Readiness, DeploymentResult)
from app.nlp import adapter, profiles, v2

ROOT = Path(__file__).resolve().parents[1]

def documents():
    return {model.__name__ + ".schema.json": model.model_json_schema()
            for model in (WorkerRequest, WorkerResponse, PackCompatibility,
                          EnrichmentRecord, EnrichmentSnapshot, Readiness, DeploymentResult)}


def documents_v2():
    models = (profiles.AnalysisProfile, profiles.ComponentDescriptor, adapter.ProfileCompatibility,
              adapter.HandshakeRequest, adapter.WorkerHandshake, adapter.PrepareRequest,
              adapter.PreparationResult, adapter.CancelRequest, adapter.UnloadRequest,
              adapter.LifecycleResult, v2.WorkerRequest, v2.WorkerResponse,
              v2.EnrichmentRecord, v2.EnrichmentSnapshot)
    return {model.__name__ + ".schema.json": model.model_json_schema() for model in models}


def fixtures_v2():
    """Small synthetic examples, generated through the same authoritative models."""
    import copy
    import hashlib

    def policy(name):
        return dict(policy_id=name, revision="1", sha256=hashlib.sha256(name.encode()).hexdigest())

    def task(name):
        return dict(task=name, task_contract=profiles.TASK_CONTRACTS[name], taxonomy=policy("taxonomy"),
                    enabled_ids=["topic.synthetic" if name == "message_topics" else "concept.synthetic"],
                    score_policy=dict(kind="probability", semantics_id="model_score_not_accuracy"))

    def component(name, tasks):
        engine = dict(package_name="Example.Engine", package_family="Example.Engine_family",
                      core_package_name="Example.Core", core_package_version="1.0.0.0", publisher="CN=Example",
                      package_version="1.0.0.0", related_set_id="set.1", engine_version="1",
                      platform="windows-x64", engine_build_sha256="a" * 64)
        return dict(component_id=name, engine=engine,
                    adapter=dict(adapter_id="synthetic." + name, adapter_version="1", adapter_build_sha256="b" * 64,
                                 architecture="synthetic", artifact_format="synthetic.v1", tokenizer_format="synthetic.v1"),
                    pack_id="pack." + name, pack_revision="1", pack_sha256="c" * 64, model_revision="1",
                    artifact_ids=["weights." + name, "tokenizer"], tokenizer_sha256="d" * 64,
                    runtime_requirements=policy("runtime"),
                    tasks=[task(item) for item in tasks],
                    executed_labels=[dict(task=item, label_id=task(item)["enabled_ids"][0], description="Synthetic label")
                                     for item in tasks],
                    preprocessing=policy("original"), windowing=policy("windows"), decoding=policy("decoder"),
                    output_mapping=policy("mapping"), decision=policy("decision"), max_source_codepoints=131072,
                    max_batch_items=16, max_windows=256, max_candidates=8192, max_output_findings=576,
                    peak_memory_bytes=1024, inference_threads=2)

    def profile(components):
        assets = [dict(artifact_id=name, sha256=digest * 64, size_bytes=16, role=role, format_id="synthetic.v1")
                  for name, digest, role in [("engine", "a", "engine"), ("runtime", "b", "runtime"),
                                             ("tokenizer", "d", "tokenizer")]]
        assets += [dict(artifact_id="weights." + item["component_id"], sha256="c" * 64, size_bytes=16,
                        role="weights", format_id="synthetic.v1") for item in components]
        for item in components:
            closure = tuple(profiles.Artifact.model_validate_json(json.dumps(asset)) for asset in assets
                            if asset["artifact_id"] in item["artifact_ids"] or asset["role"] in ("engine", "runtime"))
            item["artifact_closure_sha256"] = profiles.artifact_closure_digest(closure)
            item["tokenizer_sha256"] = profiles.artifact_closure_digest(tuple(asset for asset in closure if asset.role == "tokenizer"))
        value = dict(profile_id="profile.synthetic", revision="1", components=components,
                     bindings=[dict(task=task["task"], component_id=item["component_id"])
                               for item in components for task in item["tasks"]], artifacts=assets,
                     aggregation=policy("aggregation"), presentation=policy("presentation"),
                     resources=dict(download_bytes=1024, installed_bytes=1024, rollback_bytes=0,
                                    temporary_bytes=1024, transition_peak_memory_bytes=1024, inference_threads=2))
        return profiles.AnalysisProfile.model_validate_json(json.dumps(value))

    joint = profile([component("joint", ["message_topics", "concept_mentions"])])
    split = profile([component("topics", ["message_topics"]), component("mentions", ["concept_mentions"])])
    revised = copy.deepcopy(joint.model_dump(mode="json"))
    revised["revision"] = "2"
    revised["components"][0].update(pack_revision="2", pack_sha256="e" * 64, model_revision="2")
    revised["artifacts"][-1]["sha256"] = "e" * 64
    revised["components"][0]["artifact_closure_sha256"] = profiles.artifact_closure_digest(tuple(
        profiles.Artifact.model_validate_json(json.dumps(asset)) for asset in revised["artifacts"]))
    revised = profiles.AnalysisProfile.model_validate_json(json.dumps(revised))
    result = {}
    for name, selected in (("joint", joint), ("split", split), ("revision", revised)):
        result[name + "-profile.json"] = selected.model_dump(mode="json")
        for item in selected.components:
            request = v2.WorkerRequest.model_validate_json(json.dumps(dict(
                request_id="request." + item.component_id, profile_sha256=selected.identity_digest(),
                component_id=item.component_id, component_sha256=item.identity_digest(),
                tasks=[task.task for task in item.tasks], deadline_ms=1000,
                items=[dict(request_item_id="item.1", text="😀 echo echo",
                            text_sha256=hashlib.sha256("😀 echo echo".encode()).hexdigest())])))
            tasks = []
            for descriptor in item.tasks:
                findings = [] if descriptor.task == "message_topics" else [
                    dict(concept_id="concept.synthetic", span=dict(start=start, end=start + 4),
                         score=dict(kind="probability", value=.95)) for start in (2, 7)]
                tasks.append(dict(component_id=item.component_id, component_sha256=item.identity_digest(),
                                  text_sha256=request.items[0].text_sha256,
                                  payload=dict(task=descriptor.task, outcome="assessed_findings" if findings else "assessed_no_match",
                                               coverage=dict(source_codepoints=11, examined=[dict(start=0, end=11)],
                                                             excluded=[], limits_reached=[]), reason="none", findings=findings)))
            response = v2.WorkerResponse.model_validate_json(json.dumps(dict(
                request_id=request.request_id, profile_sha256=request.profile_sha256,
                component_id=item.component_id, component_sha256=item.identity_digest(),
                items=[dict(request_item_id="item.1", tasks=tasks)])))
            prefix = name + "-" + item.component_id
            result[prefix + "-request.json"] = request.model_dump(mode="json")
            result[prefix + "-response.json"] = response.model_dump(mode="json")
    return result

if __name__ == "__main__":
    target = ROOT / "shared/nlp/v1"
    target.mkdir(parents=True, exist_ok=True)
    for name, schema in documents().items():
        (target / name).write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
    target = ROOT / "shared/nlp/v2"
    target.mkdir(parents=True, exist_ok=True)
    for name, schema in documents_v2().items():
        (target / name).write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
    (target / "fixtures").mkdir(exist_ok=True)
    for name, value in fixtures_v2().items():
        (target / "fixtures" / name).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
