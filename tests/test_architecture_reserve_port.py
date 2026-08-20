"""Guards the reserve port from background callers, plus a partial cost check.

The carried-cost check here is syntactic and single-expression: it reads one
expression at a time and has no dataflow, so it sees interpretation only where
the value is named in the interpreting expression itself. Interpretation after
any hop -- a local, a chain of locals, a container bound to a local, or a
function parameter -- is invisible to it and is documented as such at
``test_guard_does_not_see_interpretation_after_a_hop``. The runtime boundary is
``CarriedCost``, covered by ``tests/test_carried_cost_value.py``; this check is
defence in depth over the two modules that own the value.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app.security.permit_reserve_port import reserve_admission


PRODUCT_ROOT = Path(__file__).resolve().parents[1]
BRAIN_ROOT = PRODUCT_ROOT / "app"

RESERVE_PORT_MODULE = "app.security.permit_reserve_port"
RESERVE_PORT_NAME = "permit_reserve_port"
_RESERVE_PORT_SYMBOLS = frozenset(
    {"ReserveOutcome", "ReserveRequest", "reserve_admission"}
)
_CONFIRMATION_MINT_SYMBOL = "issue_admission_confirmation"
_CONFIRMATION_MINT_ADAPTER = "api/security.py"
_CARRIED_COST_MODULES = (
    "security/admission_confirmation.py",
    "security/permit_reserve_port.py",
)
_BACKGROUND_PLANE_MARKERS = (
    "background",
    "pipeline",
    "projection",
    "rebuild",
    "scheduler",
    "scheduling",
    "worker",
)
_BINDING_ACQUISITION_MODULE = "app.provisioning.binding_acquisition"
_BINDING_ACQUISITION_ACTION = "acquire_creator_account_binding"
_EXPECTED_BACKGROUND_MODULES = frozenset(
    {
        "analytics/pipeline.py",
        "analytics/projection_store.py",
        "analytics/rebuild.py",
        "analytics/resilient_projection_store.py",
        "analytics/scheduling.py",
        "analytics/sqlite_projection_store.py",
        "persistence/projection_activation.py",
    }
)
_ORDERED_COMPARISONS = (ast.Lt, ast.LtE, ast.Gt, ast.GtE)
_ARITHMETIC_UNARY_OPERATORS = (ast.Invert, ast.UAdd, ast.USub)


def _is_background_plane_path(path: Path) -> bool:
    normalized = path.as_posix().replace("-", "_").lower()
    return any(marker in normalized for marker in _BACKGROUND_PLANE_MARKERS)


def _background_plane_modules(brain_root: Path) -> frozenset[str]:
    return frozenset(
        path.relative_to(brain_root).as_posix()
        for path in brain_root.rglob("*.py")
        if _is_background_plane_path(path.relative_to(brain_root))
    )


def _reserve_port_references(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    references: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == RESERVE_PORT_MODULE:
                    references.add(f"imports {RESERVE_PORT_MODULE}")
        elif isinstance(node, ast.ImportFrom):
            if node.module == RESERVE_PORT_MODULE:
                references.add(f"imports from {RESERVE_PORT_MODULE}")
            if any(alias.name == RESERVE_PORT_NAME for alias in node.names):
                references.add(f"imports {RESERVE_PORT_NAME}")
            for alias in node.names:
                if alias.name in _RESERVE_PORT_SYMBOLS:
                    references.add(f"references {alias.name}")
        elif isinstance(node, ast.Name) and node.id in _RESERVE_PORT_SYMBOLS:
            references.add(f"references {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in _RESERVE_PORT_SYMBOLS:
            references.add(f"references {node.attr}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if RESERVE_PORT_NAME in node.value:
                references.add(f"names {RESERVE_PORT_NAME}")
    return sorted(references)


def _background_reserve_violations(brain_root: Path) -> list[str]:
    violations: list[str] = []
    for path in sorted(brain_root.rglob("*.py")):
        relative = path.relative_to(brain_root)
        if not _is_background_plane_path(relative):
            continue
        references = _reserve_port_references(path)
        if references:
            violations.append(f"{relative.as_posix()}: {references}")
    return violations


def _binding_acquisition_references(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    references: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _BINDING_ACQUISITION_MODULE:
                    references.add(f"imports {_BINDING_ACQUISITION_MODULE}")
        elif isinstance(node, ast.ImportFrom):
            if node.module == _BINDING_ACQUISITION_MODULE:
                references.add(f"imports from {_BINDING_ACQUISITION_MODULE}")
            if any(alias.name == _BINDING_ACQUISITION_ACTION for alias in node.names):
                references.add(f"references {_BINDING_ACQUISITION_ACTION}")
        elif isinstance(node, ast.Name) and node.id == _BINDING_ACQUISITION_ACTION:
            references.add(f"references {_BINDING_ACQUISITION_ACTION}")
        elif (
            isinstance(node, ast.Attribute)
            and node.attr == _BINDING_ACQUISITION_ACTION
        ):
            references.add(f"references {_BINDING_ACQUISITION_ACTION}")
    return sorted(references)


def _background_binding_acquisition_violations(brain_root: Path) -> list[str]:
    violations: list[str] = []
    for path in sorted(brain_root.rglob("*.py")):
        relative = path.relative_to(brain_root)
        if not _is_background_plane_path(relative):
            continue
        references = _binding_acquisition_references(path)
        if references:
            violations.append(f"{relative.as_posix()}: {references}")
    return violations


def _mint_importers(brain_root: Path) -> list[str]:
    importers: list[str] = []
    for path in sorted(brain_root.rglob("*.py")):
        relative = path.relative_to(brain_root).as_posix()
        if relative.startswith("security/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and any(
                alias.name == _CONFIRMATION_MINT_SYMBOL for alias in node.names
            ):
                importers.append(relative)
                break
    return importers


def _mentions_carried_cost(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id.endswith("credit_cost"):
            return True
        if isinstance(child, ast.Attribute) and child.attr.endswith("credit_cost"):
            return True
    return False


def _carried_cost_violations(path: Path) -> list[str]:
    """Return single-expression interpretations of the carried cost in one file.

    Detection is limited to expressions that name the value directly. It has no
    dataflow and therefore no claim to completeness.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, (ast.BinOp, ast.AugAssign)) or (
            isinstance(node, ast.UnaryOp)
            and isinstance(node.op, _ARITHMETIC_UNARY_OPERATORS)
        ):
            if _mentions_carried_cost(node):
                violations.add(f"line {node.lineno} computes with the carried cost")
        elif isinstance(node, ast.Compare):
            ordered = any(isinstance(op, _ORDERED_COMPARISONS) for op in node.ops)
            if ordered and _mentions_carried_cost(node):
                violations.add(f"line {node.lineno} orders the carried cost")
    return sorted(violations)


def test_background_plane_classification_is_not_empty() -> None:
    modules = _background_plane_modules(BRAIN_ROOT)

    missing = sorted(_EXPECTED_BACKGROUND_MODULES - modules)
    assert not missing, (
        "The scheduler, worker, and projection classification no longer covers:\n"
        + "\n".join(missing)
    )


def test_no_background_module_imports_the_reserve_port() -> None:
    violations = _background_reserve_violations(BRAIN_ROOT)

    assert not violations, (
        "A scheduler, worker, or projection module reaches the reserve port:\n"
        + "\n".join(violations)
    )


def _assert_no_background_module_imports_the_binding_acquisition_action(
    brain_root: Path,
) -> None:
    violations = _background_binding_acquisition_violations(brain_root)

    assert not violations, (
        "A scheduler, worker, or background module imports the binding acquisition "
        "action:\n" + "\n".join(violations)
    )


def test_no_background_module_imports_the_binding_acquisition_action() -> None:
    _assert_no_background_module_imports_the_binding_acquisition_action(BRAIN_ROOT)


def test_guard_rejects_reserve_port_import_in_a_background_module(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app" / "analytics" / "scheduling.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        f"from {RESERVE_PORT_MODULE} import reserve_admission\n",
        encoding="utf-8",
    )

    assert _background_reserve_violations(tmp_path / "app") == [
        "analytics/scheduling.py: "
        f"['imports from {RESERVE_PORT_MODULE}', 'references reserve_admission']"
    ]


def test_guard_rejects_binding_acquisition_import_in_a_background_module(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app" / "analytics" / "scheduling.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        f"from {_BINDING_ACQUISITION_MODULE} import {_BINDING_ACQUISITION_ACTION}\n",
        encoding="utf-8",
    )

    with pytest.raises(
        AssertionError,
        match="A scheduler, worker, or background module imports the binding acquisition",
    ):
        _assert_no_background_module_imports_the_binding_acquisition_action(
            tmp_path / "app"
        )


def test_confirmation_is_minted_only_through_the_request_adapter() -> None:
    assert _mint_importers(BRAIN_ROOT) == [_CONFIRMATION_MINT_ADAPTER]


def test_reserve_entry_point_requires_a_confirmation_token() -> None:
    parameter = inspect.signature(reserve_admission).parameters["confirmation_token"]

    assert parameter.default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        reserve_admission(None, request=None)  # type: ignore[call-arg]


@pytest.mark.parametrize("module", _CARRIED_COST_MODULES)
def test_carried_cost_is_not_interpreted_in_a_single_expression(module: str) -> None:
    violations = _carried_cost_violations(BRAIN_ROOT / module)

    assert not violations, (
        f"{module} attaches local meaning to the carried cost in a single "
        "expression:\n" + "\n".join(violations)
    )


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            "    cost = subject.credit_cost\n    return cost + 1\n",
            id="one_local",
        ),
        pytest.param(
            "    cost = subject.credit_cost\n"
            "    carried = cost\n"
            "    return carried + 1\n",
            id="two_locals",
        ),
        pytest.param(
            '    row = {"cost": subject.credit_cost}\n    return row["cost"] + 1\n',
            id="container_local",
        ),
        pytest.param(
            "    def relay(value):\n        return value + 1\n\n"
            "    return relay(subject.credit_cost)\n",
            id="function_parameter",
        ),
    ],
)
def test_guard_does_not_see_interpretation_after_a_hop(
    tmp_path: Path, body: str
) -> None:
    """Records the guard's blind spots; ``CarriedCost`` is what refuses these."""

    path = tmp_path / "module.py"
    path.write_text(f"def carried(subject):\n{body}", encoding="utf-8")

    assert _carried_cost_violations(path) == []


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("subject.credit_cost + 1", ["line 2 computes with the carried cost"]),
        ("-subject.credit_cost", ["line 2 computes with the carried cost"]),
        ("subject.credit_cost > 0", ["line 2 orders the carried cost"]),
        ("not subject.credit_cost", []),
        ("subject.credit_cost == presented", []),
    ],
)
def test_guard_separates_interpretation_from_comparison(
    tmp_path: Path, expression: str, expected: list[str]
) -> None:
    path = tmp_path / "module.py"
    path.write_text(
        f"def carried(subject, presented):\n    return {expression}\n", encoding="utf-8"
    )

    assert _carried_cost_violations(path) == expected
