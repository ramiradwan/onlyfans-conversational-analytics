"""Behavioural falsifiers for CI's coverage of Windows-only backend tests.

`pytest.ini` carries no OS selection: tests guarded with
`@pytest.mark.skipif(os.name != "nt", ...)` only execute where a job actually
runs on a Windows runner. `windows-package.yml` triggers on `v*` tags alone, so
it never runs on a push or pull request; `ci.yml` is the only workflow that
does, and it must itself contain a job that runs pytest on a Windows runner.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

BACKEND_TEST_COMMAND = "python -m pytest"
DEPENDENCY_INSTALL_COMMAND = "python -m pip install -r requirements-dev.txt"
FRONTEND_BUILD_COMMAND = "npm run build --prefix frontend"


def _workflow_document() -> dict[str, Any]:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{WORKFLOW} is not a mapping document"
    return document


def _jobs(workflow: dict[str, Any]) -> dict[str, Any]:
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict) and jobs, "the workflow declares no jobs"
    return jobs


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job.get("steps") or []
    assert isinstance(steps, list)
    return [step for step in steps if isinstance(step, dict)]


def _runs_on_windows(job: dict[str, Any]) -> bool:
    runner = job.get("runs-on")
    return isinstance(runner, str) and runner.lower().startswith("windows")


def _run_step_indexes(job: dict[str, Any], command: str) -> list[int]:
    return [
        index
        for index, step in enumerate(_steps(job))
        if isinstance(step.get("run"), str) and step["run"].strip() == command
    ]


def _assert_a_windows_runner_executes_the_backend_tests(
    workflow: dict[str, Any],
) -> str:
    """The job is identified by what it runs, not by its name.

    Returns the job name so callers can locate it without re-deriving it.
    """

    names = sorted(
        name
        for name, job in _jobs(workflow).items()
        if _runs_on_windows(job) and _run_step_indexes(job, BACKEND_TEST_COMMAND)
    )
    assert len(names) == 1, (
        f"exactly one ci.yml job must run `{BACKEND_TEST_COMMAND}` on a Windows "
        f"runner, so tests guarded with skipif(os.name != \"nt\") execute "
        f"somewhere; found {names}"
    )
    return names[0]


def _assert_the_windows_job_installs_dependencies_before_testing(
    workflow: dict[str, Any],
) -> None:
    job_name = _assert_a_windows_runner_executes_the_backend_tests(workflow)
    job = _jobs(workflow)[job_name]

    install = _run_step_indexes(job, DEPENDENCY_INSTALL_COMMAND)
    test = _run_step_indexes(job, BACKEND_TEST_COMMAND)
    assert install, f"the Windows job never runs `{DEPENDENCY_INSTALL_COMMAND}`"
    assert install[0] < test[0], (
        "the Windows job must install backend dependencies before running pytest"
    )


def _jobs_testing_backend(workflow: dict[str, Any]) -> list[str]:
    return sorted(
        name
        for name, job in _jobs(workflow).items()
        if _run_step_indexes(job, BACKEND_TEST_COMMAND)
    )


def _assert_the_frontend_is_built_before_the_backend_tests_run(
    workflow: dict[str, Any],
) -> None:
    names = _jobs_testing_backend(workflow)
    assert names, f"no ci.yml job runs `{BACKEND_TEST_COMMAND}`"
    for name in names:
        job = _jobs(workflow)[name]
        build = _run_step_indexes(job, FRONTEND_BUILD_COMMAND)
        test = _run_step_indexes(job, BACKEND_TEST_COMMAND)
        assert build, (
            f"job `{name}` runs `{BACKEND_TEST_COMMAND}` but never builds the "
            f"frontend (`{FRONTEND_BUILD_COMMAND}`)"
        )
        assert build[0] < test[0], (
            f"job `{name}` must build the frontend before pytest runs"
        )


def test_a_windows_runner_executes_the_backend_tests_on_every_push() -> None:
    """Retargeting the Windows job at ubuntu-latest turns the named check red.

    That is the failure mode this check exists to catch: the backend suite
    still runs, still passes, and every `skipif(os.name != "nt")` test quietly
    stops executing anywhere in CI.
    """

    workflow = _workflow_document()
    windows_job = _assert_a_windows_runner_executes_the_backend_tests(workflow)

    retargeted = deepcopy(workflow)
    _jobs(retargeted)[windows_job]["runs-on"] = "ubuntu-latest"
    with pytest.raises(AssertionError, match="exactly one ci.yml job must run"):
        _assert_a_windows_runner_executes_the_backend_tests(retargeted)


def test_the_windows_job_installs_backend_dependencies_before_testing() -> None:
    """Moving pytest ahead of the dependency install turns the named check red."""

    workflow = _workflow_document()
    _assert_the_windows_job_installs_dependencies_before_testing(workflow)

    reordered = deepcopy(workflow)
    windows_job = _assert_a_windows_runner_executes_the_backend_tests(reordered)
    steps = _jobs(reordered)[windows_job]["steps"]
    test_index = _run_step_indexes(_jobs(reordered)[windows_job], BACKEND_TEST_COMMAND)[0]
    steps.insert(0, steps.pop(test_index))
    with pytest.raises(
        AssertionError, match="must install backend dependencies before running pytest"
    ):
        _assert_the_windows_job_installs_dependencies_before_testing(reordered)


def test_the_frontend_is_built_before_the_backend_tests_run() -> None:
    """Moving pytest ahead of the frontend build turns the named check red.

    app/api/endpoints/frontend.py resolves app/static/dist at module import
    time, and that directory is a frontend build output rather than a
    tracked file. On a fresh checkout it does not exist until the frontend
    is built, so importing app.main - and therefore collecting the backend
    test suite - fails without this ordering.
    """

    workflow = _workflow_document()
    _assert_the_frontend_is_built_before_the_backend_tests_run(workflow)

    reordered = deepcopy(workflow)
    job_name = _jobs_testing_backend(reordered)[0]
    steps = _jobs(reordered)[job_name]["steps"]
    test_index = _run_step_indexes(_jobs(reordered)[job_name], BACKEND_TEST_COMMAND)[0]
    steps.insert(0, steps.pop(test_index))
    with pytest.raises(
        AssertionError, match="must build the frontend before pytest runs"
    ):
        _assert_the_frontend_is_built_before_the_backend_tests_run(reordered)


def test_every_pytest_job_builds_the_frontend_not_just_the_intersection() -> None:
    """A pytest job that never builds the frontend at all turns the check red.

    An intersection-based check (jobs that both build the frontend AND run
    pytest) cannot see this defect: a job that runs pytest without ever
    building the frontend is simply absent from that intersection, so the
    check would pass while the job fails on a fresh checkout. The check must
    instead require the ordering from every job that runs pytest.
    """

    workflow = _workflow_document()
    pytest_jobs = _jobs_testing_backend(workflow)
    assert len(pytest_jobs) > 1, "need at least two pytest jobs to isolate one"

    stripped = deepcopy(workflow)
    job_name = _assert_a_windows_runner_executes_the_backend_tests(stripped)
    job = _jobs(stripped)[job_name]
    build_indexes = set(_run_step_indexes(job, FRONTEND_BUILD_COMMAND))
    assert build_indexes, f"job `{job_name}` is expected to build the frontend"
    job["steps"] = [
        step for index, step in enumerate(job["steps"]) if index not in build_indexes
    ]

    with pytest.raises(AssertionError, match="never builds the frontend"):
        _assert_the_frontend_is_built_before_the_backend_tests_run(stripped)
