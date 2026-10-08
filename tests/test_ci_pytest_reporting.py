"""Execution-level checks for CI evidence, independent of application fixtures."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.ci_tier("fast")


ROOT = Path(__file__).resolve().parents[1]


def run_suite(tmp_path, source, *arguments):
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers = omitted: selection fixture\n", encoding="utf-8")
    (tmp_path / "test_sample.py").write_text(source, encoding="utf-8")
    destination = tmp_path / "evidence"
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTEST_ADDOPTS="", CI_TEST_LANE="report-fixture", CI_REPORT_DIR=str(destination),
               PRODUCT_SHA="a" * 40, GITHUB_RUN_ID="123", GITHUB_RUN_ATTEMPT="2", GITHUB_JOB="backend-fast",
               GITHUB_STEP_SUMMARY=str(tmp_path / "github-summary.md"))
    result = subprocess.run([sys.executable, "-m", "pytest", "-p", "tools.ci_pytest", "--junitxml=" + str(tmp_path / "original.xml"),
                             *arguments], cwd=tmp_path, env=env, text=True, capture_output=True, timeout=30)
    report = json.loads((destination / "report.json").read_text(encoding="utf-8"))
    return result, report, destination


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
    spec = ci_gate.ReportSpec("backend-fast", report["platform"])
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
