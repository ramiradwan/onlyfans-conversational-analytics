"""The single source for required grant-type sets, and its layering.

Two enforcement points holding their own copy of a required grant-type set is
the defect these cases guard against. The membership of each set is checked
against values declared here, and the source tree is checked for any second
definition of such a set.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.security import grant_types


SOURCE_MODULE = Path(grant_types.__file__).resolve()
APPLICATION_ROOT = SOURCE_MODULE.parents[1]

# Declared here rather than imported, so that changing a set in the module
# under test disagrees with this file instead of restating its own expectation.
EXPECTED_GRANT_TYPE_NAMES = frozenset(
    {
        "installation_grant",
        "membership_snapshot",
        "creator_account_binding",
        "license_entitlement",
    }
)
EXPECTED_ACTIVATION = ("installation_grant", "membership_snapshot")
EXPECTED_ACCOUNT_AUTHORITY = (
    "installation_grant",
    "membership_snapshot",
    "creator_account_binding",
)
EXPECTED_PROVISIONING = (
    "creator_account_binding",
    "installation_grant",
    "license_entitlement",
    "membership_snapshot",
)
EXPECTED_AGENT_PAIRING = ("installation_grant", "creator_account_binding")
EXPECTED_HOSTED_CLAIM = (
    "installation_grant",
    "membership_snapshot",
    "license_entitlement",
)
# Modules holding a predicate that reads one of the sets. The scan is only
# evidence if it walks these.
COVERED_MODULES = (
    "app/persistence/auth.py",
    "app/provisioning/finalize.py",
    "app/security/account_bindings.py",
    "app/security/activation_gate.py",
    "app/security/hosted_grants.py",
    "app/security/webauthn.py",
)


def _grant_type_values() -> dict[str, str]:
    """Map every identifier in the source module bound to a grant-type name."""

    return {
        name: value
        for name, value in vars(grant_types).items()
        if isinstance(value, str) and value in EXPECTED_GRANT_TYPE_NAMES
    }


def _module_level_sequences(tree: ast.Module) -> list[tuple[int, str, ast.expr]]:
    """Yield `(line, name, value)` for each module-level sequence assignment."""

    found: list[tuple[int, str, ast.expr]] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets: list[ast.expr] = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        value = node.value
        if isinstance(value, ast.Call) and value.args:
            # `frozenset({...})` and `tuple([...])` wrap the sequence itself.
            value = value.args[0]
        if not isinstance(value, (ast.Tuple, ast.List, ast.Set)):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                found.append((node.lineno, target.id, value))
    return found


def _resolved_grant_types(value: ast.expr, names: dict[str, str]) -> set[str]:
    """Resolve sequence elements naming a grant type, literally or by binding."""

    resolved: set[str] = set()
    for element in getattr(value, "elts", []):
        if isinstance(element, ast.Constant) and element.value in EXPECTED_GRANT_TYPE_NAMES:
            resolved.add(str(element.value))
        elif isinstance(element, ast.Name) and element.id in names:
            resolved.add(names[element.id])
    return resolved


def _imported_grant_type_names(tree: ast.Module) -> dict[str, str]:
    """Map identifiers imported from the source module to their grant types."""

    exported = _grant_type_values()
    imported: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module != grant_types.__name__:
            continue
        for alias in node.names:
            value = exported.get(alias.name)
            if value is not None:
                imported[alias.asname or alias.name] = value
    return imported


def _scanned_modules(root: Path) -> list[Path]:
    return [path for path in sorted(root.rglob("*.py")) if path.resolve() != SOURCE_MODULE]


def _redefinitions(root: Path = APPLICATION_ROOT) -> list[str]:
    """Find every module-level grant-type set defined outside the source module."""

    offenders: list[str] = []
    for path in _scanned_modules(root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = _imported_grant_type_names(tree)
        for lineno, name, value in _module_level_sequences(tree):
            if len(_resolved_grant_types(value, names)) >= 2:
                offenders.append(f"{path.relative_to(root).as_posix()}:{lineno} {name}")
    return offenders


def test_the_scan_walks_the_modules_that_enforce_the_sets() -> None:
    """An empty scan result means nothing if the scan reached nothing."""

    scanned = {
        path.relative_to(APPLICATION_ROOT.parent).as_posix()
        for path in _scanned_modules(APPLICATION_ROOT)
    }

    assert APPLICATION_ROOT.name == "app"
    assert set(COVERED_MODULES) <= scanned


def test_no_module_outside_the_source_defines_a_grant_type_set() -> None:
    """A second copy of a required set is what let two of them disagree."""

    assert _redefinitions() == []


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            'COPY = ("installation_grant", "membership_snapshot")\n',
            ["probe.py:1 COPY"],
        ),
        (
            "from app.security.grant_types import (\n"
            "    INSTALLATION_GRANT,\n"
            "    MEMBERSHIP_SNAPSHOT,\n"
            ")\n"
            "COPY = frozenset({INSTALLATION_GRANT, MEMBERSHIP_SNAPSHOT})\n",
            ["probe.py:5 COPY"],
        ),
        ('SINGLE = ("installation_grant",)\n', []),
        ('AUDIENCES = {"installation_grant": "urn:example"}\n', []),
    ],
)
def test_the_scan_reports_a_reintroduced_set(
    tmp_path: Path, body: str, expected: list[str]
) -> None:
    """The scan's own detection, over both literal and imported spellings."""

    (tmp_path / "probe.py").write_text(body, encoding="utf-8")

    assert _redefinitions(tmp_path) == expected


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        (grant_types.ACTIVATION_GRANT_TYPES, EXPECTED_ACTIVATION),
        (grant_types.ACCOUNT_AUTHORITY_GRANT_TYPES, EXPECTED_ACCOUNT_AUTHORITY),
        (grant_types.PROVISIONING_GRANT_TYPES, EXPECTED_PROVISIONING),
        (grant_types.AGENT_PAIRING_GRANT_TYPES, EXPECTED_AGENT_PAIRING),
        (grant_types.HOSTED_CLAIM_GRANT_TYPES, EXPECTED_HOSTED_CLAIM),
    ],
)
def test_each_declared_set_holds_the_expected_types(
    declared: tuple[str, ...], expected: tuple[str, ...]
) -> None:
    assert declared == expected


def test_the_grant_type_names_are_the_four_signed_types() -> None:
    assert set(_grant_type_values().values()) == EXPECTED_GRANT_TYPE_NAMES


def test_activation_is_strictly_weaker_than_acting_for_an_account() -> None:
    """Activation reads no account, so it cannot reach the account layer."""

    assert set(EXPECTED_ACTIVATION) < set(EXPECTED_ACCOUNT_AUTHORITY)
    assert set(grant_types.ACTIVATION_GRANT_TYPES) < set(
        grant_types.ACCOUNT_AUTHORITY_GRANT_TYPES
    )
    assert "creator_account_binding" not in grant_types.ACTIVATION_GRANT_TYPES


def test_provisioning_covers_everything_acting_for_an_account_requires() -> None:
    """Finalization cannot record a tuple weaker than the acting set."""

    assert set(EXPECTED_ACCOUNT_AUTHORITY) < set(EXPECTED_PROVISIONING)
    assert set(grant_types.ACCOUNT_AUTHORITY_GRANT_TYPES) < set(
        grant_types.PROVISIONING_GRANT_TYPES
    )
