"""Execution-level checks for CI evidence, independent of application fixtures."""
from __future__ import annotations

import json
import os
import subprocess
import sys
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
