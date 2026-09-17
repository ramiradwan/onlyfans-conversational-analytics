from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
CORE_SOURCE_JOBS = (
    "build-and-test",
    "fixed-sqlcipher-wheel",
    "windows-tests",
    "windows-browser-e2e",
)


def _workflow_document() -> dict[str, Any]:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{WORKFLOW} is not a mapping document"
    return document


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for step in job.get("steps", []) if isinstance(step, dict)]


def test_core_ci_uses_one_canonical_product_source_coordinate() -> None:
    workflow = _workflow_document()
    assert workflow["env"]["PRODUCT_SHA"] == "${{ github.event.pull_request.head.sha || github.sha }}"
    for name in CORE_SOURCE_JOBS:
        checkout = next(
            step for step in _steps(workflow["jobs"][name])
            if step.get("name") == "Checkout exact Product revision"
        )
        assert checkout["with"]["ref"] == "${{ env.PRODUCT_SHA }}"


def test_transient_build_artifacts_are_source_bound_and_short_lived() -> None:
    steps = _steps(_workflow_document()["jobs"]["build-and-test"])
    expected = {
        "Upload frontend build": "frontend-dist-${{ env.PRODUCT_SHA }}",
        "Upload audited extension artifact": "extension-dist-${{ env.PRODUCT_SHA }}",
    }
    for step_name, artifact_name in expected.items():
        step = next(candidate for candidate in steps if candidate.get("name") == step_name)
        assert step["with"]["name"] == artifact_name
        assert step["with"]["retention-days"] == 7
        assert step["with"]["if-no-files-found"] == "error"


def test_sqlcipher_and_persistence_artifacts_record_source_and_run_identity() -> None:
    workflow = _workflow_document()
    sql_steps = _steps(workflow["jobs"]["fixed-sqlcipher-wheel"])
    source = next(step for step in sql_steps if step.get("name") == "Record fixed SQLCipher CI source")
    assert "source_commit = $env:PRODUCT_SHA" in source["run"]
    assert "workflow_run_id" in source["run"]
    sql_upload = next(step for step in sql_steps if step.get("name") == "Retain fixed SQLCipher wheel and provenance")
    assert sql_upload["with"]["name"] == "fixed-sqlcipher-wheel-${{ env.PRODUCT_SHA }}"

    windows_steps = _steps(workflow["jobs"]["windows-tests"])
    verify = next(step for step in windows_steps if step.get("name") == "Verify fixed SQLCipher CI source")
    assert "source SHA mismatch" in verify["run"]
    assert "run ID mismatch" in verify["run"]
    persistence = next(step for step in windows_steps if step.get("name") == "Record Windows persistence CI source")
    assert "source_commit = $env:PRODUCT_SHA" in persistence["run"]
    assert "workflow_run_id" in persistence["run"]
