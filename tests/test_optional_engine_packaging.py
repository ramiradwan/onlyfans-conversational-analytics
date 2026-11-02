"""Pinned optional Engine packaging with synthetic PE and SDK outputs."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import warnings
import xml.etree.ElementTree as ET
import zipfile

import pytest

from tools import msix_packaging as msix
from tools import optional_engine_packaging as producer
from tools import optional_msix as optional
from test_related_release import profile

pytestmark = pytest.mark.ci_tier("fast")


def pin(path):
    return dict(path=str(path), size_bytes=path.stat().st_size, sha256=msix.sha256(path))


def pe(body):
    header = bytearray(70)
    header[:2] = b"MZ"
    struct.pack_into("<I", header, 60, 64)
    header[64:] = b"PE\0\0\x64\x86"
    return bytes(header) + body.encode()


def fixture(tmp_path, *, instruction=False, encoder=False):
    source = tmp_path / "inputs"
    source.mkdir()
    worker = source / "worker"
    worker.mkdir()
    selected = profile()
    profile_path = source / "profile.json"
    profile_path.write_bytes(msix.canonical_json(selected))
    names = ["TextAnalysisWorker.exe", "onnxruntime.dll"]
    if instruction:
        names = ["TextAnalysisWorker.exe", *sorted(producer.RUNTIME_FILES),
                 *sorted(producer.INSTRUCTION_FILES - {"ggml-cpu-x64.dll"})]
    artifacts = []
    for index, name in enumerate(names):
        path = worker / name
        path.write_bytes(pe(name))
        artifacts.append(dict(file=name, artifact=dict(artifact_id="runtime." + str(index),
            sha256=msix.sha256(path), size_bytes=path.stat().st_size,
            role="engine" if index == 0 else "runtime",
            format_id="llama-b11669-cpu.dll.v1" if name in producer.INSTRUCTION_FILES else "windows-pe-x64.v1")))
    relation = optional._release_relation(selected)
    engine = dict(vars(relation), related_set_id=selected["servicing"]["installed_artifact"]["related_set_id"],
        engine_version="1", platform="windows-x64", worker_protocol="nlp-worker.v2",
        engine_build_sha256=artifacts[0]["artifact"]["sha256"])
    capability = dict(adapter=dict(adapter_id="boundary_entities", adapter_version="1",
        adapter_build_sha256=engine["engine_build_sha256"], abi="nlp-adapter.v1", architecture="boundary-shared-v1",
        artifact_format="onnx-boundary-entity.v1", tokenizer_format="huggingface-tokenizer-json.v1"),
        task_contracts=["concept_mentions.v1"], score_kinds=["probability"], max_source_codepoints=16384,
        max_batch_items=16, max_windows=1, max_candidates=192, max_output_findings=512,
        runtime_requirements_sha256="b" * 64)
    adapters = [capability]
    if encoder:
        cap = copy.deepcopy(capability)
        cap["adapter"].update(adapter_id="encoder_topics", architecture="encoder-cls-v1",
            artifact_format="onnx-encoder-classification.v1")
        cap.update(task_contracts=["message_topics.v1"], score_kinds=["probability", "none"],
                   max_candidates=64, max_output_findings=64)
        adapters.append(cap)
    if instruction:
        cap = copy.deepcopy(capability)
        cap["adapter"].update(adapter_id="instruction_topics", architecture="llama-b11669-cpu-v1",
            artifact_format="gguf-model.v1", tokenizer_format="gguf-embedded.v1")
        cap.update(task_contracts=["message_topics.v1"], score_kinds=["none"], max_batch_items=1,
                   max_candidates=18, max_output_findings=18)
        adapters.append(cap)
    release_path = worker / "worker-release.json"
    release_path.write_bytes(msix.canonical_json(dict(schema="nlp-engine-release.v1", engine=engine,
                                                    artifacts=artifacts, adapters=adapters)))
    tool = source / "makeappx.exe"
    tool.write_bytes(b"synthetic SDK tool")
    notice = source / "LICENSE.txt"
    notice.write_bytes(b"Synthetic fixture notice.\n")
    receipt = source / "build.json"
    receipt.write_bytes(b'{"compiler":"synthetic","source_revision":"historical"}\n')
    logo = msix.CONFIG_ROOT / "Assets/StoreLogo.png"
    inputs = dict(schema="optional-engine-package-inputs/v1", package_profile=pin(profile_path),
        worker_release=pin(release_path), worker_directory=str(worker), logo=pin(logo),
        notices=[dict(file="runtime/LICENSE.txt", source=pin(notice))], makeappx=pin(tool), build_receipts=[pin(receipt)])
    inputs_path = source / "inputs.json"
    inputs_path.write_bytes(msix.canonical_json(inputs))
    return inputs_path, inputs, release_path, tmp_path / "output"


def rewrite(inputs_path, inputs, release_path=None, release=None):
    if release is not None:
        release_path.write_bytes(msix.canonical_json(release))
        inputs["worker_release"] = pin(release_path)
    inputs_path.write_bytes(msix.canonical_json(inputs))


def sdk_runner(commands, *, corrupt=None):
    def run(command, **kwargs):
        commands.append(command)
        assert kwargs["check"] is True
        assert not any(arg.lower() in ("/nv", "/o", "/no") for arg in command)
        output = Path(command[command.index("/p") + 1])
        if command[1] == "pack":
            stage = Path(command[command.index("/d") + 1])
            with zipfile.ZipFile(output, "w") as archive:
                for path in stage.rglob("*"):
                    if path.is_file():
                        archive.writestr(path.relative_to(stage).as_posix(), path.read_bytes())
                if corrupt == "extra":
                    archive.writestr("worker/unexpected.dll", b"extra")
        else:
            application = next(output.parent.glob("*.msix"))
            with zipfile.ZipFile(application) as archive:
                identity = ET.fromstring(archive.read("AppxManifest.xml")).find("{" + msix.FOUNDATION + "}Identity")
            namespace = "http://schemas.microsoft.com/appx/2013/bundle"
            root = ET.Element("{" + namespace + "}Bundle", SchemaVersion="5.0")
            ET.SubElement(root, "{" + namespace + "}Identity", **{k: identity.get(k) for k in ("Name", "Publisher", "Version")})
            packages = ET.SubElement(root, "{" + namespace + "}Packages")
            ET.SubElement(packages, "{" + namespace + "}Package", Type="application", Architecture="x64",
                          Version=identity.get("Version"), FileName=application.name)
            with zipfile.ZipFile(output, "w") as archive:
                archive.writestr("AppxMetadata/AppxBundleManifest.xml", ET.tostring(root))
                archive.writestr(application.name, application.read_bytes())
                if corrupt == "outer-extra":
                    archive.writestr("model.gguf", b"undeclared model")
                elif corrupt == "outer-duplicate":
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", UserWarning)
                        archive.writestr(application.name, application.read_bytes())
                elif corrupt == "outer-signature":
                    archive.writestr("AppxSignature.p7x", b"unexpected signature")
        if corrupt == "staged":
            (output.parent / "payload/worker/onnxruntime.dll").write_bytes(b"changed")
    return run


@pytest.mark.parametrize("instruction,encoder,expected", [(False, False, 2), (True, False, 12), (False, True, 2)])
def test_optional_engine_producer_packs_exact_variable_closure_and_provenance(tmp_path, instruction, encoder, expected):
    path, inputs, release_path, output = fixture(tmp_path, instruction=instruction, encoder=encoder)
    original = {item: item.read_bytes() for item in release_path.parent.iterdir()}
    commands = []
    result = producer.build(path, msix.sha256(path), output, runner=sdk_runner(commands))
    assert len(commands) == 2 and result["signed"] is False
    assert result["producer_sources"] == list(producer.EXECUTING_SOURCES)
    assert len([row for row in result["payload"] if row["file"].startswith("worker/")]) == expected + 1
    assert result["inputs"] == inputs
    assert json.loads((output / "producer-receipt.json").read_bytes()) == result
    assert {item: item.read_bytes() for item in release_path.parent.iterdir()} == original
    for row in result["payload"]:
        assert pin(output / "payload" / row["file"]) == dict(path=str(output / "payload" / row["file"]), **{k: row[k] for k in ("sha256", "size_bytes")})
    for row in result["outputs"]:
        assert msix.sha256(output / row["file"]) == row["sha256"]


@pytest.mark.parametrize("entities", [False, True])
@pytest.mark.parametrize("scores", [["probability"], ["none"], ["probability", "none"]])
def test_encoder_topic_metadata_accepts_exact_declaration_without_additional_runtimes(tmp_path, entities, scores):
    path, inputs, release_path, output = fixture(tmp_path, encoder=True)
    release = json.loads(release_path.read_bytes())
    release["adapters"][-1].update(score_kinds=scores, max_source_codepoints=131072, max_batch_items=16)
    if not entities:
        release["adapters"].pop(0)
    rewrite(path, inputs, release_path, release)
    assert producer.build(path, msix.sha256(path), output, validate_only=True)["status"] == "inputs-validated"
    assert {entry["file"] for entry in release["artifacts"]} == {"TextAnalysisWorker.exe", "onnxruntime.dll"}
    assert not output.exists()


@pytest.mark.parametrize("field,value", [
    ("adapter_id", "boundary_topics"), ("adapter_id", "boundary_entities"),
    ("adapter_version", "2"), ("adapter_build_sha256", "c" * 64), ("abi", "nlp-adapter.v2"),
    ("architecture", "boundary-shared-v1"), ("architecture", "llama-b11669-cpu-v1"),
    ("artifact_format", "onnx-boundary-classification.v1"), ("artifact_format", "onnx-boundary-entity.v1"),
    ("artifact_format", "gguf-model.v1"), ("tokenizer_format", "gguf-embedded.v1"),
    ("task_contracts", ["concept_mentions.v1"]), ("task_contracts", ["message_topics.v1", "concept_mentions.v1"]),
    ("task_contracts", ["message_topics.v1", "message_topics.v1"]), ("task_contracts", []),
    ("score_kinds", ["logit"]), ("score_kinds", ["margin"]),
    ("score_kinds", ["probability", "probability"]), ("score_kinds", []),
    ("max_source_codepoints", 131073), ("max_batch_items", 17), ("max_windows", 2),
    ("max_candidates", 65), ("max_output_findings", 65), ("max_candidates", 0),
])
def test_encoder_topic_metadata_refuses_substitutions_before_packing(tmp_path, field, value):
    path, inputs, release_path, output = fixture(tmp_path, encoder=True)
    release = json.loads(release_path.read_bytes())
    cap = release["adapters"][-1]
    (cap["adapter"] if field in cap["adapter"] else cap)[field] = value
    rewrite(path, inputs, release_path, release)
    called = []
    with pytest.raises(ValueError):
        producer.build(path, msix.sha256(path), output, runner=sdk_runner(called))
    assert not called and not output.exists()


def test_optional_engine_validation_creates_nothing(tmp_path):
    path, _, _, output = fixture(tmp_path)
    assert producer.build(path, msix.sha256(path), output, validate_only=True) == dict(
        status="inputs-validated", payload_files=6, output_created=False)
    assert not output.exists()


@pytest.mark.parametrize("mutation", ["extra-input", "missing-input", "substituted", "hardlink", "bad-pe",
    "duplicate-artifact", "case-alias", "extra-release", "stale-core", "worker-hash", "model-role", "unknown-runtime",
    "duplicate-adapter", "adapter-hash", "adapter-kind", "task", "scores", "bounds", "notice-path", "notice-duplicate",
    "notice-binary", "pin-size", "pin-hash", "repository-field", "existing-output", "output-in-source", "manifest-hash"])
def test_optional_engine_refuses_invalid_inputs_before_packing(tmp_path, mutation):
    path, inputs, release_path, output = fixture(tmp_path)
    release = json.loads(release_path.read_bytes())
    worker = release_path.parent
    if mutation == "extra-input": (worker / "extra.dll").write_bytes(pe("extra"))
    elif mutation == "missing-input": (worker / "onnxruntime.dll").unlink()
    elif mutation == "substituted": (worker / "onnxruntime.dll").write_bytes(pe("different"))
    elif mutation == "hardlink": os.link(worker / "onnxruntime.dll", tmp_path / "linked.dll")
    elif mutation == "bad-pe":
        binary = worker / "onnxruntime.dll"; binary.write_bytes(b"not PE")
        release["artifacts"][1]["artifact"].update(sha256=msix.sha256(binary), size_bytes=binary.stat().st_size)
    elif mutation == "duplicate-artifact": release["artifacts"].append(copy.deepcopy(release["artifacts"][1]))
    elif mutation == "case-alias": release["artifacts"][1]["file"] = "TEXTANALYSISWORKER.EXE"
    elif mutation == "extra-release": release["approval"] = True
    elif mutation == "stale-core": release["engine"]["core_package_version"] = "1.0.10.0"
    elif mutation == "worker-hash": release["engine"]["engine_build_sha256"] = "a" * 64
    elif mutation == "model-role": release["artifacts"][1]["artifact"]["role"] = "weights"
    elif mutation == "unknown-runtime": release["artifacts"][1]["file"] = "python313.dll"
    elif mutation == "duplicate-adapter": release["adapters"].append(copy.deepcopy(release["adapters"][0]))
    elif mutation == "adapter-hash": release["adapters"][0]["adapter"]["adapter_build_sha256"] = "c" * 64
    elif mutation == "adapter-kind": release["adapters"][0]["adapter"]["adapter_id"] = "custom"
    elif mutation == "task": release["adapters"][0]["task_contracts"] = ["message_topics.v1"]
    elif mutation == "scores": release["adapters"][0]["score_kinds"] = ["probability", "probability"]
    elif mutation == "bounds": release["adapters"][0]["max_windows"] = 257
    elif mutation == "notice-path": inputs["notices"][0]["file"] = "../escape.txt"
    elif mutation == "notice-duplicate": inputs["notices"].append(copy.deepcopy(inputs["notices"][0]))
    elif mutation == "notice-binary": inputs["notices"][0]["file"] = "model.gguf"
    elif mutation == "pin-size": inputs["logo"]["size_bytes"] += 1
    elif mutation == "pin-hash": inputs["makeappx"]["sha256"] = "a" * 64
    elif mutation == "repository-field": inputs["repository"] = str(tmp_path)
    elif mutation == "existing-output": output.mkdir()
    elif mutation == "output-in-source": output = worker / "new-output"
    rewrite(path, inputs, release_path, release)
    called = []
    with pytest.raises((ValueError, OSError)):
        producer.build(path, "0" * 64 if mutation == "manifest-hash" else msix.sha256(path), output,
                       runner=sdk_runner(called))
    assert not called and not (output / "producer-receipt.json").exists()


@pytest.mark.parametrize("mutation", ["missing-backend", "both-backends", "missing-crt", "runtime-format", "score", "topic-bound"])
def test_instruction_closure_matches_installed_admission(tmp_path, mutation):
    path, inputs, release_path, output = fixture(tmp_path, instruction=True)
    release = json.loads(release_path.read_bytes())
    if mutation in ("missing-backend", "missing-crt"):
        name = "ggml-cpu-alderlake.dll" if mutation == "missing-backend" else "msvcp140.dll"
        release["artifacts"] = [entry for entry in release["artifacts"] if entry["file"] != name]
        (release_path.parent / name).unlink()
    elif mutation == "both-backends":
        entry = copy.deepcopy(release["artifacts"][-1]); entry["file"] = "ggml-cpu-x64.dll"
        entry["artifact"]["artifact_id"] = "extra.backend"
        source = release_path.parent / release["artifacts"][-1]["file"]
        (release_path.parent / entry["file"]).write_bytes(source.read_bytes())
        release["artifacts"].append(entry)
    elif mutation == "runtime-format": release["artifacts"][-1]["artifact"]["format_id"] = "windows-pe-x64.v1"
    elif mutation == "score": release["adapters"][1]["score_kinds"] = ["probability"]
    elif mutation == "topic-bound": release["adapters"][1]["max_output_findings"] = 65
    rewrite(path, inputs, release_path, release)
    with pytest.raises(ValueError):
        producer.build(path, msix.sha256(path), output, validate_only=True)
    assert not output.exists()


@pytest.mark.parametrize("failure", ["pack", "bundle", "extra", "staged", "outer-extra", "outer-duplicate", "outer-signature"])
def test_optional_engine_sdk_failure_or_changed_bytes_never_emit_success(tmp_path, failure):
    path, _, _, output = fixture(tmp_path)
    commands = []
    sdk = sdk_runner(commands, corrupt=failure)
    def runner(command, **kwargs):
        if command[1] == failure:
            raise subprocess.CalledProcessError(1, command)
        sdk(command, **kwargs)
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        producer.build(path, msix.sha256(path), output, runner=runner)
    assert not (output / "producer-receipt.json").exists()


@pytest.mark.parametrize("field", ["package_profile", "worker_release"])
def test_pinned_metadata_reread_cannot_change_admitted_meaning(tmp_path, monkeypatch, field):
    path, inputs, _, output = fixture(tmp_path)
    target = Path(inputs[field]["path"])
    original = producer.checked
    calls = 0
    def checked(pin):
        nonlocal calls
        result = original(pin)
        if pin.path == str(target):
            calls += 1
            if calls == 2:
                value = json.loads(target.read_bytes())
                if field == "package_profile":
                    value["release_sequence"] = 10
                else:
                    value["engine"]["core_package_version"] = "1.0.10.0"
                target.write_bytes(msix.canonical_json(value))
        return result
    monkeypatch.setattr(producer, "checked", checked)
    with pytest.raises(ValueError, match="pin"):
        producer.build(path, msix.sha256(path), output, validate_only=True)
    assert not output.exists()


def test_staging_compares_original_pins_after_admission(tmp_path, monkeypatch):
    path, _, release_path, output = fixture(tmp_path)
    original = producer.plan
    def plan(*args):
        result = original(*args)
        (release_path.parent / "onnxruntime.dll").write_bytes(pe("changed after admission"))
        return result
    monkeypatch.setattr(producer, "plan", plan)
    commands = []
    with pytest.raises(ValueError, match="staged|pin changed"):
        producer.build(path, msix.sha256(path), output, runner=sdk_runner(commands))
    assert not commands and not (output / "producer-receipt.json").exists()


def test_packed_application_size_is_bounded_before_read(tmp_path, monkeypatch):
    path, _, _, output = fixture(tmp_path)
    commands = []
    sdk = sdk_runner(commands)
    def runner(command, **kwargs):
        sdk(command, **kwargs)
        if command[1] == "bundle":
            monkeypatch.setattr(producer, "MAX_APPLICATION", 1)
    with pytest.raises(ValueError, match="application exceeds"):
        producer.build(path, msix.sha256(path), output, runner=runner)
    assert not (output / "producer-receipt.json").exists()


def test_producer_source_identity_is_checked_before_staging(tmp_path, monkeypatch):
    path, _, _, output = fixture(tmp_path)
    records = copy.deepcopy(producer.EXECUTING_SOURCES)
    records[0]["sha256"] = "0" * 64
    monkeypatch.setattr(producer, "EXECUTING_SOURCES", records)
    with pytest.raises(ValueError, match="producer source"):
        producer.build(path, msix.sha256(path), output, validate_only=True)
    assert not output.exists()


@pytest.mark.parametrize("pinned", [False, True])
def test_document_growth_after_admission_uses_a_bounded_read(tmp_path, monkeypatch, pinned):
    path = tmp_path / "document.json"
    path.write_bytes(b"{}")
    declared = producer.FilePin.model_validate(pin(path))
    original = producer.checked if pinned else producer.regular
    def admitted(value):
        result = original(value)
        path.write_bytes(b" " * 100)
        return result
    monkeypatch.setattr(producer, "checked" if pinned else "regular", admitted)
    with pytest.raises(ValueError, match="bound"):
        if pinned:
            producer.pinned_bytes(declared, 8)
        else:
            producer.document(path, producer.PackageInputs, 8)


def test_growing_source_copy_stops_at_original_pinned_size(tmp_path, monkeypatch):
    source, destination = tmp_path / "source.bin", tmp_path / "copied.bin"
    original_bytes = b"pinned input"
    source.write_bytes(original_bytes)
    record = pin(source)
    original_open = Path.open
    reads = []
    class GrowingSource(io.BytesIO):
        def read(self, count=-1):
            assert 0 < count <= 1048576
            reads.append(count)
            return super().read(count)
    def open_path(path, *args, **kwargs):
        if path == source and args == ("rb",):
            return GrowingSource(original_bytes + b"x" * 1000000)
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", open_path)
    with pytest.raises(ValueError, match="pin changed while copying"):
        producer.copy_pinned(source, destination, record)
    assert reads == [len(original_bytes), 1]
    assert destination.read_bytes() == original_bytes


def test_duplicate_json_and_oversized_inputs_are_refused(tmp_path):
    path, _, _, output = fixture(tmp_path)
    original = path.read_bytes()
    path.write_bytes(original.replace(b'{', b'{"schema":"duplicate",', 1))
    with pytest.raises(ValueError, match="duplicate"):
        producer.build(path, msix.sha256(path), output, validate_only=True)
    path.write_bytes(b" " * (producer.MAX_JSON + 1))
    with pytest.raises(ValueError, match="bound"):
        producer.build(path, msix.sha256(path), output, validate_only=True)
