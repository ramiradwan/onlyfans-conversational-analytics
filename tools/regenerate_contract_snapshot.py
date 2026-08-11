#!/usr/bin/env python3
"""Regenerate the selected public contract export and its consumer pin."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_ROOT = REPOSITORY_ROOT / "contracts"
EXPORT_SET = [
    "grant-profile-v1",
    "capability-permit-v1",
    "permit-consumption",
    "production",
    "schemas",
]
EXPORT_SOURCES = {
    "grant-profile-v1": "test-vectors/grant-profile-v1",
    "capability-permit-v1": "test-vectors/capability-permit-v1",
    "permit-consumption": "test-vectors/adr-0012-v1/permit-consumption",
}
SCHEMA_EXPORT = "schemas"
SCHEMA_ROOT = Path("schemas")
SCHEMA_ENTRYPOINT = Path("commercial/v1/capability-permit.schema.json")
EXPECTED_SCHEMA_CLOSURE_SIZE = 2
EXPECTED_FILE_COUNT = 428
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
    """Resolve local schema files required by the capability-permit schema."""

    entrypoint = schema_root / SCHEMA_ENTRYPOINT
    pending = [entrypoint]
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
    if len(closure) != EXPECTED_SCHEMA_CLOSURE_SIZE:
        raise SystemExit(
            "capability-permit schema closure has "
            f"{len(closure)} members; expected {EXPECTED_SCHEMA_CLOSURE_SIZE}"
        )
    if target_root.exists():
        shutil.rmtree(target_root)
    for relative in sorted(closure, key=lambda path: path.as_posix().encode("utf-8")):
        destination = target_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_schemas / relative, destination)


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
    for export, source_path in EXPORT_SOURCES.items():
        source = source_root / source_path
        target = CONTRACTS_ROOT / export
        if not source.is_dir():
            raise SystemExit(f"approved source checkout does not contain export source: {source_path}")
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)
    copy_schema_closure(source_root, CONTRACTS_ROOT / SCHEMA_EXPORT)

    count = len(fixture_entries())
    if count != EXPECTED_FILE_COUNT:
        raise SystemExit(f"selected export has {count} files; expected {EXPECTED_FILE_COUNT}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate the selected grant-profile contract snapshot and public pin."
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
