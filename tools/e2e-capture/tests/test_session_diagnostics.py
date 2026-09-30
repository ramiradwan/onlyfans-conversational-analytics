from pathlib import Path
import importlib.util
import json
from types import SimpleNamespace
import pytest

MODULE = Path(__file__).resolve().parents[1] / 'helpers' / 'session_diagnostics.py'

def load_module():
    assert MODULE.exists(), 'The E2E session failure diagnostic is missing'
    spec = importlib.util.spec_from_file_location('session_diagnostics_under_test', MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def failure():
    scope = {'__name__': 'app.security.companion_session_authority'}
    exec(compile("def validate_config():\n    raise ValueError('private_ticket private_account private_message')\n", 'private_path', 'exec'), scope)
    try:
        try:
            scope['validate_config']()
        except ValueError:
            raise RuntimeError('private_outer') from None
    except RuntimeError as error:
        return error

def test_suppressed_cause_retains_only_phase_class_and_source_location():
    module = load_module()
    report = module.session_failure('agent.storage.rotate', failure())
    assert report['method'] == 'agent.storage.rotate'
    assert report['causes'][-1]['errorName'] == 'ValueError'
    assert report['causes'][-1]['phase'] == 'validate_config'
    assert report['causes'][-1]['frames'] == [{'module': 'authority', 'line': 2}]
    assert 'private_' not in json.dumps(report)

def test_diagnostic_bounds_exception_cycles_and_unknown_values():
    module = load_module()
    class PrivateFailure(Exception):
        pass
    error = PrivateFailure('private_message')
    error.__context__ = error
    report = module.session_failure('private_method', error)
    assert report['method'] == 'other'
    assert len(report['causes']) == 1
    assert report['causes'][0]['errorName'] == 'other'
    assert 'private_' not in json.dumps(report)

def test_rpc_wrapper_preserves_result_exception_identity_and_limits_output():
    module = load_module()
    error = failure()
    class RPC:
        def call(self, method, params):
            if method == 'agent.config.get':
                return params
            raise error
    target = SimpleNamespace(SessionRPC=RPC)
    output = []
    module.install_session_diagnostics(target, emit=output.append, limit=2)
    rpc = RPC()
    result = {'private_token': 'private_token'}
    assert rpc.call('agent.config.get', result) is result
    for _ in range(4):
        with pytest.raises(RuntimeError) as caught:
            rpc.call('agent.storage.rotate', result)
        assert caught.value is error
    assert len(output) == 2
    assert all(line.startswith('e2e-session-failure: ') for line in output)
    assert 'private_' not in ''.join(output)

def test_diagnostic_sink_failure_does_not_replace_the_original_failure():
    module = load_module()
    error = failure()
    class RPC:
        def call(self, method, params):
            raise error
    def emit(line):
        raise OSError('private_sink')
    module.install_session_diagnostics(SimpleNamespace(SessionRPC=RPC), emit=emit)
    with pytest.raises(RuntimeError) as caught:
        RPC().call('agent.storage.rotate', {})
    assert caught.value is error
