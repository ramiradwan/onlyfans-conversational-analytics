"""Graph membership reuse is bounded and checked independently of construction."""
from contextvars import ContextVar
from dataclasses import replace
from types import SimpleNamespace
import hashlib
import json
import zlib

import pytest
from app.analytics import shared_graph
from app.analytics import graph_membership_selection as selection
from app.analytics.opaque_refs import account_ref, conversation_ref
from tests.continuous_analytics_fixture import ACCOUNT, cleanup, cold_equal
from tests.test_tied_insertion_safety import small_fixture, mutate

pytestmark = [pytest.mark.ci_tier("integration")]


@pytest.fixture
def fixture(tmp_path):
    f = small_fixture(tmp_path)
    try: yield f
    finally: cleanup(f)


def test_tie_construction_avoids_generic_membership_sql(fixture, monkeypatch):
    from app.analytics import conversation_graph_insertion as insertion
    active = ContextVar('construction', default=False)
    original, lookup = insertion.replace_suffix, shared_graph.selected_content_ids
    def build(*a, **k):
        token = active.set(True)
        try: return original(*a, **k)
        finally: active.reset(token)
    def sql(*a, **k):
        assert not active.get(), 'generic membership query inside admitted insertion'
        return lookup(*a, **k)
    monkeypatch.setattr(insertion, 'replace_suffix', build)
    monkeypatch.setattr(shared_graph, 'selected_content_ids', sql)
    mutate(fixture)
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    fixture.pipeline.publish_candidate(candidate)
    cold_equal(fixture, candidate.artifact())


def test_changed_chunks_are_consumed_once_and_selection_is_released(fixture, monkeypatch):
    from app.analytics import conversation_append, conversation_integrity_store
    active = ContextVar('changed_stream', default=False)
    calls, consumed, selections, snapshots = [], [], [], []
    original, spans = shared_graph._verified_changed_segment_chunks, conversation_append._checked_record_spans
    def stream(*a, **k):
        tagged = active.get()
        if tagged: calls.append((a[0].kind, a[0].segment_id))
        yield from spans(*a, **k)
        if tagged: consumed.append((a[0].kind, a[0].segment_id))
    def verify(*a, **k):
        token = active.set(True)
        try: value = original(*a, **k)
        finally: active.reset(token)
        if value is not None:
            snapshots.append(len(value.segments))
            if value.membership_selection is not None:
                selections.append(value.membership_selection)
                assert value.membership_selection.ready
        return value
    monkeypatch.setattr(conversation_append, '_checked_record_spans', stream)
    monkeypatch.setattr(shared_graph, '_verified_changed_segment_chunks', verify)
    mutate(fixture)
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    fixture.pipeline.publish_candidate(candidate)
    assert len(calls) == sum(snapshots) and calls == consumed
    assert selections and all(not x.ready and not x.values for x in selections)
    cold_equal(fixture, candidate.artifact())


def test_loader_cannot_authorize_a_copied_unit_or_outlive_scope(fixture):
    with fixture.stores.projections.open_conversation_fragments(ACCOUNT) as load:
        unit = load.previous_graph_unit(conversation_ref(ACCOUNT, 'chat-1'))
        assert load.insertion_graph_groups(unit) is not None
        assert load.insertion_graph_groups(replace(unit)) is None
    assert load.insertion_graph_groups(unit) is None
    with pytest.raises(ValueError, match='conversation_reader_closed'):
        load.graph_segment_chunk('node', '00')


@pytest.mark.parametrize('fault', ['truncated_stream', 'false_membership', 'write_after_integrity'])
def test_incomplete_or_altered_stored_verification_cannot_publish(fixture, monkeypatch, fault):
    from app.analytics import conversation_append, conversation_integrity_store as integrity
    before = fixture.stores.database.active_generation(ACCOUNT).generation_id
    if fault == 'truncated_stream':
        active = ContextVar('stream', default=False)
        original = shared_graph._verified_changed_segment_chunks
        spans = conversation_append._checked_record_spans
        def verify(*a, **k):
            token = active.set(True)
            try: return original(*a, **k)
            finally: active.reset(token)
        def stream(*a, **k):
            if not active.get():
                yield from spans(*a, **k); return
            previous = None
            for row in spans(*a, **k):
                if previous is not None: yield previous
                previous = row
        monkeypatch.setattr(shared_graph, '_verified_changed_segment_chunks', verify)
        monkeypatch.setattr(conversation_append, '_checked_record_spans', stream)
    elif fault == 'false_membership':
        original = selection.MembershipSelection.selected
        def wrong(*a, **k):
            value = original(*a, **k)
            if value: value[next(iter(value))] = '0'*64
            return value
        monkeypatch.setattr(selection.MembershipSelection, 'selected', wrong)
    else:
        original = integrity.verify_generation_integrity
        def write(db, *a, **k):
            value = original(db, *a, **k)
            if k.get('graph_validation') is not None:
                db.execute("UPDATE projection_generations SET lease_expires_at=lease_expires_at WHERE status='building'")
            return value
        monkeypatch.setattr(integrity, 'verify_generation_integrity', write)
    mutate(fixture)
    from app.analytics.graph_store import GraphReferentialIntegrityError
    from app.analytics.sqlite_projection_store import ProjectionValidationError
    with pytest.raises((ValueError, GraphReferentialIntegrityError, ProjectionValidationError)):
        fixture.pipeline.build_candidate(ACCOUNT)
    assert fixture.stores.database.active_generation(ACCOUNT).generation_id == before


@pytest.mark.parametrize('limit', ['rows', 'bytes', 'no_hint', 'missing_proof'])
def test_absent_or_over_capacity_selection_retains_correct_fallback(fixture, monkeypatch, limit):
    if limit == 'rows': monkeypatch.setattr(shared_graph, 'MAX_CHANGED_VERIFICATION_ROWS', 1)
    elif limit == 'bytes': monkeypatch.setattr(shared_graph, 'MAX_CHANGED_VERIFICATION_BYTES', 1)
    elif limit == 'no_hint': monkeypatch.setattr(selection, 'prepare_selection', lambda *a: None)
    else: fixture.stores.projections._conversation_graph_proofs.clear()
    mutate(fixture)
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    fixture.pipeline.publish_candidate(candidate)
    cold_equal(fixture, candidate.artifact())


def test_selection_is_bound_to_owner_generation_account_and_writes(monkeypatch):
    monkeypatch.setattr(selection, 'content_stamp', lambda c: ('store','schema',1,2))
    db = SimpleNamespace(total_changes=0,in_transaction=True)
    generation = dict(generation_id='candidate', creator_account_id='account')
    proof = object()
    value = selection.MembershipSelection(db,generation,'account',proof)
    value.request('node','segment','g1:'+'0'*64)
    value.observe('node','segment','g1:'+'0'*64,'1'*64)
    assert value.selected('node','segment',['g1:'+'0'*64]) is None
    value.finish()
    assert value.matches(db,generation,'account',proof)
    assert not value.matches(SimpleNamespace(total_changes=0,in_transaction=True),generation,'account',proof)
    assert not value.matches(db,dict(generation,generation_id='other'),'account',proof)
    assert not value.matches(db,generation,'other',proof)
    assert not value.matches(db,generation,'account',object())
    db.total_changes += 1
    assert not value.matches(db,generation,'account',proof)
    db.total_changes = 0; db.in_transaction = False
    assert not value.matches(db,generation,'account',proof)
    value.discard()
    assert not value.values and not value.ready


@pytest.mark.parametrize('limit', ['rows','bytes'])
def test_selection_storage_refuses_to_grow_past_original_bounds(monkeypatch,limit):
    monkeypatch.setattr(selection,'content_stamp',lambda c:('store','schema',1,2))
    monkeypatch.setattr(shared_graph,'MAX_CHANGED_VERIFICATION_ROWS',2 if limit=='rows' else 100)
    monkeypatch.setattr(shared_graph,'MAX_CHANGED_VERIFICATION_BYTES',2000 if limit=='rows' else 1)
    value=selection.MembershipSelection(SimpleNamespace(total_changes=0,in_transaction=True),
        {'generation_id':'g'},'account',object())
    for i in range(3): value.request('node','segment','g1:'+f'{i:064x}')
    assert value.discarded and value.rows==0 and value.bytes==0 and not value.values


@pytest.mark.parametrize('kind', ['node','edge'])
def test_streamed_hints_match_selected_buckets_and_remain_bounded(monkeypatch,kind):
    monkeypatch.setattr(selection,'content_stamp',lambda c:('store','schema',1,2))
    value=selection.MembershipSelection(SimpleNamespace(total_changes=0,in_transaction=True),
        {'generation_id':'g'},'account',object())
    prefix='g1:' if kind=='node' else 'e1:'
    ids=sorted(prefix+hashlib.sha256(str(i).encode()).hexdigest() for i in range(9000))
    data=zlib.compress(json.dumps(ids,separators=(',',':')).encode(),1)
    wanted={(kind,'00'):'segment-00',(kind,'ff'):'segment-ff'}
    selection._request_frame(value,kind,data,len(ids),wanted,lambda:None)
    actual=set().union(*(set(v) for v in value.values.values()))
    assert actual=={k for k in ids if k[3:5] in ('00','ff')}
    assert not value.ready and value.rows==len(actual)
    assert value.bytes<=value.max_bytes


@pytest.mark.parametrize('damage',['truncated','trailing','oversized','bad_count','cancelled'])
def test_malformed_hint_stream_never_authorizes_data(monkeypatch,damage):
    monkeypatch.setattr(selection,'content_stamp',lambda c:('store','schema',1,2))
    value=selection.MembershipSelection(SimpleNamespace(total_changes=0,in_transaction=True),
        {'generation_id':'g'},'account',object())
    data=zlib.compress(json.dumps(['g1:'+'0'*64],separators=(',',':')).encode())
    if damage=='truncated':data=data[:-1]
    elif damage=='trailing':data+=b'junk'
    elif damage=='oversized':data=zlib.compress(b' '*100000)
    def check():
        if damage=='cancelled':raise RuntimeError('cancelled')
    if damage=='cancelled':
        with pytest.raises(RuntimeError):selection._request_frame(value,'node',data,1,{('node','00'):'s'},check)
    else:
        selection._request_frame(value,'node',data,-1 if damage=='bad_count' else 1,{('node','00'):'s'},check)
        assert value.discarded
    assert not value.ready


def test_rehashed_reordered_candidate_is_not_proven_by_old_group(fixture):
    from app.analytics.conversation_integrity import decode_manifest, encode_manifest, membership_digest, groups_for_members
    from app.analytics.conversation_graph_units import graph_unit_ids, unit_id
    with fixture.stores.projections.open_conversation_fragments(ACCOUNT) as load:
        unit=load.previous_graph_unit(conversation_ref(ACCOUNT,'chat-1'))
    old=decode_manifest(unit); nodes,edges=graph_unit_ids(unit)
    # A small fixture may have one ID per bucket; reverse the entire ordered
    # selection and recompute its unit ID so the order check cannot rely on it.
    bad_nodes=tuple(reversed(nodes))
    bad=replace(unit,node_ids=zlib.compress(json.dumps(bad_nodes,separators=(',',':')).encode()),
        header=replace(unit.header,unit_id=unit_id(unit.header.graph_digest,bad_nodes,edges,checksum_version=2)))
    with pytest.raises(ValueError):groups_for_members(bad,proven_summaries=old)
    good,members=groups_for_members(unit,proven_summaries=old)
    assert good==old
    assert sum(len(v) for k,v in members.items() if k[0]=='node')==len(nodes)



def test_small_conversation_does_not_open_account_bucket_chunks(tmp_path,monkeypatch):
    from tests.test_dominant_append_reuse import dominant_fixture
    from app.analytics.conversation_graph_units import graph_unit_ids
    f=dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        with f.stores.projections.open_conversation_fragments(ACCOUNT) as load:
            unit=load.previous_graph_unit(conversation_ref(ACCOUNT,'chat-1'))
            nodes,_=graph_unit_ids(unit)
            expected=load.graph_content_ids('node',nodes)
            def forbidden(*a,**k):raise AssertionError('small conversation read a whole account chunk')
            monkeypatch.setattr('app.analytics.conversation_append.verified_chunk_content_ids',forbidden)
            assert load.insertion_graph_content_ids('node',nodes)==expected
    finally:cleanup(f)


def test_missing_coverage_releases_buffer_before_fallback(fixture,monkeypatch):
    original=selection.MembershipSelection.selected
    seen=[]
    def absent(self,*a):
        seen.append(self)
        return None
    monkeypatch.setattr(selection.MembershipSelection,'selected',absent)
    mutate(fixture)
    candidate=fixture.pipeline.build_candidate(ACCOUNT)
    fixture.pipeline.publish_candidate(candidate)
    assert seen and all(s.discarded and not s.values for s in seen)
    cold_equal(fixture,candidate.artifact())
