"""Executable architecture boundary tests and permanent negative controls using Import Linter."""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from dataclasses import fields
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES_ROOT = ROOT / "tests" / "architecture_invalid" / "python"
CANONICAL_READ_MODEL_OWNER = ROOT / "app" / "canonical" / "read_models.py"
CANONICAL_READ_MODEL_LEGACY_FIXTURES = (
    "legacy_transport_account_read_model.py.txt",
    "legacy_transport_from_import_account_read_model.py.txt",
    "legacy_transport_from_import_alias_account_read_model.py.txt",
    "legacy_transport_dotted_import_account_read_model.py.txt",
    "legacy_transport_dotted_import_alias_account_read_model.py.txt",
)

from app.persistence.factory import CanonicalRepositories, create_canonical_repositories


def _get_lint_imports_command() -> list[str]:
    """Resolve the lint-imports executable or fall back to python -c entrypoint."""
    binary = shutil.which("lint-imports")
    if binary:
        return [binary]
    entrypoint = "import sys; from importlinter.cli import lint_imports_command; sys.exit(lint_imports_command())"
    return [sys.executable, "-c", entrypoint]


def run_import_linter(
    config_path: Path,
    *,
    cwd: Path | None = None,
    contract: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute Import Linter in an isolated subprocess with deterministic configuration."""
    cmd = _get_lint_imports_command()
    cmd.extend(["--config", str(config_path.resolve())])
    if contract:
        cmd.extend(["--contract", contract])

    working_dir = cwd if cwd is not None else ROOT
    env = dict(os.environ)
    # Prioritize working directory on PYTHONPATH so fixture packages are discovered
    env["PYTHONPATH"] = str(working_dir.resolve())

    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(working_dir.resolve()),
        env=env,
    )


def _legacy_transport_account_read_model_imports(source: str) -> list[str]:
    """Return static uses of the canonical type through its retired transport path."""

    tree = ast.parse(source)
    legacy_uses = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "app.transport.ingestion"
        for alias in node.names
        if alias.name == "AccountReadModel"
    ]
    transport_ingestion_aliases = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "app.transport"
        for alias in node.names
        if alias.name == "ingestion"
    }
    transport_ingestion_aliases.update(
        alias.asname
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "app.transport.ingestion" and alias.asname is not None
    )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr != "AccountReadModel":
            continue
        if isinstance(node.value, ast.Name) and node.value.id in transport_ingestion_aliases:
            legacy_uses.append(node.value.id)
        elif _attribute_path(node) == "app.transport.ingestion.AccountReadModel":
            legacy_uses.append("app.transport.ingestion")
    return legacy_uses


def _attribute_path(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if not isinstance(node, ast.Attribute):
        return None
    prefix = _attribute_path(node.value)
    return None if prefix is None else f"{prefix}.{node.attr}"


def _assert_no_legacy_transport_account_read_model_import(path: Path, source: str) -> None:
    legacy_imports = _legacy_transport_account_read_model_imports(source)
    assert not legacy_imports, (
        f"{path.relative_to(ROOT)} imports AccountReadModel from "
        "app.transport.ingestion; use app.canonical.read_models instead"
    )


# ------------------------------------------------------------------------------
# Production Contracts (Enforced)
# ------------------------------------------------------------------------------


def test_canonical_read_model_ownership_contract() -> None:
    """AccountReadModel has one canonical owner and no legacy transport alias."""

    canonical_definitions = [
        path
        for path in (ROOT / "app").rglob("*.py")
        if any(
            isinstance(node, ast.ClassDef) and node.name == "AccountReadModel"
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    ]
    assert canonical_definitions == [CANONICAL_READ_MODEL_OWNER]

    for path in (ROOT / "app").rglob("*.py"):
        _assert_no_legacy_transport_account_read_model_import(
            path, path.read_text(encoding="utf-8")
        )

    ingestion_source = (ROOT / "app" / "transport" / "ingestion.py").read_text(encoding="utf-8")
    assert "AccountReadModel" not in {
        node.name
        for node in ast.parse(ingestion_source).body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }

    from app.transport import ingestion

    assert not hasattr(ingestion, "AccountReadModel")
    with pytest.raises(ImportError):
        exec("from app.transport.ingestion import AccountReadModel", {})


def test_canonical_read_model_ownership_allows_other_ingestion_symbols() -> None:
    """The ownership control remains limited to AccountReadModel."""

    _assert_no_legacy_transport_account_read_model_import(
        ROOT / "app" / "transport" / "ingestion.py",
        "from app.transport.ingestion import IngestionService\n",
    )


def test_canonical_persistence_no_upward_contract() -> None:
    """Contract A: Core canonical persistence modules must not import analytics, services, API, provisioning, or transport."""
    result = run_import_linter(
        ROOT / ".importlinter",
        cwd=ROOT,
        contract="contract-a-canonical-persistence-no-upward",
    )
    assert result.returncode == 0, f"Contract A violated in production:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    assert "Contract A: Canonical persistence core cannot depend upward or into transport KEPT" in result.stdout


def test_canonical_history_gateway_contract() -> None:
    """Contract B: Only approved gateway modules may import app.persistence.history."""
    result = run_import_linter(
        ROOT / ".importlinter",
        cwd=ROOT,
        contract="contract-b-canonical-history-gateway",
    )
    assert result.returncode == 0, f"Contract B violated in production:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    assert "Contract B: Only approved gateway modules may import app.persistence.history KEPT" in result.stdout


def test_service_transport_separation_contract() -> None:
    """Contract C: insights_service cannot discover transport infrastructure."""
    result = run_import_linter(
        ROOT / ".importlinter",
        cwd=ROOT,
        contract="contract-c-service-transport-separation",
    )
    assert result.returncode == 0, f"Contract C violated in production:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    assert "Contract C: Application services must not import transport KEPT" in result.stdout


def test_persistence_factory_analytics_separation_contract() -> None:
    """Contract D: persistence assembly cannot import analytics adapters."""

    result = run_import_linter(
        ROOT / ".importlinter",
        cwd=ROOT,
        contract="contract-d-persistence-factory-no-analytics",
    )
    assert result.returncode == 0, f"Contract D violated in production:\n{result.stdout}"
    assert "Contract D: Persistence factory must not import analytics KEPT" in result.stdout


def test_transport_manager_canonical_analytics_source_separation_contract() -> None:
    """Contract E: transport cannot construct the canonical analytics adapter."""

    result = run_import_linter(
        ROOT / ".importlinter",
        cwd=ROOT,
        contract="contract-e-transport-manager-no-canonical-analytics-source",
    )
    assert result.returncode == 0, f"Contract E violated in production:\n{result.stdout}"
    assert "Contract E: Transport manager must not import canonical analytics source KEPT" in result.stdout


def test_canonical_repositories_exposes_persistence_resources_only() -> None:
    """The removed analytics adapter cannot silently return through the factory."""

    assert "ingestion" not in {field.name for field in fields(CanonicalRepositories)}
    repositories = create_canonical_repositories("memory")
    assert not hasattr(repositories, "ingestion")


def test_production_import_linter_all_contracts() -> None:
    """Full production Import Linter run must pass with all contracts kept."""
    result = run_import_linter(ROOT / ".importlinter", cwd=ROOT)
    assert result.returncode == 0, f"Production lint-imports failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    assert "Contracts: 5 kept, 0 broken." in result.stdout


# ------------------------------------------------------------------------------
# Permanent Negative Controls (Isolated Non-Importable Fixtures)
# ------------------------------------------------------------------------------


@pytest.mark.parametrize("fixture_name", CANONICAL_READ_MODEL_LEGACY_FIXTURES)
def test_negative_control_legacy_transport_account_read_model_import(fixture_name: str) -> None:
    """Prove the ownership check rejects the retired transport import path."""

    invalid_fixture_path = (
        ROOT / "tests" / "fixtures" / "architecture_boundaries" / fixture_name
    )
    with pytest.raises(AssertionError, match="app.canonical.read_models"):
        _assert_no_legacy_transport_account_read_model_import(
            invalid_fixture_path,
            invalid_fixture_path.read_text(encoding="utf-8"),
        )


def test_negative_control_ordinary_analytics_imports_history() -> None:
    """Prove rejection of ordinary analytics directly importing app.persistence.history (Contract B)."""
    fixture_dir = FIXTURES_ROOT / "ordinary_analytics_imports_history"
    config_path = fixture_dir / ".importlinter"
    assert config_path.is_file(), f"Fixture config missing: {config_path}"

    result = run_import_linter(config_path, cwd=fixture_dir)
    assert result.returncode == 1, f"Expected rejection, but check passed:\n{result.stdout}"
    assert "Contract B: Only approved gateway modules may import app.persistence.history BROKEN" in result.stdout
    assert "app.analytics.metrics -> app.persistence.history" in result.stdout


def test_negative_control_protected_persistence_imports_analytics() -> None:
    """Prove rejection of a protected persistence module importing app.analytics (Contract A)."""
    fixture_dir = FIXTURES_ROOT / "protected_persistence_imports_analytics"
    config_path = fixture_dir / ".importlinter"
    assert config_path.is_file(), f"Fixture config missing: {config_path}"

    result = run_import_linter(config_path, cwd=fixture_dir)
    assert result.returncode == 1, f"Expected rejection, but check passed:\n{result.stdout}"
    assert "Contract A: Canonical persistence core cannot depend upward or into transport BROKEN" in result.stdout
    assert "app.persistence.history -> app.analytics" in result.stdout


def test_negative_control_protected_persistence_imports_transport() -> None:
    """Prove rejection of a protected persistence module importing app.transport (Contract A)."""
    fixture_dir = FIXTURES_ROOT / "protected_persistence_imports_transport"
    config_path = fixture_dir / ".importlinter"
    assert config_path.is_file(), f"Fixture config missing: {config_path}"

    result = run_import_linter(config_path, cwd=fixture_dir)
    assert result.returncode == 1, f"Expected rejection, but check passed:\n{result.stdout}"
    assert "Contract A: Canonical persistence core cannot depend upward or into transport BROKEN" in result.stdout
    assert "app.persistence.database -> app.transport" in result.stdout


def test_negative_control_insights_service_imports_transport() -> None:
    """Prove rejection of app.services.insights_service importing app.transport."""
    fixture_dir = FIXTURES_ROOT / "future_insights_service_imports_transport"
    config_path = fixture_dir / ".importlinter"
    assert config_path.is_file(), f"Fixture config missing: {config_path}"

    result = run_import_linter(config_path, cwd=fixture_dir)
    assert result.returncode == 1, f"Expected rejection, but check passed:\n{result.stdout}"
    assert "Contract C: Application services must not import transport BROKEN" in result.stdout
    assert "app.services.insights_service -> app.transport" in result.stdout


def test_negative_control_persistence_factory_imports_analytics() -> None:
    """Prove rejection of app.persistence.factory importing app.analytics under future post-7B Contract D."""
    fixture_dir = FIXTURES_ROOT / "future_persistence_factory_imports_analytics"
    config_path = fixture_dir / ".importlinter"
    assert config_path.is_file(), f"Fixture config missing: {config_path}"

    result = run_import_linter(config_path, cwd=fixture_dir)
    assert result.returncode == 1, f"Expected rejection, but check passed:\n{result.stdout}"
    assert "Contract D: Persistence factory must not import analytics BROKEN" in result.stdout
    assert "app.persistence.factory -> app.analytics" in result.stdout


def test_negative_control_transport_manager_imports_canonical_analytics_source() -> None:
    """Prove Contract E rejects canonical analytics construction in transport."""

    fixture_dir = FIXTURES_ROOT / "future_transport_manager_imports_canonical_analytics"
    config_path = fixture_dir / ".importlinter"
    result = run_import_linter(config_path, cwd=fixture_dir)
    assert result.returncode == 1, f"Expected rejection, but check passed:\n{result.stdout}"
    assert "Contract E: Transport manager must not import canonical analytics source BROKEN" in result.stdout
    assert "app.transport.manager -> app.analytics.canonical_source" in result.stdout
