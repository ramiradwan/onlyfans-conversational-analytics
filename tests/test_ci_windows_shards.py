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
    return dict(schema_version=1, files={"test_one.py": 1, "test_two.py": 2})


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def inventory():
    rows = [row("test_one.py::test_plain"), row("test_two.py::test_windows", "windows_production"),
            row("test_one.py::test_large", "slow")]
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
            row("test_one.py::test_ignored", "slow"), row("test_two.py::test_stateful", "stateful_tier_b")]
    rows[0].update(tier="invalid", shard=4, windows_contract="platform")
    first = windows.selected_nodeids(rows, manifest(), 1)
    second = windows.selected_nodeids(rows, manifest(), 2)
    assert first == {r"test_one.py::test_bytes[\xff]"}
    assert second == {"test_two.py::test_native"}
    assert not first & second and first | second == windows.legacy_nodeids(rows)


@pytest.mark.parametrize("problem", ["unknown", "missing", "duplicate"])
def test_full_inventory_refuses_unassigned_missing_and_duplicate_nodes(problem):
    rows = [row("test_one.py::test_a"), row("test_two.py::test_b")]
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


@pytest.mark.parametrize("files", [{"test_one.py": True, "test_two.py": 2}, {"test_one.py": 3, "test_two.py": 2},
    {"test_one.py": 1}, {"../test_one.py": 1, "test_two.py": 2}, {"test_one.py::test_a": 1, "test_two.py": 2}])
def test_manifest_rejects_invalid_owners_or_paths(tmp_path, files):
    path = write_json(tmp_path/"manifest.json", dict(schema_version=1, files=files))
    with pytest.raises(windows.WindowsShardError):
        windows.load_manifest(tmp_path, path)


def test_manifest_rejects_duplicate_json_keys(tmp_path):
    path = tmp_path/"manifest.json"
    path.write_text('{"schema_version":1,"files":{"test_one.py":1,"test_one.py":2,"test_two.py":2}}')
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
    with pytest.raises(runner.SelectionError, match="exactly two"):
        runner.build_command(args, [], {})


def plugin_suite(tmp_path, shard, *extra):
    (tmp_path/"pytest.ini").write_text("[pytest]\nmarkers =\n slow: old exclusion\n windows_production: original Windows obligation\n ci_tier(value): deliberately irrelevant\n", encoding="utf-8")
    (tmp_path/"test_one.py").write_text("import pytest\ndef test_plain(): pass\n@pytest.mark.slow\ndef test_large(): assert False\n",encoding="utf-8")
    (tmp_path/"test_two.py").write_text("import pytest\n@pytest.mark.windows_production\n@pytest.mark.ci_tier('not-a-tier')\ndef test_native(): pass\n",encoding="utf-8")
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
    assert first_result.returncode == second_result.returncode == 0
    assert first["selected"] == ["test_one.py::test_plain"]
    assert second["selected"] == ["test_two.py::test_native"]
    assert first["collected"] == second["collected"]
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
    assert set(checked["files"].values()) == {1,2}
