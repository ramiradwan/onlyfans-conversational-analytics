"""Falsifiers for the frozen A07 acceptance protocol; not capacity evidence."""
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from tools import analytics_qualification as q

MANIFEST = q.read_json(Path(__file__).resolve().parents[1] / "docs/analytics/acceptance-manifest.json")


def source():
    return {"revision": "a" * 40, "working_tree": "", "signature_valid": True,
            "signer": "ADO-agent <269472843+ADO-agent@users.noreply.github.com>", "files": {"a.py": "b" * 64}}


@pytest.fixture
def evidence(tmp_path):
    directory = tmp_path / "evidence"
    context = q.initialize(directory, MANIFEST, source(), {"python": "test fixture"})
    session = q.session(directory, context)
    return directory, context, session


def clocks(elapsed=1.0):
    return {"operation_started": 0.0, "durable_canonical_commit": 0.1,
            "activation": 0.2, "first_valid_visible_result": elapsed + 0.1,
            "required_cleanup_complete": elapsed + 0.1, "backlog_drained": elapsed + 0.1,
            "operation_finished": elapsed + 0.1}


def test_missing_jobs_cannot_pass(evidence):
    directory, _, _ = evidence
    result = q.verify(directory, MANIFEST)
    assert result["status"] == "BLOCKED"
    assert len(result["jobs"]) == 38
    assert all(x["status"] != "PASS" for x in result["jobs"].values())


def test_interrupted_attempt_remains_failed_after_resume(evidence):
    directory, context, session = evidence
    q.begin_attempt(directory, context, session, "regression")
    resumed = q.session(directory, context)
    assert resumed != session
    attempt = q.begin_attempt(directory, context, resumed, "regression")
    q.write_once(attempt / "result.json", {"job": "regression", "context_sha256": q.digest(context),
                 "status": "BLOCKED", "worker_started": False, "reason": "unavailable prerequisite"})
    result = q.verify(directory, MANIFEST)
    assert result["status"] == "FAIL"
    assert result["jobs"]["regression"]["attempts"] == 2


def test_records_cannot_be_overwritten(tmp_path):
    path = tmp_path / "receipt.json"
    q.write_once(path, {"status": "FAIL"})
    with pytest.raises(FileExistsError):
        q.write_once(path, {"status": "PASS"})
    assert q.read_json(path)["status"] == "FAIL"


def visibility(profile="reference-windows-16g"):
    return {"initial_messages": 100000, "scheduler_closed": True, "restart_scheduler_closed": True,
            "detached_workers": 0, "restart_detached_workers": 0, "backlog": 0, "restart_backlog": 0, "probes": [{"case": case, "clocks": clocks(), "backlog_before": 0,
             "backlog_after": 0, "valid_current_result": True, "cleanup_complete": True}
             for case in MANIFEST["visibility"]["process_cases"][0]]}


def test_every_visibility_probe_includes_cleanup():
    data = visibility()
    job = "visibility/reference-windows-16g/0"
    assert not q.check_visibility(MANIFEST, job, data)
    data["probes"][2]["clocks"] = clocks(16.8896784)
    assert q.check_visibility(MANIFEST, job, data)
    data["probes"][2]["clocks"]["first_valid_visible_result"] = 0.5
    assert q.check_visibility(MANIFEST, job, data)


def test_constrained_profile_does_not_inherit_reference_latency():
    data = visibility()
    data["probes"][0]["clocks"] = clocks(16.8896784)
    assert not q.check_visibility(MANIFEST, "visibility/constrained-windows-8g/0", data)


@pytest.mark.parametrize("field", ["activation", "durable_canonical_commit", "required_cleanup_complete", "backlog_drained"])
def test_missing_clocks_fail(field):
    data = {"clocks": clocks()}
    del data["clocks"][field]
    assert q.timestamps(data, mutation=True)


def questions():
    identity = uuid4().hex
    data = {"initial_messages": 100000, "case": "empty", "state": "fresh", "page_size": 50,
            "question": "no_later_creator_reply.v1", "filters": __import__("tools.analytics_qualification_fixture", fromlist=["question_plan"]).question_plan(MANIFEST, "empty")["filters"],
            "process_instance": identity, "cold_readiness_seconds": 403.0,
            "first_query": {"seconds": 0.2, "error": None, "correct": True, "actual": [],
                            "process_instance": identity, "records_examined": 407, "truncated": False},
            "plan": __import__("tools.analytics_qualification_fixture", fromlist=["question_plan"]).question_plan(MANIFEST, "empty"),
            "expected": [], "scheduler_closed": True, "detached_workers": 0, "backlog": 0, "pricing_disabled": True,
            "calls": [{"index": index, "phase": "warmup" if index < 5 else "measured",
                       "process_instance": identity, "seconds": 0.1, "error": None,
                       "correct": True, "records_examined": 407, "truncated": False,
                       "rows": 0, "actual": [], "has_more": False, "undetermined_conversations": 0} for index in range(105)]}
    data["statistics"] = q.question_statistics(data["calls"], 5)
    return data


@pytest.mark.parametrize("fault", ["errors", "restart", "missing", "nan", "work_limit", "p95"])
def test_warm_samples_fail_closed(fault):
    data = questions()
    job = "questions/reference-windows-16g/empty/fresh"
    assert not q.check_questions(MANIFEST, job, data)
    if fault == "errors":
        for call in data["calls"][5:]:
            call["error"] = "analytics_question_limit_exceeded"
    elif fault == "restart":
        data["calls"][-1]["process_instance"] = uuid4().hex
    elif fault == "missing":
        data["calls"].pop()
    elif fault == "nan":
        data["calls"][-1]["seconds"] = float("nan")
    elif fault == "work_limit":
        data["calls"][-1]["records_examined"] = 10001
    else:
        for call in data["calls"][-6:]:
            call["seconds"] = 1.001
    assert q.check_questions(MANIFEST, job, data)


def matrix(size):
    count, revision = size, 1
    phases = []
    for name, (delta, revisions) in zip(MANIFEST["phases"], [(0, 0), (0, 0), (1, 1), (0, 1), (-100, 100), (10100, 100)]):
        before = {"messages": count, "revision": revision}
        count, revision = count + delta, revision + revisions
        phases.append({"phase": name, "source_before": before,
                       "source_after": {"messages": count, "revision": revision},
                       "clocks": clocks(), "independent_rebuild_equal": True,
                       "persisted_content_revalidated": True, "stale_reference_rejected": True})
    return {"phases": phases, "backlog": 0, "detached_workers": 0,
            "scheduler_closed": True, "no_unpermitted_orphans": True}


@pytest.mark.parametrize("size", [10000, 100000])
@pytest.mark.parametrize("fault", ["missing_phase", "counts", "oracle", "cleanup", "backlog"])
def test_matrix_requires_complete_verified_work(size, fault):
    data = matrix(size)
    job = f"mutation/reference-windows-16g/{size}"
    assert not q.check_matrix(MANIFEST, job, data)
    if fault == "missing_phase":
        data["phases"] = data["phases"][:3]
    elif fault == "counts":
        data["phases"][-1]["source_after"]["messages"] -= 1
    elif fault == "oracle":
        data["phases"][-1]["independent_rebuild_equal"] = False
    elif fault == "cleanup":
        data["scheduler_closed"] = False
    else:
        data["backlog"] = 1
    assert q.check_matrix(MANIFEST, job, data)


def test_source_change_invalidates_run(evidence):
    directory, _, _ = evidence
    changed = source()
    changed["files"]["a.py"] = "c" * 64
    assert q.verify(directory, MANIFEST, current_source=changed)["status"] == "FAIL"


def test_manifest_change_invalidates_run(evidence):
    directory, _, _ = evidence
    changed = deepcopy(MANIFEST)
    changed["limits"]["visibility_seconds"] = 20
    assert q.verify(directory, changed)["status"] == "FAIL"


def test_serial_owner_lock_is_exclusive(tmp_path):
    with q.owner_lock(tmp_path / "owner.lock"):
        with pytest.raises(OSError):
            with q.owner_lock(tmp_path / "owner.lock"):
                pytest.fail("second owner admitted")


@pytest.mark.parametrize("body", ['{"status":"FAIL","status":"PASS"}', '{"seconds":NaN}', '{"seconds":Infinity}'])
def test_ambiguous_json_is_rejected(tmp_path, body):
    path = tmp_path / "ambiguous.json"
    path.write_text(body)
    with pytest.raises(ValueError):
        q.read_json(path)


def test_unknown_status_cannot_pass():
    assert q.combine(["SUCCESS"]) == "FAIL"


def test_killed_worker_cannot_be_relabelled_blocked(evidence):
    directory, context, session = evidence
    attempt = q.begin_attempt(directory, context, session, "regression")
    q.write_once(attempt / "result.json", {"job": "regression", "context_sha256": q.digest(context),
        "status": "BLOCKED", "worker_started": True, "exit_code": -9, "reason": "worker disappeared"})
    assert q.verify(directory, MANIFEST)["jobs"]["regression"]["status"] == "FAIL"


def test_malformed_receipt_does_not_crash_verifier(evidence):
    directory, context, session = evidence
    attempt = q.begin_attempt(directory, context, session, "regression")
    q.write_once(attempt / "result.json", [])
    assert q.verify(directory, MANIFEST)["status"] == "FAIL"


def regression_attempt(evidence):
    from tools.qualify_analytics_baseline import RUNS
    directory, context, session = evidence
    attempt = q.begin_attempt(directory, context, session, "regression")
    runs, attachments = [], []
    for name, profile, tests in RUNS:
        run = {"name": name, "profile": profile, "tests": tests, "seed": 20260918,
               "exit_code": 0, "timed_out": False, "seconds": 0.01,
               "counts": {"tests": 2, "failures": 0, "errors": 0, "skipped": 0}}
        runs.append(run)
        path = attempt / "inputs" / (name + ".result.json")
        q.write_once(path, run)
        attachments.append(dict(q.attach(attempt, path), name=path.name))
        path = path.with_name(name + ".xml")
        path.write_text('<testsuites><testsuite tests="2" failures="0" errors="0" skipped="0">'
                        '<testcase name="synthetic_a"/><testcase name="synthetic_b"/>'
                        '</testsuite></testsuites>')
        attachments.append(dict(q.attach(attempt, path), name=path.name))
    payload = {"source_revision": context["source"]["revision"], "working_tree": "",
               "runs": runs, "complete": True, "passed": True}
    path = attempt / "inputs/report.json"
    q.write_once(path, payload)
    attachments.append(dict(q.attach(attempt, path), name=path.name))
    result = {"job": "regression", "context_sha256": q.digest(context), "status": "PASS",
              "payload": payload, "payload_sha256": q.digest(payload), "attachments": attachments,
              "source_after_sha256": q.digest(context["source"]), "process_instance": uuid4().hex,
              "worker_started": True, "worker_joined": True, "complete": True, "exit_code": 0,
              "timed_out": False, "seconds": 1.0, "maximum_seconds": 1800}
    q.write_once(attempt / "result.json", result)
    return attempt, result


def test_complete_regression_requires_raw_receipts_and_junit(evidence):
    directory, _, _ = evidence
    regression_attempt(evidence)
    result = q.verify(directory, MANIFEST)
    assert result["jobs"]["regression"]["status"] == "PASS"
    assert result["status"] == "BLOCKED"


@pytest.mark.parametrize("fault", ["blob_changed", "missing_phase_receipt", "junit_disagrees", "source_changed", "guard_changed"])
def test_summary_pass_cannot_replace_failed_raw_evidence(evidence, fault):
    directory, _, _ = evidence
    attempt, result = regression_attempt(evidence)
    reference = next(item for item in result["attachments"] if item["name"] == "analytics.xml")
    if fault == "blob_changed":
        (attempt / reference["path"]).write_text("corrupted")
    elif fault == "missing_phase_receipt":
        result["attachments"] = [item for item in result["attachments"] if item["name"] != "analytics.result.json"]
    elif fault == "junit_disagrees":
        wrong = attempt / "wrong.xml"
        wrong.write_text('<testsuite tests="2" failures="1" errors="0" skipped="0"/>')
        reference.update(q.attach(attempt, wrong))
    elif fault == "source_changed":
        result["source_after_sha256"] = "0" * 64
    else:
        result["maximum_seconds"] = 3600
    # Deliberate on-disk corruption bypasses the writer to exercise the reader.
    (attempt / "result.json").write_bytes(q.encoded(result))
    assert q.verify(directory, MANIFEST)["status"] == "FAIL"


@pytest.mark.parametrize("fault", ["filters", "first_process", "first_work", "boolean_count", "first_truncated"])
def test_question_bindings_reject_summary_substitutions(fault):
    data = questions()
    if fault == "filters":
        data["filters"] = {}
    elif fault == "first_process":
        data["first_query"]["process_instance"] = "another-process"
    elif fault == "first_work":
        data["first_query"]["records_examined"] = 10001
    elif fault == "boolean_count":
        data["calls"][-1]["records_examined"] = False
    else:
        data["first_query"]["truncated"] = True
    assert q.check_questions(MANIFEST, "questions/reference-windows-16g/empty/fresh", data)


def test_idle_label_without_elapsed_idle_does_not_qualify():
    data = questions()
    data["state"] = "idle"
    assert q.check_questions(MANIFEST, "questions/reference-windows-16g/empty/idle", data)


def test_post_restart_visibility_requires_joined_restart_workers():
    data = visibility()
    data["restart_detached_workers"] = 1
    assert q.check_visibility(MANIFEST, "visibility/reference-windows-16g/0", data)


def test_ci_result_is_bound_to_raw_response_and_review(evidence):
    from tools.analytics_qualification_ci import select_checks
    directory, context, session = evidence
    attempt = q.begin_attempt(directory, context, session, "source-ci")
    pages = [{"check_runs": [{"id": index, "name": name, "head_sha": source()["revision"],
              "status": "completed", "conclusion": "success", "html_url": "https://example.invalid/test"}
              for index, name in enumerate(MANIFEST["ci_jobs"])]}]
    payload = {"api_responses": pages, "requested_sha": source()["revision"], "source_unchanged": True,
               "reviewed_source_sha": source()["revision"], "review": {"source_sha256": q.digest(source())},
               "checks": select_checks(pages, MANIFEST["ci_jobs"], source()["revision"])}
    path = attempt / "ci.json"
    q.write_once(path, payload)
    result = {"payload": payload, "attachments": [dict(q.attach(attempt, path), name="ci.json")]}
    assert not q.check_ci_evidence(attempt, result, context, MANIFEST)
    payload["api_responses"][0]["check_runs"][0]["conclusion"] = "failure"
    path = attempt / "changed.json"
    q.write_once(path, payload)
    result["attachments"] = [dict(q.attach(attempt, path), name="ci.json")]
    assert q.check_ci_evidence(attempt, result, context, MANIFEST)


def test_failed_question_timings_never_become_successful_latency_statistics():
    calls = questions()["calls"]
    for call in calls:
        call.update(error="analytics_question_limit_exceeded", correct=False)
    summary = q.question_statistics(calls, 5)
    assert summary["errors"] == {"analytics_question_limit_exceeded": 100}
    assert summary["p95_seconds"] is None and summary["maximum_seconds"] is None
    assert summary["latency_unqualified_reason"]
