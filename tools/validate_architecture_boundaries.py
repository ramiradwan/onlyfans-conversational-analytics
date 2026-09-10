#!/usr/bin/env python3
"""Validate the machine-readable architecture boundary manifest and markdown contract."""

from __future__ import annotations

import ast
import datetime
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST_PATH = ROOT / "docs" / "architecture-boundaries.json"
DEFAULT_MARKDOWN_PATH = ROOT / "docs" / "architecture-boundaries.md"

ALLOWED_ZONES = frozenset({"green", "yellow", "orange", "red"})
ALLOWED_AUTHORITIES = frozenset({"authoritative", "derived", "presentation", "composition"})
ALLOWED_RULE_TYPES = frozenset({"forbidden", "protected", "boundary"})
ALLOWED_ENFORCEMENT_STATUSES = frozenset({"enforced", "documented"})
ALLOWED_ASSURANCE_STATUSES = frozenset({"documented", "qualified"})
ALLOWED_SEVERITIES = frozenset({"critical", "high", "medium", "low"})
ALLOWED_EXCEPTION_STATUSES = frozenset({"temporary_exception", "current_design"})
INVARIANT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")

ALLOWED_EXECUTABLE_EXTENSIONS = frozenset({".py", ".mjs", ".js", ".ts", ".tsx", ".sh", ".ps1"})
DOCUMENTATION_EXTENSIONS = frozenset({".md", ".txt", ".json", ".yaml", ".yml", ".rst", ".html", ".css", ".ini", ".lock"})

DEFAULT_NON_PRODUCTION_PREFIXES = (
    "tests/",
    "tools/",
    "docs/",
    ".husky/",
    ".git/",
    ".github/",
    ".pytest_temp/",
    ".pytest_cache/",
    "extension/test-fixtures/",
)

DEFAULT_ROOT_METADATA_FILES = frozenset(
    {
        ".gitattributes",
        ".gitignore",
        ".importlinter",
        "AI-instructions.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "README.md",
        "THIRD_PARTY_NOTICES.md",
        "communication-spec.md",
        "pytest.ini",
        "requirements-dev.txt",
        "requirements.txt",
    }
)


class ArchitectureBoundaryError(Exception):
    """Raised when architecture boundary validation fails."""


class UnclassifiedProductionPathError(ArchitectureBoundaryError):
    """Raised when a production path does not map to any declared architectural module."""


@dataclass(frozen=True)
class PathClassification:
    path: str
    is_production: bool
    is_vendored_contract: bool
    module_id: str | None
    zone: str | None
    authority: str | None
    protected_invariants: tuple[str, ...]


def pattern_to_regex(pattern: str) -> re.Pattern[str]:
    """Convert a glob pattern with ** support to an exact regex."""
    pattern = pattern.replace("\\", "/")
    i = 0
    n = len(pattern)
    res: list[str] = ["^"]
    while i < n:
        if pattern[i : i + 3] == "/**":
            res.append("(?:/.*)?")
            i += 3
        elif pattern[i : i + 2] == "**":
            res.append(".*")
            i += 2
        elif pattern[i] == "*":
            res.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            res.append("[^/]")
            i += 1
        else:
            res.append(re.escape(pattern[i]))
            i += 1
    res.append("$")
    return re.compile("".join(res))


def load_manifest(path: Path | str) -> dict[str, Any]:
    """Load and parse JSON manifest."""
    manifest_path = Path(path)
    if not manifest_path.exists():
        raise ArchitectureBoundaryError(f"Manifest file does not exist: {manifest_path}")
    try:
        content = manifest_path.read_text(encoding="utf-8")
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise ArchitectureBoundaryError(f"Invalid JSON in manifest {manifest_path}: {exc}") from exc


def validate_protected_impact_mappings(manifest: dict[str, Any]) -> list[str]:
    """Validate manifest-owned mappings used by pull-request impact review."""

    errors: list[str] = []
    protected_impact = manifest.get("protected_impact")
    if not isinstance(protected_impact, dict):
        return ["protected_impact must be an object in docs/architecture-boundaries.json"]

    entries = protected_impact.get("invariant_path_mappings")
    if not isinstance(entries, list) or not entries:
        return ["protected_impact.invariant_path_mappings must be a non-empty list"]

    declared_invariants = {
        entry.get("id")
        for entry in manifest.get("semantic_invariants", [])
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    }
    module_invariants = {
        invariant
        for module in manifest.get("modules", [])
        if isinstance(module, dict)
        for invariant in module.get("protected_invariants", [])
        if isinstance(invariant, str)
    }
    seen_invariants: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append(f"protected_impact.invariant_path_mappings[{index}] must be an object")
            continue
        invariant = entry.get("invariant")
        if not isinstance(invariant, str) or not INVARIANT_ID_PATTERN.fullmatch(invariant):
            errors.append(
                f"protected_impact.invariant_path_mappings[{index}].invariant must be a non-empty invariant id"
            )
            continue
        if invariant in seen_invariants:
            errors.append(f"protected_impact.invariant_path_mappings duplicates invariant {invariant!r}")
        seen_invariants.add(invariant)
        if invariant not in declared_invariants:
            errors.append(
                f"protected_impact.invariant_path_mappings references undeclared invariant {invariant!r}"
            )
        if invariant not in module_invariants:
            errors.append(
                f"protected_impact.invariant_path_mappings invariant {invariant!r} is not protected by any module"
            )
        patterns = entry.get("path_patterns")
        if not isinstance(patterns, list) or not patterns:
            errors.append(
                f"protected_impact.invariant_path_mappings[{index}].path_patterns must be a non-empty list"
            )
            continue
        if any(not isinstance(pattern, str) or not pattern.strip() for pattern in patterns):
            errors.append(f"protected_impact.invariant_path_mappings[{index}] has an empty path pattern")
    return errors


def resolve_executable_reference(ref: Any, repo_root: Path, context: str) -> list[str]:
    """Safely resolve an executable reference without shell execution or module importing."""
    if not isinstance(ref, str) or not ref.strip():
        return [f"{context}: reference must be a non-empty string, got {ref!r}"]

    clean_ref = ref.strip()
    if clean_ref.startswith("planned:"):
        return [f"{context}: executable reference cannot be planned: {clean_ref!r}"]

    parts = clean_ref.split("::")
    if len(parts) > 2:
        return [f"{context}: malformed executable reference syntax: {clean_ref!r}"]

    file_rel_path = parts[0].strip().replace("\\", "/")
    symbol_name = parts[1].strip() if len(parts) == 2 else None

    # Check for drive letters, protocol schemes, or absolute paths
    if file_rel_path.startswith("/") or ":" in file_rel_path:
        return [f"{context}: reference path cannot be absolute or contain scheme/drive: {file_rel_path!r}"]

    # Reject repo escaping paths
    try:
        target_file = (repo_root / file_rel_path).resolve()
        if not target_file.is_relative_to(repo_root):
            return [f"{context}: reference path escapes repository root: {file_rel_path!r}"]
    except (ValueError, OSError) as exc:
        return [f"{context}: invalid reference path {file_rel_path!r}: {exc}"]

    # Reject documentation and non-executable files
    suffix = target_file.suffix.lower()
    if suffix in DOCUMENTATION_EXTENSIONS or suffix not in ALLOWED_EXECUTABLE_EXTENSIONS:
        return [
            f"{context}: non-executable reference file type {suffix!r} "
            f"(expected executable test or control script): {file_rel_path}"
        ]

    if not target_file.is_file():
        return [f"{context}: referenced file does not exist: {file_rel_path}"]

    if symbol_name is not None:
        if file_rel_path.endswith(".py"):
            try:
                tree = ast.parse(target_file.read_text(encoding="utf-8"), filename=str(target_file))
            except SyntaxError as exc:
                return [f"{context}: syntax error in {file_rel_path}: {exc}"]

            found = False
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if node.name == symbol_name:
                        found = True
                        break
            if not found:
                return [f"{context}: executable symbol {symbol_name!r} not found as function or class in {file_rel_path}"]
        else:
            try:
                content = target_file.read_text(encoding="utf-8")
                if symbol_name not in content:
                    return [f"{context}: symbol {symbol_name!r} not found in {file_rel_path}"]
            except OSError as exc:
                return [f"{context}: cannot read {file_rel_path}: {exc}"]

    return []


def validate_manifest_schema(
    manifest: dict[str, Any],
    *,
    repo_root: Path | None = None,
    reference_date: datetime.date | None = None,
) -> list[str]:
    """Validate manifest structure, constraints, rules, invariants, and exceptions."""
    errors: list[str] = []
    root = repo_root or ROOT
    current_date = reference_date or datetime.date.today()

    if not isinstance(manifest, dict):
        return ["Manifest root must be a JSON object"]

    if manifest.get("schema_version") != "1.0.0":
        errors.append(f"schema_version must be '1.0.0', got {manifest.get('schema_version')!r}")

    if manifest.get("machine_authority") != "docs/architecture-boundaries.json":
        errors.append(
            "machine_authority must be 'docs/architecture-boundaries.json', "
            f"got {manifest.get('machine_authority')!r}"
        )

    # 1. Modules validation
    modules = manifest.get("modules")
    if not isinstance(modules, list) or len(modules) == 0:
        errors.append("modules must be a non-empty list")
        modules = []

    seen_module_ids: set[str] = set()
    module_by_id: dict[str, dict[str, Any]] = {}
    for idx, mod in enumerate(modules):
        if not isinstance(mod, dict):
            errors.append(f"modules[{idx}] must be an object")
            continue

        mod_id = mod.get("id")
        if not mod_id or not isinstance(mod_id, str):
            errors.append(f"modules[{idx}]: missing or empty id")
            continue

        if mod_id in seen_module_ids:
            errors.append(f"duplicate module id: {mod_id}")
        seen_module_ids.add(mod_id)
        module_by_id[mod_id] = mod

        role = mod.get("role")
        if not role or not isinstance(role, str) or not role.strip():
            errors.append(f"module {mod_id}: missing or empty role")

        authority = mod.get("authority")
        if authority not in ALLOWED_AUTHORITIES:
            errors.append(
                f"module {mod_id}: invalid authority {authority!r}; "
                f"must be one of {sorted(ALLOWED_AUTHORITIES)}"
            )

        zone = mod.get("zone")
        if zone not in ALLOWED_ZONES:
            errors.append(
                f"module {mod_id}: invalid zone {zone!r}; "
                f"must be one of {sorted(ALLOWED_ZONES)}"
            )

        path_patterns = mod.get("path_patterns")
        if not isinstance(path_patterns, list) or len(path_patterns) == 0:
            errors.append(f"module {mod_id}: path_patterns must be a non-empty list of globs")
        else:
            for pat in path_patterns:
                if not isinstance(pat, str) or not pat.strip():
                    errors.append(f"module {mod_id}: path_pattern must be a non-empty string, got {pat!r}")

        permitted_inputs = mod.get("permitted_inputs")
        if not isinstance(permitted_inputs, list):
            errors.append(f"module {mod_id}: missing permitted_inputs (must be a list)")
        else:
            for inp in permitted_inputs:
                if not isinstance(inp, str) or not inp.strip():
                    errors.append(f"module {mod_id}: permitted_input item must be non-empty string, got {inp!r}")

        permitted_outputs = mod.get("permitted_outputs")
        if not isinstance(permitted_outputs, list):
            errors.append(f"module {mod_id}: missing permitted_outputs (must be a list)")
        else:
            for out in permitted_outputs:
                if not isinstance(out, str) or not out.strip():
                    errors.append(f"module {mod_id}: permitted_output item must be non-empty string, got {out!r}")

        protected_invariants = mod.get("protected_invariants")
        if not isinstance(protected_invariants, list):
            errors.append(f"module {mod_id}: protected_invariants must be a list")

    # 2. Rules validation
    rules = manifest.get("rules")
    if not isinstance(rules, list):
        errors.append("rules must be a list")
        rules = []

    seen_rule_ids: set[str] = set()
    rule_by_id: dict[str, dict[str, Any]] = {}
    for idx, rule in enumerate(rules):
        if not isinstance(rule, dict):
            errors.append(f"rules[{idx}] must be an object")
            continue

        rule_id = rule.get("id")
        if not rule_id or not isinstance(rule_id, str):
            errors.append(f"rules[{idx}]: missing or empty id")
            continue

        if rule_id in seen_rule_ids:
            errors.append(f"duplicate rule id: {rule_id}")
        seen_rule_ids.add(rule_id)
        rule_by_id[rule_id] = rule

        description = rule.get("description")
        if not description or not isinstance(description, str):
            errors.append(f"rule {rule_id}: missing or empty description")

        source_modules = rule.get("source_modules")
        if not isinstance(source_modules, list) or len(source_modules) == 0:
            errors.append(f"rule {rule_id}: source_modules must be a non-empty list")
        else:
            for sm in source_modules:
                if sm not in seen_module_ids:
                    errors.append(f"rule {rule_id}: source module {sm!r} not defined in modules")

        target_modules = rule.get("target_modules")
        if not isinstance(target_modules, list) or len(target_modules) == 0:
            errors.append(f"rule {rule_id}: target_modules must be a non-empty list")
        else:
            for tm in target_modules:
                if tm not in seen_module_ids:
                    errors.append(f"rule {rule_id}: target module {tm!r} not defined in modules")

        rule_type = rule.get("type")
        if rule_type not in ALLOWED_RULE_TYPES:
            errors.append(
                f"rule {rule_id}: invalid type {rule_type!r}; "
                f"must be one of {sorted(ALLOWED_RULE_TYPES)}"
            )

        enforcement = rule.get("enforcement")
        if not isinstance(enforcement, dict):
            errors.append(f"rule {rule_id}: enforcement must be an object")
        else:
            status = enforcement.get("status")
            if status not in ALLOWED_ENFORCEMENT_STATUSES:
                errors.append(
                    f"rule {rule_id}: enforcement status {status!r} must be one of "
                    f"{sorted(ALLOWED_ENFORCEMENT_STATUSES)}"
                )

            enforced_by = enforcement.get("enforced_by")
            if status == "enforced":
                if not isinstance(enforced_by, list) or len(enforced_by) == 0:
                    errors.append(
                        f"rule {rule_id}: enforced rule must name at least one executable control in enforced_by"
                    )
                else:
                    for ctrl in enforced_by:
                        ref_errors = resolve_executable_reference(ctrl, root, f"rule {rule_id} enforced_by")
                        errors.extend(ref_errors)
            elif status == "documented":
                if enforced_by is not None and not isinstance(enforced_by, list):
                    errors.append(f"rule {rule_id}: enforced_by must be a list if provided")

    # 3. Semantic invariants validation
    invariants = manifest.get("semantic_invariants")
    if not isinstance(invariants, list):
        errors.append("semantic_invariants must be a list")
        invariants = []

    seen_invariant_ids: set[str] = set()
    for idx, inv in enumerate(invariants):
        if not isinstance(inv, dict):
            errors.append(f"semantic_invariants[{idx}] must be an object")
            continue

        inv_id = inv.get("id")
        if not inv_id or not isinstance(inv_id, str):
            errors.append(f"semantic_invariants[{idx}]: missing or empty id")
            continue

        if inv_id in seen_invariant_ids:
            errors.append(f"duplicate semantic invariant id: {inv_id}")
        seen_invariant_ids.add(inv_id)

        owner = inv.get("owner")
        if not owner or not isinstance(owner, str):
            errors.append(f"semantic invariant {inv_id}: missing or empty owner")

        severity = inv.get("severity")
        if severity not in ALLOWED_SEVERITIES:
            errors.append(
                f"semantic invariant {inv_id}: invalid severity {severity!r}; "
                f"must be one of {sorted(ALLOWED_SEVERITIES)}"
            )

        req = inv.get("requirement_or_scenario")
        if not req or not isinstance(req, str) or not req.strip():
            errors.append(f"semantic invariant {inv_id}: missing requirement_or_scenario")

        assurance = inv.get("assurance")
        if not isinstance(assurance, dict):
            errors.append(f"semantic invariant {inv_id}: assurance must be an object")
        else:
            status = assurance.get("status")
            if status not in ALLOWED_ASSURANCE_STATUSES:
                errors.append(
                    f"semantic invariant {inv_id}: assurance status {status!r} must be one of "
                    f"{sorted(ALLOWED_ASSURANCE_STATUSES)}"
                )

            evidence = assurance.get("evidence")
            falsifier = assurance.get("falsifier")

            if status == "qualified":
                if not isinstance(evidence, list) or len(evidence) == 0:
                    errors.append(
                        f"semantic invariant {inv_id}: qualified status requires non-empty evidence list"
                    )
                else:
                    for ev in evidence:
                        errors.extend(
                            resolve_executable_reference(ev, root, f"semantic invariant {inv_id} evidence")
                        )

                if falsifier is None or not isinstance(falsifier, str) or not falsifier.strip():
                    errors.append(
                        f"semantic invariant {inv_id}: qualified status requires permanent falsifier reference"
                    )
                else:
                    errors.extend(
                        resolve_executable_reference(
                            falsifier, root, f"semantic invariant {inv_id} falsifier"
                        )
                    )

    # Cross-check module protected_invariants against declared semantic invariants
    for mod in modules:
        mod_id = mod.get("id", "")
        for inv_ref in mod.get("protected_invariants", []):
            if inv_ref not in seen_invariant_ids:
                errors.append(
                    f"module {mod_id}: references undeclared protected invariant {inv_ref!r}"
                )

    # 4. Exceptions validation
    exceptions = manifest.get("exceptions")
    if not isinstance(exceptions, list):
        errors.append("exceptions must be a list")
        exceptions = []

    # Compile matchers for validating exception source scope
    matchers = build_module_matchers(manifest)
    seen_exception_identities: set[tuple[str, str, str]] = set()

    for idx, exc in enumerate(exceptions):
        if not isinstance(exc, dict):
            errors.append(f"exceptions[{idx}] must be an object")
            continue

        source = exc.get("source")
        target = exc.get("target")
        rule_ref = exc.get("rule")
        reason = exc.get("reason")
        status = exc.get("status")
        tracking = exc.get("tracking_issue")

        if all(isinstance(value, str) and value for value in (source, target, rule_ref)):
            identity = (source, target, rule_ref)
            if identity in seen_exception_identities:
                errors.append(
                    f"duplicate exception identity: {rule_ref}: {source} -> {target}"
                )
            seen_exception_identities.add(identity)

        if not source or not isinstance(source, str):
            errors.append(f"exceptions[{idx}]: missing or empty source")
        if not target or not isinstance(target, str):
            errors.append(f"exceptions[{idx}]: missing or empty target")
        if not rule_ref or not isinstance(rule_ref, str):
            errors.append(f"exceptions[{idx}]: missing or empty rule reference")
        elif rule_ref not in seen_rule_ids:
            errors.append(f"exceptions[{idx}]: referenced rule {rule_ref!r} not defined in rules")
        else:
            # Validate that exception source module is in rule.source_modules
            rule_obj = rule_by_id[rule_ref]
            norm_source = source.replace("\\", "/").lstrip("./")
            source_mod_id: str | None = None
            for m_obj, rx_list in matchers:
                if any(rx.match(norm_source) for rx in rx_list):
                    source_mod_id = m_obj["id"]
                    break

            if source_mod_id is not None:
                if source_mod_id not in rule_obj.get("source_modules", []):
                    errors.append(
                        f"exceptions[{idx}] ({source} -> {target}): source module {source_mod_id!r} "
                        f"is not in rule {rule_ref!r} source_modules {rule_obj.get('source_modules')!r}"
                    )
            else:
                errors.append(
                    f"exceptions[{idx}] ({source} -> {target}): exception source does not match any declared module"
                )

        if not reason or not isinstance(reason, str) or not reason.strip():
            errors.append(f"exceptions[{idx}] ({source} -> {target}): missing or empty reason")

        if status not in ALLOWED_EXCEPTION_STATUSES:
            errors.append(
                f"exceptions[{idx}] ({source} -> {target}): invalid status {status!r}; "
                f"must be one of {sorted(ALLOWED_EXCEPTION_STATUSES)}"
            )

        if not tracking or not isinstance(tracking, str) or not tracking.strip():
            errors.append(
                f"exceptions[{idx}] ({source} -> {target}): missing or empty tracking_issue"
            )

        if status == "temporary_exception":
            expires = exc.get("expires")
            removal_task = exc.get("removal_task")

            if not removal_task or not isinstance(removal_task, str) or not removal_task.strip():
                errors.append(
                    f"exceptions[{idx}] ({source} -> {target}): temporary_exception missing removal_task"
                )

            if not expires or not isinstance(expires, str) or not expires.strip():
                errors.append(
                    f"exceptions[{idx}] ({source} -> {target}): temporary_exception missing expires date"
                )
            else:
                try:
                    expiry_date = datetime.date.fromisoformat(expires)
                    if expiry_date < current_date:
                        errors.append(
                            f"exceptions[{idx}] ({source} -> {target}): temporary exception expired on {expires} "
                            f"(current date: {current_date.isoformat()})"
                        )
                except ValueError:
                    errors.append(
                        f"exceptions[{idx}] ({source} -> {target}): invalid expires date format {expires!r}, expected YYYY-MM-DD"
                    )
        elif status == "current_design":
            if exc.get("expires") is not None:
                errors.append(
                    f"exceptions[{idx}] ({source} -> {target}): current_design exception must have null expires"
                )

    errors.extend(validate_protected_impact_mappings(manifest))
    return errors


def build_module_matchers(manifest: dict[str, Any]) -> list[tuple[dict[str, Any], list[re.Pattern[str]]]]:
    """Compile path patterns for each module into regex matchers."""
    matchers = []
    for mod in manifest.get("modules", []):
        patterns = mod.get("path_patterns", [])
        regexes = [pattern_to_regex(p) for p in patterns if isinstance(p, str)]
        matchers.append((mod, regexes))
    return matchers


def classify_path(rel_path: str, manifest: dict[str, Any]) -> PathClassification:
    """Classify a path according to the machine architecture manifest. Fails closed for unknown paths."""
    normalized = rel_path.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]

    # 1. Check against declared module patterns first!
    matchers = build_module_matchers(manifest)
    for mod, regexes in matchers:
        if any(rx.match(normalized) for rx in regexes):
            return PathClassification(
                path=normalized,
                is_production=True,
                is_vendored_contract=False,
                module_id=mod["id"],
                zone=mod["zone"],
                authority=mod["authority"],
                protected_invariants=tuple(mod.get("protected_invariants", ())),
            )

    # 2. Check explicit non-production policy from manifest
    policy = manifest.get("non_production_policy", {})
    vendored_globs = policy.get("external_vendored_contracts", ["contracts/**"])
    dev_test_globs = policy.get("development_and_test_namespaces", list(DEFAULT_NON_PRODUCTION_PREFIXES))
    root_metadata = set(policy.get("root_metadata_files", list(DEFAULT_ROOT_METADATA_FILES)))

    # Exclude external vendored contracts explicitly
    for g in vendored_globs:
        if pattern_to_regex(g).match(normalized) or normalized.startswith("contracts/"):
            return PathClassification(
                path=normalized,
                is_production=False,
                is_vendored_contract=True,
                module_id=None,
                zone=None,
                authority=None,
                protected_invariants=(),
            )

    # Exclude root metadata files
    if normalized in root_metadata or ("/" not in normalized and normalized.endswith((".md", ".txt", ".ini", ".lock"))):
        return PathClassification(
            path=normalized,
            is_production=False,
            is_vendored_contract=False,
            module_id=None,
            zone=None,
            authority=None,
            protected_invariants=(),
        )

    # Exclude narrow non-production development/test namespaces
    for g in dev_test_globs:
        if (
            pattern_to_regex(g).match(normalized)
            or any(normalized.startswith(prefix) for prefix in DEFAULT_NON_PRODUCTION_PREFIXES)
            or normalized.startswith((".pytest_temp", ".pytest_cache", ".ruff_cache", ".mypy_cache"))
        ):
            return PathClassification(
                path=normalized,
                is_production=False,
                is_vendored_contract=False,
                module_id=None,
                zone=None,
                authority=None,
                protected_invariants=(),
            )

    # 3. Any other path fails closed!
    raise UnclassifiedProductionPathError(
        f"Path {normalized!r} matches no declared module in docs/architecture-boundaries.json and is not in an excluded non-production namespace"
    )


def get_tracked_files(repo_root: Path) -> list[str]:
    """Retrieve all tracked files using git ls-files, falling back to filesystem walk."""
    try:
        git_output = subprocess.check_output(
            ["git", "ls-files"], cwd=str(repo_root), text=True, stderr=subprocess.DEVNULL
        )
        files = [f.strip() for f in git_output.splitlines() if f.strip()]
        if files:
            return files
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        pass

    # Fallback to filesystem walk excluding ignore directories
    discovered: list[str] = []
    ignore_parts = {
        "__pycache__",
        "node_modules",
        ".venv",
        ".python",
        ".git",
        "dist",
        "build",
    }
    for p in repo_root.rglob("*"):
        if p.is_file() and not any(
            part in ignore_parts
            or part.startswith((".pytest_temp", ".pytest_cache", ".ruff_cache", ".mypy_cache"))
            for part in p.parts
        ):
            discovered.append(p.relative_to(repo_root).as_posix())
    return discovered


def validate_all_files_coverage(manifest: dict[str, Any], file_paths: list[str]) -> list[str]:
    """Verify that every tracked file in the repository can be classified without unclassified errors."""
    errors: list[str] = []
    for fp in file_paths:
        try:
            classify_path(fp, manifest)
        except UnclassifiedProductionPathError as exc:
            errors.append(str(exc))
    return errors


def _parse_markdown_tables(md_text: str) -> list[list[dict[str, str]]]:
    """Parse all markdown tables in a document into lists of row dicts."""
    tables: list[list[dict[str, str]]] = []
    lines = md_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("|") and line.endswith("|") and "|" in line[1:-1]:
            # Potential table header
            header_cells = [c.strip(" `").lower() for c in line.strip("|").split("|")]
            if i + 1 < len(lines):
                sep_line = lines[i + 1].strip()
                if sep_line.startswith("|") and all(
                    re.match(r"^:?-+:?$", cell.strip())
                    for cell in sep_line.strip("|").split("|")
                ):
                    # Valid table detected
                    table_rows: list[dict[str, str]] = []
                    i += 2
                    while i < len(lines):
                        row_line = lines[i].strip()
                        if not (row_line.startswith("|") and row_line.endswith("|")):
                            break
                        row_cells = [c.strip(" `") for c in row_line.strip("|").split("|")]
                        if len(row_cells) == len(header_cells):
                            row_dict = {
                                header_cells[idx]: row_cells[idx]
                                for idx in range(len(header_cells))
                            }
                            table_rows.append(row_dict)
                        i += 1
                    tables.append(table_rows)
                    continue
        i += 1
    return tables


def check_manifest_markdown_consistency(manifest: dict[str, Any], markdown_path: Path | str) -> list[str]:
    """Verify that human-readable markdown is structurally and semantically consistent with machine manifest."""
    errors: list[str] = []
    md_file = Path(markdown_path)
    if not md_file.exists():
        return [f"Markdown contract file not found: {md_file}"]

    md_text = md_file.read_text(encoding="utf-8")

    # 1. Authority statement must be present
    required_authority_statement = "docs/architecture-boundaries.json is the authoritative machine-readable architecture baseline"
    if (
        required_authority_statement not in md_text
        and f"`docs/architecture-boundaries.json` is the authoritative machine-readable architecture baseline" not in md_text
    ):
        errors.append(
            f"Markdown missing required machine-authority statement: {required_authority_statement!r}"
        )

    # 2. Parse structured tables
    tables = _parse_markdown_tables(md_text)

    modules_table: list[dict[str, str]] | None = None
    rules_table: list[dict[str, str]] | None = None
    exceptions_table: list[dict[str, str]] | None = None
    invariants_table: list[dict[str, str]] | None = None

    for tbl in tables:
        if not tbl:
            continue
        first_row_keys = set(tbl[0].keys())
        if {"module id", "zone", "authority"}.issubset(first_row_keys):
            modules_table = tbl
        elif {"rule id", "type", "enforcement"}.issubset(first_row_keys):
            rules_table = tbl
        elif {"source", "target", "rule", "status"}.issubset(first_row_keys):
            exceptions_table = tbl
        elif {"invariant id", "owner", "severity", "assurance"}.issubset(first_row_keys):
            invariants_table = tbl

    # 3. Verify modules table (bidirectional keys and exact columns)
    if modules_table is None:
        errors.append("Markdown missing structured modules table with columns 'Module ID', 'Zone', 'Authority', 'Responsibility'")
    else:
        json_modules = {m["id"]: m for m in manifest.get("modules", []) if m.get("id")}
        md_modules = {r["module id"]: r for r in modules_table}

        # Check bidirectional row keys
        missing_in_md = set(json_modules.keys()) - set(md_modules.keys())
        for m_id in sorted(missing_in_md):
            errors.append(f"Markdown modules table missing module id: {m_id}")

        extra_in_md = set(md_modules.keys()) - set(json_modules.keys())
        for m_id in sorted(extra_in_md):
            errors.append(f"Markdown modules table contains extra undeclared module id: {m_id}")

        for mod_id in sorted(set(json_modules.keys()) & set(md_modules.keys())):
            mod = json_modules[mod_id]
            row = md_modules[mod_id]
            expected_zone = str(mod.get("zone")).lower()
            actual_zone = row.get("zone", "").lower()
            if actual_zone != expected_zone:
                errors.append(
                    f"Markdown table value mismatch for module {mod_id!r}: "
                    f"zone in markdown is {actual_zone!r}, but in JSON manifest is {expected_zone!r}"
                )
            expected_auth = str(mod.get("authority")).lower()
            actual_auth = row.get("authority", "").lower()
            if actual_auth != expected_auth:
                errors.append(
                    f"Markdown table value mismatch for module {mod_id!r}: "
                    f"authority in markdown is {actual_auth!r}, but in JSON manifest is {expected_auth!r}"
                )

    # 4. Verify rules table (bidirectional keys and exact columns: type, enforcement, source/target modules, description)
    if rules_table is None:
        errors.append("Markdown missing structured rules table with columns 'Rule ID', 'Type', 'Enforcement', 'Source modules', 'Target modules', 'Description'")
    else:
        json_rules = {r["id"]: r for r in manifest.get("rules", []) if r.get("id")}
        md_rules = {r["rule id"]: r for r in rules_table}

        missing_in_md = set(json_rules.keys()) - set(md_rules.keys())
        for r_id in sorted(missing_in_md):
            errors.append(f"Markdown rules table missing rule id: {r_id}")

        extra_in_md = set(md_rules.keys()) - set(json_rules.keys())
        for r_id in sorted(extra_in_md):
            errors.append(f"Markdown rules table contains extra undeclared rule id: {r_id}")

        for rule_id in sorted(set(json_rules.keys()) & set(md_rules.keys())):
            rule = json_rules[rule_id]
            row = md_rules[rule_id]
            expected_type = str(rule.get("type")).lower()
            actual_type = row.get("type", "").lower()
            if actual_type != expected_type:
                errors.append(
                    f"Markdown table value mismatch for rule {rule_id!r}: "
                    f"type in markdown is {actual_type!r}, but in JSON manifest is {expected_type!r}"
                )
            expected_enf = str(rule.get("enforcement", {}).get("status")).lower()
            actual_enf = row.get("enforcement", "").lower()
            if actual_enf != expected_enf:
                errors.append(
                    f"Markdown table value mismatch for rule {rule_id!r}: "
                    f"enforcement in markdown is {actual_enf!r}, but in JSON manifest is {expected_enf!r}"
                )

            # Check source modules
            if "source modules" in row:
                md_sources = {s.strip(" `") for s in row["source modules"].split(",") if s.strip(" `")}
                json_sources = set(rule.get("source_modules", []))
                if md_sources != json_sources:
                    errors.append(
                        f"Markdown table value mismatch for rule {rule_id!r}: "
                        f"source modules in markdown are {sorted(md_sources)!r}, but in JSON manifest are {sorted(json_sources)!r}"
                    )

            # Check target modules
            if "target modules" in row:
                md_targets = {t.strip(" `") for t in row["target modules"].split(",") if t.strip(" `")}
                json_targets = set(rule.get("target_modules", []))
                if md_targets != json_targets:
                    errors.append(
                        f"Markdown table value mismatch for rule {rule_id!r}: "
                        f"target modules in markdown are {sorted(md_targets)!r}, but in JSON manifest are {sorted(json_targets)!r}"
                    )

            # Check description
            if "description" in row:
                norm_md_desc = " ".join(row["description"].split())
                norm_json_desc = " ".join(rule.get("description", "").split())
                if norm_md_desc != norm_json_desc:
                    errors.append(
                        f"Markdown table value mismatch for rule {rule_id!r}: "
                        f"description in markdown does not match JSON description"
                    )

    # 5. Verify exceptions table (bidirectional row keys and exact columns)
    json_exceptions = {(e["source"], e["target"]): e for e in manifest.get("exceptions", []) if e.get("source") and e.get("target")}
    if exceptions_table is None:
        if json_exceptions:
            errors.append("Markdown missing structured exceptions table with columns 'Source', 'Target', 'Rule', 'Status', 'Expires', 'Tracking Issue'")
    else:
        md_exceptions = {(r["source"], r["target"]): r for r in exceptions_table}

        missing_in_md = set(json_exceptions.keys()) - set(md_exceptions.keys())
        for src, tgt in sorted(missing_in_md):
            errors.append(f"Markdown exceptions table missing exception ({src} -> {tgt})")

        extra_in_md = set(md_exceptions.keys()) - set(json_exceptions.keys())
        for src, tgt in sorted(extra_in_md):
            errors.append(f"Markdown exceptions table contains extra undeclared exception ({src} -> {tgt})")

        for key in sorted(set(json_exceptions.keys()) & set(md_exceptions.keys())):
            exc = json_exceptions[key]
            row = md_exceptions[key]
            src, tgt = key
            if row.get("rule") != exc.get("rule"):
                errors.append(
                    f"Markdown table value mismatch for exception ({src} -> {tgt}): "
                    f"rule in markdown is {row.get('rule')!r}, but in JSON manifest is {exc.get('rule')!r}"
                )
            if row.get("status") != exc.get("status"):
                errors.append(
                    f"Markdown table value mismatch for exception ({src} -> {tgt}): "
                    f"status in markdown is {row.get('status')!r}, but in JSON manifest is {exc.get('status')!r}"
                )
            if row.get("tracking issue") != exc.get("tracking_issue"):
                errors.append(
                    f"Markdown table value mismatch for exception ({src} -> {tgt}): "
                    f"tracking issue in markdown is {row.get('tracking issue')!r}, but in JSON manifest is {exc.get('tracking_issue')!r}"
                )
            expected_expires = str(exc.get("expires"))
            actual_expires = row.get("expires", "")
            if actual_expires != expected_expires:
                errors.append(
                    f"Markdown table value mismatch for exception ({src} -> {tgt}): "
                    f"expires in markdown is {actual_expires!r}, but in JSON manifest is {expected_expires!r}"
                )

    # 6. Verify invariants table (bidirectional keys and exact columns: owner, severity, assurance, requirement)
    if invariants_table is None:
        errors.append("Markdown missing structured invariants table with columns 'Invariant ID', 'Owner', 'Severity', 'Assurance', 'Requirement / Quality scenario'")
    else:
        json_invariants = {inv["id"]: inv for inv in manifest.get("semantic_invariants", []) if inv.get("id")}
        md_invariants = {r["invariant id"]: r for r in invariants_table}

        missing_in_md = set(json_invariants.keys()) - set(md_invariants.keys())
        for inv_id in sorted(missing_in_md):
            errors.append(f"Markdown invariants table missing invariant id: {inv_id}")

        extra_in_md = set(md_invariants.keys()) - set(json_invariants.keys())
        for inv_id in sorted(extra_in_md):
            errors.append(f"Markdown invariants table contains extra undeclared invariant id: {inv_id}")

        for inv_id in sorted(set(json_invariants.keys()) & set(md_invariants.keys())):
            inv = json_invariants[inv_id]
            row = md_invariants[inv_id]

            if row.get("owner", "").strip() != inv.get("owner", "").strip():
                errors.append(
                    f"Markdown table value mismatch for invariant {inv_id!r}: "
                    f"owner in markdown is {row.get('owner')!r}, but in JSON manifest is {inv.get('owner')!r}"
                )

            expected_sev = str(inv.get("severity")).lower()
            actual_sev = row.get("severity", "").lower()
            if actual_sev != expected_sev:
                errors.append(
                    f"Markdown table value mismatch for invariant {inv_id!r}: "
                    f"severity in markdown is {actual_sev!r}, but in JSON manifest is {expected_sev!r}"
                )

            expected_ass = str(inv.get("assurance", {}).get("status")).lower()
            actual_ass = row.get("assurance", "").lower()
            if actual_ass != expected_ass:
                errors.append(
                    f"Markdown table value mismatch for invariant {inv_id!r}: "
                    f"assurance in markdown is {actual_ass!r}, but in JSON manifest is {expected_ass!r}"
                )

            if "requirement / quality scenario" in row:
                norm_md_req = " ".join(row["requirement / quality scenario"].split())
                norm_json_req = " ".join(inv.get("requirement_or_scenario", "").split())
                if norm_md_req != norm_json_req:
                    errors.append(
                        f"Markdown table value mismatch for invariant {inv_id!r}: "
                        f"requirement in markdown does not match JSON requirement"
                    )

    return errors


def main() -> int:
    """Run validation checks on the repository architecture contracts."""
    print("Validating architecture boundary manifest and contracts...")
    try:
        manifest = load_manifest(DEFAULT_MANIFEST_PATH)
    except ArchitectureBoundaryError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    schema_errors = validate_manifest_schema(manifest, repo_root=ROOT)
    if schema_errors:
        print("Schema validation failed:", file=sys.stderr)
        for err in schema_errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    consistency_errors = check_manifest_markdown_consistency(manifest, DEFAULT_MARKDOWN_PATH)
    if consistency_errors:
        print("Markdown consistency validation failed:", file=sys.stderr)
        for err in consistency_errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    tracked_files = get_tracked_files(ROOT)
    coverage_errors = validate_all_files_coverage(manifest, tracked_files)
    if coverage_errors:
        print("Path classification / coverage validation failed:", file=sys.stderr)
        for err in coverage_errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print(
        f"Architecture boundaries manifest valid. "
        f"Modules: {len(manifest.get('modules', []))}, "
        f"Rules: {len(manifest.get('rules', []))}, "
        f"Semantic invariants: {len(manifest.get('semantic_invariants', []))}, "
        f"Exceptions: {len(manifest.get('exceptions', []))}, "
        f"Tracked files checked: {len(tracked_files)}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
