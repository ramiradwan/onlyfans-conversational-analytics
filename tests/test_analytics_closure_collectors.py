"""Small executable collector checks; these are not capacity measurements."""
from copy import deepcopy
import os
from pathlib import Path

import pytest

from tools import analytics_qualification as q
from tools.analytics_qualification_worker import collect

pytestmark = [pytest.mark.ci_tier('integration')]

ROOT = Path(__file__).resolve().parents[1]


def configuration(tmp_path, mode, case="empty"):
    manifest = q.read_json(ROOT / "docs/analytics/acceptance-manifest.json")
    # Deliberately different, test-only inputs. The frozen manifest verifier rejects them.
    for profile in manifest["profiles"].values():
        profile["numeric_latency_gates"] = False  # Unit tests are not reference-profile measurements.
    manifest["history"].update(batches=2, messages_per_batch=10)
    manifest["questions"].update(messages=1000, idle_seconds=0.01)
    manifest["visibility"].update(messages=1000, idle_seconds=0.01)
    subject = {"revision": "unit-test", "files": {"app/analytics/pipeline.py": q.file_digest(ROOT / "app/analytics/pipeline.py")}}
    return {"subject": subject, "mode": mode, "output": str(tmp_path / "collector"),
            "data": str(tmp_path / "collector/data"), "manifest": manifest,
            "subject_directory": str(ROOT), "subject_files": {"app/analytics/pipeline.py": q.file_digest(ROOT / "app/analytics/pipeline.py")},
            "subject_sha256": q.digest(subject), "messages": 1000, "case": case,
            "state": "fresh", "repeat": 0, "known_kinds": True,
            "entry_point": str(ROOT / "tools/qualify_analytics_baseline.py")}


def raw_result(tmp_path, config, report):
    attempt = tmp_path / "raw-attempt"
    attempt.mkdir()
    input_path = attempt / "worker-input.json"
    q.write_once(input_path, config)
    references = [dict(q.attach(attempt, input_path), name="worker-input.json")]
    output = Path(config["output"])
    for path in sorted(output.rglob("*.json")):
        if "data" not in path.relative_to(output).parts:
            references.append(dict(q.attach(attempt, path), name=path.relative_to(output).as_posix()))
    return attempt, {"payload": report, "attachments": references, "subject": config["subject"],
                     "subject_after_sha256": config["subject_sha256"], "process_instance": "test-owner"}


@pytest.mark.parametrize("case", ["empty", "populated", "tied_time", "generation_bound_pagination"])
def test_question_collector_records_fresh_answers_and_failures(tmp_path, monkeypatch, case):
    monkeypatch.setenv("OFCA_QUALIFICATION_PROCESS", "test-owner")
    config = configuration(tmp_path, "questions", case)
    report = collect(config)
    assert report["complete"], report
    assert report["collector_process"]["pid"] == os.getpid()
    assert report["process_instance"].split(":")[0] != str(os.getpid())
    assert all(call["process_instance"] == report["process_instance"] for call in report["calls"])
    # Self-tests do not assume qualification hardware. A real request-budget
    # failure must remain a failed sample, not make the harness invent an answer.
    failures = [call for call in report["calls"] if call["error"] is not None]
    successes = [call for call in report["calls"] if call["error"] is None]
    assert successes and all(call["correct"] for call in successes), report
    assert all(call["error"] == "analytics_question_limit_exceeded"
               and call["correct"] is False and call["actual"] is None for call in failures), report
    errors = q.check_questions(config["manifest"], f"questions/reference-windows-16g/{case}/fresh", report)
    if failures or report["first_query"]["error"] is not None:
        assert errors, "failed product queries must fail qualification"
        if any(call["phase"] == "measured" for call in failures):
            assert report["statistics"]["p95_seconds"] is None
    else:
        assert not errors, report
    assert report["scheduler_closed"] and report["detached_workers"] == 0
    assert report["preparation"]["independent_rebuild_equal"]
    assert len(list((tmp_path / "collector/fresh-process/events").glob("*-question.json"))) == 105
    attempt, raw = raw_result(tmp_path, config, report)
    assert not q.check_collector_evidence(attempt, raw, config["manifest"])
    raw["attachments"] = [x for x in raw["attachments"] if not x["name"].endswith("-first-query.json")]
    assert q.check_collector_evidence(attempt, raw, config["manifest"])


def test_six_phase_collector_has_independent_results_and_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv("OFCA_QUALIFICATION_PROCESS", "test-owner")
    config = configuration(tmp_path, "matrix")
    report = collect(config)
    assert report["complete"], report
    assert_matrix_recording(config["manifest"], report)
    events = list((tmp_path / "collector/events").glob("*-phase.json"))
    assert len(events) == 6
    assert all(q.read_json(path)["value"]["expected"] == q.read_json(path)["value"]["actual"] for path in events)
    attempt, raw = raw_result(tmp_path, config, report)
    assert not q.check_collector_evidence(attempt, raw, config["manifest"])
    removed = next(x["name"] for x in raw["attachments"] if x["name"].endswith("-phase.json"))
    raw["attachments"] = [x for x in raw["attachments"] if x["name"] != removed]
    assert q.check_collector_evidence(attempt, raw, config["manifest"])


def test_visibility_restarts_the_interpreter_not_only_backend_objects(tmp_path, monkeypatch):
    monkeypatch.setenv("OFCA_QUALIFICATION_PROCESS", "test-owner")
    import time
    from tools.analytics_qualification_execution import StateBudget
    config = configuration(tmp_path, "visibility")
    config["execution_schedule"] = dict(config["manifest"]["visibility_execution"],
        directory=str(Path(config["output"]) / "execution"))
    budget = StateBudget(config["execution_schedule"], started=time.monotonic(), token="test-owner")
    report = collect(config)
    ended = time.monotonic()
    budget.poll(ended, finished=True)
    assert report["complete"], report
    assert len(set(report["runtime_processes"])) == 2
    assert report["probes"][-1]["process_instance"] != report["probes"][0]["process_instance"]
    assert not q.check_visibility(config["manifest"], "visibility/reference-windows-16g/0", report), report
    assert report["restart_scheduler_closed"] and report["restart_detached_workers"] == 0
    attempt, raw = raw_result(tmp_path, config, report)
    raw.update(execution=budget.report(ended), seconds=ended-budget.started,
               maximum_seconds=config["execution_schedule"]["maximum_worker_seconds"])
    assert not q.check_collector_evidence(attempt, raw, config["manifest"])
    raw["execution"]["intervals"][1]["ended"] = raw["execution"]["intervals"][1]["started"]
    assert q.check_collector_evidence(attempt, raw, config["manifest"])


def test_source_change_fails_before_fixture_creation(tmp_path):
    config = configuration(tmp_path, "matrix")
    config["subject_files"]["app/analytics/pipeline.py"] = "0" * 64
    with pytest.raises(ValueError, match="subject_source_mismatch"):
        collect(config)
    assert not Path(config["data"]).exists()


def assert_matrix_recording(manifest, report):
    """Check recording without requiring product latency on an unqualified host."""
    failed_queries = []
    for phase in report["phases"]:
        clocks = phase["clocks"]
        if clocks["first_valid_visible_result"] is not None:
            continue
        observation = phase.get("observation", {})
        assert observation.get("error") == "analytics_question_limit_exceeded", phase
        assert observation.get("current") is False, phase
        # Only the missing successful observation is expected. Other clocks,
        # canonical equality, cleanup, source counts and backlog must still pass.
        names = ["operation_started", "activation", "required_cleanup_complete",
                 "backlog_drained", "operation_finished"]
        values = [clocks.get(name) for name in names]
        assert all(q.finite(value) for value in values), phase
        assert values == sorted(values), phase
        if phase["phase"] not in ("cold", "unchanged_rebuild"):
            committed = clocks.get("durable_canonical_commit")
            assert q.finite(committed) and values[0] <= committed <= values[1], phase
        failed_queries.append(phase["phase"])
    errors = q.check_matrix(manifest, "mutation/reference-windows-16g/1000", report)
    assert errors == ["missing_or_invalid_operation_clock"] * len(failed_queries), {
        "errors": errors, "phases": [{"phase": p["phase"], "clocks": p["clocks"],
        "observation": p.get("observation")} for p in report["phases"]]}
    # The original report is not changed or promoted: every failed query still
    # makes the frozen qualification verifier fail.
    if failed_queries:
        assert errors


def test_recorded_query_failure_still_fails_matrix_qualification():
    from tests.test_analytics_closure_qualification import MANIFEST, matrix
    report = matrix(1000)
    report["phases"][0]["clocks"]["first_valid_visible_result"] = None
    report["phases"][0]["observation"] = {
        "error": "analytics_question_limit_exceeded", "current": False}
    original = deepcopy(report)
    assert_matrix_recording(MANIFEST, report)
    assert report == original
    assert q.check_matrix(MANIFEST, "mutation/reference-windows-16g/1000", report)


@pytest.mark.parametrize("fault", ["activation", "required_cleanup_complete",
                                    "backlog_drained", "operation_finished", "unexplained_query"])
def test_query_failure_cannot_hide_a_recording_defect(fault):
    from tests.test_analytics_closure_qualification import MANIFEST, matrix
    report = matrix(1000)
    phase = report["phases"][0]
    phase["clocks"]["first_valid_visible_result"] = None
    phase["observation"] = {"error": "analytics_question_limit_exceeded", "current": False}
    if fault == "unexplained_query":
        phase["observation"]["error"] = None
    else:
        phase["clocks"][fault] = None
    with pytest.raises(AssertionError):
        assert_matrix_recording(MANIFEST, report)
