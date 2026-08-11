"""Guards the runtime-policy authorization boundary."""

from __future__ import annotations

import ast
from pathlib import Path


PRODUCT_ROOT = Path(__file__).resolve().parents[1]
BRAIN_ROOT = PRODUCT_ROOT / "app"

_AUTHORITY_TYPES = frozenset(
    {
        "AuthContext",
        "AuthenticatedAccountSession",
        "AuthorizationSnapshot",
        "RuntimePolicy",
    }
)
_AUTHORITY_TABLES = frozenset(
    {
        "auth_revocation_state",
        "verified_grant_references",
        "webauthn_credentials",
        "agent_pairings",
        "bridge_sessions",
        "runtime_tickets",
        "authorization_epoch",
    }
)
_SIGNED_OBJECT_VERIFIERS = frozenset(
    {
        "app.security.capability_permit_verifier",
        "app.security.grant_verifier",
    }
)


def _is_kernel_path(path: Path) -> bool:
    normalized = path.as_posix()
    return normalized.startswith("security/") or normalized == "persistence/auth.py"


def _annotation_name(annotation: ast.expr | None) -> str | None:
    if isinstance(annotation, ast.Name):
        return annotation.id
    if isinstance(annotation, ast.Attribute):
        return annotation.attr
    return None


def _root_name(node: ast.AST) -> str | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _module_violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _SIGNED_OBJECT_VERIFIERS:
            violations.add(f"imports signed-object verifier {node.module}")
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            normalized = node.value.lower()
            for table in _AUTHORITY_TABLES:
                if table in normalized:
                    violations.add(f"references authorization row {table}")

    for function in (
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        authority_arguments = {
            argument.arg
            for argument in (
                *function.args.posonlyargs,
                *function.args.args,
                *function.args.kwonlyargs,
            )
            if _annotation_name(argument.annotation) in _AUTHORITY_TYPES
        }
        if not authority_arguments:
            continue
        for node in ast.walk(function):
            if not isinstance(node, ast.Compare):
                continue
            for candidate in ast.walk(node):
                if (
                    isinstance(candidate, ast.Attribute)
                    and candidate.attr == "role"
                    and _root_name(candidate) in authority_arguments
                ):
                    violations.add(
                        f"line {node.lineno} makes a role decision outside the kernel"
                    )
    return sorted(violations)


def _runtime_policy_boundary_violations(brain_root: Path) -> list[str]:
    violations: list[str] = []
    for path in sorted(brain_root.rglob("*.py")):
        relative = path.relative_to(brain_root)
        if _is_kernel_path(relative):
            continue
        references = _module_violations(path)
        if references:
            violations.append(f"{relative.as_posix()}: {references}")
    return violations


def test_runtime_authorization_is_confined_to_the_security_kernel() -> None:
    violations = _runtime_policy_boundary_violations(BRAIN_ROOT)

    assert not violations, (
        "A module outside the runtime/security kernel independently resolves authority:\n"
        + "\n".join(violations)
    )


def test_guard_rejects_endpoint_role_decision(tmp_path: Path) -> None:
    path = tmp_path / "app" / "api" / "endpoint.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "def authorize(policy: RuntimePolicy) -> bool:\n"
        "    return policy.identity.role == 'creator'\n",
        encoding="utf-8",
    )

    assert _runtime_policy_boundary_violations(tmp_path / "app") == [
        "api/endpoint.py: ['line 2 makes a role decision outside the kernel']"
    ]
