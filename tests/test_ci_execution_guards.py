from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
CORE_JOBS = {
    "build-and-test",
    "fixed-sqlcipher-wheel",
    "windows-tests",
    "windows-browser-e2e",
}


def _workflow_document() -> dict[str, Any]:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{WORKFLOW} is not a mapping document"
    return document


def test_ci_uses_pr_scoped_cancellation_but_preserves_main_runs() -> None:
    workflow = _workflow_document()
    assert workflow.get("permissions") == {"contents": "read"}
    concurrency = workflow["concurrency"]
    assert concurrency["group"] == "ci-${{ github.event.pull_request.number || github.ref }}"
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"


def test_release_qualified_ci_keeps_the_exact_four_job_topology() -> None:
    assert set(_workflow_document()["jobs"]) == CORE_JOBS


def test_core_jobs_have_explicit_timeouts() -> None:
    jobs = _workflow_document()["jobs"]
    for name in CORE_JOBS:
        timeout = jobs[name].get("timeout-minutes")
        assert isinstance(timeout, int) and 0 < timeout <= 60, f"{name} needs a bounded timeout"


def test_windows_consumers_fail_closed_on_the_shared_producer() -> None:
    jobs = _workflow_document()["jobs"]
    assert jobs["windows-tests"]["needs"] == "fixed-sqlcipher-wheel"
    assert jobs["windows-browser-e2e"]["needs"] == "fixed-sqlcipher-wheel"
