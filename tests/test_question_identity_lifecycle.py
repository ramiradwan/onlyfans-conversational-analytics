"""Real-time lifecycle regressions using the existing synthetic question collector."""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from tools import analytics_qualification as qualification
from tools.analytics_qualification_worker import collect
from tests.test_analytics_closure_collectors import configuration

pytestmark = [pytest.mark.ci_tier('scale')]


def record_failure(config, report, errors, output_directory):
    """Keep synthetic diagnostics without treating them as CI execution reports."""
    invalid_indices = {int(error.rsplit(":", 1)[1]) for error in errors
                       if error.startswith("invalid_question_call:")}
    expected = report.get("expected", [])[:50]
    fields = ("index", "phase", "error", "correct", "seconds", "records_examined",
              "truncated", "rows", "undetermined_conversations", "has_more")
    observations = []
    for index, call in enumerate(report.get("calls", [])):
        if index in invalid_indices:
            observations.append({"position": index, **{field: call.get(field) for field in fields},
                "expected_phase": "warmup" if index < config["manifest"]["questions"]["warmups"] else "measured",
                "actual_matches_expected": call.get("actual") == expected,
                "expected_rows": len(expected), "expected_has_more": len(report.get("expected", [])) > 50,
                "expected_undetermined_conversations": 1 if config["case"] == "tied_time" else 0,
                "request_max_records": config["manifest"]["limits"]["request_max_records"]})
    print("Question lifecycle failure details: " + json.dumps({
        "qualification_errors": errors, "invalid_calls": observations,
        "statistics": report.get("statistics"), "complete": report.get("complete"),
        "scheduler_closed": report.get("scheduler_closed"),
        "detached_workers": report.get("detached_workers"), "backlog": report.get("backlog"),
    }, sort_keys=True))
    if not output_directory:
        return
    # Only journal observations for failing calls and lifecycle transitions are
    # copied. The full payload retains all calls; databases and other tmp files
    # never enter the CI artifact. At most warmups + samples + four events apply.
    events = []
    source = Path(config["output"])
    for directory in (source / "events", source / "fresh-process/events"):
        for path in sorted(directory.glob("*.json")):
            label = path.name.split("-", 1)[-1].removesuffix(".json")
            if label not in {"readiness", "mutation", "first-query", "failure", "question"}:
                continue
            event = qualification.read_json(path)
            if label == "question" and event["value"].get("index") not in invalid_indices:
                continue
            events.append({"path": path.relative_to(source).as_posix(), "record": event})
    destination = Path(output_directory) / "diagnostics" / f"lifecycle-{config['case']}-{config['state']}.payload.txt"
    temporary = destination.with_name(destination.name + "." + uuid4().hex + ".tmp")
    try:
        qualification.write_once(temporary, {"qualification_errors": errors, "payload": report,
                                             "collector_events": events})
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Complete synthetic lifecycle diagnostics: {destination}")


@pytest.mark.ci_tier('scale')
@pytest.mark.slow
@pytest.mark.parametrize("case,state", [
    ("populated", "idle"),
    ("generation_bound_pagination", "mutated"),
])
def test_owned_questions_after_real_idle_and_mutation(tmp_path, monkeypatch, request, case, state):
    monkeypatch.setenv("OFCA_QUALIFICATION_PROCESS", "lifecycle-test-owner")
    config = configuration(tmp_path, "questions", case)
    config["state"] = state
    config["manifest"]["questions"]["idle_seconds"] = 61
    report = collect(config)
    errors = ["collector_incomplete"]
    try:
        assert report["complete"], report
        errors = qualification.check_questions(config["manifest"],
            f"questions/reference-windows-16g/{case}/{state}", report)
        assert not errors, report
        assert report["statistics"]["errors"] == {}
        assert report["scheduler_closed"] and report["detached_workers"] == 0
        if state == "idle":
            assert report["idle_seconds"] >= 61
        else:
            assert report["stale_cursor_rejected"] and report["pagination_complete"]
    except AssertionError:
        try:
            record_failure(config, report, errors, request.config.getoption("ci_output_dir", default=None))
        except (OSError, ValueError) as diagnostic_error:
            print(f"Unable to retain lifecycle diagnostics: {diagnostic_error}")
        raise


@pytest.mark.ci_tier('fast')
def test_lifecycle_diagnostics_preserve_failure_and_complete_synthetic_evidence(tmp_path, monkeypatch, capsys):
    from copy import deepcopy
    from datetime import datetime
    from types import SimpleNamespace
    from tests.test_analytics_closure_qualification import questions
    from tools.analytics_qualification_fixture import question_plan
    from tools.analytics_qualification_questions import expected_rows

    config = configuration(tmp_path, "questions", "populated")
    config["state"] = "idle"
    config["manifest"]["questions"]["idle_seconds"] = 61
    expected = expected_rows(SimpleNamespace(size=1000, account="synthetic-continuous-owner",
        clock=datetime.fromisoformat(config["manifest"]["fixture"]["evaluation_clock"])), "populated")
    report = questions()
    report.update(initial_messages=1000, case="populated", state="idle", complete=True, idle_seconds=61,
                  plan=question_plan(config["manifest"], "populated"), filters={}, expected=expected)
    report["first_query"]["actual"] = expected[:50]
    for call in report["calls"]:
        call.update(actual=expected[:50], rows=50, has_more=True)
    report["statistics"] = qualification.question_statistics(report["calls"], 5)
    job = "questions/reference-windows-16g/populated/idle"
    assert qualification.check_questions(config["manifest"], job, report) == []
    report["calls"][7].update(error="analytics_question_limit_exceeded", correct=False,
        actual=None, rows=None, has_more=None, truncated=None, undetermined_conversations=None)
    report["statistics"] = qualification.question_statistics(report["calls"], 5)
    original = deepcopy(report)
    events = Path(config["output"]) / "fresh-process/events"
    for name, value in (("00001-readiness", {"scheduler_availability": "available"}),
                        ("00002-question", report["calls"][7]), ("00003-question", report["calls"][8])):
        qualification.write_once(events / f"{name}.json", {"value": value})
    database = Path(config["data"]) / "canonical.sqlite3"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"database contents must not enter diagnostic artifacts")
    output = tmp_path / "ci-evidence"
    request = SimpleNamespace(config=SimpleNamespace(getoption=lambda *args, **kwargs: str(output)))
    monkeypatch.setattr(__name__ + ".collect", lambda _: report)
    monkeypatch.setattr(__name__ + ".configuration", lambda *args: config)
    with pytest.raises(AssertionError) as recorded_failure:
        test_owned_questions_after_real_idle_and_mutation(tmp_path, monkeypatch, request, "populated", "idle")
    diagnostic = output / "diagnostics/lifecycle-populated-idle.payload.txt"
    retained = qualification.read_json(diagnostic)
    assert retained["payload"] == original == report
    assert "invalid_question_call:7" in retained["qualification_errors"]
    assert retained["payload"]["scheduler_closed"] and retained["payload"]["detached_workers"] == 0
    assert retained["payload"]["backlog"] == 0
    assert [entry["path"] for entry in retained["collector_events"]] == [
        "fresh-process/events/00001-readiness.json", "fresh-process/events/00002-question.json"]
    assert not list(output.rglob("*.json")) and not list(output.rglob("*.sqlite3"))
    captured = capsys.readouterr().out
    assert '"error": "analytics_question_limit_exceeded"' in captured
    assert '"actual_matches_expected": false' in captured
    assert qualification.check_questions(config["manifest"], job, report)

    def unavailable_storage(*args):
        raise OSError("diagnostic storage unavailable")
    monkeypatch.setattr(qualification, "write_once", unavailable_storage)
    with pytest.raises(AssertionError) as original_failure:
        test_owned_questions_after_real_idle_and_mutation(tmp_path, monkeypatch, request, "populated", "idle")
    assert original_failure.value.args == recorded_failure.value.args
    assert "Unable to retain lifecycle diagnostics: diagnostic storage unavailable" in capsys.readouterr().out
