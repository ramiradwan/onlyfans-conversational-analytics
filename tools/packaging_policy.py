"""Verify the declared immutable contents of a staged Windows package."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
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
    *,
    target: str = "staging",
    adapters: tuple[str, ...] = ("inno",),
) -> tuple[PackagingFinding, ...]:
    """Return all policy findings for ``staging_root``; never raise for bad input."""

    root = Path(staging_root)
    findings: list[PackagingFinding] = []
    try:
        effective_policy = dict(load_runtime_policy() if policy is None else policy)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        return (PackagingFinding("policy_invalid", "runtime-files.json", str(error)),)

    if not root.is_dir():
        return (PackagingFinding("staging_root_missing", str(root), "staging root is not a directory"),)

    if effective_policy.get("schema_version") not in (1, 2):
        return (PackagingFinding("policy_invalid", "schema_version", "unsupported runtime policy"),)
    for field in ("required_files", "required_directories"):
        if not isinstance(effective_policy.get(field), list) or not all(
            isinstance(path, str) and path for path in effective_policy[field]
        ):
            return (PackagingFinding("policy_invalid", field, "required paths must be declared"),)
    if effective_policy.get("schema_version") == 2 and (
        not isinstance(effective_policy.get("staging_only_directories"), list)
        or not all(isinstance(path, str) and path for path in effective_policy["staging_only_directories"])
        or not isinstance(effective_policy.get("package_payloads"), dict)
    ):
        return (PackagingFinding("policy_invalid", "package_payloads", "invalid staging and adapter classification"),)
    if target == "staging" and (not adapters or len(adapters) > 2 or any(adapter not in {"inno", "msix"} for adapter in adapters)):
        return (PackagingFinding("policy_invalid", "package_payloads", "unknown staged adapter selection"),)
    if target == "staging" and effective_policy.get("schema_version") == 2:
        try:
            resources = [resource for adapter in adapters
                         for resource in payload_declaration(adapter, effective_policy)["adapter_resources"]]
        except ValueError as error:
            return (PackagingFinding("policy_invalid", "package_payloads", str(error)),)
        effective_policy["required_files"] = [*effective_policy["required_files"], *resources]
        effective_policy["required_directories"] = [*effective_policy["required_directories"],
                                                   *effective_policy["staging_only_directories"]]
    elif target != "staging":
        try:
            declaration = payload_declaration(target, effective_policy)
        except ValueError as error:
            return (PackagingFinding("policy_invalid", "package_payloads", str(error)),)
        effective_policy["required_files"] = [
            path for path in effective_policy["required_files"]
            if not _excluded_from_payload(path, declaration)
        ] + declaration["generated_files"] + declaration["adapter_resources"]
        effective_policy["required_directories"] = [
            path for path in effective_policy["required_directories"]
            if not _excluded_from_payload(path, declaration)
        ]

    try:
        for candidate in _nonfollowing_tree(root):
            if _is_reparse_point(candidate):
                findings.append(PackagingFinding("path_alias_forbidden", str(candidate.relative_to(root)),
                                                 "runtime files must not use links or reparse points"))
    except OSError as error:
        findings.append(PackagingFinding("runtime_inspection_failed", str(root), str(error)))
    if findings:
        return tuple(findings)

    _check_required_paths(root, effective_policy, findings)
    _check_forbidden_paths(root, effective_policy, findings)
    _check_forbidden_material(root, effective_policy, findings)
    _check_sql_catalogs(root, effective_policy, findings)
    _check_contracts(root, effective_policy, findings)
    _check_frontend_closure(root, effective_policy, findings)
    if target == "staging":
        _check_agent_artifact(root, effective_policy, findings)
    elif "signing_parameters" in effective_policy.get("agent_artifact", {}).get("release_declarations", []):
        _check_publication_resources(root, findings)
    if target != "staging":
        for relative in (path.relative_to(root).as_posix() for path in root.rglob("*")):
            if _excluded_from_payload(relative, declaration) and relative not in declaration["generated_files"]:
                findings.append(PackagingFinding("build_input_installed", relative, "file belongs outside this installed payload"))
    return tuple(findings)


def _is_reparse_point(path: Path) -> bool:
    information = path.lstat()
    return stat.S_ISLNK(information.st_mode) or bool(
        getattr(information, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _nonfollowing_tree(root: Path):
    pending = [root]
    while pending:
        path = pending.pop()
        yield path
        if path.is_dir() and not _is_reparse_point(path):
            with os.scandir(path) as entries:
                pending.extend(Path(entry.path) for entry in entries)


def payload_declaration(target: str, policy: Mapping[str, Any] | None = None) -> dict[str, list[str]]:
    """Return an explicit installed closure; staging qualification stays mandatory."""
    effective = load_runtime_policy() if policy is None else policy
    if effective.get("schema_version") != 2 or target not in {"inno", "msix"}:
        raise ValueError("an installed target requires runtime policy v2 and an explicit adapter")
    payloads = effective.get("package_payloads")
    if not isinstance(payloads, dict):
        raise ValueError("invalid package payload mapping")
    declaration = payloads.get(target)
    fields = {"excluded_directories", "excluded_files", "generated_files", "adapter_resources"}
    if not isinstance(declaration, dict) or set(declaration) != fields:
        raise ValueError("invalid package payload declaration")
    for paths in (*declaration.values(), effective.get("staging_only_directories")):
        if not isinstance(paths, list) or not all(
            isinstance(path, str) and path not in {"", "."} and "\\" not in path and ":" not in path
            and not PurePosixPath(path).is_absolute() and ".." not in PurePosixPath(path).parts
            for path in paths
        ):
            raise ValueError("invalid package payload path")
    return {**declaration, "excluded_directories": [*effective.get("staging_only_directories", []),
                                                    *declaration["excluded_directories"]]}


def _excluded_from_payload(relative: str, declaration: Mapping[str, list[str]]) -> bool:
    normalized = relative.casefold()
    return normalized in {path.casefold() for path in declaration["excluded_files"]} or any(
        normalized == path.casefold() or normalized.startswith(path.casefold() + "/")
        for path in declaration["excluded_directories"]
    )


def installed_runtime_files(staging_root: Path | str, target: str,
                            policy: Mapping[str, Any] | None = None) -> tuple[str, ...]:
    """Select files only after the complete staged runtime passes its existing gates."""
    findings = verify_runtime_files(staging_root, policy, adapters=(target,))
    if findings:
        raise ValueError("staged runtime rejected: " + ", ".join(finding.code for finding in findings))
    declaration = payload_declaration(target, policy)
    root = Path(staging_root)
    paths = tuple(sorted(path.relative_to(root).as_posix() for path in root.rglob("*")
                         if path.is_file() and not _excluded_from_payload(path.relative_to(root).as_posix(), declaration)))
    if len({path.casefold() for path in paths}) != len(paths):
        raise ValueError("installed paths contain case aliases")
    return paths


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
        for relative in paths:
            if PurePosixPath(relative).match(pattern):
                findings.append(PackagingFinding("forbidden_path_present", relative, f"matches {pattern}"))


def _check_forbidden_material(
    root: Path, policy: Mapping[str, Any], findings: list[PackagingFinding]
) -> None:
    """Reject named per-user material in any staged path or file payload."""

    declarations = policy.get("forbidden_material", [])
    if not isinstance(declarations, list):
        findings.append(
            PackagingFinding(
                "policy_invalid",
                "forbidden_material",
                "declaration is not a list",
            )
        )
        return
    staged_paths = tuple(
        (path, path.relative_to(root).as_posix())
        for path in root.rglob("*")
        if path.is_file() or path.is_dir()
    )
    # Keep one bucket per declaration, including invalid and duplicate rules,
    # so scanning each file once preserves the declaration-first output order.
    declaration_findings: list[list[PackagingFinding]] = []
    compiled_rules: list[
        tuple[str, re.Pattern[str], Mapping[str, str], list[PackagingFinding]]
    ] = []
    for declaration in declarations:
        bucket: list[PackagingFinding] = []
        declaration_findings.append(bucket)
        if not isinstance(declaration, Mapping):
            bucket.append(
                PackagingFinding(
                    "policy_invalid",
                    "forbidden_material",
                    "declaration is not an object",
                )
            )
            continue
        name = declaration.get("name")
        pattern = declaration.get("pattern")
        if not isinstance(name, str) or not name or not isinstance(pattern, str) or not pattern:
            bucket.append(
                PackagingFinding(
                    "policy_invalid",
                    "forbidden_material",
                    "declaration requires nonempty name and pattern",
                )
            )
            continue
        try:
            matcher = re.compile(pattern)
        except re.error as error:
            bucket.append(PackagingFinding("policy_invalid", name, str(error)))
            continue
        reviewed_files = _reviewed_synthetic_files(declaration, name, bucket)
        compiled_rules.append((name, matcher, reviewed_files, bucket))

    if compiled_rules:
        for path, relative in staged_paths:
            payload = _read_material_payload(path) if path.is_file() else None
            try:
                contents = payload.decode("utf-8") if payload is not None else None
            except UnicodeError:
                contents = None
            for name, matcher, reviewed_files, bucket in compiled_rules:
                if matcher.search(relative) is not None or (
                    payload is not None and contents is not None
                    and matcher.search(contents) is not None
                    and (
                        relative not in reviewed_files
                        or hashlib.sha256(payload).hexdigest() != reviewed_files[relative]
                    )
                ):
                    bucket.append(
                        PackagingFinding(
                            "forbidden_material_present",
                            relative,
                            f"matches forbidden material declaration: {name}",
                        )
                    )

    for bucket in declaration_findings:
        findings.extend(bucket)


def _reviewed_synthetic_files(
    declaration: Mapping[str, Any], name: str, findings: list[PackagingFinding]
) -> Mapping[str, str]:
    """Admit only exact reviewed bytes for this one material rule's payload scan.

    Paths must be canonical relative POSIX file names, never globs. Admission
    does not suppress path checks, other material rules or contract integrity.
    Any malformed entry invalidates the whole admission map, leaving the rule
    active without exceptions.
    """

    reviewed = declaration.get("reviewed_synthetic_files", {})
    if isinstance(reviewed, Mapping) and all(
        isinstance(relative, str)
        and relative
        and PurePosixPath(relative).parts
        and not PurePosixPath(relative).is_absolute()
        and PurePosixPath(relative).as_posix() == relative
        and ".." not in PurePosixPath(relative).parts
        and not any(character in relative for character in "\\:*?[]<>|\"")
        and not any(ord(character) < 32 or ord(character) == 127 for character in relative)
        and isinstance(digest, str)
        and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
        for relative, digest in reviewed.items()
    ):
        return reviewed
    findings.append(
        PackagingFinding(
            "policy_invalid",
            f"forbidden_material.{name}.reviewed_synthetic_files",
            "expected exact relative paths mapped to lowercase SHA-256 digests",
        )
    )
    return {}


def _read_material_payload(path: Path) -> bytes | None:
    """Read once so payload matching and fixture hashing use the same bytes."""

    try:
        return path.read_bytes()
    except OSError:
        return None


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
    for candidate in sorted(contracts_root.rglob("*")):
        contract_relative = candidate.relative_to(contracts_root)
        if candidate.is_file() and (
            "__pycache__" in contract_relative.parts
            or candidate.suffix.lower() in {".pyc", ".pyo"}
        ):
            findings.append(PackagingFinding(
                "contracts_cached_data_present",
                candidate.relative_to(root).as_posix(),
                "cached bytecode is not contract data",
            ))
    expected_root_files = contracts.get("root_files")
    if not isinstance(expected_root_files, list) or not all(
        isinstance(filename, str) and filename for filename in expected_root_files
    ):
        findings.append(PackagingFinding("policy_invalid", "contracts.root_files", "root files are invalid"))
        return
    actual_root_files = (
        {path.name for path in contracts_root.iterdir() if path.is_file()}
        if contracts_root.is_dir()
        else set()
    )
    declared_root_files = set(expected_root_files)
    for filename in sorted(declared_root_files - actual_root_files):
        findings.append(
            PackagingFinding(
                "contracts_root_file_missing",
                f"{relative}/{filename}",
                "declared contracts-root file is absent",
            )
        )
    for filename in sorted(actual_root_files - declared_root_files):
        findings.append(
            PackagingFinding(
                "contracts_root_file_unexpected",
                f"{relative}/{filename}",
                "contracts-root files must be exact",
            )
        )
    for filename, key in (("manifest.json", "manifest_sha256"), ("consumer-pin.json", "consumer_pin_sha256")):
        candidate = contracts_root / filename
        expected_digest = contracts.get(key)
        if not candidate.is_file():
            continue
        if not isinstance(expected_digest, str) or _sha256(candidate) != expected_digest:
            findings.append(PackagingFinding("byte_preservation_failed", f"{relative}/{filename}", "SHA-256 differs from declaration"))
    root_file_digests = contracts.get("root_file_sha256", {})
    if not isinstance(root_file_digests, Mapping) or not all(
        isinstance(filename, str) and isinstance(digest, str)
        for filename, digest in root_file_digests.items()
    ):
        findings.append(
            PackagingFinding(
                "policy_invalid",
                "contracts.root_file_sha256",
                "root file digest anchors are invalid",
            )
        )
    else:
        for filename, expected_digest in root_file_digests.items():
            candidate = contracts_root / filename
            if not candidate.is_file():
                continue
            if _sha256(candidate) != expected_digest:
                findings.append(
                    PackagingFinding(
                        "byte_preservation_failed",
                        f"{relative}/{filename}",
                        "SHA-256 differs from declaration",
                    )
                )
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
    required_entries = frontend.get("required_entries", [])
    if not isinstance(required_entries, list) or not all(isinstance(value, str) for value in required_entries):
        findings.append(PackagingFinding("policy_invalid", "frontend.required_entries", "entry names must be a list of strings"))
        return
    for required in required_entries:
        if required not in entry_keys:
            findings.append(PackagingFinding("frontend_entry_missing", str(required), "required frontend entry is absent"))
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


def _check_agent_artifact(
    root: Path, policy: Mapping[str, Any], findings: list[PackagingFinding]
) -> None:
    """Reject a staged Agent artifact that declares no Legal release bindings.

    The staged build metadata records which bindings the extension build
    verified, never their contents, so this reads the declaration only.
    """

    declaration = policy.get("agent_artifact")
    if not isinstance(declaration, Mapping):
        findings.append(
            PackagingFinding("policy_invalid", "agent_artifact", "declaration is not an object")
        )
        return
    relative = declaration.get("build_metadata_path")
    schema = declaration.get("schema")
    names = declaration.get("release_declarations")
    if (
        not isinstance(relative, str)
        or not relative
        or not isinstance(schema, str)
        or not schema
        or not isinstance(names, list)
        or not names
        or not all(isinstance(name, str) and name for name in names)
    ):
        findings.append(
            PackagingFinding(
                "policy_invalid",
                "agent_artifact",
                "declaration requires build_metadata_path, schema and release_declarations",
            )
        )
        return

    candidate = root / relative
    if not candidate.is_file():
        findings.append(
            PackagingFinding(
                "agent_build_metadata_missing",
                relative,
                "staged Agent artifact carries no build metadata",
            )
        )
        return
    try:
        metadata = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        findings.append(PackagingFinding("agent_build_metadata_invalid", relative, str(error)))
        return
    if not isinstance(metadata, Mapping):
        findings.append(
            PackagingFinding("agent_build_metadata_invalid", relative, "metadata is not an object")
        )
        return
    if metadata.get("schema") != schema:
        findings.append(
            PackagingFinding(
                "agent_build_metadata_schema_unexpected",
                relative,
                f"build metadata schema is not {schema}",
            )
        )
        return
    for name in names:
        _check_release_declaration(relative, metadata.get(name), name, findings)
    if "signing_parameters" in names:
        _check_publication_resources(root, findings)


def _check_release_declaration(
    relative: str, declared: Any, name: str, findings: list[PackagingFinding]
) -> None:
    """Validate Legal document identity or packaged signing authority."""

    if declared is None:
        findings.append(
            PackagingFinding(
                "agent_release_declaration_absent",
                relative,
                f"staged Agent artifact declares no {name}",
            )
        )
        return
    if not isinstance(declared, Mapping):
        findings.append(
            PackagingFinding(
                "agent_release_declaration_invalid",
                relative,
                f"{name} is not an object",
            )
        )
        return
    if name == "signing_parameters":
        expected = {
            "schema": "ofca-signing-parameter-consumer/v1", "mode": "verified",
            "engine": "onlyfans-sha1-checksum/v1", "parameter_schema": "local-packaged-signing-rule/v1",
            "trust_sha256": "9a0965a3684afae9b4745d9c36845abb2f0a168ce3b1da3256567720542873c9",
            "distribution_origin": "https://updates.dipsy.fi", "distribution_base_path": "/signing-parameters/v1/",
        }
        if declared != expected:
            findings.append(PackagingFinding("agent_release_declaration_invalid", relative,
                                             "signing_parameters differs from the reviewed engine/trust/distribution"))
        return
    for field in ("schema", "source_revision", f"{name}_digest"):
        value = declared.get(field)
        if not isinstance(value, str) or not value:
            findings.append(
                PackagingFinding(
                    "agent_release_declaration_invalid",
                    relative,
                    f"{name}.{field} is missing or empty",
                )
            )


def _check_publication_resources(root: Path, findings: list[PackagingFinding]) -> None:
    """Reject altered production trust/origin even when the files exist."""
    from app.security.signing_parameter_publications import parse, validate_trust
    references = ("app/security/signing-publication-trust.json", "app/core/signing-parameter-release.json")
    try:
        trust = parse((root / "_internal" / references[0]).read_bytes(), maximum=16384)
        validate_trust(trust, production=True)
        release = parse((root / "_internal" / references[1]).read_bytes(), maximum=4096)
        if release != {"schema": "ofca-signing-parameter-release/v1", "origin": "https://updates.dipsy.fi",
                       "base_path": "/signing-parameters/v1/",
                       "trust_sha256": "9a0965a3684afae9b4745d9c36845abb2f0a168ce3b1da3256567720542873c9"}:
            raise ValueError("fixed distribution mismatch")
    except (OSError, ValueError, TypeError, RecursionError):
        findings.append(PackagingFinding("signing_publication_resource_invalid", "_internal/app",
                                         "production signing trust or fixed distribution is invalid"))


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
