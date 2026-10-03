"""Exercise the public closure command and owned-worker failure paths."""
from pathlib import Path
import os
import sys

import pytest

from tools import analytics_qualification as q
from tools import analytics_qualification_runner as runner
from tests.test_analytics_closure_qualification import source

pytestmark = [pytest.mark.ci_tier('integration')]

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def command(tmp_path, monkeypatch):
    monkeypatch.setattr(q, "source_context", lambda root: source())
    monkeypatch.setattr(q, "runtime_context", lambda: {"python": "unit-test"})
    monkeypatch.setattr(runner, "windows_preflight", lambda: {"status": "BLOCKED", "reason": "unit-test"})
    monkeypatch.setattr(runner.Path, "home", staticmethod(lambda: tmp_path))
    output = tmp_path / "evidence"
    def invoke(*arguments):
        monkeypatch.setattr(sys, "argv", ["qualify_analytics_baseline.py", "--closure", "--output", str(output), *arguments])
        return runner.main(ROOT)
    return output, invoke


def test_preflight_without_evidence_is_blocked(command):
    output, invoke = command
    assert invoke() == 2
    assert q.read_json(next((output / "preflight").glob("*.json")))["profiles"] == "BLOCKED"


def test_resume_creates_a_new_session_without_overwriting_evidence(command):
    output, invoke = command
    assert invoke() == 2
    original = (output / "context.json").read_bytes()
    assert invoke("--resume") == 2
    assert len(list((output / "sessions").glob("*.json"))) == 2
    assert len(list((output / "preflight").glob("*.json"))) == 1
    assert (output / "context.json").read_bytes() == original
    assert invoke("--verify") == 2
    assert len(list((output / "sessions").glob("*.json"))) == 2


def test_changed_source_cannot_resume(command, monkeypatch):
    output, invoke = command
    assert invoke() == 2
    changed = source()
    changed["revision"] = "b" * 40
    monkeypatch.setattr(q, "source_context", lambda root: changed)
    with pytest.raises(SystemExit) as error:
        invoke("--resume")
    assert error.value.code == 2
    assert invoke("--verify") == 1
    assert len(list((output / "sessions").glob("*.json"))) == 1


def test_existing_directory_requires_explicit_resume(command):
    _, invoke = command
    assert invoke() == 2
    assert invoke() == 1


def test_owned_worker_success_and_timeout_are_distinct(tmp_path):
    completed = tmp_path / "complete"
    completed.mkdir()
    result = runner.supervise([sys.executable, "-c", "print('complete')"], ROOT, completed, 5)
    assert result["status"] == "PASS"
    assert result["worker_joined"] is True
    cancelled = tmp_path / "timeout"
    cancelled.mkdir()
    result = runner.supervise([sys.executable, "-c", "import time; time.sleep(30)"], ROOT, cancelled, 0.05)
    assert result["status"] == "FAIL"
    assert result["timed_out"] is True
    assert result["worker_joined"] is True


def test_owned_worker_cancellation_retains_failure(tmp_path):
    from threading import Event
    event = Event()
    event.set()
    result = runner.supervise([sys.executable, "-c", "import time; time.sleep(30)"],
                              ROOT, tmp_path, 5, cancel_event=event)
    assert result["status"] == "FAIL"
    assert result["reason"] == "operator_cancelled"
    assert result["worker_joined"] is True


def test_headroom_failure_blocks_before_worker_start(tmp_path):
    limits = {"minimum_free_disk_bytes": 10**30, "minimum_available_memory_bytes": 0}
    result = runner.supervise([sys.executable, "-c", "raise AssertionError('must not run')"],
                              ROOT, tmp_path, 5, limits=limits)
    assert result["status"] == "BLOCKED"
    assert result["worker_started"] is False


def test_real_failed_worker_is_durable_and_resume_cannot_erase_it(command, monkeypatch):
    from tools.analytics_qualification_process import supervise
    output, invoke = command
    def failing(_command, root, attempt, limit, **kwargs):
        return supervise([sys.executable, "-c", "raise SystemExit(9)"], root, attempt, limit)
    monkeypatch.setattr(runner, "supervise", failing)
    assert invoke("--run-source", "matrix") == 1
    receipt = next((output / "attempts").glob("*/result.json"))
    original = receipt.read_bytes()
    assert q.read_json(receipt)["exit_code"] == 9
    assert invoke("--resume") == 1
    assert receipt.read_bytes() == original
    assert len(list((output / "sessions").glob("*.json"))) == 2
    assert invoke("--verify") == 1


def test_verifier_does_not_initialize_application_settings(tmp_path):
    import subprocess
    from tests.test_analytics_closure_qualification import questions, MANIFEST
    inputs = tmp_path / "inputs.json"
    inputs.write_bytes(q.encoded({"manifest": MANIFEST, "payload": questions()}))
    code = ("import json,sys; from pathlib import Path; "
            "from tools import analytics_qualification as q; "
            "r=json.loads(Path(sys.argv[1]).read_text()); "
            "assert not q.check_questions(r['manifest'], 'questions/reference-windows-16g/empty/fresh', r['payload']); "
            "assert 'app.core.config' not in sys.modules; "
            "assert 'tests.continuous_analytics_fixture' not in sys.modules")
    completed = subprocess.run([sys.executable, "-c", code, str(inputs)], cwd=ROOT,
                               capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stderr


def test_reporting_error_retains_owned_worker_result(command, monkeypatch):
    from types import SimpleNamespace
    directory, invoke = command
    assert invoke() == 2
    context = q.read_json(directory / "context.json")
    session = q.session(directory, context)
    manifest = q.read_json(ROOT / "docs/analytics/acceptance-manifest.json")
    def completed_worker(command, root, attempt, seconds, **kwargs):
        q.write_once(attempt / "collector/payload.json", {"complete": True})
        (attempt / "worker.log").write_text("owned worker finished")
        return {"status": "FAIL", "worker_started": True, "worker_joined": True,
                "process_instance": "retained-owner", "exit_code": 1, "seconds": 2,
                "timed_out": False, "reason": "known_product_failure"}
    def invalid_payload(*args):
        raise ValueError("injected verifier failure")
    monkeypatch.setattr(runner, "supervise", completed_worker)
    monkeypatch.setattr(q, "check_payload", invalid_payload)
    args = SimpleNamespace(run_source="questions", messages=100000, case="empty",
                           state="fresh", repeat=0, subject_root=None, known_synthetic_kinds=True)
    runner.run_source(ROOT, directory, context, session, manifest, args)
    result = q.read_json(next((directory / "attempts").glob("*/result.json")))
    assert result["status"] == "FAIL" and result["complete"] is False
    assert result["worker_joined"] is True and result["worker_started"] is True
    assert result["exit_code"] == 1 and result["process_instance"] == "retained-owner"
    assert result["reason"] == "known_product_failure" and result["reporting_error"] == "ValueError"
    assert result["payload"] == {"complete": True}
    assert {x["name"] for x in result["attachments"]} == {"payload.json", "worker-input.json", "worker.log"}


def hardware_runner_case(command, monkeypatch, mode):
    from types import SimpleNamespace
    from tools import analytics_qualification_hardware_evidence as hardware
    from tools import analytics_qualification_packaged as packaged
    directory, invoke = command
    assert invoke() == 2
    context = q.read_json(directory / "context.json")
    session = q.session(directory, context)
    manifest = q.read_json(ROOT / "docs/analytics/acceptance-manifest.json")
    args = SimpleNamespace(run_source="questions", run_questions=True, run_package="package",
                           profile="reference-windows-16g", messages=100000, case="empty",
                           state="fresh", repeat=0, subject_root=None, known_synthetic_kinds=True,
                           hardware_handoff=None, package_inputs=directory / "package-inputs.json")
    monkeypatch.setattr(hardware, "workload_paths", lambda *args, **kwargs: {"temporary": "temporary"})
    if mode == "packaged":
        q.write_once(args.package_inputs, {})
        def prerequisites(inputs, manifest, source, profile, *, hardware_observer):
            return {"hardware": hardware_observer("browser.exe"), "artifacts": context.get("artifacts")}
        monkeypatch.setattr(packaged, "prerequisites", prerequisites)
    def run():
        (packaged.run if mode == "packaged" else runner.run_source)(
            ROOT, directory, context, session, manifest, args)
        return q.read_json(next((directory / "attempts").glob("*/result.json")))
    return run


@pytest.mark.parametrize("mode", ["source", "packaged"])
def test_missing_hardware_pre_blocks_before_worker(command, monkeypatch, mode):
    from tools import analytics_qualification_process as process
    run = hardware_runner_case(command, monkeypatch, mode)
    def forbidden(*args, **kwargs):
        pytest.fail("worker must not start without pre observation")
    monkeypatch.setattr(runner, "supervise", forbidden)
    monkeypatch.setattr(process, "supervise", forbidden)
    result = run()
    assert result["status"] == "BLOCKED"
    assert result["worker_started"] is False and result["exit_code"] is None
    assert result["reason"] == "hardware_handoff_not_configured"


@pytest.mark.parametrize("mode", ["source", "packaged"])
def test_invalid_hardware_post_retains_joined_worker_failure(command, monkeypatch, mode):
    from tools import analytics_qualification_hardware_evidence as hardware
    from tools import analytics_qualification_process as process
    from tools import analytics_qualification_tracks as tracks
    run = hardware_runner_case(command, monkeypatch, mode)
    observed = []
    class InvalidPost:
        hardware = {"disk": "SSD"}
        runtime_paths = {"temporary": "temporary"}
        references = []
        def __init__(self, *args):
            pass
        def before_worker(self):
            observed.append("before")
        def after_worker(self, result):
            assert result["worker_joined"] is True
            assert result["process_instance"] == "retained-owner"
            observed.append("after")
            raise ValueError("hardware_topology_changed_during_worker")
    def completed_worker(command, root, attempt, seconds, **kwargs):
        q.write_once(attempt / "collector/payload.json", {"complete": True})
        (attempt / "worker.log").write_text("worker joined", encoding="utf-8")
        return {"status": "PASS", "worker_started": True, "worker_joined": True,
                "process_instance": "retained-owner", "exit_code": 0, "seconds": 2,
                "timed_out": False, "reason": None}
    monkeypatch.setattr(hardware, "HardwareEvidence", InvalidPost)
    monkeypatch.setattr(tracks, "check_profile", lambda *args: [])
    monkeypatch.setattr(q, "check_payload", lambda *args: [])
    monkeypatch.setattr(runner, "supervise", completed_worker)
    monkeypatch.setattr(process, "supervise", completed_worker)
    result = run()
    assert observed == ["before", "after"]
    assert result["status"] == "FAIL" and result["complete"] is False
    assert result["worker_started"] is True and result["worker_joined"] is True
    assert result["exit_code"] == 0 and result["process_instance"] == "retained-owner"
    assert result["reason"] == "hardware_topology_changed_during_worker"
