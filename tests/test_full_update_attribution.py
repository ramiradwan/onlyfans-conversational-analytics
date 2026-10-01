"""No production semantics change while accounting for full scheduled work."""
import asyncio
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest

from tools.analytics_update_attribution import UpdateAttribution
from tools import analytics_insertion_diagnostic as focused
from tools import analytics_qualification as q
from tools import light_first_update_benchmark as runner


@pytest.mark.parametrize('failure', [False, True])
def test_context_body_is_not_charged_as_acquisition(failure):
    calls=[]
    class Owner:
        @contextmanager
        def locked(self):
            calls.append('acquired')
            try: yield 41
            finally: calls.append('released')
    before=Owner.locked
    trace=UpdateAttribution()
    trace.context(Owner,'locked')
    try:
        with trace.span('root'):
            try:
                with Owner().locked() as value:
                    assert value==41
                    with trace.span('body'):
                        if failure: raise RuntimeError('expected')
            except RuntimeError:
                assert failure
    finally: trace.restore()
    assert Owner.locked is before
    assert calls==['acquired','released']
    events={e['name']:e for e in trace.events}
    assert events['body']['parent']==events['root']['id']
    assert abs(events['root']['seconds']-events['root']['self_seconds']-
        sum(events[n]['seconds'] for n in ['body','Owner.locked.enter','Owner.locked.exit'])) < 1e-8


def test_sql_group_redacts_arguments_and_key_and_restores_inheritance():
    class Base:
        def execute(self, statement, parameters=()): return len(parameters)
    class Owner(Base): pass
    trace=UpdateAttribution()
    trace.sql(Owner,'execute')
    try:
        assert Owner().execute('SELECT * FROM things WHERE id IN (?,?)',('private','secret'))==2
        Owner().execute('PRAGMA key = "private-key"')
        snap=trace.snapshot()
        text=str(snap)
        assert 'private' not in text and 'secret' not in text and 'SELECT' not in text
        assert len(snap['sql_groups'])==1
        assert snap['sql_groups'][0]['calls']==1
    finally: trace.restore()
    assert 'execute' not in vars(Owner)


def test_full_install_restore():
    from app.persistence.database import _TrackedConnection
    from app.analytics.pipeline import AnalyticsPipeline
    before=dict(vars(_TrackedConnection))
    lock=AnalyticsPipeline._account_lock
    trace=UpdateAttribution()
    try: trace.install()
    finally: trace.restore()
    assert dict(vars(_TrackedConnection))==before
    assert AnalyticsPipeline._account_lock is lock


def test_full_scope_cli_is_explicit():
    base=['runner','--source-root','source','--expected-sha','sha','--output','out','--owner-lock','lock','--full-attribution']
    for extra in [[],['--preparation','focused-component'],['--preparation','focused-update','--trace-mode','none']]:
        with patch('sys.argv',base+extra),pytest.raises(SystemExit):runner.options()
    with patch('sys.argv',base+['--preparation','focused-update']):
        assert runner.options().full_attribution


def test_actual_scheduled_smoke_full_attribution(tmp_path):
    args=SimpleNamespace(messages=1000,output=tmp_path,trace_mode='coarse',full_attribution=True,focused_operation='insert')
    manifest=q.read_json(Path(__file__).resolve().parents[1]/'docs/analytics/acceptance-manifest.json')
    from app.persistence.database import LocalSQLite, _TrackedConnection
    from app.analytics.pipeline import AnalyticsPipeline
    descriptors = (LocalSQLite.transaction, LocalSQLite.read, _TrackedConnection.close,
                   AnalyticsPipeline._account_lock)
    result={}
    outer=runner.Trace()
    light=SimpleNamespace(atomic_status=lambda *a,**k:None)
    asyncio.run(focused.run_update(args,q,light,outer,tmp_path/'status.json',manifest,result,tmp_path/'work'))
    assert descriptors == (LocalSQLite.transaction, LocalSQLite.read,
                           _TrackedConnection.close, AnalyticsPipeline._account_lock)
    assert 'commit' not in vars(_TrackedConnection)
    assert 'execute' not in vars(_TrackedConnection)
    assert result['complete'] is True,result
    assert result['safety']==dict(scheduler_closed=True,detached_workers=0,backlog=0)
    trace=result['full_attribution']
    assert trace['events'] and trace['sql_groups']
    names={e['name'] for e in trace['events']}
    assert 'SQLiteGraphGenerationWriter.validate' in names
    assert 'app.analytics.incremental_graph.write_incremental_graph' in names
    assert (tmp_path/'operation-attribution.json').is_file()
    assert result['probe']['independent_rebuild_equal']
    assert result['probe']['persisted_content_revalidated']
