"""Reject incomplete, failed, or unreadable baseline evidence."""

from copy import deepcopy

import pytest

from tools.qualify_analytics_baseline import baseline_passed, counts


def successful_run() -> dict:
    return {
        "exit_code": 0, "timed_out": False,
        "counts": {"tests": 2, "failures": 0, "errors": 0, "skipped": 0},
    }


def test_baseline_requires_every_planned_run() -> None:
    assert not baseline_passed([], 0)
    assert not baseline_passed([successful_run()], 2)
    assert baseline_passed([successful_run(), successful_run()], 2)


@pytest.mark.parametrize("fault", ["exit", "timeout", "missing", "empty", "failure", "error"])
def test_baseline_rejects_incomplete_or_failed_evidence(fault: str) -> None:
    run = deepcopy(successful_run())
    if fault == "exit":
        run["exit_code"] = 1
    elif fault == "timeout":
        run["timed_out"] = True
    elif fault == "missing":
        run["counts"] = None
    elif fault == "empty":
        run["counts"]["tests"] = 0
    elif fault == "failure":
        run["counts"]["failures"] = 1
    elif fault == "error":
        run["counts"]["errors"] = 1
    assert not baseline_passed([run], 1)


def test_junit_counts_include_all_suites(tmp_path) -> None:
    path = tmp_path / "results.xml"
    path.write_text('<testsuites><testsuite tests="3" failures="1" errors="0" skipped="1"/>'
                    '<testsuite tests="2" failures="0" errors="1" skipped="0"/></testsuites>')
    assert counts(path) == {"tests": 5, "failures": 1, "errors": 1, "skipped": 1}


@pytest.mark.parametrize("content", [None, "<testsuites>"])
def test_absent_or_partial_junit_is_not_a_pass(tmp_path, content) -> None:
    path = tmp_path / "results.xml"
    if content is not None:
        path.write_text(content)
    assert counts(path) is None


def test_entirely_skipped_suite_does_not_qualify() -> None:
    run = successful_run()
    run["counts"]["skipped"] = run["counts"]["tests"]
    assert not baseline_passed([run], 1)


@pytest.mark.parametrize("missing", ["revision", "status"])
def test_unidentified_checkout_cannot_produce_evidence(tmp_path, monkeypatch, missing) -> None:
    from tools import qualify_analytics_baseline as qualification

    def git_result(*args):
        if args[0] == "rev-parse":
            return None if missing == "revision" else "a" * 40
        return None if missing == "status" else ""

    output = tmp_path / "baseline"
    monkeypatch.setattr(qualification, "git_output", git_result)
    monkeypatch.setattr(qualification.sys, "argv", ["qualify", "--output", str(output)])
    with pytest.raises(SystemExit) as error:
        qualification.main()
    assert error.value.code == 2
    assert not output.exists()
