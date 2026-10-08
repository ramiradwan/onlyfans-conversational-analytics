"""Work regressions for verified appends and owned pending reads."""
import asyncio
from threading import Event
from unittest.mock import Mock
import pytest
from tests.continuous_analytics_fixture import ACCOUNT,NOW,advance,cleanup,cold_equal,insert_message
from tests.test_dominant_append_reuse import dominant_fixture

pytestmark = [pytest.mark.ci_tier("integration")]


def test_eligible_append_does_not_materialize_full_canonical_prefix(tmp_path,monkeypatch,record_property):
    from app.analytics.pipeline import AnalyticsPipeline
    f=dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        original=AnalyticsPipeline._canonical_conversations.__func__
        sizes=[]
        def observe(cls,account,**kwargs):
            sizes.extend(len(v['messages']) for v in account.conversations.values())
            return original(cls,account,**kwargs)
        monkeypatch.setattr(AnalyticsPipeline,'_canonical_conversations',classmethod(observe))
        with f.repositories.database.transaction() as db:
            insert_message(db,'chat-0','stream-new-tail',NOW,2);advance(db)
        candidate=f.pipeline.build_candidate(ACCOUNT)
        f.pipeline.publish_candidate(candidate)
        record_property('canonical_message_models_requested',sum(sizes))
        assert max(sizes,default=0)<=2,sizes
        cold_equal(f,candidate.artifact())
    finally:cleanup(f)


@pytest.mark.asyncio
async def test_pending_questions_do_not_open_databases(tmp_path,monkeypatch,record_property):
    from app.analytics.query_runtime import QuestionResources
    from app.analytics.scheduling import InProcessProjectionScheduler
    from app.analytics.errors import ProjectionUnavailable
    from tools.analytics_qualification_workloads import policy
    from tools.analytics_qualification_fixture import question_plan
    from tools import analytics_qualification as q
    from app.persistence.database import LocalSQLite
    f=dominant_fixture(tmp_path)
    ready,release=Event(),Event();scheduler=None;resources=None
    try:
        f.pipeline.project_account(ACCOUNT)
        original=f.pipeline.build_candidate
        def blocked(*args,**kwargs):
            ready.set()
            assert release.wait(10)
            return original(*args,**kwargs)
        f.pipeline.build_candidate=blocked
        scheduler=InProcessProjectionScheduler(f.pipeline,worker_count=1)
        await scheduler.start(recover=False)
        with f.repositories.database.transaction() as db:
            insert_message(db,'chat-1','pending-tail',NOW,2);advance(db)
        await scheduler.schedule(ACCOUNT,2)
        assert await asyncio.to_thread(ready.wait,10)
        resources=QuestionResources(f.source,f.pipeline,clock=lambda:NOW)
        manifest=q.read_json(__import__('pathlib').Path(__file__).parents[1]/'docs/analytics/acceptance-manifest.json')
        request=question_plan(manifest,'empty')
        request.update(start=NOW.isoformat(),end=NOW.isoformat(),cutoff=NOW.isoformat())
        from datetime import timedelta
        request['start']=(NOW-timedelta(days=1)).isoformat()
        calls=Mock(wraps=LocalSQLite.connect)
        # Preserve descriptor binding while counting only the blocked interval.
        original_connect=LocalSQLite.connect
        seen=[]
        def connect(self):seen.append(1);return original_connect(self)
        with monkeypatch.context() as patch:
            patch.setattr(LocalSQLite,'connect',connect)
            for _ in range(10):
                with pytest.raises(ProjectionUnavailable):resources.execute(policy(ACCOUNT),request)
        record_property('pending_query_database_opens',len(seen))
        assert not seen, len(seen)
    finally:
        release.set()
        if scheduler is not None:
            await scheduler.wait(ACCOUNT)
            assert await scheduler.close(timeout=10)
        if resources is not None:resources.close()
        cleanup(f)
