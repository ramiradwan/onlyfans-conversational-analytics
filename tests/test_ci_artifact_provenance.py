from __future__ import annotations

from pathlib import Path
from copy import deepcopy
from typing import Any

import pytest
import yaml

pytestmark = [pytest.mark.ci_tier("fast")]


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
CORE_SOURCE_JOBS = (
    "web-build-and-test",
    "backend-fast",
    "analytics-integration",
    "fixed-sqlcipher-wheel",
    "windows-platform-contract",
    "analytics-windows-contract",
    "analytics-scale-qualification",
    "windows-full-shards",
    "browser-reporting-safety",
    "browser-e2e-execution",
    "browser-e2e-serial-control",
    "windows-browser-e2e",
    "required-ci-gate",
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
    steps = _steps(_workflow_document()["jobs"]["web-build-and-test"])
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

    windows_steps = _steps(workflow["jobs"]["windows-platform-contract"])
    verify = next(step for step in windows_steps if step.get("name") == "Verify fixed SQLCipher CI source")
    assert "source SHA mismatch" in verify["run"]
    assert "run ID mismatch" in verify["run"]
    persistence = next(step for step in windows_steps if step.get("name") == "Record Windows persistence CI source")
    assert "source_commit = $env:PRODUCT_SHA" in persistence["run"]
    assert "workflow_run_id" in persistence["run"]


def test_stable_consumer_artifacts_can_be_republished_on_an_exact_source_rerun() -> None:
    expected = {
        "frontend-dist-${{ env.PRODUCT_SHA }}",
        "extension-dist-${{ env.PRODUCT_SHA }}",
        "fixed-sqlcipher-wheel-${{ env.PRODUCT_SHA }}",
        "windows-persistence-evidence-${{ env.PRODUCT_SHA }}",
        "legal-activation-v2-${{ env.PRODUCT_SHA }}",
    }
    observed = set()
    for job in _workflow_document()["jobs"].values():
        for step in _steps(job):
            if not str(step.get("uses", "")).startswith("actions/upload-artifact@"):
                continue
            settings = step["with"]
            if settings["name"] in expected:
                assert settings["overwrite"] is True
                observed.add(settings["name"])
            else:
                assert settings["name"].startswith(("ci-tests-", "browser-e2e-inputs-", "browser-e2e-diagnostics-", "browser-e2e-verified-"))
                assert not settings.get("overwrite"), "retained attempt evidence cannot be replaced"
    assert observed == expected


def _assert_browser_publication_is_validated(workflow: dict[str, Any]) -> None:
    jobs = workflow["jobs"]
    producer = jobs["browser-e2e-execution"]
    steps = _steps(producer)
    execute = next(step for step in steps if step.get("id") == "browser-execution")
    seal = next(step for step in steps if step.get("id") == "browser-seal")
    complete = next(step for step in steps if step.get("name") == "Retain immutable browser producer evidence")
    diagnostic = next(step for step in steps if step.get("name") == "Retain restricted browser diagnostics")
    assert steps.index(execute) < steps.index(seal) < steps.index(complete)
    assert seal["if"] == "always()"
    assert seal["run"] == (
        'python tools/ci_browser_gate.py seal --artifact-dir "$env:BROWSER_CI_REPORT_DIR" '
        '--publish-dir "$env:BROWSER_CI_REPORT_DIR-published" '
        '--diagnostics-dir "$env:BROWSER_CI_REPORT_DIR-diagnostics"'
    )
    assert complete["if"] == "${{ success() && steps.browser-seal.outcome == 'success' }}"
    assert complete["with"]["path"] == "${{ env.BROWSER_CI_REPORT_DIR }}-published/"
    assert diagnostic["if"] == "always()"
    assert diagnostic["with"]["path"] == "${{ env.BROWSER_CI_REPORT_DIR }}-diagnostics/"
    for step, prefix in ((complete, "browser-e2e-inputs"), (diagnostic, "browser-e2e-diagnostics")):
        assert step["with"]["name"] == f"{prefix}-${{{{ matrix.lane }}}}-${{{{ env.PRODUCT_SHA }}}}-${{{{ github.run_id }}}}-${{{{ github.run_attempt }}}}"
        assert not step["with"].get("overwrite"), "producer evidence must remain immutable"
        assert step["with"]["retention-days"] == 14
    aggregate = jobs["windows-browser-e2e"]
    assert aggregate["permissions"] == {"contents": "read", "actions": "read"}
    steps = _steps(aggregate)
    retrieve = next(step for step in steps if step.get("name") == "Retrieve immutable browser producer evidence")
    assert retrieve["with"]["pattern"] == "browser-e2e-inputs-*"
    assert retrieve["with"]["merge-multiple"] is False
    validate = next(step for step in steps if step.get("name") == "Validate browser execution and assemble Legal inputs")
    assert validate["run"] == (
        "python tools/ci_browser_gate.py --reports-dir artifacts/browser-evidence "
        "--execution-root artifacts/legal/execution --runtime-proof artifacts/legal/runtime-ui-proof.json "
        "--verified-output artifacts/browser-evidence/assembly-receipt.json"
    )
    assert validate["env"] == {"CI_NEEDS_JSON": "${{ toJSON(needs) }}", "GH_TOKEN": "${{ github.token }}"}
    generate = next(step for step in steps if step.get("name") == "Generate Product #5 Legal evidence bundle")
    publish = next(step for step in steps if step.get("name") == "Upload Product #5 Legal evidence bundle")
    assert steps.index(retrieve) < steps.index(validate) < steps.index(generate) < steps.index(publish)
    for step in (validate, generate, publish):
        assert "if" not in step and not step.get("continue-on-error"), "Legal publication requires successful validation"
    assert publish["with"]["name"] == "legal-activation-v2-${{ env.PRODUCT_SHA }}"
    assert publish["with"]["overwrite"] is True


@pytest.mark.parametrize("mutation", ["raw-diagnostics", "unsealed-inputs", "failed-inputs", "overwrite-producer", "merged-attempts", "skip-validator", "publish-after-failure"])
def test_browser_artifacts_cannot_publish_unvalidated_or_failed_raw_evidence(mutation: str) -> None:
    workflow = _workflow_document()
    _assert_browser_publication_is_validated(workflow)
    broken = deepcopy(workflow)
    producer = _steps(broken["jobs"]["browser-e2e-execution"])
    aggregate = _steps(broken["jobs"]["windows-browser-e2e"])
    if mutation == "raw-diagnostics":
        next(step for step in producer if step.get("name") == "Retain restricted browser diagnostics")["with"]["path"] = "${{ env.BROWSER_CI_REPORT_DIR }}/"
    elif mutation == "unsealed-inputs":
        next(step for step in producer if step.get("name") == "Retain immutable browser producer evidence")["with"]["path"] = "${{ env.BROWSER_CI_REPORT_DIR }}/"
    elif mutation == "failed-inputs":
        next(step for step in producer if step.get("name") == "Retain immutable browser producer evidence")["if"] = "always()"
    elif mutation == "overwrite-producer":
        next(step for step in producer if step.get("name") == "Retain immutable browser producer evidence")["with"]["overwrite"] = True
    elif mutation == "merged-attempts":
        next(step for step in aggregate if step.get("name") == "Retrieve immutable browser producer evidence")["with"]["merge-multiple"] = True
    elif mutation == "skip-validator":
        next(step for step in aggregate if step.get("name") == "Validate browser execution and assemble Legal inputs")["if"] = "false"
    else:
        next(step for step in aggregate if step.get("name") == "Upload Product #5 Legal evidence bundle")["if"] = "always()"
    with pytest.raises(AssertionError):
        _assert_browser_publication_is_validated(broken)
