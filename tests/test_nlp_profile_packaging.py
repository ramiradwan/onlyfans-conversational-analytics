"""Unsigned data preparation preserves exact closures and refuses incomplete inputs."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from app.nlp.profiles import Artifact, artifact_closure_digest, read_profile
from app.nlp.release_catalog import ReleaseEntry, canonical
from tools import nlp_profile_packaging as producer

pytestmark = pytest.mark.ci_tier("fast")
ROOT = Path(__file__).resolve().parents[1]


def pin(path):
    raw = path.read_bytes()
    return dict(path=str(path), size_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())


def rebind(document):
    assets = [Artifact.model_validate(asset) for asset in document["artifacts"]]
    for component in document["components"]:
        members = [asset for asset in assets if asset.artifact_id in component["artifact_ids"]]
        component["artifact_closure_sha256"] = artifact_closure_digest(tuple(
            members + [asset for asset in assets if asset.role in {"engine", "runtime"}]))
        tokenizers = [asset for asset in members if asset.role == "tokenizer"]
        component["tokenizer_sha256"] = artifact_closure_digest(tuple(tokenizers))


class Example:
    def __init__(self, root, *, boundary=False, version="nlp-profile.v3"):
        self.source = root / "inputs"
        self.source.mkdir()
        self.path = self.source / "inputs.json"
        self.output = root / "output"
        self.profile_path = self.source / "profile.json"
        self.release_path = self.source / "worker-release.json"
        self.document = json.loads((ROOT / "shared/nlp/v2/fixtures/split-profile.json").read_bytes())
        self.document["schema_version"] = version
        self.document["artifacts"] = []
        self.payloads = {}
        self.release = dict(schema="nlp-engine-release.v1", artifacts=[], adapters=[])
        for index, name in enumerate(["TextAnalysisWorker.exe", *sorted(producer.RUNTIME_FILES)]):
            key = "executable." + str(index)
            asset = self.asset(key, "engine" if index == 0 else "runtime", "windows-pe-x64.v1",
                               b"synthetic binary: " + name.encode())
            self.release["artifacts"].append(dict(file=name, artifact=copy.deepcopy(asset)))
        worker_hash = self.document["artifacts"][0]["sha256"]
        for index, component in enumerate(self.document["components"]):
            topic = index == 0
            component["engine"]["engine_build_sha256"] = worker_hash
            adapter_id = "boundary_topics" if boundary else "encoder_topics"
            fmt = "onnx-boundary-classification.v1" if boundary else "onnx-encoder-classification.v1"
            if not topic:
                adapter_id, fmt = "boundary_entities", "onnx-boundary-entity.v1"
            component["adapter"].update(adapter_id=adapter_id, adapter_build_sha256=worker_hash,
                architecture="encoder-cls-v1" if adapter_id == "encoder_topics" else "boundary-shared-v1",
                artifact_format=fmt, tokenizer_format="huggingface-tokenizer-json.v1")
            if topic:
                component.update(max_windows=1, max_candidates=64, max_output_findings=64)
            prefix = "topics" if topic else "mentions"
            tokenizer = prefix + ".tokenizer"
            self.asset(tokenizer, "tokenizer", "huggingface-tokenizer-json.v1", b'{"synthetic":"' + prefix.encode() + b'"}')
            graph_names = ["graph"] if adapter_id == "encoder_topics" else ["encoder", "classifier"] if topic else ["encoder", "marginals", "scorer"]
            graphs = {}
            for name in graph_names:
                key = prefix + "." + name
                self.asset(key, "weights", fmt, b"synthetic graph: " + key.encode())
                graphs[name] = key
            config_format = ("boundary-topic-config.v1" if boundary else "encoder-topic-config.v1") if topic else "boundary-entity-config.v2"
            config = dict(schema=config_format, tokenizer=tokenizer, labels=[], policies={}, decoding={})
            if topic and boundary:
                config["task_name"] = "synthetic"
            config["graph" if adapter_id == "encoder_topics" else "graphs"] = graphs["graph"] if adapter_id == "encoder_topics" else graphs
            component["artifact_ids"] = [*graphs.values(), tokenizer, prefix + ".config"]
            if not topic:
                config["presentation_catalog"] = "presentation"
                component["artifact_ids"].append("presentation")
            self.asset(prefix + ".config", "configuration", config_format, canonical(config))
            self.release["adapters"].append(dict(adapter=copy.deepcopy(component["adapter"]),
                task_contracts=[component["tasks"][0]["task_contract"]], score_kinds=["probability", "none"],
                runtime_requirements_sha256=component["runtime_requirements"]["sha256"],
                **{name: component[name] for name in ("max_source_codepoints", "max_batch_items", "max_windows", "max_candidates", "max_output_findings")}))
        self.release["engine"] = copy.deepcopy(self.document["components"][0]["engine"])
        # Construct the expected catalogue directly from each task's declared taxonomy.
        catalogue = dict(policy_id=self.document["presentation"]["policy_id"],
            revision=self.document["presentation"]["revision"], labels=[
                dict(task_contract=task["task_contract"], taxonomy=task["taxonomy"],
                     category_id=key, display_label="Readable " + key)
                for component in self.document["components"] for task in component["tasks"]
                for key in task["enabled_ids"]])
        asset = self.asset("presentation", "configuration", "nlp-presentation.v1", canonical(catalogue))
        self.document["presentation"]["sha256"] = asset["sha256"]
        self.inputs = dict(schema="nlp-profile-package-inputs/v1", maximum_output_bytes=1024 * 1024,
                           minimum_free_bytes=1024, artifacts=[])
        self.save(account=True)

    def asset(self, key, role, fmt, raw):
        self.payloads[key] = raw
        asset = dict(artifact_id=key, role=role, format_id=fmt, size_bytes=len(raw), sha256=producer.digest(raw))
        self.document["artifacts"].append(asset)
        return asset

    def replace(self, key, raw):
        self.payloads[key] = raw
        next(asset for asset in self.document["artifacts"] if asset["artifact_id"] == key).update(
            size_bytes=len(raw), sha256=producer.digest(raw))

    def save(self, *, account=False):
        rebind(self.document)
        if account:
            data = sorted((a for a in self.document["artifacts"] if a["role"] in producer.DATA_ROLES), key=lambda a: a["artifact_id"])
            receipt_size = len(canonical(data))
            all_bytes = sum(a["size_bytes"] for a in self.document["artifacts"])
            self.document["resources"].update(download_bytes=all_bytes,
                installed_bytes=all_bytes + receipt_size,
                temporary_bytes=2 * sum(a["size_bytes"] for a in data) + receipt_size)
        self.inputs["artifacts"] = []
        for key, raw in self.payloads.items():
            path = self.source / (key + ".bin")
            path.write_bytes(raw)
            self.inputs["artifacts"].append(dict(artifact_id=key, source=pin(path)))
        self.profile_path.write_bytes(canonical(read_profile(canonical(self.document))))
        self.release_path.write_bytes(canonical(self.release))
        self.inputs.update(profile=pin(self.profile_path), worker_release=pin(self.release_path))
        self.write_inputs()

    def write_inputs(self):
        self.path.write_bytes(canonical(self.inputs))

    def run(self, **kwargs):
        return producer.prepare(self.path, pin(self.path)["sha256"], self.output, **kwargs)


@pytest.mark.parametrize("boundary,version", [(False, "nlp-profile.v3"), (False, "nlp-profile.v2"), (True, "nlp-profile.v2")])
def test_exact_data_and_release_entry_are_deterministic_unsigned_and_preserve_sources(tmp_path, boundary, version):
    value = Example(tmp_path, boundary=boundary, version=version)
    original = {path: path.read_bytes() for path in value.source.iterdir()}
    receipt = value.run()
    output = {path.relative_to(value.output).as_posix(): path.read_bytes() for path in value.output.rglob("*") if path.is_file()}
    expected_data = {"data/" + a["sha256"] for a in value.document["artifacts"] if a["role"] in producer.DATA_ROLES}
    assert set(output) == expected_data | {"profile.json", "release-entry.json", "inventory.json", "producer-receipt.json"}
    assert receipt["signed"] is False and receipt["approved"] is False and receipt["runtime_executed"] is False
    assert ReleaseEntry.model_validate_json(output["release-entry.json"]).profile_sha256 == producer.digest(output["profile.json"])
    assert {path: path.read_bytes() for path in original} == original
    assert all(str(tmp_path).encode() not in raw for name, raw in output.items() if not name.startswith("data/"))
    inventory = json.loads(output["inventory.json"])
    assert {row["file"] for row in inventory["files"]} == expected_data | {"profile.json", "release-entry.json"}
    for row in inventory["files"]:
        assert len(output[row["file"]]) == row["size_bytes"]
        assert producer.digest(output[row["file"]]) == row["sha256"]
    value.output = tmp_path / "other-output"
    value.run()
    assert {path.relative_to(value.output).as_posix(): path.read_bytes() for path in value.output.rglob("*") if path.is_file()} == output


def test_validate_only_verifies_without_creating_output(tmp_path):
    value = Example(tmp_path)
    assert value.run(validate_only=True)["approved"] is False
    assert not value.output.exists()
    assert not list(tmp_path.glob(".*.stage-*"))


@pytest.mark.parametrize("fault", ["missing", "extra", "duplicate", "pin_hash", "pin_size", "unknown_field"])
def test_mapping_is_closed_and_exactly_pinned(tmp_path, fault):
    value = Example(tmp_path)
    if fault == "missing":
        value.inputs["artifacts"].pop()
    elif fault == "extra":
        value.inputs["artifacts"].append(dict(artifact_id="extra", source=value.inputs["profile"]))
    elif fault == "duplicate":
        value.inputs["artifacts"].append(value.inputs["artifacts"][0])
    elif fault == "pin_hash":
        value.inputs["artifacts"][-1]["source"]["sha256"] = "0" * 64
    elif fault == "pin_size":
        value.inputs["artifacts"][-1]["source"]["size_bytes"] += 1
    else:
        value.inputs["approval"] = True
    value.write_inputs()
    with pytest.raises(ValueError):
        value.run()
    assert not value.output.exists()


@pytest.mark.parametrize("fault", ["engine", "runtime", "architecture", "task", "version", "limit", "runtime_policy"])
def test_worker_metadata_must_match_the_profile_and_compiled_adapter(tmp_path, fault):
    value = Example(tmp_path)
    if fault == "engine":
        value.release["engine"]["package_version"] = "2.0.0.0"
    elif fault == "runtime":
        value.release["artifacts"][-1]["artifact"]["sha256"] = "0" * 64
    elif fault in {"architecture", "version"}:
        value.release["adapters"][0]["adapter"]["architecture" if fault == "architecture" else "adapter_version"] = "other"
    elif fault == "task":
        value.release["adapters"][0]["task_contracts"] = ["concept_mentions.v1"]
    elif fault == "limit":
        value.release["adapters"][0]["max_candidates"] = 1
    else:
        value.release["adapters"][0]["runtime_requirements_sha256"] = "0" * 64
    value.save()
    with pytest.raises(ValueError):
        value.run()
    assert not value.output.exists()


@pytest.mark.parametrize("fmt", ["python-pickle.v1", "python-script.v1", "windows-pe-x64.v1", "unknown.v1"])
def test_v3_cannot_hide_unsupported_data_formats_in_valid_rehashed_closure(tmp_path, fmt):
    value = Example(tmp_path)
    next(a for a in value.document["artifacts"] if a["artifact_id"] == "mentions.encoder")["format_id"] = fmt
    value.save()
    with pytest.raises(ValueError, match="unsupported adapter data format"):
        value.run()


@pytest.mark.parametrize("fault", ["config_reference", "missing_label", "no_catalogue"])
def test_configs_and_whole_profile_readable_labels_are_required(tmp_path, fault):
    value = Example(tmp_path)
    if fault == "config_reference":
        config = json.loads(value.payloads["topics.config"])
        config["graph"] = "mentions.encoder"
        value.replace("topics.config", canonical(config))
    else:
        catalogue = json.loads(value.payloads["presentation"])
        catalogue["labels"] = [row for row in catalogue["labels"] if row["task_contract"] != "message_topics.v1"]
        if fault == "no_catalogue":
            config = json.loads(value.payloads["mentions.config"])
            del config["presentation_catalog"]
            config["schema"] = "boundary-entity-config.v1"
            value.replace("mentions.config", canonical(config))
            next(a for a in value.document["artifacts"] if a["artifact_id"] == "mentions.config")["format_id"] = config["schema"]
            value.document["components"][1]["artifact_ids"].remove("presentation")
            value.document["artifacts"] = [a for a in value.document["artifacts"] if a["artifact_id"] != "presentation"]
            del value.payloads["presentation"]
        else:
            value.replace("presentation", canonical(catalogue))
            value.document["presentation"]["sha256"] = producer.digest(canonical(catalogue))
    value.save(account=True)
    with pytest.raises(ValueError):
        value.run()


@pytest.mark.parametrize("resource", ["installed_bytes", "temporary_bytes", "download_bytes"])
def test_resource_accounting_includes_retained_receipt_and_cold_staging(tmp_path, resource):
    value = Example(tmp_path)
    value.document["resources"][resource] -= 1
    value.save()
    with pytest.raises(ValueError, match="cold staging"):
        value.run()


@pytest.mark.parametrize("fault", ["data", "profile", "manifest", "noncanonical", "duplicate_json"])
def test_corruption_and_noncanonical_or_ambiguous_metadata_are_rejected(tmp_path, fault):
    value = Example(tmp_path)
    if fault == "data":
        Path(value.inputs["artifacts"][-1]["source"]["path"]).write_bytes(b"corrupt")
    elif fault == "profile":
        value.profile_path.write_bytes(b"corrupt")
    elif fault == "manifest":
        with pytest.raises(ValueError):
            producer.prepare(value.path, "0" * 64, value.output)
        return
    elif fault == "noncanonical":
        value.profile_path.write_bytes(json.dumps(value.document, indent=2).encode())
        value.inputs["profile"] = pin(value.profile_path)
        value.write_inputs()
    else:
        value.path.write_bytes(value.path.read_bytes()[:-1] + b',"minimum_free_bytes":1024}')
    with pytest.raises(ValueError):
        value.run()
    assert not value.output.exists()


@pytest.mark.parametrize("fault", ["output_limit", "free_space", "existing", "overlap", "hardlink"])
def test_output_limits_and_input_ownership_are_enforced(tmp_path, monkeypatch, fault):
    value = Example(tmp_path)
    if fault == "output_limit":
        value.inputs["maximum_output_bytes"] = 1
        value.write_inputs()
    elif fault == "free_space":
        monkeypatch.setattr(producer.shutil, "disk_usage", lambda _: SimpleNamespace(free=1024))
    elif fault == "existing":
        value.output.mkdir()
        (value.output / "keep").write_bytes(b"foreign")
    elif fault == "overlap":
        value.output = value.source
    else:
        os.link(value.profile_path, value.source / "hardlink")
    with pytest.raises(ValueError):
        value.run()
    if fault == "existing":
        assert (value.output / "keep").read_bytes() == b"foreign"


def test_copy_failure_leaves_no_final_directory_or_completion_receipt(tmp_path, monkeypatch):
    value = Example(tmp_path)
    original = producer.Source.transfer
    def fail(self, target=None, **kwargs):
        if target is not None:
            target.write(b"partial")
            raise OSError("synthetic write failure")
        return original(self, target, **kwargs)
    monkeypatch.setattr(producer.Source, "transfer", fail)
    with pytest.raises(OSError, match="synthetic write failure"):
        value.run()
    assert not value.output.exists()
    assert not list(tmp_path.glob(".*.stage-*"))


def test_changed_source_during_copy_is_refused_and_source_is_preserved(tmp_path, monkeypatch):
    value = Example(tmp_path)
    original = producer.Source.transfer
    changed = []
    def mutate(self, target=None, **kwargs):
        result = original(self, target, **kwargs)
        if target is not None and not changed:
            self.path.write_bytes(b"changed after copy")
            changed.append(self.path)
        return result
    monkeypatch.setattr(producer.Source, "transfer", mutate)
    with pytest.raises(ValueError, match="changed"):
        value.run()
    assert changed[0].read_bytes() == b"changed after copy"
    assert not value.output.exists()


def test_publication_does_not_replace_a_concurrent_destination(tmp_path, monkeypatch):
    value = Example(tmp_path)
    original = producer._exclusive_publish
    def concurrent(stage, output):
        output.mkdir()
        (output / "keep").write_bytes(b"foreign")
        original(stage, output)
    monkeypatch.setattr(producer, "_exclusive_publish", concurrent)
    with pytest.raises(OSError):
        value.run()
    assert list(value.output.iterdir()) == [value.output / "keep"]
    assert (value.output / "keep").read_bytes() == b"foreign"
    assert not list(tmp_path.glob(".*.stage-*"))


@pytest.mark.parametrize("fault", ["inventory", "receipt", "extra_root", "extra_data", "replacement"])
def test_every_staged_member_is_verified_and_foreign_replacements_survive_cleanup(tmp_path, monkeypatch, fault):
    value = Example(tmp_path)
    original = producer.Source.transfer
    injected = []
    def tamper(self, target=None, **kwargs):
        result = original(self, target, **kwargs)
        stages = list(tmp_path.glob(".*.stage-*"))
        if not injected and stages and (stages[0] / "producer-receipt.json").exists():
            stage = stages[0]
            path = stage / {"inventory": "inventory.json", "receipt": "producer-receipt.json",
                "extra_root": "extra", "extra_data": "data/extra", "replacement": "profile.json"}[fault]
            if fault == "replacement":
                # Move the owned inode aside so the replacement has a distinct identity.
                path.rename(stage / "original-profile")
            path.write_bytes(b"foreign")
            injected.append(path)
        return result
    monkeypatch.setattr(producer.Source, "transfer", tamper)
    with pytest.raises(ValueError, match="staged"):
        value.run()
    assert injected and not value.output.exists()
    if fault in {"extra_root", "extra_data", "replacement"}:
        assert injected[0].read_bytes() == b"foreign"
    assert not list(tmp_path.glob(".*.stage-*/producer-receipt.json"))


def test_aliased_parent_directory_is_refused(tmp_path):
    value = Example(tmp_path)
    alias = tmp_path / "alias"
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(value.source)],
                       check=True, capture_output=True)
    else:
        alias.symlink_to(value.source, target_is_directory=True)
    value.inputs["artifacts"][-1]["source"]["path"] = str(alias / "presentation.bin")
    value.write_inputs()
    with pytest.raises(ValueError):
        value.run()
    assert not value.output.exists()


def test_output_budget_is_checked_before_streaming_large_data(tmp_path, monkeypatch):
    value = Example(tmp_path)
    value.inputs["maximum_output_bytes"] = 1
    value.write_inputs()
    original = producer.Source.transfer
    def forbid(self, target=None, **kwargs):
        assert self.path.name != "mentions.encoder.bin", "weights were read before budget refusal"
        return original(self, target, **kwargs)
    monkeypatch.setattr(producer.Source, "transfer", forbid)
    with pytest.raises(ValueError, match="output exceeds"):
        value.run()


def test_streaming_is_bounded_and_data_pin_lengths_are_not_executable_pin_limits(tmp_path, monkeypatch):
    value = Example(tmp_path)
    value.replace("mentions.encoder", b"bounded synthetic graph" * 1000)
    value.save(account=True)
    monkeypatch.setattr(producer, "CHUNK_BYTES", 37)
    original = producer.Source.__init__
    class BoundedReader:
        def __init__(self, stream):
            self.stream = stream
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def read(self, size):
            assert 0 < size <= 37
            return self.stream.read(size)
    def bounded(self, pin, stack):
        original(self, pin, stack)
        self.stream = BoundedReader(self.stream)
    monkeypatch.setattr(producer.Source, "__init__", bounded)
    value.run()
    assert producer.FilePin(path="D:/model.gguf", size_bytes=2**32, sha256="a" * 64).size_bytes == 2**32


def test_encoder_configuration_unknown_fields_are_refused(tmp_path):
    value = Example(tmp_path)
    config = json.loads(value.payloads["topics.config"])
    config["loader"] = "arbitrary"
    value.replace("topics.config", canonical(config))
    value.save(account=True)
    with pytest.raises(ValueError, match="configuration fields"):
        value.run()


def test_foreign_replacement_data_directory_is_preserved_on_refusal(tmp_path, monkeypatch):
    value = Example(tmp_path)
    original = producer.Source.transfer
    injected = []
    def replace_directory(self, target=None, **kwargs):
        result = original(self, target, **kwargs)
        stages = list(tmp_path.glob(".*.stage-*"))
        if not injected and stages and (stages[0] / "producer-receipt.json").exists():
            directory = stages[0] / "data"
            directory.rename(stages[0] / "original-data")
            directory.mkdir()
            injected.append(directory)
        return result
    monkeypatch.setattr(producer.Source, "transfer", replace_directory)
    with pytest.raises(ValueError, match="staged directory membership"):
        value.run()
    assert injected[0].is_dir() and not list(injected[0].iterdir())
    assert not value.output.exists()
    assert not list(tmp_path.glob(".*.stage-*/producer-receipt.json"))
