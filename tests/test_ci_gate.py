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
WINDOWS_FILES = {
    "tests/test_unit.py": 1,
    "tests/test_platform.py": 1,
    "tests/test_contract_snapshot.py": 1,
    "tests/test_projection1.py": 1,
    "tests/test_projection2.py": 2,
    "tests/test_projection3.py": 2,
    "tests/test_projection4.py": 2,
    "tests/test_boot.py": 2,
}


@pytest.fixture(autouse=True)
def windows_manifest(monkeypatch):
    manifest = {"schema_version": 1, "files": copy.deepcopy(WINDOWS_FILES)}
    monkeypatch.setattr(gate, "load_windows_full_manifest", lambda root: manifest)
    monkeypatch.setattr(gate, "load_backend_manifest", lambda root: {"windows_contracts": {
        "platform": [{"selector": node} for node in (
            "tests/test_platform.py::test_fs", "tests/test_boot.py::test_boot",
            "tests/test_packaged_runtime.py", "tests/test_packaging_smoke.py",
        )],
        "analytics": [{"selector": node} for node in (
            "tests/test_projection1.py::test_store", "tests/test_analytics_closure_process.py",
        )],
    }})
    return manifest


def _phase_reports(selected):
    return [{"nodeid": node, "when": when, "outcome": "passed", "duration": 0.01,
             "wasxfail": None, "detail": ""}
            for node in selected for when in ("setup", "call", "teardown")]


def _reports():
    rows = {}

    def row(node, markers=(), windows=False, contract=None):
        rows[node] = {"nodeid": node, "path": node.split("::", 1)[0], "markers": list(markers),
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
        elif lane in gate.WINDOWS_FULL_LANES:
            selected = {node for node in windows if WINDOWS_FILES[node.split("::", 1)[0]] == int(lane[-1])}
        elif lane == "legacy-windows-reference":
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
            for n, name in enumerate(sorted(gate.ACTUAL_JOBS))}


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


def _windows_owner(reports, node):
    return next(lane for lane in gate.WINDOWS_FULL_LANES if node in reports[lane]["selected"])


def test_complete_required_graph_and_independent_coverage_pass(tmp_path):
    reports, jobs = _reports(), _jobs()
    _write_reports(tmp_path, reports)
    gate.validate_needs(_needs(), jobs)
    selected = gate.load_reports(tmp_path, source_commit=SHA, run_id=RUN, jobs=jobs)
    assert gate.validate_coverage(selected) == {
        "legacy_linux": 7, "legacy_windows": 8, "required_windows": 3, "scale": 4, "reports": 25,
    }


@pytest.mark.parametrize("job", ["backend-fast", "windows-full-shards", "windows-full-regression"])
@pytest.mark.parametrize("outcome", ["failure", "cancelled", "skipped", "timed_out", None])
def test_skipped_or_failed_dependency_cannot_be_hidden_by_successful_reports(job, outcome):
    needs = _needs()
    needs[job]["result"] = outcome
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


@pytest.mark.parametrize("fault", ["overlap", "missing", "extra"])
def test_windows_shard_union_must_equal_raw_legacy_selection(fault):
    reports = _reports()
    first, second = gate.WINDOWS_FULL_LANES
    node = reports[first]["selected"][0]
    if fault == "overlap":
        _replace_selection(reports[second], reports[second]["selected"] + [node])
    elif fault == "missing":
        _replace_selection(reports[first], reports[first]["selected"][1:])
    else:
        _replace_selection(reports[first], reports[first]["selected"] + ["tests/test_scale_family1.py::test_large"])
    with pytest.raises(gate.GateError, match="exactly one owner|union must equal"):
        gate.validate_coverage(reports)


def test_windows_shards_cannot_split_one_file_even_with_exact_node_union():
    reports = _reports()
    node = "tests/test_unit.py::test_second"
    for report in reports.values():
        report["collected"].append({"nodeid": node, "path": "tests/test_unit.py", "markers": []})
        report["deselected"].append(node)
    for lane in ("legacy-linux-reference", "legacy-windows-reference", "backend-fast", "windows-full-regression-2"):
        _replace_selection(reports[lane], reports[lane]["selected"] + [node])
        if gate.SPECS[lane].reference:
            reports[lane]["reports"] = []
    with pytest.raises(gate.GateError, match="every test file in exactly one shard"):
        gate.validate_coverage(reports)


@pytest.mark.parametrize("fault", ["missing-file", "extra-file", "wrong-owner"])
def test_windows_shards_must_match_checked_in_file_manifest(windows_manifest, fault):
    reports = _reports()
    if fault == "missing-file":
        windows_manifest["files"].pop("tests/test_unit.py")
    elif fault == "extra-file":
        windows_manifest["files"]["tests/test_nonexistent.py"] = 1
    else:
        windows_manifest["files"]["tests/test_unit.py"] = 2
    with pytest.raises(gate.GateError, match="manifest"):
        gate.validate_coverage(reports)


def test_windows_manifest_load_failure_is_a_gate_failure(monkeypatch):
    def invalid_manifest(root):
        raise ValueError("invalid manifest schema")

    monkeypatch.setattr(gate, "load_windows_full_manifest", invalid_manifest)
    with pytest.raises(gate.GateError, match="invalid Windows full regression manifest"):
        gate.validate_coverage(_reports())


@pytest.mark.parametrize("lane", gate.WINDOWS_FULL_LANES)
@pytest.mark.parametrize("fault", ["narrowed", "markers", "path"])
def test_every_windows_shard_must_collect_the_complete_raw_reference(lane, fault):
    reports = _reports()
    report = reports[lane]
    # Remove an excluded slow node: selected coverage alone would still match.
    node = "tests/test_scale_family1.py::test_large"
    if fault == "narrowed":
        report["collected"] = [row for row in report["collected"] if row["nodeid"] != node]
        report["deselected"].remove(node)
    else:
        row = next(row for row in report["collected"] if row["nodeid"] == node)
        row[fault] = [] if fault == "markers" else "tests/test_forged.py"
    with pytest.raises(gate.GateError, match="independent Windows reference|inconsistent file path"):
        gate.validate_coverage(reports)


def test_raw_windows_coverage_does_not_depend_on_classifier_fields():
    reports = _reports()
    for lane in gate.WINDOWS_FULL_LANES + ("legacy-windows-reference",):
        for row in reports[lane]["collected"]:
            row.update(tier="scale", legacy_windows=False, windows_contract=None, shard=99)
    gate.validate_coverage(reports)
    # A changed reference selection cannot redefine the old marker expression.
    reports["legacy-windows-reference"]["selected"].pop()
    with pytest.raises(gate.GateError, match="independent Windows legacy reference"):
        gate.validate_coverage(reports)


@pytest.mark.parametrize("name", ["windows-full-regression", *gate.WINDOWS_FULL_LANES])
@pytest.mark.parametrize("fault", ["missing", "failure"])
def test_each_windows_shard_and_stable_aggregate_must_succeed(name, fault):
    jobs = _jobs()
    if fault == "missing":
        jobs.pop(name)
    else:
        jobs[name]["conclusion"] = "failure"
    with pytest.raises(gate.GateError, match="latest mandatory job"):
        gate.validate_needs(_needs(), jobs)


@pytest.mark.parametrize("name", ["windows-full-shards", "windows-full-regression"])
def test_gate_dependencies_must_include_matrix_and_stable_aggregate(name):
    needs = _needs()
    needs.pop(name)
    with pytest.raises(gate.GateError, match="dependencies"):
        gate.validate_needs(needs, _jobs())


@pytest.mark.parametrize("lane", gate.WINDOWS_FULL_LANES)
def test_successful_aggregate_cannot_replace_missing_shard_evidence(tmp_path, lane):
    reports = _reports()
    reports.pop(lane)
    _write_reports(tmp_path, reports)
    gate.validate_needs(_needs(), _jobs())
    with pytest.raises(gate.GateError, match="missing evidence for latest"):
        gate.load_reports(tmp_path, source_commit=SHA, run_id=RUN, jobs=_jobs())


@pytest.mark.parametrize("fault", ["incomplete", "wrong-os", "missing-teardown", "failed-call", "duplicate-call",
                                    "failed-subtest", "skipped-subtest"])
def test_windows_shard_outcomes_cannot_hide_behind_successful_aggregate(fault):
    reports = _reports()
    report = reports["windows-full-regression-2"]
    call = next(phase for phase in report["reports"] if phase["when"] == "call")
    if fault == "incomplete":
        report["complete"] = False
    elif fault == "wrong-os":
        report["platform"] = "Linux"
    elif fault == "missing-teardown":
        report["reports"].pop()
    elif fault == "failed-call":
        call["outcome"] = "failed"
    elif fault == "duplicate-call":
        report["reports"].append(copy.deepcopy(call))
    else:
        report["reports"].append(dict(call, outcome=fault.split("-", 1)[0], skip_reason="child unavailable",
                                      subtest={"index": 1, "msg": None, "kwargs": {}}))
    with pytest.raises(gate.GateError, match="incomplete|platform|failed|duplicate pytest phase|subtest did not pass"):
        gate.validate_coverage(reports)


def test_each_skip_requires_corroboration_from_the_actual_owning_windows_shard():
    reports = _reports()
    node = "tests/test_projection2.py::test_store"
    _skip(reports["analytics-integration-2"], node, "existing optional dependency")
    with pytest.raises(gate.GateError, match="required OS"):
        gate.validate_coverage(reports)
    _skip(reports["windows-full-regression-2"], node, "existing optional dependency")
    gate.validate_coverage(reports)
    _skip(reports["windows-full-regression-2"], node, "new missing bootstrap")
    with pytest.raises(gate.GateError, match="required OS"):
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
    _skip(reports[_windows_owner(reports, node)], node)
    gate.validate_coverage(reports)
    _skip(reports["analytics-windows-contract"], node, "new missing bootstrap")
    with pytest.raises(gate.GateError, match="required OS"):
        gate.validate_coverage(reports)


def test_same_skip_reason_with_different_platform_paths_is_preserved():
    reports = _reports()
    node = "tests/test_unit.py::test_fast"
    _skip(reports["backend-fast"], node, "Skipped: upstream approval is not configured")
    owner = _windows_owner(reports, node)
    _skip(reports[owner], node, "Skipped: upstream approval is not configured")
    for lane, detail in (
        ("backend-fast", "('/home/runner/work/product/tests/test_unit.py', 12, 'Skipped: upstream approval is not configured')"),
        (owner, "('D:\\a\\product\\tests\\test_unit.py', 14, 'Skipped: upstream approval is not configured')"),
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


def _with_subtests():
    report = _reports()["analytics-windows-contract"]
    node = report["selected"][0]
    children = [dict(report["reports"][1], subtest={
        "index": index, "msg": "same context", "kwargs": {"value": "'same'"},
    }) for index in (1, 2)]
    report["reports"][1:1] = children
    return report, children


def test_explicit_subtests_allow_repeated_context_but_keep_one_parent_call():
    report, _ = _with_subtests()
    assert gate.validate_report(report, gate.SPECS[report["lane"]]) == {
        report["selected"][0]: "executed",
    }
    report["reports"].append(copy.deepcopy(report["reports"][-2]))
    with pytest.raises(gate.GateError, match="duplicate pytest phase"):
        gate.validate_report(report, gate.SPECS[report["lane"]])


@pytest.mark.parametrize("child", [
    None,
    {"index": 0, "msg": None, "kwargs": {}},
    {"index": 2, "msg": None, "kwargs": {}},
    {"index": True, "msg": None, "kwargs": {}},
    {"index": 1, "msg": [], "kwargs": {}},
    {"index": 1, "msg": None, "kwargs": []},
    {"index": 1, "msg": None, "kwargs": {"value": 123}},
    {"index": 1, "msg": None, "kwargs": {}, "extra": "unexpected"},
])
def test_malformed_or_missing_subtest_identity_is_rejected(child):
    report, children = _with_subtests()
    children[0]["subtest"] = child
    with pytest.raises(gate.GateError, match="subtest identity"):
        gate.validate_report(report, gate.SPECS[report["lane"]])


def test_duplicated_subtest_identity_is_rejected():
    report, children = _with_subtests()
    children[1]["subtest"]["index"] = 1
    with pytest.raises(gate.GateError, match="duplicate subtest identity"):
        gate.validate_report(report, gate.SPECS[report["lane"]])


@pytest.mark.parametrize("outcome", ["failed", "skipped"])
def test_failed_or_skipped_subtest_cannot_hide_behind_successful_parent(outcome):
    report, children = _with_subtests()
    children[0].update(outcome=outcome, skip_reason="child unavailable")
    with pytest.raises(gate.GateError, match="failed|subtest did not pass"):
        gate.validate_report(report, gate.SPECS[report["lane"]])


def test_subtest_must_be_a_call_and_cannot_supply_the_parent_call():
    report, children = _with_subtests()
    children[0]["when"] = "setup"
    with pytest.raises(gate.GateError, match="subtest did not pass its call"):
        gate.validate_report(report, gate.SPECS[report["lane"]])
    report, _ = _with_subtests()
    report["reports"] = [phase for phase in report["reports"]
                         if phase["when"] != "call" or "subtest" in phase]
    with pytest.raises(gate.GateError, match="without a call outcome"):
        gate.validate_report(report, gate.SPECS[report["lane"]])


def test_subtests_cannot_make_a_skipped_parent_look_executed():
    report, _ = _with_subtests()
    _skip(report, report["selected"][0], "parent unavailable")
    for phase in report["reports"]:
        if "subtest" in phase:
            phase["outcome"] = "passed"
    with pytest.raises(gate.GateError, match="no successful parent call"):
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


@pytest.mark.parametrize("lane", ["analytics-integration-2", "windows-full-regression-2"])
def test_retained_successful_dependencies_are_valid_on_failed_jobs_rerun(tmp_path, lane):
    jobs, old = _jobs(), _reports()
    _write_reports(tmp_path, old)
    newer = copy.deepcopy(old[lane])
    newer["run_attempt"] = "2"
    jobs[lane].update(run_attempt=2, id=2000)
    _write_reports(tmp_path, {lane: newer})
    reports = gate.load_reports(tmp_path, source_commit=SHA, run_id=RUN, jobs=jobs)
    assert reports[lane]["run_attempt"] == "2"
    assert reports["backend-fast"]["run_attempt"] == "1"
    assert reports["windows-full-regression-1"]["run_attempt"] == "1"
    gate.validate_coverage(reports)


@pytest.mark.parametrize("lane", ["analytics-integration-2", *gate.WINDOWS_FULL_LANES])
def test_older_success_cannot_supply_missing_latest_execution_artifact(tmp_path, lane):
    jobs = _jobs()
    _write_reports(tmp_path, _reports())
    jobs[lane]["run_attempt"] = 2
    with pytest.raises(gate.GateError, match="missing evidence for latest"):
        gate.load_reports(tmp_path, source_commit=SHA, run_id=RUN, jobs=jobs)


@pytest.mark.parametrize("field,value", [("source_commit", "b" * 40), ("workflow_run_id", "82"),
                                         ("job_id", "backend-fast")])
@pytest.mark.parametrize("lane", ["analytics-integration-1", *gate.WINDOWS_FULL_LANES])
def test_evidence_identity_mismatch_is_rejected(tmp_path, field, value, lane):
    reports = _reports()
    reports[lane][field] = value
    _write_reports(tmp_path, reports)
    with pytest.raises(gate.GateError, match="identity mismatch"):
        gate.load_reports(tmp_path, source_commit=SHA, run_id=RUN, jobs=_jobs())


@pytest.mark.parametrize("lane", ["backend-fast", *gate.WINDOWS_FULL_LANES])
def test_artifact_attempt_disagreement_and_duplicate_reports_fail(tmp_path, lane):
    reports = _reports()
    _write_reports(tmp_path, reports)
    path = next(tmp_path.rglob(f"{lane}.json"))
    payload = json.loads(path.read_text())
    payload["run_attempt"] = "2"
    path.write_text(json.dumps(payload))
    with pytest.raises(gate.GateError, match="artifact and report"):
        gate.load_reports(tmp_path, source_commit=SHA, run_id=RUN, jobs=_jobs())
    path.write_text(json.dumps(reports[lane]))
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


def _pr_inputs():
    jobs, needs, reports = _jobs(), _needs(), _reports()
    jobs[gate.SCALE_LANE]["conclusion"] = "skipped"
    needs[gate.SCALE_LANE]["result"] = "skipped"
    needs["windows-full-shards"]["result"] = "skipped"
    for lane in gate.WINDOWS_FULL_LANES:
        jobs.pop(lane)
        reports.pop(lane)
    reports.pop(gate.SCALE_LANE)
    return jobs, needs, reports


@pytest.mark.parametrize("unexpected_lane", [gate.SCALE_LANE, *gate.WINDOWS_FULL_LANES])
def test_pr_requires_expected_producer_skips_and_no_exhaustive_or_scale_artifact(tmp_path, unexpected_lane):
    jobs, needs, reports = _pr_inputs()
    _write_reports(tmp_path, reports)
    gate.validate_needs(needs, jobs, event="pull_request")
    selected = gate.load_reports(tmp_path, source_commit=SHA, run_id=RUN, jobs=jobs, event="pull_request")
    assert gate.validate_coverage(selected, event="pull_request")["scale"] == 0
    assert len(selected) == 22
    _write_reports(tmp_path, {unexpected_lane: _reports()[unexpected_lane]})
    with pytest.raises(gate.GateError, match="unexpected report lane"):
        gate.load_reports(tmp_path, source_commit=SHA, run_id=RUN, jobs=jobs, event="pull_request")


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
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


@pytest.mark.parametrize("event", ["schedule", "pull_request_target", "", "release"])
def test_unsupported_ci_events_cannot_authorize_producer_skips(event):
    with pytest.raises(gate.GateError, match="unsupported CI event"):
        gate.required_report_lanes(event)
    with pytest.raises(gate.GateError, match="unsupported CI event"):
        gate.validate_needs(_needs(), _jobs(), event=event)


@pytest.mark.parametrize("result", ["success", "failure", "cancelled", "timed_out", None])
def test_pr_cannot_accept_an_unexpected_exhaustive_matrix_result(result):
    jobs, needs, _ = _pr_inputs()
    needs["windows-full-shards"]["result"] = result
    with pytest.raises(gate.GateError, match="windows-full-shards"):
        gate.validate_needs(needs, jobs, event="pull_request")


@pytest.mark.parametrize("job", ["windows-platform-contract", "analytics-windows-contract", "windows-browser-e2e",
                                 "windows-full-regression"])
@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", None])
def test_pr_cutover_keeps_focused_contracts_browser_and_stable_aggregate_blocking(job, result):
    jobs, needs, _ = _pr_inputs()
    jobs[job]["conclusion"] = result
    with pytest.raises(gate.GateError, match="latest mandatory job"):
        gate.validate_needs(needs, jobs, event="pull_request")


@pytest.mark.parametrize("name", [*gate.WINDOWS_FULL_LANES, "windows-full-regression-"])
@pytest.mark.parametrize("result", ["success", "failure", "cancelled", "timed_out", None])
def test_pr_refuses_any_unexpected_actual_exhaustive_execution(name, result):
    jobs, needs, _ = _pr_inputs()
    jobs[name] = {"status": "completed", "conclusion": result}
    with pytest.raises(gate.GateError, match="latest mandatory job|exhaustive Windows execution"):
        gate.validate_needs(needs, jobs, event="pull_request")


def _rename_node(reports, original, renamed):
    for report in reports.values():
        for row in report["collected"]:
            if row["nodeid"] == original:
                row.update(nodeid=renamed, path=renamed.split("::", 1)[0])
        for key in ("selected", "deselected"):
            report[key] = [renamed if node == original else node for node in report[key]]
        for phase in report["reports"]:
            if phase["nodeid"] == original:
                phase["nodeid"] = renamed


@pytest.mark.parametrize("identity,reason", list(gate.PR_SKIP_BASELINE.items()))
def test_pr_accepts_only_reviewed_skip_identity_platform_and_reason(identity, reason):
    node, platform = identity
    _, _, reports = _pr_inputs()
    original = ("tests/test_projection1.py::test_store" if reason == "Skipped: Linux process filesystem observation"
                else "tests/test_platform.py::test_fs" if platform == "Windows" else "tests/test_unit.py::test_fast")
    _rename_node(reports, original, node)
    for lane, report in reports.items():
        if (node in report["selected"] and not gate.SPECS[lane].reference
                and gate.SPECS[lane].platform == platform):
            _skip(report, node, reason)
    gate.validate_coverage(reports, event="pull_request")
    for report in reports.values():
        if node in report["selected"] and report["reports"] and report["platform"] == platform:
            _skip(report, node, reason + " changed")
    with pytest.raises(gate.GateError, match="unreviewed pull-request skip"):
        gate.validate_coverage(reports, event="pull_request")


def test_reviewed_linux_proc_skip_on_windows_still_requires_linux_execution():
    _, _, reports = _pr_inputs()
    node = "tests/test_analytics_closure_process.py::test_proc_observation_only_accepts_disappearance[OSError]"
    _rename_node(reports, "tests/test_projection1.py::test_store", node)
    _skip(reports["analytics-windows-contract"], node, gate.PR_SKIP_BASELINE[(node, "Windows")])
    gate.validate_coverage(reports, event="pull_request")
    _skip(reports["analytics-integration-1"], node, "Skipped: lost Linux observation")
    with pytest.raises(gate.GateError, match="requires actual Linux execution"):
        gate.validate_coverage(reports, event="pull_request")


def test_reviewed_windows_skip_cannot_move_to_another_contract():
    _, _, reports = _pr_inputs()
    node = "tests/test_packaging_smoke.py::test_platform_capability_probe_leaves_no_persisted_key"
    _rename_node(reports, "tests/test_projection1.py::test_store", node)
    _skip(reports["analytics-windows-contract"], node, gate.PR_SKIP_BASELINE[(node, "Windows")])
    with pytest.raises(gate.GateError, match="Windows contract classification differs"):
        gate.validate_coverage(reports, event="pull_request")


@pytest.mark.parametrize("event", ["pull_request", "push", "workflow_dispatch"])
@pytest.mark.parametrize("skip_platform", [False, True])
def test_reviewed_platform_skip_cannot_authorize_skipped_windows_production_boot(event, skip_platform, windows_manifest):
    reports = _pr_inputs()[2] if event == "pull_request" else _reports()
    node = "tests/test_packaging_smoke.py::test_platform_capability_probe_leaves_no_persisted_key"
    _rename_node(reports, "tests/test_boot.py::test_boot", node)
    windows_manifest["files"][node.split("::", 1)[0]] = windows_manifest["files"].pop("tests/test_boot.py")
    # Retain the independent native marker and platform assignment. The old
    # exception must not permit the newly required bootstrap invocation to skip.
    reason = gate.PR_SKIP_BASELINE[(node, "Windows")]
    if skip_platform:
        _skip(reports["windows-platform-contract"], node, reason)
    _skip(reports["windows-production-boot"], node, reason)
    if event != "pull_request":
        for lane in gate.WINDOWS_FULL_LANES:
            if node in reports[lane]["selected"]:
                _skip(reports[lane], node, reason)
    with pytest.raises(gate.GateError, match="Windows production boot requires actual execution"):
        gate.validate_coverage(reports, event=event)


def test_pr_new_default_skip_is_rejected_without_same_run_exhaustive_evidence():
    _, _, reports = _pr_inputs()
    _skip(reports["backend-fast"], "tests/test_unit.py::test_fast")
    with pytest.raises(gate.GateError, match="unreviewed pull-request skip"):
        gate.validate_coverage(reports, event="pull_request")


@pytest.mark.parametrize("lane", ["backend-fast", "analytics-integration-4", "windows-platform-contract", "analytics-windows-contract"])
@pytest.mark.parametrize("fault", ["missing-excluded-node", "markers", "path"])
def test_pr_focused_reports_retain_exact_independent_raw_inventory(lane, fault):
    _, _, reports = _pr_inputs()
    report = reports[lane]
    node = "tests/test_scale_family1.py::test_large"
    if fault == "missing-excluded-node":
        report["collected"] = [row for row in report["collected"] if row["nodeid"] != node]
        report["deselected"].remove(node)
    else:
        row = next(row for row in report["collected"] if row["nodeid"] == node)
        row[fault] = [] if fault == "markers" else "tests/test_forged.py"
    with pytest.raises(gate.GateError, match="collection differs|inconsistent file path"):
        gate.validate_coverage(reports, event="pull_request")


def test_linux_platform_skip_requires_actual_windows_contract_execution():
    _, _, reports = _pr_inputs()
    node = "tests/test_platform.py::test_fs"
    _skip(reports["backend-fast"], node, "Skipped: Windows-only")
    gate.validate_coverage(reports, event="pull_request")
    _skip(reports["windows-platform-contract"], node, "Skipped: Windows-only")
    with pytest.raises(gate.GateError, match="unreviewed pull-request skip"):
        gate.validate_coverage(reports, event="pull_request")


def test_raw_windows_marker_cannot_be_hidden_by_classifier_fields():
    _, _, reports = _pr_inputs()
    node = "tests/test_platform.py::test_fs"
    for report in reports.values():
        row = next(row for row in report["collected"] if row["nodeid"] == node)
        row.update(markers=["windows_compat"], windows_compat=False, windows_contract=None)
    _replace_selection(reports["windows-platform-contract"],
                       [selected for selected in reports["windows-platform-contract"]["selected"] if selected != node])
    with pytest.raises(gate.GateError, match="Windows contract classification differs|raw native Windows marker"):
        gate.validate_coverage(reports, event="pull_request")


@pytest.mark.parametrize("marker", ["windows_compat", "windows_production"])
def test_independent_native_marker_refuses_a_contradictory_derived_flag(marker):
    _, _, reports = _pr_inputs()
    node = "tests/test_platform.py::test_fs"
    for report in reports.values():
        row = next(row for row in report["collected"] if row["nodeid"] == node)
        row.update(markers=[marker], windows_compat=False)
    if marker == "windows_production":
        _replace_selection(reports["legacy-linux-reference"],
                           [selected for selected in reports["legacy-linux-reference"]["selected"] if selected != node])
        reports["legacy-linux-reference"]["reports"] = []
        _replace_selection(reports["backend-fast"],
                           [selected for selected in reports["backend-fast"]["selected"] if selected != node])
        _replace_selection(reports["windows-production-boot"],
                           reports["windows-production-boot"]["selected"] + [node])
    with pytest.raises(gate.GateError, match="raw native Windows marker contradicts classifier flags"):
        gate.validate_coverage(reports, event="pull_request")


def test_focused_windows_collections_cannot_disagree_on_contract_classification():
    _, _, reports = _pr_inputs()
    node = "tests/test_platform.py::test_fs"
    row = next(row for row in reports["analytics-windows-contract"]["collected"] if row["nodeid"] == node)
    row["windows_contract"] = None
    with pytest.raises(gate.GateError, match="Windows contract classification differs"):
        gate.validate_coverage(reports, event="pull_request")


def test_reviewed_default_skip_cannot_authorize_a_skipped_explicit_profile(monkeypatch):
    _, _, reports = _pr_inputs()
    node = reports["backend-fast-brain-general"]["selected"][0]
    reason = "Skipped: reviewed only as a default case"
    monkeypatch.setitem(gate.PR_SKIP_BASELINE, (node, "Linux"), reason)
    _skip(reports["backend-fast-brain-general"], node, reason)
    with pytest.raises(gate.GateError, match="mandatory explicit profile test was skipped"):
        gate.validate_coverage(reports, event="pull_request")


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
        report["collected"].append({"nodeid": node, "path": node.split("::", 1)[0], "markers": ["slow"],
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
