"""Proven source reuse and single-read validation retain independent checks."""
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
import json
import pytest
from tools import analytics_insertion_diagnostic as diagnostic
from app.analytics import enrichment_prefix as prefix
from app.analytics import conversation_insertion as insertion
from app.analytics import conversation_enrichment_unit_sql as sql
from app.analytics import conversation_enrichment_insertion as stored
from app.analytics import conversation_enrichment_units as units

pytestmark = [pytest.mark.ci_tier("integration")]

ACCOUNT='synthetic-continuous-owner'
CLOCK=datetime(2026,9,18,12,tzinfo=timezone.utc)

@pytest.fixture
def content():
    raw=diagnostic.raw_fixture(4000,CLOCK)
    old=diagnostic.full_unit(raw,ACCOUNT,CLOCK-timedelta(days=90))
    sql._validate_unit(old)
    db=diagnostic.component_database(old)
    try:yield raw,old,db
    finally:db.close()


def test_exact_source_digest_replaces_historical_model_parsing(content,monkeypatch):
    raw,old,_=content
    current=diagnostic.mutate_raw(raw,'insert')
    rows=units.message_records(old)
    expected=insertion.match_inserted_source(ACCOUNT,current,old.header,rows,lambda:None)
    calls=[];decode=prefix.json.loads
    def counted(value,*args,**kwargs):calls.append(1);return decode(value,*args,**kwargs)
    monkeypatch.setattr(prefix.json,'loads',counted)
    actual=insertion.match_proven_inserted_source(ACCOUNT,current,old.header,rows,lambda:None)
    assert actual==expected
    assert len(calls)<=insertion.PAGE_RECORDS
    assert len(calls)<old.header.message_count


@pytest.mark.parametrize('fault',['text','direction','time','ordinal','ordinal_type','participant','unread','duplicate','two_additions','deletion'])
def test_source_mutations_outside_suffix_cannot_reuse_prefix(content,fault):
    raw,old,_=content
    current=diagnostic.mutate_raw(raw,'insert')
    if fault=='text':current['messages'][0]['text']='Edited'
    elif fault=='direction':current['messages'][0]['direction']='outbound'
    elif fault=='time':current['messages'][0]['sent_at']=(CLOCK-timedelta(days=5)).isoformat()
    elif fault=='ordinal':current['messages'][0]['source_ordinal']=77
    elif fault=='ordinal_type':current['messages'][0]['source_ordinal']=False
    elif fault=='participant':current['platform_user_id']='different'
    elif fault=='unread':current['unread_count']=1
    elif fault=='duplicate':current['messages'][-2]['message_id']=current['messages'][0]['message_id']
    elif fault=='two_additions':current['messages'].append(dict(current['messages'][-2]))
    else:current['messages'].pop(0)
    rows=units.message_records(old)
    assert insertion.match_proven_inserted_source(ACCOUNT,current,old.header,rows,lambda:None) is None


@pytest.mark.parametrize('encoding',['spaced','escaped_key','negative_zero','duplicate','shadow','float','boolean','numeric_time'])
def test_batch_metadata_rule_matches_independent_json_admission(encoding):
    rows=[]
    for n in range(600):
        value=dict(source_ordinal=n,sent_at='2026-09-18T12:00:00+00:00',extra='ordinary')
        if encoding=='float' and n==1:value['source_ordinal']=1.0
        if encoding=='boolean' and n==1:value['source_ordinal']=True
        if encoding=='numeric_time' and n==1:value['sent_at']=1
        if encoding=='shadow':value['extra']={'source_ordinal':42,'sent_at':'other'}
        raw=json.dumps(value,separators=(',',':')).encode()
        if encoding=='spaced':raw=json.dumps(value,indent=None).encode()
        if encoding=='escaped_key':raw=raw.replace(b'source_ordinal',b'source_ordin\\u0061l')
        if encoding=='negative_zero' and n==0:raw=raw.replace(b':0,',b':-0,')
        if encoding=='duplicate':raw=raw[:-1]+b',"source_ordinal":'+str(n).encode()+b'}'
        rows.append(raw)
    expected=all(type((v:=json.loads(row))['source_ordinal']) is int and v['source_ordinal']==i
                 and isinstance(v['sent_at'],str) for i,row in enumerate(rows))
    assert prefix.ordinary_records(rows)==expected


def test_block_temporary_bounds_and_cancellation(monkeypatch):
    monkeypatch.setattr(prefix,'BLOCK_RECORDS',3);monkeypatch.setattr(prefix,'BLOCK_BYTES',300)
    rows=[json.dumps(dict(source_ordinal=i,sent_at='date',payload='x'*(400 if i==4 else 2)),separators=(',',':')).encode() for i in range(17)]
    original=prefix._ordinary_block;observed=[]
    def bounded(rows,start,check):
        observed.append((len(rows),sum(len(r)+1 for r in rows)))
        return original(rows,start,check)
    monkeypatch.setattr(prefix,'_ordinary_block',bounded)
    assert prefix.ordinary_records(rows)
    assert all(count<=3 and size<=300 for count,size in observed)
    def cancelled():raise RuntimeError('cancelled')
    with pytest.raises(RuntimeError,match='cancelled'):prefix.ordinary_records(rows,check=cancelled)


@pytest.mark.parametrize('operation',['append','insert'])
def test_stored_dispatch_reads_each_actual_frame_once(content,monkeypatch,operation):
    raw,old,db=content
    candidate=diagnostic.construct(old,diagnostic.mutate_raw(raw,operation),operation,ACCOUNT,lambda:None)
    previous_read=sql.load_unit;decode=units.message_frame;reads=[];frames=[]
    def load(*a,**k):reads.append(1);return previous_read(*a,**k)
    def frame(unit):frames.append(unit.header.unit_id);return decode(unit)
    monkeypatch.setattr(sql,'load_unit',load);monkeypatch.setattr(units,'message_frame',frame)
    for _ in range(2):
        assert sql.validate_one_added_unit(db,'previous',old.header,candidate,check=lambda:None)==operation
    assert len(reads)==2 and len(frames)==4
    assert frames==[old.header.unit_id,candidate.header.unit_id]*2
    assert sql._validate_appended_unit(db,'previous',old.header,candidate,check=lambda:None)==(operation=='append')
    if operation=='insert':assert stored.validate_inserted_unit(db,'previous',old.header,candidate,check=lambda:None)


def test_prior_success_does_not_cache_corrupt_predecessor(content,monkeypatch):
    raw,old,db=content
    candidate=diagnostic.construct(old,diagnostic.mutate_raw(raw,'insert'),'insert',ACCOUNT,lambda:None)
    assert sql.validate_one_added_unit(db,'previous',old.header,candidate,check=lambda:None)=='insert'
    broken=replace(old,messages=old.messages[:-1]+bytes([old.messages[-1]^1]))
    monkeypatch.setattr(sql,'load_unit',lambda *a,**k:broken)
    with pytest.raises(ValueError):sql.validate_one_added_unit(db,'previous',old.header,candidate,check=lambda:None)


def test_copied_header_or_generation_mismatch_does_not_authorize(content):
    raw,old,db=content
    candidate=diagnostic.construct(old,diagnostic.mutate_raw(raw,'insert'),'insert',ACCOUNT,lambda:None)
    assert sql.validate_one_added_unit(db,'absent-generation',old.header,candidate,check=lambda:None) is None
    assert sql.validate_one_added_unit(db,'previous',replace(old.header,input_digest='sha256:'+'0'*64),candidate,check=lambda:None) is None


def test_actual_reader_rejects_copied_units_closed_scope_and_missing_proofs(tmp_path):
    from tests.continuous_analytics_fixture import make_fixture,insert_message,advance,cleanup,NOW
    from app.analytics.conversation_sql import fragment_reader
    from app.analytics.opaque_refs import conversation_ref
    f=make_fixture(tmp_path,conversations=3,messages=0)
    try:
        with f.repositories.database.transaction() as db:
            for i in range(1100):
                insert_message(db,'chat-1',f'bound-{i*2:06}',NOW-timedelta(microseconds=1),2)
            insert_message(db,'chat-0','other-0',NOW-timedelta(days=1),2)
            insert_message(db,'chat-2','other-2',NOW-timedelta(days=1),2)
        f.pipeline.project_account(ACCOUNT)
        with f.repositories.database.transaction() as db:
            insert_message(db,'chat-1','bound-002197',NOW-timedelta(microseconds=1),2)
            advance(db)
        raw=f.source.conversation_read_model(ACCOUNT,'chat-1')
        with fragment_reader(f.stores.projections,ACCOUNT) as reader:
            unit=reader.previous_enrichment_unit(conversation_ref(ACCOUNT,'chat-1'))
            assert unit is not None
            assert reader.matched_enrichment_source(raw,unit,lambda:None) is not None
            assert reader.matched_enrichment_source(raw,replace(unit),lambda:None) is None
            assert reader.matched_enrichment_source(raw,replace(unit,messages=b'forged'),lambda:None) is None
            altered=dict(raw,platform_user_id='different')
            assert reader.matched_enrichment_source(altered,unit,lambda:None) is None
            def cancelled():raise RuntimeError('cancelled')
            with pytest.raises(RuntimeError,match='cancelled'):
                reader.matched_enrichment_source(raw,unit,cancelled)
            assert reader.previous_enrichment_unit(conversation_ref(ACCOUNT,'missing')) is None
            assert reader.matched_enrichment_source(raw,unit,lambda:None) is None
            unit=reader.previous_enrichment_unit(conversation_ref(ACCOUNT,'chat-1'))
            assert reader.matched_enrichment_source(raw,unit,lambda:None) is not None
        assert reader.matched_enrichment_source(raw,unit,lambda:None) is None
        assert reader.previous_enrichment_unit(conversation_ref(ACCOUNT,'chat-1')) is None
        f.stores.projections._conversation_enrichment_proofs.clear()
        with fragment_reader(f.stores.projections,ACCOUNT) as reader:
            assert not hasattr(reader,'matched_enrichment_source')
    finally:cleanup(f)


def test_large_scheduled_construction_uses_verified_prefix_and_remains_equal(tmp_path,monkeypatch):
    from tests.continuous_analytics_fixture import make_fixture,insert_message,advance,cleanup,NOW,cold_equal
    f=make_fixture(tmp_path,conversations=3,messages=0)
    calls=[];match=insertion.match_proven_inserted_source
    def observed(*a,**k):
        result=match(*a,**k);calls.append(result is not None);return result
    monkeypatch.setattr(insertion,'match_proven_inserted_source',observed)
    try:
        with f.repositories.database.transaction() as db:
            for i in range(1100):insert_message(db,'chat-1',f'bound-{i*2:06}',NOW-timedelta(microseconds=1),2)
            insert_message(db,'chat-0','other-0',NOW-timedelta(days=1),2)
            insert_message(db,'chat-2','other-2',NOW-timedelta(days=1),2)
        f.pipeline.project_account(ACCOUNT)
        with f.repositories.database.transaction() as db:
            insert_message(db,'chat-1','bound-002197',NOW-timedelta(microseconds=1),2);advance(db)
        candidate=f.pipeline.build_candidate(ACCOUNT)
        assert calls==[True]
        f.pipeline.publish_candidate(candidate)
        cold_equal(f,candidate.artifact())
    finally:cleanup(f)


@pytest.mark.parametrize('shifted',[1,2,127,128,129])
def test_bound_match_preserves_the_exact_suffix_limit(shifted):
    raw=diagnostic.raw_fixture(600,CLOCK)
    count=len(raw['messages'])
    at=raw['messages'][-1]['sent_at']
    for i,row in enumerate(raw['messages']):
        row['message_id']=f'ordered-{i:06}-b'
        row['sent_at']=at
    old=diagnostic.full_unit(raw,ACCOUNT,CLOCK-timedelta(days=90))
    sql._validate_unit(old)
    index=count-shifted
    rows=[dict(row) for row in raw['messages']]
    rows.insert(index,dict(rows[index],message_id=f'ordered-{index:06}-a'))
    rows=[dict(row,source_ordinal=i) for i,row in enumerate(rows)]
    current=dict(raw,messages=rows)
    prior=units.message_records(old)
    original=insertion.match_inserted_source(ACCOUNT,current,old.header,prior,lambda:None)
    bound=insertion.match_proven_inserted_source(ACCOUNT,current,old.header,prior,lambda:None)
    assert bound==original
    if shifted<=insertion.PAGE_RECORDS:
        assert bound is not None and bound[0]==index
    else:
        assert bound is None


def test_validation_dispatch_rechecks_cancellation_after_frame_reads(content,monkeypatch):
    raw,old,db=content
    candidate=diagnostic.construct(old,diagnostic.mutate_raw(raw,'insert'),'insert',ACCOUNT,lambda:None)
    decode=units.message_frame;reads=[]
    def frame(value):
        reads.append(value.header.unit_id)
        return decode(value)
    def check():
        if len(reads)==2:raise RuntimeError('cancelled_after_reads')
    monkeypatch.setattr(units,'message_frame',frame)
    with pytest.raises(RuntimeError,match='cancelled_after_reads'):
        sql.validate_one_added_unit(db,'previous',old.header,candidate,check=check)
    assert reads==[old.header.unit_id,candidate.header.unit_id]
