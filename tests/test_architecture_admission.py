"""Guards production Brain modules from capability-admission references."""

from __future__ import annotations

import ast
import re
from pathlib import Path

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


def test_no_module_resolves_admission_into_authorization_decision() -> None:
    violations: list[str] = []

    for path in sorted(BRAIN_ROOT.rglob("*.py")):
        references = _module_references(path)
        if references:
            relative = path.relative_to(PRODUCT_ROOT).as_posix()
            violations.append(f"{relative}: reserved admission references={references}")

    assert not violations, (
        "Production Brain code references the reserved capability-admission contract "
        "surface:\n"
        + "\n".join(violations)
    )
