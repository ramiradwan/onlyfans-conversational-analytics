#!/usr/bin/env python3
"""Regenerate the selected public contract export and its consumer pin."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_ROOT = REPOSITORY_ROOT / "contracts"
APPROVED_SOURCE_COMMIT = "ce510faf767e8d808a04eb9ceb28523b598eac0f"
EXPORT_SET = [
    "grant-profile-v1",
    "capability-permit-v1",
    "permit-consumption",
    "production",
    "schemas",
    "onboarding-progress",
]
EXPORT_SOURCES = {
    "grant-profile-v1": "test-vectors/grant-profile-v1",
    "capability-permit-v1": "test-vectors/capability-permit-v1",
    "permit-consumption": "test-vectors/adr-0012-v1/permit-consumption",
    "onboarding-progress": "test-vectors/adr-0012-v1/onboarding-progress",
}
SCHEMA_EXPORT = "schemas"
SCHEMA_ROOT = Path("schemas")
SCHEMA_ENTRYPOINTS = (
    Path("commercial/v1/capability-permit.schema.json"),
    Path("provisioning/v1/onboarding-progress-report.schema.json"),
    Path("provisioning/v1/onboarding-progress-response.schema.json"),
    Path("provisioning/v1/report-proof-challenge.schema.json"),
)
EXPECTED_SCHEMA_CLOSURE = frozenset(
    {
        *SCHEMA_ENTRYPOINTS,
        Path("common/v1/definitions.schema.json"),
    }
)
EXPECTED_PROGRESS_VECTOR_FILES = frozenset(
    {
        "metadata-rejected.expected.json",
        "metadata-rejected.json",
        "unknown-milestone.expected.json",
        "unknown-milestone.json",
        "valid.expected.json",
        "valid.json",
    }
)
EXPECTED_PROGRESS_PROFILE = "urn:bridge-clean:onboarding-progress:v1"
EXPECTED_FILE_COUNT = 437
APPROVED_SOURCE_SHA256 = {
    "schemas/commercial/v1/capability-permit.schema.json": "9b0bfee05eb11ed87876a43b6ee88035f67a6f3067254fcc79e23dd5c42b1aa8",
    "schemas/common/v1/definitions.schema.json": "c81c62d1a05a295b7aaa701a9866df707c8069f8e9910b5cbbc5e879cd8f15fa",
    "schemas/provisioning/v1/onboarding-progress-report.schema.json": "062a9f277af0c29cdc22a469d2e35d73e306fb3f6dbf6435f942558b75987d65",
    "schemas/provisioning/v1/onboarding-progress-response.schema.json": "fc3f762656fbbf605ca96ad4fa7c7401b3aa855194e952010e3b84a2ba2bf452",
    "schemas/provisioning/v1/report-proof-challenge.schema.json": "96aeefee034ee710d98013b1cb2996ba84e576ec1000e40c5b24d2b28ba5f8f8",
    "test-vectors/adr-0012-v1/onboarding-progress/metadata-rejected.expected.json": "2f9029dabbc408bf53239dafce563745c9f8884f1b4ad65c98ceeda805e34c81",
    "test-vectors/adr-0012-v1/onboarding-progress/metadata-rejected.json": "b4e6637ad87e10801094be012ba0c2e6ec382e06d6f58f900473ceb9be17358f",
    "test-vectors/adr-0012-v1/onboarding-progress/unknown-milestone.expected.json": "2f9029dabbc408bf53239dafce563745c9f8884f1b4ad65c98ceeda805e34c81",
    "test-vectors/adr-0012-v1/onboarding-progress/unknown-milestone.json": "691de920f7e9b24931af356d976c9d901f38bd7772020b379279b777f182d1be",
    "test-vectors/adr-0012-v1/onboarding-progress/valid.expected.json": "dfd9af3bf11a1bf83d5ebb81873a8fb2aa1328e5471884ad4ffd07a3779b0686",
    "test-vectors/adr-0012-v1/onboarding-progress/valid.json": "55620d8c602da8feadabdb00e28dab91cdd500a3071f165742062f79a33e6f3f",
}
VECTOR_MANIFESTS = {
    "grant-profile-v1": "grant-profile-v1/manifest.json",
    "capability-permit-v1": "capability-permit-v1/manifest.json",
}
TRUST_SETS = {
    "grant-profile-v1": "grant-profile-v1/keys/trust-set.json",
    "capability-permit-v1": "capability-permit-v1/trust-set.json",
}
POLICY_PROFILES = {
    "permit-consumption": "permit-consumption/policy.json",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def encode(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def fixture_entries() -> list[dict[str, Any]]:
    files = sorted(
        (
            path
            for export in EXPORT_SET
            for path in (CONTRACTS_ROOT / export).rglob("*")
            if path.is_file()
        ),
        key=lambda path: path.relative_to(CONTRACTS_ROOT).as_posix().encode("utf-8"),
    )
    return [
        {
            "path": path.relative_to(CONTRACTS_ROOT).as_posix(),
            "sha256": sha256(path),
            "size": path.stat().st_size,
        }
        for path in files
    ]


def aggregate_digest(entries: list[dict[str, Any]]) -> str:
    material = b"".join(
        entry["path"].encode("utf-8")
        + b"\0"
        + entry["sha256"].encode("ascii")
        + b"\n"
        for entry in entries
    )
    return hashlib.sha256(material).hexdigest()


def _schema_references(value: Any) -> list[str]:
    if isinstance(value, dict):
        references = [value["$ref"]] if isinstance(value.get("$ref"), str) else []
        for child in value.values():
            references.extend(_schema_references(child))
        return references
    if isinstance(value, list):
        return [reference for child in value for reference in _schema_references(child)]
    return []


def schema_dependency_closure(schema_root: Path) -> set[Path]:
    """Resolve the selected permit and onboarding-progress schema closure."""

    pending = [schema_root / entrypoint for entrypoint in SCHEMA_ENTRYPOINTS]
    resolved: set[Path] = set()
    while pending:
        schema = pending.pop()
        try:
            relative = schema.resolve().relative_to(schema_root.resolve())
        except ValueError as exc:
            raise SystemExit(f"schema reference escapes schema root: {schema}") from exc
        if relative in resolved:
            continue
        if not schema.is_file():
            raise SystemExit(f"schema reference is missing: {relative.as_posix()}")
        resolved.add(relative)
        document = json.loads(schema.read_text(encoding="utf-8"))
        for reference in _schema_references(document):
            reference_path, _fragment = reference.split("#", 1) if "#" in reference else (reference, "")
            if not reference_path:
                continue
            parsed = urlsplit(reference_path)
            if parsed.scheme or parsed.netloc:
                raise SystemExit(f"schema reference is not local: {reference}")
            pending.append(schema.parent / parsed.path)
    return resolved


def copy_schema_closure(source_root: Path, target_root: Path) -> None:
    source_schemas = source_root / SCHEMA_ROOT
    closure = schema_dependency_closure(source_schemas)
    if closure != EXPECTED_SCHEMA_CLOSURE:
        difference = sorted(
            closure ^ EXPECTED_SCHEMA_CLOSURE,
            key=lambda path: path.as_posix().encode("utf-8"),
        )
        raise SystemExit(
            "selected schema closure does not match approved closure: "
            + ", ".join(path.as_posix() for path in difference)
        )
    existing = (
        {
            path.relative_to(target_root)
            for path in target_root.rglob("*")
            if path.is_file()
        }
        if target_root.exists()
        else set()
    )
    unexpected = existing - closure
    if unexpected:
        first = min(unexpected, key=lambda path: path.as_posix().encode("utf-8"))
        raise SystemExit(f"selected schema export contains an unexpected file: {first.as_posix()}")
    for relative in sorted(closure, key=lambda path: path.as_posix().encode("utf-8")):
        source = source_schemas / relative
        destination = target_root / relative
        if destination.is_file():
            if destination.read_bytes() != source.read_bytes():
                raise SystemExit(f"existing vendored schema differs from approved source: {relative.as_posix()}")
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def selected_progress_profile() -> str:
    progress_root = CONTRACTS_ROOT / "onboarding-progress"
    actual = {
        path.relative_to(progress_root).as_posix()
        for path in progress_root.rglob("*")
        if path.is_file()
    }
    if actual != EXPECTED_PROGRESS_VECTOR_FILES:
        difference = sorted(actual ^ EXPECTED_PROGRESS_VECTOR_FILES)
        raise SystemExit(
            "selected onboarding-progress export does not match approved file set: "
            + ", ".join(difference)
        )
    profiles = {
        json.loads((progress_root / name).read_text("utf-8"))["request"]["profile"]
        for name in actual
        if name.endswith(".json") and not name.endswith(".expected.json")
    }
    if profiles != {EXPECTED_PROGRESS_PROFILE}:
        raise SystemExit("selected onboarding-progress vectors have an unexpected profile")
    return EXPECTED_PROGRESS_PROFILE


def verify_approved_source_bytes(source_root: Path) -> None:
    for relative, expected_digest in APPROVED_SOURCE_SHA256.items():
        source = source_root / relative
        if not source.is_file():
            raise SystemExit(f"approved source checkout is missing pinned blob: {relative}")
        if sha256(source) != expected_digest:
            raise SystemExit(
                f"approved source blob does not match {APPROVED_SOURCE_COMMIT}: {relative}"
            )


def copy_immutable_export(source: Path, target: Path, source_path: str) -> None:
    if not target.exists():
        shutil.copytree(source, target)
        return
    source_files = {
        path.relative_to(source).as_posix(): path
        for path in source.rglob("*")
        if path.is_file()
    }
    target_files = {
        path.relative_to(target).as_posix(): path
        for path in target.rglob("*")
        if path.is_file()
    }
    if source_files.keys() != target_files.keys():
        difference = sorted(source_files.keys() ^ target_files.keys())
        raise SystemExit(
            f"existing vendored export differs from approved source {source_path}: "
            + ", ".join(difference)
        )
    for relative, source_file in source_files.items():
        if source_file.read_bytes() != target_files[relative].read_bytes():
            raise SystemExit(f"existing vendored upstream fixture is immutable: {target / relative}")


def build_records() -> tuple[dict[str, Any], dict[str, Any]]:
    vector_manifests = [
        {
            "export": export,
            "path": path,
            "sha256": sha256(CONTRACTS_ROOT / path),
        }
        for export, path in VECTOR_MANIFESTS.items()
    ]
    generator_versions = [
        {
            "export": entry["export"],
            "version": json.loads((CONTRACTS_ROOT / entry["path"]).read_text("utf-8"))["generator_version"],
        }
        for entry in vector_manifests
    ]
    trust_sets = [
        {
            "export": export,
            "path": path,
            "sha256": sha256(CONTRACTS_ROOT / path),
        }
        for export, path in TRUST_SETS.items()
    ]
    profiles = [
        json.loads((CONTRACTS_ROOT / entry["path"]).read_text("utf-8"))["profile"]
        for entry in vector_manifests
    ]
    profiles.extend(
        json.loads((CONTRACTS_ROOT / path).read_text("utf-8"))["profile"]
        for path in POLICY_PROFILES.values()
    )
    profiles.append(selected_progress_profile())
    entries = fixture_entries()
    manifest = {
        "content_digest": aggregate_digest(entries),
        "export_set": EXPORT_SET,
        "files": entries,
        "manifest_version": 2,
        "profiles": profiles,
    }
    manifest_bytes = encode(manifest)
    pin = {
        "aggregate_bundle_sha256": manifest["content_digest"],
        "consumer_pin_version": 2,
        "contract_manifest_path": "manifest.json",
        "contract_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "export_set": EXPORT_SET,
        "generator_versions": generator_versions,
        "supported_profiles": profiles,
        "trust_sets": trust_sets,
        "vector_manifests": vector_manifests,
    }
    return manifest, pin


def copy_selected_export(source_root: Path) -> None:
    verify_approved_source_bytes(source_root)
    for export, source_path in EXPORT_SOURCES.items():
        source = source_root / source_path
        target = CONTRACTS_ROOT / export
        if not source.is_dir():
            raise SystemExit(f"approved source checkout does not contain export source: {source_path}")
        copy_immutable_export(source, target, source_path)
    copy_schema_closure(source_root, CONTRACTS_ROOT / SCHEMA_EXPORT)

    count = len(fixture_entries())
    if count != EXPECTED_FILE_COUNT:
        raise SystemExit(f"selected export has {count} files; expected {EXPECTED_FILE_COUNT}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate the selected contract snapshot and public pin."
    )
    parser.add_argument(
        "--copy-from",
        type=Path,
        help="clean, approved source checkout; only its selected export is copied",
    )
    parser.add_argument("--check", action="store_true", help="fail if generated records are stale")
    args = parser.parse_args()
    if args.copy_from is not None:
        if args.check:
            raise SystemExit("--copy-from and --check cannot be combined")
        copy_selected_export(args.copy_from.resolve())
    manifest, pin = build_records()
    expected = {
        CONTRACTS_ROOT / "manifest.json": encode(manifest),
        CONTRACTS_ROOT / "consumer-pin.json": encode(pin),
    }
    stale = [path.name for path, content in expected.items() if not path.is_file() or path.read_bytes() != content]
    if args.check:
        if stale:
            raise SystemExit(f"selected contract snapshot is stale: {', '.join(stale)}")
        print(f"selected export records are current ({len(manifest['files'])} files)")
        return 0
    for path, content in expected.items():
        path.write_bytes(content)
    print(f"regenerated selected export records ({len(manifest['files'])} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
