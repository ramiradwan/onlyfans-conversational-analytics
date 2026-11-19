"""Execution-level checks for CI evidence, independent of application fixtures."""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.ci_tier("fast")


ROOT = Path(__file__).resolve().parents[1]


def run_suite(tmp_path, source, *arguments, environment=None):
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n omitted: selection fixture\n ci_tier(name): explicit CI owner\n", encoding="utf-8")
    sources = source if isinstance(source, dict) else {"test_sample.py": source}
    for name, content in sources.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
    destination = tmp_path / "evidence"
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [str(ROOT), os.environ.get("PYTHONPATH")])),
               PYTEST_ADDOPTS="", CI_PYTEST_WATCHDOG_SECONDS="", CI_TEST_LANE="report-fixture", CI_REPORT_DIR=str(destination),
               PRODUCT_SHA="a" * 40, GITHUB_RUN_ID="123", GITHUB_RUN_ATTEMPT="2", GITHUB_JOB="backend-fast",
               GITHUB_STEP_SUMMARY=str(tmp_path / "github-summary.md"))
    for name in ("PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT", "PYTEST_XDIST_TESTRUNUID"):
        env.pop(name, None)
    env.update(environment or {})
    result = subprocess.run([sys.executable, "-m", "pytest", "-p", "tools.ci_pytest", "--junitxml=" + str(tmp_path / "original.xml"),
                             *arguments], cwd=tmp_path, env=env, text=True, capture_output=True, timeout=30)
    report = json.loads((destination / "report.json").read_text(encoding="utf-8"))
    return result, report, destination


def test_real_reporter_partitions_windows_platform_without_a_top_level_shard(tmp_path):
    from copy import deepcopy
    from tools import ci_gate

    assignments = {f"test_platform_{number}.py": number for number in (1, 2, 3)}
    sources = {name: "import pytest\npytestmark = pytest.mark.ci_tier('integration')\n"
                     "def test_first(): pass\ndef test_second(): pass\n"
               for name in assignments}
    manifest = {
        "schema_version": 1,
        "integration_shards": assignments,
        "windows_platform_shards": assignments,
        "windows_contracts": {"platform": [
            {"selector": name, "reason": "Synthetic Windows platform contract"} for name in assignments
        ], "analytics": []},
        "stateful_profiles": {},
    }
    expected = {f"{name}::{case}" for name in assignments for case in ("test_first", "test_second")}
    reports = {}
    for number, lane in enumerate(ci_gate.WINDOWS_PLATFORM_LANES, start=1):
        suite = tmp_path / str(number)
        suite.mkdir()
        manifest_path = suite / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        result, report, _ = run_suite(
            suite, sources, "--ci-lane", "windows-platform", "--ci-shard", str(number),
            "--ci-manifest", str(manifest_path), "--ci-validate",
            environment={"CI_TEST_LANE": lane, "GITHUB_JOB": "windows-platform-contract"},
        )
        assert result.returncode == report["exit_code"] == 0, result.stdout + result.stderr
        assert report["schema"] == "ci-test-report/v1" and "shard" not in report
        assert report["lane"] == lane and report["job_id"] == "windows-platform-contract"
        assert {row["nodeid"] for row in report["collected"]} == expected
        assert all(row["windows_platform_shard"] == assignments[row["path"]] for row in report["collected"])
        assert set(report["selected"]) == {node for node in expected if assignments[node.split("::", 1)[0]] == number}
        spec = ci_gate.ReportSpec(lane, report["platform"], python=f"{sys.version_info.major}.{sys.version_info.minor}")
        assert set(ci_gate.validate_report(report, spec).values()) == {"executed"}
        reports[lane] = report

    owners = ci_gate._windows_platform_owners(reports, expected, assignments)
    assert owners == {node: f"windows-platform-contract-{assignments[node.split('::', 1)[0]]}" for node in expected}
    misplaced = deepcopy(reports)
    first, second = (misplaced[lane] for lane in ci_gate.WINDOWS_PLATFORM_LANES[:2])
    first["selected"], second["selected"] = second["selected"], first["selected"]
    with pytest.raises(ci_gate.GateError, match="owner differs from the checked-in manifest"):
        ci_gate._windows_platform_owners(misplaced, expected, assignments)


def test_reports_selection_outcomes_and_preserves_failure_exit(tmp_path):
    result, report, destination = run_suite(tmp_path,
        "import pytest\n"
        "def test_pass(): pass\n"
        "def test_fail(): assert False, 'actual failure'\n"
        "@pytest.mark.skip(reason='fixture skip')\ndef test_skip(): pass\n"
        "@pytest.mark.omitted\ndef test_omitted(): pass\n", "-m", "not omitted")
    assert result.returncode == report["exit_code"] == 1
    assert len(report["collected"]) == 4
    assert len(report["selected"]) == 3
    assert report["deselected"] == ["test_sample.py::test_omitted"]
    assert {r["when"] for r in report["reports"]} == {"setup", "call", "teardown"}
    assert report["complete"] and report["wall_seconds"] > 0
    assert (destination / "junit.xml").is_file()
    summary = (destination / "summary.md").read_text(encoding="utf-8")
    assert "actual failure" in summary and 'python -m pytest "test_sample.py::test_fail"' in summary
    assert report["source_commit"] == "a" * 40 and report["run_attempt"] == "2"
    assert report["python"] == platform.python_version()
    assert report["python_implementation"] == platform.python_implementation()
    skipped = [r for r in report["reports"] if r["outcome"] == "skipped"]
    assert [r["skip_reason"] for r in skipped] == ["Skipped: fixture skip"]
    assert str(tmp_path) not in skipped[0]["skip_reason"]
    progress = [json.loads(line) for line in (destination / "progress.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [event["sequence"] for event in progress] == list(range(1, len(progress) + 1))
    assert progress[0]["event"] == "session_start"
    assert progress[0]["source_commit"] == "a" * 40 and progress[0]["run_attempt"] == "2"
    assert progress[-1]["event"] == "session_finish" and progress[-1]["exit_code"] == 1
    collection = next(event for event in progress if event["event"] == "collection_finish")
    assert collection["collected"] == 4 and collection["selected"] == 3
    started = [(event["nodeid"], event["when"]) for event in progress if event["event"] == "phase_start"]
    finished = [(event["nodeid"], event["when"]) for event in progress if event["event"] == "phase_finish"]
    assert started == finished
    assert ("test_sample.py::test_fail", "call") in started
    assert any(event["event"] == "phase_report" and event["outcome"] == "failed" for event in progress)


def test_collection_error_is_visible_and_nonzero(tmp_path):
    result, report, _ = run_suite(tmp_path, "raise RuntimeError('collection broken')\n")
    assert result.returncode == report["exit_code"] == 2
    assert "collection broken" in report["collection_errors"][0]["detail"]


def test_setup_failure_is_not_counted_as_a_pass(tmp_path):
    result, report, destination = run_suite(tmp_path,
        "import pytest\n@pytest.fixture\ndef broken(): raise ValueError('setup broken')\n"
        "def test_setup(broken): pass\n")
    assert result.returncode == 1
    assert any(r["when"] == "setup" and r["outcome"] == "failed" for r in report["reports"])
    assert "0 passed · 1 failed" in (destination / "summary.md").read_text(encoding="utf-8")


def test_empty_selected_suite_keeps_pytest_exit_five(tmp_path):
    result, report, _ = run_suite(tmp_path, "def test_one(): pass\n", "-k", "absent")
    assert result.returncode == report["exit_code"] == 5
    assert report["selected"] == [] and len(report["deselected"]) == 1


def test_only_the_exact_full_windows_marker_is_canonical_not_partial(tmp_path):
    from tools.ci_windows_shards import LEGACY_WINDOWS_EXPRESSION
    source = "import pytest\n@pytest.mark.slow\ndef test_excluded(): pass\ndef test_included(): pass\n"
    result, report, _ = run_suite(tmp_path, source, "-m", LEGACY_WINDOWS_EXPRESSION, "test_sample.py")
    assert result.returncode == 0
    assert report["partial"] is False
    assert report["selection"]["marker"] == LEGACY_WINDOWS_EXPRESSION
    assert report["selected"] == ["test_sample.py::test_included"]
    assert report["deselected"] == ["test_sample.py::test_excluded"]
    result, report, _ = run_suite(tmp_path, source, "-m", "not slow", "test_sample.py")
    assert result.returncode == 0 and report["partial"] is True
    result, report, _ = run_suite(tmp_path, source, "-m", LEGACY_WINDOWS_EXPRESSION, "-k", "included", "test_sample.py")
    assert result.returncode == 0 and report["partial"] is True


def test_collect_only_is_distinguished_from_executed_coverage(tmp_path):
    result, report, _ = run_suite(tmp_path, "def test_one(): assert False\n", "--collect-only")
    assert result.returncode == 0 and report["collect_only"] is True
    assert report["selected"] == ["test_sample.py::test_one"] and report["reports"] == []


def test_xfail_reason_is_recorded_without_platform_path(tmp_path):
    result, report, _ = run_suite(tmp_path,
        "import pytest\n@pytest.mark.xfail(reason='known contract')\ndef test_known(): assert False\n")
    assert result.returncode == 0
    skipped = [r for r in report["reports"] if r["outcome"] == "skipped"]
    assert [r["skip_reason"] for r in skipped] == ["known contract"]


def test_unwritable_optional_summary_does_not_change_test_exit(tmp_path):
    (tmp_path / "github-summary.md").mkdir()
    result, report, _ = run_suite(tmp_path, "def test_pass(): pass\n", "-W", "error")
    assert result.returncode == report["exit_code"] == 0
    assert "Could not write optional CI summary" in result.stderr


def test_interrupted_worker_retains_flushed_progress_without_complete_report(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "test_blocked.py").write_text(
        "from pathlib import Path\nimport time\n"
        "def test_blocked():\n"
        " Path('body-started').touch()\n"
        " time.sleep(60)\n", encoding="utf-8")
    destination = tmp_path / "evidence"
    environment = dict(os.environ, PYTHONPATH=str(ROOT), PYTEST_ADDOPTS="",
                       CI_TEST_LANE="interrupted-fixture", CI_REPORT_DIR=str(destination),
                       CI_PYTEST_LIVE_PROGRESS="1")
    process = subprocess.Popen([sys.executable, "-m", "pytest", "-p", "tools.ci_pytest"],
                               cwd=tmp_path, env=environment, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 15
        progress = []
        while time.monotonic() < deadline and process.poll() is None:
            journal = destination / "progress.jsonl"
            if journal.is_file():
                # Only newline-terminated entries are committed to the journal.
                lines = journal.read_text(encoding="utf-8").split("\n")[:-1]
                progress = [json.loads(line) for line in lines]
            if (tmp_path / "body-started").is_file() and any(
                event["event"] == "phase_start" and event.get("when") == "call"
                for event in progress
            ):
                break
            time.sleep(0.02)
        else:
            pytest.fail("isolated pytest did not reach its blocked call")
        assert not (destination / "report.json").exists()
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            console, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            console, _ = process.communicate(timeout=5)
    assert process.returncode != 0
    assert "[ci-progress] start test_blocked.py::test_blocked" in console
    retained = [json.loads(line) for line in (destination / "progress.jsonl").read_text(encoding="utf-8").splitlines()]
    assert retained[0]["event"] == "session_start"
    assert retained[-1]["event"] == "phase_start" and retained[-1]["when"] == "call"
    assert not any(event["event"] == "session_finish" for event in retained)
    assert not (destination / "report.json").exists()


@pytest.mark.parametrize("api", ["unittest", "pytest"])
@pytest.mark.parametrize("child_outcome", ["passed", "failed", "skipped"])
def test_subtests_keep_distinct_identity_and_cannot_hide_child_outcomes(tmp_path, api, child_outcome):
    pytest.importorskip("_pytest.subtests")
    from tools import ci_gate

    action = {"passed": "pass", "failed": "assert False, 'child failed'",
              "skipped": "pytest.skip('child unavailable')"}[child_outcome]
    if api == "unittest":
        source = (
            "import unittest, pytest\n"
            "class TestChildren(unittest.TestCase):\n"
            " def test_children(self):\n"
            "  for index in range(3):\n"
            "   with self.subTest('same context', value='same'):\n"
            f"    if index == 1: {action}\n"
        )
    else:
        source = (
            "import pytest\n"
            "def test_children(subtests):\n"
            " for index in range(3):\n"
            "  with subtests.test('same context', value='same'):\n"
            f"   if index == 1: {action}\n"
        )
    result, report, destination = run_suite(tmp_path, source)
    assert result.returncode == report["exit_code"] == (1 if child_outcome == "failed" else 0)
    children = [phase for phase in report["reports"] if "subtest" in phase]
    ordinary = [phase for phase in report["reports"] if "subtest" not in phase]
    assert [phase["when"] for phase in ordinary] == ["setup", "call", "teardown"]
    assert [phase["subtest"] for phase in children] == [
        {"index": index, "msg": "same context", "kwargs": {"value": "'same'"}}
        for index in (1, 2, 3)
    ]
    assert [phase["outcome"] for phase in children] == ["passed", child_outcome, "passed"]
    rendered = (destination / "summary.md").read_text(encoding="utf-8")
    assert {"passed": "1 passed · 0 failed · 0 skipped", "failed": "0 passed · 1 failed · 0 skipped",
            "skipped": "0 passed · 0 failed · 1 skipped"}[child_outcome] in rendered
    spec = ci_gate.ReportSpec("backend-fast", report["platform"],
                              python=f"{sys.version_info.major}.{sys.version_info.minor}")
    if child_outcome == "passed":
        assert ci_gate.validate_report(report, spec) == {report["selected"][0]: "executed"}
    else:
        # Even a forged successful process exit cannot hide a failed/skipped child.
        report["exit_code"] = 0
        with pytest.raises(ci_gate.GateError, match="failed|subtest did not pass"):
            ci_gate.validate_report(report, spec)
    if child_outcome == "failed":
        assert "child failed" in rendered and "Subtest context:" in rendered
    if child_outcome == "skipped":
        assert children[1]["skip_reason"] == "Skipped: child unavailable"


def test_socket_loop_test_module_imports_without_application_dependencies(tmp_path):
    module = ROOT / "tests/test_e2e_text_analysis_fixture.py"
    source = (
        "import importlib.abc, importlib.util, sys\n"
        "class NoApplication(importlib.abc.MetaPathFinder):\n"
        " def find_spec(self, fullname, path=None, target=None):\n"
        "  if fullname.startswith('app.') and fullname not in {'app.core', 'app.core.server_loop'}:\n"
        "   raise AssertionError('Application dependency imported: ' + fullname)\n"
        "def test_import():\n"
        " sys.meta_path.insert(0, NoApplication())\n"
        f" spec = importlib.util.spec_from_file_location('socket_loop_cases', {str(module)!r})\n"
        " loaded = importlib.util.module_from_spec(spec)\n"
        " spec.loader.exec_module(loaded)\n"
        " assert 'e2e_text_analysis_fixture' not in sys.modules\n"
        " assert 'app.core.server_loop' in sys.modules\n"
        " assert not any(name.startswith(('app.persistence', 'app.nlp', 'app.security')) for name in sys.modules)\n"
    )
    result, report, _ = run_suite(tmp_path, source, "--noconftest")
    assert result.returncode == report["exit_code"] == 0, result.stdout + result.stderr
    assert report["selected"] == ["test_sample.py::test_import"]
    assert [phase["outcome"] for phase in report["reports"]] == ["passed"] * 3


LANE_SOURCE = '''import os, time
from pathlib import Path
import pytest
pytestmark = pytest.mark.ci_tier("fast")
@pytest.mark.parametrize("case", range(3))
def test_parallel(case, unused_tcp_port):
    assert int(os.environ["LOCAL_BACKEND_PORT_START"]) <= unused_tcp_port <= int(os.environ["LOCAL_BACKEND_PORT_END"])
    active = Path(ACTIVE) / (str(os.getpid()) + "-" + str(case))
    active.touch()
    time.sleep(0.3)
    active.unlink()
def test_skipped():
    pytest.skip("unavailable here")
@pytest.mark.xfail(reason="known gap", strict=True)
def test_expected_failure():
    assert False
def test_children(subtests):
    for index in range(2):
        with subtests.test("child", value=index, label="same"):
            pass
@pytest.mark.omitted
def test_deselected():
    pass
@pytest.mark.serial
def test_serial_runs_alone():
    assert not list(Path(ACTIVE).iterdir())
'''


def lane_command(tmp_path, output):
    return [sys.executable, "-m", "pytest", "-p", "tools.ci_pytest", "--override-ini=addopts=",
            "--ci-output-dir", str(output), "--junitxml", str(output / "junit.xml"), "-m", "not omitted"]


def run_lane_suite(tmp_path, source=LANE_SOURCE, workers=2, dist="load"):
    """Run the fixture suite once serially and once through the CI lane runner."""
    from tools import test_backend_parallel as parallel

    active = tmp_path / "active"
    active.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    (project / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n omitted: selection fixture\n ci_tier(name): explicit CI owner\n"
        " serial: exclusive\n", encoding="utf-8")
    for name in ("test_a.py", "test_b.py"):
        (project / name).write_text(source.replace("ACTIVE", repr(str(active))), encoding="utf-8")
    env = dict(os.environ, PYTEST_ADDOPTS="", CI_TEST_LANE="lane-fixture", PRODUCT_SHA="a" * 40,
               GITHUB_RUN_ID="123", GITHUB_RUN_ATTEMPT="2", GITHUB_JOB="backend-fast",
               PYTHONPATH=os.pathsep.join(filter(None, [str(ROOT), os.environ.get("PYTHONPATH")])))
    env.pop("GITHUB_STEP_SUMMARY", None)
    baseline_env = dict(env, LOCAL_BACKEND_PORT_START="31000", LOCAL_BACKEND_PORT_END="31063")
    baseline = subprocess.run([*lane_command(tmp_path, tmp_path / "serial"), "-p", "tools.test_backend_parallel"],
                              cwd=project, env=baseline_env, capture_output=True, text=True, timeout=120)
    serial = json.loads((tmp_path / "serial/report.json").read_text(encoding="utf-8"))
    assert baseline.returncode == serial["exit_code"], baseline.stdout + baseline.stderr

    def run(command, environment):
        return subprocess.run(command, cwd=project, env=environment, capture_output=True, timeout=120).returncode

    output = tmp_path / "lane"
    code = parallel.run_lane(lane_command(tmp_path, output), env, output, workers=workers,
                             work_root=tmp_path / "phases", run=run, port_base=30000, port_width=64, dist=dist)
    merged = json.loads((output / "report.json").read_text(encoding="utf-8"))
    return code, serial, merged, output


def phase_signature(report):
    return sorted((phase["nodeid"], phase["when"], phase["outcome"], phase["skip_reason"], phase["wasxfail"],
                   json.dumps(phase.get("subtest"), sort_keys=True)) for phase in report["reports"])


def test_parallel_lane_report_equals_serial_execution_evidence(tmp_path, monkeypatch):
    pytest.importorskip("xdist")
    from tools import ci_gate

    monkeypatch.setenv("CI_PYTEST_WATCHDOG_SECONDS", "600")
    code, serial, merged, output = run_lane_suite(tmp_path)
    assert code == merged["exit_code"] == serial["exit_code"] == 0
    assert merged["complete"] is True and "phase" not in merged
    for key in ("collected", "deselected", "collection_errors", "partial", "selection", "lane"):
        assert merged[key] == serial[key], key
    assert sorted(merged["selected"]) == sorted(serial["selected"])
    assert len(merged["selected"]) == 14 and len(merged["deselected"]) == 2
    assert phase_signature(merged) == phase_signature(serial)
    assert [(phase["name"], phase["selected"]) for phase in merged["execution"]["phases"]] == [
        ("parallel", 12), ("serial", 2)]
    assert merged["reports"][-1]["nodeid"].endswith("test_serial_runs_alone")
    assert merged["execution"]["dist"] == "load"
    workers = merged["execution"]["phases"][0]["workers"]
    assert sorted(workers) == ["gw0", "gw1"] and "workers" not in merged["execution"]["phases"][1]
    assert sum(worker["tests"] for worker in workers.values()) == 12
    assert min(worker["idle_tail_seconds"] for worker in workers.values()) == 0.0
    assert all(worker["busy_seconds"] >= 0 for worker in workers.values())
    spec = ci_gate.ReportSpec("backend-fast", merged["platform"],
                              python=f"{sys.version_info.major}.{sys.version_info.minor}")
    assert ci_gate.validate_report(merged, spec) == ci_gate.validate_report(serial, spec)
    # The gate reads every JSON file in a lane artifact as a lane report.
    assert sorted(path.name for path in output.rglob("*.json")) == ["report.json"]
    assert len(list(output.glob("progress-gw*-parallel.jsonl"))) == 2
    for phase in ("parallel", "serial"):
        assert "monitor stopped" in (output / f"watchdog-{phase}.log").read_text(encoding="utf-8")
    assert "# Backend: lane-fixture" in (output / "summary.md").read_text(encoding="utf-8")
    from tools import test_backend_parallel as parallel
    assert parallel.junit_outcomes(output / "junit.xml") == parallel.junit_outcomes(tmp_path / "serial/junit.xml")


def test_work_stealing_lane_report_equals_serial_execution_evidence(tmp_path):
    pytest.importorskip("xdist")
    code, serial, merged, _ = run_lane_suite(tmp_path, dist="worksteal")
    assert code == merged["exit_code"] == serial["exit_code"] == 0
    assert merged["execution"]["dist"] == "worksteal" and merged["complete"] is True
    assert sorted(merged["selected"]) == sorted(serial["selected"])
    assert phase_signature(merged) == phase_signature(serial)


def test_parallel_lane_without_serial_tests_runs_one_phase_and_keeps_failures(tmp_path):
    pytest.importorskip("xdist")
    source = LANE_SOURCE.replace(
        "@pytest.mark.serial\ndef test_serial_runs_alone():\n    assert not list(Path(ACTIVE).iterdir())\n", "").replace(
        "def test_skipped():\n    pytest.skip(\"unavailable here\")", "def test_skipped():\n    assert False")
    code, serial, merged, _ = run_lane_suite(tmp_path, source)
    assert code == merged["exit_code"] == serial["exit_code"] == 1
    assert [phase["name"] for phase in merged["execution"]["phases"]] == ["parallel"]
    assert phase_signature(merged) == phase_signature(serial)


def test_lane_phase_command_keeps_rootdir_inputs_and_trailing_pytest_arguments(tmp_path):
    from tools import test_backend_parallel as parallel

    command = ["python", "-m", "pytest", "--ci-output-dir", "out", "--junitxml", "out/junit.xml", "-k", "name"]
    directory = tmp_path / "parallel"
    result = parallel.lane_phase_command(command, directory, "parallel", 4)
    assert str(directory) not in result and "out" not in result
    assert f"--ci-output-dir={directory}" in result and f"--junitxml={directory / 'junit.xml'}" in result
    assert result[result.index("-n") + 1] == "4" and result[result.index("--ci-phase") + 1] == "parallel"
    assert result[-2:] == ["-k", "name"]
    serial = parallel.lane_phase_command(command, directory, "serial", 4)
    assert serial[serial.index("-n") + 1] == "0" and serial[serial.index("--ci-phase") + 1] == "serial"
    assert "--dist=load" in result and "--dist=load" in serial
    stealing = parallel.lane_phase_command(command, directory, "parallel", 4, "worksteal")
    assert "--dist=worksteal" in stealing and "--dist=load" not in stealing
    # The serial phase is one process, so its scheduler never varies.
    assert parallel.lane_phase_command(command, directory, "serial", 4, "worksteal") == serial
    with pytest.raises(ValueError, match="scheduler"):
        parallel.lane_phase_command(command, directory, "parallel", 4, "loadfile")


def journal_lines(started_at, *tests):
    lines = [{"event": "session_start", "elapsed_seconds": 0.0, **({} if started_at is None else {"started_at": started_at})}]
    for node, start, finish in tests:
        lines += [{"event": "test_start", "elapsed_seconds": start, "nodeid": node},
                  {"event": "test_finish", "elapsed_seconds": finish, "nodeid": node}]
    return "\n".join(json.dumps(line) for line in lines) + "\n"


def test_worker_timings_align_workers_on_their_session_start(tmp_path):
    from tools import test_backend_parallel as parallel

    (tmp_path / "progress-gw0.jsonl").write_text(journal_lines(100.0, ("a", 1.0, 4.0), ("b", 4.0, 10.0)), encoding="utf-8")
    (tmp_path / "progress-gw1.jsonl").write_text(journal_lines(102.0, ("c", 0.5, 3.0)) + "torn", encoding="utf-8")
    (tmp_path / "progress-gw2.jsonl").write_text(journal_lines(101.0), encoding="utf-8")
    timings = parallel.worker_timings(sorted(tmp_path.glob("progress-gw*.jsonl")))
    assert timings == {
        "gw0": {"tests": 2, "busy_seconds": 9.0, "first_start_seconds": 1.0, "last_finish_seconds": 10.0,
                "idle_tail_seconds": 0.0},
        "gw1": {"tests": 1, "busy_seconds": 2.5, "first_start_seconds": 2.5, "last_finish_seconds": 5.0,
                "idle_tail_seconds": 5.0},
        "gw2": {"tests": 0, "busy_seconds": 0.0, "first_start_seconds": None, "last_finish_seconds": None,
                "idle_tail_seconds": 10.0},
    }
    (tmp_path / "progress-gw2.jsonl").write_text(journal_lines(None), encoding="utf-8")
    assert parallel.worker_timings(sorted(tmp_path.glob("progress-gw*.jsonl"))) is None
    assert parallel.worker_timings([]) is None


def phase_report(name, selected, deferred, **changes):
    report = {"schema": "ci-test-report/v1", "lane": "lane", "collected": [{"nodeid": node} for node in ("a", "b", "c")],
              "deselected": ["c"], "selected": selected, "reports": [{"nodeid": node} for node in selected],
              "collection_errors": [], "exit_code": 0, "wall_seconds": 2.0, "complete": True,
              "phase": {"name": name, "deferred": deferred}}
    for key in ("source_commit", "workflow_run_id", "run_attempt", "job_id", "platform", "python",
                "python_implementation", "pytest", "profile", "selection_profile", "collect_only", "partial", "selection"):
        report[key] = ""
    report.update(changes)
    return report


def test_lane_merge_requires_an_exact_partition_of_the_lane_selection():
    from tools import test_backend_parallel as parallel

    merged = parallel.merge_lane_reports([phase_report("parallel", ["a"], ["b"]), phase_report("serial", ["b"], ["a"])], 4)
    assert merged["selected"] == ["a", "b"] and merged["wall_seconds"] == 4.0 and merged["execution"]["workers"] == 4
    for reports, message in (
        ([phase_report("parallel", ["a"], ["b"])], "partition"),
        ([phase_report("parallel", ["a"], ["b"]), phase_report("serial", ["a", "b"], ["a"])], "more than one phase"),
        ([phase_report("parallel", ["a"], ["b"]), phase_report("serial", [], ["a"])], "partition"),
        ([phase_report("parallel", ["a"], ["b"]), phase_report("serial", ["b"], ["a"], deselected=[])], "identity or collection"),
        ([phase_report("serial", ["b"], ["a"])], "parallel phase"),
        ([phase_report("parallel", ["a"], [], phase=None)], "parallel phase"),
    ):
        if reports[0]["phase"] is None:
            del reports[0]["phase"]
        with pytest.raises(ValueError, match=message):
            parallel.merge_lane_reports(reports, 4)
    failed = parallel.merge_lane_reports([phase_report("parallel", ["a"], ["b"], exit_code=1),
                                          phase_report("serial", ["b"], ["a"], complete=False)], 4)
    assert failed["exit_code"] == 1 and failed["complete"] is False
    empty = parallel.merge_lane_reports([phase_report("parallel", [], ["b"], exit_code=5),
                                         phase_report("serial", ["b"], [])], 4)
    assert empty["exit_code"] == 0


def test_distributed_controller_requires_identical_worker_collections():
    from tools import ci_pytest

    options = {"numprocesses": 2, "ci_windows_shard": None, "ci_lane": None, "ci_shard": None, "ci_phase": "parallel"}
    config = SimpleNamespace(getoption=lambda name, default=None: options.get(name, default))
    view = {"collected": [{"nodeid": "a"}], "selected": ["a"], "deselected": [], "deferred": []}
    evidence = ci_pytest.Evidence(config)
    evidence.worker_collections = {"gw0": view, "gw1": dict(view)}
    assert evidence.adopt_worker_collection() is True and evidence.selected == ["a"]
    for collections, failures in (({"gw0": view, "gw1": dict(view, selected=[])}, []),
                                  ({"gw0": view}, []), ({"gw0": view, "gw1": view}, ["gw2"]), ({}, [])):
        evidence = ci_pytest.Evidence(config)
        evidence.worker_collections, evidence.worker_failures = collections, failures
        assert evidence.adopt_worker_collection() is False


WATCHDOG_SAMPLE = """import os, time
from pathlib import Path
import pytest

@pytest.mark.parametrize("case", [0, 1])
def test_slow(case, request):
    directory = Path(os.environ["CI_REPORT_DIR"])
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    journal = "progress-" + worker if worker else "progress"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            lines = (directory / "watchdog.log").read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        if any(journal + ": no progress" in line and request.node.nodeid in line
               and "phase=call" in line for line in lines):
            return
        time.sleep(0.02)
    pytest.fail("external watchdog did not report this test")
"""


@pytest.mark.parametrize("workers", [0, 2])
def test_external_watchdog_reports_stalled_test_without_terminating_it(tmp_path, workers):
    if workers:
        pytest.importorskip("xdist")
    arguments = ["-n", str(workers), "--max-worker-restart=0"] if workers else []
    result, report, destination = run_suite(
        tmp_path, WATCHDOG_SAMPLE, *arguments,
        "-o", "faulthandler_timeout=0",
        environment={"CI_PYTEST_WATCHDOG_SECONDS": "0.1"},
    )
    assert result.returncode == report["exit_code"] == 0, result.stdout + result.stderr
    log = (destination / "watchdog.log").read_text(encoding="utf-8")
    assert "no progress for" in log
    assert "test_sample.py::test_slow[0]" in log
    assert "test_sample.py::test_slow[1]" in log
    assert "phase=call" in log
    assert "monitor stopped" in log
    assert "Timeout (" not in result.stderr
    assert "no progress for" in result.stdout
    if workers:
        assert "progress-gw0: no progress" in log
        assert "progress-gw1: no progress" in log
        assert log.count("external progress monitor started") == 1


def test_external_watchdog_preserves_test_failure(tmp_path):
    result, report, destination = run_suite(
        tmp_path, "def test_bad(): assert False\n",
        "-o", "faulthandler_timeout=0",
        environment={"CI_PYTEST_WATCHDOG_SECONDS": "0.1"},
    )
    assert result.returncode == report["exit_code"] == 1
    assert "monitor stopped" in (destination / "watchdog.log").read_text(encoding="utf-8")


def test_watchdog_journal_handles_partial_records_and_resumed_progress(tmp_path):
    from tools.ci_pytest_watchdog import Journal
    path = tmp_path / "progress-gw0.jsonl"
    journal = Journal(0)
    path.write_bytes(b'{"event":"phase_start","nodeid":"test_slow","when":"call"}\n{"event":')
    journal.poll(path, 1)
    assert journal.warning("gw0", 2, 2) is None
    assert "test_slow" in journal.warning("gw0", 3, 2)
    assert journal.warning("gw0", 4, 2) is None
    with path.open("ab") as stream:
        stream.write(b'"test_finish","nodeid":"test_slow"}\n')
    journal.poll(path, 5)
    assert journal.warning("gw0", 6, 2) is None
    with path.open("ab") as stream:
        stream.write(b'{"event":"session_finish"}\n')
    journal.poll(path, 7)
    assert journal.warning("gw0", 100, 2) is None


def test_external_watchdog_exits_if_controller_dies(tmp_path):
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    script = (
        "from pathlib import Path\n"
        "import os, sys, time\n"
        "from tools.ci_pytest_watchdog import start\n"
        "directory = Path(sys.argv[1])\n"
        "process = start(directory, 0.1, distributed=False)\n"
        "deadline = time.monotonic() + 5\n"
        "while not (directory / 'watchdog.log').is_file() and time.monotonic() < deadline:\n"
        "    time.sleep(0.02)\n"
        "os._exit(17)\n"
    )
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)],
                            env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 17
    assert "monitor stopped" in (tmp_path / "watchdog.log").read_text(encoding="utf-8")


def test_live_annotations_share_job_budget_and_omit_payloads(tmp_path):
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    env = {"GITHUB_ACTIONS": "true", "GITHUB_WORKSPACE": str(tmp_path),
           "RUNNER_TEMP": str(runner_temp)}
    source = "import pytest\n" + "\n".join(
        f"def test_failure_{i}(): raise ValueError('sensitive payload')" for i in range(10))
    first, report, _ = run_suite(tmp_path, source, "-q", "-k", "failure_0 or failure_1", environment=env)
    second, second_report, _ = run_suite(tmp_path, source, "-q", environment=env | {"HYPOTHESIS_PROFILE": "other"})
    third, _, _ = run_suite(tmp_path, source, "-q", environment=env)
    commands = [line for result in (first, second, third) for line in result.stdout.splitlines()
                if line.startswith(("::error", "::notice"))]
    assert sum(line.startswith("::error") for line in commands) == 8
    assert sum(line.startswith("::notice") for line in commands) == 1
    assert all("sensitive payload" not in line for line in commands)
    assert all("file=test_sample.py,line=" in line for line in commands if line.startswith("::error"))
    assert first.returncode == report["exit_code"] == second.returncode == second_report["exit_code"] == 1
    assert sum(row["outcome"] == "failed" for row in second_report["reports"]) == 10


def test_annotation_command_escaping():
    from tools.ci_pytest import _command_escape
    assert _command_escape("%\r\n::error::") == "%25%0D%0A::error::"
    assert _command_escape("a:b,c%\n", property_value=True) == "a%3Ab%2Cc%25%0A"


def test_distributed_annotations_deduplicate_shared_fixture_failures(tmp_path):
    pytest.importorskip("xdist")
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    env = {"GITHUB_ACTIONS": "true", "GITHUB_WORKSPACE": str(tmp_path),
           "RUNNER_TEMP": str(runner_temp)}
    source = ("import pytest\n"
              "@pytest.fixture\ndef broken(): raise RuntimeError('private fixture data')\n"
              "@pytest.mark.parametrize('value', range(4))\n"
              "def test_broken(broken, value): pass\n"
              "@pytest.mark.parametrize('value', ['private-parameter'])\n"
              "def test_call(value): assert False\n")
    result, report, _ = run_suite(tmp_path, source, "-q", "-n", "2", environment=env)
    commands = [line for line in result.stdout.splitlines() if line.startswith("::error")]
    assert len(commands) == 2, result.stdout
    assert any("setup |" in line and "RuntimeError" in line for line in commands)
    assert any("call |" in line and "test_call[id:" in line for line in commands)
    assert all("private" not in line for line in commands)
    assert sum(row["outcome"] == "failed" for row in report["reports"]) == 5
    assert result.returncode == report["exit_code"] == 1


def test_failure_annotation_is_streamed_before_pytest_finishes(tmp_path):
    import threading

    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "test_live.py").write_text(
        "from pathlib import Path\nimport time\n"
        "def test_first(): assert False\n"
        "def test_wait_for_annotation():\n"
        " deadline = time.monotonic() + 25\n"
        " while not Path('release').exists() and time.monotonic() < deadline: time.sleep(0.02)\n"
        " assert Path('release').exists()\n", encoding="utf-8")
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTEST_ADDOPTS="", GITHUB_ACTIONS="true",
               GITHUB_WORKSPACE=str(tmp_path), RUNNER_TEMP=str(runner_temp), GITHUB_JOB="live-fixture",
               CI_REPORT_DIR=str(tmp_path / "evidence"), CI_PYTEST_WATCHDOG_SECONDS="", GITHUB_STEP_SUMMARY="")
    for name in ("PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT", "PYTEST_XDIST_TESTRUNUID"):
        env.pop(name, None)
    source = ("import os, sys; from pathlib import Path; from tools import test_backend as runner; "
              "runner.ROOT = Path.cwd(); sys.exit(runner.run_streaming("
              "[sys.executable, '-m', 'pytest', '-q', '-p', 'tools.ci_pytest'], os.environ, Path('logs')))")
    process = subprocess.Popen([sys.executable, "-c", source], cwd=tmp_path, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines = []
    seen = threading.Event()

    def read_output():
        for line in process.stdout:
            lines.append(line)
            if line.startswith("::error"):
                seen.set()

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    try:
        assert seen.wait(20), "Failure annotation did not reach the streaming parent"
        assert process.poll() is None
        assert not (tmp_path / "evidence/report.json").exists()
    finally:
        (tmp_path / "release").touch()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        reader.join(timeout=5)
        process.stdout.close()
    assert process.returncode == 1, "".join(lines)
    report = json.loads((tmp_path / "evidence/report.json").read_text(encoding="utf-8"))
    assert sum(row["outcome"] == "failed" for row in report["reports"]) == 1
    assert "::error file=test_live.py" in (tmp_path / "logs/run.log").read_text(encoding="utf-8")
