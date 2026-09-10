#!/usr/bin/env python3
"""Fail closed when a pull request omits a required protected-impact declaration.

The architecture manifest is the only source for path classification, invariant
mapping, enforced-rule definitions, and exception-ledger entries.  This script
does not call the GitHub API: CI supplies the checked-out diff and event payload
and local callers may provide the same inputs explicitly.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

try:  # Works both as ``python tools/...`` and as ``tools....`` in pytest.
    from tools.validate_architecture_boundaries import (
        ArchitectureBoundaryError,
        UnclassifiedProductionPathError,
        classify_path,
        load_manifest,
        pattern_to_regex,
        validate_manifest_schema,
    )
except ModuleNotFoundError:  # pragma: no cover - exercised by direct CLI use.
    from validate_architecture_boundaries import (  # type: ignore[no-redef]
        ArchitectureBoundaryError,
        UnclassifiedProductionPathError,
        classify_path,
        load_manifest,
        pattern_to_regex,
        validate_manifest_schema,
    )


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST_PATH = ROOT / "docs" / "architecture-boundaries.json"
MANIFEST_REPOSITORY_PATH = "docs/architecture-boundaries.json"
ZONE_RANK = {"green": 0, "yellow": 1, "orange": 2, "red": 3}
DECLARATION_FIELDS = (
    "Architecture rationale",
    "Potentially affected invariant dispositions",
    "Invariant / boundary actually affected",
    "Safety evidence",
)
SECTION_HEADING = "Architecture impact"
HEADING = re.compile(r"^ {0,3}#{1,2}\s+")
FENCE_RUN = re.compile(r"^ {0,3}([`~]+)")


class BoundaryDeclarationError(ValueError):
    """Raised for missing or malformed deterministic gate inputs."""


@dataclass(frozen=True)
class ArchitectureImpact:
    """Stable classifier output plus non-rendered governance trigger details."""

    maximum_zone: str
    affected_modules: tuple[str, ...]
    potentially_affected_invariants: tuple[str, ...]
    affected_rules: tuple[str, ...]
    exceptions_changed: tuple[str, ...]
    unclassified_paths: tuple[str, ...]
    authority_changes: tuple[str, ...] = ()
    invariant_definition_changes: tuple[str, ...] = ()
    protected_mapping_changed: bool = False

    def as_dict(self) -> dict[str, object]:
        """Return only the stable public architecture_impact contract."""

        return {
            "maximum_zone": self.maximum_zone,
            "affected_modules": list(self.affected_modules),
            "potentially_affected_invariants": list(self.potentially_affected_invariants),
            "affected_rules": list(self.affected_rules),
            "exceptions_changed": list(self.exceptions_changed),
            "unclassified_paths": list(self.unclassified_paths),
        }

    @property
    def has_governance_impact(self) -> bool:
        return bool(
            self.authority_changes
            or self.invariant_definition_changes
            or self.protected_mapping_changed
        )


@dataclass(frozen=True)
class InvariantDisposition:
    invariant_id: str
    status: str
    rationale: str | None


@dataclass(frozen=True)
class ArchitectureDeclaration:
    fields: dict[str, str]
    dispositions: dict[str, InvariantDisposition]
    dispositions_are_na: bool


def _normalized_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _identifier_in(text: str, identifier: str) -> bool:
    """Match an identifier as a complete token, never as a substring."""

    return re.search(
        rf"(?<![a-z0-9_-]){re.escape(identifier.lower())}(?![a-z0-9_-])",
        text.lower(),
    ) is not None


def _exception_label(entry: dict[str, Any]) -> str:
    return f"{entry.get('rule', '<unknown-rule>')}: {entry.get('source', '<unknown-source>')} -> {entry.get('target', '<unknown-target>')}"


def _entries_by_id(entries: object) -> dict[str, dict[str, Any]]:
    if not isinstance(entries, list):
        return {}
    return {
        entry["id"]: entry
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    }


def _exception_entries(entries: object) -> dict[str, dict[str, Any]]:
    if not isinstance(entries, list):
        return {}
    indexed: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = _exception_label(entry)
        indexed[key] = entry
    return indexed


def _changed_ids(current: object, previous: object) -> set[str]:
    current_by_id = _entries_by_id(current)
    previous_by_id = _entries_by_id(previous)
    return {
        identifier
        for identifier in set(current_by_id) | set(previous_by_id)
        if _canonical(current_by_id.get(identifier)) != _canonical(previous_by_id.get(identifier))
    }


def _manifest_mapping_entries(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    protected_impact = manifest.get("protected_impact", {})
    if not isinstance(protected_impact, dict):
        return []
    entries = protected_impact.get("invariant_path_mappings", [])
    return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []


def _mapping_invariants_for_path(path: str, manifests: Iterable[dict[str, Any]]) -> set[str]:
    result: set[str] = set()
    for manifest in manifests:
        for entry in _manifest_mapping_entries(manifest):
            invariant = entry.get("invariant")
            patterns = entry.get("path_patterns")
            if not isinstance(invariant, str) or not isinstance(patterns, list):
                continue
            if any(
                isinstance(pattern, str) and pattern_to_regex(pattern).match(path)
                for pattern in patterns
            ):
                result.add(invariant)
    return result


def _manifest_governance_changes(
    current: dict[str, Any], previous: dict[str, Any]
) -> tuple[set[str], set[str], bool]:
    """Return changed authority/trust and invariant declaration identifiers."""

    current_modules = _entries_by_id(current.get("modules"))
    previous_modules = _entries_by_id(previous.get("modules"))
    authority_changes: set[str] = set()
    for module_id in set(current_modules) | set(previous_modules):
        current_module = current_modules.get(module_id, {})
        previous_module = previous_modules.get(module_id, {})
        protected_fields = ("authority", "permitted_inputs", "permitted_outputs")
        if any(
            _canonical(current_module.get(field)) != _canonical(previous_module.get(field))
            for field in protected_fields
        ):
            authority_changes.add(module_id)

    invariant_changes = _changed_ids(
        current.get("semantic_invariants"), previous.get("semantic_invariants")
    )
    mapping_changed = _canonical(current.get("protected_impact")) != _canonical(
        previous.get("protected_impact")
    )
    return authority_changes, invariant_changes, mapping_changed


def classify_architecture_impact(
    changed_paths: Iterable[str],
    manifest: dict[str, Any],
    *,
    base_manifest: dict[str, Any] | None = None,
) -> ArchitectureImpact:
    """Classify paths through the manifest without inferring semantic certainty."""

    paths = sorted({_normalized_path(path) for path in changed_paths if path and path.strip()})
    modules: set[str] = set()
    zones: list[str] = []
    potential_invariants: set[str] = set()
    unclassified: list[str] = []
    mapping_manifests = (manifest,) if base_manifest is None else (manifest, base_manifest)

    for path in paths:
        try:
            classification = classify_path(path, manifest)
        except UnclassifiedProductionPathError:
            unclassified.append(path)
            continue
        if classification.module_id is not None:
            modules.add(classification.module_id)
        if classification.zone is not None:
            zones.append(classification.zone)
        if classification.is_production:
            potential_invariants.update(_mapping_invariants_for_path(path, mapping_manifests))

    affected_rules: set[str] = set()
    exceptions_changed: set[str] = set()
    authority_changes: set[str] = set()
    invariant_definition_changes: set[str] = set()
    protected_mapping_changed = False
    if MANIFEST_REPOSITORY_PATH in paths and base_manifest is not None:
        current_rules = _entries_by_id(manifest.get("rules"))
        previous_rules = _entries_by_id(base_manifest.get("rules"))
        for rule_id in _changed_ids(manifest.get("rules"), base_manifest.get("rules")):
            current_rule = current_rules.get(rule_id, {})
            previous_rule = previous_rules.get(rule_id, {})
            current_status = current_rule.get("enforcement", {}).get("status")
            previous_status = previous_rule.get("enforcement", {}).get("status")
            if "enforced" in {current_status, previous_status}:
                affected_rules.add(rule_id)

        current_exceptions = _exception_entries(manifest.get("exceptions"))
        previous_exceptions = _exception_entries(base_manifest.get("exceptions"))
        for label in set(current_exceptions) | set(previous_exceptions):
            if _canonical(current_exceptions.get(label)) != _canonical(previous_exceptions.get(label)):
                exceptions_changed.add(label)

        (
            authority_changes,
            invariant_definition_changes,
            protected_mapping_changed,
        ) = _manifest_governance_changes(manifest, base_manifest)

    maximum_zone = "unknown" if unclassified else "none"
    if zones:
        maximum_zone = max(zones, key=lambda zone: ZONE_RANK[zone])

    return ArchitectureImpact(
        maximum_zone=maximum_zone,
        affected_modules=tuple(sorted(modules)),
        potentially_affected_invariants=tuple(sorted(potential_invariants)),
        affected_rules=tuple(sorted(affected_rules)),
        exceptions_changed=tuple(sorted(exceptions_changed)),
        unclassified_paths=tuple(sorted(unclassified)),
        authority_changes=tuple(sorted(authority_changes)),
        invariant_definition_changes=tuple(sorted(invariant_definition_changes)),
        protected_mapping_changed=protected_mapping_changed,
    )


def _markdown_lines_outside_fences(body: str) -> list[str]:
    """Blank CommonMark fenced blocks so declarations cannot be code-block spoofed."""

    lines: list[str] = []
    active_fence: tuple[str, int] | None = None
    for line in body.splitlines():
        match = FENCE_RUN.match(line)
        fence_character = match.group(1)[0] if match is not None else None
        fence_length = len(match.group(1)) if match is not None else 0
        if active_fence is None and fence_length >= 3:
            active_fence = (fence_character, fence_length)
            lines.append("")
        elif active_fence is not None:
            opening_character, opening_length = active_fence
            is_matching_closer = (
                fence_character == opening_character
                and fence_length >= opening_length
                and match is not None
                and not line[match.end() :].strip(" \t")
            )
            if is_matching_closer:
                active_fence = None
            lines.append("")
        else:
            lines.append(line)
    return lines


def _is_na(value: str) -> bool:
    return " ".join(value.strip().split()).lower() in {"n/a", "na", "not applicable"}


def _is_placeholder(value: str) -> bool:
    normalized = " ".join(value.strip().split()).lower()
    if not normalized or _is_na(normalized):
        return True
    if normalized in {"none", "todo", "tbd", "placeholder", "same as above", "..."}:
        return True
    if (normalized.startswith("[") and normalized.endswith("]")) or (
        normalized.startswith("<") and normalized.endswith(">")
    ):
        return True
    return any(
        phrase in normalized
        for phrase in (
            "replace this",
            "describe architecture",
            "describe the architecture",
            "provide safety evidence",
            "provide evidence",
            "insert rationale",
        )
    )


def _parse_dispositions(value: str) -> tuple[dict[str, InvariantDisposition], bool, list[str]]:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if len(lines) == 1 and _is_na(lines[0]):
        return {}, True, []
    if not lines:
        return {}, False, ["Potentially affected invariant dispositions must be N/A or explicit dispositions"]

    errors: list[str] = []
    dispositions: dict[str, InvariantDisposition] = {}
    index = 0
    while index < len(lines):
        header = lines[index]
        match = re.fullmatch(r"([a-z0-9][a-z0-9-]*):", header)
        if match is None:
            errors.append(
                f"invalid invariant disposition line {header!r}; use an invariant id followed by ':' on its own line"
            )
            break
        invariant_id = match.group(1)
        if invariant_id in dispositions:
            errors.append(f"duplicate invariant disposition for {invariant_id}")
        index += 1
        if index >= len(lines):
            errors.append(f"invariant {invariant_id} is missing an affected/not affected disposition")
            break
        status = lines[index].lower()
        if status not in {"affected", "not affected"}:
            errors.append(
                f"invariant {invariant_id} has invalid disposition {lines[index]!r}; use affected or not affected"
            )
            break
        index += 1
        rationale: str | None = None
        if status == "not affected":
            if index >= len(lines) or not lines[index].lower().startswith("rationale:"):
                errors.append(f"invariant {invariant_id} marked not affected requires a non-empty rationale")
            else:
                rationale = lines[index][len("rationale:") :].strip()
                if _is_placeholder(rationale):
                    errors.append(f"invariant {invariant_id} marked not affected requires a non-empty rationale")
                index += 1
        dispositions.setdefault(
            invariant_id,
            InvariantDisposition(
                invariant_id=invariant_id,
                status=status,
                rationale=rationale,
            ),
        )
    return dispositions, False, errors


def parse_architecture_declaration(body: str) -> tuple[ArchitectureDeclaration | None, list[str]]:
    """Parse exactly one machine-readable Architecture impact section."""

    lines = _markdown_lines_outside_fences(body)
    section_heading = re.compile(rf"^ {{0,3}}##\s+{re.escape(SECTION_HEADING)}\s*$")
    section_starts = [index for index, line in enumerate(lines) if section_heading.fullmatch(line)]
    if not section_starts:
        return None, ["PR body is missing the exact '## Architecture impact' section"]
    if len(section_starts) != 1:
        return None, ["PR body contains duplicate '## Architecture impact' sections"]

    start = section_starts[0] + 1
    end = len(lines)
    for index in range(start, len(lines)):
        if HEADING.match(lines[index]):
            end = index
            break
    section = lines[start:end]
    field_positions: dict[str, list[tuple[int, str]]] = {field: [] for field in DECLARATION_FIELDS}
    field_pattern = re.compile(
        r"^ {0,3}(Architecture rationale|Potentially affected invariant dispositions|Invariant / boundary actually affected|Safety evidence):(?:\s*(.*))?$"
    )
    for index, line in enumerate(section):
        match = field_pattern.fullmatch(line)
        if match is not None:
            field_positions[match.group(1)].append((index, match.group(2) or ""))

    errors: list[str] = []
    for field, positions in field_positions.items():
        if not positions:
            errors.append(f"Architecture impact section is missing field '{field}:'")
        elif len(positions) > 1:
            errors.append(f"Architecture impact section contains duplicate field '{field}:'")
    if errors:
        return None, errors

    all_positions = sorted(
        (position, field, inline_value)
        for field, positions in field_positions.items()
        for position, inline_value in positions
    )
    fields: dict[str, str] = {}
    for current_index, (position, field, inline_value) in enumerate(all_positions):
        next_position = (
            all_positions[current_index + 1][0]
            if current_index + 1 < len(all_positions)
            else len(section)
        )
        values = [inline_value] if inline_value.strip() else []
        values.extend(section[position + 1 : next_position])
        fields[field] = "\n".join(values).strip()

    dispositions, is_na, disposition_errors = _parse_dispositions(
        fields["Potentially affected invariant dispositions"]
    )
    return (
        ArchitectureDeclaration(fields=fields, dispositions=dispositions, dispositions_are_na=is_na),
        disposition_errors,
    )


def validate_protected_impact_declaration(
    impact: ArchitectureImpact, body: str
) -> list[str]:
    """Validate dispositions and evidence only where protected impact warrants it."""

    declaration, errors = parse_architecture_declaration(body)
    if declaration is None:
        return errors

    expected = set(impact.potentially_affected_invariants)
    actual = set(declaration.dispositions)
    if expected and declaration.dispositions_are_na:
        errors.append("N/A is not a valid disposition for machine-matched protected invariants")
    for invariant in sorted(actual - expected):
        errors.append(f"unknown invariant disposition {invariant!r}; it was not machine-matched")
    for invariant in sorted(expected - actual):
        errors.append(f"machine-matched invariant {invariant!r} requires exactly one disposition")

    other_protected_impact = bool(
        impact.affected_rules or impact.exceptions_changed or impact.has_governance_impact
    )
    if not expected and not other_protected_impact and not declaration.dispositions_are_na:
        errors.append("invariant dispositions must be N/A when the classifier reports no potential invariant impact")

    confirmed_invariants = {
        invariant
        for invariant, disposition in declaration.dispositions.items()
        if invariant in expected and disposition.status == "affected"
    }
    confirmed = bool(confirmed_invariants or other_protected_impact)
    if not confirmed:
        return errors

    rationale = declaration.fields["Architecture rationale"]
    actual_boundary = declaration.fields["Invariant / boundary actually affected"]
    evidence = declaration.fields["Safety evidence"]
    if _is_placeholder(rationale):
        errors.append("confirmed protected impact requires meaningful Architecture rationale")
    if _is_placeholder(actual_boundary):
        errors.append("confirmed protected impact requires an actual invariant / boundary declaration")
    if _is_placeholder(evidence):
        errors.append("confirmed protected impact requires meaningful Safety evidence")

    for invariant in sorted(confirmed_invariants):
        if not _identifier_in(actual_boundary, invariant):
            errors.append(
                f"actual invariant / boundary declaration must name affected invariant {invariant!r}"
            )
    for rule in impact.affected_rules:
        if not _identifier_in(actual_boundary, rule):
            errors.append(f"actual invariant / boundary declaration must name affected rule {rule!r}")
    if impact.exceptions_changed and not _identifier_in(actual_boundary, "exception"):
        errors.append("actual invariant / boundary declaration must identify the changed exception")
    if impact.authority_changes and not (
        _identifier_in(actual_boundary, "authority") or _identifier_in(actual_boundary, "trust")
    ):
        errors.append("actual invariant / boundary declaration must identify the affected authority or trust relationship")
    if impact.invariant_definition_changes and not _identifier_in(actual_boundary, "invariant"):
        errors.append("actual invariant / boundary declaration must identify the changed invariant declaration")
    if impact.protected_mapping_changed and not _identifier_in(actual_boundary, "mapping"):
        errors.append("actual invariant / boundary declaration must identify the changed protected-impact mapping")
    return errors


def validate_gate(
    changed_paths: Iterable[str],
    body: str | None,
    manifest: dict[str, Any],
    *,
    base_manifest: dict[str, Any] | None = None,
    reference_date: dt.date | None = None,
    require_declaration: bool = True,
) -> tuple[ArchitectureImpact, list[str]]:
    """Run deterministic manifest, classifier, and declaration validation."""

    errors = validate_manifest_schema(manifest, repo_root=ROOT, reference_date=reference_date)
    impact = classify_architecture_impact(changed_paths, manifest, base_manifest=base_manifest)
    for path in impact.unclassified_paths:
        errors.append(
            f"unclassified production path {path!r}; add it to docs/architecture-boundaries.json before merging"
        )
    if require_declaration:
        if body is None:
            errors.append("PR body is required; pass --pr-body-file, --pr-body, or --event-path")
        else:
            errors.extend(validate_protected_impact_declaration(impact, body))
    return impact, errors


def _read_changed_files_file(path: Path) -> list[str]:
    if not path.is_file():
        raise BoundaryDeclarationError(f"changed-files input does not exist: {path}")
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _git_changed_files(base_ref: str, head_ref: str) -> list[str]:
    try:
        completed = subprocess.run(
            ["git", "diff", "--name-only", base_ref, head_ref],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        raise BoundaryDeclarationError(
            f"cannot determine changed files from git diff {base_ref!r}..{head_ref!r}: {detail}"
        ) from exc
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _git_manifest_at(base_ref: str) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["git", "show", f"{base_ref}:{MANIFEST_REPOSITORY_PATH}"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        return json.loads(completed.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        raise BoundaryDeclarationError(
            f"cannot read base architecture manifest at {base_ref!r}: {detail}"
        ) from exc


def _pr_body_from_event(path: Path) -> str:
    if not path.is_file():
        raise BoundaryDeclarationError(f"GitHub event payload does not exist: {path}")
    try:
        event = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BoundaryDeclarationError(f"GitHub event payload is not JSON: {path}") from exc
    body = event.get("pull_request", {}).get("body") if isinstance(event, dict) else None
    if not isinstance(body, str):
        raise BoundaryDeclarationError(
            "GitHub event payload has no pull_request.body; this gate must run on a pull_request event"
        )
    return body


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    changed_group = parser.add_mutually_exclusive_group()
    changed_group.add_argument(
        "--changed-file",
        action="append",
        dest="changed_files",
        help="Explicit repository-relative changed path; repeat for each path.",
    )
    changed_group.add_argument(
        "--changed-files-file",
        type=Path,
        help="UTF-8 newline-separated repository-relative changed paths.",
    )
    parser.add_argument("--base-ref", help="Git ref/SHA used as the diff and base-manifest side.")
    parser.add_argument("--head-ref", default="HEAD", help="Git ref/SHA for the changed side (default: HEAD).")
    body_group = parser.add_mutually_exclusive_group()
    body_group.add_argument("--pr-body", help="PR body text for local deterministic use.")
    body_group.add_argument("--pr-body-file", type=Path, help="UTF-8 file containing the PR body.")
    body_group.add_argument("--event-path", type=Path, help="GitHub pull_request event payload JSON.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument(
        "--reference-date",
        type=dt.date.fromisoformat,
        help="UTC ISO date for deterministic temporary-exception expiry validation.",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Report classifier output without requiring a PR declaration (local diagnostics only).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    try:
        if args.changed_files is not None:
            changed_paths = args.changed_files
        elif args.changed_files_file is not None:
            changed_paths = _read_changed_files_file(args.changed_files_file)
        elif args.base_ref:
            changed_paths = _git_changed_files(args.base_ref, args.head_ref)
        else:
            raise BoundaryDeclarationError(
                "changed-file input is required; pass --changed-file(s), --changed-files-file, or --base-ref"
            )

        manifest = load_manifest(args.manifest)
        normalized_paths = {_normalized_path(path) for path in changed_paths}
        base_manifest = _git_manifest_at(args.base_ref) if args.base_ref else None
        if MANIFEST_REPOSITORY_PATH in normalized_paths and base_manifest is None:
            raise BoundaryDeclarationError(
                "--base-ref is required when docs/architecture-boundaries.json changes so rule and exception changes can be checked"
            )

        body: str | None = None
        if not args.report_only:
            if args.pr_body is not None:
                body = args.pr_body
            elif args.pr_body_file is not None:
                if not args.pr_body_file.is_file():
                    raise BoundaryDeclarationError(f"PR body file does not exist: {args.pr_body_file}")
                body = args.pr_body_file.read_text(encoding="utf-8")
            elif args.event_path is not None:
                body = _pr_body_from_event(args.event_path)
            else:
                raise BoundaryDeclarationError(
                    "PR body input is required; pass --pr-body-file, --pr-body, or --event-path"
                )

        impact, errors = validate_gate(
            changed_paths,
            body,
            manifest,
            base_manifest=base_manifest,
            reference_date=args.reference_date,
            require_declaration=not args.report_only,
        )
    except (ArchitectureBoundaryError, BoundaryDeclarationError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2

    print(json.dumps({"architecture_impact": impact.as_dict()}, indent=2, sort_keys=True))
    if errors:
        print("Architecture impact declaration failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print("Architecture impact declaration passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
