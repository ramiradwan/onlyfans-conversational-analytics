"""Run the unchanged tied-time input through normal scheduler/query lifecycles."""
import asyncio
import json
from pathlib import Path
from time import perf_counter
import pytest
from tools import analytics_qualification as q
from tools.analytics_qualification_fixture import Workload,Journal
from tools.analytics_qualification_workloads import direct,scheduled


@pytest.mark.asyncio
async def test_scheduler_idle_insertion_keeps_queries_cleanup_and_rebuild_equality(tmp_path,monkeypatch,record_property):
    from app.analytics.query_runtime import QuestionResources
    from app.analytics.scheduling import InProcessProjectionScheduler
    from app.analytics import conversation_insertion as insertion
    manifest=q.read_json(Path(__file__).parents[1]/'docs/analytics/acceptance-manifest.json')
    work=Workload(tmp_path/'fixture',manifest,1000,known_kinds=True)
    resources=QuestionResources(work.f.source,work.f.pipeline)
    scheduler=InProcessProjectionScheduler(work.f.pipeline,worker_count=1,queue_capacity=64,reconciliation_interval=30)
    journal=Journal(tmp_path/'events','scheduler-regression')
    try:
        await direct(work,journal,resources,'cold')
        resources.start()
        await scheduler.start(recover=True)
        await scheduled(work,journal,resources,scheduler,'one_committed_message',
            lambda:work.add(1,'visibility-ordinary-small'),case='ordinary/small')
        await direct(work,journal,resources,'unchanged_rebuild')
        await scheduled(work,journal,resources,scheduler,'one_committed_message',
            lambda:work.add(0,'visibility-rebuilt-dominant'),case='rebuilt/dominant')
        recovery_calls=[]
        prepared_calls=[]
        original_recovery=work.f.stores.projections.prepare_update_reuse
        original_prepared=work.f.stores.projections.update_reuse_prepared
        def observe_recovery(*args,**kwargs):
            recovery_calls.append(True)
            return original_recovery(*args,**kwargs)
        def observe_prepared(*args,**kwargs):
            prepared_calls.append(True)
            return original_prepared(*args,**kwargs)
        monkeypatch.setattr(work.f.stores.projections,'prepare_update_reuse',observe_recovery)
        monkeypatch.setattr(work.f.stores.projections,'update_reuse_prepared',observe_prepared)
        started=perf_counter()
        await asyncio.sleep(manifest['visibility']['idle_seconds'])
        idle=perf_counter()-started
        accepted=[];original=insertion.try_insert
        def observe(*args,**kwargs):
            value=original(*args,**kwargs);accepted.append(value is not None);return value
        monkeypatch.setattr(insertion,'try_insert',observe)
        result=await scheduled(work,journal,resources,scheduler,'one_committed_message',
            lambda:work.add(1,'visibility-idle-small'),case='idle/small')
        assert accepted==[True]
        assert idle>=manifest['visibility']['idle_seconds']
        assert prepared_calls, 'idle reconciliation did not consult the verified envelope'
        assert recovery_calls==[], 'idle reconciliation repeated complete recovery despite a live envelope'
        assert result['independent_rebuild_equal'] and result['persisted_content_revalidated']
        assert result['stale_reference_rejected'] and result['valid_current_result'] and result['cleanup_complete']
        assert result['backlog_before']==result['backlog_after']==0
        record_property('actual_idle_seconds',idle)
        record_property('result',json.dumps(result,sort_keys=True))
        record_property('scope','1000-message real-lifecycle regression, not 100k latency qualification')
    finally:
        resources.close()
        assert await scheduler.close(timeout=10)
        assert scheduler.detached_worker_count==0
        work.close()
