"""State changes cannot hide unfinished work or reset an expired deadline."""
from pathlib import Path
import pytest

from tools import analytics_qualification as q
from tools.analytics_qualification_execution import SCHEMA, StateBudget


def make_budget(tmp_path):
    policy = {'directory': str(tmp_path), 'states': ['cold', 'ordinary', 'rebuilt'],
              'maximum_state_seconds': 10}
    return StateBudget(policy, started=100, token='owner')


def state(tmp_path, ordinal, name, at, **changes):
    value = {'schema': SCHEMA, 'index': ordinal, 'state': name, 'at': at,
             'process_instance': 'original-runtime', 'supervisor_instance': 'owner'}
    value.update(changes)
    q.write_once(tmp_path / f'{ordinal:02}-{name}.json', value)


def test_all_states_are_bounded_without_restarting_the_runtime(tmp_path):
    budget = make_budget(tmp_path)
    state(tmp_path, 0, 'cold', 101); budget.poll(102)
    state(tmp_path, 1, 'ordinary', 108); budget.poll(109)
    state(tmp_path, 2, 'rebuilt', 117); budget.poll(126, finished=True)
    report = budget.report(126)
    assert report['complete'] and report['intervals'][0]['started'] == 100
    assert all(v['ended'] - v['started'] <= 10 for v in report['intervals'])


@pytest.mark.parametrize('fault', ['late', 'future', 'backdated', 'process', 'owner', 'skip', 'index'])
def test_invalid_transition_cannot_reset_the_deadline(tmp_path, fault):
    budget = make_budget(tmp_path)
    state(tmp_path, 0, 'cold', 101); budget.poll(102)
    changes = {'process_instance': 'new-runtime'} if fault == 'process' else (
              {'supervisor_instance': 'other-owner'} if fault == 'owner' else (
              {'index': 0} if fault == 'index' else {}))
    at = {'late': 111, 'future': 109, 'backdated': 99}.get(fault, 105)
    if fault == 'skip':
        state(tmp_path, 2, 'rebuilt', at)
    else:
        state(tmp_path, 1, 'ordinary', at, **changes)
    with pytest.raises((ValueError, TimeoutError)):
        budget.poll(112 if fault == 'late' else 108)


def test_initial_worker_launch_is_inside_cold_budget(tmp_path):
    budget = make_budget(tmp_path)
    state(tmp_path, 0, 'cold', 109)
    with pytest.raises(TimeoutError):
        budget.poll(111)


def test_missing_states_cannot_pass_on_normal_worker_exit(tmp_path):
    budget = make_budget(tmp_path)
    state(tmp_path, 0, 'cold', 101)
    with pytest.raises(ValueError, match='incomplete'):
        budget.poll(105, finished=True)


@pytest.mark.parametrize('fault', ['changed', 'removed', 'extra'])
def test_completed_records_are_immutable(tmp_path, fault):
    budget = make_budget(tmp_path)
    state(tmp_path, 0, 'cold', 101); budget.poll(102)
    if fault == 'changed':
        (tmp_path / '00-cold.json').write_text('{}')
    elif fault == 'removed':
        (tmp_path / '00-cold.json').unlink()
    else:
        (tmp_path / '00-other.json').write_text('{}')
    with pytest.raises(ValueError):
        budget.poll(103)


def test_pending_atomic_record_cannot_renew_time(tmp_path):
    budget = make_budget(tmp_path)
    (tmp_path / '00-cold.pending').write_text('{partial')
    with pytest.raises(TimeoutError):
        budget.poll(111)


@pytest.mark.parametrize('omit_last', [False, True])
def test_owned_worker_cannot_pass_with_missing_execution_states(tmp_path, omit_last):
    import sys
    from tools.analytics_qualification_process import supervise
    root = Path(__file__).resolve().parents[1]
    policy = {'directory': str(tmp_path / 'states'), 'states': ['cold', 'ordinary'],
              'maximum_state_seconds': 5, 'maximum_worker_seconds': 10}
    states = policy['states'][:1] if omit_last else policy['states']
    code = ('from tools.analytics_qualification_execution import mark_state; '
            f'config={{"execution_schedule":{policy!r}}}; '
            f'[mark_state(config,s,"one-process") for s in {states!r}]')
    result = supervise([sys.executable, '-c', code], root, tmp_path, 10,
                       execution_schedule=policy)
    assert result['worker_joined']
    assert result['status'] == ('FAIL' if omit_last else 'PASS')
    assert result['execution']['complete'] is not omit_last
    if omit_last:
        assert result['reason'] == 'execution_states_incomplete'


def test_expired_state_kills_and_joins_the_owned_worker(tmp_path):
    import sys
    from tools.analytics_qualification_process import supervise
    policy = {'directory': str(tmp_path / 'states'), 'states': ['cold', 'ordinary'],
              'maximum_state_seconds': 0.1, 'maximum_worker_seconds': 10}
    result = supervise([sys.executable, '-c', 'import time; time.sleep(30)'],
        Path(__file__).resolve().parents[1], tmp_path, 10, execution_schedule=policy)
    assert result['status'] == 'FAIL' and result['timed_out'] and result['worker_joined']
    assert result['reason'] == 'execution_state_watchdog_expired'


def test_visibility_payload_cannot_avoid_budgets_by_claiming_another_mode():
    from tools.analytics_qualification_execution import check_evidence
    manifest = {'visibility_execution': {'states': ['cold', 'ordinary', 'rebuilt', 'idle', 'restarted'],
                'maximum_state_seconds': 1800, 'maximum_worker_seconds': 9000}}
    errors = check_evidence(manifest, {'mode': 'matrix'}, {'payload': {'probes': []}},
                            {}, lambda name: None, [])
    assert errors
