"""Encoder topic profiles bind one graph, tokenizer and configuration."""
import copy
import json
from pathlib import Path

import pytest

from app.nlp.profiles import Artifact, artifact_closure_digest, read_profile

pytestmark = pytest.mark.ci_tier("fast")
ROOT = Path(__file__).resolve().parents[1]


def rebind(value):
    for component in value["components"]:
        members = [Artifact.model_validate(asset) for asset in value["artifacts"]
                   if asset["artifact_id"] in component["artifact_ids"]]
        closure = members + [Artifact.model_validate(asset) for asset in value["artifacts"]
                             if asset["role"] in ("engine", "runtime")]
        component["artifact_closure_sha256"] = artifact_closure_digest(tuple(closure))
        component["tokenizer_sha256"] = artifact_closure_digest(
            tuple(asset for asset in members if asset.role == "tokenizer"))


def descriptor():
    value = json.loads((ROOT / "shared/nlp/v2/fixtures/split-profile.json").read_bytes())
    component = value["components"][0]
    component["max_windows"] = 1
    component["adapter"].update(
        adapter_id="encoder_topics", adapter_version="1", architecture="encoder-cls-v1",
        artifact_format="onnx-encoder-classification.v1",
        tokenizer_format="huggingface-tokenizer-json.v1")
    component["artifact_ids"] = ["weights.topics", "topics.tokenizer", "topics.config"]
    next(asset for asset in value["artifacts"] if asset["artifact_id"] == "weights.topics")[
        "format_id"] = "onnx-encoder-classification.v1"
    for name, role, fmt in (
        ("topics.tokenizer", "tokenizer", "huggingface-tokenizer-json.v1"),
        ("topics.config", "configuration", "encoder-topic-config.v1"),
    ):
        value["artifacts"].append(dict(
            artifact_id=name, role=role, format_id=fmt, sha256="e" * 64, size_bytes=16))
    rebind(value)
    return value


@pytest.mark.parametrize("version", ["nlp-profile.v2", "nlp-profile.v3"])
@pytest.mark.parametrize("score_kind", ["probability", "none"])
def test_encoder_and_entity_components_retain_separate_bindings(version, score_kind):
    value = descriptor()
    value["schema_version"] = version
    value["components"][0]["tasks"][0]["score_policy"]["kind"] = score_kind
    original_entity = copy.deepcopy(value["components"][1])
    profile = read_profile(json.dumps(value))
    assert profile.execution_mode == "serial_unload"
    assert {binding.task for binding in profile.bindings} == {"message_topics", "concept_mentions"}
    assert profile.components[0].tokenizer_sha256 != profile.components[1].tokenizer_sha256
    assert profile.components[1].model_dump(mode="json") == original_entity


@pytest.mark.parametrize("fault", [
    "architecture", "version", "format", "tokenizer_format", "scores", "calibration",
    "threads", "windows", "empty_labels", "extra_task_label", "other_task",
])
def test_incompatible_encoder_descriptor_is_rejected(fault):
    value = descriptor()
    component = value["components"][0]
    task = component["tasks"][0]
    if fault == "architecture":
        component["adapter"]["architecture"] = "boundary-shared-v1"
    elif fault == "version":
        component["adapter"]["adapter_version"] = "2"
    elif fault == "format":
        component["adapter"]["artifact_format"] = "onnx-boundary-classification.v1"
    elif fault == "tokenizer_format":
        component["adapter"]["tokenizer_format"] = "none"
    elif fault == "scores":
        task["score_policy"]["kind"] = "logit"
    elif fault == "calibration":
        task["score_policy"].update(calibration="calibrated", calibration_sha256="f" * 64)
    elif fault == "threads":
        component["inference_threads"] = 8
    elif fault == "windows":
        component["max_windows"] = 2
    elif fault == "empty_labels":
        component["executed_labels"] = []
        task["enabled_ids"] = []
    elif fault == "extra_task_label":
        component["executed_labels"].append(dict(
            task="concept_mentions", label_id="concept.extra", description="Example"))
    elif fault == "other_task":
        task.update(task="concept_mentions", task_contract="concept_mentions.v1", enabled_ids=[])
    with pytest.raises(ValueError, match="encoder topic adapter mismatch"):
        read_profile(json.dumps(value))


@pytest.mark.parametrize("fault", ["extra_graph", "missing_config", "configuration_format", "weights_format"])
def test_rehashed_but_incompatible_artifact_closure_is_rejected(fault):
    value = descriptor()
    value["schema_version"] = "nlp-profile.v3"
    component = value["components"][0]
    if fault == "extra_graph":
        artifact = dict(next(asset for asset in value["artifacts"]
                             if asset["artifact_id"] == "weights.topics"))
        artifact["artifact_id"] = "weights.extra"
        value["artifacts"].append(artifact)
        component["artifact_ids"].append(artifact["artifact_id"])
    elif fault == "missing_config":
        component["artifact_ids"].remove("topics.config")
        value["artifacts"] = [a for a in value["artifacts"] if a["artifact_id"] != "topics.config"]
    else:
        identity = "topics.config" if fault == "configuration_format" else "weights.topics"
        next(a for a in value["artifacts"] if a["artifact_id"] == identity)["format_id"] = "other.v1"
    rebind(value)
    with pytest.raises(ValueError, match="encoder topic artifact closure mismatch"):
        read_profile(json.dumps(value))
