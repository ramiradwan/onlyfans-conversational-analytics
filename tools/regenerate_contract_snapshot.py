#!/usr/bin/env python3
"""Regenerate the selected public contract export and its consumer pin."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_ROOT = REPOSITORY_ROOT / "contracts"
EXPORT_SET = ["grant-profile-v1"]
VECTOR_MANIFEST_PATH = "grant-profile-v1/manifest.json"
TRUST_SET_PATH = "grant-profile-v1/keys/trust-set.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def encode(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def fixture_entries() -> list[dict[str, Any]]:
    files = sorted(
        (path for path in (CONTRACTS_ROOT / EXPORT_SET[0]).rglob("*") if path.is_file()),
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


def build_records() -> tuple[dict[str, Any], dict[str, Any]]:
    vector_manifest = json.loads((CONTRACTS_ROOT / VECTOR_MANIFEST_PATH).read_text("utf-8"))
    profile = vector_manifest["profile"]
    generator_version = vector_manifest["generator_version"]
    entries = fixture_entries()
    manifest = {
        "content_digest": aggregate_digest(entries),
        "export_set": EXPORT_SET,
        "files": entries,
        "manifest_version": 2,
        "profiles": [profile],
    }
    manifest_bytes = encode(manifest)
    pin = {
        "aggregate_bundle_sha256": manifest["content_digest"],
        "consumer_pin_version": 1,
        "contract_manifest_path": "manifest.json",
        "contract_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "export_set": EXPORT_SET,
        "generator_version": generator_version,
        "supported_profiles": [profile],
        "trust_set_path": TRUST_SET_PATH,
        "trust_set_sha256": sha256(CONTRACTS_ROOT / TRUST_SET_PATH),
        "vector_manifest_path": VECTOR_MANIFEST_PATH,
        "vector_manifest_sha256": sha256(CONTRACTS_ROOT / VECTOR_MANIFEST_PATH),
    }
    return manifest, pin


def copy_selected_export(source_root: Path) -> None:
    source = source_root / "test-vectors" / EXPORT_SET[0]
    target = CONTRACTS_ROOT / EXPORT_SET[0]
    if not source.is_dir():
        raise SystemExit("approved source checkout does not contain the selected export")
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)


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
