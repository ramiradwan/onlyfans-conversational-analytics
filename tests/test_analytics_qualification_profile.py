"""Attribution uses the existing collector and cannot qualify acceptance."""
from types import SimpleNamespace

import pytest

from tools import analytics_qualification as q
from tools.analytics_qualification_profile import install

pytestmark = [pytest.mark.ci_tier('fast')]


@pytest.mark.parametrize("failure", [False, True])
def test_profile_preserves_results_and_errors(tmp_path, failure):
    def action(*args, **kwargs):
        if failure:
            raise ValueError("test failure")
        return 42
    work = SimpleNamespace(f=SimpleNamespace(pipeline=SimpleNamespace(
        build_candidate=action, publish_candidate=action)))
    install(work, tmp_path)
    work.profile_phase = "ordinary/small"
    if failure:
        with pytest.raises(ValueError, match="test failure"):
            work.f.pipeline.build_candidate()
    else:
        assert work.f.pipeline.build_candidate() == 42
    record = q.read_json(next((tmp_path / "profiles").glob("*.json")))
    assert record["qualifies_latency"] is False and record["functions"]
    assert q.check_payload({}, {}, "visibility/test/0", {"profiling": True})


def test_attribution_does_not_mix_other_threads(tmp_path):
    from threading import Event, Thread, get_ident
    started, release = Event(), Event()
    def action():
        started.set()
        assert release.wait(5)
    work = SimpleNamespace(f=SimpleNamespace(pipeline=SimpleNamespace(
        build_candidate=action, publish_candidate=action)))
    install(work, tmp_path)
    work.profile_phase = 'ordinary/small'
    worker = Thread(target=work.f.pipeline.build_candidate)
    worker.start()
    try:
        assert started.wait(5)
        # Deliberately matches a tracked runtime function name, on another thread.
        def scan_identity():
            return 42
        assert scan_identity() == 42
    finally:
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    record = q.read_json(next((tmp_path / 'profiles').glob('*.json')))
    assert record['thread_id'] != get_ident()
    assert all(row['function'] != 'scan_identity' for row in record['functions'])


@pytest.mark.parametrize('claim', [False, True, 0, 'false'])
def test_raw_profile_configuration_cannot_qualify_as_uninstrumented(tmp_path, claim):
    q.write_once(tmp_path / 'worker-input.json', {'profile_updates': True})
    names = ['worker-input.json', 'payload.json', 'process.json', 'fixture.json']
    result = {'payload': {'profiling': claim},
        'attachments': [{'name': name, 'path': name} for name in names]}
    errors = q.check_collector_evidence(tmp_path, result, {})
    assert errors == ['instrumented_run_is_diagnostic_only' if claim is True
                      else 'collector_profiling_flag_mismatch']
