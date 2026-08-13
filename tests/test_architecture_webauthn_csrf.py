"""Structural guard for the narrowly scoped WebAuthn CSRF exemption."""

from __future__ import annotations

import ast
from pathlib import Path


PRODUCT_ROOT = Path(__file__).resolve().parents[1]
ENDPOINTS_ROOT = PRODUCT_ROOT / "app" / "api" / "endpoints"
_CEREMONY_HANDLER_NAMES = frozenset(
    {
        "begin_registration",
        "finish_registration",
        "begin_login",
        "finish_login",
    }
)
_STATE_CHANGING_METHODS = frozenset({"post", "put", "delete", "patch"})


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return node.func.id if isinstance(node.func, ast.Name) else None


def _is_state_changing_route(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr in _STATE_CHANGING_METHODS
        for decorator in function.decorator_list
    )


def _runtime_policy_parameter(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    return any(
        isinstance(argument.annotation, ast.Name)
        and argument.annotation.id == "RuntimePolicy"
        for argument in function.args.args
    )


def _has_visible_identity_guard(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    return any(
        isinstance(statement, ast.If)
        and isinstance(statement.test, ast.Compare)
        and len(statement.test.ops) == 1
        and isinstance(statement.test.ops[0], ast.IsNot)
        and isinstance(statement.test.left, ast.Attribute)
        and isinstance(statement.test.left.value, ast.Name)
        and statement.test.left.value.id == "policy"
        and statement.test.left.attr == "identity"
        and any(
            isinstance(node, ast.Call) and _call_name(node) == "verify_csrf_token"
            for node in ast.walk(statement)
        )
        for statement in function.body
    )


def _csrf_boundary_violations(endpoints_root: Path) -> list[str]:
    violations: list[str] = []
    for path in sorted(endpoints_root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for function in (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and _is_state_changing_route(node)
            and _runtime_policy_parameter(node)
        ):
            calls = {
                _call_name(node)
                for node in ast.walk(function)
                if isinstance(node, ast.Call)
            }
            if path.name == "webauthn.py" and function.name in _CEREMONY_HANDLER_NAMES:
                if not _has_visible_identity_guard(function):
                    violations.append(
                        f"{path.name}:{function.name} lacks its visible identity guard"
                    )
            elif "verify_csrf_token" not in calls:
                violations.append(
                    f"{path.name}:{function.name} can change state without CSRF"
                )
    return violations


def test_only_webauthn_ceremony_routes_can_skip_session_csrf() -> None:
    assert _csrf_boundary_violations(ENDPOINTS_ROOT) == []


def test_guard_rejects_a_non_ceremony_state_changing_route_without_csrf(
    tmp_path: Path,
) -> None:
    endpoint = tmp_path / "history.py"
    endpoint.write_text(
        "@router.post('/state')\n"
        "def change(policy: RuntimePolicy):\n"
        "    return {}\n",
        encoding="utf-8",
    )

    assert _csrf_boundary_violations(tmp_path) == [
        "history.py:change can change state without CSRF"
    ]
