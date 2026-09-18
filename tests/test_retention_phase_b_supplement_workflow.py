from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "retention-phase-b-supplement.yml"


def _document() -> dict[str, Any]:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for step in job.get("steps", []) if isinstance(step, dict)]


def test_supplement_is_manual_and_requires_an_exact_qualified_source() -> None:
    workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert inputs["product_sha"]["required"] == "true"
    assert inputs["ci_run_id"]["required"] == "true"

    document = _document()
    assert document["permissions"] == {"contents": "read", "actions": "read"}
    assert document["concurrency"]["cancel-in-progress"] is False
    assert document["env"]["PRODUCT_SHA"] == "${{ inputs.product_sha }}"
    assert document["env"]["CI_RUN_ID"] == "${{ inputs.ci_run_id }}"


def test_supplement_preserves_the_fixed_historical_coordinates() -> None:
    env = _document()["env"]
    assert env["PHASE_A_SHA"] == "f5526d326031fd1c16469bdb50e9d5c124e5d09d"
    assert env["ACCEPTED_PHASE_B_SHA"] == "64af971a7a8db469d35e0db0a6af5d31fdceba2c"
    assert env["ACCEPTED_PHASE_B_ARTIFACT"] == "retention-phase-b-64af971a7a8db469d35e0db0a6af5d31fdceba2c"
    assert env["ACCEPTED_PHASE_B_ARTIFACT_SHA256"] == "b44738da429561ba07686caadaea162b01c18d54062865c02ec2740cea8f061b"


def test_supplement_validates_ci_before_any_product_checkout() -> None:
    jobs = _document()["jobs"]
    qualifier = jobs["qualify-source"]
    verification = _steps(qualifier)[0]["run"]
    for required in (
        'run.get("name") == "CI"',
        'run.get("head_sha") == os.environ["PRODUCT_SHA"]',
        'run.get("status") == "completed"',
        'run.get("conclusion") == "success"',
        'int(run.get("id") or 0) == run_id',
    ):
        assert required in verification
    assert jobs["linux-evidence"]["needs"] == "qualify-source"
    assert jobs["windows-evidence"]["needs"] == "qualify-source"


def test_supplement_reuses_high_value_exact_run_ci_artifacts() -> None:
    jobs = _document()["jobs"]
    linux_steps = _steps(jobs["linux-evidence"])
    linux_frontend = next(step for step in linux_steps if step.get("name") == "Retrieve exact-run frontend build")
    assert linux_frontend["with"]["name"] == "frontend-dist-${{ env.PRODUCT_SHA }}"
    assert linux_frontend["with"]["run-id"] == "${{ env.CI_RUN_ID }}"
    assert not any(step.get("name") == "Build frontend" for step in linux_steps)

    windows_steps = _steps(jobs["windows-evidence"])
    names = {step.get("name") for step in windows_steps}
    assert "Build fixed SQLCipher wheel" not in names
    assert "Build installer assets" not in names
    for step_name, artifact_name in {
        "Retrieve exact-run fixed SQLCipher wheel": "fixed-sqlcipher-wheel-${{ env.PRODUCT_SHA }}",
        "Retrieve exact-run frontend build": "frontend-dist-${{ env.PRODUCT_SHA }}",
        "Retrieve exact-run extension build": "extension-dist-${{ env.PRODUCT_SHA }}",
    }.items():
        step = next(candidate for candidate in windows_steps if candidate.get("name") == step_name)
        assert step["with"]["name"] == artifact_name
        assert step["with"]["run-id"] == "${{ env.CI_RUN_ID }}"

    install = next(step for step in windows_steps if step.get("name") == "Install dependencies")
    assert "npm ci --prefix frontend" in install["run"]
    assert "npm ci --prefix extension" in install["run"]
    assert "--find-links" in install["run"]


def test_supplement_final_manifest_records_the_qualified_ci_coordinate() -> None:
    finalize = _document()["jobs"]["finalize"]
    assert set(finalize["needs"]) == {"qualify-source", "linux-evidence", "windows-evidence"}
    emit = next(step for step in _steps(finalize) if step.get("name") == "Emit exact-head supplement manifest")
    assert '"run_id": int(os.environ["CI_RUN_ID"])' in emit["run"]
    assert '"source_commit": product_sha' in emit["run"]
