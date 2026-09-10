"""Executable architecture boundary tests and permanent negative controls using Import Linter."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES_ROOT = ROOT / "tests" / "architecture_invalid" / "python"


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


# ------------------------------------------------------------------------------
# Production Contracts (Enforced)
# ------------------------------------------------------------------------------

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


def test_production_import_linter_all_contracts() -> None:
    """Full production Import Linter run must pass with all contracts kept."""
    result = run_import_linter(ROOT / ".importlinter", cwd=ROOT)
    assert result.returncode == 0, f"Production lint-imports failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    assert "Contracts: 3 kept, 0 broken." in result.stdout


# ------------------------------------------------------------------------------
# Permanent Negative Controls (Isolated Non-Importable Fixtures)
# ------------------------------------------------------------------------------

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
