"""Prefix attribution keeps the v7 worker, oracle order and resource lifecycle."""
import asyncio
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from tools import analytics_qualification as q
from tools import analytics_prefix_attribution as observation
from tools import light_first_update_benchmark as runner
from tools.analytics_insertion_diagnostic import Attribution


def test_window_and_oracle_order_with_restoration(tmp_path):
    calls = []
    journal = SimpleNamespace(save=lambda label,value: calls.append(label))
    async def scheduled(*args, case, **kwargs):
        assert observer.trace.enabled
        calls.append('scheduled')
        journal.save('operation', {'case':case})
        assert not observer.trace.enabled
        calls.append('oracle')
        journal.save('phase', dict(case=case, independent_rebuild_equal=True,
                                  persisted_content_revalidated=True))
        return {'case':case}
    worker = SimpleNamespace(scheduled=scheduled)
    observer = observation.PrefixAttribution(journal,q,tmp_path,worker=worker)
    saved = journal.save
    with observer:
        answer = asyncio.run(worker.scheduled(case='ordinary/dominant'))
    assert answer == {'case':'ordinary/dominant'}
    assert calls == ['scheduled','operation','oracle','phase']
    assert worker.scheduled is scheduled and journal.save is saved
    snapshot = observer.snapshot()
    assert snapshot['windows'][0]['independent_verification_completed']
    assert q.read_json(tmp_path/'attribution/ordinary-dominant.json')['independent_verification_pending']
    assert not observer.trace.patches and not observer.calls.patches


@pytest.mark.parametrize('failure',[RuntimeError,asyncio.CancelledError])
def test_exceptions_disable_and_restore(tmp_path,failure):
    journal = SimpleNamespace(save=lambda *args:None)
    async def scheduled(*args,**kwargs): raise failure('expected')
    worker=SimpleNamespace(scheduled=scheduled)
    observer=observation.PrefixAttribution(journal,q,tmp_path,worker=worker)
    with pytest.raises(failure):
        with observer:
            asyncio.run(worker.scheduled(case='ordinary/dominant'))
    assert not observer.trace.enabled
    assert worker.scheduled is scheduled
    assert not observer.trace.patches and not observer.calls.patches
    assert not observer.windows[0].get('independent_verification_completed')


def test_executor_calls_are_bounded_and_preserve_result(monkeypatch):
    monkeypatch.setattr(observation,'MAX_EXECUTOR_CALLS',1)
    calls=observation.ExecutorCalls(lambda:'lifecycle')
    item,fn=calls.wrap('owned_call',lambda:42)
    assert fn()==42 and item['end']>=item['start']>=item['submitted']
    with pytest.raises(RuntimeError,match='capacity'):calls.wrap('owned_call',lambda:0)


def test_window_close_race_does_not_change_wrapped_call(monkeypatch):
    trace=Attribution()
    @contextmanager
    def closed(*args,**kwargs): yield {}
    monkeypatch.setattr(trace,'span',closed)
    owner=SimpleNamespace(__name__='example', method=lambda value:value+1)
    original=owner.method
    trace.patch(owner,'method')
    try: assert owner.method(5)==6
    finally: trace.restore()
    assert owner.method is original


def test_cli_allows_only_explicit_instrumented_prefix():
    base=['runner','--source-root','source','--expected-sha','sha','--output','out',
          '--owner-lock','lock','--preparation','repeat1-prefix','--full-attribution']
    with patch('sys.argv',base):
        assert runner.options().full_attribution
    with patch('sys.argv',base+['--trace-mode','none']),pytest.raises(SystemExit):
        runner.options()


def test_real_prefix_keeps_all_three_checks_and_joins(tmp_path):
    from app.analytics.pipeline import AnalyticsPipeline
    from app.persistence.database import LocalSQLite,_TrackedConnection
    original=(AnalyticsPipeline._account_lock,LocalSQLite.transaction,_TrackedConnection.close)
    args=SimpleNamespace(output=tmp_path,messages=1000,trace_mode='coarse',
                         full_attribution=True,timeout_seconds=9000)
    manifest=q.read_json(Path(__file__).resolve().parents[1]/'docs/analytics/acceptance-manifest.json')
    result={};trace=runner.Trace()
    asyncio.run(runner.run_repeat1_prefix(args,q,None,trace,None,manifest,result,tmp_path/'work'))
    assert result['complete'],result
    assert result['safety']==dict(scheduler_closed=True,detached_workers=0,backlog=0)
    assert [s['case'] for s in result['summaries']]==list(observation.CASES)
    assert not any(result['probe_checks'].values())
    assert result['state_monitor_joined']
    full=result['prefix_attribution']
    assert len(full['windows'])==3
    assert all(w['independent_verification_completed'] for w in full['windows'])
    assert {e['phase'] for e in full['full_attribution']['events']} <= set(observation.CASES)
    assert any(e['phase']=='lifecycle' for e in full['executor_calls'])
    assert original==(AnalyticsPipeline._account_lock,LocalSQLite.transaction,_TrackedConnection.close)
    assert 'execute' not in vars(_TrackedConnection) and 'commit' not in vars(_TrackedConnection)
    assert len(list((tmp_path/'attribution').glob('*.json')))==3
    assert not (tmp_path/'restarted-process').exists()
