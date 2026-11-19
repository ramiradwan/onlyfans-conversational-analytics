from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

import pytest

pytestmark = [pytest.mark.ci_tier('fast')]


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
REQUIRED_JOBS = {
    "desktop-native-contracts",
    "web-build-and-test", "backend-fast", "analytics-integration",
    "fixed-sqlcipher-wheel", "windows-platform-contract",
    "analytics-windows-contract", "windows-browser-e2e", "windows-full-regression",
    "windows-full-shards",
    "analytics-scale-qualification", "candidate-preflight", "windows-qualification", "web-build-assets",
}
BACKEND_JOBS = {
    "backend-fast", "analytics-integration", "windows-platform-contract",
    "analytics-windows-contract", "windows-full-shards",
    "analytics-scale-qualification", "windows-qualification",
}
WINDOWS_CONSUMERS = {
    "windows-platform-contract", "analytics-windows-contract",
    "browser-e2e-execution", "browser-e2e-serial-control", "windows-full-shards",
    "analytics-scale-qualification", "windows-qualification",
}
WINDOWS_FULL_CUTOVER_RUN = (
    'case "$GITHUB_EVENT_NAME" in\n'
    "  pull_request) test '${{ needs.windows-full-shards.result }}' = 'skipped' ;;\n"
    "  push|workflow_dispatch) test '${{ needs.windows-full-shards.result }}' = 'success' ;;\n"
    '  *) echo "Unsupported Product CI event"; exit 1 ;;\n'
    "esac\n"
)


def _workflow_document() -> dict[str, Any]:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{WORKFLOW} is not a mapping document"
    return document


def _assert_gate_covers_every_lane(workflow: dict[str, Any]) -> None:
    jobs = workflow["jobs"]
    assert REQUIRED_JOBS <= set(jobs), "a required lane is missing"
    gate = jobs["required-ci-gate"]
    assert gate["name"] == "Required CI"
    assert gate["if"] == "${{ always() }}", "the gate must evaluate upstream failures"
    assert set(gate["needs"]) == REQUIRED_JOBS, "the gate must wait for every required lane"
    for job_name in REQUIRED_JOBS:
        job = jobs[job_name]
        if job_name == "analytics-scale-qualification":
            assert job.get("if") == "${{ always() && !cancelled() && (github.event_name != 'pull_request') && needs.fixed-sqlcipher-wheel.result == 'success' }}", "scale may skip only pull requests"
        elif job_name == "windows-full-regression":
            assert job.get("if") == "${{ always() }}", "full Windows aggregate must evaluate shard failures"
            assert job["needs"] == "windows-full-shards"
            assert job["steps"][0]["run"] == WINDOWS_FULL_CUTOVER_RUN
        elif job_name == "windows-full-shards":
            assert job.get("if") == "${{ always() && !cancelled() && (github.event_name == 'push' || github.event_name == 'workflow_dispatch') && needs.fixed-sqlcipher-wheel.result == 'success' }}", "exhaustive Windows may skip only pull requests"
        elif job_name == "windows-browser-e2e":
            assert job.get("if") == "${{ always() }}", "browser aggregate must evaluate execution failures"
            assert set(job["needs"]) == {"browser-reporting-safety", "browser-e2e-execution", "browser-e2e-serial-control"}
        elif job_name in {"windows-platform-contract", "analytics-windows-contract", "windows-qualification"}:
            assert job.get("if") == "${{ always() && !cancelled() && (true) && needs.fixed-sqlcipher-wheel.result == 'success' }}"
        else:
            assert "if" not in job, f"required lane {job_name} cannot skip during PR cutover"
        assert not job.get("continue-on-error"), f"required lane {job_name} cannot ignore failures"


def test_ci_uses_pr_scoped_cancellation_but_preserves_every_main_run() -> None:
    workflow = _workflow_document()
    assert workflow.get("permissions") == {"contents": "read"}
    concurrency = workflow["concurrency"]
    assert concurrency["group"] == (
        "${{ github.workflow }}-${{ github.event_name }}-"
        "${{ github.event.pull_request.number || github.run_id }}"
    )
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"


@pytest.mark.parametrize("mutation", ["missing", "unneeded", "skip", "ignore-failure", "skip-gate", "skip-scale-on-main"])
def test_required_gate_fails_closed_when_a_lane_is_weakened(mutation: str) -> None:
    workflow = _workflow_document()
    _assert_gate_covers_every_lane(workflow)
    broken = deepcopy(workflow)
    jobs = broken["jobs"]
    if mutation == "missing":
        del jobs["analytics-integration"]
    elif mutation == "unneeded":
        jobs["required-ci-gate"]["needs"].remove("analytics-integration")
    elif mutation == "skip":
        jobs["windows-full-regression"]["if"] = "github.event_name != 'pull_request'"
    elif mutation == "ignore-failure":
        jobs["analytics-windows-contract"]["continue-on-error"] = True
    elif mutation == "skip-scale-on-main":
        jobs["analytics-scale-qualification"]["if"] = "github.event_name == 'schedule'"
    else:
        jobs["required-ci-gate"]["if"] = "success()"
    with pytest.raises(AssertionError):
        _assert_gate_covers_every_lane(broken)


def test_all_jobs_have_explicit_timeouts_and_integration_is_bounded() -> None:
    jobs = _workflow_document()["jobs"]
    for name, job in jobs.items():
        timeout = job.get("timeout-minutes")
        limit = 90 if name == "windows-full-shards" else 60
        assert isinstance(timeout, int) and 0 < timeout <= limit, f"{name} needs a bounded timeout"
    assert jobs["backend-fast"]["timeout-minutes"] == 30
    shard = jobs["analytics-integration"]
    assert shard["name"] == "analytics-integration-${{ matrix.shard }}"
    assert shard["strategy"] == {"fail-fast": False, "max-parallel": 4, "matrix": {"shard": [1, 2, 3, 4]}}
    assert shard["timeout-minutes"] == 30


def _assert_full_windows_shards_are_blocking(workflow: dict[str, Any]) -> None:
    _assert_gate_covers_every_lane(workflow)
    job = workflow["jobs"]["windows-full-shards"]
    assert job["name"] == "windows-full-regression-${{ matrix.shard }}"
    assert job["runs-on"] == "windows-latest"
    assert job["strategy"] == {"fail-fast": False, "max-parallel": 8, "matrix": {"shard": list(range(1, 9))}}
    assert job["timeout-minutes"] == 90
    execution = next(step for step in job["steps"] if step.get("name") == "Test complete Windows backend regression shard")
    assert execution["run"] == "python tools/test_backend.py --lane windows-full-regression --windows-v9-group full-${{ matrix.shard }}"
    assert execution["env"]["CI_TEST_LANE"] == "candidate-full-${{ matrix.shard }}"
    assert not execution.get("continue-on-error")
    upload = next(step for step in job["steps"] if step.get("name") == "Retain backend timing and selection")
    assert upload["with"]["name"] == "ci-tests-candidate-full-${{ matrix.shard }}-${{ env.PRODUCT_SHA }}-${{ github.run_id }}-${{ github.run_attempt }}"


@pytest.mark.parametrize("mutation", ["omit-shard", "two-shards", "serialize-shards", "overlap-shard", "ignore-failure",
                                      "omit-aggregate", "hide-shards", "allow-cancelled"])
def test_full_windows_split_cannot_drop_or_hide_a_required_shard(mutation: str) -> None:
    workflow = _workflow_document()
    _assert_full_windows_shards_are_blocking(workflow)
    broken = deepcopy(workflow)
    jobs = broken["jobs"]
    if mutation == "omit-shard":
        jobs["windows-full-shards"]["strategy"]["matrix"]["shard"] = [1]
    elif mutation == "two-shards":
        jobs["windows-full-shards"]["strategy"].update({"max-parallel": 2, "matrix": {"shard": [1, 2]}})
    elif mutation == "serialize-shards":
        jobs["windows-full-shards"]["strategy"]["max-parallel"] = 2
    elif mutation == "overlap-shard":
        jobs["windows-full-shards"]["strategy"]["matrix"]["shard"] = [1, 1]
    elif mutation == "ignore-failure":
        jobs["windows-full-shards"]["continue-on-error"] = True
    elif mutation == "omit-aggregate":
        jobs["required-ci-gate"]["needs"].remove("windows-full-regression")
    elif mutation == "hide-shards":
        jobs["required-ci-gate"]["needs"].remove("windows-full-shards")
    else:
        jobs["windows-full-regression"]["steps"][0]["run"] = "true"
    with pytest.raises(AssertionError):
        _assert_full_windows_shards_are_blocking(broken)


def test_windows_consumers_fail_closed_on_the_shared_producer() -> None:
    jobs = _workflow_document()["jobs"]
    for name in WINDOWS_CONSUMERS:
        job = jobs[name]
        if name in {"browser-e2e-execution", "browser-e2e-serial-control"}:
            assert set(job["needs"]) == {"fixed-sqlcipher-wheel", "browser-reporting-safety"}
        else:
            assert set(job["needs"]) == {"fixed-sqlcipher-wheel", "web-build-assets"}
        assert job["runs-on"].startswith("windows")
        steps = job["steps"]
        verify = next(step for step in steps if step.get("name") == "Verify fixed SQLCipher CI source")
        install = next(step for step in steps if step.get("name") == "Install backend dependencies")
        assert steps.index(verify) < steps.index(install)
        assert "source SHA mismatch" in verify["run"] and "run ID mismatch" in verify["run"]
        assert "workflow_run_attempt -lt 1" in verify["run"]
        assert "workflow_run_attempt -gt [int]$env:GITHUB_RUN_ATTEMPT" in verify["run"]


def _assert_backend_execution_evidence(workflow: dict[str, Any]) -> None:
    jobs = workflow["jobs"]
    lanes = set()
    for name in BACKEND_JOBS:
        steps = jobs[name]["steps"]
        # This exact command only consumes the preceding raw report. Everything
        # else invoking the runner remains subject to execution-evidence checks.
        report_validation = (
            "python tools/test_backend.py validate-windows-v9-manifest "
            "--inventory artifacts/ci-tests/legacy-windows-reference/report.json"
        )
        invocations = [step for step in steps
                       if step.get("run") != report_validation
                       and ("python -m pytest" in str(step.get("run", ""))
                            or "python tools/test_backend.py" in str(step.get("run", "")))]
        assert invocations, f"{name} has no tests"
        for step in invocations:
            assert isinstance(step.get("env"), dict), f"{step.get('name')} has no execution evidence environment"
            environment = step["env"]
            lane = environment["CI_TEST_LANE"]
            assert lane not in lanes, f"{lane} overwrites another invocation's evidence"
            lanes.add(lane)
            assert environment["CI_REPORT_DIR"] == f"artifacts/ci-tests/{lane}"
            addopts = environment["PYTEST_ADDOPTS"]
            for required in ("-p tools.ci_pytest", "--durations=50", "--durations-min=0.25", f"--junitxml=artifacts/ci-tests/{lane}/junit.xml"):
                assert required in addopts
        upload = next(step for step in steps if step.get("name") == "Retain backend timing and selection")
        assert upload["if"] == "always()"
        assert upload["with"]["retention-days"] == 14
        assert upload["with"]["if-no-files-found"] == "error"
        assert not upload["with"].get("overwrite"), "attempt-specific execution evidence must stay immutable"
        for coordinate in ("${{ env.PRODUCT_SHA }}", "${{ github.run_id }}", "${{ github.run_attempt }}"):
            assert coordinate in upload["with"]["name"]


def test_every_backend_invocation_retains_distinct_failure_evidence() -> None:
    _assert_backend_execution_evidence(_workflow_document())


def test_only_the_default_lane_runners_use_parallel_workers() -> None:
    jobs = _workflow_document()["jobs"]
    parallel = {}
    for name in BACKEND_JOBS:
        for step in jobs[name]["steps"]:
            environment = step.get("env", {})
            if "CI_PYTEST_WORKERS" in environment:
                parallel[environment["CI_TEST_LANE"]] = (environment["CI_PYTEST_WORKERS"], step["run"])
    assert {lane: workers for lane, (workers, _) in parallel.items()} == {
        "backend-fast": "4", "analytics-integration-${{ matrix.shard }}": "4",
        "candidate-platform-${{ matrix.shard }}": "4", "candidate-analytics-${{ matrix.shard }}": "4",
        "candidate-full-${{ matrix.shard }}": "4"}
    # Stateful profiles, explicit selectors and scale qualification stay in one process.
    assert all(command.startswith("python tools/test_backend.py --lane ") for _, command in parallel.values())


@pytest.mark.parametrize("command", [
    "python tools/test_backend.py --lane fast",
    "python tools/test_backend.py validate-windows-v9-manifest "
    "--inventory artifacts/ci-tests/legacy-windows-reference/report.json\n"
    "python tools/test_backend.py --lane fast",
])
def test_read_only_validation_cannot_hide_execution_without_failure_evidence(command: str) -> None:
    workflow = _workflow_document()
    step = next(step for step in workflow["jobs"]["windows-qualification"]["steps"]
                if step.get("name") == "Validate exhaustive Windows shard inventory")
    step["run"] = command
    with pytest.raises(AssertionError, match="no execution evidence environment"):
        _assert_backend_execution_evidence(workflow)


def test_gate_keeps_attempt_artifacts_separate_and_compatibility_checks_block() -> None:
    jobs = _workflow_document()["jobs"]
    gate = jobs["required-ci-gate"]
    assert gate["permissions"] == {"contents": "read", "actions": "read"}
    download = next(step for step in gate["steps"] if step.get("name") == "Retrieve backend execution evidence")
    assert download["with"]["pattern"] == "ci-tests-*"
    assert download["with"]["merge-multiple"] is False
    check = next(step for step in gate["steps"] if step.get("name") == "Require complete successful CI evidence")
    assert "tools/ci_gate.py" in check["run"]
    assert check["env"]["CI_NEEDS_JSON"] == "${{ toJSON(needs) }}"
    for alias in ("build-and-test", "windows-tests"):
        job = jobs[alias]
        assert job["if"] == "${{ always() }}"
        assert job["needs"] == "required-ci-gate"
        assert job["steps"][0]["run"] == "test '${{ needs.required-ci-gate.result }}' = 'success'"


def test_main_and_manual_qualification_are_explicit_without_duplicate_schedule_or_path_filters() -> None:
    workflow = _workflow_document()
    assert workflow["env"]["CI_POLICY_VERSION"] == "sharded-v9-pr-cutover"
    events = workflow["on"]
    assert set(events) == {"push", "pull_request", "workflow_dispatch"}
    dispatch = events["workflow_dispatch"]
    assert set(dispatch) == {"inputs"}
    assert set(dispatch["inputs"]) == {"browser_qualification", "browser_serial_control", "force_sqlcipher_cold"}
    for declaration in dispatch["inputs"].values():
        assert declaration["type"] == "boolean" and declaration["default"] is False
        assert declaration.get("required", False) is False
    assert events["push"] == {"branches": ["main"]}
    assert events["pull_request"] == {"branches": ["main"], "types": ["opened", "synchronize", "reopened"]}
    for event in ("push", "pull_request"):
        assert "paths" not in events[event] and "paths-ignore" not in events[event]
    assert "edited" not in events["pull_request"]["types"]
    architecture = yaml.load((WORKFLOW.parent / "architecture-impact.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert "edited" in architecture["on"]["pull_request"]["types"]


def test_analytics_scale_qualification_uses_complete_analytics_populations() -> None:
    jobs = _workflow_document()["jobs"]
    job = jobs["analytics-scale-qualification"]
    assert job["if"] == "${{ always() && !cancelled() && (github.event_name != 'pull_request') && needs.fixed-sqlcipher-wheel.result == 'success' }}"
    steps = job["steps"]
    execution = next(step for step in steps if step.get("env", {}).get("CI_TEST_LANE") == "analytics-scale-qualification")
    assert execution["run"] == (
        "python tools/test_backend.py scale -- --ignore=tests/test_packaged_runtime.py "
        "--ignore=tests/test_installation_key.py"
    )
    contract_steps = jobs["analytics-windows-contract"]["steps"]
    contract_execution = next(step for step in contract_steps if step.get("env", {}).get("CI_TEST_LANE") == "candidate-analytics-${{ matrix.shard }}")
    assert steps[:steps.index(execution)] == contract_steps[:contract_steps.index(contract_execution)]


def test_legacy_reference_collections_are_independent_of_the_new_selector() -> None:
    jobs = _workflow_document()["jobs"]
    for job_name, lane, marker in (
        ("backend-fast", "legacy-linux-reference", "not slow and not windows_production and not stateful_tier_a and not stateful_tier_b and not stateful_agent_tier_a"),
        ("windows-qualification", "legacy-windows-reference", "not slow and not stateful_tier_a and not stateful_tier_b and not stateful_agent_tier_a"),
    ):
        references = [step for step in jobs[job_name]["steps"] if step.get("env", {}).get("CI_TEST_LANE") == lane]
        assert len(references) == 1
        reference = references[0]
        assert reference["run"] == f'python -m pytest --collect-only --override-ini=addopts= -m "{marker}"'
        assert "--ci-lane" not in reference["env"]["PYTEST_ADDOPTS"]


HOST_JOBS = {"windows-platform-contract": "Test Windows platform contract",
             "analytics-windows-contract": "Test Windows analytics contract",
             "windows-full-shards": "Test complete Windows backend regression shard",
             "analytics-scale-qualification": "Qualify analytics scale on Windows"}
HOST_STEPS = ("Describe Windows test host", "Start host counter sampling", "Stop host counter sampling")
BENCH = WORKFLOW.parent / "windows-backend-bench.yml"


def _assert_windows_host_diagnostics(jobs) -> None:
    for name, job in jobs.items():
        steps = job.get("steps", [])
        names = [step.get("name") for step in steps]
        if name not in HOST_JOBS:
            assert not set(HOST_STEPS) & set(names), name
            continue
        lane = names.index(HOST_JOBS[name])
        describe, start, stop = (steps[names.index(step)] for step in HOST_STEPS)
        assert describe["run"] == "./tools/ci_windows_host.ps1" and "if" not in describe
        assert names.index("Describe Windows test host") < lane
        # Sampling brackets exactly the lane step and can never fail the job.
        assert names[lane - 1] == "Start host counter sampling" and names[lane + 1] == "Stop host counter sampling"
        assert start["run"] == "./tools/ci_windows_host.ps1 -StartCounters" and "if" not in start
        assert stop["if"] == "always()"
        assert start["continue-on-error"] is True and stop["continue-on-error"] is True
        assert stop["run"] == "./tools/ci_windows_host.ps1 -StopCounters -Destination " + steps[lane]["env"]["CI_REPORT_DIR"]
        assert lane + 1 < names.index("Retain backend timing and selection")
    for job in jobs.values():
        for step in job.get("steps", []):
            assert "CI_PYTEST_DIST" not in step.get("env", {}), "Product CI keeps the default scheduler"


def test_windows_backend_jobs_record_host_state_and_counters_around_the_lane() -> None:
    _assert_windows_host_diagnostics(_workflow_document()["jobs"])


@pytest.mark.parametrize("fault", ["no-describe", "unbracketed", "fatal-sampler", "wrong-destination", "dist"])
def test_windows_host_diagnostic_falsifiers(fault: str) -> None:
    jobs = deepcopy(_workflow_document()["jobs"])
    steps = jobs["windows-full-shards"]["steps"]
    named = {step["name"]: step for step in steps}
    if fault == "no-describe":
        steps.remove(named["Describe Windows test host"])
    elif fault == "unbracketed":
        steps.remove(named["Start host counter sampling"])
        steps.insert(0, named["Start host counter sampling"])
    elif fault == "fatal-sampler":
        del named["Stop host counter sampling"]["continue-on-error"]
    elif fault == "wrong-destination":
        named["Stop host counter sampling"]["run"] = "./tools/ci_windows_host.ps1 -StopCounters -Destination artifacts"
    else:
        named["Test complete Windows backend regression shard"]["env"]["CI_PYTEST_DIST"] = "worksteal"
    with pytest.raises((AssertionError, KeyError, ValueError)):
        _assert_windows_host_diagnostics(jobs)


def _historical_bench_product():
    return yaml.safe_load((ROOT / "tests/fixtures/product-ci-sharded-v8-pr-cutover.yml").read_text(encoding="utf-8"))


def _bench_document() -> dict[str, Any]:
    document = yaml.safe_load(BENCH.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _bench_triggers(bench: dict[str, Any]) -> dict[str, Any]:
    # PyYAML reads the unquoted key `on` as True.
    return bench[True] if True in bench else bench["on"]


def _assert_bench_repeats_product_ci(bench: dict[str, Any], product: dict[str, Any]) -> None:
    jobs = bench["jobs"]
    assert set(jobs) == {"plan", "fixed-sqlcipher-wheel", "windows-full-regression"}
    # Invalid bench refs stop in the plan job before any Windows setup runs.
    # The worker benchmark deliberately measures the cold wheel control, not cache reuse.
    cold = deepcopy(product["jobs"]["fixed-sqlcipher-wheel"])
    cold.pop("permissions", None)
    cold["steps"] = [step for step in cold["steps"] if step["name"] in {
        "Checkout exact Product revision", "Setup Python", "Prepare native dependency cache",
        "Cache native dependencies", "Build fixed SQLCipher wheel", "Record fixed SQLCipher CI source",
        "Retain fixed SQLCipher wheel and provenance"}]
    for step in cold["steps"]:
        step.pop("if", None)
        if step.get("id") == "vcpkg-cache":
            step.pop("continue-on-error", None)
            step["uses"] = step["uses"].replace("actions/cache/restore@", "actions/cache@")
    assert jobs["fixed-sqlcipher-wheel"] == {"name": "bench-fixed-sqlcipher-wheel", "needs": "plan", **cold}
    shard = "${{ needs.plan.outputs.shard }}"
    def substitute(value):
        if isinstance(value, dict):
            return {key: substitute(item) for key, item in value.items()}
        if isinstance(value, list):
            return [substitute(item) for item in value]
        return value.replace("${{ matrix.shard }}", shard) if isinstance(value, str) else value

    expected = substitute(product["jobs"]["windows-full-shards"]["steps"])
    lane = next(step for step in expected if step["name"] == "Test complete Windows backend regression shard")
    lane["env"]["CI_PYTEST_WORKERS"] = "${{ needs.plan.outputs.workers }}"
    lane["env"]["CI_PYTEST_DIST"] = "${{ needs.plan.outputs.dist }}"
    full = jobs["windows-full-regression"]
    assert full["steps"] == expected, "bench shard steps must repeat Product CI apart from its parameters"
    assert full["name"] == f"bench-windows-full-regression-{shard}"
    assert full["needs"] == ["plan", "fixed-sqlcipher-wheel"]
    assert full["env"] == product["jobs"]["windows-full-shards"]["env"]
    assert full["timeout-minutes"] == product["jobs"]["windows-full-shards"]["timeout-minutes"]
    assert bench["env"]["CI_POLICY_VERSION"] == product["env"]["CI_POLICY_VERSION"]


def test_windows_backend_bench_repeats_the_product_ci_shard() -> None:
    bench = _bench_document()
    _assert_bench_repeats_product_ci(bench, _historical_bench_product())
    # The bench runs only for pushes to bench branches, never for main or pull requests.
    assert _bench_triggers(bench) == {"push": {"branches": ["bench/windows-backend/**"]}}
    assert bench["permissions"] == {"contents": "read"}
    assert bench["concurrency"] == {"group": "${{ github.workflow }}-${{ github.ref }}", "cancel-in-progress": False}


@pytest.mark.parametrize("fault", ["step", "wheel", "wheel-before-plan", "workers", "extra-job"])
def test_windows_backend_bench_falsifiers(fault: str) -> None:
    bench = deepcopy(_bench_document())
    jobs = bench["jobs"]
    if fault == "step":
        jobs["windows-full-regression"]["steps"][-2]["run"] = "true"
    elif fault == "wheel":
        jobs["fixed-sqlcipher-wheel"]["timeout-minutes"] = 5
    elif fault == "wheel-before-plan":
        del jobs["fixed-sqlcipher-wheel"]["needs"]
    elif fault == "workers":
        lane = next(step for step in jobs["windows-full-regression"]["steps"] if "CI_TEST_LANE" in step.get("env", {}))
        lane["env"]["CI_PYTEST_WORKERS"] = "8"
    else:
        jobs["backend-fast"] = _workflow_document()["jobs"]["backend-fast"]
    with pytest.raises(AssertionError):
        _assert_bench_repeats_product_ci(bench, _historical_bench_product())


def _assert_bench_shards_match_product_ci(run: str, product: dict[str, Any]) -> None:
    import re

    shards = "".join(str(shard) for shard in product["jobs"]["windows-full-shards"]["strategy"]["matrix"]["shard"])
    pattern = re.compile(re.search(r"=~ (\S+) \]\]", run).group(1))
    assert {str(shard) for shard in range(10) if pattern.fullmatch(f"bench/windows-backend/s{shard}-w4-load")} == set(shards)
    assert f"[{shards}]:[234]:load|[{shards}]:[234]:worksteal) ;;" in run


def test_windows_backend_bench_branch_names_select_only_supported_parameters() -> None:
    import re

    run = _bench_document()["jobs"]["plan"]["steps"][0]["run"]
    _assert_bench_shards_match_product_ci(run, _historical_bench_product())
    pattern = re.compile(re.search(r"=~ (\S+) \]\]", run).group(1))
    accepted = {"bench/windows-backend/s2-w3-load": ("2", "3", "load"),
                "bench/windows-backend/s1-w2-worksteal-r1": ("1", "2", "worksteal"),
                "bench/windows-backend/s3-w4-load": ("3", "4", "load"),
                "bench/windows-backend/s2-w4-load-round-3": ("2", "4", "load")}
    for name, groups in accepted.items():
        assert pattern.fullmatch(name).groups()[:3] == groups
    for name in ("bench/windows-backend/s4-w3-load", "bench/windows-backend/s0-w3-load",
                 "bench/windows-backend/s2-w8-load",
                 "bench/windows-backend/s2-w3-loadfile", "bench/windows-backend/s2-w3-load-R1",
                 "bench/windows-backend/x/s2-w3-load", "main"):
        assert pattern.fullmatch(name) is None, name


@pytest.mark.parametrize("old,new", [("s([123])-w", "s([12])-w"), ("s([123])-w", "s([1234])-w"),
                                     ("[123]:[234]:load", "[12]:[234]:load"),
                                     ("[123]:[234]:worksteal", "[1234]:[234]:worksteal")])
def test_windows_backend_bench_shards_cannot_drift_from_product_ci(old: str, new: str) -> None:
    run = _bench_document()["jobs"]["plan"]["steps"][0]["run"]
    assert old in run
    with pytest.raises(AssertionError):
        _assert_bench_shards_match_product_ci(run.replace(old, new, 1), _workflow_document())


@pytest.mark.parametrize("defender_available", [True, False])
def test_windows_host_sampler_records_defender_activity_without_requiring_access(tmp_path, defender_available):
    import csv
    import shutil
    import subprocess

    pwsh = shutil.which("pwsh")
    assert pwsh is not None, "PowerShell is required to exercise the mocked counter contract"
    harness = tmp_path / "sample.ps1"
    harness.write_text(r"""
param([string]$Script, [string]$Output, [string]$DefenderAvailable)
function Split-Path {
    param($Path, [switch]$Qualifier)
    if (-not $Qualifier) { throw "Unexpected path operation" }
    "C:"
}
function Get-Counter {
    param($Counter, $SampleInterval, [switch]$Continuous, $ErrorAction)
    [pscustomobject]@{
        Timestamp = [datetime]::UtcNow
        CounterSamples = @([pscustomobject]@{
            Path = '\host\processor(_total)\% processor time'; CookedValue = 42.5
        })
    }
}
function Get-Process {
    param($Name, $ErrorAction)
    if ($Name -ne 'MsMpEng' -or $DefenderAvailable -ne 'yes') { throw 'not accessible' }
    [pscustomobject]@{ Id = 1234; CPU = 12.25; WorkingSet64 = 98765 }
}
& $Script -SampleCounters -Output $Output
""", encoding="utf-8")
    output = tmp_path / "host-counters.csv"
    result = subprocess.run([pwsh, "-NoProfile", "-File", str(harness), "-Script",
                             str(ROOT / "tools/ci_windows_host.ps1"), "-Output", str(output),
                             "-DefenderAvailable", "yes" if defender_available else "no"],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    with output.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1 and None not in rows[0]
    row = rows[0]
    assert row["processor_time_percent"] == "42.5" and row["disk_queue_length"] == ""
    values = tuple(row[k] for k in ("defender_pid", "defender_cpu_seconds", "defender_working_set_bytes"))
    assert values == (("1234", "12.25", "98765") if defender_available else ("", "", ""))
