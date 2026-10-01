"""Tie insertion preserves ordering, account authority and complete fallbacks."""
from datetime import timedelta
from dataclasses import replace
from unittest.mock import Mock
import hashlib
import json
import pytest
from tests.continuous_analytics_fixture import ACCOUNT,NOW,make_fixture,insert_message,advance,cleanup,cold_equal
from app.analytics.opaque_refs import account_ref,conversation_ref,message_ref
from app.analytics.conversation_enrichment_units import message_records,analyzer_records,_canonical,_unit_id,_compress

pytestmark = [pytest.mark.ci_tier("integration")]


def small_fixture(path):
    f=make_fixture(path,conversations=3,messages=0)
    with f.repositories.database.transaction() as db:
        for i in range(6):
            insert_message(db,'chat-1',f'tie-{i*2:02}',NOW-timedelta(microseconds=1),2,
                           text='Thanks pricing' if i%2==0 else 'Bad service tomorrow')
            if i%2:db.execute("UPDATE account_messages SET direction='outbound' WHERE message_id=?",(f'tie-{i*2:02}',))
        for chat in (0,2):insert_message(db,f'chat-{chat}',f'other-{chat}',NOW-timedelta(days=1),0)
    f.pipeline.project_account(ACCOUNT)
    return f


def mutate(f,name='tie-05',*,direction='inbound',time=None):
    with f.repositories.database.transaction() as db:
        insert_message(db,'chat-1',name,time or NOW-timedelta(microseconds=1),2,text='Hello support $12')
        if direction=='outbound':db.execute("UPDATE account_messages SET direction='outbound' WHERE message_id=?",(name,))
        advance(db)


@pytest.mark.parametrize('name',['tie--first','tie-01','tie-05','tie-09'])
@pytest.mark.parametrize('direction',['inbound','outbound'])
def test_tied_positions_and_directions_rebuild_exact_ordering(tmp_path,monkeypatch,name,direction):
    from app.analytics import conversation_insertion as insertion
    f=small_fixture(tmp_path)
    try:
        accepted=[];original=insertion.try_insert
        def observe(*args,**kwargs):
            result=original(*args,**kwargs);accepted.append(result is not None);return result
        monkeypatch.setattr(insertion,'try_insert',observe)
        old=[a.calls for a in f.analyzers]
        mutate(f,name,direction=direction)
        candidate=f.pipeline.build_candidate(ACCOUNT);f.pipeline.publish_candidate(candidate)
        assert accepted==[True]
        assert [a.calls-n for a,n in zip(f.analyzers,old)]==[1,1,1]
        artifact=candidate.artifact()
        values=[m for m in artifact.projection.message_enrichments if m.conversation_ref==conversation_ref(ACCOUNT,'chat-1')]
        expected=sorted([f'tie-{i*2:02}' for i in range(6)]+[name])
        assert [m.message_ref for m in values]==[message_ref(ACCOUNT,'chat-1',n) for n in expected]
        assert [m.source_ordinal for m in values]==list(range(7))
        cold_equal(f,artifact)
    finally:cleanup(f)


@pytest.mark.parametrize('fault',['edit','direction','participant','non_tied','two_additions','delete','proof','capacity','graph_capacity','unit_capacity'])
def test_ineligible_source_or_capacity_retains_complete_result(tmp_path,monkeypatch,fault):
    from app.analytics import conversation_insertion as insertion
    from app.analytics import conversation_reuse as reuse
    from app.analytics import conversation_graph_units as graph_units
    f=small_fixture(tmp_path)
    try:
        accepted=[];original=insertion.try_insert
        def observe(*a,**k):
            result=original(*a,**k);accepted.append(result is not None);return result
        monkeypatch.setattr(insertion,'try_insert',observe)
        if fault=='proof':f.stores.projections._conversation_enrichment_proofs.clear()
        elif fault=='capacity':monkeypatch.setattr(insertion,'PAGE_RECORDS',1)
        elif fault=='graph_capacity':
            from app.analytics import conversation_graph_insertion as graph_insert
            monkeypatch.setattr(graph_insert,'create_membership_unit',lambda **kwargs:None)
        elif fault=='unit_capacity':monkeypatch.setattr(reuse,'MAX_ENRICHMENT_UNIT_TOTAL_BYTES',1)
        with f.repositories.database.transaction() as db:
            if fault=='edit':db.execute("UPDATE account_messages SET text='Changed source' WHERE message_id='tie-00'")
            if fault=='direction':db.execute("UPDATE account_messages SET direction='outbound' WHERE message_id='tie-00'")
            if fault=='participant':db.execute("UPDATE account_chats SET platform_user_id='different' WHERE chat_id='chat-1'")
            if fault=='two_additions':insert_message(db,'chat-1','tie-07',NOW-timedelta(microseconds=1),2)
            if fault=='delete':db.execute("DELETE FROM account_messages WHERE message_id='tie-00'")
        mutate(f,time=NOW-timedelta(hours=1) if fault=='non_tied' else None)
        candidate=f.pipeline.build_candidate(ACCOUNT);f.pipeline.publish_candidate(candidate)
        if fault!='unit_capacity':assert not any(accepted)
        cold_equal(f,candidate.artifact())
    finally:cleanup(f)


def test_context_dependent_analyzer_uses_ordinary_path(tmp_path,monkeypatch):
    from app.analytics.analyzers import RuleBasedSentimentAnalyzer
    from app.analytics.enrichment_cache import AnalyzerCachePolicy
    from app.analytics.enrichment import EnrichmentStage
    from app.analytics.pipeline import AnalyticsPipeline
    from app.analytics import conversation_insertion as insertion
    class ContextSentiment(RuleBasedSentimentAnalyzer):
        cache_policy=AnalyzerCachePolicy(taxonomy_revision='context-test',preceding_messages=1,following_messages=1)
        def analyze_with_context(self,message,context):return self.analyze(message)
    f=small_fixture(tmp_path)
    try:
        f.pipeline=AnalyticsPipeline(f.source,projections=f.stores.projections,
            enrichment=EnrichmentStage(sentiment=ContextSentiment()),clock=lambda:NOW)
        f.pipeline.project_account(ACCOUNT)
        returns=[];original=insertion.try_insert
        def observe(*a,**k):
            value=original(*a,**k);returns.append(value);return value
        monkeypatch.setattr(insertion,'try_insert',observe)
        mutate(f);candidate=f.pipeline.build_candidate(ACCOUNT);f.pipeline.publish_candidate(candidate)
        assert all(v is None for v in returns)
        # The custom analyzer delegates to the same pure rule result.
        cold=AnalyticsPipeline(f.source,enrichment=f.pipeline.enrichment,clock=lambda:NOW,reuse_enrichment=False,reuse_conversations=False)
        expected=cold._build(ACCOUNT,f.source.account_read_model(ACCOUNT),projection_generation=candidate.artifact().projection.projection_generation)
        assert candidate.artifact()==expected
    finally:cleanup(f)


@pytest.mark.parametrize('recovered',[False,True])
def test_ordinary_lifecycle_reestablishes_safe_insertion_reuse(tmp_path,monkeypatch,recovered):
    from app.analytics.factory import create_analytics_stores
    from app.analytics.pipeline import AnalyticsPipeline
    from app.analytics import conversation_insertion as insertion
    f=small_fixture(tmp_path)
    try:
        if recovered:
            f.stores.projections.close_retention_scheduler()
            f.stores=create_analytics_stores('sqlite',projections_path=f.stores.database.path,
                activation=f.repositories.projection_activation,canonical_identity_reader=f.source.read_identity,
                retention_clock=lambda:NOW)
            f.pipeline=AnalyticsPipeline(f.source,projections=f.stores.projections,enrichment=f.pipeline.enrichment,clock=lambda:NOW)
        else:f.stores.projections._currentness.entries.clear()
        assert f.pipeline.prepare_questions(ACCOUNT,1)
        accepted=[];original=insertion.try_insert
        def observe(*a,**k):
            value=original(*a,**k);accepted.append(value is not None);return value
        monkeypatch.setattr(insertion,'try_insert',observe)
        mutate(f);candidate=f.pipeline.build_candidate(ACCOUNT);f.pipeline.publish_candidate(candidate)
        assert any(accepted)
        cold_equal(f,candidate.artifact())
    finally:cleanup(f)


def rehash(unit,rows,analyzers=None,**changes):
    from app.analytics.conversation_enrichment_units import _unit_id
    raw=b'\n'.join(rows);analyzer_raw=b'\n'.join(analyzers) if analyzers else b'[]'
    if analyzers is None:analyzer_raw=b'\n'.join(analyzer_records(unit)) or b'[]'
    h=replace(unit.header,**changes)
    h=replace(h,canonical_digest=hashlib.sha256(raw).hexdigest(),analyzer_digest=hashlib.sha256(analyzer_raw).hexdigest())
    h=replace(h,unit_id=_unit_id(h.canonical_digest,h.analyzer_digest,hashlib.sha256(_canonical(h.metrics.model_dump(mode='json'))).hexdigest(),h.message_count))
    return replace(unit,header=h,messages=_compress(raw),analyzers=_compress(analyzer_raw))


@pytest.mark.parametrize('fault',['ordinal','direction','classification','duplicate','account','timestamp','confidence_total','metrics','analyzer','config'])
def test_self_consistent_tampering_cannot_reuse_insertion_verification(tmp_path,fault):
    from app.analytics.conversation_enrichment_unit_sql import load_unit
    from app.analytics.conversation_enrichment_insertion import validate_inserted_unit
    f=small_fixture(tmp_path)
    try:
        previous_generation=f.stores.database.active_generation(ACCOUNT).generation_id
        with f.stores.database.read() as db:previous=load_unit(db,previous_generation,account_ref(ACCOUNT),conversation_ref(ACCOUNT,'chat-1'))
        mutate(f);candidate=f.pipeline.build_candidate(ACCOUNT)
        with f.stores.database.read() as db:
            unit=load_unit(db,candidate.reference.generation_id,account_ref(ACCOUNT),conversation_ref(ACCOUNT,'chat-1'))
            assert validate_inserted_unit(db,previous_generation,previous.header,unit,check=lambda:None)
            rows=list(message_records(unit));values=[json.loads(r) for r in rows];changes={};analyzers=None
            if fault=='ordinal':values[-1]['source_ordinal']-=1
            elif fault=='direction':values[-1]['direction']='inbound' if values[-1]['direction']=='outbound' else 'outbound'
            elif fault=='classification':values[-1]['sentiment']['score']=.123
            elif fault=='duplicate':values[-1]['message_ref']=values[0]['message_ref']
            elif fault=='account':values[-1]['account_ref']=account_ref('wrong')
            elif fault=='timestamp':values[-1]['sent_at']=(NOW-timedelta(days=2)).isoformat()
            elif fault=='confidence_total':changes['sentiment']=replace(unit.header.sentiment,numerator='0')
            elif fault=='metrics':changes['metrics']=unit.header.metrics.model_copy(update={'turn_count':99})
            elif fault=='config':changes['config_digest']='sha256:'+'0'*64
            else:
                analyzers=list(analyzer_records(unit));value=json.loads(analyzers[-1]);value['key']['message_ref']=message_ref(ACCOUNT,'chat-1','wrong');analyzers[-1]=_canonical(value)
            bad=rehash(unit,[_canonical(v) for v in values],analyzers,**changes)
            assert not validate_inserted_unit(db,previous_generation,previous.header,bad,check=lambda:None)
        f.pipeline.publish_candidate(candidate)
    finally:cleanup(f)



def test_invalid_retained_unit_is_not_relabelled_as_capacity_fallback(tmp_path,monkeypatch):
    from app.analytics import conversation_graph_units as units
    f=small_fixture(tmp_path)
    try:
        previous=f.stores.database.active_generation(ACCOUNT).generation_id
        monkeypatch.setattr(units,'MAX_GRAPH_UNIT_BYTES',1)
        mutate(f)
        with pytest.raises(ValueError,match='conversation_graph_unit_size_invalid'):
            f.pipeline.build_candidate(ACCOUNT)
        assert f.stores.database.active_generation(ACCOUNT).generation_id==previous
    finally:cleanup(f)


def test_cancelled_insertion_keeps_previous_generation_and_joins_heartbeat(tmp_path,monkeypatch):
    import threading
    from app.analytics import conversation_insertion as insertion
    f=small_fixture(tmp_path)
    try:
        previous=f.stores.database.active_generation(ACCOUNT).generation_id
        original=insertion.match_inserted_source
        def cancelled(*args):
            old_check=args[-1];calls=0
            def check():
                nonlocal calls
                old_check();calls+=1
                if calls==4:raise RuntimeError('insertion_cancelled')
            return original(*args[:-1],check)
        monkeypatch.setattr(insertion,'match_inserted_source',cancelled)
        mutate(f)
        with pytest.raises(RuntimeError,match='insertion_cancelled'):f.pipeline.build_candidate(ACCOUNT)
        assert f.stores.database.active_generation(ACCOUNT).generation_id==previous
        assert not any(t.name.startswith('graph-lease-') for t in threading.enumerate())
    finally:cleanup(f)


def test_expired_sources_are_not_reused(tmp_path,monkeypatch):
    from app.analytics import conversation_insertion as insertion
    f=small_fixture(tmp_path)
    try:
        f.clock.now=NOW+timedelta(days=91)
        accepted=[];original=insertion.try_insert
        def observe(*a,**k):
            value=original(*a,**k);accepted.append(value is not None);return value
        monkeypatch.setattr(insertion,'try_insert',observe)
        mutate(f,time=f.clock.now)
        result=f.pipeline.project_account(ACCOUNT)
        assert not any(accepted)
        assert len(result.artifact.projection.message_enrichments)==1
        cold_equal(f,result.artifact)
    finally:cleanup(f)


def test_source_change_after_private_build_cannot_publish(tmp_path):
    from app.analytics.errors import CanonicalRevisionChanged
    f=small_fixture(tmp_path)
    try:
        previous=f.stores.database.active_generation(ACCOUNT).generation_id
        mutate(f);candidate=f.pipeline.build_candidate(ACCOUNT)
        mutate(f,'tie-07')
        with pytest.raises(CanonicalRevisionChanged):f.pipeline.publish_candidate(candidate)
        assert f.stores.database.active_generation(ACCOUNT).generation_id==previous
    finally:cleanup(f)


def test_verified_classifications_survive_small_optional_analyzer_cache(tmp_path,monkeypatch):
    from app.analytics import enrichment_cache as cache
    monkeypatch.setattr(cache,'MAX_CACHE_ENTRIES',3)
    monkeypatch.setattr(cache,'MAX_CONVERSATION_CACHE_ENTRIES',3)
    f=small_fixture(tmp_path)
    try:
        before=[a.calls for a in f.analyzers]
        mutate(f);result=f.pipeline.project_account(ACCOUNT)
        assert [a.calls-n for a,n in zip(f.analyzers,before)]==[1,1,1]
        cold_equal(f,result.artifact)
    finally:cleanup(f)



def test_noncanonical_legal_timestamp_requires_complete_stored_validation(tmp_path,monkeypatch):
    from app.analytics import conversation_enrichment_unit_sql as storage
    from app.analytics.conversation_enrichment_insertion import validate_inserted_unit
    f=small_fixture(tmp_path)
    try:
        generation=f.stores.database.active_generation(ACCOUNT).generation_id
        account,chat=account_ref(ACCOUNT),conversation_ref(ACCOUNT,'chat-1')
        with f.stores.database.read() as db:old=storage.load_unit(db,generation,account,chat)
        mutate(f);candidate=f.pipeline.build_candidate(ACCOUNT)
        with f.stores.database.read() as db:
            new=storage.load_unit(db,candidate.reference.generation_id,account,chat)
            before=[json.loads(row) for row in message_records(old)]
            after=[json.loads(row) for row in message_records(new)]
            from datetime import datetime
            before[0]['sent_at']=datetime.fromisoformat(before[0]['sent_at']).timestamp()
            after[0]['sent_at']=before[0]['sent_at']
            old=rehash(old,[_canonical(v) for v in before]);new=rehash(new,[_canonical(v) for v in after])
            storage._validate_unit(old)
            storage._validate_unit(new)
            with monkeypatch.context() as patch:
                patch.setattr(storage,'load_unit',lambda *a,**k:old)
                assert validate_inserted_unit(db,generation,old.header,new,check=lambda:None) is False
        f.pipeline.publish_candidate(candidate)
    finally:cleanup(f)
