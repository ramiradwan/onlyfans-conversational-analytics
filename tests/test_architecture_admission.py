"""Guards grant and licence authorization modules from permit admission references."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PRODUCT_ROOT = Path(__file__).resolve().parents[1]
BRAIN_ROOT = PRODUCT_ROOT / "app"

_RESERVED_ADMISSION_MARKERS = frozenset(
    {
        "analysis_run_manifest",
        "analysis_run_result",
        "attempt_fence",
        "authorization_epoch",
        "backup_set_id",
        "capability_admission",
        "capability_permit",
        "credit_cost",
        "execution_limit",
        "job_attempt_id",
        "permit_admission",
        "permit_spent",
        "permit_state",
        "pending_admission",
        "promised_generation_id",
        "request_idempotency_key",
        "requested_job_id",
        "reserved_job_id",
        "single_execution_admission",
    }
)
_RESERVED_ADMISSION_LITERALS = frozenset(
    {
        "urn:bridge-clean:capability-permit:v1",
        "urn:bridge-clean:local-brain:analysis-run-manifest:v1",
        "urn:bridge-clean:local-brain:analysis-run-result:v1",
        "urn:bridge-clean:local-brain:capability-permit",
    }
)


def _normalized_identifier(value: str) -> str:
    snake_case = re.sub(r"(?<!^)(?=[A-Z])", "_", value)
    return snake_case.replace("-", "_").lower()


def _is_grant_or_licence_authorization_path(path: Path) -> bool:
    normalized = path.as_posix().replace("-", "_").lower()
    return "grant" in normalized or "licence" in normalized or "license" in normalized


def _module_references(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    identifiers: set[str] = set()
    literals: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(_normalized_identifier(node.id))
        elif isinstance(node, ast.Attribute):
            identifiers.add(_normalized_identifier(node.attr))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            identifiers.add(_normalized_identifier(node.name))
        elif isinstance(node, ast.alias):
            identifiers.update(
                _normalized_identifier(part) for part in node.name.split(".")
            )
            if node.asname:
                identifiers.add(_normalized_identifier(node.asname))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals.add(node.value)

    hits = {
        marker
        for marker in _RESERVED_ADMISSION_MARKERS
        if any(marker in identifier for identifier in identifiers)
    }
    hits.update(
        literal
        for literal in _RESERVED_ADMISSION_LITERALS
        if any(literal in value for value in literals)
    )
    return sorted(hits)


def _grant_or_licence_admission_violations(brain_root: Path) -> list[str]:
    violations: list[str] = []

    for path in sorted(brain_root.rglob("*.py")):
        relative = path.relative_to(brain_root)
        if not _is_grant_or_licence_authorization_path(relative):
            continue
        references = _module_references(path)
        if references:
            violations.append(f"{relative.as_posix()}: reserved admission references={references}")
    return violations


def test_no_grant_or_licence_authorization_module_resolves_permit_admission() -> None:
    violations = _grant_or_licence_admission_violations(BRAIN_ROOT)

    assert not violations, (
        "Grant or licence authorization code references the reserved permit-admission contract "
        "surface:\n"
        + "\n".join(violations)
    )


@pytest.mark.parametrize("module_name", ["capability_licence.py", "grant_verifier.py"])
def test_guard_rejects_permit_identifier_in_grant_or_licence_path(
    tmp_path: Path, module_name: str
) -> None:
    path = tmp_path / "app" / "security" / module_name
    path.parent.mkdir(parents=True)
    path.write_text("execution_limit = 1\n", encoding="utf-8")

    assert _grant_or_licence_admission_violations(tmp_path / "app") == [
        f"security/{module_name}: reserved admission references=['execution_limit']"
    ]
