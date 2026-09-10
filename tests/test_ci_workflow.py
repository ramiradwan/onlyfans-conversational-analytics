"""Behavioural falsifiers for CI's coverage of Windows-only backend tests.

`pytest.ini` carries no OS selection: tests guarded with
`@pytest.mark.skipif(os.name != "nt", ...)` only execute where a job actually
runs on a Windows runner. `windows-package.yml` runs only on an explicit
dispatch, so it never runs on a push or pull request; `ci.yml` is the only
workflow that does, and it must itself contain a job that runs pytest on a
Windows runner.

Jobs are matched by what a step collects rather than by an exact command
string, because marker selection still collects every test path and must not
make a job invisible to these checks.
"""

from __future__ import annotations

import shlex
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
PYTEST_INI = ROOT / "pytest.ini"

BACKEND_TEST_COMMAND = "python -m pytest"
DEPENDENCY_INSTALL_COMMAND = "python -m pip install -r requirements-dev.txt"
FRONTEND_BUILD_COMMAND = "npm run build --prefix frontend"
WINDOWS_PRODUCTION_MARKER = "windows_production"
FIXED_SQLCIPHER_BUILDER = "packaging\\sqlcipher\\build-fixed-wheel.ps1"
STATEFUL_TIER_A_MARKER = "stateful_tier_a"
STATEFUL_TIER_B_MARKER = "stateful_tier_b"
STATEFUL_AGENT_TIER_A_MARKER = "stateful_agent_tier_a"
TIER_A_TARGETS = {
    "tier_a_general": "tests/stateful/test_brain_ingestion.py::TestBrainIngestionGeneral",
    "tier_a_deletion": "tests/stateful/test_brain_ingestion.py::TestBrainIngestionDeletion",
}
ANALYTICS_DETERMINISM_TARGET = "tests/stateful/test_analytics_determinism.py::TestAnalyticsDeterminism"
ANALYTICS_DETERMINISM_PROFILE = "analytics_determinism_fast"
TIER_B_TARGETS = {
    "tier_b_general": "tests/stateful/test_brain_persistent_ingestion.py::TestPersistentGeneral",
    "tier_b_deletion": "tests/stateful/test_brain_persistent_ingestion.py::TestPersistentDeletion",
    "windows_persistence_smoke": "tests/stateful/test_brain_persistent_ingestion.py::TestWindowsProductionPersistenceSmoke",
}
AGENT_TIER_A_TARGETS = {
    "agent_tier_a_general": "tests/stateful/test_agent_delivery.py::TestAgentDeliveryGeneral",
    "agent_tier_a_deletion": "tests/stateful/test_agent_delivery.py::TestAgentDeliveryDeletion",
}
ANALYTICS_CONVERGENCE_TARGETS = {
    "analytics_convergence_fast": (
        "tests/stateful/test_analytics_equivalence.py::TestAnalyticsConvergence"
    ),
    "analytics_deletion_fast": (
        "tests/stateful/test_analytics_equivalence.py::TestAnalyticsDeletionConvergence"
    ),
}
ANALYTICS_CONVERGENCE_FALSIFIER_TARGETS = (
    "tests/stateful/test_analytics_equivalence.py::test_analytics_convergence_falsifiers_reject_metric_provenance_identity_graph_and_deletion_faults",
    "tests/stateful/test_analytics_equivalence.py::test_shared_oracle_rejects_same_forged_topic_or_entity_graph_in_both_artifacts",
    "tests/stateful/test_analytics_equivalence.py::test_shared_oracle_rejects_same_stale_deleted_message_metric_in_both_artifacts",
    "tests/stateful/test_analytics_equivalence.py::test_active_publication_oracle_rejects_stale_deleted_material_with_current_witness",
    "tests/stateful/test_analytics_equivalence.py::test_deliberate_falsifier_failure_configures_hypothesis_shrink_phase",
)

BROWSER_SUITE_DIRECTORY = "tools/e2e-capture"
BROWSER_SUITE_INVOCATIONS = ("npm test", "npm run test", "playwright test")
WINDOWS_ONLY_SPEC_GUARD = 'process.platform !== \'win32\''


def _workflow_document() -> dict[str, Any]:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{WORKFLOW} is not a mapping document"
    return document


def test_pull_request_body_edits_rerun_architecture_declaration_gate() -> None:
    # BaseLoader preserves YAML's "on" key instead of coercing it to True.
    workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    events = workflow["on"]["pull_request"]
    assert "edited" in events.get("types", []), (
        "correcting a PR architecture declaration must schedule a fresh event payload"
    )
    assert {"opened", "synchronize", "reopened"} <= set(events["types"])


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


def _backend_dependency_install_indexes(job: dict[str, Any]) -> list[int]:
    """Find the dependency install even when Windows supplies a wheelhouse."""

    return [
        index
        for index, step in enumerate(_steps(job))
        if isinstance(step.get("run"), str)
        and "python -m pip install" in step["run"]
        and "requirements-dev.txt" in step["run"]
    ]


def _fixed_sqlcipher_build_indexes(job: dict[str, Any]) -> list[int]:
    return [
        index
        for index, step in enumerate(_steps(job))
        if isinstance(step.get("run"), str) and FIXED_SQLCIPHER_BUILDER in step["run"]
    ]


def _collects_the_whole_suite(command: str) -> bool:
    """Whether a command collects every path in ``testpaths``.

    Marker selection narrows what runs but not what is collected, so those
    steps still import the whole tree and still need the frontend build. A
    step naming explicit test paths collects a subset and does not.
    """

    tokens = shlex.split(command)
    if tokens[:3] != ["python", "-m", "pytest"]:
        return False
    remaining = tokens[3:]
    index = 0
    while index < len(remaining):
        if remaining[index] == "-m":
            index += 2
            continue
        if remaining[index].startswith("-"):
            index += 1
            continue
        return False
    return True


def _backend_suite_indexes(job: dict[str, Any]) -> list[int]:
    return [
        index
        for index, step in enumerate(_steps(job))
        if isinstance(step.get("run"), str)
        and _collects_the_whole_suite(step["run"].strip())
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
        if _runs_on_windows(job) and _backend_suite_indexes(job)
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

    install = _backend_dependency_install_indexes(job)
    test = _backend_suite_indexes(job)
    assert install, f"the Windows job never installs `{DEPENDENCY_INSTALL_COMMAND}`"
    assert install[0] < test[0], (
        "the Windows job must install backend dependencies before running pytest"
    )


def _jobs_testing_backend(workflow: dict[str, Any]) -> list[str]:
    return sorted(
        name
        for name, job in _jobs(workflow).items()
        if _backend_suite_indexes(job)
    )


def _assert_the_frontend_is_built_before_the_backend_tests_run(
    workflow: dict[str, Any],
) -> None:
    names = _jobs_testing_backend(workflow)
    assert names, f"no ci.yml job runs `{BACKEND_TEST_COMMAND}`"
    for name in names:
        job = _jobs(workflow)[name]
        build = _run_step_indexes(job, FRONTEND_BUILD_COMMAND)
        test = _backend_suite_indexes(job)
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
    test_index = _backend_suite_indexes(_jobs(reordered)[windows_job])[0]
    steps.insert(0, steps.pop(test_index))
    with pytest.raises(
        AssertionError, match="must install backend dependencies before running pytest"
    ):
        _assert_the_windows_job_installs_dependencies_before_testing(reordered)


def test_windows_ci_builds_the_fixed_wheel_before_resolving_requirements() -> None:
    """Removing the local wheel build makes the Windows requirement unsatisfiable."""

    workflow = _workflow_document()
    windows_job = _assert_a_windows_runner_executes_the_backend_tests(workflow)
    job = _jobs(workflow)[windows_job]
    install = _backend_dependency_install_indexes(job)
    wheel_job = _jobs(workflow).get("fixed-sqlcipher-wheel")
    assert isinstance(wheel_job, dict), "CI must declare the fixed SQLCipher wheel job"
    wheel = _fixed_sqlcipher_build_indexes(wheel_job)
    assert len(wheel) == 1, "CI must construct exactly one fixed SQLCipher wheel"
    assert _jobs(workflow)[windows_job].get("needs") == "fixed-sqlcipher-wheel"
    assert "Retrieve fixed SQLCipher wheel" in str(_steps(job))
    assert "--find-links" in str(_steps(job)[install[0]]["run"])

    removed = deepcopy(workflow)
    _jobs(removed).pop("fixed-sqlcipher-wheel")
    with pytest.raises(AssertionError, match="construct exactly one fixed SQLCipher wheel"):
        removed_job = _jobs(removed).get("fixed-sqlcipher-wheel")
        assert isinstance(removed_job, dict) and len(_fixed_sqlcipher_build_indexes(removed_job)) == 1, "CI must construct exactly one fixed SQLCipher wheel"


def _assert_fixed_sqlcipher_native_cache(workflow: dict[str, Any]) -> None:
    job = _jobs(workflow)["fixed-sqlcipher-wheel"]
    steps = _steps(job)
    build = steps[_fixed_sqlcipher_build_indexes(job)[0]]
    prepare = next(step for step in steps if step.get("id") == "native-cache")
    cache = next(step for step in steps if step.get("id") == "vcpkg-cache")
    assert "if" not in job and "if" not in build, "every run must build a fresh wheel"
    assert steps.index(prepare) < steps.index(cache) < steps.index(build)
    action, _, revision = cache["uses"].partition("@")
    assert action == "actions/cache" and len(revision) == 40
    assert all(character in "0123456789abcdef" for character in revision)
    cache_inputs = cache["with"]
    assert "restore-keys" not in cache_inputs, "native cache must use exact inputs"
    expected_path = r"${{ runner.temp }}\ofca-vcpkg-cache"
    assert cache_inputs["path"] == expected_path, "cache only native dependency archives"
    assert build.get("env", {}).get("VCPKG_BINARY_SOURCES") == (
        f"clear;files,{expected_path},readwrite"
    ), "vcpkg must use only the dedicated cached directory"
    for scope in (workflow, job):
        assert "VCPKG_BINARY_SOURCES" not in scope.get("env", {}), "scope the cache to the wheel build"
    for required in (
        "${{ runner.os }}",
        "${{ runner.arch }}",
        "${{ steps.native-cache.outputs.image-version }}",
    ):
        assert required in cache_inputs["key"], f"cache key must include {required}"
    hashed_inputs = cache_inputs["key"].partition("hashFiles(")[2].partition(")")[0]
    for required in (
        "'packaging/sqlcipher/fixed-runtime-sources.json'",
        "'packaging/sqlcipher/build-fixed-wheel.py'",
        "'packaging/sqlcipher/build-fixed-wheel.ps1'",
    ):
        assert required in hashed_inputs, f"cache key must include {required} in hashFiles"


@pytest.mark.parametrize(
    "omitted",
    [
        "${{ runner.os }}",
        "${{ runner.arch }}",
        "${{ steps.native-cache.outputs.image-version }}",
        "'packaging/sqlcipher/fixed-runtime-sources.json'",
        "'packaging/sqlcipher/build-fixed-wheel.py'",
        "'packaging/sqlcipher/build-fixed-wheel.ps1'",
    ],
)
def test_native_dependency_cache_invalidates_all_build_inputs(omitted: str) -> None:
    workflow = _workflow_document()
    _assert_fixed_sqlcipher_native_cache(workflow)
    changed = deepcopy(workflow)
    cache = next(
        step for step in _steps(_jobs(changed)["fixed-sqlcipher-wheel"])
        if step.get("id") == "vcpkg-cache"
    )
    cache["with"]["key"] = cache["with"]["key"].replace(omitted, "", 1)
    with pytest.raises(AssertionError, match="cache key must include"):
        _assert_fixed_sqlcipher_native_cache(changed)


@pytest.mark.parametrize("mutation", ["skip-wheel", "different-path", "fallback-source", "restore-prefix"])
def test_native_dependency_cache_cannot_replace_or_redirect_the_wheel_build(mutation: str) -> None:
    workflow = _workflow_document()
    _assert_fixed_sqlcipher_native_cache(workflow)
    changed = deepcopy(workflow)
    job = _jobs(changed)["fixed-sqlcipher-wheel"]
    steps = _steps(job)
    build = steps[_fixed_sqlcipher_build_indexes(job)[0]]
    cache = next(step for step in steps if step.get("id") == "vcpkg-cache")
    if mutation == "skip-wheel":
        build["if"] = "steps.vcpkg-cache.outputs.cache-hit != 'true'"
        expected = "every run must build a fresh wheel"
    elif mutation == "different-path":
        cache["with"]["path"] = r"${{ runner.temp }}\ofca-fixed-sqlcipher-wheelhouse"
        expected = "cache only native dependency archives"
    elif mutation == "fallback-source":
        build["env"]["VCPKG_BINARY_SOURCES"] = build["env"]["VCPKG_BINARY_SOURCES"].removeprefix("clear;")
        expected = "vcpkg must use only the dedicated cached directory"
    else:
        cache["with"]["restore-keys"] = "ofca-vcpkg-v1-"
        expected = "native cache must use exact inputs"
    with pytest.raises(AssertionError, match=expected):
        _assert_fixed_sqlcipher_native_cache(changed)


def _tier_b_steps(workflow: dict[str, Any]) -> list[tuple[str, int, dict[str, Any]]]:
    return [
        (job_name, index, step)
        for job_name, job in _jobs(workflow).items()
        for index, step in enumerate(_steps(job))
        if isinstance(step.get("run"), str)
        and any(target in step["run"] for target in TIER_B_TARGETS.values())
    ]


def _assert_windows_tier_b_qualification(workflow: dict[str, Any]) -> None:
    windows_job = _assert_a_windows_runner_executes_the_backend_tests(workflow)
    job = _jobs(workflow)[windows_job]
    steps = _steps(job)
    install = _backend_dependency_install_indexes(job)
    runtime_indexes = [
        index
        for index, step in enumerate(steps)
        if "tools/qualify_sqlcipher_runtime.py" in str(step.get("run", ""))
    ]
    assert len(runtime_indexes) == 1, "Windows CI must dynamically qualify SQLCipher"
    assert install[0] < runtime_indexes[0], "qualify SQLCipher after installing its fixed wheel"
    evidence = [
        step
        for step in steps
        if step.get("name") == "Retain Windows persistence evidence"
    ]
    assert len(evidence) == 1, "Windows CI must retain persistence runner evidence"
    evidence_with = evidence[0].get("with", {})
    assert evidence_with.get("name") == "windows-persistence-evidence-${{ github.sha }}"
    assert evidence_with.get("if-no-files-found") == "error"
    evidence_paths = str(evidence_with.get("path", ""))
    assert "fixed-sqlcipher-runtime-evidence.json" in evidence_paths
    for profile in ("general", "deletion", "smoke"):
        assert f"tier-b-{profile}-junit.xml" in evidence_paths
    found = _tier_b_steps(workflow)
    assert len(found) == 3, f"expected three explicit Brain Tier B steps, found {found}"
    for profile, target in TIER_B_TARGETS.items():
        matches = [item for item in found if target in item[2]["run"]]
        assert len(matches) == 1, f"{target} must run exactly once"
        job_name, index, step = matches[0]
        assert job_name == windows_job and _runs_on_windows(job)
        assert step.get("env", {}).get("HYPOTHESIS_PROFILE") == profile
        assert "--override-ini=addopts=" in step["run"]
        assert "--junitxml" in step["run"]
        assert runtime_indexes[0] < index, "Tier B must run after SQLCipher qualification"


def test_windows_ci_qualifies_the_fixed_runtime_and_every_tier_b_profile() -> None:
    """Removing a persistent profile or its native-runtime probe turns CI red."""

    workflow = _workflow_document()
    _assert_windows_tier_b_qualification(workflow)

    broken = deepcopy(workflow)
    job_name, index, _ = _tier_b_steps(broken)[1]
    del _jobs(broken)[job_name]["steps"][index]
    with pytest.raises(AssertionError, match="three explicit Brain Tier B steps"):
        _assert_windows_tier_b_qualification(broken)


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
    test_index = _backend_suite_indexes(_jobs(reordered)[job_name])[0]
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


def _marker_expression(command: str) -> str | None:
    """The ``-m`` value of a pytest command, ignoring the interpreter's own."""

    tokens = shlex.split(command)[3:]
    for index, token in enumerate(tokens):
        if token == "-m" and index + 1 < len(tokens):
            return tokens[index + 1]
    return None


def _selects_production_boot(expression: str) -> bool:
    return (
        WINDOWS_PRODUCTION_MARKER in expression
        and f"not {WINDOWS_PRODUCTION_MARKER}" not in expression
    )


def test_the_production_boot_tests_are_selected_on_windows() -> None:
    """Production persistence derives its keys from Windows DPAPI.

    These tests are selected positively by a Windows job rather than skipped
    everywhere, so the assertions stay required. A dedicated invocation whose
    marker matches nothing exits 5, which fails the job rather than quietly
    dropping the coverage.
    """

    jobs = _jobs(_workflow_document())
    selecting = sorted(
        name
        for name, job in jobs.items()
        if _runs_on_windows(job)
        and any(
            _selects_production_boot(expression)
            for expression in (
                _marker_expression(step["run"].strip())
                for step in _steps(job)
                if isinstance(step.get("run"), str)
            )
            if expression is not None
        )
    )
    assert selecting, (
        f"no Windows job selects `{WINDOWS_PRODUCTION_MARKER}`, so the production "
        f"boot tests would stop executing anywhere in CI"
    )


def _windows_only_sources() -> list[str]:
    """Browser-suite sources that refuse to run off Windows, read from source.

    Derived at check time rather than listed here: a literal list would keep
    asserting the suite is Windows-only after the guard was removed from it.
    Both the specs and the helpers they share are scanned, because a spec
    inherits the constraint from any helper it calls.
    """

    directory = ROOT / BROWSER_SUITE_DIRECTORY
    return sorted(
        f"{path.parent.name}/{path.name}"
        for pattern in ("tests/*.mjs", "lib/*.mjs")
        for path in directory.glob(pattern)
        if WINDOWS_ONLY_SPEC_GUARD in path.read_text(encoding="utf-8")
    )


def _runs_the_browser_suite(step: dict[str, Any]) -> bool:
    command = step.get("run")
    if not isinstance(command, str):
        return False
    directory = str(step.get("working-directory", "")).replace("\\", "/").strip("/")
    if directory != BROWSER_SUITE_DIRECTORY:
        return False
    return any(invocation in command for invocation in BROWSER_SUITE_INVOCATIONS)


def _jobs_running_the_browser_suite(workflow: dict[str, Any]) -> list[str]:
    return sorted(
        name
        for name, job in _jobs(workflow).items()
        if any(_runs_the_browser_suite(step) for step in _steps(job))
    )


def _assert_the_browser_suite_runs_on_windows(workflow: dict[str, Any]) -> None:
    names = _jobs_running_the_browser_suite(workflow)
    assert names, (
        f"no ci.yml job runs the `{BROWSER_SUITE_DIRECTORY}` suite, so the "
        f"browser acceptance specs would stop executing in CI"
    )
    for name in names:
        assert _runs_on_windows(_jobs(workflow)[name]), (
            f"job `{name}` runs the `{BROWSER_SUITE_DIRECTORY}` suite on a "
            f"non-Windows runner, where {_windows_only_sources()} throw"
        )


def test_the_browser_suite_runs_where_its_windows_only_specs_can_execute() -> None:
    """Retargeting the browser job at ubuntu-latest turns the named check red.

    A spec that refuses to run off Windows fails the job rather than skipping,
    so routing the suite to a Linux runner is a defect the workflow cannot
    express as a passing run.
    """

    workflow = _workflow_document()
    assert _windows_only_sources(), (
        "no Playwright spec carries the Windows-only guard, so this check "
        "could only ever return one answer"
    )
    _assert_the_browser_suite_runs_on_windows(workflow)

    retargeted = deepcopy(workflow)
    job_name = _jobs_running_the_browser_suite(retargeted)[0]
    _jobs(retargeted)[job_name]["runs-on"] = "ubuntu-latest"
    with pytest.raises(AssertionError, match="on a non-Windows runner"):
        _assert_the_browser_suite_runs_on_windows(retargeted)


def test_dropping_the_browser_suite_from_ci_turns_the_check_red() -> None:
    """Deleting the job must fail rather than pass vacuously.

    The Windows-runner assertion iterates the jobs that run the suite, so with
    no such job it holds trivially. This is the check that notices.
    """

    workflow = _workflow_document()
    stripped = deepcopy(workflow)
    for name in _jobs_running_the_browser_suite(stripped):
        del _jobs(stripped)[name]
    with pytest.raises(AssertionError, match="no ci.yml job runs the"):
        _assert_the_browser_suite_runs_on_windows(stripped)


def test_non_windows_jobs_deselect_the_production_boot_tests() -> None:
    """A job that collects the whole suite off Windows must exclude them.

    Without the exclusion the suite fails on the unsupported platform, which
    is the failure this reconciliation removes.
    """

    jobs = _jobs(_workflow_document())
    for name, job in jobs.items():
        if _runs_on_windows(job):
            continue
        for index in _backend_suite_indexes(job):
            expression = _marker_expression(_steps(job)[index]["run"].strip())
            assert expression is not None and (
                f"not {WINDOWS_PRODUCTION_MARKER}" in expression
            ), (
                f"job `{name}` runs the backend suite on a non-Windows runner "
                f"without deselecting `{WINDOWS_PRODUCTION_MARKER}`"
            )


def _tier_a_steps(workflow: dict[str, Any]) -> list[tuple[str, int, dict[str, Any]]]:
    return [
        (job_name, index, step)
        for job_name, job in _jobs(workflow).items()
        for index, step in enumerate(_steps(job))
        if isinstance(step.get("run"), str)
        and any(target in step["run"] for target in TIER_A_TARGETS.values())
    ]


def _assert_tier_a_profiles_are_required_once(workflow: dict[str, Any]) -> None:
    found = _tier_a_steps(workflow)
    assert len(found) == 2, f"expected two explicit Brain Tier A steps, found {found}"
    for profile, target in TIER_A_TARGETS.items():
        matches = [item for item in found if target in item[2]["run"]]
        assert len(matches) == 1, f"{target} must run exactly once"
        job_name, index, step = matches[0]
        job = _jobs(workflow)[job_name]
        assert not _runs_on_windows(job), "high-volume Tier A belongs in the required Linux lane"
        assert step.get("env", {}).get("HYPOTHESIS_PROFILE") == profile
        assert "--override-ini=addopts=" in step["run"]
        installs = _run_step_indexes(job, DEPENDENCY_INSTALL_COMMAND)
        assert installs and installs[0] < index, "Tier A must run after backend dependencies install"


def test_brain_tier_a_profiles_run_once_in_the_required_linux_job() -> None:
    workflow = _workflow_document()
    _assert_tier_a_profiles_are_required_once(workflow)

    missing = deepcopy(workflow)
    job_name, index, _ = _tier_a_steps(missing)[0]
    del _jobs(missing)[job_name]["steps"][index]
    with pytest.raises(AssertionError, match="two explicit Brain Tier A steps"):
        _assert_tier_a_profiles_are_required_once(missing)


def _assert_analytics_determinism_profile(workflow: dict[str, Any]) -> None:
    matches = [
        (job_name, step)
        for job_name, job in _jobs(workflow).items()
        for step in _steps(job)
        if isinstance(step.get("run"), str) and ANALYTICS_DETERMINISM_TARGET in step["run"]
    ]
    assert len(matches) == 1, "analytics determinism must run in exactly one explicit CI step"
    job_name, step = matches[0]
    assert not _runs_on_windows(_jobs(workflow)[job_name])
    assert step.get("env", {}).get("HYPOTHESIS_PROFILE") == ANALYTICS_DETERMINISM_PROFILE
    assert "--override-ini=addopts=" in step["run"]


def test_analytics_determinism_profile_runs_once_in_the_required_linux_job() -> None:
    """Analytics determinism is explicit CI evidence, not a default rerun."""

    workflow = _workflow_document()
    _assert_analytics_determinism_profile(workflow)

    missing = deepcopy(workflow)
    job_name, _ = [
        (name, step)
        for name, job in _jobs(missing).items()
        for step in _steps(job)
        if isinstance(step.get("run"), str) and ANALYTICS_DETERMINISM_TARGET in step["run"]
    ][0]
    job = _jobs(missing)[job_name]
    job["steps"] = [
        candidate
        for candidate in job["steps"]
        if not (isinstance(candidate.get("run"), str) and ANALYTICS_DETERMINISM_TARGET in candidate["run"])
    ]
    with pytest.raises(AssertionError, match="analytics determinism must run"):
        _assert_analytics_determinism_profile(missing)


def test_agent_tier_a_profiles_run_once_and_default_backend_excludes_them() -> None:
    workflow = _workflow_document()
    rendered = WORKFLOW.read_text(encoding="utf-8")
    for profile, target in AGENT_TIER_A_TARGETS.items():
        assert rendered.count(target) == 1
        assert f"HYPOTHESIS_PROFILE: {profile}" in rendered
    assert f"not {STATEFUL_AGENT_TIER_A_MARKER}" in PYTEST_INI.read_text(encoding="utf-8")
    for job in _jobs(workflow).values():
        for index in _backend_suite_indexes(job):
            expression = _marker_expression(_steps(job)[index]["run"].strip())
            if expression is not None and not _selects_production_boot(expression):
                assert f"not {STATEFUL_AGENT_TIER_A_MARKER}" in expression


def _analytics_convergence_steps(workflow: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (job_name, step)
        for job_name, job in _jobs(workflow).items()
        for step in _steps(job)
        if isinstance(step.get("run"), str)
        and any(target in step["run"] for target in ANALYTICS_CONVERGENCE_TARGETS.values())
    ]


def _assert_analytics_convergence_profiles_are_required_once(workflow: dict[str, Any]) -> None:
    matches = _analytics_convergence_steps(workflow)
    assert len(matches) == 2, "analytics convergence must run two explicit CI profiles"
    for profile, target in ANALYTICS_CONVERGENCE_TARGETS.items():
        selected = [item for item in matches if target in item[1]["run"]]
        assert len(selected) == 1, f"{target} must run exactly once"
        job_name, step = selected[0]
        assert not _runs_on_windows(_jobs(workflow)[job_name])
        assert step.get("env", {}).get("HYPOTHESIS_PROFILE") == profile
        assert "--override-ini=addopts=" in step["run"]


def test_analytics_convergence_and_deletion_profiles_run_once_in_linux_ci() -> None:
    """The two expensive profiles need explicit execution, not default collection."""

    workflow = _workflow_document()
    _assert_analytics_convergence_profiles_are_required_once(workflow)

    missing = deepcopy(workflow)
    job_name, step = _analytics_convergence_steps(missing)[0]
    _jobs(missing)[job_name]["steps"].remove(step)
    with pytest.raises(AssertionError, match="analytics convergence must run two explicit"):
        _assert_analytics_convergence_profiles_are_required_once(missing)


def _assert_analytics_convergence_falsifiers_required_once(workflow: dict[str, Any]) -> None:
    """Require one Linux invocation of both marker-excluded falsifiers."""

    matches = [
        (job_name, step)
        for job_name, job in _jobs(workflow).items()
        for step in _steps(job)
        if isinstance(step.get("run"), str)
        and ANALYTICS_CONVERGENCE_FALSIFIER_TARGETS[0] in step["run"]
    ]
    assert len(matches) == 1, "analytics oracle falsifiers must run exactly once"
    job_name, step = matches[0]
    assert not _runs_on_windows(_jobs(workflow)[job_name])
    assert all(target in step["run"] for target in ANALYTICS_CONVERGENCE_FALSIFIER_TARGETS)


def test_analytics_convergence_permanent_falsifiers_run_once_in_linux_ci() -> None:
    """The ordinary suite excludes these expensive negative controls."""

    workflow = _workflow_document()
    _assert_analytics_convergence_falsifiers_required_once(workflow)

    missing = deepcopy(workflow)
    job_name, step = next(
        (candidate_job, candidate_step)
        for candidate_job, job in _jobs(missing).items()
        for candidate_step in _steps(job)
        if isinstance(candidate_step.get("run"), str)
        and ANALYTICS_CONVERGENCE_FALSIFIER_TARGETS[0] in candidate_step["run"]
    )
    _jobs(missing)[job_name]["steps"].remove(step)
    with pytest.raises(AssertionError, match="analytics oracle falsifiers"):
        _assert_analytics_convergence_falsifiers_required_once(missing)


def test_ordinary_backend_suites_exclude_explicit_tier_a_tests() -> None:
    assert f"not {STATEFUL_TIER_A_MARKER}" in PYTEST_INI.read_text(encoding="utf-8")
    for job_name, job in _jobs(_workflow_document()).items():
        for index in _backend_suite_indexes(job):
            expression = _marker_expression(_steps(job)[index]["run"].strip())
            if expression is not None and not _selects_production_boot(expression):
                assert f"not {STATEFUL_TIER_A_MARKER}" in expression, (
                    f"job `{job_name}` ordinary backend suite reruns Tier A"
                )


def test_tier_b_is_explicit_windows_qualification_not_an_ordinary_suite() -> None:
    workflow = _workflow_document()
    serialized = WORKFLOW.read_text(encoding="utf-8")
    for profile, target in TIER_B_TARGETS.items():
        assert profile in serialized and target in serialized
    assert f"not {STATEFUL_TIER_B_MARKER}" in PYTEST_INI.read_text(encoding="utf-8")
    for job in _jobs(workflow).values():
        for index in _backend_suite_indexes(job):
            expression = _marker_expression(_steps(job)[index]["run"].strip())
            if expression is not None and not _selects_production_boot(expression):
                assert f"not {STATEFUL_TIER_B_MARKER}" in expression
