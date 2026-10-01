"""Behavioral failure cases for the gate, evidence binding and rerun policy."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tools import ci_gate as gate
from tools import engineering_attestation as producer

pytestmark = [pytest.mark.ci_tier('fast')]

SHA = "a" * 40
RUN = 81


def _phase_reports(selected):
    return [{"nodeid": node, "when": when, "outcome": "passed", "duration": 0.01,
             "wasxfail": None, "detail": ""}
            for node in selected for when in ("setup", "call", "teardown")]


def _reports():
    rows = {}

    def row(node, markers=(), windows=False, contract=None):
        rows[node] = {"nodeid": node, "markers": list(markers),
                      "windows_compat": windows, "windows_contract": contract}
        return node

    fast = {row("tests/test_unit.py::test_fast"),
            row("tests/test_platform.py::test_fs", windows=True, contract="platform"),
            row("tests/test_contract_snapshot.py::test_contract", ["contract_integrity"])}
    integrations = {n: row(f"tests/test_projection{n}.py::test_store", windows=n == 1,
                           contract="analytics" if n == 1 else None) for n in range(1, 5)}
    boot = row("tests/test_boot.py::test_boot", ["windows_production"], windows=True, contract="platform")
    profile_nodes = {}
    for lane, spec in gate.SPECS.items():
        if spec.selectors:
            profile_nodes[lane] = {row(selector + ("::runTest" if "::Test" in selector else ""),
                                       ["stateful_tier_b" if spec.platform == "Windows" else "stateful_tier_a"])
                                   for selector in spec.selectors}
    scales = {row(f"tests/test_scale_family{number}.py::test_large", ["slow"]) for number in range(1, 5)}
    row("tests/test_packaged_runtime.py::test_large", ["slow"])
    row("tests/test_installation_key.py::test_large", ["slow"])
    linux = fast | set(integrations.values())
    windows = linux | {boot}
    result = {}
    for lane, spec in gate.SPECS.items():
        if lane == "backend-fast":
            selected = fast
        elif lane.startswith("analytics-integration-"):
            selected = {integrations[int(lane[-1])]}
        elif lane == "windows-platform-contract":
            selected = {"tests/test_platform.py::test_fs", boot}
        elif lane == "analytics-windows-contract":
            selected = {integrations[1]}
        elif lane in {"windows-full-regression", "legacy-windows-reference"}:
            selected = windows
        elif lane == "legacy-linux-reference":
            selected = linux
        elif lane == "windows-production-boot":
            selected = {boot}
        elif lane == "backend-fast-contract-integrity":
            selected = {"tests/test_contract_snapshot.py::test_contract"}
        elif lane == gate.SCALE_LANE:
            selected = scales
        else:
            selected = profile_nodes[lane]
        result[lane] = {
            "schema": "ci-test-report/v1", "source_commit": SHA, "workflow_run_id": str(RUN),
            "run_attempt": "1", "job_id": spec.logical_job, "lane": lane,
            "platform": spec.platform, "python": "3.11.14", "profile": spec.profile,
            "selection_profile": spec.selection_profile, "collected": copy.deepcopy(list(rows.values())),
            "selected": sorted(selected), "deselected": sorted(set(rows) - selected),
            "reports": [] if spec.reference else _phase_reports(sorted(selected)),
            "collection_errors": [], "complete": True, "collect_only": spec.reference,
            "exit_code": 0, "wall_seconds": 0.1,
        }
    return result


def _jobs(attempt=1):
    return {name: {"id": 1000 + n, "name": name, "run_id": RUN, "run_attempt": attempt,
                   "head_sha": SHA, "status": "completed", "conclusion": "success"}
            for n, name in enumerate(sorted(producer.REQUIRED_SHARDED_PRODUCT_CI_JOB_NAMES))}


def _write_reports(root: Path, reports):
    for lane, report in reports.items():
        artifact = root / f"ci-tests-{gate.SPECS[lane].job}-{report['source_commit']}-{report['workflow_run_id']}-{report['run_attempt']}"
        artifact.mkdir(parents=True, exist_ok=True)
        (artifact / f"{lane}.json").write_text(json.dumps(report), encoding="utf-8")


def _needs():
    return {name: {"result": "success"} for name in gate.NEEDED_JOBS}


def _replace_selection(report, selected):
    report["selected"] = sorted(selected)
    report["deselected"] = sorted(set(gate.inventory(report)) - set(selected))
    report["reports"] = _phase_reports(sorted(selected))


def _skip(report, node, reason="unavailable optional dependency"):
    for phase in report["reports"]:
        if phase["nodeid"] == node and phase["when"] == "call":
            phase.update(outcome="skipped", detail=reason, skip_reason=reason)


def test_complete_required_graph_and_independent_coverage_pass(tmp_path):
    reports, jobs = _reports(), _jobs()
    _write_reports(tmp_path, reports)
    gate.validate_needs(_needs(), jobs)
    selected = gate.load_reports(tmp_path, source_commit=SHA, run_id=RUN, jobs=jobs)
    assert gate.validate_coverage(selected) == {
        "legacy_linux": 7, "legacy_windows": 8, "required_windows": 3, "scale": 4, "reports": 24,
    }


@pytest.mark.parametrize("outcome", ["failure", "cancelled", "skipped", "timed_out", None])
def test_skipped_or_failed_dependency_cannot_be_hidden_by_successful_reports(outcome):
    needs = _needs()
    needs["backend-fast"]["result"] = outcome
    with pytest.raises(gate.GateError, match="dependency"):
        gate.validate_needs(needs, _jobs())


def test_missing_dependency_and_newer_api_failure_are_rejected():
    needs = _needs()
    del needs["analytics-integration"]
    with pytest.raises(gate.GateError, match="dependencies"):
        gate.validate_needs(needs, _jobs())
    jobs = _jobs()
    jobs["backend-fast"]["conclusion"] = "failure"
    with pytest.raises(gate.GateError, match="latest mandatory"):
        gate.validate_needs(_needs(), jobs)


def test_a_missing_or_duplicated_integration_owner_fails():
    reports = _reports()
    node = reports["analytics-integration-2"]["selected"][0]
    _replace_selection(reports["analytics-integration-1"], reports["analytics-integration-1"]["selected"] + [node])
    with pytest.raises(gate.GateError, match="exactly one owner"):
        gate.validate_coverage(reports)
    reports = _reports()
    _replace_selection(reports["analytics-integration-2"], [])
    with pytest.raises(gate.GateError, match="exactly one owner"):
        gate.validate_coverage(reports)


def test_changing_reference_selection_cannot_hide_lost_legacy_coverage():
    reports = _reports()
    reports["legacy-linux-reference"]["selected"].pop()
    with pytest.raises(gate.GateError, match="independent Linux legacy reference"):
        gate.validate_coverage(reports)


def test_linux_execution_does_not_replace_required_windows_execution():
    reports = _reports()
    node = reports["analytics-windows-contract"]["selected"][0]
    _skip(reports["analytics-windows-contract"], node)
    with pytest.raises(gate.GateError, match="required OS"):
        gate.validate_coverage(reports)


def test_only_independently_corroborated_same_reason_skips_survive():
    reports = _reports()
    node = reports["analytics-windows-contract"]["selected"][0]
    _skip(reports["analytics-windows-contract"], node)
    _skip(reports["windows-full-regression"], node)
    gate.validate_coverage(reports)
    _skip(reports["analytics-windows-contract"], node, "new missing bootstrap")
    with pytest.raises(gate.GateError, match="required OS"):
        gate.validate_coverage(reports)


def test_same_skip_reason_with_different_platform_paths_is_preserved():
    reports = _reports()
    node = "tests/test_unit.py::test_fast"
    _skip(reports["backend-fast"], node, "Skipped: upstream approval is not configured")
    _skip(reports["windows-full-regression"], node, "Skipped: upstream approval is not configured")
    for lane, detail in (
        ("backend-fast", "('/home/runner/work/product/tests/test_unit.py', 12, 'Skipped: upstream approval is not configured')"),
        ("windows-full-regression", "('D:\\a\\product\\tests\\test_unit.py', 14, 'Skipped: upstream approval is not configured')"),
    ):
        next(phase for phase in reports[lane]["reports"]
             if phase["nodeid"] == node and phase["when"] == "call")["detail"] = detail
    gate.validate_coverage(reports)
    _skip(reports["backend-fast"], node, "Skipped: new missing bootstrap dependency")
    with pytest.raises(gate.GateError, match="required OS"):
        gate.validate_coverage(reports)


def test_skips_without_a_structured_reason_are_rejected():
    report = _reports()["analytics-windows-contract"]
    _skip(report, report["selected"][0])
    next(phase for phase in report["reports"] if phase["when"] == "call").pop("skip_reason")
    with pytest.raises(gate.GateError, match="structured skip reason"):
        gate.validate_report(report, gate.SPECS[report["lane"]])


def test_expected_xfail_is_not_a_lost_execution():
    reports = _reports()
    report = reports["analytics-windows-contract"]
    node = report["selected"][0]
    _skip(report, node, "expected failure")
    next(phase for phase in report["reports"] if phase["when"] == "call")["wasxfail"] = "existing tracked expectation"
    gate.validate_coverage(reports)


@pytest.mark.parametrize("field,value", [("complete", False), ("exit_code", 5), ("collect_only", True),
                                         ("platform", "Linux"), ("profile", "wrong"),
                                         ("selection_profile", "wrong"), ("python", "3.12.0")])
def test_missing_execution_or_wrong_platform_profile_fails(field, value):
    report = _reports()["windows-persistence-general"]
    report[field] = value
    with pytest.raises(gate.GateError):
        gate.validate_report(report, gate.SPECS[report["lane"]])


def test_missing_teardown_and_phase_failures_fail_even_with_zero_exit_code():
    report = _reports()["analytics-windows-contract"]
    report["reports"].pop()
    with pytest.raises(gate.GateError, match="incomplete"):
        gate.validate_report(report, gate.SPECS[report["lane"]])
    report = _reports()["analytics-windows-contract"]
    report["reports"][0]["outcome"] = "failed"
    with pytest.raises(gate.GateError, match="failed"):
        gate.validate_report(report, gate.SPECS[report["lane"]])


def test_explicit_profile_selection_and_skip_are_mandatory():
    reports = _reports()
    lane = "backend-fast-brain-general"
    _replace_selection(reports[lane], ["tests/test_unit.py::test_fast"])
    with pytest.raises(gate.GateError, match="selection parity"):
        gate.validate_coverage(reports)
    reports = _reports()
    _skip(reports[lane], reports[lane]["selected"][0])
    with pytest.raises(gate.GateError, match="explicit profile test was skipped"):
        gate.validate_coverage(reports)


def test_retained_successful_dependencies_are_valid_on_failed_jobs_rerun(tmp_path):
    jobs, old = _jobs(), _reports()
    _write_reports(tmp_path, old)
    newer = copy.deepcopy(old["analytics-integration-2"])
    newer["run_attempt"] = "2"
    jobs["analytics-integration-2"].update(run_attempt=2, id=2000)
    _write_reports(tmp_path, {"analytics-integration-2": newer})
    reports = gate.load_reports(tmp_path, source_commit=SHA, run_id=RUN, jobs=jobs)
    assert reports["analytics-integration-2"]["run_attempt"] == "2"
    assert reports["backend-fast"]["run_attempt"] == "1"
    gate.validate_coverage(reports)


def test_older_success_cannot_supply_missing_latest_execution_artifact(tmp_path):
    jobs = _jobs()
    _write_reports(tmp_path, _reports())
    jobs["analytics-integration-2"]["run_attempt"] = 2
    with pytest.raises(gate.GateError, match="missing evidence for latest"):
        gate.load_reports(tmp_path, source_commit=SHA, run_id=RUN, jobs=jobs)


@pytest.mark.parametrize("field,value", [("source_commit", "b" * 40), ("workflow_run_id", "82"),
                                         ("job_id", "backend-fast")])
def test_evidence_identity_mismatch_is_rejected(tmp_path, field, value):
    reports = _reports()
    reports["analytics-integration-1"][field] = value
    _write_reports(tmp_path, reports)
    with pytest.raises(gate.GateError, match="identity mismatch"):
        gate.load_reports(tmp_path, source_commit=SHA, run_id=RUN, jobs=_jobs())


def test_artifact_attempt_disagreement_and_duplicate_reports_fail(tmp_path):
    reports = _reports()
    _write_reports(tmp_path, reports)
    path = next(tmp_path.rglob("backend-fast.json"))
    payload = json.loads(path.read_text())
    payload["run_attempt"] = "2"
    path.write_text(json.dumps(payload))
    with pytest.raises(gate.GateError, match="artifact and report"):
        gate.load_reports(tmp_path, source_commit=SHA, run_id=RUN, jobs=_jobs())
    path.write_text(json.dumps(reports["backend-fast"]))
    path.with_name("duplicate.json").write_text(path.read_text())
    with pytest.raises(gate.GateError, match="duplicate report"):
        gate.load_reports(tmp_path, source_commit=SHA, run_id=RUN, jobs=_jobs())


def test_malformed_or_duplicate_json_evidence_fails(tmp_path):
    _write_reports(tmp_path, _reports())
    path = next(tmp_path.rglob("backend-fast.json"))
    path.write_text('{"schema":"ci-test-report/v1","schema":"ci-test-report/v1"}')
    with pytest.raises(gate.GateError, match="malformed evidence"):
        gate.load_reports(tmp_path, source_commit=SHA, run_id=RUN, jobs=_jobs())


class _JobsApi:
    def __init__(self, jobs):
        self.jobs = jobs

    def get(self, path):
        assert path.endswith("?filter=all&per_page=100&page=1")
        return {"total_count": len(self.jobs), "jobs": self.jobs}


def test_api_chooses_latest_execution_per_name_not_latest_success():
    jobs = list(_jobs().values())
    newer = dict(jobs[0], id=9999, run_attempt=2, conclusion="failure")
    result = producer.latest_ci_jobs(_JobsApi(jobs + [newer]), run_id=RUN, run_attempt=2, source_commit=SHA)
    assert result[newer["name"]]["conclusion"] == "failure"
    assert len(result) == len(jobs)


@pytest.mark.parametrize("field,value", [("head_sha", "b" * 40), ("run_id", 99),
                                         ("run_attempt", 3), ("id", None)])
def test_api_rejects_mismatched_job_coordinates(field, value):
    jobs = list(_jobs().values())
    jobs[0][field] = value
    with pytest.raises(producer.ContractError, match="identity, attempt, or source"):
        producer.latest_ci_jobs(_JobsApi(jobs), run_id=RUN, run_attempt=2, source_commit=SHA)


def test_api_rejects_duplicate_execution_names_in_one_attempt():
    jobs = list(_jobs().values())
    jobs.append(dict(jobs[0], id=9999))
    with pytest.raises(producer.ContractError, match="duplicate job execution"):
        producer.latest_ci_jobs(_JobsApi(jobs), run_id=RUN, run_attempt=1, source_commit=SHA)


def test_summary_links_job_failures_and_does_not_override_results(tmp_path):
    jobs = _jobs()
    url = f"https://github.com/ramiradwan/onlyfans-conversational-analytics/actions/runs/{RUN}/job/999"
    jobs["backend-fast"].update(conclusion="failure", html_url=url)
    output = tmp_path / "summary.md"
    gate.write_summary(str(output), jobs=jobs, reports={}, message="Required CI failed: backend-fast failed")
    text = output.read_text(encoding="utf-8")
    assert f"[backend-fast]({url}) | failure" in text
    assert "Required CI failed: backend-fast failed" in text
    # This path is a directory: a failed optional write must not raise.
    gate.write_summary(str(tmp_path), jobs=jobs, reports={}, message="Required CI failed")


def test_pr_permits_only_the_explicit_scale_skip_and_no_scale_artifact(tmp_path):
    jobs, needs, reports = _jobs(), _needs(), _reports()
    jobs[gate.SCALE_LANE]["conclusion"] = "skipped"
    needs[gate.SCALE_LANE]["result"] = "skipped"
    scale_report = reports.pop(gate.SCALE_LANE)
    _write_reports(tmp_path, reports)
    gate.validate_needs(needs, jobs, event="pull_request")
    selected = gate.load_reports(tmp_path, source_commit=SHA, run_id=RUN, jobs=jobs, event="pull_request")
    assert gate.validate_coverage(selected, event="pull_request")["scale"] == 0
    assert len(selected) == 23
    _write_reports(tmp_path, {gate.SCALE_LANE: scale_report})
    with pytest.raises(gate.GateError, match="unexpected report lane"):
        gate.load_reports(tmp_path, source_commit=SHA, run_id=RUN, jobs=jobs, event="pull_request")


@pytest.mark.parametrize("event", ["push", "schedule", "workflow_dispatch"])
def test_non_pr_events_require_scale_success_and_complete_evidence(tmp_path, event):
    jobs, needs, reports = _jobs(), _needs(), _reports()
    _write_reports(tmp_path, reports)
    gate.validate_needs(needs, jobs, event=event)
    selected = gate.load_reports(tmp_path, source_commit=SHA, run_id=RUN, jobs=jobs, event=event)
    assert gate.validate_coverage(selected, event=event)["scale"] == 4
    needs[gate.SCALE_LANE]["result"] = "skipped"
    with pytest.raises(gate.GateError, match="mandatory dependency"):
        gate.validate_needs(needs, jobs, event=event)
    needs[gate.SCALE_LANE]["result"] = "success"
    jobs[gate.SCALE_LANE]["conclusion"] = "skipped"
    with pytest.raises(gate.GateError, match="latest mandatory job"):
        gate.validate_needs(needs, jobs, event=event)


def test_main_scale_refuses_missing_cases_extra_cases_and_skips():
    reports = _reports()
    report = reports[gate.SCALE_LANE]
    _replace_selection(report, report["selected"][:-1])
    with pytest.raises(gate.GateError, match="analytics scale qualification: selection parity"):
        gate.validate_coverage(reports)
    reports = _reports()
    report = reports[gate.SCALE_LANE]
    _replace_selection(report, report["selected"] + ["tests/test_packaged_runtime.py::test_large"])
    with pytest.raises(gate.GateError, match="analytics scale qualification: selection parity"):
        gate.validate_coverage(reports)
    reports = _reports()
    report = reports[gate.SCALE_LANE]
    _skip(report, report["selected"][0])
    with pytest.raises(gate.GateError, match="scale qualification skipped"):
        gate.validate_coverage(reports)


def test_main_scale_refuses_missing_or_wrong_platform_report(tmp_path):
    reports = _reports()
    reports.pop(gate.SCALE_LANE)
    _write_reports(tmp_path, reports)
    with pytest.raises(gate.GateError, match="missing evidence for latest"):
        gate.load_reports(tmp_path, source_commit=SHA, run_id=RUN, jobs=_jobs())
    report = _reports()[gate.SCALE_LANE]
    report["platform"] = "Linux"
    with pytest.raises(gate.GateError, match="platform"):
        gate.validate_report(report, gate.SPECS[gate.SCALE_LANE])


def test_new_slow_module_automatically_requires_main_qualification():
    reports = _reports()
    node = "tests/test_future_analytics_family.py::test_large_graph"
    for report in reports.values():
        report["collected"].append({"nodeid": node, "markers": ["slow"],
                                    "windows_compat": False, "windows_contract": None})
        report["deselected"].append(node)
    with pytest.raises(gate.GateError, match="analytics scale qualification: selection parity"):
        gate.validate_coverage(reports)
    _replace_selection(reports[gate.SCALE_LANE], reports[gate.SCALE_LANE]["selected"] + [node])
    assert gate.validate_coverage(reports)["scale"] == 5


def test_wrong_event_or_unexpected_scale_success_on_pr_fails_closed():
    with pytest.raises(gate.GateError, match="unsupported CI event"):
        gate.validate_needs(_needs(), _jobs(), event="pull_request_target")
    with pytest.raises(gate.GateError, match="required result 'skipped'"):
        gate.validate_needs(_needs(), _jobs(), event="pull_request")
