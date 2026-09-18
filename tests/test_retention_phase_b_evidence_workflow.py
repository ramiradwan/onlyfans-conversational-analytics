from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "retention-phase-b-evidence.yml"


def _document() -> dict[str, Any]:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def test_retention_evidence_starts_from_completed_ci_instead_of_polling() -> None:
    raw = WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.load(raw, Loader=yaml.BaseLoader)
    trigger = workflow["on"]["workflow_run"]
    assert trigger["workflows"] == ["CI"]
    assert trigger["types"] == ["completed"]
    assert "pull_request" not in workflow["on"]
    assert "time.sleep" not in raw
    assert "actions/runs?" not in raw


def test_retention_evidence_automates_only_successful_same_repo_pr_ci() -> None:
    condition = _document()["jobs"]["lifecycle-evidence"]["if"]
    assert "workflow_run.conclusion == 'success'" in condition
    assert "head_repository.full_name == github.repository" in condition
    assert "workflow_run.event == 'pull_request'" in condition
    assert "workflow_run.event == 'push'" not in condition
    assert "github.event_name == 'workflow_dispatch'" in condition


def test_retention_evidence_validates_upstream_before_checking_out_product_code() -> None:
    job = _document()["jobs"]["lifecycle-evidence"]
    steps = job["steps"]
    resolve = next(i for i, step in enumerate(steps) if step.get("name") == "Resolve exact successful upstream CI")
    checkout = next(i for i, step in enumerate(steps) if step.get("name") == "Checkout exact Product revision")
    assert resolve < checkout
    assert steps[checkout]["with"]["ref"] == "${{ env.PRODUCT_SHA }}"
    assert steps[checkout]["with"]["persist-credentials"] is False

    verification = steps[resolve]["run"]
    for required in (
        'run.get("name") == "CI"',
        'run.get("head_sha") == product_sha',
        'run.get("status") == "completed"',
        'run.get("conclusion") == "success"',
        'int(run.get("id") or 0) == run_id',
    ):
        assert required in verification


def test_retention_evidence_reuses_the_exact_upstream_frontend_artifact() -> None:
    job = _document()["jobs"]["lifecycle-evidence"]
    steps = job["steps"]
    download = next(step for step in steps if step.get("name") == "Retrieve exact-run frontend build")
    assert download["with"]["name"] == "frontend-dist-${{ env.PRODUCT_SHA }}"
    assert download["with"]["run-id"] == "${{ env.CI_RUN_ID }}"
    assert download["with"]["repository"] == "${{ github.repository }}"
    assert download["with"]["github-token"] == "${{ github.token }}"
    assert not any(step.get("name") == "Build frontend" for step in steps)


def test_retention_evidence_cancels_only_superseded_pr_evidence() -> None:
    workflow = _document()
    concurrency = workflow["concurrency"]
    assert "pull_requests[0].number" in concurrency["group"]
    assert concurrency["cancel-in-progress"] == (
        "${{ github.event_name == 'workflow_run' && github.event.workflow_run.event == 'pull_request' }}"
    )
    assert workflow["permissions"] == {"contents": "read", "actions": "read"}
