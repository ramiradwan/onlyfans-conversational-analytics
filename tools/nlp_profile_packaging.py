"""Prepare unsigned profile metadata and data, without qualifying or executing models.

Use --inputs, --inputs-sha256 and a fresh absolute --output directory. The closed
nlp-profile-package-inputs/v1 document pins profile/worker_release files and an
artifacts list of {artifact_id, source: {path, size_bytes, sha256}}, and declares
maximum_output_bytes and minimum_free_bytes. --validate-only does not copy files.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import ctypes
import hashlib
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Annotated, Literal

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pydantic import Field
from app.nlp.adapter import (HandshakeRequest, ProfileCompatibility, WorkerHandshake,
                             validate_compatibility, validate_handshake)
from app.nlp.contracts import Contract, Digest, Identifier
from app.nlp.child_protocol import BootstrapRequest, encode_envelope
from app.nlp.presentation import load_resolver, MAX_CATALOG_BYTES
from app.nlp.profile_authentication import MAX_PROFILE_BYTES
from app.nlp.profiles import MAX_SAFE_INTEGER, canonical_bytes, read_profile
from app.nlp.release_catalog import ReleaseEntry, canonical
from app.persistence.private_files import (apply_private_directory_security,
    apply_private_file_security, sync_directory, reject_path_aliases, PrivateFileSecurityError)
from app.security.signed_objects import parse
from tools.optional_engine_packaging import (WorkerRelease, INSTRUCTION_FILES,
    RUNTIME_FILES, member, regular as engine_regular, validate_capabilities)

DATA_ROLES = {"weights", "tokenizer", "configuration"}
CHUNK_BYTES = 1024 * 1024
MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
PositiveBytes = Annotated[int, Field(ge=1, le=MAX_SAFE_INTEGER)]


class FilePin(Contract):
    path: Annotated[str, Field(min_length=1, max_length=32768)]
    size_bytes: PositiveBytes
    sha256: Digest


class ArtifactInput(Contract):
    artifact_id: Identifier
    source: FilePin


class PackageInputs(Contract):
    schema_: Literal["nlp-profile-package-inputs/v1"] = Field(alias="schema")
    profile: FilePin
    worker_release: FilePin
    artifacts: Annotated[tuple[ArtifactInput, ...], Field(min_length=1, max_length=256)]
    maximum_output_bytes: PositiveBytes
    minimum_free_bytes: PositiveBytes


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def refuse_alias(path):
    try:
        reject_path_aliases(path)
    except PrivateFileSecurityError as error:
        raise ValueError("aliased source or target") from error


def regular(path):
    refuse_alias(path)
    return engine_regular(path)


def _identity(value):
    # Windows path stat and CRT fstat can report different creation timestamps.
    # Byte pins, stable file IDs, size and modification time are checked on both.
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
            value.st_ctime_ns if os.name != "nt" else 0, value.st_nlink)


class Source:
    """Retain the opened identity; every copy and final check verifies its pin."""
    def __init__(self, pin, stack):
        self.pin = pin
        self.path = regular(Path(pin.path))
        before = self.path.stat()
        self.stream = stack.enter_context(self.path.open("rb"))
        self.identity = _identity(os.fstat(self.stream.fileno()))
        if _identity(before) != self.identity or before.st_size != pin.size_bytes:
            raise ValueError("input identity or size differs from pin")

    def check_identity(self):
        regular(self.path)
        if (_identity(self.path.stat()) != self.identity
                or _identity(os.fstat(self.stream.fileno())) != self.identity):
            raise ValueError("input changed during preparation")

    def transfer(self, target=None, collect=False, progress=None):
        self.check_identity()
        self.stream.seek(0)
        remaining, hashed, blocks = self.pin.size_bytes, hashlib.sha256(), []
        while remaining:
            block = self.stream.read(min(CHUNK_BYTES, remaining))
            if not block:
                raise ValueError("input bytes differ from pin")
            hashed.update(block)
            remaining -= len(block)
            if collect:
                blocks.append(block)
            if target is not None:
                target.write(block)
            if progress is not None:
                progress(len(block))
        if self.stream.read(1) or hashed.hexdigest() != self.pin.sha256:
            raise ValueError("input bytes differ from pin")
        self.check_identity()
        return b"".join(blocks) if collect else None

    def document(self, maximum):
        if self.pin.size_bytes > maximum:
            raise ValueError("document exceeds byte limit")
        raw = self.transfer(collect=True)
        parse(raw, maximum=maximum)
        return raw


def _no_local_paths(value):
    if isinstance(value, dict):
        for item in value.values():
            _no_local_paths(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _no_local_paths(item)
    elif isinstance(value, str) and (value.startswith(("/", "\\"))
            or re.search(r"[A-Za-z]:[\\/]", value)):
        raise ValueError("local paths are not distributable metadata")


def _release_closure(profile, release):
    actual = {asset.artifact_id: asset for asset in profile.artifacts
              if asset.role in {"engine", "runtime"}}
    expected, names = {}, set()
    for entry in release.artifacts:
        name, asset = member(entry.file), entry.artifact
        if ("/" in name or name.casefold() in names or asset.artifact_id in expected
                or name not in {"TextAnalysisWorker.exe"} | RUNTIME_FILES | INSTRUCTION_FILES):
            raise ValueError("worker artifact closure differs")
        names.add(name.casefold())
        expected[asset.artifact_id] = asset
        if (asset.role not in {"engine", "runtime"}
                or (asset.role == "engine") != (name == "TextAnalysisWorker.exe")
                or (asset.role == "runtime" and not name.endswith(".dll"))
                or (name in INSTRUCTION_FILES and asset.format_id != "llama-b11669-cpu.dll.v1")):
            raise ValueError("worker artifact closure differs")
    workers = [asset for asset in expected.values() if asset.role == "engine"]
    if (actual != expected or len(workers) != 1
            or workers[0].sha256 != release.engine.engine_build_sha256
            or "onnxruntime.dll" not in names):
        raise ValueError("profile differs from worker release closure")
    validate_capabilities(release, {entry.file for entry in release.artifacts})
    request = HandshakeRequest(request_id="metadata.compatibility", expected_engine=release.engine)
    declaration = WorkerHandshake(request_id=request.request_id, engine=release.engine, adapters=release.adapters)
    validate_handshake(profile, request, canonical(declaration))


def _data_formats(profile, configurations):
    """Check known adapter data declarations; native semantic validation is later."""
    assets = {asset.artifact_id: asset for asset in profile.artifacts}
    for component in profile.components:
        adapter = component.adapter.adapter_id
        ids = set(component.artifact_ids)
        members = [assets[key] for key in ids]
        config_formats = {
            "boundary_entities": {"boundary-entity-config.v1", "boundary-entity-config.v2"},
            "boundary_topics": {"boundary-topic-config.v1"},
            "encoder_topics": {"encoder-topic-config.v1"},
            "instruction_topics": {"instruction-topic-config.v1", "instruction-topic-config.v2"},
        }[adapter]
        configs = [asset for asset in members if asset.format_id in config_formats and asset.role == "configuration"]
        if len(configs) != 1:
            raise ValueError("one supported adapter configuration required")
        config = configs[0]
        value = parse(configurations[config.artifact_id], maximum=MAX_CATALOG_BYTES)
        if type(value) is not dict or value.get("schema") != config.format_id:
            raise ValueError("adapter configuration schema differs")
        required = {"schema", "labels", "policies"}
        optional = set()
        if adapter == "instruction_topics":
            required |= {"model", "system_prompt", "grammar", "runtime", "context_tokens",
                         "output_tokens", "inference_threads"}
        else:
            required |= {"tokenizer", "decoding", "graph" if adapter == "encoder_topics" else "graphs"}
            if adapter == "boundary_topics":
                required.add("task_name")
                optional |= {"instruction", "groups"}
        if config.format_id.endswith(".v2"):
            required.add("presentation_catalog")
        if not required <= value.keys() or value.keys() - required - optional:
            raise ValueError("adapter configuration fields differ")
        expected = {config.artifact_id: ("configuration", config.format_id)}

        def reference(key, role, formats):
            if type(key) is not str or key not in ids or key in expected:
                raise ValueError("adapter data reference differs")
            asset = assets[key]
            if asset.role != role or asset.format_id not in formats:
                raise ValueError("unsupported adapter data format")
            expected[key] = (asset.role, asset.format_id)

        if adapter == "instruction_topics":
            for field, fmt, role in (("model", "gguf-model.v1", "weights"),
                    ("system_prompt", "instruction-system-utf8.v1", "configuration"),
                    ("grammar", "llama-gbnf.v1", "configuration")):
                reference(value.get(field), role, {fmt})
        else:
            reference(value.get("tokenizer"), "tokenizer", {"huggingface-tokenizer-json.v1"})
            if adapter == "encoder_topics":
                reference(value.get("graph"), "weights", {"onnx-encoder-classification.v1"})
            else:
                graphs = value.get("graphs")
                graph_names = {"encoder", "marginals", "scorer"} if adapter == "boundary_entities" else {"encoder", "classifier"}
                if type(graphs) is not dict or set(graphs) != graph_names:
                    raise ValueError("adapter graph membership differs")
                for name, key in graphs.items():
                    formats = {component.adapter.artifact_format}
                    if adapter == "boundary_topics" and name == "encoder":
                        formats.add("onnx-boundary-entity.v1")
                    reference(key, "weights", formats)
        if "presentation_catalog" in value:
            if not config.format_id.endswith(".v2"):
                raise ValueError("unsupported presentation configuration")
            reference(value["presentation_catalog"], "configuration", {"nlp-presentation.v1"})
        if set(expected) != ids:
            raise ValueError("adapter data closure differs")


def _output_path(output, sources):
    if not output.is_absolute() or ".." in output.parts:
        raise ValueError("fresh absolute external output required")
    refuse_alias(output)
    if (output.exists() or not output.parent.is_dir() or output.is_relative_to(ROOT)
            or any(output.is_relative_to(source.path) or source.path.is_relative_to(output)
                   for source in sources)):
        raise ValueError("output exists or overlaps inputs")


def _exclusive_publish(stage, output):
    if os.name == "nt":
        os.rename(stage, output)  # Windows refuses an existing destination.
    elif sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        rename = getattr(libc, "renameat2", None)
        if rename is None:
            raise ValueError("exclusive directory publication unavailable")
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(stage), -100, os.fsencode(output), 1) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
    else:
        raise ValueError("exclusive directory publication unavailable")


def prepare(inputs_path: Path, expected_sha256: str, output: Path, *, validate_only=False):
    """Verify a closed input set; optionally materialize an unsigned data directory."""
    regular(inputs_path)
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("SHA256 pin required")
    with ExitStack() as stack:
        sources = []
        def open_source(pin):
            source = Source(pin, stack)
            sources.append(source)
            return source
        manifest = open_source(FilePin(path=str(inputs_path), size_bytes=inputs_path.stat().st_size,
                                       sha256=expected_sha256))
        inputs = PackageInputs.model_validate_json(manifest.document(MAX_DOCUMENT_BYTES))
        profile_source = open_source(inputs.profile)
        profile_raw = profile_source.document(MAX_PROFILE_BYTES)
        profile = read_profile(profile_raw)
        if canonical_bytes(profile) != profile_raw:
            raise ValueError("canonical profile required")
        encode_envelope("bootstrap", BootstrapRequest(channel_nonce="0" * 32,
            profile_json=profile_raw.decode("ascii"), artifact_ids=tuple(a.artifact_id for a in profile.artifacts)))
        _no_local_paths(profile.model_dump(mode="json"))
        release_source = open_source(inputs.worker_release)
        release = WorkerRelease.model_validate_json(release_source.document(MAX_DOCUMENT_BYTES))
        mapped = {entry.artifact_id: entry.source for entry in inputs.artifacts}
        assets = {asset.artifact_id: asset for asset in profile.artifacts}
        if len(mapped) != len(inputs.artifacts) or mapped.keys() != assets.keys():
            raise ValueError("artifact mapping must match the complete profile")
        artifact_sources, configurations = {}, {}
        for key, asset in assets.items():
            pin = mapped[key]
            if (pin.sha256, pin.size_bytes) != (asset.sha256, asset.size_bytes):
                raise ValueError("artifact pin differs from profile")
            source = artifact_sources[key] = open_source(pin)
            if asset.role == "configuration":
                if asset.size_bytes > MAX_CATALOG_BYTES:
                    raise ValueError("configuration exceeds byte limit")
                # Grammar/system text is not JSON; the resolver only reads JSON members.
                configurations[key] = source.transfer(collect=True)
        identities = [(source.identity[0], source.identity[1]) for source in sources]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate source file identity")
        _output_path(output, sources)
        _release_closure(profile, release)
        _data_formats(profile, configurations)
        if load_resolver(profile, configurations.__getitem__) is None:
            raise ValueError("complete readable presentation required")
        data = sorted((asset for asset in profile.artifacts if asset.role in DATA_ROLES),
                      key=lambda asset: asset.artifact_id)
        installed_receipt = canonical([asset.model_dump(mode="json") for asset in data])
        data_bytes = sum(asset.size_bytes for asset in data)
        all_bytes = sum(asset.size_bytes for asset in profile.artifacts)
        accounting = dict(data_bytes=data_bytes, artifact_bytes=all_bytes,
            installed_receipt_bytes=len(installed_receipt),
            minimum_installed_bytes=all_bytes + len(installed_receipt),
            minimum_temporary_bytes=2 * data_bytes + len(installed_receipt))
        if (profile.resources.download_bytes < all_bytes
                or profile.resources.installed_bytes < accounting["minimum_installed_bytes"]
                or profile.resources.temporary_bytes < accounting["minimum_temporary_bytes"]):
            raise ValueError("declared resources omit complete closure or cold staging")
        compatibility = ProfileCompatibility(profile_sha256=profile.identity_digest(),
            compatible_engines=(release.engine,), adapters=tuple(c.adapter for c in profile.components))
        validate_compatibility(profile, compatibility)
        entry = ReleaseEntry(profile_id=profile.profile_id, revision=profile.revision,
            profile_sha256=profile.identity_digest(), compatibility=compatibility)
        payload = {"profile.json": profile_raw, "release-entry.json": canonical(entry)}
        copies = {}
        for asset in data:
            name = "data/" + asset.sha256
            if name in copies and copies[name].pin.size_bytes != asset.size_bytes:
                raise ValueError("content-addressed size conflict")
            copies[name] = artifact_sources[asset.artifact_id]
        records = [dict(file=name, size_bytes=len(raw), sha256=digest(raw)) for name, raw in payload.items()]
        records += [dict(file=name, size_bytes=source.pin.size_bytes, sha256=source.pin.sha256)
                    for name, source in copies.items()]
        inventory = dict(schema="nlp-profile-data-inventory/v1", signed=False, approved=False,
                         files=sorted(records, key=lambda row: row["file"]))
        payload["inventory.json"] = canonical(inventory)
        receipt = dict(schema="nlp-profile-preparation-receipt/v1", signed=False, approved=False,
            validation="unsigned-structural-preparation", runtime_executed=False,
            inputs_sha256=expected_sha256,
            profile_sha256=profile.identity_digest(), worker_release_sha256=inputs.worker_release.sha256,
            inventory_sha256=digest(payload["inventory.json"]), accounting=accounting)
        payload["producer-receipt.json"] = canonical(receipt)
        output_bytes = sum(len(raw) for raw in payload.values()) + sum(s.pin.size_bytes for s in copies.values())
        if output_bytes > inputs.maximum_output_bytes:
            raise ValueError("output exceeds explicit byte limit")
        def space(remaining):
            if shutil.disk_usage(output.parent).free < inputs.minimum_free_bytes + remaining:
                raise ValueError("insufficient free space for output and reserve")
        space(output_bytes)
        if validate_only:
            for source in sources:
                source.transfer()
            return receipt
        # Executable members are verified in place, never copied into the data output.
        for key, asset in assets.items():
            if asset.role not in DATA_ROLES:
                artifact_sources[key].transfer()
        stage = Path(tempfile.mkdtemp(prefix="." + output.name + ".stage-", dir=output.parent))
        owned = {}
        stage_identity = _identity(stage.stat())[:2]
        directories = {stage: stage_identity}
        try:
            apply_private_directory_security(stage)
            (stage / "data").mkdir(mode=0o700)
            apply_private_directory_security(stage / "data")
            data_identity = _identity((stage / "data").stat())[:2]
            directories[stage / "data"] = data_identity
            remaining = output_bytes
            def progress(size):
                nonlocal remaining
                remaining -= size
                space(remaining)
            def write(name, raw=None, source=None):
                path = stage / name
                with path.open("xb") as target:
                    owned[path] = _identity(os.fstat(target.fileno()))[:2]
                    apply_private_file_security(path)
                    if source is not None:
                        source.transfer(target, progress=progress)
                    else:
                        target.write(raw)
                        progress(len(raw))
                    target.flush()
                    os.fsync(target.fileno())
            for name, source in sorted(copies.items()):
                write(name, source=source)
            for name, raw in payload.items():
                write(name, raw=raw)
            for source in sources:
                source.transfer()
            refuse_alias(stage)
            refuse_alias(stage / "data")
            if (_identity(stage.stat())[:2] != stage_identity
                    or _identity((stage / "data").stat())[:2] != data_identity
                    or {path.name for path in stage.iterdir()} != set(payload) | {"data"}
                    or {"data/" + path.name for path in (stage / "data").iterdir()} != copies.keys()):
                raise ValueError("staged directory membership differs")
            complete_records = records + [dict(file=name, size_bytes=len(payload[name]),
                sha256=digest(payload[name])) for name in ("inventory.json", "producer-receipt.json")]
            for record in complete_records:
                path = regular(stage / record["file"])
                if _identity(path.stat())[:2] != owned[path]:
                    raise ValueError("staged file identity differs")
                try:
                    with ExitStack() as verification:
                        Source(FilePin(path=str(path), size_bytes=record["size_bytes"],
                            sha256=record["sha256"]), verification).transfer()
                except ValueError as error:
                    raise ValueError("staged output differs from inventory") from error
            space(0)
            _output_path(output, sources)
            sync_directory(stage / "data")
            sync_directory(stage)
            _exclusive_publish(stage, output)
            sync_directory(output.parent)
            return receipt
        finally:
            # Only remove files created by this invocation, never foreign descendants.
            if stage.exists() and _identity(stage.stat())[:2] == stage_identity:
                for path in reversed(owned):
                    if path.exists():
                        try:
                            refuse_alias(path)
                        except ValueError:
                            continue
                        if _identity(path.stat())[:2] == owned[path]:
                            path.unlink()
                for directory, identity in reversed(directories.items()):
                    try:
                        refuse_alias(directory)
                        if _identity(directory.stat())[:2] == identity:
                            directory.rmdir()
                    except (OSError, ValueError):
                        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--inputs-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    try:
        receipt = prepare(args.inputs, args.inputs_sha256, args.output, validate_only=args.validate_only)
    except (ValueError, OSError) as error:
        parser.exit(1, str(error) + "\n")
    print(canonical(receipt).decode("ascii"))


if __name__ == "__main__":
    main()
