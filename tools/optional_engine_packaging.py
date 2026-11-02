"""Pack an exact prebuilt optional executable closure without signing or installing."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
from pathlib import Path, PurePosixPath
import re
import struct
import subprocess
import sys
from typing import Annotated, Literal
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pydantic import Field
from app.nlp.adapter import AdapterCapability
from app.nlp.contracts import Contract, Digest
from app.nlp.profiles import Artifact, EngineIdentity
from tools import msix_packaging as msix, optional_msix as optional

# The preparer path is fixed by this module, never by release inputs.
_spec = importlib.util.spec_from_file_location("optional_engine_worker_preparation", ROOT / "tools/nlp-worker/prepare.py")
_preparation = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_preparation)
refuse_alias = _preparation.refuse_alias

MAX_FILE = 1024 * 1024 * 1024
MAX_JSON = 2 * 1024 * 1024
MAX_APPLICATION = 512 * 1024 * 1024
MAX_BUNDLE = MAX_APPLICATION + 16 * 1024 * 1024
BoundedPath = Annotated[str, Field(min_length=1, max_length=32768)]
RUNTIME_FILES = {"onnxruntime.dll", "onnxruntime_providers_shared.dll", "msvcp140.dll",
                 "msvcp140_1.dll", "vcruntime140.dll", "vcruntime140_1.dll"}
INSTRUCTION_FILES = {"libomp.dll", "ggml-base.dll", "ggml.dll", "llama.dll",
                     "ggml-cpu-x64.dll", "ggml-cpu-alderlake.dll"}
PRODUCER_FILES = (
    "tools/optional_engine_packaging.py", "tools/optional_msix.py", "tools/msix_packaging.py",
    "tools/packaging_policy.py", "tools/nlp-worker/prepare.py", "app/nlp/contracts.py",
    "app/nlp/profiles.py", "app/nlp/adapter.py", "app/nlp/v2.py", "app/core/desktop_release.py",
    "packaging/msix/package-profile-v2.schema.json", "packaging/msix/direct-delivery.json",
    "shared/desktop-release/prerequisite-policy.json",
)
EXECUTING_SOURCES = tuple(dict(file=name, size_bytes=(ROOT / name).stat().st_size,
                              sha256=msix.sha256(ROOT / name)) for name in PRODUCER_FILES)


class FilePin(Contract):
    path: BoundedPath
    sha256: Digest
    size_bytes: Annotated[int, Field(ge=1, le=MAX_FILE)]


class NoticeInput(Contract):
    file: Annotated[str, Field(min_length=1, max_length=240)]
    source: FilePin


class PackageInputs(Contract):
    schema_: Literal["optional-engine-package-inputs/v1"] = Field(alias="schema")
    package_profile: FilePin
    worker_release: FilePin
    worker_directory: BoundedPath
    logo: FilePin
    notices: Annotated[tuple[NoticeInput, ...], Field(min_length=1, max_length=4096)]
    makeappx: FilePin
    build_receipts: Annotated[tuple[FilePin, ...], Field(max_length=32)]


class WorkerArtifact(Contract):
    file: Annotated[str, Field(min_length=1, max_length=120)]
    artifact: Artifact


class WorkerRelease(Contract):
    schema_: Literal["nlp-engine-release.v1"] = Field(alias="schema")
    engine: EngineIdentity
    artifacts: Annotated[tuple[WorkerArtifact, ...], Field(min_length=2, max_length=12)]
    adapters: Annotated[tuple[AdapterCapability, ...], Field(min_length=1, max_length=16)]


def regular(path: Path) -> Path:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("absolute ordinary input path required")
    refuse_alias(path)
    if not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("ordinary unaliased input file required")
    return path


def checked(pin: FilePin) -> Path:
    path = regular(Path(pin.path))
    if path.stat().st_size != pin.size_bytes:
        raise ValueError("input bytes differ from their pin")
    digest = hashlib.sha256()
    remaining = pin.size_bytes + 1
    with path.open("rb") as stream:
        while remaining:
            block = stream.read(min(1048576, remaining))
            if not block:
                break
            digest.update(block)
            remaining -= len(block)
    if remaining != 1 or digest.hexdigest() != pin.sha256:
        raise ValueError("input bytes differ from their pin")
    return path


def read_bounded(path: Path, maximum: int) -> bytes:
    regular(path)
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if not 0 < len(raw) <= maximum:
        raise ValueError("file exceeds its read bound")
    return raw


def document(path: Path, model, maximum=MAX_JSON):
    raw = read_bounded(path, maximum)
    optional._decode_object(raw, maximum)
    return model.model_validate_json(raw), raw


def pinned_bytes(pin: FilePin, maximum: int) -> bytes:
    if pin.size_bytes > maximum:
        raise ValueError("pinned document exceeds its bound")
    path = checked(pin)
    raw = read_bounded(path, min(maximum, pin.size_bytes))
    if len(raw) != pin.size_bytes or hashlib.sha256(raw).hexdigest() != pin.sha256:
        raise ValueError("input bytes differ from their pin")
    return raw


def member(name: str) -> str:
    path = PurePosixPath(name)
    if (path.is_absolute() or path.as_posix() != name or not path.parts
            or any(not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_. -]*", part)
                   or part.endswith((".", " ")) or part in (".", "..")
                   or part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}
                   for part in path.parts)):
        raise ValueError("canonical payload member required")
    return name


def pe_x64(path: Path) -> None:
    with path.open("rb") as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            raise ValueError("worker/runtime must be x64 PE files")
        offset = struct.unpack_from("<I", header, 60)[0]
        if offset < 64 or offset > 1048576:
            raise ValueError("invalid PE header offset")
        stream.seek(offset)
        if stream.read(6) != b"PE\0\0\x64\x86":
            raise ValueError("worker/runtime must be x64 PE files")


def file_record(path: Path, name: str) -> dict:
    return dict(file=name, size_bytes=path.stat().st_size, sha256=msix.sha256(path))


def verify_producer_sources() -> None:
    for record in EXECUTING_SOURCES:
        path = regular(ROOT / record["file"])
        if file_record(path, record["file"]) != record:
            raise ValueError("executing producer source changed")


def validate_capabilities(release: WorkerRelease, names: set[str]) -> None:
    """Match installed-owner metadata admission; this does not inspect compiled code."""
    identities = set()
    instruction = False
    for cap in release.adapters:
        adapter = cap.adapter
        key = (adapter.adapter_id, adapter.adapter_version)
        if key in identities:
            raise ValueError("duplicate worker adapter")
        identities.add(key)
        is_instruction = adapter.adapter_id == "instruction_topics"
        is_encoder = adapter.adapter_id == "encoder_topics"
        instruction |= is_instruction
        formats = {"boundary_entities": ("onnx-boundary-entity.v1", "concept_mentions.v1"),
                   "boundary_topics": ("onnx-boundary-classification.v1", "message_topics.v1"),
                   "encoder_topics": ("onnx-encoder-classification.v1", "message_topics.v1"),
                   "instruction_topics": ("gguf-model.v1", "message_topics.v1")}
        declaration = formats.get(adapter.adapter_id)
        if declaration is None:
            raise ValueError("unsupported compiled adapter declaration")
        format_id, task = declaration
        topics = task == "message_topics.v1"
        if (adapter.adapter_version != "1" or adapter.adapter_build_sha256 != release.engine.engine_build_sha256
                or adapter.artifact_format != format_id or cap.task_contracts != (task,)
                or adapter.architecture != ("llama-b11669-cpu-v1" if is_instruction else
                                             "encoder-cls-v1" if is_encoder else "boundary-shared-v1")
                or adapter.tokenizer_format != ("gguf-embedded.v1" if is_instruction else "huggingface-tokenizer-json.v1")):
            raise ValueError("compiled adapter declaration differs from installed policy")
        allowed = {"none"} if is_instruction else {"probability", "none"} if topics else {"probability", "logit", "margin", "none"}
        if (not set(cap.score_kinds) <= allowed or cap.max_windows > (1 if topics else 256)
                or cap.max_candidates > (64 if topics else 8192)
                or cap.max_output_findings > (64 if topics else 576)):
            raise ValueError("adapter capability exceeds installed policy")
    if instruction:
        if (not RUNTIME_FILES <= names or not (INSTRUCTION_FILES - {"ggml-cpu-x64.dll", "ggml-cpu-alderlake.dll"}) <= names
                or len(names & {"ggml-cpu-x64.dll", "ggml-cpu-alderlake.dll"}) != 1):
            raise ValueError("instruction runtime closure differs")
    elif names & INSTRUCTION_FILES:
        raise ValueError("undeclared instruction runtime")


def fresh_output(output: Path, sources: list[Path]) -> None:
    if not output.is_absolute() or ".." in output.parts:
        raise ValueError("fresh absolute external output required")
    refuse_alias(output)
    if output.exists() or output.is_relative_to(ROOT) or any(
            output.is_relative_to(source) or source.is_relative_to(output) for source in sources):
        raise ValueError("fresh absolute external output required")


def plan(inputs_path: Path, expected_sha256: str, output: Path) -> dict:
    verify_producer_sources()
    regular(inputs_path)
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("input manifest pin differs")
    inputs, raw = document(inputs_path, PackageInputs)
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("input manifest pin differs")
    pins = [inputs.package_profile, inputs.worker_release, inputs.logo, inputs.makeappx,
            *inputs.build_receipts, *(entry.source for entry in inputs.notices)]
    paths = [checked(pin) for pin in pins]
    profile = msix.validate_profile(optional._decode_object(pinned_bytes(inputs.package_profile, 65536), 65536))
    if (profile["schema"] != "ofca-msix-package-profile/v2"
            or profile["distribution"] != "direct-appinstaller"
            or profile["servicing"]["installed_artifact"]["kind"] != "related_bundle"):
        raise ValueError("optional Engine requires a v2 direct related profile")
    release_raw = pinned_bytes(inputs.worker_release, 1048576)
    optional._decode_object(release_raw, 1048576)
    release = WorkerRelease.model_validate_json(release_raw)
    engine = release.engine
    optional.validate_identity(engine)
    relation = optional._release_relation(profile)
    expected = dict(package_name=relation.package_name, package_family=relation.package_family,
                    package_version=relation.package_version, publisher=relation.publisher,
                    core_package_name=relation.core_package_name, core_package_version=relation.core_package_version,
                    related_set_id=profile["servicing"]["installed_artifact"]["related_set_id"])
    if any(getattr(engine, key) != value for key, value in expected.items()):
        raise ValueError("worker release differs from the selected Core/Engine relation")
    directory = Path(inputs.worker_directory)
    if not directory.is_absolute() or ".." in directory.parts:
        raise ValueError("absolute worker directory required")
    refuse_alias(directory)
    if not directory.is_dir() or Path(inputs.worker_release.path) != directory / "worker-release.json":
        raise ValueError("worker metadata must belong to the exact worker directory")
    sources = [inputs_path, directory, *paths]
    fresh_output(output, sources)
    names, ids, payload, inventory = set(), set(), {}, []
    def payload_member(name, path, sha256, size_bytes):
        payload[name] = path
        inventory.append(dict(file=name, sha256=sha256, size_bytes=size_bytes))
    for entry in release.artifacts:
        name = member(entry.file)
        artifact = entry.artifact
        if ("/" in name or name.casefold() in names or artifact.artifact_id in ids
                or artifact.role not in ("engine", "runtime")
                or name not in {"TextAnalysisWorker.exe"} | RUNTIME_FILES | INSTRUCTION_FILES
                or (artifact.role == "engine" and name != "TextAnalysisWorker.exe")
                or (artifact.role == "runtime" and not name.endswith(".dll"))
                or (name in INSTRUCTION_FILES and artifact.format_id != "llama-b11669-cpu.dll.v1")):
            raise ValueError("worker artifact closure differs")
        names.add(name.casefold()); ids.add(artifact.artifact_id)
        path = checked(FilePin(path=str(directory / name), sha256=artifact.sha256, size_bytes=artifact.size_bytes))
        pe_x64(path)
        payload_member("worker/" + name, path, artifact.sha256, artifact.size_bytes)
    workers = [entry.artifact for entry in release.artifacts if entry.artifact.role == "engine"]
    if len(workers) != 1 or workers[0].sha256 != engine.engine_build_sha256 or "onnxruntime.dll" not in names:
        raise ValueError("worker executable differs from Engine build identity")
    observed = {path.name for path in directory.iterdir()}
    if observed != {entry.file for entry in release.artifacts} | {"worker-release.json"}:
        raise ValueError("worker directory contains missing or extra members")
    validate_capabilities(release, {entry.file for entry in release.artifacts})
    payload_member("worker/worker-release.json", paths[1], inputs.worker_release.sha256, inputs.worker_release.size_bytes)
    payload_member("Assets/StoreLogo.png", paths[2], inputs.logo.sha256, inputs.logo.size_bytes)
    with paths[2].open("rb") as stream:
        logo_header = stream.read(8)
    if logo_header != b"\x89PNG\r\n\x1a\n":
        raise ValueError("package logo must be PNG")
    for entry in inputs.notices:
        name = "Notices/" + member(entry.file)
        if (PurePosixPath(name).suffix.lower() not in ("", ".txt", ".md", ".json", ".html", ".htm", ".rst", ".license", ".docx")
                and not re.fullmatch(r"license(?:-[A-Za-z0-9.-]+)?", PurePosixPath(name).name, re.IGNORECASE)):
            raise ValueError("notice must be a document")
        if name.casefold() in {item.casefold() for item in payload}:
            raise ValueError("duplicate notice member")
        payload_member(name, Path(entry.source.path), entry.source.sha256, entry.source.size_bytes)
    # Reject file/directory collisions before creating a staging directory.
    folded = {name.casefold() for name in payload}
    if any(parent.as_posix().casefold() in folded for name in payload for parent in PurePosixPath(name).parents if str(parent) != "."):
        raise ValueError("payload file/directory collision")
    manifest = optional.optional_manifest(engine, minimum_os=profile["minimum_os_version"],
                                           tested_os=profile["maximum_os_version_tested"])
    inventory.append(dict(file="AppxManifest.xml", size_bytes=len(manifest), sha256=hashlib.sha256(manifest).hexdigest()))
    if sum(record["size_bytes"] for record in inventory) > MAX_APPLICATION:
        raise ValueError("Engine payload exceeds its bound")
    return dict(inputs=inputs, raw=raw, profile=profile, engine=engine, payload=payload,
                manifest=manifest, inventory=sorted(inventory, key=lambda item: item["file"]),
                pins=pins, sources=sources, release_raw=release_raw)


def verify_payload(directory: Path, inventory: list[dict]) -> None:
    actual = []
    for path in directory.rglob("*"):
        refuse_alias(path)
        if path.is_file():
            regular(path)
            actual.append(file_record(path, path.relative_to(directory).as_posix()))
    if sorted(actual, key=lambda item: item["file"]) != inventory:
        raise ValueError("staged payload differs from the pinned inventory")


def copy_pinned(source: Path, destination: Path, record: dict) -> None:
    regular(source)
    remaining = record["size_bytes"]
    digest = hashlib.sha256()
    with source.open("rb") as source_stream, destination.open("xb") as destination_stream:
        while remaining:
            block = source_stream.read(min(1048576, remaining))
            if not block:
                raise ValueError("source pin changed while copying")
            destination_stream.write(block)
            digest.update(block)
            remaining -= len(block)
        if source_stream.read(1) or digest.hexdigest() != record["sha256"]:
            raise ValueError("source pin changed while copying")


def verify_application(raw: bytes, inventory: list[dict]) -> None:
    expected = {record["file"]: record for record in inventory}
    seen = set()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for entry in archive.infolist():
            name = msix.installed_archive_path(entry.filename)
            if name.casefold() in seen:
                raise ValueError("duplicate packed member")
            seen.add(name.casefold())
            if entry.is_dir() or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("aliased packed member")
            if name in msix.FOOTPRINT_FILES:
                if name == "AppxSignature.p7x":
                    raise ValueError("producer output must remain unsigned")
                continue
            record = expected.pop(name, None)
            if record is None or entry.file_size != record["size_bytes"]:
                raise ValueError("packed payload differs from inventory")
            if hashlib.sha256(archive.read(entry)).hexdigest() != record["sha256"]:
                raise ValueError("packed payload differs from inventory")
    if expected:
        raise ValueError("packed payload is incomplete")


def verify_bundle(bundle: Path, application: Path, profile: dict) -> None:
    if not 0 < bundle.stat().st_size <= MAX_BUNDLE:
        raise ValueError("packed bundle exceeds its bound")
    manifest_name = "AppxMetadata/AppxBundleManifest.xml"
    permitted = msix.FOOTPRINT_FILES | {application.name, manifest_name}
    seen = set()
    with zipfile.ZipFile(bundle) as archive:
        for entry in archive.infolist():
            if (entry.filename not in permitted or entry.filename in seen or entry.is_dir()
                    or entry.filename == "AppxSignature.p7x"
                    or (entry.external_attr >> 16) & 0o170000 == 0o120000
                    or entry.file_size > MAX_BUNDLE):
                raise ValueError("bundle contains an undeclared or aliased member")
            seen.add(entry.filename)
        if not {application.name, manifest_name} <= seen:
            raise ValueError("bundle is incomplete")
        entry = archive.getinfo(application.name)
        if entry.file_size != application.stat().st_size:
            raise ValueError("bundle application differs from its packed input")
        with archive.open(entry) as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != msix.sha256(application):
            raise ValueError("bundle application differs from its packed input")
    optional.verify_engine_bundle(bundle, profile)


def build(inputs_path: Path, expected_sha256: str, output: Path, *, validate_only=False, runner=subprocess.run) -> dict:
    prepared = plan(inputs_path, expected_sha256, output)
    if validate_only:
        return dict(status="inputs-validated", payload_files=len(prepared["inventory"]), output_created=False)
    output.mkdir(parents=True, exist_ok=False)
    stage = output / "payload"
    stage.mkdir()
    expected_payload = {record["file"]: record for record in prepared["inventory"]}
    for name, source in prepared["payload"].items():
        destination = stage / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        copy_pinned(source, destination, expected_payload[name])
    (stage / "AppxManifest.xml").write_bytes(prepared["manifest"])
    verify_payload(stage, prepared["inventory"])
    version = prepared["engine"].package_version
    application = output / optional.engine_application_filename(version)
    bundle = output / optional.artifact_filename("engine", version)
    mapping = output / "bundle-mapping.txt"
    mapping.write_text('[Files]\n"' + application.name + '" "' + application.name + '"\n', encoding="utf-8")
    commands = [[prepared["inputs"].makeappx.path, "pack", "/d", str(stage), "/p", str(application)],
                [prepared["inputs"].makeappx.path, "bundle", "/f", str(mapping), "/p", str(bundle), "/bv", version]]
    for index, command in enumerate(commands):
        for pin in prepared["pins"]:
            checked(pin)
        verify_payload(stage, prepared["inventory"])
        with (output / f"makeappx-{index}.log").open("x", encoding="utf-8") as log:
            runner(command, cwd=output, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=300)
        regular(application if index == 0 else bundle)
    verify_payload(stage, prepared["inventory"])
    if not 0 < application.stat().st_size <= MAX_APPLICATION:
        raise ValueError("packed application exceeds its bound")
    verify_application(read_bounded(application, MAX_APPLICATION), prepared["inventory"])
    verify_bundle(bundle, application, prepared["profile"])
    for pin in prepared["pins"]:
        checked(pin)
    # Recheck binary inputs and exact directory membership after packing as well.
    plan(inputs_path, expected_sha256, output / "input-recheck")
    verify_producer_sources()
    receipt = dict(schema="optional-engine-package-build/v1", signed=False,
                   inputs_sha256=expected_sha256, inputs=prepared["inputs"].model_dump(mode="json", by_alias=True),
                   engine=prepared["engine"].model_dump(mode="json"), payload=prepared["inventory"],
                   producer_sources=list(EXECUTING_SOURCES),
                   outputs=[file_record(path, path.name) for path in (application, bundle)], commands=commands)
    with (output / "producer-receipt.json").open("xb") as stream:
        stream.write(msix.canonical_json(receipt))
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--inputs-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    try:
        result = build(args.inputs, args.inputs_sha256, args.output, validate_only=args.validate_only)
    except (ValueError, OSError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
        print(json.dumps(dict(success=False, error=str(error))), file=sys.stderr)
        raise SystemExit(2) from error
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
