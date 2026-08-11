"""Offline integrity and trust loading for the vendored contract snapshot."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any


class ContractsIntegrityError(RuntimeError):
    """Raised when the immutable contract snapshot cannot be trusted."""


_MANIFEST = "manifest.json"
_PIN = "consumer-pin.json"
_MANIFEST_KEYS = {
    "content_digest",
    "export_set",
    "files",
    "manifest_version",
    "profiles",
}
_PIN_KEYS = {
    "aggregate_bundle_sha256",
    "consumer_pin_version",
    "contract_manifest_path",
    "contract_manifest_sha256",
    "export_set",
    "generator_versions",
    "supported_profiles",
    "trust_sets",
    "vector_manifests",
}


def _contracts_root(root: Path | None = None) -> Path:
    return (root or Path(__file__).resolve().parent).resolve()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractsIntegrityError(f"cannot load {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractsIntegrityError(f"{path.name} must contain a JSON object")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _aggregate_digest(entries: list[dict[str, Any]]) -> str:
    """Return the deterministic aggregate digest for manifest-listed bytes."""

    material = b"".join(
        entry["path"].encode("utf-8")
        + b"\0"
        + entry["sha256"].encode("ascii")
        + b"\n"
        for entry in entries
    )
    return hashlib.sha256(material).hexdigest()


def _valid_relative_path(value: str) -> bool:
    pure = PurePosixPath(value)
    return (
        bool(value)
        and "\\" not in value
        and not pure.is_absolute()
        and pure.as_posix() == value
        and all(part not in {"", ".", ".."} for part in pure.parts)
    )


def verify_manifest(root: Path | None = None) -> dict[str, Any]:
    """Verify every manifest-listed byte and return the manifest.

    The manifest is deliberately excluded from its own entries so it can be
    generated deterministically. Paths are confined below the contract root.
    """

    contract_root = _contracts_root(root)
    manifest_path = contract_root / _MANIFEST
    manifest = _read_json(manifest_path)
    if set(manifest) != _MANIFEST_KEYS or manifest.get("manifest_version") != 2:
        raise ContractsIntegrityError("manifest.json has an invalid envelope")
    entries = manifest.get("files")
    export_set = manifest.get("export_set")
    profiles = manifest.get("profiles")
    if (
        not isinstance(entries, list)
        or not entries
        or not isinstance(export_set, list)
        or not export_set
        or not all(isinstance(item, str) and item for item in export_set)
        or not isinstance(profiles, list)
        or not all(isinstance(item, str) and item for item in profiles)
    ):
        raise ContractsIntegrityError("manifest.json has an invalid selection")
    seen: set[str] = set()
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"path", "sha256", "size"}
            or not isinstance(entry.get("path"), str)
            or not _valid_relative_path(entry["path"])
            or not isinstance(entry.get("sha256"), str)
            or len(entry["sha256"]) != 64
            or any(char not in "0123456789abcdef" for char in entry["sha256"])
            or not isinstance(entry.get("size"), int)
            or isinstance(entry["size"], bool)
            or entry["size"] < 0
        ):
            raise ContractsIntegrityError("manifest contains an invalid file entry")
        relative = entry["path"]
        candidate = (contract_root / relative).resolve()
        try:
            candidate.relative_to(contract_root)
        except ValueError as exc:
            raise ContractsIntegrityError(f"manifest path escapes contracts: {relative}") from exc
        if relative in seen:
            raise ContractsIntegrityError(f"duplicate manifest entry: {relative}")
        seen.add(relative)
        if not candidate.is_file():
            raise ContractsIntegrityError(f"missing vendored file: {relative}")
        digest = _sha256(candidate)
        if digest != entry.get("sha256"):
            raise ContractsIntegrityError(f"digest mismatch: {relative}")
        size = candidate.stat().st_size
        if size != entry.get("size"):
            raise ContractsIntegrityError(f"size mismatch: {relative}")
    if [entry["path"] for entry in entries] != sorted(seen):
        raise ContractsIntegrityError("manifest paths are not sorted")
    for selected in export_set:
        selected_root = contract_root / selected
        if not selected_root.is_dir():
            raise ContractsIntegrityError(f"selected export is missing: {selected}")
        actual = {
            path.relative_to(contract_root).as_posix()
            for path in selected_root.rglob("*")
            if path.is_file()
        }
        expected = {path for path in seen if path == selected or path.startswith(f"{selected}/")}
        if actual != expected:
            difference = sorted(actual ^ expected)
            raise ContractsIntegrityError(
                f"selected export does not match manifest: {difference[0]}"
            )
    if manifest.get("content_digest") != _aggregate_digest(entries):
        raise ContractsIntegrityError("aggregate bundle digest mismatch")
    return manifest


def verify_snapshot_integrity(root: Path | None = None) -> dict[str, Any]:
    """Verify the selected export plus its independent public consumer pin."""

    contract_root = _contracts_root(root)
    manifest = verify_manifest(contract_root)
    pin = _read_json(contract_root / _PIN)
    if set(pin) != _PIN_KEYS or pin.get("consumer_pin_version") != 2:
        raise ContractsIntegrityError("consumer pin has an invalid envelope")
    manifest_path = contract_root / _MANIFEST
    if pin["contract_manifest_path"] != _MANIFEST or pin["contract_manifest_sha256"] != _sha256(manifest_path):
        raise ContractsIntegrityError("consumer pin does not match contract manifest")
    if pin["supported_profiles"] != manifest["profiles"]:
        raise ContractsIntegrityError("consumer pin does not match supported profiles")
    if pin["export_set"] != manifest["export_set"]:
        raise ContractsIntegrityError("consumer pin does not match export set")
    if pin["aggregate_bundle_sha256"] != manifest["content_digest"]:
        raise ContractsIntegrityError("consumer pin does not match aggregate bundle")
    listed = {entry["path"]: entry for entry in manifest["files"]}
    exports = set(manifest["export_set"])

    def verify_artifact_collection(value: Any, label: str) -> dict[str, dict[str, str]]:
        if not isinstance(value, list):
            raise ContractsIntegrityError(f"consumer pin has invalid {label} collection")
        records: dict[str, dict[str, str]] = {}
        for item in value:
            if (
                not isinstance(item, dict)
                or set(item) != {"export", "path", "sha256"}
                or not all(isinstance(item.get(key), str) for key in item)
                or item["export"] not in exports
                or item["export"] in records
                or not _valid_relative_path(item["path"])
                or len(item["sha256"]) != 64
                or any(char not in "0123456789abcdef" for char in item["sha256"])
            ):
                raise ContractsIntegrityError(f"consumer pin has invalid {label} collection")
            entry = listed.get(item["path"])
            if (
                entry is None
                or not item["path"].startswith(f"{item['export']}/")
                or item["sha256"] != entry["sha256"]
                or item["sha256"] != _sha256(contract_root / item["path"])
            ):
                raise ContractsIntegrityError(f"consumer pin does not match {label}")
            records[item["export"]] = item
        return records

    vector_manifests = verify_artifact_collection(pin.get("vector_manifests"), "vector manifests")
    trust_sets = verify_artifact_collection(pin.get("trust_sets"), "trust sets")
    if set(vector_manifests) != set(trust_sets):
        raise ContractsIntegrityError("consumer pin artifact exports do not match")
    generator_versions = pin.get("generator_versions")
    if not isinstance(generator_versions, list):
        raise ContractsIntegrityError("consumer pin has invalid generator versions")
    versions: dict[str, str] = {}
    for item in generator_versions:
        if (
            not isinstance(item, dict)
            or set(item) != {"export", "version"}
            or not isinstance(item.get("export"), str)
            or not isinstance(item.get("version"), str)
            or item["export"] not in vector_manifests
            or item["export"] in versions
        ):
            raise ContractsIntegrityError("consumer pin has invalid generator versions")
        versions[item["export"]] = item["version"]
    if set(versions) != set(vector_manifests):
        raise ContractsIntegrityError("consumer pin generator exports do not match")
    for export, item in vector_manifests.items():
        vector_manifest = _read_json(contract_root / item["path"])
        if vector_manifest.get("profile") not in manifest["profiles"]:
            raise ContractsIntegrityError("vector manifest profile is not supported")
        if versions[export] != vector_manifest.get("generator_version"):
            raise ContractsIntegrityError("consumer pin does not match generator version")
    return manifest


def load_trust_set(path: str | os.PathLike[str], *, environment: str = "production") -> dict[str, Any]:
    """Load a pinned trust set, refusing non-production material in production."""

    manifest = verify_snapshot_integrity()
    candidate = (manifest_root := _contracts_root()) / Path(path)
    try:
        candidate.relative_to(manifest_root)
    except ValueError as exc:
        raise ContractsIntegrityError(f"trust-set path escapes contracts: {path}") from exc
    if candidate.name != "trust-set.json":
        raise ContractsIntegrityError(f"not a trust set: {path}")
    trust_set = _read_json(candidate)
    if environment != "development" and trust_set.get("production_usable") is not True:
        raise ContractsIntegrityError(f"trust set is not production usable: {path}")
    if not isinstance(trust_set.get("profile"), str) or not isinstance(trust_set.get("keys"), list):
        raise ContractsIntegrityError(f"invalid trust set shape: {path}")
    return trust_set
