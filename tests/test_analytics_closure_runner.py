"""Exercise the public closure command and owned-worker failure paths."""
from pathlib import Path
import os
import sys

import pytest

from tools import analytics_qualification as q
from tools import analytics_qualification_runner as runner
from tests.test_analytics_closure_qualification import source

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
