"""Counterexamples for the independent exhaustive Windows file partition."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tools import ci_windows_shards as windows
from tools import test_backend as runner


pytestmark = pytest.mark.ci_tier("fast")
ROOT = Path(__file__).resolve().parents[1]


def row(nodeid, *markers):
    return dict(nodeid=nodeid, path=nodeid.split("::", 1)[0], markers=list(markers))


def manifest():
    return dict(schema_version=1, files={"test_one.py": 1, "test_two.py": 2, "test_three.py": 3})


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def inventory():
    rows = [row("test_one.py::test_plain"), row("test_two.py::test_windows", "windows_production"),
            row("test_three.py::test_serial", "serial"), row("test_one.py::test_large", "slow")]
    selected = sorted(windows.legacy_nodeids(rows))
    return dict(schema="ci-test-report/v1", complete=True, exit_code=0, collect_only=False, partial=False,
                platform="Windows", profile="", selection_profile="", collection_errors=[],
                source_commit="a"*40, workflow_run_id="1", run_attempt="1",
                collected=rows, selected=selected, deselected=["test_one.py::test_large"],
                selection=dict(keyword="", marker="", targets=["tests"]),
                reports=[dict(nodeid=nodeid, when=phase, outcome="passed", duration=seconds)
                         for nodeid in selected for phase, seconds in (("setup", 1.0), ("call", 3.0), ("teardown", .5))])


def test_raw_partition_ignores_classifier_and_preserves_parameter_bytes():
    rows = [row(r"test_one.py::test_bytes[\xff]"), row("test_two.py::test_native", "windows_production"),
            row("test_three.py::test_third"),
            row("test_one.py::test_ignored", "slow"), row("test_two.py::test_stateful", "stateful_tier_b")]
    rows[0].update(tier="invalid", shard=4, windows_contract="platform")
    first, second, third = (windows.selected_nodeids(rows, manifest(), shard) for shard in (1, 2, 3))
    assert first == {r"test_one.py::test_bytes[\xff]"}
    assert second == {"test_two.py::test_native"}
    assert third == {"test_three.py::test_third"}
    assert not first & second and not (first | second) & third
    assert first | second | third == windows.legacy_nodeids(rows)
    with pytest.raises(windows.WindowsShardError, match="1, 2 or 3"):
        windows.selected_nodeids(rows, manifest(), 4)


@pytest.mark.parametrize("problem", ["unknown", "missing", "duplicate"])
def test_full_inventory_refuses_unassigned_missing_and_duplicate_nodes(problem):
    rows = [row("test_one.py::test_a"), row("test_two.py::test_b"), row("test_three.py::test_c")]
    if problem == "unknown":
        rows.append(row("test_new.py::test_c"))
    elif problem == "missing":
        rows.pop()
    else:
        rows.append(rows[0])
    with pytest.raises(windows.WindowsShardError):
        windows.selected_nodeids(rows, manifest(), 1)


def test_targeting_allows_missing_files_but_never_unassigned_ones():
    rows = [row("test_one.py::test_a")]
    assert windows.selected_nodeids(rows, manifest(), 1, complete=False) == {rows[0]["nodeid"]}
    with pytest.raises(windows.WindowsShardError, match="unassigned"):
        windows.selected_nodeids([row("test_new.py::test_b")], manifest(), 1, complete=False)


@pytest.mark.parametrize("files", [{"test_one.py": True, "test_two.py": 2, "test_three.py": 3},
    {"test_one.py": 4, "test_two.py": 2, "test_three.py": 3}, {"test_one.py": 1, "test_two.py": 2},
    {"test_one.py": 3, "test_two.py": 2}, {"test_one.py": 1},
    {"../test_one.py": 1, "test_two.py": 2, "test_three.py": 3},
    {"test_one.py::test_a": 1, "test_two.py": 2, "test_three.py": 3}])
def test_manifest_rejects_invalid_owners_or_paths(tmp_path, files):
    path = write_json(tmp_path/"manifest.json", dict(schema_version=1, files=files))
    with pytest.raises(windows.WindowsShardError):
        windows.load_manifest(tmp_path, path)


def test_manifest_rejects_duplicate_json_keys(tmp_path):
    path = tmp_path/"manifest.json"
    path.write_text('{"schema_version":1,"files":{"test_one.py":1,"test_one.py":2,"test_two.py":2,"test_three.py":3}}')
    with pytest.raises(windows.WindowsShardError, match="Duplicate"):
        windows.load_manifest(tmp_path, path)


def test_update_preserves_owners_and_marks_new_unmeasured_files(tmp_path):
    source = write_json(tmp_path/"timings.json", inventory())
    report = windows.read_inventory(source, execution=True)
    first = windows.updated_manifest(None, report, timing_report=report, rebalance=True)
    assert first["assignment_basis"]["source"]["report_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    enlarged = dict(report, collected=[*report["collected"], row("test_new.py::test_c")])
    after = windows.updated_manifest(first, enlarged)
    assert all(after["files"][path] == owner for path, owner in first["files"].items())
    assert after["assignment_basis"]["unmeasured_files"] == ["test_new.py"]
    assert after["assignment_basis"]["status"] == "measured_with_unmeasured_additions"
    assert windows.updated_manifest(after, enlarged) == after
    with pytest.raises(windows.WindowsShardError, match="every current file"):
        windows.updated_manifest(after, enlarged, rebalance=True)


def shard_reports(tmp_path, change=None, owners=((1, ("test_one.py",)), (2, ("test_two.py",)), (3, ("test_three.py",)))):
    full = inventory()
    reports = {}
    for shard, owned in owners:
        selected = [node for node in full["selected"] if node.startswith(owned)]
        reports[shard] = dict(full, lane=f"windows-full-regression-{shard}", selected=selected,
                              deselected=[r["nodeid"] for r in full["collected"] if r["nodeid"] not in selected],
                              reports=[phase for phase in full["reports"] if phase["nodeid"] in selected])
    if change == "missing": reports.pop(2)
    elif change == "missing-last": reports.pop(3)
    elif change == "overlap": reports[2]["selected"] = full["selected"]
    elif change == "other_run": reports[2]["workflow_run_id"] = "2"
    elif change == "failed": reports[2]["exit_code"] = 1
    elif change == "incomplete": reports[1]["complete"] = False
    for shard, report in reports.items():
        (tmp_path/f"shard-{shard}").mkdir()
        write_json(tmp_path/f"shard-{shard}/report.json", report)
    return full


def test_timing_import_joins_both_exhaustive_shard_reports(tmp_path):
    full = shard_reports(tmp_path)
    combined = windows.read_inventory(tmp_path, execution=True)
    assert sorted(combined["selected"]) == full["selected"] and combined["deselected"] == full["deselected"]
    assert windows.file_timings(combined) == windows.file_timings(full)
    updated = windows.updated_manifest(None, full, timing_report=combined, rebalance=True)
    source = updated["assignment_basis"]["source"]
    assert [entry["lane"] for entry in source["execution_reports"]] == list(windows.SHARD_LANES)
    assert source["report_sha256"] == source["execution_reports"][0]["report_sha256"]
    assert source["report_sha256"] == hashlib.sha256((tmp_path/"shard-1/report.json").read_bytes()).hexdigest()


def test_timing_import_accepts_every_report_of_an_earlier_two_shard_run(tmp_path):
    full = shard_reports(tmp_path, owners=((1, ("test_one.py",)), (2, ("test_two.py", "test_three.py"))))
    combined = windows.read_inventory(tmp_path, execution=True)
    assert windows.file_timings(combined) == windows.file_timings(full)
    assert [entry["lane"] for entry in combined["_execution_reports"]] == list(windows.SHARD_LANES[:2])


@pytest.mark.parametrize("change", ["missing", "missing-last", "overlap", "other_run", "failed", "incomplete"])
def test_timing_import_refuses_partial_or_mismatched_shard_reports(tmp_path, change):
    shard_reports(tmp_path, change)
    with pytest.raises(windows.WindowsShardError):
        windows.read_inventory(tmp_path, execution=True)


@pytest.mark.parametrize("change", ["failed", "collect_only", "keyword", "target", "selection", "deselection", "complete_type", "exit_type"])
def test_timing_import_refuses_incomplete_or_narrowed_evidence(tmp_path, change):
    report = inventory()
    if change == "failed": report["exit_code"] = 1
    elif change == "collect_only": report["collect_only"] = True
    elif change == "keyword": report["selection"]["keyword"] = "specific"
    elif change == "target": report["selection"]["targets"] = ["test_one.py"]
    elif change == "selection": report["selected"].pop()
    elif change == "deselection": report["deselected"].clear()
    elif change == "complete_type": report["complete"] = 1
    else: report["exit_code"] = False
    with pytest.raises(windows.WindowsShardError):
        windows.read_inventory(write_json(tmp_path/"report.json", report), execution=True)


@pytest.mark.parametrize("change", ["missing", "duplicate", "failed", "skipped_teardown", "nan", "failed_child"])
def test_timing_import_checks_complete_phases_and_child_success(change):
    report = inventory()
    if change == "missing": report["reports"].pop()
    elif change == "duplicate": report["reports"].append(report["reports"][0])
    elif change == "failed": report["reports"][1]["outcome"] = "failed"
    elif change == "skipped_teardown": report["reports"][2]["outcome"] = "skipped"
    elif change == "nan": report["reports"][0]["duration"] = float("nan")
    else: report["reports"].append(dict(report["reports"][1], subtest={"index":1}, outcome="failed"))
    with pytest.raises(windows.WindowsShardError):
        windows.file_timings(report)


@pytest.mark.parametrize("fault", ["unknown_node", "wrong_phase", "empty", "duplicate", "orphan_index"])
def test_timing_import_refuses_malformed_successful_child_reports(fault):
    report = inventory()
    child = dict(report["reports"][1], subtest=dict(index=1,msg=None,kwargs={}),duration=0)
    if fault == "unknown_node": child["nodeid"] = "test_absent.py::test_x"
    elif fault == "wrong_phase": child["when"] = "setup"
    elif fault == "empty": child["subtest"] = {}
    elif fault == "duplicate": report["reports"].append(child)
    else: child["subtest"]["index"] = 2
    report["reports"].append(child)
    with pytest.raises(windows.WindowsShardError):
        windows.file_timings(report)


def test_runner_uses_raw_expression_for_listing_and_execution_without_taxonomy(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("Raw Windows execution must not load taxonomy")
    monkeypatch.setattr(runner, "load_manifest", forbidden)
    missing = tmp_path/"missing-taxonomy.json"
    assert runner.main(["windows-full-regression", "--manifest", str(missing), "--dry-run"]) == 0
    args = runner.parser().parse_args(["list", "windows-full-regression", "--shard", "1", "--validate"])
    command, environment = runner.build_command(args, [], {})
    assert "--ci-lane" not in command and "--ci-manifest" not in command
    assert command[command.index("-m", 3)+1] == windows.LEGACY_WINDOWS_EXPRESSION
    assert command[command.index("--ci-windows-shard")+1] == "1"
    assert "--collect-only" in command and "--ci-validate" in command
    monkeypatch.delenv("CI_TEST_LANE", raising=False)
    _, environment = runner.build_command(args, [], {})
    assert environment["CI_TEST_LANE"] == "windows-full-regression-1"
    args = runner.parser().parse_args(["windows-full-regression", "--shard", "3"])
    command, _ = runner.build_command(args, [], {})
    assert command[command.index("--ci-windows-shard")+1] == "3"
    args = runner.parser().parse_args(["windows-full-regression", "--shard", "4"])
    with pytest.raises(runner.SelectionError, match="exactly three"):
        runner.build_command(args, [], {})


def plugin_suite(tmp_path, shard, *extra):
    (tmp_path/"pytest.ini").write_text("[pytest]\nmarkers =\n slow: old exclusion\n windows_production: original Windows obligation\n ci_tier(value): deliberately irrelevant\n", encoding="utf-8")
    (tmp_path/"test_one.py").write_text("import pytest\ndef test_plain(): pass\n@pytest.mark.slow\ndef test_large(): assert False\n",encoding="utf-8")
    (tmp_path/"test_two.py").write_text("import pytest\n@pytest.mark.windows_production\n@pytest.mark.ci_tier('not-a-tier')\ndef test_native(): pass\n",encoding="utf-8")
    (tmp_path/"test_three.py").write_text("def test_third(): pass\n",encoding="utf-8")
    (tmp_path/"ci").mkdir(exist_ok=True)
    (tmp_path/"ci/backend-test-shards.json").write_text("malformed taxonomy deliberately ignored",encoding="utf-8")
    write_json(tmp_path/"windows.json", manifest())
    output = tmp_path/f"report-{shard}"
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTEST_ADDOPTS="", CI_REPORT_DIR=str(output),
               CI_TEST_LANE="inherited-must-not-change-shard-identity", HYPOTHESIS_PROFILE="", GITHUB_STEP_SUMMARY=str(tmp_path/"summary.md"))
    result = subprocess.run([sys.executable,"-m","pytest","-p","tools.ci_pytest","--ci-windows-shard",str(shard),
                             "--ci-windows-manifest",str(tmp_path/"windows.json"),*extra],
                            cwd=tmp_path,env=env,text=True,capture_output=True,timeout=30)
    payload = json.loads((output/"report.json").read_text()) if (output/"report.json").exists() else None
    return result,payload


def test_plugin_partitions_raw_unmarked_tests_with_broken_classifier_metadata(tmp_path):
    first_result, first = plugin_suite(tmp_path,1)
    second_result, second = plugin_suite(tmp_path,2)
    third_result, third = plugin_suite(tmp_path,3)
    assert first_result.returncode == second_result.returncode == third_result.returncode == 0
    assert first["selected"] == ["test_one.py::test_plain"]
    assert second["selected"] == ["test_two.py::test_native"]
    assert third["selected"] == ["test_three.py::test_third"]
    assert third["lane"] == "windows-full-regression-3"
    assert first["collected"] == second["collected"] == third["collected"]
    assert all(set(item) == {"nodeid","path","markers"} for item in first["collected"])
    assert first["lane"] == "windows-full-regression-1"
    assert second["lane"] == "windows-full-regression-2"
    assert first["selection"]["marker"] == windows.LEGACY_WINDOWS_EXPRESSION


def test_plugin_allows_explicit_local_node_target_without_other_files(tmp_path):
    result, report = plugin_suite(tmp_path,1,"test_one.py::test_plain")
    assert result.returncode == 0
    assert report["selected"] == ["test_one.py::test_plain"]
    assert len(report["collected"]) == 1


@pytest.mark.parametrize("extra", [("--ci-lane","fast"),("--ci-shard","1"),("-m","slow")])
def test_plugin_refuses_classifier_coupling_and_marker_override(tmp_path,extra):
    result, report = plugin_suite(tmp_path,1,*extra)
    assert result.returncode == 4
    assert "cannot use" in result.stderr or "frozen legacy" in result.stderr


def test_checked_in_manifest_is_measured_and_separate_from_tier_partition():
    checked = windows.load_manifest(ROOT)
    assert checked["assignment_basis"]["source"]["workflow_run_id"].isdigit()
    assert len(checked["assignment_basis"]["source"]["report_sha256"]) == 64
    assert "tests/test_ci_windows_shards.py" in checked["files"]
    assert set(checked["files"].values()) == {1,2,3}
    basis = checked["assignment_basis"]
    assert basis["phase_model"] == {"workers": 4, "contention": basis["phase_model"]["contention"]}
    assert basis["phase_model"]["contention"] >= 1
    assert set(basis["expected_shard_seconds"]) == {"1", "2", "3"}


def test_validate_manifest_uses_complete_raw_windows_report_without_taxonomy(tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("Inventory validation must remain independent of tier metadata")
    monkeypatch.setattr(runner, "load_manifest", forbidden)
    report = write_json(tmp_path / "report.json", dict(inventory(), collect_only=True, reports=[]))
    partition = write_json(tmp_path / "windows.json", manifest())
    assert runner.main(["validate-windows-manifest", "--inventory", str(report),
                        "--windows-manifest", str(partition)]) == 0
    assert "3 files, 3 default tests, 3 exhaustive shards" in capsys.readouterr().out


@pytest.mark.parametrize("fault", ["unassigned", "absent", "non_windows", "partial", "narrowed", "failed"])
def test_validate_manifest_refuses_stale_or_incomplete_raw_reference(tmp_path, fault, capsys):
    report = inventory()
    partition = manifest()
    if fault == "unassigned": partition["files"].pop("test_one.py")
    elif fault == "absent": partition["files"]["test_absent.py"] = 1
    elif fault == "non_windows": report["platform"] = "Linux"
    elif fault == "partial": report["partial"] = True
    elif fault == "narrowed": report["selection"]["targets"] = ["test_one.py"]
    else: report["exit_code"] = 1
    # Preserve both owner values so the collection comparison finds the drift.
    if fault == "unassigned": partition["files"]["test_fourth.py"] = 1
    report_path = write_json(tmp_path / "report.json", report)
    manifest_path = write_json(tmp_path / "windows.json", partition)
    assert runner.main(["validate-windows-manifest", "--inventory", str(report_path),
                        "--windows-manifest", str(manifest_path)]) == 2
    assert "Backend CI selection error" in capsys.readouterr().err


def test_pr_windows_inventory_validation_runs_after_raw_collection_before_execution():
    import yaml
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["windows-platform-contract"]
    steps = job["steps"]
    names = [step.get("name") for step in steps]
    check = names.index("Validate exhaustive Windows shard inventory")
    assert "if" not in job and steps[check]["if"] == "matrix.shard == 1"
    assert names.index("Collect independent legacy Windows selection") < check < names.index("Test Windows platform contract")
    assert steps[check]["run"] == (
        "python tools/test_backend.py validate-windows-manifest "
        "--inventory artifacts/ci-tests/legacy-windows-reference/report.json"
    )


def phased_inventory():
    report = inventory()
    for phase in report["reports"]:
        phase["duration"] = {"test_one.py": 8.0, "test_two.py": 4.0, "test_three.py": 2.0}[windows.node_path(phase["nodeid"])] / 3
    report["_report_sha256"] = "0" * 64
    return report


def test_phase_model_weighs_serial_tests_fully_and_other_tests_per_worker():
    report = phased_inventory()
    model = windows.phase_model(4, 2.0)
    updated = windows.updated_manifest(None, report, timing_report=report, rebalance=True, model=model)
    basis = updated["assignment_basis"]
    assert basis["phase_model"] == {"workers": 4, "contention": 2.0}
    assert basis["serial_seconds"] == {"test_three.py": pytest.approx(2.0)}
    # Expected seconds: one 8 * 2 / 4 = 4, two 4 * 2 / 4 = 2, three serial 2.
    assert basis["expected_shard_seconds"] == {"1": 4.0, "2": 2.0, "3": 2.0}
    assert basis["method"].startswith("expected job seconds")
    plain = windows.updated_manifest(None, report, timing_report=report, rebalance=True)
    assert "phase_model" not in plain["assignment_basis"] and plain["assignment_basis"]["method"].startswith("file-duration")


def test_phase_model_is_retained_for_later_additions():
    report = phased_inventory()
    first = windows.updated_manifest(None, report, timing_report=report, rebalance=True, model=windows.phase_model(4, 2.0))
    enlarged = dict(report, collected=[*report["collected"], row("test_new.py::test_c")])
    after = windows.updated_manifest(first, enlarged)
    assert after["assignment_basis"]["phase_model"] == first["assignment_basis"]["phase_model"]
    assert after["assignment_basis"]["serial_seconds"] == first["assignment_basis"]["serial_seconds"]
    assert after["assignment_basis"]["unmeasured_files"] == ["test_new.py"]
    # The median expected weight (2.0) joins a lightest shard, so 2 takes it before 3.
    assert after["files"]["test_new.py"] == 2


@pytest.mark.parametrize("workers,contention", [(0, 2.0), (4, 0.5), (4, float("nan")), (4, float("inf")), (True, 2.0), (4, "2")])
def test_phase_model_refuses_invalid_parameters(workers, contention):
    with pytest.raises(windows.WindowsShardError, match="Phase model"):
        windows.phase_model(workers, contention)


def test_phase_model_refuses_serial_seconds_above_measured_seconds():
    report = phased_inventory()
    first = windows.updated_manifest(None, report, timing_report=report, rebalance=True, model=windows.phase_model(4, 2.0))
    first["assignment_basis"]["serial_seconds"]["test_three.py"] = 99.0
    with pytest.raises(windows.WindowsShardError, match="Serial seconds"):
        windows.updated_manifest(first, report)


@pytest.mark.parametrize("options", [["--workers", "4"], ["--contention", "2.0"]])
def test_manifest_update_needs_workers_and_contention_together(tmp_path, options, capsys):
    report = write_json(tmp_path / "report.json", dict(inventory(), collect_only=True, reports=[]))
    assert runner.main(["update-windows-manifest", "--inventory", str(report), "--dry-run",
                        "--windows-manifest", str(tmp_path / "absent.json"), *options]) == 2
    assert "--workers with --contention" in capsys.readouterr().err


def test_contention_belongs_only_to_manifest_updates():
    args = runner.parser().parse_args(["windows-full-regression", "--shard", "1", "--contention", "2.0"])
    with pytest.raises(runner.SelectionError, match="--contention"):
        runner.build_command(args, [], {})



def test_candidate_model_keeps_complete_files_and_serial_work():
    report = inventory()
    models = windows.model_candidate_partitions(report, counts=[3])
    assert len(models) == 1
    candidate = models[0]
    assert set(candidate["files"]) == {"test_one.py", "test_two.py", "test_three.py"}
    assert set(candidate["files"].values()) == {1, 2, 3}
    assert candidate["expected_work_seconds"][candidate["files"]["test_three.py"]] == 4.5
    assert candidate["peak_work_seconds"] == 4.5
    assert candidate["stress_1_5_work_seconds"] == 6.75
    assert candidate["stress_1_7_work_seconds"] == pytest.approx(7.65)
    assert candidate == windows.model_candidate_partitions(report, counts=[3])[0]
    assert sum(candidate["expected_work_seconds"].values()) == 6.75


@pytest.mark.parametrize("counts", [[], [2], [13], [3, 3], [True], [4]])
def test_candidate_model_rejects_invalid_counts(counts):
    with pytest.raises(windows.WindowsShardError, match="Candidate counts"):
        windows.model_candidate_partitions(inventory(), counts=counts)


def test_candidate_model_rejects_incomplete_evidence_and_phase_data():
    report = inventory()
    report["complete"] = False
    with pytest.raises(windows.WindowsShardError):
        windows.model_candidate_partitions(report, counts=[3])
    report = inventory()
    report["reports"].pop()
    with pytest.raises(windows.WindowsShardError, match="Incomplete Windows timing evidence"):
        windows.model_candidate_partitions(report, counts=[3])


def test_candidate_model_handles_zero_weights_without_empty_shards():
    report = inventory()
    for phase in report["reports"]:
        phase["duration"] = 0.0
    candidate = windows.model_candidate_partitions(report, counts=[3])[0]
    assert set(candidate["files"].values()) == {1, 2, 3}
    assert candidate["peak_work_seconds"] == 0.0



def test_queue_replay_respects_matrix_caps_and_runner_capacity():
    jobs = {"wheel": {"duration": 3, "release": 0, "needs": ()},
            "a": {"duration": 4, "release": 0, "needs": ("wheel",), "group": "matrix"},
            "b": {"duration": 4, "release": 0, "needs": ("wheel",), "group": "matrix"},
            "other": {"duration": 5, "release": 0, "needs": ()},
            "gate": {"duration": 1, "release": 0, "needs": ("a", "b", "other")}}
    result = windows.replay_schedule(jobs, capacity=2, group_limits={"matrix": 1})
    assert result["elapsed_seconds"] == 12
    assert result["jobs"]["b"]["queue"] == 4
    assert result["peak_concurrency"] == 2
    assert result["runner_seconds"] == 17
    jobs["wheel"]["needs"] = ("gate",)
    with pytest.raises(windows.WindowsShardError, match="cyclic"):
        windows.replay_schedule(jobs, capacity=2, group_limits={"matrix": 1})


def test_topology_replay_is_reproducible_and_includes_stress_and_overlapping_runs():
    weights = {f"f{n}.py": (4.0 if n == 0 else 0.0, 16.0) for n in range(6)}
    profiles = [{"serial": 1.0, "parallel": 1.0, "overhead": 2.0},
                {"serial": 1.4, "parallel": 1.5, "overhead": 4.0}]
    other = {"wheel": {"duration": 3, "needs": ()}, "visual": {"duration": 6, "needs": ()}}
    args = dict(observations=profiles, counts=[3, 4], capacity=3, other_jobs=other,
                repeats=20, second_run_delay=0, matrix_limit=2)
    result = windows.replay_topologies(weights, **args)
    assert result == windows.replay_topologies(weights, **args)
    assert [r["shards"] for r in result] == [3, 4]
    for row in result:
        assert set(row["files"]) == set(weights)
        assert row["conditional_p95_seconds"] >= row["median_seconds"]
        assert row["two_run_conditional_p95_seconds"] >= row["conditional_p95_seconds"]
        assert row["stress"]["1.7"]["peak_concurrency"] <= 3
        assert max(row["stress"]["1.7"]["run_seconds"]) >= max(row["stress"]["1.5"]["run_seconds"])
        assert row["max_parallel"] == 2


def test_external_workflows_consume_capacity_without_delaying_product_gate_when_capacity_is_available():
    weights = {f"f{n}.py": (1., 4.) for n in range(3)}
    result = windows.replay_topologies(weights, observations=[{"serial": 1., "parallel": 1., "overhead": 0.}],
        counts=[3], capacity=20, other_jobs={"wheel": {"duration": 1, "needs": ()},
        "visual": {"duration": 100, "needs": (), "gate_dependency": False}}, repeats=2)[0]
    assert result["median_seconds"] == 18
    assert result["median_runner_seconds"] == 122



def overlap_fixture():
    full = inventory()
    full.update(python="3.11.9", python_implementation="CPython", pytest="9.1.1",
                execution={"workers": 4, "dist": "load"})
    selected = ["test_two.py::test_windows"]
    focused = dict(full, selected=selected,
                   deselected=sorted({r["nodeid"] for r in full["collected"]} - set(selected)),
                   reports=[dict(r) for r in full["reports"] if r["nodeid"] in selected])
    return full, focused


def test_overlap_requires_exact_focused_execution_and_retains_complement():
    full, focused = overlap_fixture()
    assert windows.prove_focused_overlap(full, [focused]) == {
        "default_count": 3, "focused_count": 1, "complement_count": 2,
        "focused_nodeids": ["test_two.py::test_windows"],
    }


@pytest.mark.parametrize("change", ["python", "workers", "outcome", "missing-phase", "inventory", "duplicate", "subtest"])
def test_overlap_rejects_non_equivalent_execution(change):
    full, focused = overlap_fixture()
    if change == "python": focused["python"] = "3.13.1"
    elif change == "workers": focused["execution"] = {"workers": 1, "dist": "load"}
    elif change == "outcome": focused["reports"][1].update(outcome="skipped", skip_reason="changed")
    elif change == "missing-phase": focused["reports"].pop()
    elif change == "inventory": focused["collected"] = [dict(r, markers=[]) for r in focused["collected"]]
    elif change == "duplicate": focused["selected"] *= 2
    elif change == "subtest": focused["reports"].append(dict(focused["reports"][1], subtest={"index": 1, "msg": "extra", "kwargs": {}}))
    with pytest.raises(windows.WindowsShardError):
        windows.prove_focused_overlap(full, [focused])



def candidate_fixture():
    full = {f"test_full_{n}.py": n for n in range(1, 11)}
    platform = {f"test_platform_{n}.py": n for n in range(1, 5)}
    full.update({p: 1 for p in platform})
    full["test_analytics.py"] = 2
    rows = [row(p + "::test_case") for p in full]
    rows.append(row("test_platform_1.py::test_slow", "slow"))
    retained = "test_platform_1.py::test_case"
    plan = {"schema": 1, "full": full, "platform": platform, "retain_in_full": [retained]}
    contracts = {"platform": [{"selector": p} for p in platform],
                 "analytics": [{"selector": "test_analytics.py::test_case"}]}
    return rows, plan, contracts


def test_candidate_keeps_every_raw_default_node_and_intentional_overlap():
    rows, plan, contracts = candidate_fixture()
    groups = windows.candidate_groups(rows, plan, contracts)
    assert set().union(*groups.values()) == windows.legacy_nodeids(rows)
    assert groups["full-1"] & groups["platform-1"] == set(plan["retain_in_full"])
    assert groups["full-2"] == {"test_full_2.py::test_case"}
    assert groups["analytics"] == {"test_analytics.py::test_case"}
    assert not groups["full-1"] & groups["platform-2"]


@pytest.mark.parametrize("fault", ["missing-file", "new-focused-file", "overlap", "retained-stale", "duplicate", "empty-shard"])
def test_candidate_refuses_invalid_scope(fault):
    rows, plan, contracts = candidate_fixture()
    if fault == "missing-file": plan["full"].pop("test_full_1.py")
    elif fault == "new-focused-file": contracts["platform"].append({"selector": "test_full_2.py"})
    elif fault == "overlap": contracts["analytics"].append({"selector": "test_platform_1.py"})
    elif fault == "retained-stale": plan["retain_in_full"].append("test_missing.py::test_case")
    elif fault == "duplicate": rows.append(rows[0])
    elif fault == "empty-shard": plan["full"]["test_full_10.py"] = 1
    with pytest.raises(windows.WindowsShardError):
        windows.candidate_groups(rows, plan, contracts)


def candidate_reports():
    rows, plan, contracts = candidate_fixture()
    groups = windows.candidate_groups(rows, plan, contracts)
    def report(lane, group, selected):
        job = ("windows-qualification" if group is None else "windows-full-shards" if group.startswith("full-")
               else "windows-platform-contract" if group.startswith("platform-") else "analytics-windows-contract")
        selected = sorted(selected)
        return dict(schema="ci-test-report/v1", complete=True, exit_code=0, collect_only=group is None,
            partial=False, platform="Windows", python="3.11.9", python_implementation="CPython", pytest="9.1.1",
            profile="", selection_profile="", collection_errors=[], source_commit="a"*40,
            workflow_run_id="1", run_attempt="1", job_id=job, lane=lane, wall_seconds=1.0,
            collected=rows, selected=selected, deselected=sorted({r["nodeid"] for r in rows}-set(selected)),
            selection={"keyword": "", "marker": windows.LEGACY_WINDOWS_EXPRESSION, "targets": ["tests"]},
            execution={"workers": 4, "dist": "load"},
            reports=[] if group is None else [dict(nodeid=n, when=phase, outcome="passed", duration=0.1,
                wasxfail=None, skip_reason=None, detail="") for n in selected for phase in ("setup", "call", "teardown")])
    reports = {f"candidate-{g}": report(f"candidate-{g}", g, nodes) for g,nodes in groups.items()}
    reports["legacy-windows-reference"] = report("legacy-windows-reference", None, windows.legacy_nodeids(rows))
    return reports, plan, contracts


def test_candidate_evidence_requires_current_run_and_exact_execution():
    from tools.ci_windows_candidate import validate_candidate
    reports, plan, contracts = candidate_reports()
    assert validate_candidate(reports, plan, contracts, source="a"*40, run=1, attempt=1) == {
        "default_nodes": 15, "full_nodes": 11, "focused_nodes": 5, "retained_overlap": 1}


@pytest.mark.parametrize("fault", ["wrong-attempt", "wrong-source", "wrong-job", "missing-report", "skip-omitted", "missing-node", "workers", "raw-marker"])
def test_candidate_gate_refuses_unproven_deduplication(fault):
    from tools.ci_windows_candidate import validate_candidate
    from tools.ci_gate import GateError
    reports, plan, contracts = candidate_reports()
    r = reports["candidate-platform-2"]
    if fault == "wrong-attempt": r["run_attempt"] = "2"
    elif fault == "wrong-source": r["source_commit"] = "b"*40
    elif fault == "wrong-job": r["job_id"] = "windows-full-shards"
    elif fault == "missing-report": reports.pop("candidate-platform-2")
    elif fault == "skip-omitted": r["reports"][1].update(outcome="skipped", skip_reason="Skipped: new reason")
    elif fault == "missing-node": r["selected"] = []
    elif fault == "workers": r["execution"]["workers"] = 2
    elif fault == "raw-marker": r["collected"] = [dict(row, markers=["changed"]) for row in r["collected"]]
    with pytest.raises(GateError):
        validate_candidate(reports, plan, contracts, source="a"*40, run=1, attempt=1)



def test_trial_workflow_preserves_main_producers_without_release_authority():
    import yaml
    from tools.render_ci_topology_trial import render, REFS
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    visual = yaml.safe_load((ROOT / ".github/workflows/visual-capture.yml").read_text(encoding="utf-8"))
    trial = render(ci, visual)
    assert trial["on"] == {"push": {"branches": REFS}}
    assert trial["name"] != ci["name"] and trial["name"] != visual["name"]
    jobs = trial["jobs"]
    assert {"required-ci-gate", "build-and-test", "windows-tests"}.isdisjoint(jobs)
    assert trial["permissions"] == {"contents": "read"}
    assert "permissions" not in jobs["fixed-sqlcipher-wheel"]
    assert not any("actions/attest@" in s.get("uses", "") for s in jobs["fixed-sqlcipher-wheel"]["steps"])
    assert jobs["windows-full-shards"]["strategy"]["matrix"]["shard"] == list(range(1, 11))
    assert jobs["windows-platform-contract"]["strategy"]["matrix"]["shard"] == list(range(1, 5))
    for name in ("windows-qualification", "candidate-preflight", "windows-full-shards", "windows-platform-contract", "analytics-windows-contract"):
        assert name in jobs["candidate-coverage"]["needs"]
    for step in ci["jobs"]["windows-platform-contract"]["steps"]:
        if step.get("if") == "matrix.shard == 1" or step.get("if") == "always() && matrix.shard == 1":
            copied = next(s for s in jobs["windows-qualification"]["steps"] if s["name"] == step["name"])
            assert copied.get("run") == step.get("run")
            assert copied.get("if") == ("always()" if step["if"].startswith("always()") else None)
    for key in visual["jobs"]:
        assert jobs[key] == visual["jobs"][key]
    assert yaml.safe_load(yaml.safe_dump(trial, sort_keys=False)) == trial


def test_candidate_plugin_selects_whole_partition_from_raw_collection(tmp_path):
    rows, plan, contracts = candidate_fixture()
    (tmp_path / "ci").mkdir()
    write_json(tmp_path / "ci/windows-topology-candidate.json", plan)
    write_json(tmp_path / "ci/backend-test-shards.json", {"windows_contracts": contracts})
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers =\n slow: legacy exclusion\n stateful_tier_a: legacy exclusion\n stateful_tier_b: legacy exclusion\n stateful_agent_tier_a: legacy exclusion\n", encoding="utf-8")
    for file in plan["full"]:
        source = "def test_case(): pass\n"
        if file == "test_platform_1.py":
            source += "import pytest\n@pytest.mark.slow\ndef test_slow(): assert False\n"
        (tmp_path / file).write_text(source, encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTEST_ADDOPTS="", CI_TEST_LANE="candidate-platform-2",
               CI_REPORT_DIR=str(tmp_path / "out"))
    command = [sys.executable, "-m", "pytest", "-p", "tools.ci_pytest", "-m", windows.LEGACY_WINDOWS_EXPRESSION,
               "--ci-windows-candidate", "platform-2"]
    result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / "out/report.json").read_text(encoding="utf-8"))
    assert report["selected"] == ["test_platform_2.py::test_case"]
    assert report["lane"] == "candidate-platform-2" and report["complete"] is True
