"""The frozen idle mutation inserts before the preceding equal-time message."""
from datetime import timedelta
from time import perf_counter
import json
import pytest
from tests.continuous_analytics_fixture import ACCOUNT, NOW, make_fixture, insert_message, advance, cleanup, cold_equal


def insertion_fixture(path):
    f=make_fixture(path, conversations=3, messages=0)
    with f.repositories.database.transaction() as db:
        for chat,count in ((0,1024),(1,500),(2,2)):
            for i in range(count):
                insert_message(db,f'chat-{chat}',f'input-{chat}-{i}',NOW-timedelta(hours=48)+timedelta(seconds=i),i)
    f.pipeline.project_account(ACCOUNT)
    with f.repositories.database.transaction() as db:
        insert_message(db,'chat-1','visibility-ordinary-small',NOW-timedelta(microseconds=1),2)
        advance(db)
    f.pipeline.project_account(ACCOUNT)
    f.pipeline.publish_candidate(f.pipeline.build_candidate(ACCOUNT,force=True))
    return f


def test_tied_insertion_does_not_rebuild_or_revalidate_unchanged_records(tmp_path,monkeypatch,record_property):
    from app.analytics import conversation_enrichment_unit_sql as storage
    f=insertion_fixture(tmp_path)
    try:
        observed={'canonical_models':0,'graph_messages':0,'full_units':0,'full_messages':0,
                  'full_validation_seconds':0,'append_attempts':0,'append_successes':0}
        canonical=f.pipeline._canonical_conversations
        def models(raw,**kwargs):
            observed['canonical_models']+=sum(len(c['messages']) for c in raw.conversations.values())
            return canonical(raw,**kwargs)
        batches=f.pipeline.graph_projector.batches
        def graph(account,revision,conversations,*args,**kwargs):
            observed['graph_messages']+=sum(len(c.messages) for c in conversations)
            yield from batches(account,revision,conversations,*args,**kwargs)
        validate=storage._validate_unit
        def checked(unit,**kwargs):
            start=perf_counter();observed['full_units']+=1;observed['full_messages']+=unit.header.message_count
            try:return validate(unit,**kwargs)
            finally:observed['full_validation_seconds']+=perf_counter()-start
        append=storage._validate_appended_unit
        def appended(*args,**kwargs):
            observed['append_attempts']+=1;result=append(*args,**kwargs)
            observed['append_successes']+=bool(result);return result
        monkeypatch.setattr(f.pipeline,'_canonical_conversations',models)
        monkeypatch.setattr(f.pipeline.graph_projector,'batches',graph)
        monkeypatch.setattr(storage,'_validate_unit',checked)
        monkeypatch.setattr(storage,'_validate_appended_unit',appended)
        calls=[a.calls for a in f.analyzers]
        with f.repositories.database.transaction() as db:
            insert_message(db,'chat-1','visibility-idle-small',NOW-timedelta(microseconds=1),2)
            advance(db)
        with f.repositories.database.read() as db:
            latest=db.execute("SELECT message_id FROM account_messages WHERE chat_id='chat-1' ORDER BY sent_at DESC,winning_stream_epoch DESC,winning_source_seq DESC,message_id DESC LIMIT 2").fetchall()
            assert [r[0] for r in latest]==['visibility-ordinary-small','visibility-idle-small']
        start=perf_counter();candidate=f.pipeline.build_candidate(ACCOUNT);f.pipeline.publish_candidate(candidate)
        observed['build_publish_seconds']=perf_counter()-start
        observed['analyzer_calls']=[a.calls-n for a,n in zip(f.analyzers,calls)]
        record_property('work',json.dumps(observed,sort_keys=True))
        assert observed['canonical_models']==0,observed
        assert observed['graph_messages']<=5,observed
        assert observed['full_messages']==0,observed
        assert observed['analyzer_calls']==[1,1,1]
        cold_equal(f,candidate.artifact())
    finally:cleanup(f)
