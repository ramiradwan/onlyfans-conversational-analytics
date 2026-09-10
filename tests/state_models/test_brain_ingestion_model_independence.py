"""Verify that the ingestion model is independent of production code."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path


MODEL_FILE = Path(__file__).resolve().parent / "brain_ingestion_model.py"

ALLOWED_STDLIB_MODULES = frozenset(
    {
        "__future__",
        "dataclasses",
        "datetime",
        "hashlib",
        "json",
        "typing",
        "uuid",
    }
)

BANNED_SYMBOLS = frozenset(
    {
        "HistoryRepository",
        "IngestionService",
        "CanonicalSQLite",
        "LocalSQLite",
        "IngestResult",
        "InvariantViolation",
        "_merge_chat",
        "_merge_message",
        "_delete_chat",
        "_delete_message",
        "_merge_staged_snapshot_entities",
        "create_canonical_repositories",
    }
)


def test_brain_ingestion_model_imports_only_allowed_stdlib() -> None:
    """AST check ensuring the reference model imports no application or third-party code."""
    assert MODEL_FILE.exists(), f"Reference model file missing: {MODEL_FILE}"

    tree = ast.parse(MODEL_FILE.read_text(encoding="utf-8"), filename=str(MODEL_FILE))

    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_modules.add(node.module.split(".")[0])

    disallowed = imported_modules - ALLOWED_STDLIB_MODULES
    assert not disallowed, (
        f"brain_ingestion_model.py imports prohibited non-stdlib or application modules: {disallowed}. "
        f"Allowed modules are strictly: {sorted(ALLOWED_STDLIB_MODULES)}"
    )


def test_brain_ingestion_model_contains_no_banned_production_code_references() -> None:
    """AST check ensuring the reference model code does not reference prohibited production symbols."""
    tree = ast.parse(MODEL_FILE.read_text(encoding="utf-8"), filename=str(MODEL_FILE))
    referenced_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            referenced_names.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced_names.add(node.attr)

    prohibited_found = referenced_names & BANNED_SYMBOLS
    assert not prohibited_found, (
        f"Reference model code references prohibited production symbols: {prohibited_found}"
    )


def test_brain_ingestion_model_runtime_loads_no_app_modules() -> None:
    """Subprocess check verifying that importing the model loads zero 'app.*' modules."""
    script = (
        "import sys\n"
        "from tests.state_models import brain_ingestion_model\n"
        "app_modules = [m for m in sys.modules if m.startswith('app.') or m == 'app']\n"
        "assert not app_modules, f'Importing model contaminated sys.modules with: {app_modules}'\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(MODEL_FILE.parents[2]),
    )
    assert result.returncode == 0, f"Dynamic independence check failed: {result.stderr}"
    assert "OK" in result.stdout
