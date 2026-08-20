"""Verify the declared immutable contents of a staged Windows package."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY_PATH = REPOSITORY_ROOT / "packaging" / "runtime-files.json"


@dataclass(frozen=True, slots=True)
class PackagingFinding:
    """One policy violation found while inspecting a staging root."""

    code: str
    path: str
    detail: str


def load_runtime_policy(path: Path | str = DEFAULT_POLICY_PATH) -> dict[str, Any]:
    """Load the standalone packaging declaration without importing ``packaging``."""

    return json.loads(Path(path).read_text(encoding="utf-8"))


def verify_runtime_files(
    staging_root: Path | str,
    policy: Mapping[str, Any] | None = None,
) -> tuple[PackagingFinding, ...]:
    """Return all policy findings for ``staging_root``; never raise for bad input."""

    root = Path(staging_root)
    findings: list[PackagingFinding] = []
    try:
        effective_policy = dict(load_runtime_policy() if policy is None else policy)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as error:
        return (PackagingFinding("policy_invalid", "runtime-files.json", str(error)),)

    if not root.is_dir():
        return (PackagingFinding("staging_root_missing", str(root), "staging root is not a directory"),)

    _check_required_paths(root, effective_policy, findings)
    _check_forbidden_paths(root, effective_policy, findings)
    _check_sql_catalogs(root, effective_policy, findings)
    _check_contracts(root, effective_policy, findings)
    _check_frontend_closure(root, effective_policy, findings)
    return tuple(findings)


def _check_required_paths(
    root: Path, policy: Mapping[str, Any], findings: list[PackagingFinding]
) -> None:
    for relative in policy.get("required_files", []):
        candidate = root / relative
        if not candidate.is_file():
            findings.append(PackagingFinding("required_file_missing", relative, "required file is absent"))
    for relative in policy.get("required_directories", []):
        candidate = root / relative
        if not candidate.is_dir():
            findings.append(PackagingFinding("required_directory_missing", relative, "required directory is absent"))


def _check_forbidden_paths(
    root: Path, policy: Mapping[str, Any], findings: list[PackagingFinding]
) -> None:
    paths = tuple(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() or path.is_dir()
    )
    for pattern in policy.get("forbidden_paths", []):
        if not isinstance(pattern, str):
            findings.append(PackagingFinding("policy_invalid", "forbidden_paths", "pattern is not a string"))
            continue
        matcher = PurePosixPath(pattern)
        for relative in paths:
            if matcher.match(relative):
                findings.append(PackagingFinding("forbidden_path_present", relative, f"matches {pattern}"))


def _check_sql_catalogs(
    root: Path, policy: Mapping[str, Any], findings: list[PackagingFinding]
) -> None:
    for catalog in policy.get("sql_catalogs", []):
        if not isinstance(catalog, Mapping):
            findings.append(PackagingFinding("policy_invalid", "sql_catalogs", "catalog is not an object"))
            continue
        relative = catalog.get("path")
        expected = catalog.get("files")
        if not isinstance(relative, str) or not isinstance(expected, Mapping):
            findings.append(PackagingFinding("policy_invalid", "sql_catalogs", "catalog has invalid path or files"))
            continue
        catalog_root = root / relative
        actual = {
            path.relative_to(catalog_root).as_posix()
            for path in catalog_root.rglob("*.sql")
        } if catalog_root.is_dir() else set()
        expected_names = set(expected)
        for name in sorted(expected_names - actual):
            findings.append(PackagingFinding("sql_catalog_file_missing", f"{relative}/{name}", "declared SQL file is absent"))
        for name in sorted(actual - expected_names):
            findings.append(PackagingFinding("sql_catalog_file_unexpected", f"{relative}/{name}", "SQL catalog must be exact"))
        for name, expected_digest in expected.items():
            candidate = catalog_root / name
            if not candidate.is_file():
                continue
            actual_digest = _sha256(candidate)
            if actual_digest != expected_digest:
                findings.append(PackagingFinding("byte_preservation_failed", f"{relative}/{name}", "SHA-256 differs from declaration"))


def _check_contracts(
    root: Path, policy: Mapping[str, Any], findings: list[PackagingFinding]
) -> None:
    contracts = policy.get("contracts")
    if not isinstance(contracts, Mapping) or not isinstance(contracts.get("path"), str):
        findings.append(PackagingFinding("policy_invalid", "contracts", "contracts declaration is invalid"))
        return
    relative = contracts["path"]
    contracts_root = root / relative
    for filename, key in (("manifest.json", "manifest_sha256"), ("consumer-pin.json", "consumer_pin_sha256")):
        candidate = contracts_root / filename
        expected_digest = contracts.get(key)
        if not candidate.is_file():
            continue
        if not isinstance(expected_digest, str) or _sha256(candidate) != expected_digest:
            findings.append(PackagingFinding("byte_preservation_failed", f"{relative}/{filename}", "SHA-256 differs from declaration"))
    try:
        from contracts.loader import verify_snapshot_integrity

        verify_snapshot_integrity(contracts_root)
    except Exception as error:  # Contract verifier supplies the precise integrity boundary.
        findings.append(PackagingFinding("contracts_closure_failed", relative, str(error)))


def _check_frontend_closure(
    root: Path, policy: Mapping[str, Any], findings: list[PackagingFinding]
) -> None:
    frontend = policy.get("frontend")
    if not isinstance(frontend, Mapping):
        findings.append(PackagingFinding("policy_invalid", "frontend", "frontend declaration is invalid"))
        return
    dist_path = frontend.get("dist_path")
    manifest_paths = frontend.get("manifest_paths")
    if not isinstance(dist_path, str) or not isinstance(manifest_paths, list):
        findings.append(PackagingFinding("policy_invalid", "frontend", "frontend paths are invalid"))
        return
    dist_root = root / dist_path
    manifest_path = next((dist_root / path for path in manifest_paths if isinstance(path, str) and (dist_root / path).is_file()), None)
    if manifest_path is None:
        findings.append(PackagingFinding("frontend_manifest_missing", dist_path, "Vite manifest closure cannot be established"))
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        findings.append(PackagingFinding("frontend_manifest_invalid", manifest_path.relative_to(root).as_posix(), str(error)))
        return
    if not isinstance(manifest, Mapping):
        findings.append(PackagingFinding("frontend_manifest_invalid", manifest_path.relative_to(root).as_posix(), "manifest is not an object"))
        return
    entries = {key: value for key, value in manifest.items() if isinstance(key, str) and isinstance(value, Mapping)}
    entry_keys = [key for key, value in entries.items() if value.get("isEntry") is True]
    if not entry_keys:
        findings.append(PackagingFinding("frontend_entry_missing", manifest_path.relative_to(root).as_posix(), "manifest has no entry"))
        return
    seen: set[str] = set()
    pending = list(entry_keys)
    while pending:
        key = pending.pop()
        if key in seen:
            continue
        seen.add(key)
        entry = entries.get(key)
        if entry is None:
            findings.append(PackagingFinding("frontend_manifest_reference_missing", key, "manifest entry is absent"))
            continue
        _check_frontend_file(dist_root, root, entry.get("file"), findings)
        for field in ("css", "assets"):
            values = entry.get(field, [])
            if not isinstance(values, list):
                findings.append(PackagingFinding("frontend_manifest_invalid", key, f"{field} is not a list"))
                continue
            for value in values:
                _check_frontend_file(dist_root, root, value, findings)
        for field in ("imports", "dynamicImports"):
            references = entry.get(field, [])
            if not isinstance(references, list):
                findings.append(PackagingFinding("frontend_manifest_invalid", key, f"{field} is not a list"))
                continue
            for reference in references:
                if not isinstance(reference, str):
                    findings.append(PackagingFinding("frontend_manifest_invalid", key, f"{field} contains a non-string"))
                else:
                    pending.append(reference)


def _check_frontend_file(dist_root: Path, staging_root: Path, relative: Any, findings: list[PackagingFinding]) -> None:
    if not isinstance(relative, str) or not relative:
        findings.append(PackagingFinding("frontend_manifest_invalid", str(relative), "asset path is invalid"))
        return
    candidate = (dist_root / relative).resolve()
    try:
        candidate.relative_to(dist_root.resolve())
    except ValueError:
        findings.append(PackagingFinding("frontend_asset_outside_dist", relative, "asset path escapes dist"))
        return
    if not candidate.is_file():
        findings.append(PackagingFinding("frontend_asset_missing", candidate.relative_to(staging_root).as_posix(), "manifest-referenced asset is absent"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
